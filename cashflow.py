"""
Кешфлоу:
  • обязательные ежемесячные расходы — разовый опрос и кнопка «➕ Добавить обязательный расход»;
  • «💰 Пришла зарплата» — нерегулярный доход, сумма каждый раз своя;
  • проверка раз в 4 дня: сколько денег на руках → вывод, успеваю ли до следующих платежей.
Роутер подключается раньше основного, чтобы кнопки меню работали из любого состояния.
"""
import html
import logging
import re
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from calculator import calc_min_payment
from forecast import analyze, mandatory_between
from keyboards import main_menu

# Кнопки главного меню не должны съедаться как ввод, пока бот ждёт сумму
MENU_TEXTS = {b.text for row in main_menu().keyboard for b in row}
NOT_MENU = ~F.text.in_(MENU_TEXTS)
from states import Checkin, Obligations, Salary

logger = logging.getLogger(__name__)
router = Router()
db = None

CHECKIN_EVERY_DAYS = 4
MONTHS_GEN = ["", "янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def bind_db(database):
    global db
    db = database


def _rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def _d(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month]}"


def _parse_amount(text: str) -> float | None:
    try:
        v = float(text.replace(" ", "").replace("\xa0", "").replace("₽", "").replace(",", "."))
        return v if v >= 0 else None
    except ValueError:
        return None


def parse_obligation(text: str) -> tuple[str, float, int | None] | None:
    """«Квартира 40 000 2» → ("Квартира", 40000, 2). День — необязательное последнее число 1–31."""
    m = re.match(r"^\s*(.*?[^\d\s].*?)\s+([\d\s.,]+)$", text.strip())
    if not m:
        return None
    name, tokens = m.group(1).strip(), m.group(2).split()
    day = None
    if len(tokens) > 1 and len(tokens[-1]) <= 2 and 1 <= int(tokens[-1]) <= 31:
        day = int(tokens.pop())
    amount = _parse_amount("".join(tokens))
    if not amount:
        return None
    return name, amount, day


async def _loans(user_id: int) -> list:
    """Активные кредиты как обязательные платежи."""
    return [
        {"name": c["name"], "payment": calc_min_payment(c, c["debt"]), "payment_day": c["payment_day"]}
        for c in await db.get_credits(user_id) if c["debt"] > 0.01
    ]


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cf_cancel")
    ]])


@router.callback_query(F.data == "cf_cancel")
async def cf_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")


# ── Обязательные расходы ──────────────────────────────────────────────────────

OBLIGATIONS_INTRO = (
    "📌 <b>Обязательные расходы</b>\n\n"
    "Что ты платишь каждый месяц в любом случае? Можно сразу списком — по расходу в строке:\n"
    "<code>название сумма день</code>\n\n"
    "Например:\n"
    "<code>Квартира 40000 2</code>\n"
    "<code>Склад 44448 10</code>\n"
    "<code>Связь 1900</code> — без дня, если платишь когда придётся\n\n"
    "Платёж по кредиту не вводи — я беру его из «💳 Кредиты и карты»."
)


def _obligations_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data="obl_done")
    ]])


async def _obligations_text(user_id: int) -> str:
    obligations = await db.get_obligations(user_id)
    loans = await _loans(user_id)
    lines = ["📌 <b>Обязательные расходы в месяц</b>\n"]
    for o in obligations:
        when = f"{o['day']}-го" if o["day"] else "в течение месяца"
        lines.append(f"• {o['name']} — {_rub(o['amount'])}, {when}")
    for l in loans:
        lines.append(f"• 🏦 {l['name']} — {_rub(l['payment'])}, {l['payment_day']}-го")
    if not obligations and not loans:
        lines.append("Пока пусто.")
    total = sum(o["amount"] for o in obligations) + sum(l["payment"] for l in loans)
    lines.append(f"\nИтого: <b>{_rub(total)}</b> в месяц")
    return "\n".join(lines)


