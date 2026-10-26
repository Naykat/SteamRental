import asyncio
import base64
import binascii
import json
import logging
import re
import time

from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

import config
import database as db
import item_images
import keyboards as kb
import rentals
import steam_guard
from playerok_client import client, load_credentials, save_credentials
from utils import esc, fmt_ts, fmt_left, list_price, mask, parse_cookies

logger = logging.getLogger(__name__)
router = Router()
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))


class AddItem(StatesGroup):
    login = State()
    password = State()
    mafile = State()
    title = State()
    description = State()
    price = State()
    hours = State()
    prime = State()
    images = State()
    confirm = State()


class EditItem(StatesGroup):
    value = State()
    images = State()




def _valid_secret(secret: str) -> bool:
    try:
        return len(base64.b64decode(secret, validate=True)) == 20
    except (binascii.Error, ValueError):
        return False


# ---------- Меню ----------

async def _menu_text() -> str:
    accounts = await db.get_accounts()
    busy = sum(1 for a in accounts if a.get("rental_id"))
    disabled = sum(1 for a in accounts if not a["enabled"] and not a.get("rental_id"))
    free = len(accounts) - busy - disabled
    pending = await db.get_pending_deals()
    if not client.acc:
        playerok = "🔴 вход не выполнен — ⚙️ Настройки → Вход в Playerok"
    elif not client.auth_ok:
        playerok = "🔴 cookies не работают — ⚙️ Настройки → Вход в Playerok"
    else:
        playerok = f"🟢 {esc(client.acc.username)}"
    text = (
        "🎮 <b>Аренда Steam CS2</b>\n\n"
        f"Товары: 🟢 {free} свободно · 🔴 {busy} в аренде · ⏸ {disabled} выкл.\n"
        f"Playerok: {playerok}"
    )
    if pending:
        text += f"\n\n⚠️ Ждут свободный аккаунт: {len(pending)} — см. 📊 Аренды"
    if config.TEST_RENTAL_MINUTES:
        text += f"\n\n🧪 Тестовый режим: аренда {config.TEST_RENTAL_MINUTES} мин"
    return text


async def _main_menu_kb():
    return kb.main_menu(len(await db.get_accounts()))


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await _menu_text(), parse_mode="HTML", reply_markup=await _main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(await _menu_text(), parse_mode="HTML", reply_markup=await _main_menu_kb())


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Отменено.", reply_markup=kb.back("menu", "⬅️ Меню"))


# ---------- Список и карточка аккаунта ----------

@router.message(Command("accounts"))
async def cmd_accounts(message: Message):
    accounts = await db.get_accounts()
    await message.answer(_accounts_text(accounts), parse_mode="HTML", reply_markup=kb.accounts_list(accounts))


@router.callback_query(F.data == "accounts")
async def cb_accounts(call: CallbackQuery, state: FSMContext):
    await state.clear()
    accounts = await db.get_accounts()
    try:
        await call.message.edit_text(_accounts_text(accounts), parse_mode="HTML",
                                     reply_markup=kb.accounts_list(accounts))
    except Exception:
        pass
    await call.answer()


def _accounts_text(accounts: list[dict]) -> str:
    if not accounts:
        return "Товаров пока нет. Нажмите «🆕 Новый товар»."
    return (
        f"📋 <b>Товары ({len(accounts)})</b>\n\n"
        "🟢 свободен · 🔴 в аренде · ⏸ выключен · ⛔ пароль не сменён"
    )


async def _account_card_text(account: dict, rental: dict | None) -> str:
    icon = kb.account_status_icon({**account, "rental_id": rental and rental["id"]})
    lines = [f"{icon} <b>{esc(account['login'])}</b>"]
    title = account.get("title") or account.get("note")
    if title:
        details = [esc(title)]
        if account.get("price"):
            details.append(f"{account['price']} ₽")
        if account.get("rental_hours"):
            details.append(f"{account['rental_hours']} ч")
        lines.append(" · ".join(details))
    lines.append(f"Пароль: <code>{esc(account['password'])}</code>")
    if rental:
        left = rental["ends_at"] - time.time()
        lines += [
            "",
            f"🔴 <b>В аренде</b> у {esc(rental['buyer_username'])} — осталось {fmt_left(left)}",
            f"До {fmt_ts(rental['ends_at'])} МСК · {rental['price']} ₽ · {rental['hours']} ч",
        ]
    elif account.get("needs_rotation"):
        lines += ["", "⛔ Пароль после аренды не сменён — товар выключен и не выставлен. "
                      "Бот повторяет попытку сам, или нажмите «🔁 Повторить смену пароля»."]
    elif not account["enabled"]:
        lines += ["", "⏸ Выключен — не выдаётся покупателям"]
    else:
        lines += ["", "🟢 Свободен"]
    return "\n".join(lines)


async def _show_account(call: CallbackQuery, account_id: int, notice: str | None = None):
    account = await db.get_account(account_id)
    if not account:
        await call.answer("Товар не найден", show_alert=True)
        return
    rental = await db.get_active_rental_for_account(account_id)
    text = await _account_card_text(account, rental)
    if notice:
        text = f"{notice}\n\n{text}"
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.account_card(account, rental))
    except Exception:
        pass


