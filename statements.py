"""
Разбор PDF-выписок по кредиткам: Т-Банк («Справка о движении средств»)
и ОТП (выписка по счёту карты).

Каждая операция приводится к виду:
    date (YYYY-MM-DD), time ("HH:MM" или ""), amount (+ на карту / − с карты),
    kind, description, uid
kind:
    topup     — пополнение карты (погашение долга)
    interest  — проценты (Т-Банк: начисление, ОТП: погашение процентов)
    fee       — комиссии
    transfer  — вывод денег с кредитки переводом (по сути взял в долг наличными)
    purchase  — покупки и оплаты
"""
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field

from pypdf import PdfReader

BANK_TITLES = {"tbank": "Т-Банк", "otp": "ОТП"}
# Значение колонки «Счёт» в таблице cashflow
SHEET_ACCOUNTS = {"tbank": "Т-Банк кредитка", "otp": "ОТП кредитка"}


@dataclass
class Txn:
    date: str
    time: str
    amount: float
    kind: str
    description: str
    uid: str = ""


@dataclass
class Statement:
    bank: str
    txns: list = field(default_factory=list)
    as_of: str | None = None          # дата, на которую указан доступный остаток
    available: float | None = None    # доступный остаток (только Т-Банк)
    period: str = ""


def _iso(d: str) -> str:
    return f"{d[6:10]}-{d[3:5]}-{d[0:2]}"


def _num(s: str) -> float:
    return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))


def _assign_uids(bank: str, txns: list):
    """uid = хэш операции + номер повтора (одинаковые покупки в один день)."""
    seen = Counter()
    for t in txns:
        key = f"{bank}|{t.date}|{t.time}|{t.amount:.2f}|{t.description}"
        seen[key] += 1
        t.uid = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:16]


def pdf_to_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages).replace("\xa0", " ")


def detect_bank(text: str) -> str | None:
    if "ТБАНК" in text and "Справка о движении средств" in text:
        return "tbank"
    if "Погашение процентов" in text or ("За период с:" in text and "Договор:" in text):
        return "otp"
    return None


# ── Т-Банк ────────────────────────────────────────────────────────────────────

_TB_ROW = re.compile(
    r"(\d\d\.\d\d\.\d{4})\n(\d\d:\d\d)\n\d\d\.\d\d\.\d{4}\n\d\d:\d\d\n"
    r"([+-][\d ]+\.\d\d) ₽ [+-][\d ]+\.\d\d ₽ (.*?)"
    r"(?=\n\d\d\.\d\d\.\d{4}\n\d\d:\d\d|\n[\d ]+,\d\d ₽Пополнения|\Z)",
    re.S,
)


def _tb_kind(amount: float, desc: str) -> str:
    if desc.startswith("Проценты"):
        return "interest"
    if amount > 0:
        return "topup"
    if desc.startswith("Плата за"):
        return "fee"
    if desc.startswith(("Внутренний перевод", "Внешний перевод", "Перевод")):
        return "transfer"
    return "purchase"


def parse_tbank(text: str) -> Statement:
    # Убираем подвал страниц, чтобы он не прилипал к описанию последней операции
    text = re.sub(r"АО «ТБанк» универсальная лицензия.*?\n\d+\n", "\n", text, flags=re.S)
    # …и шапку таблицы, которая повторяется на каждой странице
    text = re.sub(r"Дата и время\s+операции\s+Дата\s+списания.*?Номер\s+карты\n", "", text, flags=re.S)
    st = Statement(bank="tbank")
    m = re.search(r"Сумма доступного остатка на (\d\d\.\d\d\.\d{4}): ([\d ]+\.\d\d)", text)
    if m:
        st.as_of, st.available = _iso(m.group(1)), _num(m.group(2))
    m = re.search(r"за период с (\d\d\.\d\d\.\d{4}) по (\d\d\.\d\d\.\d{4})", text)
    if m:
        st.period = f"{m.group(1)}–{m.group(2)}"
    for d, tm, amount, desc in _TB_ROW.findall(text):
        a = _num(amount)
        desc = " ".join(desc.split())
        desc = re.sub(r"\s+(\d{4}|—)$", "", desc)     # хвост: номер карты или «—»
        desc = re.sub(r"^Оплата в\s+", "", desc)
        st.txns.append(Txn(_iso(d), tm, a, _tb_kind(a, desc), desc))
    _assign_uids("tbank", st.txns)
    return st


# ── ОТП ───────────────────────────────────────────────────────────────────────

_OTP_ROW = re.compile(
    r"\n\d+\n(\d\d\.\d\d\.\d{4})\n(.+?)\n(\d+\.\d\d)\n(.*?)"
    r"(?=\n\d+\n\d\d\.\d\d\.\d{4}\n|\n\d+\n\d+\.\d\d\n\d+\.\d\d\n|\Z)",
    re.S,
)