def _obligations_kb(obligations: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 {o['name']}", callback_data=f"obl_del:{o['id']}")]
            for o in obligations]
    rows.append([InlineKeyboardButton(text="➕ Добавить обязательный расход", callback_data="obl_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_obligations_setup(message: Message, state: FSMContext):
    await state.set_state(Obligations.add)
    await message.answer(OBLIGATIONS_INTRO, parse_mode="HTML", reply_markup=_obligations_done_kb())


@router.callback_query(F.data == "obl_start")
async def obl_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_obligations_setup(callback.message, state)


@router.callback_query(F.data == "obl_add")
async def obl_add(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Obligations.add)
    await callback.message.answer(
        "Напиши расход: <code>название сумма день</code>\nНапример: <code>Интернет 1000 28</code>",
        parse_mode="HTML", reply_markup=_obligations_done_kb()
    )


@router.message(Obligations.add, NOT_MENU)
async def obl_input(message: Message, state: FSMContext):
    # Можно прислать сразу список — по расходу в строке
    added, failed = [], []
    for line in (message.text or "").splitlines():
        if not line.strip():
            continue
        parsed = parse_obligation(line)
        if not parsed:
            failed.append(line.strip())
            continue
        name, amount, day = parsed
        await db.add_obligation(message.from_user.id, name, amount, day)
        added.append(f"✅ {html.escape(name)} — {_rub(amount)}" + (f", {day}-го числа" if day else ""))

    lines = added[:]
    if failed:
        lines.append("\nНе понял 🤔 " + ", ".join(f"«{html.escape(f)}»" for f in failed))
        lines.append("Формат: <code>название сумма день</code>, например <code>Квартира 40000 2</code>")
    lines.append("\nЕщё? Пиши или жми «Готово».")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_obligations_done_kb())


@router.callback_query(F.data == "obl_done")
async def obl_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    first_time = user.get("obligations_asked") != 1
    await db.set_user_fields(callback.from_user.id, obligations_asked=1)
    obligations = await db.get_obligations(callback.from_user.id)
    await callback.message.answer(
        await _obligations_text(callback.from_user.id),
        parse_mode="HTML", reply_markup=_obligations_kb(obligations)
    )
    if first_time and not await db.get_balance_checks(callback.from_user.id, limit=1):
        await _ask_balance(callback.message, state)


@router.callback_query(F.data == "obl_list")
async def obl_list(callback: CallbackQuery):
    await callback.answer()
    obligations = await db.get_obligations(callback.from_user.id)
    await callback.message.answer(
        await _obligations_text(callback.from_user.id),
        parse_mode="HTML", reply_markup=_obligations_kb(obligations)
    )


@router.callback_query(F.data.startswith("obl_del:"))
async def obl_delete(callback: CallbackQuery):
    await db.delete_obligation(callback.from_user.id, int(callback.data.split(":")[1]))
    await callback.answer("Удалено")
    obligations = await db.get_obligations(callback.from_user.id)
    await callback.message.edit_text(
        await _obligations_text(callback.from_user.id),
        parse_mode="HTML", reply_markup=_obligations_kb(obligations)
    )


# ── Зарплата ──────────────────────────────────────────────────────────────────

@router.message(F.text == "💰 Пришла зарплата")
async def salary_start(message: Message, state: FSMContext):
    await state.set_state(Salary.amount)
    await message.answer("💰 Сколько пришло? (₽)", reply_markup=_cancel_kb())


@router.message(Salary.amount, NOT_MENU)
async def salary_amount(message: Message, state: FSMContext):
    amount = _parse_amount(message.text or "")
    if not amount:
        await message.answer("Введи сумму, например: <code>85000</code>", parse_mode="HTML")
        return
    await state.clear()
    await db.add_income(message.from_user.id, amount)
    await message.answer(
        f"✅ Записал зарплату: <b>+{_rub(amount)}</b>",
        parse_mode="HTML", reply_markup=main_menu()
    )
    await message.answer(
        "Сверим остаток, чтобы я пересчитал, на сколько хватит денег?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Сверить остаток", callback_data="checkin")
        ]])
    )


# ── Проверка остатка раз в 4 дня ──────────────────────────────────────────────

async def _ask_balance(message: Message, state: FSMContext):
    await state.set_state(Checkin.balance)
    await message.answer(
        "💳 Сколько сейчас денег на руках? Сложи все карты и наличные (₽):",
        reply_markup=_cancel_kb()
    )


@router.callback_query(F.data == "checkin")
async def checkin_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _ask_balance(callback.message, state)


