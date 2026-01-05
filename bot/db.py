import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _month_key(dt: Optional[datetime] = None) -> str:
    dt = dt or _utc_now()
    return dt.strftime("%Y-%m")


def _day_key(dt: Optional[datetime] = None) -> str:
    dt = dt or _utc_now()
    return dt.strftime("%Y-%m-%d")


class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self.init_schema()
        await self.ensure_default_plans()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def init_schema(self):
        assert self._conn is not None

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_monthly (
                user_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, month)
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                day TEXT NOT NULL,
                user_id INTEGER,
                event TEXT NOT NULL,
                meta TEXT
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                title TEXT,
                price_uah INTEGER,
                credits INTEGER,
                is_subscription INTEGER,
                is_active INTEGER,
                created_at TEXT
            );
            """
        )
        await self._conn.commit()

    async def ensure_default_plans(self):
        assert self._conn is not None

        cur = await self._conn.execute("SELECT COUNT(*) FROM plans")
        row = await cur.fetchone()
        if row[0] > 0:
            return

        now = _utc_now().isoformat()
        plans = [
            ("p1", "1 фото", 5, 1, 0),
            ("p10", "10 фото", 45, 10, 0),
            ("p30", "30 фото", 120, 30, 0),
            ("sub100", "100 фото / мес", 199, 100, 1),
        ]

        for code, title, price, credits, sub in plans:
            await self._conn.execute(
                """
                INSERT INTO plans
                (code, title, price_uah, credits, is_subscription, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (code, title, price, credits, sub, now),
            )

        await self._conn.commit()

    # ---------- USERS ----------

    async def touch_user(self, user_id: int):
        assert self._conn is not None
        now = _utc_now().isoformat()

        await self._conn.execute(
            """
            INSERT INTO users (user_id, first_seen, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
            """,
            (user_id, now, now),
        )
        await self._conn.commit()

    # ---------- USAGE ----------

    async def get_used_this_month(self, user_id: int) -> int:
        assert self._conn is not None
        mk = _month_key()

        cur = await self._conn.execute(
            "SELECT used FROM usage_monthly WHERE user_id=? AND month=?",
            (user_id, mk),
        )
        row = await cur.fetchone()
        return row["used"] if row else 0

    async def inc_used_this_month(self, user_id: int):
        assert self._conn is not None
        mk = _month_key()
        now = _utc_now().isoformat()

        await self._conn.execute(
            """
            INSERT INTO usage_monthly (user_id, month, used, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, month)
            DO UPDATE SET used = used + 1, updated_at = excluded.updated_at
            """,
            (user_id, mk, now),
        )
        await self._conn.commit()

    # ---------- EVENTS ----------

    async def log_event(self, event: str, user_id: int | None = None, meta: str | None = None):
        assert self._conn is not None
        now = _utc_now()

        await self._conn.execute(
            "INSERT INTO events (ts, day, user_id, event, meta) VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(), _day_key(now), user_id, event, meta),
        )
        await self._conn.commit()

    # ---------- STATS ----------

    async def list_plans(self) -> List[Dict[str, Any]]:
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT code, title, price_uah, credits, is_subscription FROM plans WHERE is_active=1"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
