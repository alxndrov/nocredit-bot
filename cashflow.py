"""
Выписки и кешфлоу:
  • приём PDF-выписок Т-Банка и ОТП → операции в БД, пересчёт долга, запись в таблицу cashflow;
  • экран «💸 Кешфлоу»: месяц по таблице, что ушло в кредитки, проценты, лимиты, запас.
Роутер подключается раньше основного, чтобы файлы не перехватывали FSM-хендлеры.
"""
import asyncio
import io
import logging
from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config  # noqa: F401  — загружает .env до чтения настроек таблицы
import sheets
from calculator import _next_payment_date, calc_min_payment
from statements import BANK_TITLES, SHEET_ACCOUNTS, Txn, parse_statement, sheet_rows

logger = logging.getLogger(__name__)
router = Router()
db = None

# Разобранные выписки, для которых пользователь ещё не выбрал карту
_pending: dict[int, object] = {}

MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
REFI_RATE = 0.20   # ориентир ставки рефинансирования, годовых


def bind_db(database):
    global db
    db = database


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _month_bounds(d: date) -> tuple[str, str]:
    start = d.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _card_stats(txns: list) -> dict:
    s = {"purchase": 0.0, "transfer": 0.0, "fee": 0.0, "interest": 0.0, "topup": 0.0}
    for t in txns:
        s[t["kind"]] += abs(t["amount"])
    s["taken"] = s["purchase"] + s["transfer"] + s["fee"]
    s["net"] = s["taken"] + s["interest"] - s["topup"]   # прирост долга
    return s


# ── Приём выписок ─────────────────────────────────────────────────────────────

@router.message(F.document)
async def statement_received(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".pdf") and doc.mime_type != "application/pdf":
        await message.answer("Я понимаю только PDF-выписки Т-Банка и ОТП.")
        return
    await state.clear()
    buf = io.BytesIO()
    await bot.download(doc, destination=buf)
    try:
        st = await asyncio.to_thread(parse_statement, buf.getvalue())
    except Exception as e:
        logger.exception("Ошибка разбора выписки")
        await message.answer(f"Не смог прочитать PDF: {e}")
        return
    if st is None or not st.txns:
        await message.answer(
            "Не узнал выписку 🤔\n\nПока умею:\n"
            "• Т-Банк — «Справка о движении средств» по кредитке\n"
            "• ОТП — выписка по счёту кредитной карты"
        )
        return

    credit = await db.get_credit_by_bank(message.from_user.id, st.bank)
    if credit:
        await _import(message, credit, st)
        return

    cards = [c for c in await db.get_credits(message.from_user.id) if not c.get("bank")]
    if not cards:
        await message.answer(
            f"Выписка {BANK_TITLES[st.bank]}, но в боте нет карты, к которой её привязать. "
            "Сначала добавь карту в «💳 Кредиты и карты»."
        )
        return
    _pending[message.from_user.id] = st
    await message.answer(
        f"📄 Выписка <b>{BANK_TITLES[st.bank]}</b>. К какой карте в боте она относится?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 {c['name']}", callback_data=f"stbind:{c['id']}")]
            for c in cards
        ]),
    )


@router.callback_query(F.data.startswith("stbind:"))
async def statement_bind(callback: CallbackQuery):
    st = _pending.pop(callback.from_user.id, None)
    if st is None:
        await callback.answer("Пришли выписку ещё раз", show_alert=True)
        return
    credit_id = int(callback.data.split(":")[1])
    await db.update_credit_fields(credit_id, bank=st.bank)
    credit = await db.get_credit(credit_id)
    await callback.message.edit_text(f"🔗 {BANK_TITLES[st.bank]} → <b>{credit['name']}</b>", parse_mode="HTML")
    await _import(callback.message, credit, st)


