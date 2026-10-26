import html
import json
import re
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def fmt_ts(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def fmt_left(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def lot_tokens(lot: str | None) -> list[str]:
    if not lot:
        return []
    tokens = []
    for token in re.split(r"[,;\n]", lot):
        token = token.strip()
        if not token:
            continue
        # Ссылка на лот -> slug
        match = re.search(r"playerok\.com/products/([^/?#\s]+)", token)
        tokens.append(match.group(1) if match else token)
    return tokens


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-([0-9a-f]{12})$", re.IGNORECASE)


def lot_matches(lot: str | None, item_id: str | None, slug: str | None, name: str | None) -> bool:
    """Проверяет, относится ли лот Playerok к привязке аккаунта.

    Привязка — список через запятую: ID лота, slug/ссылка или часть названия.
    При покупке Playerok создаёт для сделки копию лота с новым ID, но slug копии
    начинается с последнего блока ID исходного лота: a1b2c3d4e5f6-arenda-cs2-... —
    по нему сделка и сопоставляется с выставленным лотом.
    """
    name_lower = (name or "").lower()
    slug_lower = (slug or "").lower()
    for token in lot_tokens(lot):
        if token == item_id or (slug and token == slug):
            return True
        uuid = _UUID_RE.match(token)
        if uuid and slug_lower.startswith(uuid.group(1).lower() + "-"):
            return True
        if not uuid and len(token) >= 3 and token.lower() in name_lower:
            return True
    return False


# Цены «до скидки»: лот выставляется по ближайшей большей, затем цена меняется на желаемую
LIST_PRICE_TIERS = (499, 999, 2499)


def list_price(desired: int) -> int:
    """Минимальная из 499/999/2499, которая больше желаемой; если таких нет — сама желаемая."""
    return next((tier for tier in LIST_PRICE_TIERS if tier > desired), desired)


_HOURS_RE = re.compile(r"(\d+)\s*(?:ч\b|час|h\b|hour)", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*(?:д\b|дн|сут|day)", re.IGNORECASE)


def hours_from_item(item) -> int | None:
    """Определяет длительность аренды по атрибутам или названию лота."""
    if item is None:
        return None
    attributes = getattr(item, "attributes", None) or {}
    if isinstance(attributes, dict):
        for key in ("amount", "hours", "rental-hours"):
            value = attributes.get(key)
            try:
                if value is not None and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                pass
    name = getattr(item, "name", "") or ""
    match = _HOURS_RE.search(name)
    if match:
        return int(match.group(1))
    match = _DAYS_RE.search(name)
    if match:
        return int(match.group(1)) * 24
    return None


def parse_cookies(text: str) -> dict[str, str]:
    """Разбирает cookies Playerok из строки заголовка Cookie, JSON-экспорта
    расширения (Cookie-Editor / EditThisCookie) или одного значения token."""
    text = (text or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    cookies: dict[str, str] = {}
    if text.startswith("[") or text.startswith("{"):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("cookies", data)
        if isinstance(data, dict):
            cookies = {str(k): str(v) for k, v in data.items()}
        else:
            for c in data:
                domain = str(c.get("domain", "playerok.com"))
                if "playerok" in domain and c.get("name"):
                    cookies[c["name"]] = str(c.get("value", ""))
    elif "=" in text:
        for part in text.replace("\n", ";").split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                if name.strip():
                    cookies[name.strip()] = value.strip()
    elif text:
        cookies = {"token": text}
    if not cookies.get("token"):
        raise ValueError("В cookies нет token")
    return cookies


def mask(value: str | None, keep: int = 6) -> str:
    if not value:
        return "—"
    return value if len(value) <= keep * 2 else f"{value[:keep]}…{value[-keep:]}"


# Мажорная версия Chrome должна совпадать с заголовками sec-ch-ua, которые шлёт playerokapi
CHROME_MAJOR = 146


def generate_user_agent() -> str:
    """User-Agent десктопного Chrome под Windows, согласованный с client hints библиотеки.

    Chrome отдаёт «урезанный» UA: минорные части версии всегда 0.0.0, платформа — Windows NT 10.0
    и для Windows 10, и для 11.
    """
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
    )