@router.callback_query(F.data.startswith("acc:"))
async def cb_account(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_account(call, int(call.data.split(":")[1]))
    await call.answer()


@router.callback_query(F.data.startswith("code:"))
async def cb_code(call: CallbackQuery):
    account = await db.get_account(int(call.data.split(":")[1]))
    if not account:
        return await call.answer("Товар не найден", show_alert=True)
    code = steam_guard.generate_code(account["shared_secret"])
    left = 30 - int(time.time()) % 30
    await call.message.answer(f"🔑 <code>{code}</code> ({esc(account['login'])}, ещё ~{left} сек)",
                              parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("ext:"))
async def cb_extend(call: CallbackQuery):
    _, aid, minutes = call.data.split(":")
    rental = await db.get_active_rental_for_account(int(aid))
    if not rental:
        return await call.answer("Нет активной аренды", show_alert=True)
    await db.extend_rental(rental["id"], int(minutes) * 60)
    await call.answer(f"Продлено на {minutes} мин")
    await _show_account(call, int(aid))


@router.callback_query(F.data.startswith("end:"))
async def cb_end(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=kb.confirm("end", aid))
    await call.answer("Завершить аренду досрочно?")


@router.callback_query(F.data.startswith("end!:"))
async def cb_end_confirm(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    active = await db.get_active_rental_for_account(aid)
    if not active:
        return await call.answer("Нет активной аренды", show_alert=True)
    await call.answer("Завершаю...")
    rental = await db.get_rental(active["id"])
    await rentals.finish_rental(rental, "finished", True, "Аренда завершена вручную")
    await _show_account(call, aid, "⏹ Аренда завершена")


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    account = await db.get_account(aid)
    if not account:
        return await call.answer("Товар не найден", show_alert=True)
    await db.update_account(aid, enabled=0 if account["enabled"] else 1)
    if not account["enabled"]:
        await rentals.process_pending_deals()
    await call.answer("Готово")
    await _show_account(call, aid)


@router.callback_query(F.data.startswith("rotate:"))
async def cb_rotate(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=kb.confirm("rotate", aid))
    await call.answer("Сменить пароль в Steam?")


@router.callback_query(F.data.startswith("rotate!:"))
async def cb_rotate_confirm(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    await call.answer("Меняю пароль, это может занять пару минут...")
    account = await db.get_account(aid)
    if account and account.get("needs_rotation"):
        # После аренды смена не прошла — повтор целиком: пароль, новый лот, удаление старого
        return await _show_account(call, aid, await rentals.retry_rotation(aid))
    result = await rentals._rotate_password(aid)
    if "Пароль сменён" in result:
        result += "\n" + await _apply_lot_changes(aid, password=True)
    await _show_account(call, aid, result.strip() or "Готово")


@router.callback_query(F.data.startswith("rotretry:"))
async def cb_rotation_retry(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    await call.answer("Меняю пароль, это может занять пару минут...")
    text = await rentals.retry_rotation(aid)
    await call.message.answer(text or "Готово", parse_mode="HTML", disable_web_page_preview=True,
                              reply_markup=kb.back(f"acc:{aid}", "📋 Товар"))


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=kb.confirm("del", aid))
    await call.answer("Удалить товар?")


@router.callback_query(F.data.startswith("del!:"))
async def cb_delete_confirm(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    if await db.get_active_rental_for_account(aid):
        return await call.answer("Сначала завершите аренду", show_alert=True)
    await db.delete_account(aid)
    item_images.remove(aid)
    await call.answer("Удалён")
    accounts = await db.get_accounts()
    await call.message.edit_text(_accounts_text(accounts), parse_mode="HTML",
                                 reply_markup=kb.accounts_list(accounts))


# ---------- Редактирование товара ----------

MAX_IMAGES = 5
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# Поля, которые меняют сам лот на Playerok
# Поля, которые меняют сам лот на Playerok (логин и пароль — через комментарий лота)
LOT_FIELDS = {"title", "description", "price", "rental_hours", "prime", "images", "login", "password"}


def _parse_mafile(raw: str, login: str) -> tuple[str, str | None] | str:
    """Возвращает (shared_secret, identity_secret) или текст ошибки."""
    try:
        mafile = json.loads(raw)
        shared_secret = mafile["shared_secret"]
    except Exception:
        return "Не удалось прочитать maFile: нужен JSON с полем shared_secret."
    account_name = mafile.get("account_name")
    if account_name and account_name.lower() != login.lower():
        return (f"Этот maFile от аккаунта <code>{esc(account_name)}</code>, а логин — "
                f"<code>{esc(login)}</code>. Отправьте maFile этого аккаунта.")
    if not _valid_secret(shared_secret):
        return "shared_secret в maFile некорректный."
    identity_secret = mafile.get("identity_secret") or None
    if not identity_secret or not _valid_secret(identity_secret):
        return ("В maFile нет корректного identity_secret. Он нужен, чтобы бот менял пароль "
                "после каждой аренды — отправьте полный maFile.")
    return shared_secret, identity_secret


async def _fill_from_lot(account: dict) -> dict:
    """Для товаров без сохранённых данных подтягивает их из текущего лота Playerok."""
    if account.get("title") or not account.get("last_item_id") or not client.acc:
        return account
    try:
        lot = await client.get_lot(account["last_item_id"])
    except Exception:
        return account
    if not lot:
        return account
    attributes = getattr(lot, "attributes", None) or {}
    fields = {
        "title": lot.name,
        "description": lot.description,
        "price": lot.price or getattr(lot, "raw_price", None),
    }
    if "prime-status" in attributes:
        fields["prime"] = 1 if attributes["prime-status"] else 0
    if not account.get("rental_hours") and str(attributes.get("amount", "")).isdigit():
        fields["rental_hours"] = int(attributes["amount"])
    await db.update_account(account["id"], **fields)
    return await db.get_account(account["id"])


async def _edit_menu_text(account: dict) -> str:
    def val(v, suffix=""):
        return "—" if v in (None, "") else f"{esc(v)}{suffix}"
    prime = "—" if account.get("prime") is None else ("да" if account["prime"] else "нет")
    description = account.get("description") or ""
    if len(description) > 300:
        description = description[:300] + "…"
    lot = account.get("last_item_id")
    return (
        f"✏️ <b>Редактирование товара</b> <code>{esc(account['login'])}</code>\n\n"
        f"📝 Название: {val(account.get('title'))}\n"
        f"💰 Цена: {val(account.get('price'), ' ₽')}\n"
        f"⏱ Срок: {val(account.get('rental_hours'), ' ч')}\n"
        f"⭐ Prime: {prime}\n"
        f"🖼 Картинок: {item_images.count(account['id']) or 'как в лоте'}\n"
        f"🔗 Лот: {f'ID {esc(lot)}' if lot else '—'}\n\n"
        f"📄 Описание:\n{esc(description) or '—'}\n\n"
        "Изменения сразу применяются к выставленному лоту, а если товар в аренде — к следующему."
    )


async def _show_edit_menu(target: Message, account_id: int, notice: str | None = None, edit: bool = True):
    account = await db.get_account(account_id)
    if not account:
        return
    account = await _fill_from_lot(account)
    text = await _edit_menu_text(account)
    if notice:
        text = f"{notice}\n\n{text}"
    markup = kb.item_edit(account, item_images.count(account_id))
    if edit:
        try:
            return await target.edit_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        except Exception:
            pass
    await target.answer(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)


async def _apply_lot_changes(account_id: int, **changed) -> str:
    """Обновляет выставленный лот на Playerok. Возвращает строку с результатом."""
    account = await db.get_account(account_id)
    if not account or not account.get("last_item_id"):
        return "💾 Сохранено (лот ещё не выставлен)"
    if not client.acc:
        return "💾 Сохранено. Лот не обновлён: нет входа в Playerok"
    kwargs = {}
    if "title" in changed:
        kwargs["name"] = account["title"]
    if "description" in changed:
        kwargs["description"] = account["description"]
    if "price" in changed:
        kwargs["price"] = account["price"]
    if "rental_hours" in changed or "prime" in changed:
        kwargs["attributes"] = rentals.lot_attributes(account)
    if "images" in changed:
        kwargs["images"] = item_images.load(account_id)
    if "login" in changed or "password" in changed:
        kwargs["comment"] = rentals.item_comment(account)
    try:
        result = await client.update_lot(account["last_item_id"], **kwargs)
    except Exception as e:
        logger.exception("[edit] ошибка обновления лота")
        return f"💾 Сохранено в боте, но лот на Playerok не обновился: <code>{esc(e)[:300]}</code>"
    if not result.get("updated"):
        return f"💾 Сохранено. {esc(result.get('reason', '').capitalize())}"
    note = "✅ Сохранено и лот на Playerok обновлён"
    if result.get("status") in ("PENDING_MODERATION", "PENDING_APPROVAL"):
        note += " (ушёл на проверку модерации)"
    return note


EDIT_PROMPTS = {
    "title": "📝 Отправьте новое <b>название</b> (10–100 символов).",
    "description": "📄 Отправьте новое <b>описание</b> (от 10 символов).",
    "price": "💰 Отправьте новую <b>цену</b> в рублях (от 90).",
    "rental_hours": "⏱ Отправьте <b>срок аренды</b> в часах (1–168).",
    "login": "👤 Отправьте новый <b>логин</b> Steam.",
    "password": "🔑 Отправьте новый <b>пароль</b> (сохраняется только в боте, в Steam не меняется).",
    "mafile": "📁 Отправьте новый <b>.maFile</b> файлом или текстом.",
    "lot": "🔗 Отправьте привязку к лотам Playerok через запятую: ID лота, ссылку на лот или часть названия.",
    "images": f"🖼 Отправьте новые <b>картинки</b> (до {MAX_IMAGES}) и нажмите «Готово» — старые будут заменены.",
}


@router.callback_query(F.data.startswith("pedit:"))
async def cb_item_edit(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await _show_edit_menu(call.message, int(call.data.split(":")[1]))


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(call: CallbackQuery, state: FSMContext):
    _, aid, field = call.data.split(":")
    if field not in EDIT_PROMPTS:
        return await call.answer()
    await state.set_state(EditItem.images if field == "images" else EditItem.value)
    await state.update_data(account_id=int(aid), field=field, images=[])
    markup = kb.images_done(0) if field == "images" else kb.back(f"pedit:{aid}", "❌ Отмена")
    await call.message.answer(EDIT_PROMPTS[field], parse_mode="HTML", reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("prime_toggle:"))
async def cb_prime_toggle(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    account = await db.get_account(aid)
    if not account:
        return await call.answer("Товар не найден", show_alert=True)
    await db.update_account(aid, prime=0 if account.get("prime") else 1)
    await call.answer("Обновляю лот...")
    await _show_edit_menu(call.message, aid, await _apply_lot_changes(aid, prime=True))


async def _save_edit(message: Message, state: FSMContext, field: str, value):
    data = await state.get_data()
    aid = data["account_id"]
    await db.update_account(aid, **{field: value})
    await state.clear()
    if field in LOT_FIELDS:
        notice = await _apply_lot_changes(aid, **{field: True})
    else:
        notice = "✅ Сохранено"
    if field == "lot":
        await rentals.process_pending_deals()
    await _show_edit_menu(message, aid, notice, edit=False)


@router.message(EditItem.value, F.text)
async def edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field, value = data["field"], message.text.strip()
    account = await db.get_account(data["account_id"])
    if not account:
        await state.clear()
        return await message.answer("Товар не найден.")

    if field == "title" and not 10 <= len(value) <= 100:
        return await message.answer("Название — от 10 до 100 символов.")
    if field == "description" and len(value) < 10:
        return await message.answer("Описание — от 10 символов.")
    if field == "price":
        value = value.replace(" ", "").replace("₽", "")
        if not value.isdigit() or int(value) < 90:
            return await message.answer("Цена — целое число от 90 ₽.")
        value = int(value)
    if field == "rental_hours":
        if not value.isdigit() or not 1 <= int(value) <= 168:
            return await message.answer("Срок аренды — целое число от 1 до 168 часов.")
        value = int(value)
    if field == "login":
        if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", value):
            return await message.answer("Логин Steam — латиница, цифры и _, от 3 символов.")
        other = await db.get_account_by_login(value)
        if other and other["id"] != account["id"]:
            return await message.answer("Такой логин уже есть у другого товара.")
    if field in ("password", "mafile"):
        try:
            await message.delete()  # секреты не оставляем в чате
        except Exception:
            pass
    if field == "mafile":
        return await _edit_mafile(message, state, account, value)
    await _save_edit(message, state, field, value)


@router.message(EditItem.value, F.document)
async def edit_value_document(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data["field"] != "mafile":
        return await message.answer("Здесь нужен текст.")
    if message.document.file_size > 1024 * 1024:
        return await message.answer("Файл слишком большой.")
    account = await db.get_account(data["account_id"])
    raw = (await bot.download(message.document)).read().decode("utf-8-sig", errors="replace")
    await _edit_mafile(message, state, account, raw)


async def _edit_mafile(message: Message, state: FSMContext, account: dict, raw: str):
    parsed = _parse_mafile(raw, account["login"])
    if isinstance(parsed, str):
        return await message.answer(parsed, parse_mode="HTML")
    shared_secret, identity_secret = parsed
    await db.update_account(account["id"], shared_secret=shared_secret, identity_secret=identity_secret)
    await state.clear()
    code = steam_guard.generate_code(shared_secret)
    await _show_edit_menu(message, account["id"],
                          f"✅ maFile обновлён. Текущий код Guard: <code>{code}</code>", edit=False)


@router.message(EditItem.images, F.photo)
async def edit_image_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    await _add_image(message, state, photo.file_id, photo.file_size)


@router.message(EditItem.images, F.document)
async def edit_image_document(message: Message, state: FSMContext):
    if not (message.document.mime_type or "").startswith("image/"):
        return await message.answer("Это не картинка. Отправьте фото или файл изображения.")
    await _add_image(message, state, message.document.file_id, message.document.file_size)


@router.callback_query(EditItem.images, F.data == "img_done")
async def edit_images_done(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("images"):
        return await call.answer("Добавьте хотя бы одну картинку", show_alert=True)
    await call.answer("Загружаю картинки...")
    aid = data["account_id"]
    images = [(await bot.download(file_id)).read() for file_id in data["images"]]
    bad = [i for i, img in enumerate(images, start=1) if not item_images.is_valid(img)]
    if bad:
        await state.update_data(images=[])
        return await call.message.answer(
            f"❌ Картинка №{', '.join(map(str, bad))} повреждена. Отправьте картинки заново и нажмите «Готово».",
            reply_markup=kb.images_done(0))
    item_images.save(aid, images)
    await state.clear()
    await _show_edit_menu(call.message, aid, await _apply_lot_changes(aid, images=True), edit=False)


# ---------- Новый товар: аккаунт + лот на Playerok ----------

STEPS = 9


def _step(n: int, text: str) -> str:
    return f"Шаг {n}/{STEPS}. {text}"


async def _start_add(message: Message, state: FSMContext, edit: bool = False):
    await state.clear()
    if not client.acc:
        text = "Сначала задайте cookies Playerok — без входа товар не выставить."
        markup = kb.back("cookies", "🍪 Cookies")
    else:
        await state.set_state(AddItem.login)
        text = "🆕 <b>Новый товар — аренда CS2</b>\n\n" + _step(1, "Отправьте <b>логин</b> Steam.")
        markup = kb.cancel()
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await _start_add(message, state)


@router.callback_query(F.data == "add")
async def cb_add(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _start_add(call.message, state, edit=True)


@router.message(AddItem.login, F.text)
async def add_login(message: Message, state: FSMContext):
    login = message.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", login):
        return await message.answer("Логин Steam — латиница, цифры и _, от 3 символов. Отправьте ещё раз.")
    if await db.get_account_by_login(login):
        return await message.answer(f"Аккаунт <code>{esc(login)}</code> уже добавлен.", parse_mode="HTML")
    await state.update_data(login=login)
    await state.set_state(AddItem.password)
    await message.answer(f"Логин: <code>{esc(login)}</code>\n\n" + _step(2, "Отправьте <b>пароль</b>."),
                         parse_mode="HTML", reply_markup=kb.cancel())


@router.message(AddItem.password, F.text)
async def add_password(message: Message, state: FSMContext):
    password = message.text.strip()
    try:
        await message.delete()  # не храним пароль в истории чата
    except Exception:
        pass
    if not password:
        return await message.answer("Пароль не может быть пустым.")
    await state.update_data(password=password)
    await state.set_state(AddItem.mafile)
    await message.answer(
        "Пароль сохранён.\n\n" + _step(3, "Отправьте <b>.maFile</b> файлом (или вставьте его содержимое текстом)."),
        parse_mode="HTML", reply_markup=kb.cancel(),
    )


async def _accept_mafile(message: Message, state: FSMContext, raw: str):
    data = await state.get_data()
    parsed = _parse_mafile(raw, data["login"])
    if isinstance(parsed, str):
        return await message.answer(parsed, parse_mode="HTML", reply_markup=kb.cancel())
    shared_secret, identity_secret = parsed
    await state.update_data(shared_secret=shared_secret, identity_secret=identity_secret)
    await state.set_state(AddItem.title)
    code = steam_guard.generate_code(shared_secret)
    await message.answer(
        f"✅ maFile принят. Текущий код Guard: <code>{code}</code> — сверьте с приложением.\n\n"
        + _step(4, "Отправьте <b>название</b> товара (от 10 символов).\n"
                   "Например: <code>Аренда CS2 Prime | 1 час | Без VAC</code>"),
        parse_mode="HTML", reply_markup=kb.cancel())


@router.message(AddItem.mafile, F.document)
async def add_mafile_doc(message: Message, state: FSMContext, bot: Bot):
    if message.document.file_size > 1024 * 1024:
        return await message.answer("Файл слишком большой.")
    raw = (await bot.download(message.document)).read().decode("utf-8-sig", errors="replace")
    await _accept_mafile(message, state, raw)


@router.message(AddItem.mafile, F.text)
async def add_mafile_text(message: Message, state: FSMContext):
    raw = message.text.strip()
    try:
        await message.delete()  # в maFile секреты — убираем из чата
    except Exception:
        pass
    await _accept_mafile(message, state, raw)


@router.message(AddItem.title, F.text)
async def add_title(message: Message, state: FSMContext):
    title = message.text.strip()
    if not 10 <= len(title) <= 100:
        return await message.answer("Название — от 10 до 100 символов.")
    await state.update_data(title=title)
    await state.set_state(AddItem.description)
    await message.answer(
        _step(5, "Отправьте <b>описание</b> товара (от 10 символов) или нажмите «Стандартное»."),
        parse_mode="HTML", reply_markup=kb.default_or_cancel())


@router.message(AddItem.description, F.text)
async def add_description(message: Message, state: FSMContext):
    description = message.text.strip()
    if len(description) < 10:
        return await message.answer("Описание — от 10 символов.")
    await _set_description(message, state, description)


@router.callback_query(AddItem.description, F.data == "default")
async def add_description_default(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await _set_description(call.message, state, config.DEFAULT_ITEM_DESCRIPTION)


async def _set_description(message: Message, state: FSMContext, description: str):
    await state.update_data(description=description)
    await state.set_state(AddItem.price)
    await message.answer(_step(6, "Отправьте <b>цену</b> в рублях (от 90)."), parse_mode="HTML",
                         reply_markup=kb.cancel())


@router.message(AddItem.price, F.text)
async def add_price(message: Message, state: FSMContext):
    value = message.text.strip().replace(" ", "").replace("₽", "")
    if not value.isdigit() or int(value) < 90:
        return await message.answer("Цена — целое число от 90 ₽.")
    await state.update_data(price=int(value))
    await state.set_state(AddItem.hours)
    await message.answer(_step(7, "На сколько <b>часов</b> аренда? (1–168)"), parse_mode="HTML",
                         reply_markup=kb.cancel())


@router.message(AddItem.hours, F.text)
async def add_hours(message: Message, state: FSMContext):
    value = message.text.strip()
    if not value.isdigit() or not 1 <= int(value) <= 168:
        return await message.answer("Срок аренды — целое число от 1 до 168 часов.")
    await state.update_data(hours=int(value))
    await state.set_state(AddItem.prime)
    await message.answer(_step(8, "На аккаунте есть <b>Prime</b>?"), parse_mode="HTML", reply_markup=kb.yes_no("prime"))


@router.callback_query(AddItem.prime, F.data.startswith("prime:"))
async def add_prime(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.update_data(prime=call.data.endswith(":yes"), images=[])
    await state.set_state(AddItem.images)
    await call.message.edit_text(
        _step(9, f"Отправьте <b>картинки</b> для лота (до {MAX_IMAGES}), затем нажмите «Готово»."),
        parse_mode="HTML", reply_markup=kb.images_done(0))


async def _add_image(message: Message, state: FSMContext, file_id: str, size: int | None):
    data = await state.get_data()
    images = list(data.get("images") or [])
    if len(images) >= MAX_IMAGES:
        return await message.answer(f"Уже {MAX_IMAGES} картинок — нажмите «Готово».",
                                    reply_markup=kb.images_done(len(images)))
    if size and size > MAX_IMAGE_SIZE:
        return await message.answer("Картинка больше 10 МБ.", reply_markup=kb.images_done(len(images)))
    images.append(file_id)
    await state.update_data(images=images)
    await message.answer(f"🖼 Картинка {len(images)}/{MAX_IMAGES} добавлена.", reply_markup=kb.images_done(len(images)))


@router.message(AddItem.images, F.photo)
async def add_image_photo(message: Message, state: FSMContext):
    photo = message.photo[-1]
    await _add_image(message, state, photo.file_id, photo.file_size)


@router.message(AddItem.images, F.document)
async def add_image_document(message: Message, state: FSMContext):
    if not (message.document.mime_type or "").startswith("image/"):
        return await message.answer("Это не картинка. Отправьте фото или файл изображения.")
    await _add_image(message, state, message.document.file_id, message.document.file_size)


@router.callback_query(AddItem.images, F.data == "img_done")
async def add_images_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("images"):
        return await call.answer("Добавьте хотя бы одну картинку", show_alert=True)
    await call.answer()
    await state.set_state(AddItem.confirm)
    premium_default = await db.get_bool_setting("publish_premium")
    await call.message.answer(_item_summary(data), parse_mode="HTML",
                              reply_markup=kb.publish_item(premium_default))


def _discount_note(price: int) -> str:
    start = list_price(price)
    return f" (на лоте зачёркнуто {start} ₽)" if start != price else ""


def _item_summary(data: dict) -> str:
    return (
        "📦 <b>Проверьте товар</b>\n\n"
        f"Аккаунт: <code>{esc(data['login'])}</code>\n"
        f"Название: {esc(data['title'])}\n"
        f"Цена: {data['price']} ₽{_discount_note(data['price'])} · {data['hours']} ч · "
        f"Prime: {'да' if data['prime'] else 'нет'}\n"
        f"Картинок: {len(data['images'])}\n\n"
        f"Описание:\n{esc(data['description'])[:800]}"
    )


@router.callback_query(AddItem.confirm, F.data.startswith("pub:"))
async def add_publish(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    premium = call.data == "pub:premium"
    if not client.acc:
        return await call.answer("Нет входа в Playerok — обновите cookies", show_alert=True)
    if await db.get_account_by_login(data["login"]):
        await state.clear()
        return await call.answer("Этот аккаунт уже добавлен", show_alert=True)
    await call.answer("Выставляю...")
    await call.message.edit_reply_markup(reply_markup=None)
    status_msg = await call.message.answer("⏳ Загружаю картинки и создаю лот на Playerok...")

    try:
        images = [(await bot.download(file_id)).read() for file_id in data["images"]]
        bad = [i for i, img in enumerate(images, start=1) if not item_images.is_valid(img)]
        if bad:
            await state.update_data(images=[])
            await state.set_state(AddItem.images)
            await status_msg.edit_text(f"❌ Картинка №{', '.join(map(str, bad))} повреждена. "
                                       "Отправьте картинки заново и нажмите «Готово».")
            return await call.message.answer("Жду картинки:", reply_markup=kb.images_done(0))
        result = await client.create_rental_item(
            title=data["title"], description=data["description"], price=data["price"],
            hours=data["hours"], prime=data["prime"], images=images,
            comment=rentals.item_comment(data), premium=premium,
            premium_fallback=await db.get_bool_setting("premium_fallback"),
        )
    except Exception as e:
        logger.exception("[item] ошибка создания лота")
        await status_msg.edit_text(f"❌ Не удалось выставить товар:\n<code>{esc(e)[:800]}</code>\n\n"
                                   "Можно попробовать ещё раз.", parse_mode="HTML")
        return await call.message.answer(_item_summary(data), parse_mode="HTML",
                                         reply_markup=kb.publish_item(premium))

    item_id = result.get("item_id") or result.get("created_id")
    account_id = await db.add_account(
        data["login"], data["password"], data["shared_secret"],
        data.get("identity_secret"), item_id, data["hours"],
    )
    await db.update_account(account_id, last_item_id=item_id, title=data["title"],
                            description=data["description"], price=data["price"], prime=int(data["prime"]))
    item_images.save(account_id, images)
    await state.clear()

    if result.get("published"):
        kind = f"⭐ премиум ({result['premium_price']} ₽)" if result.get("premium") else "обычное"
        text = f"✅ <b>Товар выставлен</b> ({kind}): {rentals.price_text(result)}"
        if result.get("warning"):
            text += f"\n⚠️ Вместо премиума — обычное: {esc(result['warning'])}"
    else:
        text = (f"⚠️ Лот создан, но не опубликован: {esc(result.get('warning'))}.\n"
                "Опубликуйте его вручную на Playerok — аккаунт уже привязан к лоту.")
    if result.get("slug"):
        text += f"\nhttps://playerok.com/products/{esc(result['slug'])}"
    await status_msg.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)

    await rentals.process_pending_deals()
    account = await db.get_account(account_id)
    await call.message.answer(await _account_card_text(account, None), parse_mode="HTML",
                              reply_markup=kb.account_card(account, None))


# ---------- Аренды и Playerok ----------

async def _rentals_text() -> str:
    now = time.time()
    active = await db.get_active_rentals()
    pending = await db.get_pending_deals()
    stats = await db.get_stats()
    lines = [
        "📊 <b>Аренды</b>",
        "",
        f"Сегодня: {stats['today'] or 0} шт. на {stats['today_revenue']} ₽",
        f"Всего: {stats['total']} шт. на {stats['revenue']} ₽",
        "",
        f"<b>Активные ({len(active)}):</b>",
    ]
    for r in active:
        lines.append(f"🔴 <code>{esc(r['login'])}</code> — {esc(r['buyer_username'])}, до {fmt_ts(r['ends_at'])}")
    if not active:
        lines.append("нет")
    if pending:
        lines += ["", f"<b>⚠️ Ждут свободный аккаунт ({len(pending)}):</b>"]
        for p in pending:
            lines.append(f"• {esc(p['buyer_username'])} — {esc(p['item_name'])} ({fmt_ts(p['created_at'])})")
    recent = [r for r in await db.get_recent_rentals(5) if r["status"] != "active"]
    if recent:
        lines += ["", "<b>Последние завершённые:</b>"]
        status_names = {"finished": "завершена", "refunded": "возврат"}
        for r in recent:
            lines.append(
                f"• <code>{esc(r['login'])}</code> — {esc(r['buyer_username'])}, "
                f"{r['price']} ₽, {status_names.get(r['status'], r['status'])}"
            )
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(await _rentals_text(), parse_mode="HTML",
                         reply_markup=kb.rentals(await db.get_active_rentals()))


@router.callback_query(F.data == "rentals")
async def cb_rentals(call: CallbackQuery):
    try:
        await call.message.edit_text(await _rentals_text(), parse_mode="HTML",
                                     reply_markup=kb.rentals(await db.get_active_rentals()))
    except Exception:
        pass
    await call.answer()


# ---------- Баланс и профиль ----------

async def _finance_text() -> tuple[str, dict | None]:
    try:
        p = await client.get_profile()
    except Exception as e:
        text = f"💰 <b>Playerok</b>\n\n❌ Ошибка запроса: {esc(e)}\n"
        p = None
    else:
        def val(v):
            return "—" if v is None else v
        text = (
            f"💰 <b>Playerok — {esc(p['username'])}</b>\n\n"
            f"Баланс: <b>{p['total']} ₽</b>\n"
            f"• доступно: {p['available']} ₽\n"
            f"• к выводу: <b>{p['withdrawable']} ₽</b>\n"
            f"• заморожено: {p['frozen']} ₽\n"
            f"• ожидает зачисления: {p['pending_income']} ₽\n\n"
            f"Рейтинг: {val(p['rating'])} · отзывов: {val(p['reviews'])}\n"
            f"Лоты: {val(p['items_total'])} всего, {val(p['items_finished'])} продано\n"
            f"Продажи: {val(p['sales_total'])} всего, {val(p['sales_finished'])} завершено\n"
            f"Непрочитанных чатов: {val(p['unread_chats'])}\n"
        )
        if p["is_blocked"]:
            text += "⛔ Аккаунт заблокирован!\n"
        if p["can_publish"] is False:
            text += "⚠️ Аккаунт не может выставлять лоты\n"
    return text, p


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    text, _ = await _finance_text()
    await message.answer(text, parse_mode="HTML", reply_markup=kb.finance())


@router.callback_query(F.data == "playerok")
async def cb_playerok(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    text, _ = await _finance_text()
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.finance())
    except Exception:
        pass


# ---------- Вывод на TRX ----------

class Withdraw(StatesGroup):
    amount = State()
    wallet = State()


TRX_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


@router.callback_query(F.data == "wd")
async def cb_withdraw(call: CallbackQuery, state: FSMContext):
    await call.answer()
    try:
        profile = await client.get_profile()
        provider = await client.get_withdraw_provider()
    except Exception as e:
        return await call.message.answer(f"❌ Ошибка Playerok: {esc(e)}", parse_mode="HTML")
    if provider.get("_missing"):
        return await call.message.answer(
            f"❌ Провайдер вывода <code>{esc(config.WITHDRAW_PROVIDER)}</code> не найден.\n"
            f"Доступные: {esc(', '.join(str(x) for x in provider['available']))}\n"
            "Укажите нужный в WITHDRAW_PROVIDER в .env.", parse_mode="HTML")
    limits = (provider.get("limits") or {}).get("outgoing") or {}
    method = client.pick_payment_method(provider)
    await state.set_state(Withdraw.amount)
    await state.update_data(withdrawable=profile["withdrawable"], method=method,
                            min=limits.get("min"), max=limits.get("max"))
    text = (
        f"💸 <b>Вывод через {esc(provider.get('name') or config.WITHDRAW_PROVIDER)}</b>\n\n"
        f"Доступно к выводу: <b>{profile['withdrawable']} ₽</b>\n"
        f"Комиссия: {esc(provider.get('fee'))}%"
    )
    if provider.get("minFeeAmount"):
        text += f" (не меньше {provider['minFeeAmount']} ₽)"
    if limits:
        text += f"\nЛимиты: {esc(limits.get('min'))} – {esc(limits.get('max'))} ₽"
    if provider.get("description"):
        text += f"\n\n{esc(provider['description'])}"
    text += "\n\nОтправьте сумму в рублях:"
    await call.message.answer(text, parse_mode="HTML", reply_markup=kb.withdraw_amount(profile["withdrawable"]))


async def _withdraw_set_amount(message: Message, state: FSMContext, amount: int):
    data = await state.get_data()
    if amount <= 0 or amount > data["withdrawable"]:
        return await message.answer(f"Сумма должна быть от 1 до {data['withdrawable']} ₽.")
    if data.get("min") and amount < data["min"]:
        return await message.answer(f"Минимальная сумма вывода — {data['min']} ₽.")
    if data.get("max") and amount > data["max"]:
        return await message.answer(f"Максимальная сумма вывода — {data['max']} ₽.")
    await state.update_data(amount=amount)
    wallet = await db.get_setting("trx_wallet")
    if wallet:
        await _withdraw_confirm(message, state, wallet)
    else:
        await state.set_state(Withdraw.wallet)
        await message.answer("Отправьте адрес TRX-кошелька (TRC20, начинается с T):", reply_markup=kb.cancel())


@router.message(Withdraw.amount, F.text)
async def withdraw_amount(message: Message, state: FSMContext):
    value = message.text.strip().replace(" ", "")
    if not value.isdigit():
        return await message.answer("Отправьте сумму целым числом.")
    await _withdraw_set_amount(message, state, int(value))


@router.callback_query(Withdraw.amount, F.data == "wd_all")
async def withdraw_all(call: CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    await _withdraw_set_amount(call.message, state, data["withdrawable"])


@router.callback_query(Withdraw.amount, F.data == "wd_wallet")
async def withdraw_change_wallet(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(Withdraw.wallet)
    await call.message.answer("Отправьте адрес TRX-кошелька (TRC20, начинается с T):", reply_markup=kb.cancel())


@router.message(Withdraw.wallet, F.text)
async def withdraw_wallet(message: Message, state: FSMContext):
    wallet = message.text.strip()
    if not TRX_ADDRESS_RE.match(wallet):
        return await message.answer("Это не похоже на TRX-адрес: 34 символа, начинается с T.")
    await db.set_setting("trx_wallet", wallet)
    await _withdraw_confirm(message, state, wallet)


async def _withdraw_confirm(message: Message, state: FSMContext, wallet: str):
    data = await state.get_data()
    await state.set_state(Withdraw.amount)
    await state.update_data(wallet=wallet)
    method = f" / {esc(data['method'])}" if data.get("method") else ""
    await message.answer(
        "Проверьте вывод:\n\n"
        f"Сумма: <b>{data['amount']} ₽</b>\n"
        f"Кошелёк: <code>{esc(wallet)}</code>\n"
        f"Провайдер: {esc(config.WITHDRAW_PROVIDER)}{method}",
        parse_mode="HTML", reply_markup=kb.withdraw_confirm(),
    )


@router.callback_query(Withdraw.amount, F.data == "wd_go")
async def withdraw_go(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("amount") or not data.get("wallet"):
        return await call.answer("Нет данных вывода, начните заново", show_alert=True)
    await state.clear()
    await call.answer("Отправляю запрос...")
    await call.message.edit_reply_markup(reply_markup=None)
    try:
        tx = await client.request_withdrawal(data["wallet"], data["amount"], data.get("method"))
    except Exception as e:
        return await call.message.answer(f"❌ Вывод не создан: {esc(e)}", parse_mode="HTML",
                                         reply_markup=kb.back("playerok"))
    tx = tx or {}
    await call.message.answer(
        "✅ <b>Заявка на вывод создана</b>\n\n"
        f"ID: <code>{esc(tx.get('id'))}</code>\n"
        f"Статус: {esc(tx.get('status'))}\n"
        f"Сумма: {esc(tx.get('value', data['amount']))} ₽, комиссия: {esc(tx.get('fee'))} ₽\n"
        f"Кошелёк: <code>{esc(data['wallet'])}</code>",
        parse_mode="HTML", reply_markup=kb.back("playerok"),
    )


# ---------- Настройки выставления ----------

async def _pubset_text_kb():
    premium = await db.get_bool_setting("publish_premium")
    fallback = await db.get_bool_setting("premium_fallback")
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Выставление лотов: {'⭐ премиум (платно, с баланса Playerok)' if premium else 'обычное (бесплатно)'}\n"
        f"Если не хватает на премиум: {'выставить обычным' if fallback else 'оставить черновиком'}\n"
        "Для отдельного товара: ✏️ Редактировать → 🎯 Выставление."
    )
    return text, kb.publish_settings(premium, fallback)


@router.callback_query(F.data == "pubset")
async def cb_pubset(call: CallbackQuery):
    text, markup = await _pubset_text_kb()
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("set:"))
async def cb_toggle_setting(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    if key not in ("publish_premium", "premium_fallback"):
        return await call.answer()
    await db.set_setting(key, not await db.get_bool_setting(key))
    text, markup = await _pubset_text_kb()
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    await call.answer("Сохранено")


@router.callback_query(F.data.startswith("pmode:"))
async def cb_publish_mode(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    account = await db.get_account(aid)
    if not account:
        return await call.answer("Товар не найден", show_alert=True)
    cycle = kb.PUBLISH_MODE_CYCLE
    current = account.get("publish_mode") if account.get("publish_mode") in cycle else None
    new_mode = cycle[(cycle.index(current) + 1) % len(cycle)]
    await db.update_account(aid, publish_mode=new_mode)
    await call.answer(f"Выставление: {kb.PUBLISH_MODE_NAMES[new_mode]}")
    await _show_edit_menu(call.message, aid)


@router.callback_query(F.data.startswith("repub:"))
async def cb_republish(call: CallbackQuery):
    aid = int(call.data.split(":")[1])
    account = await db.get_account(aid)
    if not account:
        return await call.answer("Товар не найден", show_alert=True)
    if not account.get("last_item_id"):
        return await call.answer("У товара нет лота — откройте «✏️ Редактировать» → «🔗 Привязка лота».",
                                 show_alert=True)
    await call.answer("Выставляю лот...")
    result = await rentals.republish_for_account(aid)
    await _show_account(call, aid, result.strip() or "Готово")


# ---------- Cookies Playerok ----------

class Cookies(StatesGroup):
    value = State()


PROXY_RE = re.compile(r"^(?:https?://)?(?:[^:@\s]+:[^:@\s]+@)?[\w.-]+:\d{2,5}$")

COOKIE_PROMPTS = {
    "cookies": (
        "🍪 Отправьте cookies Playerok — текстом или файлом .txt/.json:\n\n"
        "• строку заголовка <code>Cookie</code> (DevTools → Network → любой запрос к playerok.com → "
        "Request Headers → cookie);\n"
        "• JSON-экспорт из расширения Cookie-Editor / EditThisCookie;\n"
        "• или только значение <code>token</code>.\n\n"
        "Важно: __ddg5_ привязан к IP — берите cookies с того же IP, что и прокси бота. "
        "Если вход не проходит, войдите на playerok.com из браузера через этот прокси и пришлите cookies заново."
    ),
    "proxy": "🌐 Отправьте прокси <code>ip:port</code> или <code>user:pass@ip:port</code>, "
             "или <code>-</code>, чтобы работать без прокси.",
}


async def _cookies_text() -> str:
    creds = await load_credentials()
    cookies = creds["cookies"]
    if client.acc and client.auth_ok:
        status = f"🟢 вход выполнен: <b>{esc(client.acc.username)}</b>"
    elif client.acc:
        status = f"🔴 cookies не работают (аккаунт {esc(client.acc.username)})"
    else:
        status = "🔴 вход не выполнен"
    listener = "🟢 работает" if client.listener_alive else "🔴 не работает"
    if client.last_event_at:
        listener += f", последнее событие {fmt_ts(client.last_event_at)} МСК"
    text = (
        "🍪 <b>Вход в Playerok</b>\n\n"
        f"Статус: {status}\n"
        f"Приём событий: {listener}\n"
        f"token: <code>{esc(mask(cookies.get('token')))}</code>\n"
        f"__ddg5_: {'есть' if cookies.get('__ddg5_') else 'нет'} · всего cookies: {len(cookies)}\n"
        f"User-Agent (авто): <code>{esc(creds['user_agent'])}</code>\n"
        f"Прокси: {esc(mask(creds.get('proxy'), 8)) if creds.get('proxy') else 'нет'}"
    )
    if client.last_error and not client.auth_ok:
        text += f"\n\nОшибка: <code>{esc(client.last_error)[:300]}</code>"
    return text


@router.message(Command("cookies"))
async def cmd_cookies(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await _cookies_text(), parse_mode="HTML", reply_markup=kb.cookies_menu())


@router.callback_query(F.data == "cookies")
async def cb_cookies(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text(await _cookies_text(), parse_mode="HTML", reply_markup=kb.cookies_menu())
    except Exception:
        await call.message.answer(await _cookies_text(), parse_mode="HTML", reply_markup=kb.cookies_menu())


@router.callback_query(F.data.startswith("ck:"))
async def cb_cookie_field(call: CallbackQuery, state: FSMContext):
    field = call.data.split(":", 1)[1]
    if field not in COOKIE_PROMPTS:
        return await call.answer()
    await state.set_state(Cookies.value)
    await state.update_data(field=field)
    await call.message.answer(COOKIE_PROMPTS[field], parse_mode="HTML", reply_markup=kb.cancel())
    await call.answer()


async def _try_login(creds: dict) -> tuple[bool, str]:
    try:
        username = await client.apply_credentials(creds)
    except Exception as e:
        client.last_error = f"{type(e).__name__}: {e}"
        return False, str(e)
    return True, username


@router.callback_query(F.data == "ck_check")
async def cb_cookies_check(call: CallbackQuery):
    await call.answer("Проверяю...")
    if client.acc:
        await client.health_check()
    else:
        await _try_login(await load_credentials())
    try:
        await call.message.edit_text(await _cookies_text(), parse_mode="HTML", reply_markup=kb.cookies_menu())
    except Exception:
        pass


@router.message(Cookies.value, F.document | F.text)
async def cookie_value(message: Message, state: FSMContext, bot: Bot):
    field = (await state.get_data())["field"]
    if message.document:
        if field != "cookies" or message.document.file_size > 512 * 1024:
            return await message.answer("Нужен текстовый файл с cookies до 512 КБ.")
        raw = (await bot.download(message.document)).read().decode("utf-8-sig", errors="replace")
    else:
        raw = message.text.strip()
    try:
        await message.delete()  # не оставляем cookies в истории чата
    except Exception:
        pass

    creds = await load_credentials()
    if field == "cookies":
        try:
            cookies = parse_cookies(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return await message.answer(f"Не удалось разобрать cookies: {esc(e)}", parse_mode="HTML")
        creds["cookies"] = cookies
        await message.answer("⏳ Проверяю вход в Playerok...")
        ok, result = await _try_login(creds)
        if not ok:
            return await message.answer(
                f"❌ С этими cookies войти не удалось, они не сохранены:\n<code>{esc(result)[:400]}</code>\n\n"
                "Пришлите другие или нажмите «Отмена».", parse_mode="HTML", reply_markup=kb.cancel())
        await save_credentials(cookies=cookies)
        await state.clear()
        await message.answer(f"✅ Cookies обновлены, вход выполнен: <b>{esc(result)}</b>\n\n" + await _cookies_text(),
                             parse_mode="HTML", reply_markup=kb.cookies_menu())
        await rentals.process_pending_deals()
        asyncio.create_task(rentals.sync_lot_comments())
        return

    value = "" if raw == "-" else raw
    if field == "proxy" and value and not PROXY_RE.match(value):
        return await message.answer("Формат прокси: <code>ip:port</code> или <code>user:pass@ip:port</code>.",
                                    parse_mode="HTML")
    await save_credentials(**{field: value})
    await state.clear()
    creds[field] = value or None
    ok, result = await _try_login(creds)
    note = (f"✅ Сохранено, вход выполнен: <b>{esc(result)}</b>" if ok else
            f"💾 Сохранено, но с текущими cookies войти не удалось: <code>{esc(result)[:300]}</code>\n"
            "Обновите cookies — они будут проверены уже с новыми настройками.")
    await message.answer(note + "\n\n" + await _cookies_text(), parse_mode="HTML", reply_markup=kb.cookies_menu())