async def _import(message: Message, credit: dict, st):
    old_debt = credit["debt"]
    new = await db.add_bank_txns(credit["user_id"], credit["id"], st.bank, st.txns)

    # Т-Банк пишет доступный остаток: при известном лимите это точный долг на дату
    limit = credit.get("credit_limit")
    if st.available is not None and limit:
        await db.set_anchor(credit["id"], limit - st.available, st.as_of)
    debt = await db.recompute_debt(credit["id"])
    if st.available is not None and not limit and debt is not None:
        limit = round(debt + st.available, 2)
        await db.update_credit_fields(credit["id"], credit_limit=limit)

    lines = [
        f"📄 <b>{BANK_TITLES[st.bank]} → {credit['name']}</b>",
        f"Период: {st.period or '—'}",
        f"Операций в выписке: {len(st.txns)}, новых: <b>{len(new)}</b>",
        "",
    ]
    if debt is None:
        lines.append(
            "⚠️ Не знаю точный долг на дату, от которой считать. "
            "Нажми «✏️ Обновить остатки» и введи «Весь долг» из приложения банка — "
            "дальше буду считать сам по выпискам."
        )
    else:
        diff = debt - old_debt
        sign = "+" if diff > 0 else ""
        lines.append(f"💰 Долг: <b>{_rub(debt)}</b>  (было в боте {_rub(old_debt)}, {sign}{_rub(diff)})")
        if limit:
            lines.append(f"📏 Лимит {_rub(limit)}, свободно ≈ {_rub(max(0.0, limit - debt))}")

    if new:
        recent = [t for t in new if t.date >= (date.today() - timedelta(days=30)).isoformat()]
        if recent:
            s = _card_stats([{"kind": t.kind, "amount": t.amount} for t in recent])
            lines += [
                "",
                "<b>За последние 30 дней:</b>",
                f"  🛒 покупки: {_rub(s['purchase'])}",
                f"  💸 вывод переводом: {_rub(s['transfer'])}" if s["transfer"] else None,
                f"  🔥 проценты: {_rub(s['interest'])}",
                f"  ✅ внесено: {_rub(s['topup'])}",
                f"  📈 долг {'вырос' if s['net'] > 0 else 'уменьшился'} на {_rub(abs(s['net']))}",
            ]
    lines += ["", await _sync_sheet(credit["id"], st.bank)]
    await message.answer("\n".join(l for l in lines if l is not None), parse_mode="HTML")


async def _sync_sheet(credit_id: int, bank: str) -> str:
    """Дописывает в cashflow операции, которых там ещё нет (новее последней даты по этому счёту)."""
    pending = await db.get_pending_sheet_txns(credit_id)
    if not pending:
        return "📊 Таблица: новых строк нет"
    if not sheets.is_configured():
        return f"📊 Таблица не подключена — {len(pending)} операций ждут записи"
    try:
        last = (await sheets.last_dates_by_account()).get(SHEET_ACCOUNTS[bank])
        cutoff = last.isoformat() if last else ""
        skip = [t["id"] for t in pending if t["op_date"] <= cutoff]
        write = [t for t in pending if t["op_date"] > cutoff]
        rows = sheet_rows(bank, [
            Txn(t["op_date"], t["op_time"], t["amount"], t["kind"], t["description"]) for t in write
        ])
        await sheets.append_rows(rows)
        await db.set_sheet_status([t["id"] for t in write], "synced")
        await db.set_sheet_status(skip, "skipped")
        return f"📊 Таблица: добавлено строк — <b>{len(rows)}</b>" + (
            f" (с {last.strftime('%d.%m')} — то, что уже было, не трогал)" if last else "")
    except Exception as e:
        logger.exception("Ошибка записи в таблицу")
        return f"📊 Не смог записать в таблицу: {e}"


# ── Кешфлоу ───────────────────────────────────────────────────────────────────

