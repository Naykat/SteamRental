import asyncio
import logging
import time

import config
import database as db
import item_images
import steam_guard
from playerok_client import client
from playerokapi.enums import ItemDealStatuses
from playerokapi.listener.events import (
    NewMessageEvent, NewDealEvent, ItemPaidEvent, DealRolledBackEvent,
    DealConfirmedEvent, DealHasProblemEvent, ChatInitializedEvent,
)
from steam_password import change_password, generate_password
from utils import esc, fmt_ts, fmt_left, lot_matches, hours_from_item

logger = logging.getLogger(__name__)

_bot = None
_deal_lock = asyncio.Lock()
_finishing: set[int] = set()
_code_last_sent: dict[str, float] = {}
_seen_other_deals: set[str] = set()


def set_bot(bot):
    global _bot
    _bot = bot


async def notify_admins(text: str, reply_markup=None):
    if not _bot:
        return
    for admin_id in config.ADMIN_IDS:
        try:
            await _bot.send_message(admin_id, text, parse_mode="HTML",
                                    reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"[tg] не удалось уведомить {admin_id}: {e}")


async def _safe_send(chat_id: str, text: str) -> bool:
    try:
        await client.send_message(chat_id, text)
        return True
    except Exception:
        return False


# ---------- Выдача аккаунта ----------

def _deal_info(deal, chat_id: str) -> dict:
    item = deal.item
    user = deal.user
    return {
        "deal_id": deal.id,
        "chat_id": chat_id or (deal.chat.id if deal.chat else None),
        "item_id": getattr(item, "id", None),
        "item_slug": getattr(item, "slug", None),
        "item_name": getattr(item, "name", None) or "",
        "buyer_id": getattr(user, "id", None),
        "buyer_username": getattr(user, "username", None) or "?",
        "price": getattr(item, "price", None) or 0,
        "hours": hours_from_item(item),
    }


async def _find_free_account(info: dict) -> dict | None:
    for account in await db.get_free_accounts():
        if account["id"] in _finishing:
            continue
        if lot_matches(account["lot"], info["item_id"], info.get("item_slug"), info["item_name"]):
            return account
    return None


async def _any_account_matches(info: dict) -> bool:
    for account in await db.get_accounts():
        if lot_matches(account["lot"], info["item_id"], info.get("item_slug"), info["item_name"]):
            return True
    return False


async def _complete_deal(deal_id: str, attempts: int = 3) -> bool:
    """Отмечает заказ выполненным (статус SENT), с повторами."""
    for attempt in range(1, attempts + 1):
        try:
            await client.confirm_deal(deal_id)
            logger.info(f"[deal] заказ {deal_id} выполнен")
            return True
        except Exception as e:
            logger.error(f"[deal] попытка {attempt}: не удалось выполнить заказ {deal_id}: {e}")
            if attempt < attempts:
                await asyncio.sleep(3 * attempt)
    return False


async def deals_poller():
    """Раз в DEALS_POLL_SECONDS сверяет оплаченные заказы через API.

    Подстраховка к websocket: заказ, оплаченный пока бот был выключен или событие
    не пришло, всё равно будет выдан и выполнен.
    """
    while True:
        if client.acc is not None:
            try:
                for deal in await client.get_paid_deals():
                    if not await db.deal_known(deal.id):
                        logger.info(f"[poller] найден оплаченный заказ {deal.id}")
                        await handle_paid_deal(deal, deal.chat.id if deal.chat else None)
            except Exception as e:
                logger.warning(f"[poller] ошибка проверки заказов: {e}")
        await asyncio.sleep(config.DEALS_POLL_SECONDS)


