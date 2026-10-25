"""
Таблица cashflow в Google Sheets: дописывание операций из выписок
и чтение итогов месяца. Работает через сервисный аккаунт Google:
GOOGLE_CREDENTIALS — путь к JSON-ключу, CASHFLOW_SHEET_ID — id таблицы
(таблицу нужно расшарить на email сервисного аккаунта с правом редактора).
Если что-то не настроено, бот работает без таблицы.
"""
import asyncio
import os
from datetime import date, timedelta

GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
CASHFLOW_SHEET_ID = os.getenv("CASHFLOW_SHEET_ID")
WORKSHEET = "cashflow"

_ws = None


def is_configured() -> bool:
    return bool(GOOGLE_CREDENTIALS and CASHFLOW_SHEET_ID and os.path.exists(GOOGLE_CREDENTIALS))


def _worksheet():
    global _ws
    if _ws is None:
        import gspread
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS)
        _ws = gc.open_by_key(CASHFLOW_SHEET_ID).worksheet(WORKSHEET)
    return _ws


def _to_date(v) -> date | None:
    """Дата из ячейки: число (серийный номер Google) или строка дд.мм.гггг."""
    if isinstance(v, (int, float)) and v > 0:
        return date(1899, 12, 30) + timedelta(days=int(v))
    if isinstance(v, str) and len(v) >= 10 and v[2] == "." and v[5] == ".":
        try:
            return date(int(v[6:10]), int(v[3:5]), int(v[0:2]))
        except ValueError:
            return None
    return None


def _to_num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _rows() -> list:
    """Колонки D..P всех строк (без заголовка), сырые значения."""
    return _worksheet().get("D2:P", value_render_option="UNFORMATTED_VALUE")


def _last_dates_by_account() -> dict:
    last = {}
    for r in _rows():
        if len(r) < 13:
            continue
        d, account = _to_date(r[10]), r[12]
        if d and account and (account not in last or d > last[account]):
            last[account] = d
    return last


def _append(rows: list):
    _worksheet().append_rows(rows, value_input_option="USER_ENTERED", table_range="A1")


def _month_totals(year: int, month: int) -> dict:
    res = {"income": 0.0, "expense": 0.0, "repay": 0.0, "by_cat": {}}
    for r in _rows():
        if len(r) < 11:
            continue
        d = _to_date(r[10])
        if not d or d.year != year or d.month != month:
            continue
        kind, cat, amount = r[0], r[1], _to_num(r[7])
        if kind == "Доход":
            res["income"] += amount
        elif kind == "Расход":
            res["expense"] += amount
            res["by_cat"][cat] = res["by_cat"].get(cat, 0.0) + amount
        elif kind == "Погашение":
            res["repay"] += amount
    return res


async def last_dates_by_account() -> dict:
    return await asyncio.to_thread(_last_dates_by_account)


async def append_rows(rows: list):
    if rows:
        await asyncio.to_thread(_append, rows)


async def month_totals(year: int, month: int) -> dict:
    return await asyncio.to_thread(_month_totals, year, month)