@router.message(Checkin.balance, NOT_MENU)
async def checkin_balance(message: Message, state: FSMContext):
    balance = _parse_amount(message.text or "")
    if balance is None:
        await message.answer("Введи сумму, например: <code>52000</code>", parse_mode="HTML")
        return
    await state.clear()
    user_id = message.from_user.id
    await db.add_balance_check(user_id, balance)
    await db.set_user_fields(user_id, checkin_asked_at=None)
    await message.answer(await verdict_text(user_id), parse_mode="HTML", reply_markup=main_menu())


async def verdict_text(user_id: int) -> str:
    checks = await db.get_balance_checks(user_id)
    obligations = await db.get_obligations(user_id)
    loans = await _loans(user_id)
    oldest = checks[-1]["date"] if checks else datetime.now().isoformat()
    incomes = await db.get_incomes(user_id, since=oldest)
    check_day = datetime.fromisoformat(checks[0]["date"]).date()
    r = analyze(checks, incomes, obligations, loans, check_day)

    pace, allowed = r["pace"], max(r["allowed"], 0.0)
    lines = []
    if r["level"] == "red_mandatory":
        lines.append(
            f"🔴 <b>Денег не хватает даже на обязательные платежи.</b>\n"
            f"На 30 дней нужно {_rub(r['mandatory_next'])}, на руках {_rub(r['balance'])} — "
            f"не хватает <b>{_rub(-r['free'])}</b>."
        )
        if r["run_out"]:
            what = f" — на «{r['run_out_payment'][0]}»" if r["run_out_payment"] else ""
            lines.append(f"Деньги кончатся <b>{_d(r['run_out'])}</b>{what}. Нужен доход до этой даты.")
    elif r["level"] == "first":
        lines.append(
            f"📝 Записал: на руках <b>{_rub(r['balance'])}</b>.\n"
            f"Чтобы продержаться 30 дней, можно тратить до <b>{_rub(allowed)}/день</b>.\n"
            f"Темп трат посчитаю на следующей проверке — через {CHECKIN_EVERY_DAYS} дня."
        )
    elif r["level"] == "red":
        what = f" — не хватит на «{r['run_out_payment'][0]}»" if r["run_out_payment"] else ""
        lines.append(
            f"🔴 <b>Тратишь быстрее, чем кончаются деньги.</b>\n"
            f"Темп ~<b>{_rub(pace)}/день</b>: деньги закончатся <b>{_d(r['run_out'])}</b>{what}.\n"
            f"Чтобы продержаться 30 дней — не больше <b>{_rub(allowed)}/день</b>."
        )
    elif r["level"] == "yellow":
        lines.append(
            f"🟡 <b>На грани.</b>\n"
            f"Тратишь ~<b>{_rub(pace)}/день</b> при допустимых {_rub(allowed)}/день."
        )
    else:
        lines.append(
            f"🟢 <b>Пока норм.</b>\n"
            f"Тратишь ~<b>{_rub(max(pace, 0))}/день</b>, можно до {_rub(allowed)}/день."
        )

    if r["level"] in ("yellow", "green"):
        lines.append(
            f"⏳ Денег хватит до <b>{_d(r['run_out'])}</b>" if r["run_out"]
            else "⏳ Денег хватит больше чем на полгода"
        )
    if r["pace_avg"] is not None and pace is not None:
        lines.append(f"📊 В среднем за последние недели: {_rub(max(r['pace_avg'], 0))}/день")
    if r["unexplained_growth"]:
        lines.append("❓ Денег стало больше, чем пришло зарплаты. Если был другой доход — внеси его через «💰 Пришла зарплата».")
    if r["income_period"]:
        lines.append(f"💰 Зарплата за период: +{_rub(r['income_period'])}")

    lines.append(
        f"\n💳 На руках {_rub(r['balance'])}, из них на обязательные 30 дней — {_rub(r['mandatory_next'])}"
    )
    if r["upcoming"]:
        lines.append("📅 Ближайшие платежи:")
        lines += [f"   {_d(d)} — {name}, {_rub(a)}" for d, name, a in r["upcoming"][:4]]
    elif not obligations and not loans:
        lines.append("📌 Обязательных расходов нет — добавь их в «💸 Кешфлоу», иначе прогноз будет слишком оптимистичным.")
    return "\n".join(lines)