async def issue_rental(info: dict, account: dict) -> dict:
    hours = info.get("hours") or account.get("rental_hours") or config.DEFAULT_RENTAL_HOURS
    if config.TEST_RENTAL_MINUTES:
        # Тестовый режим: любая аренда длится TEST_RENTAL_MINUTES с момента покупки
        ends_at = int(time.time()) + config.TEST_RENTAL_MINUTES * 60
    else:
        ends_at = int(time.time()) + hours * 3600 + config.BONUS_MINUTES * 60
    rental_id = await db.create_rental(
        account["id"], info["deal_id"], info["chat_id"], info["item_id"], info["item_name"],
        info["buyer_id"], info["buyer_username"], info["price"], hours, ends_at,
    )
    # info["item_id"] — ID копии лота в сделке; у товара остаётся ID его выставленного лота
    if not account.get("last_item_id") and info.get("item_id"):
        await db.update_account(account["id"], last_item_id=info["item_id"])

    text = config.CREDENTIALS_TEXT.format(
        login=account["login"], password=account["password"], ends_at=fmt_ts(ends_at), hours=hours,
    )
    sent = await _safe_send(info["chat_id"], text)

    # Автовыполнение заказа: логин и пароль есть и в комментарии лота, поэтому заказ
    # выполняется даже если сообщение в чат не ушло
    confirmed = None
    if config.AUTO_CONFIRM_DEAL and info.get("deal_id"):
        confirmed = await _complete_deal(info["deal_id"])

    status_line = "✅ данные отправлены" if sent else "❌ НЕ удалось отправить данные в чат (они есть в комментарии лота)"
    if confirmed is True:
        status_line += ", заказ выполнен"
    elif confirmed is False:
        status_line += ", ❌ заказ не удалось отметить выполненным — сделайте это вручную"

    await notify_admins(
        "🛒 <b>Новая аренда</b>\n\n"
        f"Покупатель: <b>{esc(info['buyer_username'])}</b>\n"
        f"Лот: {esc(info['item_name'])}\n"
        f"Цена: {info['price']} ₽\n"
        f"Аккаунт: <code>{esc(account['login'])}</code>\n"
        f"Срок: {hours} ч, до {fmt_ts(ends_at)} МСК\n\n"
        f"{status_line}"
    )
    logger.info(f"[rental] #{rental_id} {account['login']} -> {info['buyer_username']} на {hours} ч")
    return {"rental_id": rental_id, "sent": sent}


async def handle_paid_deal(deal, chat_id: str):
    if not deal or not deal.item:
        return
    status = getattr(deal, "status", None)
    if status not in (None, ItemDealStatuses.PAID, ItemDealStatuses.PENDING):
        return
    info = _deal_info(deal, chat_id)
    if not info["chat_id"]:
        # В списке сделок Playerok не отдаёт чат — запрашиваем сделку целиком
        try:
            full = await client.get_deal(deal.id)
            info["chat_id"] = full.chat.id if full and full.chat else None
            if full and full.item and not info["item_slug"]:
                info["item_slug"] = getattr(full.item, "slug", None)
        except Exception as e:
            logger.warning(f"[deal] не удалось получить сделку {deal.id}: {e}")
    if not info["chat_id"]:
        logger.warning(f"[deal] у сделки {deal.id} нет чата")
        return

    async with _deal_lock:
        if await db.deal_known(deal.id):
            return
        if not await _any_account_matches(info):
            # Это не лот аренды — просто сообщаем о продаже
            if deal.id in _seen_other_deals:
                return
            _seen_other_deals.add(deal.id)
            await notify_admins(
                "💰 <b>Новая продажа</b> (лот не привязан к аккаунтам)\n\n"
                f"Покупатель: <b>{esc(info['buyer_username'])}</b>\n"
                f"Лот: {esc(info['item_name'])}\n"
                f"Цена: {info['price']} ₽"
            )
            return

        account = await _find_free_account(info)
        if account:
            await issue_rental(info, account)
            return

        await db.add_pending_deal(
            info["deal_id"], info["chat_id"], info["item_id"], info["item_name"],
            info["buyer_id"], info["buyer_username"], info["price"], info["hours"],
        )

    await _safe_send(info["chat_id"], config.NO_FREE_ACCOUNTS_TEXT)
    await notify_admins(
        "⚠️ <b>Покупка без свободного аккаунта!</b>\n\n"
        f"Покупатель: <b>{esc(info['buyer_username'])}</b>\n"
        f"Лот: {esc(info['item_name'])}\n"
        f"Цена: {info['price']} ₽\n\n"
        "Сделка поставлена в очередь — данные будут выданы автоматически, "
        "как только освободится подходящий аккаунт."
    )


async def _has_pending_for(account_id: int) -> bool:
    account = await db.get_account(account_id)
    if not account or not account["enabled"]:
        return False
    for pending in await db.get_pending_deals():
        if pending["chat_id"] and lot_matches(account["lot"], pending["item_id"], None, pending["item_name"]):
            return True
    return False