@router.message(F.text == "💸 Кешфлоу")
async def cashflow_screen(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(await build_cashflow_text(message.from_user.id), parse_mode="HTML")


async def build_cashflow_text(user_id: int) -> str:
    today = date.today()
    m_from, m_to = _month_bounds(today)
    lines = [f"💸 <b>Кешфлоу — {MONTHS_NOM[today.month]} {today.year}</b>", ""]

    # Деньги по таблице
    if sheets.is_configured():
        try:
            t = await sheets.month_totals(today.year, today.month)
            lines += [
                "<b>По таблице:</b>",
                f"  ⬆️ доходы: {_rub(t['income'])}",
                f"  ⬇️ расходы: {_rub(t['expense'])}",
                f"  {'🟢' if t['income'] >= t['expense'] else '🔴'} сальдо: {_rub(t['income'] - t['expense'])}",
            ]
            top = sorted(t["by_cat"].items(), key=lambda x: -x[1])[:4]
            if top:
                lines.append("  крупнее всего: " + ", ".join(f"{c} {_rub(v)}" for c, v in top))
            lines.append("")
        except Exception as e:
            logger.exception("Ошибка чтения таблицы")
            lines += [f"📊 Таблица недоступна: {e}", ""]

    cards = [c for c in await db.get_credits(user_id) if c.get("bank")]
    if not cards:
        lines.append("Пришли PDF-выписки Т-Банка и ОТП — покажу, что происходит с кредитками.")
        return "\n".join(lines)

    month_txns = await db.get_txns(user_id, m_from, m_to)
    q_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for _ in range(2):
        q_start = (q_start - timedelta(days=1)).replace(day=1)
    q_txns = await db.get_txns(user_id, q_start.isoformat(), (today.replace(day=1) - timedelta(days=1)).isoformat())

    last_txn = max((t["op_date"] for t in month_txns), default=None)
    lines.append("<b>Кредитки в этом месяце</b>" + (
        f" <i>(выписки по {last_txn[8:10]}.{last_txn[5:7]})</i>" if last_txn else " <i>(нет выписок)</i>"))
    total_net = total_interest_avg = total_debt = total_free = q_net = 0.0
    for c in cards:
        s = _card_stats([t for t in month_txns if t["credit_id"] == c["id"]])
        q = _card_stats([t for t in q_txns if t["credit_id"] == c["id"]])
        interest_avg = q["interest"] / 3
        total_net += s["net"]
        q_net += q["net"]
        total_interest_avg += interest_avg
        total_debt += c["debt"]
        limit = c.get("credit_limit")
        free = max(0.0, limit - c["debt"]) if limit else 0.0
        total_free += free
        rate = interest_avg * 12 / c["debt"] * 100 if c["debt"] > 0 else 0
        lines += [
            f"💳 <b>{c['name']}</b> — долг {_rub(c['debt'])}" + (f" из {_rub(limit)}" if limit else ""),
            f"   взято {_rub(s['taken'])}, внесено {_rub(s['topup'])}, проценты {_rub(s['interest'])}",
            f"   долг за месяц: {'+' if s['net'] >= 0 else '−'}{_rub(abs(s['net']))}",
            f"   проценты в среднем {_rub(interest_avg)}/мес (≈ {rate:.0f}% годовых)" if interest_avg else None,
            f"   📅 платёж {_next_payment_date(c['payment_day'])}, минималка ≈ {_rub(calc_min_payment(c, c['debt']))}",
        ]

    lines += [
        "",
        f"📈 <b>Кредитки за месяц: {'+' if total_net >= 0 else '−'}{_rub(abs(total_net))}</b> долга",
        f"🔥 Проценты: ≈ {_rub(total_interest_avg)}/мес — {_rub(total_interest_avg * 12)} в год",
    ]
    avg_growth = q_net / 3
    if total_free and avg_growth > 0:
        lines.append(
            f"⏳ Свободного лимита {_rub(total_free)}. При темпе последних 3 мес "
            f"(+{_rub(avg_growth)}/мес) его хватит примерно на <b>{total_free / avg_growth:.1f} мес.</b>"
        )
    if total_debt > 0 and total_interest_avg > 0:
        refi = total_debt * REFI_RATE / 12
        lines.append(
            f"🔄 Рефинанс всего долга под {REFI_RATE:.0%}: проценты ≈ {_rub(refi)}/мес вместо "
            f"{_rub(total_interest_avg)} — экономия {_rub(total_interest_avg - refi)}/мес"
        )
    return "\n".join(l for l in lines if l is not None)
