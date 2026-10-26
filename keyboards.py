import time

from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup

from utils import fmt_left


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(count: int = 0) -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text=f"📋 Товары ({count})", callback_data="accounts"), Btn(text="🆕 Новый товар", callback_data="add")],
        [Btn(text="📊 Аренды", callback_data="rentals"), Btn(text="💰 Баланс", callback_data="playerok")],
        [Btn(text="⚙️ Настройки", callback_data="pubset")],
    ])


def back(to: str = "menu", text: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    return _kb([[Btn(text=text, callback_data=to)]])


# ---------- Товары ----------

def account_status_icon(account: dict) -> str:
    if account.get("rental_id"):
        return "🔴"
    if account.get("needs_rotation"):
        return "⛔"
    if not account["enabled"]:
        return "⏸"
    return "🟢"


def accounts_list(accounts: list[dict]) -> InlineKeyboardMarkup:
    now = time.time()
    rows = []
    for a in accounts:
        label = f"{account_status_icon(a)} {a['login']}"
        if a.get("rental_id"):
            label += f" · {fmt_left(a['ends_at'] - now)}"
        rows.append([Btn(text=label, callback_data=f"acc:{a['id']}")])
    rows.append([Btn(text="🆕 Новый товар", callback_data="add"), Btn(text="🔄 Обновить", callback_data="accounts")])
    rows.append([Btn(text="⬅️ Меню", callback_data="menu")])
    return _kb(rows)


def account_card(account: dict, rental: dict | None) -> InlineKeyboardMarkup:
    """Только ежедневные действия; настройки товара — в «Редактировать»."""
    aid = account["id"]
    rows = [[Btn(text="🔑 Код Guard", callback_data=f"code:{aid}")]]
    if rental:
        rows.append([
            Btn(text="⏱ +30 мин", callback_data=f"ext:{aid}:30"),
            Btn(text="⏱ +1 ч", callback_data=f"ext:{aid}:60"),
            Btn(text="⏹ Завершить", callback_data=f"end:{aid}"),
        ])
    elif account.get("needs_rotation"):
        rows.append([Btn(text="🔁 Повторить смену пароля", callback_data=f"rotretry:{aid}")])
    else:
        rows[0].append(Btn(text="📤 Выставить лот", callback_data=f"repub:{aid}"))
    rows.append([Btn(text="✏️ Редактировать", callback_data=f"pedit:{aid}")])
    rows.append([
        Btn(text="⏸ Выключить" if account["enabled"] else "▶️ Включить", callback_data=f"toggle:{aid}"),
        Btn(text="🗑 Удалить", callback_data=f"del:{aid}"),
    ])
    rows.append([Btn(text="⬅️ К товарам", callback_data="accounts")])
    return _kb(rows)


PUBLISH_MODE_NAMES = {None: "как в настройках", "premium": "⭐ премиум", "default": "обычное"}
PUBLISH_MODE_CYCLE = [None, "premium", "default"]


def item_edit(account: dict, images_count: int) -> InlineKeyboardMarkup:
    aid = account["id"]
    prime = "—" if account.get("prime") is None else ("да" if account["prime"] else "нет")
    mode = PUBLISH_MODE_NAMES.get(account.get("publish_mode"), "как в настройках")
    return _kb([
        [Btn(text="📝 Название", callback_data=f"edit:{aid}:title"),
         Btn(text="📄 Описание", callback_data=f"edit:{aid}:description")],
        [Btn(text="💰 Цена", callback_data=f"edit:{aid}:price"),
         Btn(text="⏱ Срок", callback_data=f"edit:{aid}:rental_hours")],
        [Btn(text=f"⭐ Prime: {prime}", callback_data=f"prime_toggle:{aid}"),
         Btn(text=f"🖼 Картинки ({images_count})", callback_data=f"edit:{aid}:images")],
        [Btn(text=f"🎯 Выставление: {mode}", callback_data=f"pmode:{aid}")],
        [Btn(text="👤 Логин", callback_data=f"edit:{aid}:login"),
         Btn(text="🔑 Пароль", callback_data=f"edit:{aid}:password"),
         Btn(text="📁 maFile", callback_data=f"edit:{aid}:mafile")],
        [Btn(text="🔁 Сменить пароль в Steam", callback_data=f"rotate:{aid}"),
         Btn(text="🔗 Привязка лота", callback_data=f"edit:{aid}:lot")],
        [Btn(text="⬅️ К товару", callback_data=f"acc:{aid}")],
    ])


def confirm(action: str, aid: int) -> InlineKeyboardMarkup:
    return _kb([[
        Btn(text="✅ Да", callback_data=f"{action}!:{aid}"),
        Btn(text="❌ Нет", callback_data=f"acc:{aid}"),
    ]])


# ---------- Аренды ----------

def rentals(active: list[dict]) -> InlineKeyboardMarkup:
    now = time.time()
    rows = [[Btn(text=f"🔴 {r['login']} · {fmt_left(r['ends_at'] - now)}", callback_data=f"acc:{r['account_id']}")]
            for r in active]
    rows.append([Btn(text="🔄 Обновить", callback_data="rentals"), Btn(text="⬅️ Меню", callback_data="menu")])
    return _kb(rows)


# ---------- Баланс и вывод ----------

def finance() -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text="💸 Вывести на TRX-кошелёк", callback_data="wd")],
        [Btn(text="🔄 Обновить", callback_data="playerok"), Btn(text="⬅️ Меню", callback_data="menu")],
    ])