async def _relist_after_lost_sale(deal):
    """Возврат по сделке, по которой аккаунт так и не был выдан: лот остался «проданным»,
    поэтому выставляем товар заново (пароль не меняем — данные покупатель не получал)."""
    item = deal.item
    if not item:
        return
    slug = getattr(item, "slug", None)
    for account in await db.get_free_accounts():
        if account["id"] in _finishing:
            continue
        if lot_matches(account["lot"], getattr(item, "id", None), slug, getattr(item, "name", None)):
            text = await republish_for_account(account["id"])
            await notify_admins(
                "↩️ <b>Возврат по сделке без выданного аккаунта</b>\n\n"
                f"Покупатель: {esc(getattr(deal.user, 'username', '?'))}\n"
                f"Товар: <code>{esc(account['login'])}</code>{text}")
            return


async def process_pending_deals():
    for pending in await db.get_pending_deals():
        if not pending["chat_id"]:
            await db.remove_pending_deal(pending["deal_id"])
            continue
        info = dict(pending)
        info["item_slug"] = None
        async with _deal_lock:
            account = await _find_free_account(info)
            if not account:
                continue
            await db.remove_pending_deal(pending["deal_id"])
            await issue_rental(info, account)


# ---------- Завершение аренды ----------

async def _rotate_password(account_id: int) -> str:
    account = await db.get_account(account_id)
    if not account:
        return ""
    if not account.get("identity_secret"):
        return "\n❌ Пароль не сменён: в maFile нет identity_secret — загрузите полный maFile"
    new_password = generate_password()
    try:
        await asyncio.to_thread(
            change_password, account["login"], account["password"],
            account["shared_secret"], account["identity_secret"], new_password,
        )
    except Exception as e:
        logger.error(f"[pass] {account['login']}: {e}")
        return f"\n❌ Ошибка смены пароля: {esc(e)}"
    await db.update_account(account_id, password=new_password)
    return f"\n🔁 Пароль сменён: <code>{esc(new_password)}</code>"


def lot_attributes(account: dict) -> dict:
    attributes = {}
    if account.get("rental_hours"):
        attributes["amount"] = account["rental_hours"]
    if account.get("prime") is not None:
        attributes["prime-status"] = bool(account["prime"])
    return attributes


def item_comment(account: dict) -> str:
    return config.ITEM_COMMENT.format(login=account["login"], password=account["password"])


def item_template(account: dict) -> dict:
    """Сохранённые в боте данные товара для выставления лота."""
    return {
        "comment": item_comment(account),
        "name": account.get("title"),
        "description": account.get("description"),
        "price": account.get("price"),
        "attributes": lot_attributes(account),
        "images": item_images.load(account["id"]),
    }


def price_text(result: dict) -> str:
    """Цена лота для отчёта: «150 ₽ (скидка с 499 ₽)» или предупреждение, если скидка не встала."""
    if result.get("discount_error"):
        return (f"{result['price']} ₽ — ⚠️ не удалось поставить цену со скидкой: "
                f"{esc(result['discount_error'])[:150]}. Поменяйте цену в «✏️ Редактировать»")
    if result.get("list_price"):
        return f"{result['price']} ₽ (скидка с {result['list_price']} ₽)"
    return f"{result['price']} ₽"


async def publish_mode(account: dict) -> str:
    """Итоговый режим выставления для аккаунта: 'premium' / 'default'."""
    mode = account.get("publish_mode")
    if mode in ("premium", "default"):
        return mode
    return "premium" if await db.get_bool_setting("publish_premium") else "default"