def _otp_kind_desc(op: str, details: str) -> tuple[str, str]:
    details = " ".join(details.split())
    if op.startswith(("Пополнение", "Зачисление")):
        return "topup", op
    if "процент" in op:
        return "interest", "проценты ОТП"
    if "комисси" in op:
        return "fee", "комиссия ОТП"
    if op.startswith("Покупка"):
        return "purchase", op.removeprefix("Покупка").strip()
    m = re.search(r"получатель:\s*(.+?)(?:, ИНН|, тел\.|, БИК|$)", details)
    who = m.group(1).strip() if m else ""
    if op.startswith("Перевод"):
        if "SberQR" in details:
            return "purchase", "SberQR"
        return "transfer", f"перевод {who}".strip()
    return "purchase", who or op


def parse_otp(text: str) -> Statement:
    st = Statement(bank="otp")
    m = re.search(r"За период с: (\d\d\.\d\d\.\d{4}) по: (\d\d\.\d\d\.\d{4})", text)
    if m:
        st.period = f"{m.group(1)}–{m.group(2)}"
    for d, op, amount, details in _OTP_ROW.findall(text):
        kind, desc = _otp_kind_desc(op.strip(), details)
        a = _num(amount)
        st.txns.append(Txn(_iso(d), "", a if kind == "topup" else -a, kind, desc))
    _assign_uids("otp", st.txns)
    return st


def parse_statement(data: bytes) -> Statement | None:
    text = pdf_to_text(data)
    bank = detect_bank(text)
    if bank == "tbank":
        return parse_tbank(text)
    if bank == "otp":
        return parse_otp(text)
    return None


# ── Категории для таблицы cashflow ────────────────────────────────────────────

_CATEGORIES = [
    ("Еда", ["магнит", "magnit", "пятерочка", "пятёрочка", "vkusvill", "вкусвилл", "магнолия", "лавка",
             "vkusnoitochka", "вкусно и точка", "chajkhana", "чайхана", "кофе", "drinkit",
             "папа джонс", "papa", "eda", "dostavka", "шашлыч", "красное", "перекресток", "агроторг"]),
    ("Транспорт: карш, метро, самокат", ["scooters", "whoosh", "вуш", "drive", "драйв", "belka",
             "ситидрайв", "citydrive", "yandex*4121*go", "такси", "метро", "тройка", "metro",
             "азс", "izi", "ампп", "парковка", "каршеринг"]),
    ("Связь и интернет", ["mts", "мтс", "yota", "йота", "t2", "kcell", "билайн", "мегафон"]),
    ("Подписки и сервисы", ["telegram", "vpn", "облако", "сервисы яндекса", "claude",
             "openai", "chatgpt", "apple", "яндекс плюс", "бери заряд"]),
    ("Ремонт", ["леруа", "лемана", "leroy"]),
    ("Здоровье", ["аптека", "apteka", "фармленд", "gorzdrav", "горздрав"]),
    ("Штрафы и налоги", ["gosuslugi", "госуслуги", "штраф"]),
    ("Развлечения", ["qtickets", "локомотив", "кино", "бар", "bar", "ресторан"]),
]


def categorize(description: str) -> str | None:
    d = description.lower()
    for cat, words in _CATEGORIES:
        if any(w in d for w in words):
            return cat
    return None


def sheet_row(bank: str, t: Txn) -> list | None:
    """Строка для cashflow (колонки A..P) или None, если операцию в таблицу не пишем.
    Вывод с кредитки переводом в таблицу не пишем: деньги ушли на свой счёт,
    расход появится там. В долге по карте он учитывается."""
    account = SHEET_ACCOUNTS[bank]
    day = f"{t.date[8:10]}.{t.date[5:7]}.{t.date[0:4]}"
    amount = round(abs(t.amount), 2)
    if t.kind == "transfer":
        return None
    if t.kind == "topup":
        kind, cat, desc = "Погашение", "Кредиты и кред карты", f"погашение {BANK_TITLES[bank]}"
    elif t.kind == "interest":
        kind, cat = "Расход", "Кредиты и кред карты"
        desc = "проценты по кредитке" if bank == "tbank" else "проценты ОТП (из выписки)"
    elif t.kind == "fee":
        kind, cat, desc = "Расход", "Кредиты и кред карты", f"комиссия: {t.description}"
    else:
        cat = categorize(t.description)
        kind, desc = "Расход", t.description
        if cat is None:
            cat, desc = "Другое", f"{t.description} — проверить"
    return ["выписка", "", "", kind, cat, "", "", "", "", "", amount, desc, "", day, "", account]


def sheet_rows(bank: str, txns: list) -> list:
    """Строки для cashflow. Покупки в одном месте за один день схлопываются
    в одну строку с пометкой «(N чеков)», как в ручных записях таблицы."""
    groups: dict = {}
    rows = []
    for t in sorted(txns, key=lambda x: (x.date, x.time)):
        row = sheet_row(bank, t)
        if row is None:
            continue
        if t.kind != "purchase":
            rows.append(row)
            continue
        key = (t.date, row[11])
        if key in groups:
            g = groups[key]
            g[0][10] = round(g[0][10] + row[10], 2)
            g[1] += 1
        else:
            groups[key] = [row, 1]
            rows.append(row)
    for row, n in groups.values():
        if n > 1:
            base = row[11].removesuffix(" — проверить")
            checks = "чека" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "чеков"
            row[11] = f"{base} ({n} {checks})" + (" — проверить" if row[4] == "Другое" else "")
    return rows
