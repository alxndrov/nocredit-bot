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
                    credit_type     TEXT    NOT NULL DEFAULT 'loan',
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
            # Миграции для старых БД
            for migration in [
                "ALTER TABLE credits ADD COLUMN credit_type TEXT NOT NULL DEFAULT 'loan'",
                "ALTER TABLE credits ADD COLUMN statement_day INTEGER DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN payday1_day INTEGER DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN payday1_amount REAL DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN payday2_day INTEGER DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN payday2_amount REAL DEFAULT NULL",
                "ALTER TABLE users ADD COLUMN payday_notify_enabled INTEGER DEFAULT 1",
                # Карты с formula+multiplier>=3 вводились как "% от баланса" → конвертируем в percent
                "UPDATE credits SET min_type='percent' WHERE credit_type='card' AND min_type='formula' AND min_multiplier >= 3",
                # Тип записи: 'payment' (платёж) или 'expense' (расход с карты)
                "ALTER TABLE payments ADD COLUMN type TEXT NOT NULL DEFAULT 'payment'",
                # Выписки: банк карты, лимит и «якорь» — точный долг на дату,
                # от которого долг считается по операциям из выписок
                "ALTER TABLE credits ADD COLUMN bank TEXT DEFAULT NULL",
                "ALTER TABLE credits ADD COLUMN credit_limit REAL DEFAULT NULL",
                "ALTER TABLE credits ADD COLUMN anchor_debt REAL DEFAULT NULL",
                "ALTER TABLE credits ADD COLUMN anchor_date TEXT DEFAULT NULL",
            ]:
                try:
                    await db.execute(migration)
                    await db.commit()
                except Exception:
                    pass  # Колонка уже существует

            # Миграция monthly_plan: пересоздаём с PRIMARY KEY включающим payday
            # (нужно для поддержки разбивки одного кредита на 2 выплаты)
            try:
                row = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='monthly_plan'"
                )
                row = await row.fetchone()
                if row and "payday" not in row[0].split("PRIMARY KEY")[1]:
                    # Старая схема без payday в PK — пересоздаём
                    await db.execute("ALTER TABLE monthly_plan RENAME TO monthly_plan_old")
                    await db.execute("""
                        CREATE TABLE monthly_plan (
                            user_id   INTEGER NOT NULL,
                            year      INTEGER NOT NULL,
                            month     INTEGER NOT NULL,
                            credit_id INTEGER NOT NULL,
                            payday    INTEGER NOT NULL DEFAULT 1,
                            amount    REAL    NOT NULL,
                            PRIMARY KEY (user_id, year, month, credit_id, payday)
                        )
                    """)
                    await db.execute("""
                        INSERT OR IGNORE INTO monthly_plan
                        SELECT user_id, year, month, credit_id, payday, amount
                        FROM monthly_plan_old
                    """)
                    await db.execute("DROP TABLE monthly_plan_old")
                    await db.commit()
            except Exception:
                pass

            # План платежей на месяц (для новых БД)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS monthly_plan (
                    user_id     INTEGER NOT NULL,
                    year        INTEGER NOT NULL,
                    month       INTEGER NOT NULL,
                    credit_id   INTEGER NOT NULL,
                    payday      INTEGER NOT NULL DEFAULT 1,
                    amount      REAL    NOT NULL,
                    PRIMARY KEY (user_id, year, month, credit_id, payday)
                )
            """)
            await db.commit()
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
            # Операции из банковских выписок.
            # amount: + пополнение карты, − списание с карты.
            # sheet_status: pending / synced / skipped (уже было в таблице или не пишется)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bank_txns (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    credit_id    INTEGER NOT NULL,
                    bank         TEXT    NOT NULL,
                    uid          TEXT    NOT NULL,
                    op_date      TEXT    NOT NULL,
                    op_time      TEXT    NOT NULL DEFAULT '',
                    amount       REAL    NOT NULL,
                    kind         TEXT    NOT NULL,
                    description  TEXT    NOT NULL,
                    sheet_status TEXT    NOT NULL DEFAULT 'pending',
                    created_at   TEXT    DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, uid)
                )
            """)
            # Перезапись бюджета на конкретный месяц
            await db.execute("""
                CREATE TABLE IF NOT EXISTS budget_overrides (
                    user_id INTEGER NOT NULL,
                    year    INTEGER NOT NULL,
                    month   INTEGER NOT NULL,
                    budget  REAL    NOT NULL,
                    PRIMARY KEY (user_id, year, month)
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

    async def get_users_with_payday_today(self, day: int) -> list:
        """Возвращает пользователей, у которых сегодня день аванса или зарплаты."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT *,
                   CASE WHEN payday1_day = ? THEN 1
                        WHEN payday2_day = ? THEN 2
                        ELSE 0 END AS payday_num
                   FROM users
                   WHERE payday1_day = ? OR payday2_day = ?""",
                (day, day, day, day)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def update_payday_notify(self, user_id: int, enabled: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET payday_notify_enabled = ? WHERE user_id = ?",
                (1 if enabled else 0, user_id)
            )
            await db.commit()

    async def update_budget(self, user_id: int, budget: float):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET monthly_budget = ? WHERE user_id = ?",
                (budget, user_id)
            )
            await db.commit()

    async def set_budget_override(self, user_id: int, year: int, month: int, budget: float):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO budget_overrides (user_id, year, month, budget) VALUES (?,?,?,?)",
                (user_id, year, month, budget)
            )
            await db.commit()

    async def get_budget_override(self, user_id: int, year: int, month: int) -> float | None:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT budget FROM budget_overrides WHERE user_id=? AND year=? AND month=?",
                (user_id, year, month)
            ) as cur:
                row = await cur.fetchone()
                return float(row[0]) if row else None

    async def delete_budget_override(self, user_id: int, year: int, month: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM budget_overrides WHERE user_id=? AND year=? AND month=?",
                (user_id, year, month)
            )
            await db.commit()

    # ── Кредиты ───────────────────────────────────────────────────────────────

    async def add_credit(self, user_id: int, name: str, debt: float,
                         rate_month: float, payment_day: int,
                         min_type: str, min_multiplier: float = 2.0,
                         min_fixed: float = None, priority: int = 1,
                         credit_type: str = 'loan',
                         statement_day: int = None) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """INSERT INTO credits
                   (user_id, name, credit_type, debt, rate_month, payment_day,
                    min_type, min_multiplier, min_fixed, priority, statement_day)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, name, credit_type, debt, rate_month, payment_day,
                 min_type, min_multiplier, min_fixed, priority, statement_day)
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

    async def update_credit_fields(self, credit_id: int, **fields):
        if not fields:
            return
        # Долг, введённый вручную, — это точный остаток из банка: делаем его якорем
        if "debt" in fields and "anchor_debt" not in fields:
            fields["anchor_debt"] = fields["debt"]
            fields["anchor_date"] = datetime.now().strftime("%Y-%m-%d")
        parts = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [credit_id]
        async with aiosqlite.connect(self.path) as db:
            await db.execute(f"UPDATE credits SET {parts} WHERE id = ?", values)
            await db.commit()

    # ── Выписки ───────────────────────────────────────────────────────────────

    async def get_credit_by_bank(self, user_id: int, bank: str) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM credits WHERE user_id = ? AND bank = ? AND is_active = 1",
                (user_id, bank)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def add_bank_txns(self, user_id: int, credit_id: int, bank: str, txns: list) -> list:
        """Сохраняет операции, пропуская уже известные. Возвращает только новые."""
        new = []
        async with aiosqlite.connect(self.path) as db:
            for t in txns:
                cur = await db.execute(
                    """INSERT OR IGNORE INTO bank_txns
                       (user_id, credit_id, bank, uid, op_date, op_time, amount, kind, description)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, credit_id, bank, t.uid, t.date, t.time, t.amount, t.kind, t.description)
                )
                if cur.rowcount:
                    new.append(t)
            await db.commit()
        return new

    async def set_anchor(self, credit_id: int, debt: float, anchor_date: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE credits SET anchor_debt = ?, anchor_date = ? WHERE id = ?",
                (debt, anchor_date, credit_id)
            )
            await db.commit()

    async def recompute_debt(self, credit_id: int) -> float | None:
        """Долг = якорь − операции из выписок после даты якоря.
        Без якоря долг не трогаем и возвращаем None."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT anchor_debt, anchor_date FROM credits WHERE id = ?", (credit_id,)
            ) as cur:
                row = await cur.fetchone()
            if not row or row[0] is None:
                return None
            anchor_debt, anchor_date = row
            async with db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM bank_txns WHERE credit_id = ? AND op_date > ?",
                (credit_id, anchor_date)
            ) as cur:
                moved = (await cur.fetchone())[0]
            debt = max(0.0, anchor_debt - moved)
            await db.execute("UPDATE credits SET debt = ? WHERE id = ?", (debt, credit_id))
            await db.commit()
            return debt

    async def get_txns(self, user_id: int, date_from: str, date_to: str = "9999-12-31") -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT * FROM bank_txns WHERE user_id = ? AND op_date >= ? AND op_date <= ?
                   ORDER BY op_date, op_time""",
                (user_id, date_from, date_to)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def get_pending_sheet_txns(self, credit_id: int) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM bank_txns WHERE credit_id = ? AND sheet_status = 'pending' ORDER BY op_date, op_time",
                (credit_id,)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def set_sheet_status(self, txn_ids: list, status: str):
        if not txn_ids:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany(
                "UPDATE bank_txns SET sheet_status = ? WHERE id = ?",
                [(status, i) for i in txn_ids]
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
                """INSERT INTO payments (credit_id, user_id, date, amount, interest, debt_after, type)
                   VALUES (?, ?, ?, ?, ?, ?, 'payment')""",
                (credit_id, user_id,
                 datetime.now().strftime("%d.%m.%Y"),
                 amount, interest, debt_after)
            )
            await db.commit()

    async def record_expense(self, credit_id: int, user_id: int,
                              amount: float, debt_after: float):
        """Записывает расход с кредитной карты (без процентов, тип expense)."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO payments (credit_id, user_id, date, amount, interest, debt_after, type)
                   VALUES (?, ?, ?, ?, 0, ?, 'expense')""",
                (credit_id, user_id,
                 datetime.now().strftime("%d.%m.%Y"),
                 amount, debt_after)
            )
            await db.commit()

    # ── Даты выплат ────────────────────────────────────────────────────────────

    async def update_user_paydays(self, user_id: int, p1_day: int, p1_amount: float,
                                   p2_day: int = None, p2_amount: float = None):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE users SET payday1_day=?, payday1_amount=?,
                                    payday2_day=?, payday2_amount=?
                   WHERE user_id=?""",
                (p1_day, p1_amount, p2_day, p2_amount, user_id)
            )
            await db.commit()

    # ── Месячный план ──────────────────────────────────────────────────────────

    async def get_monthly_plan(self, user_id: int, year: int, month: int) -> list:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT credit_id, payday, amount FROM monthly_plan
                   WHERE user_id=? AND year=? AND month=?""",
                (user_id, year, month)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def save_monthly_plan(self, user_id: int, year: int, month: int,
                                 allocations: list):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM monthly_plan WHERE user_id=? AND year=? AND month=?",
                (user_id, year, month)
            )
            for a in allocations:
                await db.execute(
                    """INSERT INTO monthly_plan
                       (user_id, year, month, credit_id, payday, amount)
                       VALUES (?,?,?,?,?,?)""",
                    (user_id, year, month, a["credit_id"], a["payday"], a["amount"])
                )
            await db.commit()

    async def update_plan_item(self, user_id: int, year: int, month: int,
                                credit_id: int, payday: int, amount: float):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO monthly_plan
                   (user_id, year, month, credit_id, payday, amount)
                   VALUES (?,?,?,?,?,?)""",
                (user_id, year, month, credit_id, payday, amount)
            )
            await db.commit()

    async def get_all_users(self) -> list:
        """Все пользователи (для ежемесячной рассылки)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def get_payments_for_month(self, user_id: int, year: int, month: int) -> list:
        """Платежи за конкретный месяц (формат даты dd.mm.yyyy)."""
        month_str = f".{month:02d}.{year}"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """SELECT p.*, c.name as credit_name FROM payments p
                   JOIN credits c ON p.credit_id = c.id
                   WHERE p.user_id = ? AND p.date LIKE ?
                   ORDER BY p.id DESC""",
                (user_id, f"%{month_str}")
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

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