async def republish_for_account(account_id: int, item_id: str | None = None,
                                replace: dict[str, str] | None = None) -> str:
    """Выставляет снова тот же лот товара (новый пароль в комментарии); если Playerok не даёт —
    создаёт новый лот, а старый отвязывает и удаляет. Возвращает строку для отчёта."""
    account = await db.get_account(account_id)
    if not account:
        return ""
    item_id = item_id or account.get("last_item_id")
    if not item_id:
        return "\n⚠️ Лот не выставлен: неизвестен исходный лот (аккаунт ещё ни разу не продавался)"
    mode = await publish_mode(account)
    premium_fallback = await db.get_bool_setting("premium_fallback")

    # 1) Тот же лот: новый пароль в комментарии + правки товара, ID не меняется
    fallback_note = ""
    try:
        result = await client.relist_same(item_id, premium=mode == "premium",
                                          premium_fallback=premium_fallback, template=item_template(account))
    except Exception as e:
        logger.warning(f"[relist] {account['login']}: тот же лот не выставился ({e}), создаю новый")
        fallback_note = f"\nℹ️ Тот же лот выставить не удалось ({esc(e)[:150]}) — создан новый."
    else:
        if not result.get("published"):
            return (f"\n⚠️ Лот обновлён, но не опубликован: {esc(result.get('warning'))}. "
                    "Опубликуйте вручную или включите «обычное, если не хватает на премиум».")
        kind = f"⭐ премиум ({result['premium_price']} ₽)" if result.get("premium") else "обычное"
        text = f"\n📤 Лот выставлен снова ({kind}): {price_text(result)}, пароль в комментарии обновлён"
        if result.get("slug"):
            text += f"\nhttps://playerok.com/products/{esc(result['slug'])}"
        if result.get("warning"):
            text += f"\n⚠️ Вместо премиума — обычное: {esc(result['warning'])}"
        if result.get("comment_error"):
            text += f"\n⚠️ Комментарий не обновился: {esc(result['comment_error'])[:150]}"
        logger.info(f"[relist] {account['login']}: тот же лот {item_id} выставлен снова")
        return text

    # 2) Запасной путь: новый лот, старый отвязывается и удаляется
    try:
        result = await client.republish(
            item_id, premium=mode == "premium",
            premium_fallback=premium_fallback, replace=replace,
            template=item_template(account),
        )
    except Exception as e:
        logger.error(f"[republish] {account['login']}: {e}")
        return f"{fallback_note.replace(' — создан новый', '')}\n❌ Не удалось выставить лот: {esc(e)}"

    if result.get("skipped"):
        return f"\nℹ️ Лот не перевыставлен: {esc(result['reason'])}"

    new_id, new_slug = result.get("item_id") or result.get("created_id"), result.get("slug") or ""
    # Отвязываем старый лот: в привязке и как исходный — только новый
    lot = account.get("lot") or ""
    for old_token, new_token in ((item_id, new_id), (result.get("old_slug"), new_slug)):
        if old_token and new_token and old_token in lot:
            lot = lot.replace(old_token, new_token)
    if new_id and new_id not in lot:
        lot = ", ".join(t for t in [new_id, lot] if t)
    await db.update_account(account_id, last_item_id=new_id, lot=lot)

    if not result.get("published"):
        return (f"\n⚠️ Лот создан, но не опубликован: {esc(result.get('warning'))}. "
                "Опубликуйте вручную или включите «обычное, если не хватает на премиум». "
                "Старый лот не удалён.")

    kind = f"⭐ премиум ({result['premium_price']} ₽)" if result["premium"] else "обычное"
    text = f"\n📤 Новый лот выставлен ({kind}): {price_text(result)}"
    if new_slug:
        text += f"\nhttps://playerok.com/products/{esc(new_slug)}"
    if result.get("warning"):
        text += f"\n⚠️ Вместо премиума — обычное: {esc(result['warning'])}"
    try:
        await client.remove_lot(item_id)
        text += "\n🗑 Старый лот удалён"
    except Exception as e:
        logger.error(f"[republish] не удалось удалить старый лот {item_id}: {e}")
        text += f"\n⚠️ Старый лот отвязан, но не удалён: {esc(e)[:200]}"
    logger.info(f"[republish] {account['login']}: {item_id} -> {new_id}")
    return fallback_note + text


async def _rotate_and_relist(account_id: int, old_item_id: str | None) -> str:
    """После аренды: всегда меняет пароль, затем выставляет тот же лот снова.

    Если пароль сменить не удалось, товар выключается и не выставляется,
    а watcher повторяет попытку каждые ROTATION_RETRY_MINUTES.
    """
    account = await db.get_account(account_id)
    if not account:
        return ""
    old_password = account["password"]
    result = await _rotate_password(account_id)
    if "Пароль сменён" not in result:
        await db.update_account(account_id, needs_rotation=1, enabled=0,
                                last_item_id=old_item_id or account.get("last_item_id"))
        return (result + "\n⛔ Товар выключен и не выставлен, пока пароль не сменится. "
                f"Бот повторит попытку через {config.ROTATION_RETRY_MINUTES} мин.")
    if account.get("needs_rotation"):
        await db.update_account(account_id, needs_rotation=0, enabled=1)
    else:
        await db.update_account(account_id, needs_rotation=0)

    new_password = (await db.get_account(account_id))["password"]
    replace = {old_password: new_password}
    if await _has_pending_for(account_id):
        return result + "\nℹ️ Лот не перевыставлен: аккаунт сразу уходит покупателю из очереди"
    return result + await republish_for_account(account_id, old_item_id, replace=replace)