async def send_checkin_reminders(bot: Bot):
    """Раз в день: если остаток не сверяли 4+ дня — просим. Повторяем не чаще раза в 2 дня."""
    now = datetime.now()
    for u in await db.get_all_users():
        checks = await db.get_balance_checks(u["user_id"], limit=1)
        if u.get("obligations_asked") != 1 and not checks:
            continue
        if checks and now - datetime.fromisoformat(checks[0]["date"]) < timedelta(days=CHECKIN_EVERY_DAYS):
            continue
        asked = u.get("checkin_asked_at")
        if asked and now - datetime.fromisoformat(asked) < timedelta(days=2):
            continue
        try:
            await bot.send_message(
                u["user_id"],
                "🔄 <b>Проверка кешфлоу</b>\n\nСколько сейчас денег на руках? "
                "Скажу, успеваешь ли до следующих платежей.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="💳 Ввести остаток", callback_data="checkin")
                ]])
            )
            await db.set_user_fields(u["user_id"], checkin_asked_at=now.isoformat(timespec="minutes"))
        except Exception as e:
            logger.error(f"Checkin reminder error user={u['user_id']}: {e}")


async def send_obligations_onboarding(bot: Bot):
    """Один раз: спрашиваем про обязательные расходы у тех, у кого есть кредиты в боте."""
    for u in await db.get_all_users():
        if u.get("obligations_asked"):
            continue
        if not await db.get_credits(u["user_id"], active_only=False):
            continue
        try:
            await bot.send_message(
                u["user_id"],
                "👋 Бот теперь следит за кешфлоу: обязательные расходы, зарплата и раз в "
                f"{CHECKIN_EVERY_DAYS} дня — успеваешь ли до следующих платежей.\n\n"
                "Для начала расскажи, какие у тебя обязательные расходы каждый месяц.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📌 Заполнить", callback_data="obl_start")
                ]])
            )
            await db.set_user_fields(u["user_id"], obligations_asked=2)   # спросили, но ещё не заполнил
        except Exception as e:
            logger.error(f"Obligations onboarding error user={u['user_id']}: {e}")


# ── Экран «Кешфлоу» ───────────────────────────────────────────────────────────

def _cashflow_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сверить остаток", callback_data="checkin")],
        [InlineKeyboardButton(text="📌 Обязательные расходы", callback_data="obl_list")],
        [InlineKeyboardButton(text="➕ Добавить обязательный расход", callback_data="obl_add")],
    ])


@router.message(F.text == "💸 Кешфлоу")
async def cashflow_screen(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    if user and user.get("obligations_asked") != 1:
        await start_obligations_setup(message, state)
        return

    checks = await db.get_balance_checks(user_id)
    if not checks:
        await message.answer("Ещё не было ни одной проверки остатка.", reply_markup=_cashflow_kb())
        return

    last = checks[0]
    last_dt = datetime.fromisoformat(last["date"])
    days_ago = (datetime.now() - last_dt).days
    lines = [f"💸 <b>Кешфлоу</b> — последняя проверка {_d(last_dt.date())}"
             + (f" ({days_ago} дн. назад)" if days_ago else " (сегодня)"), ""]
    lines.append(await verdict_text(user_id))

    if days_ago >= 1:
        # Оценка остатка на сегодня: зарплата и обязательные платежи с момента проверки + темп трат
        incomes = await db.get_incomes(user_id, since=last["date"])
        obligations = await db.get_obligations(user_id)
        loans = await _loans(user_id)
        r = analyze(checks, incomes, obligations, loans, last_dt.date())
        estimate = (last["balance"] + sum(i["amount"] for i in incomes)
                    - mandatory_between(last_dt.date(), date.today(), obligations, loans)
                    - max(r["pace"] or 0, 0) * days_ago)
        lines.append(f"\n≈ Сейчас на руках примерно <b>{_rub(estimate)}</b> — сверь остаток для точного прогноза.")

    month_start = date.today().replace(day=1).isoformat()
    month_income = sum(i["amount"] for i in await db.get_incomes(user_id, since=month_start))
    if month_income:
        lines.append(f"💰 Зарплата в этом месяце: +{_rub(month_income)}")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=_cashflow_kb())
