import time

import aiosqlite

import config
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS steam_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                shared_secret TEXT NOT NULL,
                identity_secret TEXT,
                lot TEXT,
                rental_hours INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rentals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                deal_id TEXT UNIQUE,
                chat_id TEXT,
                item_id TEXT,
                item_name TEXT,
                buyer_id TEXT,
                buyer_username TEXT,
                price INTEGER,
                hours INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                warned INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES steam_accounts(id) ON DELETE CASCADE
            )
        """)
        # Сделки, по которым не нашлось свободного аккаунта
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_deals (
                deal_id TEXT PRIMARY KEY,
                chat_id TEXT,
                item_id TEXT,
                item_name TEXT,
                buyer_id TEXT,
                buyer_username TEXT,
                price INTEGER,
                hours INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # last_item_id — последний лот аккаунта (шаблон для перевыставления),
        # publish_mode — NULL (как в настройках) / 'premium' / 'default' / 'off'
        # title/description/price/prime — данные лота (шаблон для выставления и перевыставления)
        for col in ["last_item_id TEXT", "publish_mode TEXT", "title TEXT", "description TEXT",
                    "price INTEGER", "prime INTEGER", "needs_rotation INTEGER DEFAULT 0"]:
            try:
                await db.execute(f"ALTER TABLE steam_accounts ADD COLUMN {col}")
            except Exception:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rentals_status ON rentals(status)")
        await db.commit()


async def _fetchall(query: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]


async def _fetchone(query: str, params: tuple = ()) -> dict | None:
    rows = await _fetchall(query, params)
    return rows[0] if rows else None


async def _execute(query: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        cursor = await db.execute(query, params)
        await db.commit()
        return cursor.lastrowid


# ---------- Steam-аккаунты ----------

async def add_account(login, password, shared_secret, identity_secret, lot, rental_hours) -> int:
    return await _execute(
        "INSERT INTO steam_accounts (login, password, shared_secret, identity_secret, lot, rental_hours, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (login, password, shared_secret, identity_secret, lot, rental_hours, int(time.time())),
    )


async def get_account(account_id: int) -> dict | None:
    return await _fetchone("SELECT * FROM steam_accounts WHERE id = ?", (account_id,))


async def get_account_by_login(login: str) -> dict | None:
    return await _fetchone("SELECT * FROM steam_accounts WHERE LOWER(login) = LOWER(?)", (login,))


async def get_accounts() -> list[dict]:
    """Все аккаунты вместе с текущей активной арендой (если есть)."""
    return await _fetchall("""
        SELECT a.*, r.id AS rental_id, r.ends_at, r.buyer_username, r.chat_id
        FROM steam_accounts a
        LEFT JOIN rentals r ON r.account_id = a.id AND r.status = 'active'
        ORDER BY a.id
    """)


async def get_free_accounts() -> list[dict]:
    return await _fetchall("""
        SELECT a.* FROM steam_accounts a
        WHERE a.enabled = 1
          AND NOT EXISTS (SELECT 1 FROM rentals r WHERE r.account_id = a.id AND r.status = 'active')
        ORDER BY a.id
    """)


_ACCOUNT_FIELDS = {"login", "password", "shared_secret", "identity_secret", "lot", "rental_hours", "enabled",
                   "note", "last_item_id", "publish_mode", "title", "description", "price", "prime",
                   "needs_rotation"}


async def update_account(account_id: int, **fields):
    fields = {k: v for k, v in fields.items() if k in _ACCOUNT_FIELDS}
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    await _execute(f"UPDATE steam_accounts SET {sets} WHERE id = ?", (*fields.values(), account_id))


async def delete_account(account_id: int):
    await _execute("DELETE FROM rentals WHERE account_id = ?", (account_id,))
    await _execute("DELETE FROM steam_accounts WHERE id = ?", (account_id,))


# ---------- Аренды ----------

async def deal_known(deal_id: str) -> bool:
    row = await _fetchone(
        "SELECT 1 AS x FROM rentals WHERE deal_id = ? UNION SELECT 1 FROM pending_deals WHERE deal_id = ?",
        (deal_id, deal_id),
    )
    return row is not None


async def create_rental(account_id, deal_id, chat_id, item_id, item_name,
                        buyer_id, buyer_username, price, hours, ends_at) -> int:
    return await _execute(
        "INSERT INTO rentals (account_id, deal_id, chat_id, item_id, item_name, buyer_id, buyer_username, "
        "price, hours, started_at, ends_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, deal_id, chat_id, item_id, item_name, buyer_id, buyer_username,
         price, hours, int(time.time()), ends_at),
    )


async def get_rental(rental_id: int) -> dict | None:
    return await _fetchone("""
        SELECT r.*, a.login, a.password, a.shared_secret, a.identity_secret
        FROM rentals r JOIN steam_accounts a ON a.id = r.account_id
        WHERE r.id = ?
    """, (rental_id,))


async def get_rental_by_deal(deal_id: str) -> dict | None:
    return await _fetchone("""
        SELECT r.*, a.login FROM rentals r JOIN steam_accounts a ON a.id = r.account_id
        WHERE r.deal_id = ?
    """, (deal_id,))


async def get_active_rentals(chat_id: str | None = None) -> list[dict]:
    query = """
        SELECT r.*, a.login, a.password, a.shared_secret, a.identity_secret
        FROM rentals r JOIN steam_accounts a ON a.id = r.account_id
        WHERE r.status = 'active'
    """
    params: tuple = ()
    if chat_id is not None:
        query += " AND r.chat_id = ?"
        params = (chat_id,)
    return await _fetchall(query + " ORDER BY r.ends_at", params)


async def get_active_rental_for_account(account_id: int) -> dict | None:
    return await _fetchone(
        "SELECT * FROM rentals WHERE account_id = ? AND status = 'active'", (account_id,)
    )


async def get_recent_rentals(limit: int = 10) -> list[dict]:
    return await _fetchall("""
        SELECT r.*, a.login FROM rentals r JOIN steam_accounts a ON a.id = r.account_id
        ORDER BY r.id DESC LIMIT ?
    """, (limit,))


async def set_rental_status(rental_id: int, status: str):
    await _execute("UPDATE rentals SET status = ? WHERE id = ?", (status, rental_id))


async def extend_rental(rental_id: int, seconds: int):
    await _execute(
        "UPDATE rentals SET ends_at = ends_at + ?, warned = 0 WHERE id = ?", (seconds, rental_id)
    )


async def mark_warned(rental_id: int):
    await _execute("UPDATE rentals SET warned = 1 WHERE id = ?", (rental_id,))


async def get_stats() -> dict:
    row = await _fetchone("""
        SELECT COUNT(*) AS total, COALESCE(SUM(price), 0) AS revenue,
               SUM(CASE WHEN started_at >= ? THEN 1 ELSE 0 END) AS today,
               COALESCE(SUM(CASE WHEN started_at >= ? THEN price ELSE 0 END), 0) AS today_revenue
        FROM rentals WHERE status != 'refunded'
    """, (_day_start(), _day_start()))
    return row or {"total": 0, "revenue": 0, "today": 0, "today_revenue": 0}


def _day_start() -> int:
    # Начало текущих суток по МСК
    now = int(time.time())
    msk = now + 3 * 3600
    return now - msk % 86400


# ---------- Ожидающие сделки ----------

async def add_pending_deal(deal_id, chat_id, item_id, item_name, buyer_id, buyer_username, price, hours):
    await _execute(
        "INSERT OR IGNORE INTO pending_deals (deal_id, chat_id, item_id, item_name, buyer_id, buyer_username, "
        "price, hours, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (deal_id, chat_id, item_id, item_name, buyer_id, buyer_username, price, hours, int(time.time())),
    )


async def get_pending_deals() -> list[dict]:
    return await _fetchall("SELECT * FROM pending_deals ORDER BY created_at")


async def get_pending_deal(deal_id: str) -> dict | None:
    return await _fetchone("SELECT * FROM pending_deals WHERE deal_id = ?", (deal_id,))


async def remove_pending_deal(deal_id: str):
    await _execute("DELETE FROM pending_deals WHERE deal_id = ?", (deal_id,))


# ---------- Настройки ----------

SETTING_DEFAULTS = {
    "publish_premium": "1" if config.PUBLISH_PREMIUM else "0",
    "premium_fallback": "1",
    "trx_wallet": "",
}


async def get_setting(key: str) -> str:
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else SETTING_DEFAULTS.get(key, "")


async def get_setting_raw(key: str) -> str | None:
    """Значение настройки или None, если она ни разу не сохранялась."""
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else None


async def get_bool_setting(key: str) -> bool:
    return await get_setting(key) == "1"


async def set_setting(key: str, value):
    if isinstance(value, bool):
        value = "1" if value else "0"
    await _execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
