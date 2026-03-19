import aiosqlite
from datetime import datetime

DB_PATH = "creditbot.db"


class Database:
    def __init__(self):
        self.path = DB_PATH

    async def setup(self):
        async with aiosqlite.connect(self.path) as db:
            # Пользователи
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id         INTEGER PRIMARY KEY,
                    monthly_budget  REAL    DEFAULT 80000,
                    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Кредиты
            await db.execute("""
                CREATE TABLE IF NOT EXISTS credits (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         INTEGER NOT NULL,
                    name            TEXT    NOT NULL,
                    debt            REAL    NOT NULL,
                    rate_month      REAL    NOT NULL,
                    payment_day     INTEGER NOT NULL,
                    min_type        TEXT    NOT NULL DEFAULT 'formula',
                    min_multiplier  REAL    DEFAULT 2.0,
                    min_fixed       REAL    DEFAULT NULL,
                    priority        INTEGER DEFAULT 1,
                    is_active       INTEGER DEFAULT 1,
                    total_paid      REAL    DEFAULT 0,
                    created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Напоминания для каждого кредита
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    credit_id   INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    days_before INTEGER NOT NULL,
                    hour        INTEGER DEFAULT 10,
                    minute      INTEGER DEFAULT 0,
                    is_active   INTEGER DEFAULT 1
                )
            """)
            # История платежей
            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    credit_id   INTEGER NOT NULL,
                    user_id     INTEGER NOT NULL,
                    date        TEXT    NOT NULL,
                    amount      REAL    NOT NULL,
                    interest    REAL    NOT NULL,
                    debt_after  REAL    NOT NULL
                )
            """)
            await db.commit()

    # ── Пользователи ──────────────────────────────────────────────────────────

    async def init_user(self, user_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
            )
            await db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def update_budget(self, user_id: int, budget: float):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET monthly_budget = ? WHERE user_id = ?",
                (budget, user_id)
            )
            await db.commit()

    # ── Кредиты ───────────────────────────────────────────────────────────────

    async def add_credit(self, user_id: int, name: str, debt: float,
                         rate_month: float, payment_day: int,
                         min_type: str, min_multiplier: float = 2.0,
                         min_fixed: float = None, priority: int = 1) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO credits
                   (user_id, name, debt, rate_month, payment_day,
                    min_type, min_multiplier, min_fixed, priority)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, name, debt, rate_month, payment_day,
                 min_type, min_multiplier, min_fixed, priority)
            )
            await db.commit()
            return cur.lastrowid

    async def get_credits(self, user_id: int, active_only=True) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            where = "WHERE user_id = ? AND is_active = 1" if active_only else "WHERE user_id = ?"
            async with db.execute(
                f"SELECT * FROM credits {where} ORDER BY priority ASC, id ASC",
                (user_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_credit(self, credit_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM credits WHERE id = ?", (credit_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def update_credit_debt(self, credit_id: int, new_debt: float, add_paid: float):
        async with aiosqlite.connect(self.path) as db:
            if new_debt < 0.01:
                await db.execute(
                    "UPDATE credits SET debt = 0, is_active = 0, total_paid = total_paid + ? WHERE id = ?",
                    (add_paid, credit_id)
                )
            else:
                await db.execute(
                    "UPDATE credits SET debt = ?, total_paid = total_paid + ? WHERE id = ?",
                    (new_debt, add_paid, credit_id)
                )
            await db.commit()

    async def update_credit_priority(self, credit_id: int, priority: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE credits SET priority = ? WHERE id = ?", (priority, credit_id)
            )
            await db.commit()

    async def delete_credit(self, credit_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE credits SET is_active = 0 WHERE id = ?", (credit_id,))
            await db.execute("UPDATE reminders SET is_active = 0 WHERE credit_id = ?", (credit_id,))
            await db.commit()

    # ── Напоминания ───────────────────────────────────────────────────────────

    async def add_reminder(self, credit_id: int, user_id: int,
                            days_before: int, hour: int = 10, minute: int = 0) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO reminders (credit_id, user_id, days_before, hour, minute)
                   VALUES (?, ?, ?, ?, ?)""",
                (credit_id, user_id, days_before, hour, minute)
            )
            await db.commit()
            return cur.lastrowid

    async def get_reminders(self, credit_id: int) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders WHERE credit_id = ? AND is_active = 1 ORDER BY days_before DESC",
                (credit_id,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_all_reminders(self) -> list:
        """Все активные напоминания для планировщика."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT r.*, c.name as credit_name, c.payment_day, c.debt,
                       c.rate_month, c.is_active as credit_active
                FROM reminders r
                JOIN credits c ON r.credit_id = c.id
                WHERE r.is_active = 1 AND c.is_active = 1
            """) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_reminder(self, reminder_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def delete_reminder(self, reminder_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE reminders SET is_active = 0 WHERE id = ?", (reminder_id,)
            )
            await db.commit()

    # ── Платежи ───────────────────────────────────────────────────────────────

    async def record_payment(self, credit_id: int, user_id: int,
                              amount: float, interest: float, debt_after: float):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO payments (credit_id, user_id, date, amount, interest, debt_after)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (credit_id, user_id,
                 datetime.now().strftime("%d.%m.%Y"),
                 amount, interest, debt_after)
            )
            await db.commit()

    async def get_history(self, user_id: int, credit_id: int = None, limit: int = 8) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if credit_id:
                async with db.execute(
                    """SELECT p.*, c.name as credit_name FROM payments p
                       JOIN credits c ON p.credit_id = c.id
                       WHERE p.user_id = ? AND p.credit_id = ?
                       ORDER BY p.id DESC LIMIT ?""",
                    (user_id, credit_id, limit)
                ) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute(
                    """SELECT p.*, c.name as credit_name FROM payments p
                       JOIN credits c ON p.credit_id = c.id
                       WHERE p.user_id = ?
                       ORDER BY p.id DESC LIMIT ?""",
                    (user_id, limit)
                ) as cur:
                    rows = await cur.fetchall()
            return [dict(r) for r in rows]
