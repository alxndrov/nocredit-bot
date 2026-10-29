"""
Прогноз кешфлоу по проверкам остатка.

Темп трат между двумя проверками:
    (остаток тогда + зарплата за период − остаток сейчас − обязательные платежи за период) / дней
Обязательные платежи считаются оплаченными по графику. Расход без дня
размазывается равномерно по месяцу.
"""
import calendar
from datetime import date, datetime, timedelta

HORIZON_DAYS = 30
MAX_RUNWAY_DAYS = 180


def _pay_day(d: date, day: int) -> int:
    return min(day, calendar.monthrange(d.year, d.month)[1])


def payments_on(d: date, obligations: list, loans: list) -> list:
    """Обязательные платежи, которые приходятся на день d: [(название, сумма)]."""
    res = [(o["name"], o["amount"]) for o in obligations if o["day"] and d.day == _pay_day(d, o["day"])]
    res += [(l["name"], l["payment"]) for l in loans if d.day == _pay_day(d, l["payment_day"])]
    return res


def _daily_undated(obligations: list) -> float:
    return sum(o["amount"] for o in obligations if not o["day"]) / 30


def mandatory_between(start: date, end: date, obligations: list, loans: list) -> float:
    """Сумма обязательных платежей в днях (start, end]."""
    total, d = 0.0, start + timedelta(days=1)
    while d <= end:
        total += sum(a for _, a in payments_on(d, obligations, loans)) + _daily_undated(obligations)
        d += timedelta(days=1)
    return total


def upcoming(today: date, obligations: list, loans: list, days: int = HORIZON_DAYS) -> list:
    """Обязательные платежи на ближайшие days дней: [(дата, название, сумма)]."""
    res = []
    for i in range(1, days + 1):
        d = today + timedelta(days=i)
        res += [(d, name, amount) for name, amount in payments_on(d, obligations, loans)]
    return res


def _pace(older: dict, newer: dict, incomes: list, obligations: list, loans: list) -> float | None:
    t0, t1 = datetime.fromisoformat(older["date"]), datetime.fromisoformat(newer["date"])
    days = (t1 - t0).total_seconds() / 86400
    if days < 1:
        return None
    income = sum(i["amount"] for i in incomes if older["date"] < i["date"] <= newer["date"])
    mandatory = mandatory_between(t0.date(), t1.date(), obligations, loans)
    return (older["balance"] + income - newer["balance"] - mandatory) / days


def analyze(checks: list, incomes: list, obligations: list, loans: list, today: date) -> dict:
    """checks — проверки остатка от новых к старым (минимум одна)."""
    now = checks[0]
    balance = now["balance"]

    # Темп за последний период и средний за ~4 недели
    prev = next((c for c in checks[1:]
                 if (datetime.fromisoformat(now["date"]) - datetime.fromisoformat(c["date"])).days >= 1), None)
    pace = _pace(prev, now, incomes, obligations, loans) if prev else None
    month_ago = (datetime.fromisoformat(now["date"]) - timedelta(days=28)).isoformat()
    oldest = next((c for c in reversed(checks) if c["date"] >= month_ago and c is not now), None)
    pace_avg = _pace(oldest, now, incomes, obligations, loans) if oldest and oldest is not prev else None

    ahead = upcoming(today, obligations, loans)
    mandatory_next = sum(a for _, _, a in ahead) + _daily_undated(obligations) * HORIZON_DAYS
    free = balance - mandatory_next
    allowed = free / HORIZON_DAYS

    res = {
        "balance": balance, "pace": pace, "pace_avg": pace_avg, "income_period": 0.0,
        "mandatory_next": mandatory_next, "free": free, "allowed": allowed,
        "upcoming": ahead, "run_out": None, "run_out_payment": None, "unexplained_growth": False,
    }
    if prev:
        res["income_period"] = sum(i["amount"] for i in incomes if prev["date"] < i["date"] <= now["date"])
        res["period_days"] = (datetime.fromisoformat(now["date"]) - datetime.fromisoformat(prev["date"])).days
    if pace is not None and pace < 0:
        res["unexplained_growth"] = True

    # Когда кончатся деньги при текущем темпе (или, если темпа ещё нет, только на обязательных)
    daily = max(pace or 0.0, 0.0) + _daily_undated(obligations)
    bal = balance
    for i in range(1, MAX_RUNWAY_DAYS + 1):
        d = today + timedelta(days=i)
        pays = payments_on(d, obligations, loans)
        bal -= daily + sum(a for _, a in pays)
        if bal < 0:
            res["run_out"] = d
            res["run_out_payment"] = pays[0] if pays else None
            break

    if free < 0:
        res["level"] = "red_mandatory"
    elif pace is None:
        res["level"] = "first"
    elif res["run_out"] and (res["run_out"] - today).days <= HORIZON_DAYS:
        res["level"] = "red"
    elif pace > allowed * 0.85:
        res["level"] = "yellow"
    else:
        res["level"] = "green"
    return res