def withdraw_amount(withdrawable: int) -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text=f"Всё доступное ({withdrawable} ₽)", callback_data="wd_all")],
        [Btn(text="❌ Отмена", callback_data="cancel")],
    ])


def withdraw_confirm() -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text="✅ Подтвердить вывод", callback_data="wd_go")],
        [Btn(text="👛 Другой кошелёк", callback_data="wd_wallet"), Btn(text="❌ Отмена", callback_data="cancel")],
    ])


# ---------- Настройки ----------

def _flag(value: bool) -> str:
    return "✅" if value else "❌"


def publish_settings(premium: bool, fallback: bool) -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text=f"Выставление: {'⭐ премиум' if premium else 'обычное'} ⇄", callback_data="set:publish_premium")],
        [Btn(text=f"{_flag(fallback)} Обычное, если не хватает на премиум", callback_data="set:premium_fallback")],
        [Btn(text="🍪 Вход в Playerok (cookies, прокси)", callback_data="cookies")],
        [Btn(text="⬅️ Меню", callback_data="menu")],
    ])


def cookies_menu() -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text="🍪 Обновить cookies", callback_data="ck:cookies"), Btn(text="🌐 Прокси", callback_data="ck:proxy")],
        [Btn(text="🔄 Проверить вход", callback_data="ck_check"), Btn(text="⬅️ Настройки", callback_data="pubset")],
    ])


# ---------- Ввод данных ----------

def cancel() -> InlineKeyboardMarkup:
    return _kb([[Btn(text="❌ Отмена", callback_data="cancel")]])


def default_or_cancel() -> InlineKeyboardMarkup:
    return _kb([[Btn(text="📄 Стандартное", callback_data="default"), Btn(text="❌ Отмена", callback_data="cancel")]])


def yes_no(prefix: str) -> InlineKeyboardMarkup:
    return _kb([
        [Btn(text="✅ Да", callback_data=f"{prefix}:yes"), Btn(text="❌ Нет", callback_data=f"{prefix}:no")],
        [Btn(text="Отмена", callback_data="cancel")],
    ])


def images_done(count: int) -> InlineKeyboardMarkup:
    return _kb([[Btn(text=f"✅ Готово ({count})", callback_data="img_done"),
                 Btn(text="❌ Отмена", callback_data="cancel")]])


def publish_item(premium_default: bool) -> InlineKeyboardMarkup:
    mark = lambda on: " ✓" if on else ""
    return _kb([
        [Btn(text=f"🚀 Выставить обычным{mark(not premium_default)}", callback_data="pub:default")],
        [Btn(text=f"⭐ Выставить с премиумом{mark(premium_default)}", callback_data="pub:premium")],
        [Btn(text="❌ Отмена", callback_data="cancel")],
    ])