async def finish_rental(rental: dict, status: str = "finished", notify_buyer: bool = True,
                        reason: str = "Аренда завершена") -> bool:
    account_id = rental["account_id"]
    if account_id in _finishing:
        return False
    _finishing.add(account_id)
    try:
        if notify_buyer and rental.get("chat_id"):
            await _safe_send(rental["chat_id"], config.EXPIRED_TEXT)
        await db.set_rental_status(rental["id"], status)
        # Перевыставляем лот товара (last_item_id), а не копию из сделки (rental.item_id)
        extra = await _rotate_and_relist(account_id, None)
        await notify_admins(
            f"⏹ <b>{esc(reason)}</b>\n\n"
            f"Аккаунт: <code>{esc(rental['login'])}</code>\n"
            f"Покупатель: {esc(rental.get('buyer_username'))}"
            f"{extra}",
            reply_markup=_retry_markup(account_id) if "⛔" in extra else None,
        )
    finally:
        _finishing.discard(account_id)
    await process_pending_deals()
    return True


async def retry_rotation(account_id: int) -> str:
    """Повторная смена пароля для товара, у которого она не прошла после аренды."""
    if account_id in _finishing:
        return "⏳ Уже выполняется"
    _finishing.add(account_id)
    try:
        account = await db.get_account(account_id)
        if not account or not account.get("needs_rotation"):
            return "Повтор не требуется"
        text = await _rotate_and_relist(account_id, account.get("last_item_id"))
    finally:
        _finishing.discard(account_id)
    await process_pending_deals()
    return text.strip()


def _retry_markup(account_id: int):
    from keyboards import _kb, Btn
    return _kb([[Btn(text="🔁 Повторить смену пароля", callback_data=f"rotretry:{account_id}")],
                [Btn(text="📋 Товар", callback_data=f"acc:{account_id}")]])


async def _rotation_retry_loop():
    while True:
        await asyncio.sleep(config.ROTATION_RETRY_MINUTES * 60)
        try:
            for account in await db.get_accounts():
                if account.get("needs_rotation") and account["id"] not in _finishing and client.acc:
                    text = await retry_rotation(account["id"])
                    if "Пароль сменён" in text:
                        await notify_admins(
                            f"✅ <b>Пароль сменён со второй попытки</b>\n\n"
                            f"Аккаунт: <code>{esc(account['login'])}</code>\n{text}")
        except Exception:
            logger.exception("[rotation-retry] ошибка")


def _warn_seconds() -> int:
    seconds = config.WARN_BEFORE_MINUTES * 60
    if config.TEST_RENTAL_MINUTES:
        # в тестовом режиме предупреждаем за минуту, а не сразу после покупки
        seconds = min(seconds, 60)
    return seconds


async def rental_watcher():
    """Фоновая задача: предупреждения и завершение аренд по времени."""
    await process_pending_deals()
    asyncio.create_task(_rotation_retry_loop())
    while True:
        try:
            now = time.time()
            for rental in await db.get_active_rentals():
                left = rental["ends_at"] - now
                if left <= 0:
                    asyncio.create_task(finish_rental(rental, "finished", True, "Аренда завершена по времени"))
                elif (config.WARN_BEFORE_MINUTES and not rental["warned"]
                      and left <= _warn_seconds()):
                    await db.mark_warned(rental["id"])
                    await _safe_send(rental["chat_id"], config.WARN_TEXT.format(
                        login=rental["login"], minutes=max(1, int(left // 60)),
                    ))
        except Exception:
            logger.exception("[watcher] ошибка")
        await asyncio.sleep(20)


async def sync_lot_comments():
    """Приводит комментарий выставленных лотов к шаблону ITEM_COMMENT (с актуальными логином/паролем).

    Лоты не удаляются — меняется только поле комментария через updateItem.
    """
    if client.acc is None:
        return
    updated, failed = [], []
    for account in await db.get_accounts():
        if not account.get("last_item_id"):
            continue
        try:
            result = await client.update_lot(account["last_item_id"], comment=item_comment(account))
        except Exception as e:
            logger.error(f"[comment] {account['login']}: {e}")
            failed.append(f"{account['login']}: {e}")
            continue
        if result.get("updated"):
            updated.append(account["login"])
    if updated or failed:
        text = "📝 <b>Комментарии лотов</b>\n"
        if updated:
            text += f"\nОбновлены ({len(updated)}): " + ", ".join(f"<code>{esc(x)}</code>" for x in updated)
        if failed:
            text += "\n\n❌ Не удалось:\n" + "\n".join(esc(x)[:200] for x in failed)
        await notify_admins(text)
    logger.info(f"[comment] обновлено {len(updated)}, ошибок {len(failed)}")


async def auth_watcher():
    """Раз в 5 минут проверяет, живы ли cookies Playerok, и сообщает об изменениях."""
    from keyboards import back
    was_ok = client.auth_ok
    while True:
        await asyncio.sleep(300)
        if client.acc is None:
            continue
        ok = await client.health_check()
        if was_ok and not ok:
            await notify_admins(
                f"🍪 <b>Cookies Playerok перестали работать</b>\n\n{esc(client.last_error)[:300]}\n\n"
                "Новые покупки не будут обработаны, пока вы не обновите cookies.",
                reply_markup=back("cookies", "🍪 Обновить cookies"),
            )
        elif ok and not was_ok:
            await notify_admins("✅ Связь с Playerok восстановлена")
        was_ok = ok


# ---------- Команды покупателя ----------

async def _handle_code(chat_id: str):
    now = time.time()
    rentals = [r for r in await db.get_active_rentals(chat_id) if r["ends_at"] > now]
    if not rentals:
        await _safe_send(chat_id, config.NO_RENTAL_TEXT)
        return
    last = _code_last_sent.get(chat_id, 0)
    if now - last < config.CODE_COOLDOWN_SECONDS:
        return
    _code_last_sent[chat_id] = now

    lines = []
    for r in rentals:
        text = config.CODE_TEXT.format(code=steam_guard.generate_code(r["shared_secret"]))
        lines.append(f"{r['login']}: {text}" if len(rentals) > 1 else text)
    await _safe_send(chat_id, "\n".join(lines))
    logger.info(f"[code] выдан код в чат {chat_id}")


async def _handle_time(chat_id: str):
    now = time.time()
    rentals = [r for r in await db.get_active_rentals(chat_id) if r["ends_at"] > now]
    if not rentals:
        await _safe_send(chat_id, config.NO_RENTAL_TEXT)
        return
    lines = []
    for r in rentals:
        text = config.TIME_TEXT.format(left=fmt_left(r["ends_at"] - now))
        lines.append(f"{r['login']}: {text}" if len(rentals) > 1 else text)
    await _safe_send(chat_id, "\n".join(lines))


# ---------- Обработка событий Playerok ----------

async def on_playerok_event(event):
    if isinstance(event, ChatInitializedEvent):
        return

    if isinstance(event, (ItemPaidEvent, NewDealEvent)):
        await handle_paid_deal(event.deal, event.chat.id if event.chat else None)
        return

    if isinstance(event, DealRolledBackEvent):
        deal = event.deal
        if not deal:
            return
        await db.remove_pending_deal(deal.id)
        rental = await db.get_rental_by_deal(deal.id)
        if rental and rental["status"] == "active":
            await finish_rental(rental, "refunded", False, "Возврат по сделке — аренда прекращена")
        elif not rental:
            await _relist_after_lost_sale(deal)
        return

    if isinstance(event, DealConfirmedEvent):
        rental = await db.get_rental_by_deal(event.deal.id) if event.deal else None
        if rental:
            await notify_admins(
                f"✅ Покупатель <b>{esc(rental['buyer_username'])}</b> подтвердил сделку "
                f"(аккаунт <code>{esc(rental['login'])}</code>)"
            )
        return

    if isinstance(event, DealHasProblemEvent):
        deal = event.deal
        rental = await db.get_rental_by_deal(deal.id) if deal else None
        login = rental["login"] if rental else "—"
        buyer = getattr(getattr(deal, "user", None), "username", "?")
        await notify_admins(
            f"🚨 <b>Проблема в сделке</b>\n\nПокупатель: {esc(buyer)}\nАккаунт: <code>{esc(login)}</code>"
        )
        return

    if isinstance(event, NewMessageEvent):
        msg = event.message
        if not msg or not msg.text:
            return
        if msg.user and client.acc and msg.user.id == client.acc.id:
            return
        command = msg.text.strip().lower()
        if command in config.CODE_COMMANDS:
            await _handle_code(event.chat.id)
        elif command in config.TIME_COMMANDS:
            await _handle_time(event.chat.id)
