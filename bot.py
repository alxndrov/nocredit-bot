import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database import Database
import cashflow
from scheduler import start_scheduler
from states import AddCredit, AddReminder, ChangeBudget, ChangeMonthBudget, SetPriority, Onboarding, EditCredit, Payment, PaydaySetup, EditPlan, EarlyPayment, CardExpense, SyncBalances
from keyboards import (
    main_menu, credits_list_kb, credit_detail_kb, confirm_pay_kb, debt_check_kb,
    reminders_kb, reminders_menu_kb, general_reminders_kb, confirm_delete_kb,
    credit_type_kb, add_more_credits_kb, strategy_kb, change_strategy_kb,
    rem_days_kb, rem_time_kb, rem_more_kb, statement_check_kb, edit_credit_kb,
    credits_section_kb, settings_kb, month_budget_actions_kb, sync_balance_kb,
    calendar_kb, plan_view_kb, plan_edit_kb, plan_edit_item_kb, payday_has_p2_kb,
    cancel_kb, wow_kb, early_credits_kb, early_type_kb, early_calc_result_kb
)
from datetime import date as date_cls, datetime
from calculator import recalculate_schedule, format_status, calc_interest, calc_min_payment, calc_min_only_schedule, generate_plan, calc_early_payment_savings, calc_credit_early_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
db = Database()


async def _get_month_override(user_id: int) -> float | None:
    """Возвращает переопределённый бюджет на текущий месяц или None."""
    today = date_cls.today()
    return await db.get_budget_override(user_id, today.year, today.month)


# ─── Общий хелпер: расчёт сравнения стратегий ────────────────────────────────

async def build_strategy_text(credits: list, budget: float) -> tuple:
    """
    Возвращает (text, recommended, av_ids, sn_ids).
    recommended = 'avalanche' | 'snowball'
    """
    total_debt = sum(c["debt"] for c in credits)

    mn_schedule = calc_min_only_schedule(credits)
    mn_months   = len(mn_schedule)
    mn_capped   = mn_months >= 120
    mn_interest = sum(sum(cr["interest"] for cr in m["credits"]) for m in mn_schedule)

    av_order    = sorted(credits, key=lambda c: c["rate_month"], reverse=True)
    av_schedule = recalculate_schedule(av_order, budget)
    av_months   = len(av_schedule)
    av_interest = sum(sum(cr["interest"] for cr in m["credits"]) for m in av_schedule)

    sn_order    = sorted(credits, key=lambda c: c["debt"])
    sn_schedule = recalculate_schedule(sn_order, budget)
    sn_months   = len(sn_schedule)
    sn_interest = sum(sum(cr["interest"] for cr in m["credits"]) for m in sn_schedule)

    recommended = "avalanche" if av_interest <= sn_interest else "snowball"

    def fmt_m(m: int, capped: bool = False) -> str:
        if m >= 12:
            y, mo = divmod(m, 12)
            s = f"{m} мес. ({y}г {mo}м)" if mo else f"{m} мес. ({y}г)"
        else:
            s = f"{m} мес."
        return s + ("+" if capped else "")

    mn_pct  = mn_interest / total_debt * 100 if total_debt > 0 else 0
    mn_warn = "  ⚠️ долг не погасить!" if mn_capped else ""

    lines = [
        "🎯 <b>Сравнение стратегий погашения</b>",
        f"💰 Бюджет: <b>{budget:,.0f} ₽/мес</b>  |  Долг: <b>{total_debt:,.0f} ₽</b>",
        "",
        f"📊 <b>Без стратегии</b> — только минималки{mn_warn}",
        f"   ⏱ {fmt_m(mn_months, mn_capped)}",
        f"   📈 Переплата: <b>{mn_interest:,.0f} ₽</b> (+{mn_pct:.0f}% от долга)",
    ]

    for icon, label, s_months, s_interest in [
        ("⛄", "Снежный ком — маленький долг первым", sn_months, sn_interest),
        ("🏔", "Лавина — высокая ставка первым",      av_months, av_interest),
    ]:
        save = mn_interest - s_interest
        faster = mn_months - s_months
        faster_str = f"  ↓ на {faster} мес. быстрее" if faster > 0 and not mn_capped else ""
        lines += [
            "",
            f"{icon} <b>{label}</b>",
            f"   ⏱ {fmt_m(s_months)}{faster_str}",
            f"   📈 Переплата: <b>{s_interest:,.0f} ₽</b>",
            f"   💚 Экономия vs минималок: <b>{save:,.0f} ₽</b>",
        ]

    if av_interest == sn_interest:
        lines += ["", "💡 Обе стратегии дают одинаковый результат — выбирай любую"]
    elif recommended == "avalanche":
        extra = sn_interest - av_interest
        lines += ["", f"⭐ <b>Рекомендуем Лавину</b> — сэкономишь ещё <b>{extra:,.0f} ₽</b> vs Снежного кома"]
    else:
        extra = av_interest - sn_interest
        lines += ["", f"⭐ <b>Рекомендуем Снежный ком</b> — сэкономишь ещё <b>{extra:,.0f} ₽</b> vs Лавины"]

    return "\n".join(lines), recommended, [c["id"] for c in av_order], [c["id"] for c in sn_order]


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await db.init_user(message.from_user.id)
    credits = await db.get_credits(message.from_user.id)
    if not credits:
        await message.answer(
            "👋 Привет! Я помогу отслеживать погашение кредитов и составлю план погашения.\n\n"
            "Для начала добавим все кредиты, карты и рассрочки.\n\n"
            "📝 <b>Кредит 1</b> — введи название:\n"
            "Например: <code>Тинькофф</code>, <code>ОТП рассрочка</code>, <code>Сбер ипотека</code>",
            parse_mode="HTML"
        )
        await state.update_data(credits_added=[])
        await state.set_state(Onboarding.cr_name)
    else:
        await message.answer("👋 С возвращением! Выбери действие:", reply_markup=main_menu())


# ─── Статус ────────────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Статус")
async def show_status(message: Message):
    user = await db.get_user(message.from_user.id)
    credits = await db.get_credits(message.from_user.id, active_only=False)
    if not credits:
        await message.answer("Кредитов нет. Добавь через 💳 Кредиты.", reply_markup=main_menu())
        return
    total_paid = sum(c.get("total_paid", 0) for c in credits)
    active = [c for c in credits if c["is_active"]]
    override = await _get_month_override(message.from_user.id)
    text = format_status(active, user["monthly_budget"], total_paid, first_month_budget=override)
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


# ─── Календарь (из главного меню) ────────────────────────────────────────────

@router.message(F.text == "📅 Календарь")
async def calendar_menu(message: Message):
    await message.answer("📅 <b>Календарь</b>", parse_mode="HTML", reply_markup=calendar_kb())


@router.callback_query(F.data == "open_calendar")
async def open_calendar_cb(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("📅 <b>Календарь</b>", parse_mode="HTML",
                                     reply_markup=calendar_kb())


# ─── История (из главного меню) ───────────────────────────────────────────────

@router.message(F.text == "📋 История")
async def history_from_menu(message: Message):
    user_id = message.from_user.id
    payments = await db.get_history(user_id, limit=8)
    if not payments:
        await message.answer("История платежей пуста.", reply_markup=main_menu())
        return
    lines = ["📋 <b>Последние платежи</b>\n"]
    for p in payments:
        lines.append(
            f"<b>{p['credit_name']}</b>  {p['date']}\n"
            f"  💸 {p['amount']:,.0f} ₽  (% {p['interest']:,.0f} ₽)  → остаток {p['debt_after']:,.0f} ₽"
        )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔍 По кредиту", callback_data="show_history")
    ]])
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)


# ─── Кредиты и карты ──────────────────────────────────────────────────────────

@router.message(F.text == "💳 Кредиты и карты")
async def credits_section(message: Message):
    credits = await db.get_credits(message.from_user.id)
    if not credits:
        await message.answer(
            "💳 <b>Кредитов пока нет.</b>\n\nДобавь первый — и бот составит план погашения.",
            parse_mode="HTML",
            reply_markup=credits_section_kb([])
        )
        return
    total_debt = sum(c["debt"] for c in credits)
    await message.answer(
        f"💳 <b>Кредиты и карты</b>\n"
        f"Общий долг: <b>{total_debt:,.0f} ₽</b>",
        parse_mode="HTML",
        reply_markup=credits_section_kb(credits)
    )


@router.callback_query(F.data == "credits_section")
async def credits_section_cb(callback: CallbackQuery):
    credits = await db.get_credits(callback.from_user.id)
    total_debt = sum(c["debt"] for c in credits)
    await callback.message.edit_text(
        f"💳 <b>Кредиты и карты</b>\n"
        f"Общий долг: <b>{total_debt:,.0f} ₽</b>",
        parse_mode="HTML",
        reply_markup=credits_section_kb(credits)
    )


@router.callback_query(F.data == "credits_menu")
async def credits_menu_cb(callback: CallbackQuery):
    """Обратная совместимость — кнопка «Назад» из карточки кредита."""
    credits = await db.get_credits(callback.from_user.id)
    total_debt = sum(c["debt"] for c in credits)
    await callback.message.edit_text(
        f"💳 <b>Кредиты и карты</b>\n"
        f"Общий долг: <b>{total_debt:,.0f} ₽</b>",
        parse_mode="HTML",
        reply_markup=credits_section_kb(credits)
    )


def _build_credit_detail(c: dict) -> str:
    """Строит текст карточки кредита."""
    cr_type = c.get("credit_type", "loan")

    if cr_type == "debt":
        return (
            f"🤝 <b>{c['name']}</b>\n\n"
            f"📉 Сумма долга: <b>{c['debt']:,.0f} ₽</b>\n"
            f"🔢 Приоритет: <b>{c['priority']}</b>\n"
            f"💸 Возвращено: <b>{c.get('total_paid', 0):,.0f} ₽</b>"
        )

    interest = calc_interest(c, c["debt"])
    min_pay  = calc_min_payment(c, c["debt"])
    if c["min_type"] == "fixed":
        min_desc = f"фикс. {c['min_fixed']:,.0f} ₽"
    elif c["min_type"] == "percent":
        min_desc = f"{c['min_multiplier']:.0f}% от долга = {min_pay:,.0f} ₽"
    else:
        min_desc = f"% × {c['min_multiplier']:.1f} = {min_pay:,.0f} ₽"
    stmt_line = ""
    if cr_type == "card" and c.get("statement_day"):
        stmt_line = f"\n📋 День выписки: <b>{c['statement_day']}-е</b>"
    rate_label = (
        f"{c['rate_month']*12*100:.1f}% год. ({c['rate_month']*100:.2f}%/мес)"
        if cr_type == "card"
        else f"{c['rate_month']*100:.2f}%/мес"
    )
    return (
        f"{'💳' if cr_type == 'card' else '🏦'} <b>{c['name']}</b>\n\n"
        f"📉 Долг: <b>{c['debt']:,.0f} ₽</b>\n"
        f"📈 Ставка: <b>{rate_label}</b>\n"
        f"💰 % за этот месяц: <b>{interest:,.0f} ₽</b>\n"
        f"✅ Минималка: <b>{min_desc}</b>\n"
        f"📅 День платежа: <b>{c['payment_day']}-е</b>{stmt_line}\n"
        f"🔢 Приоритет: <b>{c['priority']}</b>\n"
        f"💸 Уплачено всего: <b>{c.get('total_paid', 0):,.0f} ₽</b>"
    )


@router.callback_query(F.data.startswith("credit_detail:"))
async def credit_detail(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    await callback.message.edit_text(
        _build_credit_detail(c), parse_mode="HTML",
        reply_markup=credit_detail_kb(credit_id, c.get("credit_type"))
    )


# ─── Добавить кредит (FSM) ────────────────────────────────────────────────────

@router.callback_query(F.data == "add_credit")
async def add_credit_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "➕ <b>Добавление</b>\n\nЧто добавляем?",
        parse_mode="HTML",
        reply_markup=credit_type_kb()
    )
    await state.set_state(AddCredit.type)


@router.callback_query(AddCredit.type, F.data.startswith("cr_type:"))
async def add_credit_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cr_type = callback.data.split(":")[1]
    await state.update_data(cr_type=cr_type)
    if cr_type == "card":
        label = "💳 Кредитная карта"
        hint  = "Например: <code>Тинькофф</code>, <code>Альфа</code>, <code>Сбер</code>"
    elif cr_type == "debt":
        label = "🤝 Долг"
        hint  = "Например: <code>Вася</code>, <code>Долг маме</code>, <code>Друг с дачи</code>"
    else:
        label = "🏦 Кредит / Рассрочка"
        hint  = "Например: <code>Тинькофф</code>, <code>ОТП</code>, <code>Сбер</code>"
    await callback.message.edit_text(
        f"{label}\n\n"
        f"Шаг 1: Введи название\n{hint}",
        parse_mode="HTML",
        reply_markup=cancel_kb("credits_section")
    )
    await state.set_state(AddCredit.name)


@router.message(AddCredit.name)
async def add_credit_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    data = await state.get_data()
    cr_type = data.get("cr_type")
    if cr_type == "card":
        text = "Шаг 2: Текущий долг на карте (сколько сейчас потрачено)\nПример: <code>50000</code>"
    elif cr_type == "debt":
        text = "Шаг 2: Сколько должен (₽)?\nПример: <code>15000</code>"
    else:
        text = "Шаг 2: Текущий остаток долга по кредиту\nПример: <code>350000</code>"
    await message.answer(text, parse_mode="HTML", reply_markup=cancel_kb("credits_section"))
    await state.set_state(AddCredit.debt)


@router.message(AddCredit.debt)
async def add_credit_debt(message: Message, state: FSMContext):
    try:
        debt = float(message.text.replace(" ", "").replace(",", ""))
        if debt <= 0:
            raise ValueError
        await state.update_data(debt=debt)
        data = await state.get_data()
        cr_type = data.get("cr_type")
        if cr_type == "debt":
            # Долг без процентов — сразу сохраняем
            existing = await db.get_credits(message.from_user.id)
            priority = len(existing) + 1
            await db.add_credit(
                user_id=message.from_user.id,
                name=data["name"],
                debt=debt,
                rate_month=0.0,
                payment_day=1,
                min_type="fixed",
                min_multiplier=1.0,
                min_fixed=0.0,
                priority=priority,
                credit_type="debt"
            )
            await state.clear()
            await message.answer(
                f"✅ <b>Добавлено!</b>\n\n"
                f"🤝 {data['name']}\n"
                f"💰 Сумма: <b>{debt:,.0f} ₽</b>\n\n"
                f"💡 Без процентов — бот будет учитывать этот долг при распределении бюджета.\n"
                f"Настроить напоминание: 💳 Кредиты → {data['name']} → 🔔 Напоминания",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
        elif cr_type == "card":
            await message.answer(
                "Шаг 3: Процентная ставка по карте <b>в год</b> (%)\n"
                "Например: <code>39.9</code> или <code>28</code>\n\n"
                "Смотри в приложении банка → раздел «Тариф» или «Условия по карте»",
                parse_mode="HTML",
                reply_markup=cancel_kb("credits_section")
            )
            await state.set_state(AddCredit.rate)
        else:
            await message.answer(
                "Шаг 3: Ежемесячный платёж по договору (₽)\n"
                "Пример: <code>15000</code>\n\n"
                "Смотри в графике платежей или СМС от банка",
                parse_mode="HTML",
                reply_markup=cancel_kb("credits_section")
            )
            await state.set_state(AddCredit.rate)
    except ValueError:
        await message.answer("Введи число, например: <code>200000</code>", parse_mode="HTML")


@router.message(AddCredit.rate)
async def add_credit_rate(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", ".").replace("%", "").replace(" ", "").strip())
        if val <= 0:
            raise ValueError
        data = await state.get_data()
        if data.get("cr_type") == "card":
            if val > 600:
                raise ValueError
            rate_month = val / 12 / 100
            await state.update_data(rate=rate_month, monthly_payment=None)
            await message.answer(
                f"✅ Ставка: <b>{val:.1f}% годовых</b> → {val/12:.2f}%/мес\n\n"
                "Шаг 4: День выставления выписки или списания минимума (1–31)\n"
                "Например: <code>1</code> или <code>25</code>",
                parse_mode="HTML",
                reply_markup=cancel_kb("credits_section")
            )
        else:
            await state.update_data(rate=0.0, monthly_payment=val)
            await message.answer(
                f"✅ Платёж: <b>{val:,.0f} ₽/мес</b>\n\n"
                "Шаг 4: Ставка по кредиту <b>в год</b> (%)\n"
                "Например: <code>21.9</code>. Беспроцентная рассрочка — <code>0</code>",
                parse_mode="HTML",
                reply_markup=cancel_kb("credits_section")
            )
            await state.set_state(AddCredit.loan_rate)
            return
        await state.set_state(AddCredit.payment_day)
    except ValueError:
        data = await state.get_data()
        if data.get("cr_type") == "card":
            await message.answer("Введи годовую ставку, например: <code>39.9</code>", parse_mode="HTML")
        else:
            await message.answer("Введи сумму платежа, например: <code>15000</code>", parse_mode="HTML")


@router.message(AddCredit.loan_rate)
async def add_credit_loan_rate(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", ".").replace("%", "").replace(" ", "").strip())
        if not 0 <= val <= 600:
            raise ValueError
    except ValueError:
        await message.answer("Введи годовую ставку, например: <code>21.9</code>", parse_mode="HTML")
        return
    await state.update_data(rate=val / 12 / 100)
    await message.answer(
        f"✅ Ставка: <b>{val:.1f}% годовых</b>\n\n"
        "Шаг 5: День списания платежа по договору (1–31)\n"
        "Например: <code>20</code> или <code>25</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb("credits_section")
    )
    await state.set_state(AddCredit.payment_day)


@router.message(AddCredit.payment_day)
async def add_credit_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        await state.update_data(payment_day=day)
        data = await state.get_data()
        if data.get("cr_type") == "card":
            await message.answer(
                "Шаг 5: Минимальный платёж по карте\n\n"
                "📱 Открой приложение банка → зайди в свою карту → «Тариф», «Условия» или «Справка».\n"
                "Найди строку <b>«Минимальный платёж»</b> — она бывает двух видов:\n\n"
                "1️⃣ <b>Процент от долга</b>\n"
                "Пример: <i>«Минимальный платёж — 5% от суммы задолженности»</i>\n"
                "→ сумма каждый месяц меняется вместе с долгом\n\n"
                "2️⃣ <b>Фиксированная сумма</b>\n"
                "Пример: <i>«Минимальный платёж — 1 500 ₽ в месяц»</i>\n"
                "→ сумма всегда одинакова\n\n"
                "❓ <i>Не можешь найти — введи 1 (самый частый вариант у кредитных карт)</i>\n\n"
                "Введи <code>1</code> или <code>2</code>:",
                parse_mode="HTML",
                reply_markup=cancel_kb("credits_section")
            )
            await state.set_state(AddCredit.min_type)
        else:
            # Loans: auto-assign priority and save immediately
            existing = await db.get_credits(message.from_user.id)
            priority = len(existing) + 1
            await db.add_credit(
                user_id=message.from_user.id,
                name=data["name"],
                debt=data["debt"],
                rate_month=data.get("rate", 0.0),
                payment_day=day,
                min_type="fixed",
                min_multiplier=2.0,
                min_fixed=data["monthly_payment"],
                priority=priority,
                credit_type="loan"
            )
            await state.clear()
            await message.answer(
                f"✅ <b>Добавлено!</b>\n\n"
                f"🏦 {data['name']}\n"
                f"💰 Долг: {data['debt']:,.0f} ₽\n"
                f"📆 Платёж: {data['monthly_payment']:,.0f} ₽/мес\n"
                f"📈 Ставка: {data.get('rate', 0.0)*12*100:.1f}% годовых\n"
                f"📅 День списания: {day}-е\n\n"
                f"💡 Приоритет и минималку можно настроить в карточке кредита → ✏️ Редактировать",
                parse_mode="HTML",
                reply_markup=main_menu()
            )
    except ValueError:
        await message.answer("Введи число от 1 до 31")


@router.message(AddCredit.min_type)
async def add_credit_min_type(message: Message, state: FSMContext):
    choice = message.text.strip()
    if choice == "1":
        await state.update_data(min_type="percent")
        await message.answer(
            "📋 Хорошо, нужен процент.\n\n"
            "В тарифе банка найди строку вроде:\n"
            "<i>«Минимальный платёж — <b>5%</b> от суммы задолженности»</i>\n\n"
            "Введи только цифру из этой строки.\n"
            "Например: <code>5</code> или <code>3</code>\n\n"
            "❓ <i>Не нашёл — введи <code>5</code>, это среднее по рынку</i>",
            parse_mode="HTML",
            reply_markup=cancel_kb("credits_section")
        )
    elif choice == "2":
        await state.update_data(min_type="fixed")
        await message.answer(
            "💰 Хорошо, нужна фиксированная сумма.\n\n"
            "Посмотри в приложении банка — в тарифе карты должна быть строка вроде:\n"
            "<i>«Минимальный платёж — <b>1 500 ₽</b>»</i>\n\n"
            "Введи эту сумму в рублях, например: <code>1500</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb("credits_section")
        )
    else:
        await message.answer("Введи <code>1</code> или <code>2</code>", parse_mode="HTML")
        return
    await state.set_state(AddCredit.min_value)


@router.message(AddCredit.min_value)
async def add_credit_min_value(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", ".").replace(" ", ""))
        if val <= 0:
            raise ValueError
        await state.update_data(min_value=val)
        data = await state.get_data()
        min_type = data["min_type"]
        min_fixed = val if min_type == "fixed" else None
        min_multiplier = val if min_type in ("formula", "percent") else 2.0

        existing = await db.get_credits(message.from_user.id)
        priority = len(existing) + 1
        await db.add_credit(
            user_id=message.from_user.id,
            name=data["name"],
            debt=data["debt"],
            rate_month=data["rate"],
            payment_day=data["payment_day"],
            min_type=min_type,
            min_multiplier=min_multiplier,
            min_fixed=min_fixed,
            priority=priority,
            credit_type="card"
        )
        await state.clear()
        min_desc = (
            f"фикс. {min_fixed:,.0f} ₽" if min_type == "fixed"
            else f"{val:.0f}% от долга"
        )
        await message.answer(
            f"✅ <b>Добавлено!</b>\n\n"
            f"💳 {data['name']}\n"
            f"💰 Долг: {data['debt']:,.0f} ₽\n"
            f"📈 Ставка: {data['rate']*12*100:.1f}% годовых\n"
            f"📅 День платежа: {data['payment_day']}-е\n"
            f"✅ Минималка: {min_desc}\n\n"
            f"💡 Приоритет можно настроить в карточке кредита → ✏️ Редактировать",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except ValueError:
        await message.answer("Введи число, например: <code>5</code>", parse_mode="HTML")


# ─── Изменить приоритет ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("set_priority:"))
async def set_priority_start(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    await state.update_data(credit_id=credit_id)
    await callback.message.edit_text(
        f"🔢 Текущий приоритет <b>{c['name']}</b>: <b>{c['priority']}</b>\n\n"
        "Введи новый приоритет (1 = первым гасить):",
        parse_mode="HTML"
    )
    await state.set_state(SetPriority.value)


@router.message(SetPriority.value)
async def set_priority_apply(message: Message, state: FSMContext):
    try:
        priority = int(message.text.strip())
        if priority < 1:
            raise ValueError
        data = await state.get_data()
        await db.update_credit_priority(data["credit_id"], priority)
        await state.clear()
        await message.answer(
            f"✅ Приоритет обновлён: <b>{priority}</b>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except ValueError:
        await message.answer("Введи целое число ≥ 1")


# ─── Удалить кредит ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("del_credit:"))
async def del_credit_confirm(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    await callback.message.edit_text(
        f"🗑 Удалить кредит <b>{c['name']}</b>?\nВсе напоминания тоже удалятся.",
        parse_mode="HTML",
        reply_markup=confirm_delete_kb(credit_id)
    )


@router.callback_query(F.data.startswith("confirm_del:"))
async def del_credit_apply(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    await db.delete_credit(credit_id)
    await callback.message.edit_text(f"✅ Кредит <b>{c['name']}</b> удалён.", parse_mode="HTML")
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


# ─── Напоминания ──────────────────────────────────────────────────────────────

@router.message(F.text == "🔔 Напоминания")
async def reminders_overview(message: Message):
    credits = await db.get_credits(message.from_user.id)
    if not credits:
        await message.answer("Сначала добавь кредит.", reply_markup=main_menu())
        return
    buttons = [[InlineKeyboardButton(
        text=f"💳 {c['name']}",
        callback_data=f"remind_list:{c['id']}"
    )] for c in credits]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("🔔 Выбери кредит для настройки напоминаний:", reply_markup=kb)


@router.callback_query(F.data.startswith("remind_list:"))
async def remind_list(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    reminders = await db.get_reminders(credit_id)

    if reminders:
        lines = [f"🔔 <b>Напоминания для {c['name']}:</b>\n"]
        for r in reminders:
            day_label = "В день платежа" if r["days_before"] == 0 else f"За {r['days_before']} дн. до платежа"
            lines.append(f"• {day_label} в {r['hour']:02d}:{r['minute']:02d}")
        lines.append(f"\n📅 День платежа: <b>{c['payment_day']}-е</b>")
        text = "\n".join(lines)
    else:
        text = f"🔔 <b>{c['name']}</b> — напоминаний нет.\nНажми ➕ чтобы добавить."

    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=reminders_kb(credit_id, reminders)
    )


@router.callback_query(F.data.startswith("add_reminder:"))
async def add_reminder_start(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    await state.update_data(credit_id=credit_id)
    await callback.message.edit_text(
        f"➕ <b>Напоминание для {c['name']}</b>\n\n"
        f"Шаг 1/2: За сколько дней до платежа напомнить?\n\n"
        f"Введи число дней: <code>0</code> = в день платежа, <code>3</code> = за 3 дня\n"
        f"(можно несколько напоминаний — добавляй по одному)",
        parse_mode="HTML",
        reply_markup=cancel_kb(f"remind_list:{credit_id}")
    )
    await state.set_state(AddReminder.days_before)


@router.message(AddReminder.days_before)
async def add_reminder_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 0 or days > 30:
            raise ValueError
        await state.update_data(days_before=days)
        data = await state.get_data()
        await message.answer(
            "Шаг 2/2: В какое время отправить? (по МСК)\n"
            "Формат: <code>ЧЧ:ММ</code>, например <code>10:00</code> или <code>09:30</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb(f"remind_list:{data['credit_id']}")
        )
        await state.set_state(AddReminder.time)
    except ValueError:
        await message.answer("Введи число от 0 до 30")


@router.message(AddReminder.time)
async def add_reminder_time(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError

        data = await state.get_data()
        credit_id = data["credit_id"]
        c = await db.get_credit(credit_id)

        await db.add_reminder(
            credit_id=credit_id,
            user_id=message.from_user.id,
            days_before=data["days_before"],
            hour=hour,
            minute=minute
        )
        await state.clear()

        day_label = "в день платежа" if data["days_before"] == 0 else f"за {data['days_before']} дн. до платежа"
        await message.answer(
            f"✅ Напоминание добавлено!\n\n"
            f"💳 <b>{c['name']}</b>\n"
            f"⏰ {day_label} ({c['payment_day']}-е число) в <b>{hour:02d}:{minute:02d}</b>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except (ValueError, IndexError):
        await message.answer("Введи время в формате <code>ЧЧ:ММ</code>, например <code>10:00</code>", parse_mode="HTML")


@router.callback_query(F.data.startswith("del_reminder:"))
async def del_reminder(callback: CallbackQuery):
    reminder_id = int(callback.data.split(":")[1])
    reminder = await db.get_reminder(reminder_id)
    credit_id = reminder["credit_id"] if reminder else None
    await db.delete_reminder(reminder_id)
    await callback.answer("Напоминание удалено")

    if credit_id:
        reminders = await db.get_reminders(credit_id)
        c = await db.get_credit(credit_id)
        await callback.message.edit_reply_markup(
            reply_markup=reminders_kb(credit_id, reminders)
        )


# ─── Внести платёж ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "back")
async def back_to_main(callback: CallbackQuery):
    await callback.answer()
    await callback.message.delete()


@router.message(F.text == "✅ Внести платёж")
async def pay_menu(message: Message):
    credits = await db.get_credits(message.from_user.id)
    active = [c for c in credits if c["debt"] > 0.01]
    if not active:
        await message.answer("Все долги закрыты! 🎉", reply_markup=main_menu())
        return
    await message.answer(
        "💳 По какому кредиту вносим платёж?",
        reply_markup=credits_list_kb(active, "pay")
    )


@router.callback_query(F.data.startswith("pay:"))
async def pay_credit_detail(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    interest = calc_interest(c, c["debt"])
    min_pay = calc_min_payment(c, c["debt"])

    # Получаем бюджет и считаем рекомендуемый платёж
    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    override = await _get_month_override(callback.from_user.id)
    schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)

    recommended = min_pay
    if schedule:
        month_data = schedule[0]
        for cr in month_data["credits"]:
            if cr["credit_id"] == credit_id:
                recommended = cr["payment"]
                break

    text = (
        f"💳 <b>{c['name']}</b>\n\n"
        f"📉 Долг сейчас: <b>{c['debt']:,.0f} ₽</b>\n"
        f"📈 % за месяц: <b>{interest:,.0f} ₽</b>\n"
        f"✅ Минималка: <b>{min_pay:,.0f} ₽</b>\n"
        f"💡 Рекомендуемый платёж: <b>{recommended:,.0f} ₽</b>\n\n"
        f"Подтвердить платёж {recommended:,.0f} ₽?"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=confirm_pay_kb(credit_id)
    )


async def _record_and_show_debt_check(
    send_fn,          # coroutine-функция для отправки/редактирования сообщения
    user_id: int,
    credit_id: int,
    payment: float,
):
    """Записывает платёж в БД и показывает сообщение с проверкой остатка."""
    c = await db.get_credit(credit_id)
    # Проценты — только за дни с прошлого платежа: при двух платежах в месяц
    # месячные проценты не должны начисляться дважды
    last = await db.get_last_payment_date(credit_id)
    since = (datetime.strptime(last, "%d.%m.%Y") if last
             else datetime.strptime(c["created_at"][:10], "%Y-%m-%d"))
    days = min(max((datetime.now() - since).days, 0), 62)
    interest = calc_interest(c, c["debt"]) * days / 30
    new_debt = max(0.0, c["debt"] + interest - payment)

    await db.update_credit_debt(credit_id, new_debt, payment)
    await db.record_payment(credit_id, user_id, payment, interest, new_debt)

    if new_debt < 0.01:
        await send_fn(
            f"🎉 <b>{c['name']} полностью закрыт!</b>\n\n"
            f"💸 Последний платёж: <b>{payment:,.0f} ₽</b>\n"
            f"Поздравляем — долг погашен! 🥳",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ К кредитам", callback_data="credits_section")
            ]])
        )
        return

    await send_fn(
        f"✅ <b>Платёж записан!</b>\n\n"
        f"💳 <b>{c['name']}</b>\n"
        f"💸 Внесено: <b>{payment:,.0f} ₽</b>  (из них % ≈ {interest:,.0f} ₽)\n\n"
        f"По моим расчётам, сейчас осталось: <b>{new_debt:,.0f} ₽</b>\n\n"
        f"Это верно? Открой приложение банка и проверь.",
        parse_mode="HTML",
        reply_markup=debt_check_kb(credit_id)
    )


@router.callback_query(F.data.startswith("confirm_pay:"))
async def confirm_pay(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return

    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    override = await _get_month_override(callback.from_user.id)
    schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)

    payment = calc_min_payment(c, c["debt"])
    if schedule:
        for cr in schedule[0]["credits"]:
            if cr["credit_id"] == credit_id:
                payment = cr["payment"]
                break

    await _record_and_show_debt_check(
        callback.message.edit_text, callback.from_user.id, credit_id, payment
    )


@router.callback_query(F.data.startswith("pay_custom:"))
async def pay_custom_start(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    await state.update_data(pay_credit_id=credit_id)
    await callback.message.edit_text(
        f"💳 <b>{c['name']}</b>\n"
        f"📉 Текущий долг: <b>{c['debt']:,.0f} ₽</b>\n\n"
        "Введи сумму, которую ты фактически внёс сегодня (₽):\n"
        "Например: <code>12000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"pay:{credit_id}")
        ]])
    )
    await state.set_state(Payment.custom_amount)


@router.message(Payment.custom_amount)
async def pay_custom_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        credit_id = data["pay_credit_id"]
        await state.clear()
        await _record_and_show_debt_check(message.answer, message.from_user.id, credit_id, amount)
    except ValueError:
        await message.answer(
            "Введи сумму в рублях, например: <code>12000</code>",
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("debt_ok:"))
async def debt_ok(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    override = await _get_month_override(callback.from_user.id)
    schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)

    next_payment_line = ""
    if schedule:
        for cr in schedule[0]["credits"]:
            if cr["credit_id"] == credit_id:
                next_payment_line = f"\n📅 Следующий платёж: <b>{cr['payment']:,.0f} ₽</b>"
                break

    await callback.message.edit_text(
        f"👍 Отлично, данные актуальны!\n\n"
        f"💳 <b>{c['name']}</b>\n"
        f"📉 Остаток: <b>{c['debt']:,.0f} ₽</b>{next_payment_line}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К кредитам", callback_data="credits_section")
        ]])
    )


@router.callback_query(F.data.startswith("debt_wrong:"))
async def debt_wrong(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    await state.update_data(debt_credit_id=credit_id)
    await callback.message.edit_text(
        f"💳 <b>{c['name']}</b>\n\n"
        f"Открой приложение банка и посмотри точный остаток долга.\n\n"
        f"По моим расчётам было: <b>{c['debt']:,.0f} ₽</b>\n\n"
        "Введи фактический остаток (₽):\n"
        "Например: <code>41000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Нет, расчёт верный", callback_data=f"debt_ok:{credit_id}")
        ]])
    )
    await state.set_state(Payment.custom_debt)


@router.message(Payment.custom_debt)
async def pay_custom_debt(message: Message, state: FSMContext):
    try:
        debt = float(message.text.replace(" ", "").replace(",", ""))
        if debt < 0:
            raise ValueError
        data = await state.get_data()
        credit_id = data["debt_credit_id"]
        await db.update_credit_fields(credit_id, debt=debt)
        await state.clear()

        c = await db.get_credit(credit_id)
        user = await db.get_user(message.from_user.id)
        credits = await db.get_credits(message.from_user.id)
        override = await _get_month_override(message.from_user.id)
        schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)

        next_payment_line = ""
        if schedule:
            for cr in schedule[0]["credits"]:
                if cr["credit_id"] == credit_id:
                    next_payment_line = f"\n📅 Следующий платёж: <b>{cr['payment']:,.0f} ₽</b>"
                    break

        await message.answer(
            f"✅ <b>Остаток обновлён!</b>\n\n"
            f"💳 <b>{c['name']}</b>\n"
            f"📉 Новый остаток: <b>{debt:,.0f} ₽</b>{next_payment_line}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ К кредитам", callback_data="credits_section")
            ]])
        )
    except ValueError:
        await message.answer(
            "Введи сумму в рублях, например: <code>41000</code>",
            parse_mode="HTML"
        )


# ─── История ──────────────────────────────────────────────────────────────────

def _history_picker_kb(credits: list) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if not c['is_active'] else ('💳' if c.get('credit_type') == 'card' else '🏦')} {c['name']}",
        callback_data=f"history:{c['id']}"
    )] for c in credits]
    buttons.append([InlineKeyboardButton(text="📋 Все платежи", callback_data="history:all")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


MONTHS_PER_PAGE = 12


def _month_to_label(month_num: int) -> str:
    """Возвращает 'апр 2026' для номера месяца от сегодня."""
    from datetime import date as _date
    import calendar as _cal
    today = _date.today()
    total = today.month + month_num - 1
    year  = today.year + (total - 1) // 12
    month = (total - 1) % 12 + 1
    names = ["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"]
    return f"{names[month-1]} {year}"


def _schedule_compact(schedule: list, credits: list, credit_id: int | None) -> str:
    """
    Мобильно-читаемый вид графика: первые 3 месяца + последний.
    credit_id=None → сводная; иначе — только по одному кредиту.
    """
    if not schedule:
        return "Нет данных"

    cmap = {c["id"]: c for c in credits}

    def fmt_month(m: dict, is_last: bool = False) -> str:
        label = _month_to_label(m["month"])
        suffix = " 🏁" if is_last else ""
        lines = [f"📅 <b>{label}</b> (мес. {m['month']}){suffix}"]

        if credit_id is not None:
            # Один кредит
            for cr in m["credits"]:
                if cr["credit_id"] == credit_id:
                    principal = cr["payment"] - cr["interest"]
                    lines.append(f"  💰 Платёж: <b>{cr['payment']:,.0f} ₽</b>")
                    lines.append(f"  📈 % банку: {cr['interest']:,.0f} ₽  |  тело: {principal:,.0f} ₽")
                    debt_str = "0 ₽ 🎉" if cr["debt_after"] < 0.01 else f"{cr['debt_after']:,.0f} ₽"
                    lines.append(f"  📉 Остаток: {debt_str}")
                    break
        else:
            # Сводная
            for cr in m["credits"]:
                c = cmap.get(cr["credit_id"])
                icon = "💳" if c and c.get("credit_type") == "card" else ("🤝" if c and c.get("credit_type") == "debt" else "🏦")
                name = c["name"] if c else "?"
                lines.append(f"  {icon} {name}: <b>{cr['payment']:,.0f} ₽</b> (% {cr['interest']:,.0f})")
            total_pay  = sum(cr["payment"]    for cr in m["credits"])
            total_debt = sum(cr["debt_after"] for cr in m["credits"])
            debt_str   = "0 ₽ 🎉" if total_debt < 0.01 else f"{total_debt:,.0f} ₽"
            lines.append(f"  💸 Итого: <b>{total_pay:,.0f} ₽</b>  →  долг: {debt_str}")

        return "\n".join(lines)

    parts = []
    shown = set()
    for i in range(min(3, len(schedule))):
        parts.append(fmt_month(schedule[i]))
        shown.add(i)

    last_i = len(schedule) - 1
    if last_i not in shown:
        remaining = len(schedule) - 3
        if remaining > 0:
            parts.append(f"<i>    ┊  ещё {remaining} мес...</i>")
        parts.append(fmt_month(schedule[last_i], is_last=True))

    return "\n\n".join(parts)


def _schedule_table(schedule: list, credit_id: int | None, credits: list, page: int) -> tuple[str, int]:
    """
    Строит ASCII-таблицу (постраничная, используется для «Весь график»).
    """
    def fmt(n): return f"{n:,.0f}".replace(",", "\u202f")

    if credit_id is None:
        rows_data = []
        for m in schedule:
            pay  = sum(cr["payment"]    for cr in m["credits"])
            intr = sum(cr["interest"]   for cr in m["credits"])
            debt = sum(cr["debt_after"] for cr in m["credits"])
            rows_data.append((m["month"], pay, intr, pay - intr, debt))
    else:
        rows_data = []
        for m in schedule:
            for cr in m["credits"]:
                if cr["credit_id"] == credit_id:
                    rows_data.append((
                        m["month"],
                        cr["payment"], cr["interest"],
                        cr["payment"] - cr["interest"],
                        cr["debt_after"]
                    ))
                    break

    total_pages = max(1, (len(rows_data) + MONTHS_PER_PAGE - 1) // MONTHS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = rows_data[page * MONTHS_PER_PAGE: (page + 1) * MONTHS_PER_PAGE]

    hdr = f"{'М':>3} │ {'Платёж':>9} │ {'Проц.':>8} │ {'Осн.':>8} │ {'Остаток':>10}"
    sep  = "─" * len(hdr)
    rows = [hdr, sep]
    for mn, pay, intr, prn, debt in chunk:
        rows.append(f"{mn:>3} │ {fmt(pay):>9} │ {fmt(intr):>8} │ {fmt(prn):>8} │ {fmt(debt):>10}")

    return "\n".join(rows), total_pages


@router.callback_query(F.data.startswith("show_schedule:"))
async def show_schedule(callback: CallbackQuery):
    await callback.answer()
    parts     = callback.data.split(":")
    credit_id = int(parts[2]) if len(parts) > 2 else 0

    user_id = callback.from_user.id
    user    = await db.get_user(user_id)
    credits = await db.get_credits(user_id)

    if not credits:
        await callback.message.edit_text(
            "Сначала добавь кредиты.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")
            ]])
        )
        return

    override = await _get_month_override(user_id)
    schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)
    if not schedule:
        await callback.message.edit_text(
            "✅ Долгов нет — нечего показывать!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")
            ]])
        )
        return

    total_interest = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule)
    cid            = credit_id if credit_id != 0 else None

    if credit_id == 0:
        title = "📊 <b>Сводный график</b>"
    else:
        c = next((c for c in credits if c["id"] == credit_id), None)
        title = f"📊 <b>{c['name'] if c else '?'}</b>"

    budget_label = (
        f"🗓 <b>{override:,.0f} ₽</b> (этот мес.)"
        if override is not None
        else f"💰 <b>{user['monthly_budget']:,.0f} ₽/мес</b>"
    )
    compact = _schedule_compact(schedule, credits, cid)
    text = (
        f"{title}\n"
        f"{budget_label}  ·  "
        f"📅 <b>{len(schedule)} мес.</b>  ·  "
        f"📈 переплата <b>{total_interest:,.0f} ₽</b>\n\n"
        f"{compact}"
    )

    # Кнопки
    kb_rows = [[InlineKeyboardButton(
        text="📋 Весь график (таблица)",
        callback_data=f"full_sched:0:{credit_id}"
    )]]

    # Переключатель кредит / сводная
    if credit_id != 0:
        kb_rows.append([InlineKeyboardButton(text="🔀 Сводный", callback_data="show_schedule:0:0")])
    for c in credits:
        if c["id"] != credit_id:
            icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
            kb_rows.append([InlineKeyboardButton(
                text=f"{icon} {c['name']}",
                callback_data=f"show_schedule:0:{c['id']}"
            )])
    kb_rows.append([InlineKeyboardButton(text="◀️ К кредитам", callback_data="credits_section")])

    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data.startswith("full_sched:"))
async def full_sched(callback: CallbackQuery):
    """Постраничная ASCII-таблица графика."""
    await callback.answer()
    parts     = callback.data.split(":")
    page      = int(parts[1])
    credit_id = int(parts[2]) if len(parts) > 2 else 0

    user_id = callback.from_user.id
    user    = await db.get_user(user_id)
    credits = await db.get_credits(user_id)

    override = await _get_month_override(user_id)
    schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=override)
    if not schedule:
        await callback.message.edit_text("✅ Долгов нет!")
        return

    total_interest = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule)
    cid_for_table  = credit_id if credit_id != 0 else None
    table_text, total_pages = _schedule_table(schedule, cid_for_table, credits, page)

    if credit_id == 0:
        title = "📊 <b>Сводный график</b>"
    else:
        c = next((c for c in credits if c["id"] == credit_id), None)
        title = f"📊 <b>{c['name'] if c else '?'}</b>"

    budget_label = (
        f"🗓 {override:,.0f} ₽ (этот мес.)"
        if override is not None
        else f"💰 {user['monthly_budget']:,.0f} ₽/мес"
    )
    text = (
        f"{title}  (стр. {page+1}/{total_pages})\n"
        f"<b>{budget_label}</b>  │  "
        f"📅 <b>{len(schedule)} мес.</b>  │  "
        f"📈 <b>{total_interest:,.0f} ₽</b> переплата\n\n"
        f"<code>{table_text}</code>"
    )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"full_sched:{page-1}:{credit_id}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"full_sched:{page+1}:{credit_id}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(
        text="◀️ Компактный вид", callback_data=f"show_schedule:0:{credit_id}"
    )])
    kb_rows.append([InlineKeyboardButton(text="◀️ К кредитам", callback_data="credits_section")])

    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@router.callback_query(F.data == "noop")
async def noop_cb(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "show_history")
async def show_history_cb(callback: CallbackQuery):
    credits = await db.get_credits(callback.from_user.id, active_only=False)
    if not credits:
        await callback.message.edit_text(
            "📋 Платежей пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")
            ]])
        )
        return
    await callback.message.edit_text(
        "📋 <b>История платежей</b>\n\nПо какому кредиту?",
        parse_mode="HTML",
        reply_markup=_history_picker_kb(credits)
    )


@router.callback_query(F.data.startswith("history:"))
async def show_history(callback: CallbackQuery):
    val = callback.data.split(":")[1]
    if val == "all":
        history = await db.get_history(callback.from_user.id, limit=10)
    else:
        history = await db.get_history(callback.from_user.id, credit_id=int(val), limit=10)

    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="show_history")
    ]])

    if not history:
        await callback.message.edit_text("Платежей пока нет.", reply_markup=back_kb)
        return

    lines = ["📋 <b>История платежей:</b>\n"]
    for h in history:
        if h.get("type") == "expense":
            lines.append(
                f"{h['date']}  <b>{h['credit_name']}</b>\n"
                f"  💸 Расход: <b>+{h['amount']:,.0f} ₽</b>  |  Долг стал: {h['debt_after']:,.0f} ₽"
            )
        else:
            lines.append(
                f"{h['date']}  <b>{h['credit_name']}</b>\n"
                f"  ✅ Внесено: {h['amount']:,.0f} ₽  |  % = {h['interest']:,.0f} ₽  |  Остаток: {h['debt_after']:,.0f} ₽"
            )
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)


# ─── Синхронизация остатков ───────────────────────────────────────────────────

async def _sync_show_next(msg, state: FSMContext, from_callback: bool = False):
    """Показывает следующий кредит для синхронизации или завершает.
    msg — объект Message (для ввода числа) или CallbackQuery.message (для кнопок).
    from_callback=True → промежуточные шаги редактируют сообщение; завершение — новое сообщение.
    """
    data = await state.get_data()
    queue = data.get("sync_queue", [])
    changes = data.get("sync_changes", [])

    if not queue:
        await state.clear()
        if changes:
            lines = ["✅ <b>Остатки обновлены!</b>\n"]
            for name, old, new in changes:
                diff = new - old
                sign = "+" if diff > 0 else ""
                lines.append(f"  • {name}: {old:,.0f} → <b>{new:,.0f} ₽</b>  ({sign}{diff:,.0f} ₽)")
        else:
            lines = ["✅ Всё актуально, изменений нет."]
        # Финальное сообщение всегда отправляем новым — чтобы вернуть ReplyKeyboard
        await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu())
        return

    credit_id = queue[0]
    c = await db.get_credit(credit_id)
    if not c:
        await state.update_data(sync_queue=queue[1:])
        await _sync_show_next(msg, state, from_callback=from_callback)
        return

    icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
    remaining = len(queue)
    await state.set_state(SyncBalances.balance)
    text = (
        f"{icon} <b>{c['name']}</b>  ({remaining} осталось)\n\n"
        f"В боте: <b>{c['debt']:,.0f} ₽</b>\n\n"
        "Открой банк и введи актуальный остаток (₽):"
    )
    kb = sync_balance_kb(credit_id)
    if from_callback:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "✏️ Обновить остатки")
async def sync_balances_start(message: Message, state: FSMContext):
    await state.clear()
    credits = await db.get_credits(message.from_user.id)
    active = [c for c in credits if c["debt"] > 0.01]
    if not active:
        await message.answer("Нет активных кредитов с долгом.", reply_markup=main_menu())
        return
    await state.update_data(
        sync_queue=[c["id"] for c in active],
        sync_changes=[]
    )
    await _sync_show_next(message, state, from_callback=False)


@router.callback_query(F.data == "start_sync_cb")
async def sync_balances_cb(callback: CallbackQuery, state: FSMContext):
    """Запуск «Обновить остатки» из inline-кнопки в уведомлении."""
    await callback.answer()
    await state.clear()
    credits = await db.get_credits(callback.from_user.id)
    active = [c for c in credits if c["debt"] > 0.01]
    if not active:
        await callback.message.answer("Нет активных кредитов с долгом.", reply_markup=main_menu())
        return
    await state.update_data(
        sync_queue=[c["id"] for c in active],
        sync_changes=[]
    )
    await _sync_show_next(callback.message, state, from_callback=False)


@router.message(SyncBalances.balance)
async def sync_balance_input(message: Message, state: FSMContext):
    try:
        new_debt = float(message.text.replace(" ", "").replace(",", "."))
        if new_debt < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи сумму в рублях, например: <code>47500</code>", parse_mode="HTML")
        return

    data = await state.get_data()
    queue = data.get("sync_queue", [])
    changes = data.get("sync_changes", [])

    if not queue:
        await state.clear()
        return

    credit_id = queue[0]
    c = await db.get_credit(credit_id)
    if c and abs(c["debt"] - new_debt) > 0.01:
        changes.append((c["name"], c["debt"], new_debt))
        await db.update_credit_fields(credit_id, debt=new_debt)

    await state.update_data(sync_queue=queue[1:], sync_changes=changes)
    await _sync_show_next(message, state, from_callback=False)


@router.callback_query(F.data.startswith("sync_ok:"))
async def sync_ok(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    queue = data.get("sync_queue", [])
    await state.update_data(sync_queue=queue[1:])
    await _sync_show_next(callback.message, state, from_callback=True)


@router.callback_query(F.data.startswith("sync_skip:"))
async def sync_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Пропущено")
    data = await state.get_data()
    queue = data.get("sync_queue", [])
    await state.update_data(sync_queue=queue[1:])
    await _sync_show_next(callback.message, state, from_callback=True)


@router.callback_query(F.data == "sync_done")
async def sync_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    changes = data.get("sync_changes", [])
    await state.clear()
    if changes:
        lines = ["✅ <b>Остатки обновлены!</b>\n"]
        for name, old, new in changes:
            diff = new - old
            sign = "+" if diff > 0 else ""
            lines.append(f"  • {name}: {old:,.0f} → <b>{new:,.0f} ₽</b>  ({sign}{diff:,.0f} ₽)")
    else:
        lines = ["✅ Всё актуально, изменений нет."]
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")


# ─── Стратегии ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "show_strategies")
async def show_strategies_cb(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    if not credits:
        await callback.message.edit_text(
            "Сначала добавь кредиты.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")
            ]])
        )
        return
    text, recommended, _, _ = await build_strategy_text(credits, user["monthly_budget"])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=change_strategy_kb(recommended))


@router.callback_query(F.data.startswith("change_strategy:"))
async def apply_strategy(callback: CallbackQuery):
    chosen = callback.data.split(":")[1]
    credits = await db.get_credits(callback.from_user.id)

    if chosen == "avalanche":
        order = sorted(credits, key=lambda c: c["rate_month"], reverse=True)
        name = "🏔 Лавина"
    else:
        order = sorted(credits, key=lambda c: c["debt"])
        name = "⛄ Снежный ком"

    for priority, c in enumerate(order, start=1):
        await db.update_credit_priority(c["id"], priority)

    # Сбрасываем кеш плана — пересчитается по новой стратегии
    today = date_cls.today()
    await db.save_monthly_plan(callback.from_user.id, today.year, today.month, [])

    order_lines = "\n".join(
        f"   {i+1}. {c['name']} — {c['debt']:,.0f} ₽"
        for i, c in enumerate(order)
    )
    await callback.message.edit_text(
        f"✅ Стратегия <b>{name}</b> применена!\n\n"
        f"Порядок погашения:\n{order_lines}\n\n"
        f"Нажми «◀️ Назад» чтобы вернуться к кредитам.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад к кредитам", callback_data="credits_section")
        ]])
    )


# ─── Настройки ────────────────────────────────────────────────────────────────

@router.message(F.text == "⚙️ Настройки")
async def settings_menu(message: Message):
    await _show_settings(message.answer, message.from_user.id)


@router.callback_query(F.data == "settings_budget")
async def settings_budget_cb(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"💰 <b>Изменить бюджет</b>\n\n"
        f"Сейчас: <b>{user['monthly_budget']:,.0f} ₽/мес</b>\n\n"
        "Это сумма, которую ты готов тратить каждый месяц на все кредиты.\n"
        "Чем больше бюджет — тем быстрее погасишь долги.\n\n"
        "Введи новую сумму в рублях, например: <code>90000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")
        ]])
    )
    await state.set_state(ChangeBudget.amount)


@router.callback_query(F.data == "settings_reminders")
async def settings_reminders_cb(callback: CallbackQuery):
    credits = await db.get_credits(callback.from_user.id)
    await callback.message.edit_text(
        "🔔 <b>Напоминания</b>\n\n"
        "Общие — уведомления в дни аванса/зарплаты.\n"
        "По кредиту — напоминания о дне платежа.",
        parse_mode="HTML",
        reply_markup=reminders_menu_kb(credits)
    )


@router.callback_query(F.data == "settings_paydays")
async def settings_paydays_cb(callback: CallbackQuery, state: FSMContext):
    """Настройка дат аванса/зарплаты из меню настроек."""
    await callback.answer()
    await state.update_data(payday_source="settings")
    user = await db.get_user(callback.from_user.id)
    p1   = user.get("payday1_day")
    cur  = f"Сейчас: {p1}-е число" if p1 else "Ещё не настроено"
    await callback.message.edit_text(
        f"📆 <b>Настройка дат выплат</b>\n\n{cur}\n\n"
        "Шаг 1 — введи день месяца <b>первой выплаты</b>\n"
        "(аванс или единственная зарплата):\n\n"
        "Например: <code>10</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")
        ]])
    )
    await state.set_state(PaydaySetup.p1_day)


@router.callback_query(F.data == "settings_sync")
async def settings_sync_cb(callback: CallbackQuery, state: FSMContext):
    """Обновить остатки — из меню настроек."""
    await callback.answer()
    await state.clear()
    user_id = callback.from_user.id
    credits = await db.get_credits(user_id)
    active = [c for c in credits if c["debt"] > 0.01]
    if not active:
        await callback.message.edit_text(
            "Нет активных кредитов с долгом.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")
            ]])
        )
        return
    await state.update_data(sync_queue=[c["id"] for c in active], sync_changes=[])
    await _sync_show_next(callback.message, state, from_callback=True)


@router.callback_query(F.data == "general_reminders")
async def general_reminders_cb(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    enabled = bool(user.get("payday_notify_enabled", 1))
    has_p2 = bool(user.get("payday2_day"))
    p1_day = user.get("payday1_day")
    p2_day = user.get("payday2_day")

    if not p1_day:
        text = (
            "📆 <b>Общие уведомления</b>\n\n"
            "Даты аванса/зарплаты ещё не настроены.\n"
            "Настрой их в <b>⚙️ Настройки → Даты аванса / зарплаты</b>."
        )
    else:
        status = "включены ✅" if enabled else "выключены ❌"
        p2_line = f", зарплата {p2_day}-го" if p2_day else ""
        text = (
            f"📆 <b>Общие уведомления</b>\n\n"
            f"Аванс {p1_day}-го{p2_line}\n"
            f"Статус: <b>{status}</b>\n\n"
            "Бот присылает список платежей в день аванса/зарплаты в 9:00 МСК."
        )
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=general_reminders_kb(enabled, has_p2))


@router.callback_query(F.data == "toggle_payday_notify")
async def toggle_payday_notify_cb(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    new_state = not bool(user.get("payday_notify_enabled", 1))
    await db.update_payday_notify(callback.from_user.id, new_state)
    await callback.answer("✅ Включено" if new_state else "❌ Выключено")
    # Обновляем экран
    await general_reminders_cb(callback)


async def _show_settings(edit_fn, user_id: int):
    """Общая функция отображения экрана настроек."""
    user = await db.get_user(user_id)
    override = await _get_month_override(user_id)
    today = date_cls.today()
    months_ru = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    override_line = (
        f"\n🗓 Бюджет на {months_ru[today.month]}: <b>{override:,.0f} ₽</b>"
        if override is not None else ""
    )
    p1 = user.get("payday1_day")
    p2 = user.get("payday2_day")
    paydays_line = ""
    if p1:
        paydays_line = f"\n📆 Аванс: <b>{p1}-го</b>"
        if p2:
            paydays_line += f"  |  Зарплата: <b>{p2}-го</b>"
    await edit_fn(
        f"⚙️ <b>Настройки</b>\n\n"
        f"💰 Стандартный бюджет: <b>{user['monthly_budget']:,.0f} ₽/мес</b>"
        f"{override_line}{paydays_line}",
        parse_mode="HTML",
        reply_markup=settings_kb(month_override=override)
    )


@router.callback_query(F.data == "settings_back")
async def settings_back_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_settings(callback.message.edit_text, callback.from_user.id)


# ─── Бюджет на текущий месяц ──────────────────────────────────────────────────

@router.callback_query(F.data == "settings_month_budget")
async def settings_month_budget_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    user = await db.get_user(callback.from_user.id)
    override = await _get_month_override(callback.from_user.id)
    today = date_cls.today()
    months_ru = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    month_name = months_ru[today.month]

    if override is not None:
        current_line = f"Сейчас на {month_name}: <b>{override:,.0f} ₽</b>\nСтандартный: <b>{user['monthly_budget']:,.0f} ₽/мес</b>"
    else:
        current_line = (
            f"Стандартный бюджет: <b>{user['monthly_budget']:,.0f} ₽/мес</b>\n"
            f"Переопределение на {month_name} не задано."
        )

    await callback.message.edit_text(
        f"🗓 <b>Бюджет на {month_name}</b>\n\n"
        f"{current_line}\n\n"
        "Введи сумму для этого месяца, например: <code>50000</code>\n"
        "<i>Все следующие месяцы останутся со стандартным бюджетом.</i>",
        parse_mode="HTML",
        reply_markup=month_budget_actions_kb(has_override=override is not None)
    )
    await state.set_state(ChangeMonthBudget.amount)


@router.message(ChangeMonthBudget.amount)
async def month_budget_apply(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(" ", "").replace(",", "."))
        if budget < 1000:
            await message.answer(
                "Минимальный бюджет — <b>1 000 ₽</b>. Введи число, например: <code>50000</code>",
                parse_mode="HTML"
            )
            return
        today = date_cls.today()
        await db.set_budget_override(message.from_user.id, today.year, today.month, budget)
        user = await db.get_user(message.from_user.id)
        credits = await db.get_credits(message.from_user.id)
        schedule = recalculate_schedule(credits, user["monthly_budget"], first_month_budget=budget)
        await state.clear()
        months_ru = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                     "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
        await message.answer(
            f"✅ <b>Бюджет на {months_ru[today.month]} установлен!</b>\n\n"
            f"🗓 Этот месяц: <b>{budget:,.0f} ₽</b>\n"
            f"💰 Следующие месяцы: <b>{user['monthly_budget']:,.0f} ₽/мес</b>\n"
            f"📅 По новому графику: <b>{len(schedule)} мес.</b>",
            parse_mode="HTML",
            reply_markup=settings_kb(month_override=budget)
        )
    except ValueError:
        await message.answer("Введи число, например: <code>50000</code>", parse_mode="HTML")


@router.callback_query(F.data == "clear_month_budget")
async def clear_month_budget_cb(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    today = date_cls.today()
    await db.delete_budget_override(callback.from_user.id, today.year, today.month)
    user = await db.get_user(callback.from_user.id)
    months_ru = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
                 "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    await callback.message.edit_text(
        f"🗑 Переопределение на {months_ru[today.month]} удалено.\n\n"
        f"⚙️ <b>Настройки</b>\n\n"
        f"💰 Стандартный бюджет: <b>{user['monthly_budget']:,.0f} ₽/мес</b>",
        parse_mode="HTML",
        reply_markup=settings_kb(month_override=None)
    )


# ─── Универсальная отмена FSM ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cancel_fsm:"))
async def cancel_fsm_cb(callback: CallbackQuery, state: FSMContext):
    """Отменяет любой FSM и возвращает на нужный экран."""
    await callback.answer()
    await state.clear()
    dest = callback.data.split(":", 1)[1]

    if dest == "credits_section":
        credits = await db.get_credits(callback.from_user.id)
        await callback.message.edit_text(
            "💳 <b>Кредиты и карты</b>", parse_mode="HTML",
            reply_markup=credits_section_kb(credits)
        )
    elif dest == "show_plan":
        # Перенаправляем через show_plan callback
        callback.data = "show_plan"
        await show_plan(callback)
    elif dest.startswith("remind_list:"):
        credit_id = int(dest.split(":")[1])
        c = await db.get_credit(credit_id)
        reminders = await db.get_reminders(credit_id)
        await callback.message.edit_text(
            f"🔔 <b>Напоминания — {c['name']}</b>",
            parse_mode="HTML",
            reply_markup=reminders_kb(credit_id, reminders)
        )
    elif dest.startswith("credit_detail:"):
        credit_id = int(dest.split(":")[1])
        callback.data = f"credit_detail:{credit_id}"
        await pay_credit_detail(callback, state)
    elif dest.startswith("edit_menu:"):
        credit_id = int(dest.split(":")[1])
        callback.data = f"edit_menu:{credit_id}"
        await edit_menu(callback, state=None)
    elif dest == "plan_edit_mode":
        callback.data = "plan_edit_mode"
        await plan_edit_mode(callback, state)
    elif dest in ("settings", "settings_back"):
        callback.data = "settings_back"
        await settings_back_cb(callback, state)


# ─── Бюджет (FSM) ─────────────────────────────────────────────────────────────

@router.message(F.text == "⚙️ Бюджет")
async def budget_prompt(message: Message, state: FSMContext):
    user = await db.get_user(message.from_user.id)
    await message.answer(
        f"Текущий бюджет: <b>{user['monthly_budget']:,.0f} ₽/мес</b>\n\n"
        "Введи новую сумму:",
        parse_mode="HTML"
    )
    await state.set_state(ChangeBudget.amount)


@router.message(ChangeBudget.amount)
async def budget_apply(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(" ", "").replace(",", "."))
        if budget < 1000:
            await message.answer(
                "Минимальный бюджет — <b>1 000 ₽</b>. Введи число, например: <code>90000</code>",
                parse_mode="HTML"
            )
            return
        await db.update_budget(message.from_user.id, budget)
        credits = await db.get_credits(message.from_user.id)
        schedule = recalculate_schedule(credits, budget)
        await state.clear()
        await message.answer(
            f"✅ <b>Бюджет обновлён!</b>\n\n"
            f"💰 Новый бюджет: <b>{budget:,.0f} ₽/мес</b>\n"
            f"📅 По новому графику: <b>{len(schedule)} мес.</b>",
            parse_mode="HTML",
            reply_markup=settings_kb()
        )
    except ValueError:
        await message.answer("Введи число, например: <code>90000</code>", parse_mode="HTML")


# ─── Онбординг ────────────────────────────────────────────────────────────────

@router.message(Onboarding.budget)
async def ob_budget(message: Message, state: FSMContext):
    try:
        budget = float(message.text.replace(" ", "").replace(",", "."))
        if budget < 1000:
            await message.answer("Минимальный бюджет — <b>1 000 ₽</b>. Введи число, например: <code>90000</code>", parse_mode="HTML")
            return
        await db.update_budget(message.from_user.id, budget)
        credits = await db.get_credits(message.from_user.id)

        # Вычисляем «Вау» статистику
        mn_schedule  = calc_min_only_schedule(credits)
        mn_months    = len(mn_schedule)
        mn_capped    = mn_months >= 120
        mn_interest  = sum(sum(cr["interest"] for cr in m["credits"]) for m in mn_schedule)

        av_order     = sorted(credits, key=lambda c: c["rate_month"], reverse=True)
        av_schedule  = recalculate_schedule(av_order, budget)
        av_months    = len(av_schedule)
        av_interest  = sum(sum(cr["interest"] for cr in m["credits"]) for m in av_schedule)

        sn_order     = sorted(credits, key=lambda c: c["debt"])
        sn_schedule  = recalculate_schedule(sn_order, budget)
        sn_months    = len(sn_schedule)
        sn_interest  = sum(sum(cr["interest"] for cr in m["credits"]) for m in sn_schedule)

        recommended  = "avalanche" if av_interest <= sn_interest else "snowball"
        best_interest = av_interest if recommended == "avalanche" else sn_interest
        best_months   = av_months   if recommended == "avalanche" else sn_months
        best_icon     = "🏔"        if recommended == "avalanche" else "⛄"
        best_name     = "Лавина"    if recommended == "avalanche" else "Снежный ком"
        savings       = mn_interest - best_interest
        months_faster = mn_months   - best_months

        # Сохраняем для следующего шага
        strategy_text, _, av_ids, sn_ids = await build_strategy_text(credits, budget)
        await state.update_data(
            av_order=av_ids, sn_order=sn_ids,
            ob_recommended=recommended,
            ob_strategy_text=strategy_text,
        )

        total_debt  = sum(c["debt"] for c in credits)
        mn_warn     = "\n⚠️ <i>При одних минималках долг не погасить за 10 лет!</i>" if mn_capped else ""
        faster_str  = (
            f"\n   ⚡ Быстрее на <b>{months_faster} мес.</b>"
            if months_faster > 0 and not mn_capped else ""
        )

        wow_text = (
            f"🤩 <b>Вот твоя финансовая картина!</b>\n\n"
            f"💸 Общий долг: <b>{total_debt:,.0f} ₽</b>\n"
            f"💰 Бюджет на погашение: <b>{budget:,.0f} ₽/мес</b>\n\n"
            f"😟 Если платить только минималки:\n"
            f"   Переплатишь <b>{mn_interest:,.0f} ₽</b> процентов\n"
            f"   Срок: <b>{mn_months} мес.</b>{mn_warn}\n\n"
            f"{best_icon} По стратегии <b>{best_name}</b>:\n"
            f"   Переплатишь лишь <b>{best_interest:,.0f} ₽</b>\n"
            f"   💚 Сэкономишь <b>{savings:,.0f} ₽</b>{faster_str}\n\n"
            f"👇 Выбери стратегию и начни экономить прямо сейчас!"
        )
        await message.answer(wow_text, parse_mode="HTML", reply_markup=wow_kb())
        await state.set_state(Onboarding.wow)
    except ValueError:
        await message.answer("Введи число ≥ 1000, например: <code>90000</code>", parse_mode="HTML")


@router.message(Onboarding.cr_name)
async def ob_cr_name(message: Message, state: FSMContext):
    await state.update_data(cr_name=message.text.strip())
    await message.answer(
        f"<b>{message.text.strip()}</b> — это кредит или кредитная карта?",
        parse_mode="HTML",
        reply_markup=credit_type_kb()
    )
    await state.set_state(Onboarding.cr_type)


@router.callback_query(F.data.startswith("cr_type:"))
async def ob_cr_type(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cr_type = callback.data.split(":")[1]  # 'loan', 'card' или 'debt'
    await state.update_data(cr_type=cr_type)

    if cr_type == "card":
        text = (
            "💳 Текущий долг на карте (сколько сейчас потрачено):\n"
            "Пример: <code>50000</code>"
        )
    elif cr_type == "debt":
        text = (
            "🤝 Сколько должен (₽)?\n"
            "Пример: <code>15000</code>"
        )
    else:
        text = (
            "💰 Текущий остаток долга по кредиту:\n"
            "Пример: <code>350000</code>"
        )

    await callback.message.edit_text(text, parse_mode="HTML")
    await state.set_state(Onboarding.cr_debt)


@router.message(Onboarding.cr_debt)
async def ob_cr_debt(message: Message, state: FSMContext):
    try:
        debt = float(message.text.replace(" ", "").replace(",", ""))
        if debt <= 0:
            raise ValueError
        data = await state.get_data()
        await state.update_data(cr_debt=debt)
        cr_type = data.get("cr_type")

        if cr_type == "debt":
            # Долг без процентов — сразу сохраняем
            credits_added = data.get("credits_added", [])
            credit_id = await db.add_credit(
                user_id=message.from_user.id,
                name=data["cr_name"],
                debt=debt,
                rate_month=0.0,
                payment_day=1,
                min_type="fixed",
                min_multiplier=1.0,
                min_fixed=0.0,
                priority=len(credits_added) + 1,
                credit_type="debt"
            )
            credits_added.append(credit_id)
            await state.update_data(credits_added=credits_added)
            await message.answer(
                f"✅ <b>{data['cr_name']}</b> добавлен! (🤝 Долг)\n"
                f"💰 Сумма: {debt:,.0f} ₽\n\n"
                "Ещё добавить кредит или карту?",
                parse_mode="HTML",
                reply_markup=add_more_credits_kb()
            )
            await state.set_state(Onboarding.more_credits)
        elif cr_type == "card":
            text = (
                "📈 Процентная ставка по карте <b>в год</b> (%):\n"
                "Например: <code>39.9</code> или <code>28</code>"
            )
            await message.answer(text, parse_mode="HTML")
            await state.set_state(Onboarding.cr_rate)
        else:
            text = (
                "💳 Ежемесячный платёж по кредиту (₽):\n"
                "Посмотри в приложении банка → «График платежей»\n"
                "Например: <code>15000</code> или <code>8500</code>"
            )
            await message.answer(text, parse_mode="HTML")
            await state.set_state(Onboarding.cr_rate)
    except ValueError:
        await message.answer("Введи число, например: <code>200000</code>", parse_mode="HTML")


@router.message(Onboarding.cr_rate)
async def ob_cr_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", ".").replace("%", "").strip())
        data = await state.get_data()

        if data.get("cr_type") == "card":
            if rate <= 0 or rate > 600:
                raise ValueError
            rate_month = rate / 12 / 100   # годовых → месячная доля
            await state.update_data(cr_rate=rate_month)
            confirm = f"({rate:.1f}%/год → {rate/12:.2f}%/мес)"
            text = (
                f"✅ Ставка: <b>{rate:.1f}% годовых</b> {confirm}\n\n"
                "📅 Дата выставления выписки / списания минимума (1–31):\n"
                "Например: <code>1</code>"
            )
        else:
            # For loans: rate field is actually monthly payment amount
            if rate <= 0:
                raise ValueError
            await state.update_data(cr_rate=0.0, cr_monthly_payment=rate)
            text = f"✅ Платёж: <b>{rate:,.0f} ₽/мес</b>\n\n📅 День платежа по договору (1–31):\nНапример: <code>20</code>"

        await message.answer(text, parse_mode="HTML")
        await state.set_state(Onboarding.cr_day)
    except ValueError:
        data = await state.get_data()
        if data.get("cr_type") == "card":
            await message.answer("Введи годовую ставку, например: <code>39.9</code>", parse_mode="HTML")
        else:
            await message.answer("Введи сумму ежемесячного платежа, например: <code>15000</code>", parse_mode="HTML")


@router.message(Onboarding.cr_day)
async def ob_cr_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        data = await state.get_data()
        await state.update_data(cr_day=day)

        if data.get("cr_type") == "card":
            await message.answer(
                "📋 <b>День выписки по карте</b>\n\n"
                "Это день, когда банк подводит итоги месяца и фиксирует задолженность "
                "(обычно отличается от дня платежа на 20–25 дней).\n\n"
                "Посмотри в приложении банка: «Выписка», «Дата формирования выписки» "
                "или в договоре.\n\n"
                "Например: <code>1</code> или <code>15</code>",
                parse_mode="HTML"
            )
            await state.set_state(Onboarding.cr_statement_day)
        else:
            # Loan: save immediately with defaults (fixed monthly payment)
            credits_added = data.get("credits_added", [])
            credit_id = await db.add_credit(
                user_id=message.from_user.id,
                name=data["cr_name"],
                debt=data["cr_debt"],
                rate_month=0.0,
                payment_day=day,
                min_type="fixed",
                min_multiplier=2.0,
                min_fixed=data["cr_monthly_payment"],
                priority=len(credits_added) + 1,
                credit_type="loan"
            )
            credits_added.append(credit_id)
            await state.update_data(credits_added=credits_added)
            await message.answer(
                f"✅ <b>{data['cr_name']}</b> добавлен! (🏦 Кредит)\n"
                f"💰 Долг: {data['cr_debt']:,.0f} ₽  |  Платёж: {data['cr_monthly_payment']:,.0f} ₽/мес\n\n"
                "Ещё добавить кредит или карту?",
                parse_mode="HTML",
                reply_markup=add_more_credits_kb()
            )
            await state.set_state(Onboarding.more_credits)
    except ValueError:
        await message.answer("Введи число от 1 до 31")


@router.message(Onboarding.cr_statement_day)
async def ob_cr_statement_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        await state.update_data(cr_statement_day=day)
        data = await state.get_data()
        # Save card with default min: 5% of balance (industry standard)
        credits_added = data.get("credits_added", [])
        credit_id = await db.add_credit(
            user_id=message.from_user.id,
            name=data["cr_name"],
            debt=data["cr_debt"],
            rate_month=data["cr_rate"],
            payment_day=data["cr_day"],
            min_type="percent",
            min_multiplier=5.0,
            min_fixed=None,
            priority=len(credits_added) + 1,
            credit_type="card",
            statement_day=day
        )
        credits_added.append(credit_id)
        await state.update_data(credits_added=credits_added)
        await message.answer(
            f"✅ <b>{data['cr_name']}</b> добавлена! (💳 Карта)\n"
            f"💰 Долг: {data['cr_debt']:,.0f} ₽  |  Ставка: {data['cr_rate']*12*100:.1f}%/год\n"
            f"💡 Минималку (сейчас 5% от долга) можно уточнить в карточке → ✏️ Редактировать\n\n"
            "Ещё добавить кредит или карту?",
            parse_mode="HTML",
            reply_markup=add_more_credits_kb()
        )
        await state.set_state(Onboarding.more_credits)
    except ValueError:
        await message.answer("Введи число от 1 до 31")


@router.callback_query(Onboarding.more_credits, F.data == "ob_add_more")
async def ob_add_more(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    n = len(data.get("credits_added", [])) + 1
    await callback.message.edit_text(
        f"📝 <b>Кредит / Карта {n}</b> — введи название:\n"
        f"Например: <code>Тинькофф</code>, <code>Альфа карта</code>, <code>Сбер ипотека</code>",
        parse_mode="HTML"
    )
    await state.set_state(Onboarding.cr_name)


@router.callback_query(Onboarding.more_credits, F.data == "ob_done_credits")
async def ob_done_credits(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "✅ Отлично! Все кредиты добавлены.\n\n"
        "💰 Теперь укажи <b>ежемесячный бюджет</b> на погашение всех кредитов:\n"
        "Например: <code>90000</code>",
        parse_mode="HTML"
    )
    await state.set_state(Onboarding.budget)


@router.callback_query(Onboarding.wow, F.data == "ob_wow_done")
async def ob_wow_done(callback: CallbackQuery, state: FSMContext):
    """Показывает сравнение стратегий после «Вау» экрана."""
    await callback.answer()
    data = await state.get_data()
    strategy_text = data.get("ob_strategy_text", "")
    recommended   = data.get("ob_recommended", "avalanche")
    await callback.message.edit_text(
        strategy_text, parse_mode="HTML",
        reply_markup=strategy_kb(recommended)
    )
    await state.set_state(Onboarding.strategy)


@router.callback_query(Onboarding.strategy, F.data.startswith("strategy:"))
async def ob_strategy(callback: CallbackQuery, state: FSMContext):
    chosen = callback.data.split(":")[1]
    data = await state.get_data()
    order = data["av_order"] if chosen == "avalanche" else data["sn_order"]

    for priority, credit_id in enumerate(order, start=1):
        await db.update_credit_priority(credit_id, priority)

    credits = await db.get_credits(callback.from_user.id)
    strategy_name = "🏔 Лавина" if chosen == "avalanche" else "⛄ Снежный ком"

    await state.update_data(rem_queue=[c["id"] for c in credits])
    await callback.message.edit_text(
        f"✅ Стратегия <b>{strategy_name}</b> выбрана!\n\n"
        "Теперь настроим напоминания о платежах для каждого кредита.",
        parse_mode="HTML"
    )
    await ob_ask_next_reminder(callback.message, state, callback.from_user.id)


async def ob_ask_next_reminder(message, state: FSMContext, user_id: int):
    data = await state.get_data()
    rem_queue = data.get("rem_queue", [])
    if not rem_queue:
        await state.clear()
        await message.answer(
            "🎉 <b>Всё готово!</b>\n\n"
            "Бот будет напоминать о платежах и помогать следить за долгами.\n"
            "Используй кнопки меню для управления.",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
        return
    credit_id = rem_queue[0]
    c = await db.get_credit(credit_id)
    await state.update_data(current_credit_id=credit_id)
    await message.answer(
        f"🔔 <b>Напоминания для «{c['name']}»</b>\n"
        f"📅 День платежа: {c['payment_day']}-е число\n\n"
        "За сколько дней до платежа напомнить?",
        parse_mode="HTML",
        reply_markup=rem_days_kb()
    )
    await state.set_state(Onboarding.rem_days)


@router.callback_query(Onboarding.rem_days, F.data.startswith("rem_days:"))
async def ob_rem_days(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    if val == "skip":
        data = await state.get_data()
        await state.update_data(rem_queue=data["rem_queue"][1:])
        await callback.answer()
        await ob_ask_next_reminder(callback.message, state, callback.from_user.id)
        return

    await state.update_data(rem_days_before=int(val))
    data = await state.get_data()
    c = await db.get_credit(data["current_credit_id"])
    await callback.message.edit_text(
        f"🔔 <b>Напоминания для «{c['name']}»</b>\n"
        f"📅 День платежа: {c['payment_day']}-е число\n\n"
        "⏰ В какое время напомнить? (МСК)",
        parse_mode="HTML",
        reply_markup=rem_time_kb()
    )
    await state.set_state(Onboarding.rem_time)


@router.callback_query(Onboarding.rem_time, F.data.startswith("rem_time:"))
async def ob_rem_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split(":", 1)[1]
    hour, minute = map(int, time_str.split(":"))
    data = await state.get_data()
    credit_id = data["current_credit_id"]
    days_before = data["rem_days_before"]

    await db.add_reminder(
        credit_id=credit_id,
        user_id=callback.from_user.id,
        days_before=days_before,
        hour=hour,
        minute=minute
    )
    day_label = "в день платежа" if days_before == 0 else f"за {days_before} дн. до платежа"
    await callback.message.edit_text(
        f"✅ Напоминание добавлено: {day_label} в {hour:02d}:{minute:02d}",
        parse_mode="HTML",
        reply_markup=rem_more_kb()
    )
    await state.set_state(Onboarding.rem_more)


@router.callback_query(Onboarding.rem_more, F.data.startswith("rem_more:"))
async def ob_rem_more(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    data = await state.get_data()
    if val == "yes":
        c = await db.get_credit(data["current_credit_id"])
        await callback.message.edit_text(
            f"🔔 Ещё напоминание для «{c['name']}» — за сколько дней?",
            parse_mode="HTML",
            reply_markup=rem_days_kb()
        )
        await state.set_state(Onboarding.rem_days)
    else:
        await state.update_data(rem_queue=data["rem_queue"][1:])
        await callback.answer()
        await ob_ask_next_reminder(callback.message, state, callback.from_user.id)


# ─── Проверка выписки по кредитной карте ──────────────────────────────────────

STMT_MOVED = "Эта проверка больше не нужна — проценты по кредиту бот считает сам." 


@router.callback_query(F.data.startswith("stmt_enter:"))
async def stmt_enter(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(STMT_MOVED)
    await callback.answer()


@router.callback_query(F.data.startswith("stmt_skip:"))
async def stmt_skip(callback: CallbackQuery):
    await callback.message.edit_text(
        callback.message.text + "\n\n⏩ <b>Пропущено.</b>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stmt_ok:"))
async def stmt_ok(callback: CallbackQuery):
    await callback.message.edit_text(STMT_MOVED)


@router.callback_query(F.data.startswith("stmt_wrong:"))
async def stmt_wrong(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(STMT_MOVED)


# ─── Месячный план платежей ───────────────────────────────────────────────────

MONTHS_RU = ["", "январе", "феврале", "марте", "апреле", "мае", "июне",
             "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]


# generate_plan импортирован из calculator.py


def _format_plan_text(allocations: list, cmap: dict, user: dict,
                      year: int, month: int, month_override: float = None) -> str:
    p1_day    = user.get("payday1_day") or "?"
    p1_amount = user.get("payday1_amount")
    p2_day    = user.get("payday2_day")
    p2_amount = user.get("payday2_amount")
    budget    = month_override if month_override is not None else user["monthly_budget"]

    p1_allocs = [a for a in allocations if a["payday"] == 1]
    p2_allocs = [a for a in allocations if a["payday"] == 2]
    p1_total  = sum(a["amount"] for a in p1_allocs)
    p2_total  = sum(a["amount"] for a in p2_allocs)

    def block(allocs, total, day, bgt, label):
        lines = [f"\n📆 <b>{label} — {day}-го числа</b>"]
        if not allocs:
            lines.append("   — платежей нет")
        for a in allocs:
            c = cmap.get(a["credit_id"])
            if c:
                icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
                lines.append(f"   {icon} {c['name']} — <b>{a['amount']:,.0f} ₽</b>")
        lines.append(f"   💸 Итого за выплату: <b>{total:,.0f} ₽</b>")
        return "\n".join(lines)

    budget_line = (
        f"🗓 Бюджет этого месяца: <b>{budget:,.0f} ₽</b>  "
        f"<i>(стандарт: {user['monthly_budget']:,.0f} ₽)</i>"
        if month_override is not None
        else f"💰 Бюджет: <b>{budget:,.0f} ₽/мес</b>"
    )
    lines = [
        f"📅 <b>План платежей на {MONTHS_RU[month]} {year}</b>\n",
        budget_line,
        block(p1_allocs, p1_total, p1_day, p1_amount,
              "Аванс / 1-я выплата" if p2_day else "Выплата"),
    ]
    if p2_day:
        lines.append(block(p2_allocs, p2_total, p2_day, p2_amount, "Зарплата / 2-я выплата"))
    lines.append(f"\n💸 Итого: <b>{p1_total + p2_total:,.0f} ₽</b> из <b>{budget:,.0f} ₽</b>")
    return "\n".join(lines)


async def _get_or_gen_plan(user_id, user, credits, year, month):
    plan = await db.get_monthly_plan(user_id, year, month)
    if plan:
        return plan
    p1_amount = user.get("payday1_amount") or user["monthly_budget"]
    p2_amount = user.get("payday2_amount")
    today = date_cls.today()
    first_month_budget = None
    if year == today.year and month == today.month:
        first_month_budget = await db.get_budget_override(user_id, year, month)
    allocations = generate_plan(credits, user["monthly_budget"], p1_amount, p2_amount,
                                first_month_budget=first_month_budget)
    if allocations:
        await db.save_monthly_plan(user_id, year, month, allocations)
    return allocations


@router.callback_query(F.data == "show_plan")
async def show_plan(callback: CallbackQuery):
    user_id = callback.from_user.id
    user    = await db.get_user(user_id)
    credits = await db.get_credits(user_id)
    if not credits:
        await callback.message.edit_text(
            "📅 Нет активных кредитов для планирования.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")
            ]])
        )
        return
    today = date_cls.today()
    plan  = await _get_or_gen_plan(user_id, user, credits, today.year, today.month)
    cmap  = {c["id"]: c for c in credits}
    override = await db.get_budget_override(user_id, today.year, today.month)

    hint = ""
    if not user.get("payday1_day"):
        hint = "\n\n💡 <i>Настрой даты выплат — и план разобьётся на аванс и зарплату.</i>"

    await callback.message.edit_text(
        _format_plan_text(plan, cmap, user, today.year, today.month, month_override=override) + hint,
        parse_mode="HTML", reply_markup=plan_view_kb()
    )


@router.callback_query(F.data == "plan_view")
async def plan_view_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_plan(callback)


@router.callback_query(F.data == "plan_reset")
async def plan_reset(callback: CallbackQuery):
    user_id = callback.from_user.id
    user    = await db.get_user(user_id)
    credits = await db.get_credits(user_id)
    today   = date_cls.today()
    p1_amount = user.get("payday1_amount") or user["monthly_budget"]
    p2_amount = user.get("payday2_amount")
    override = await db.get_budget_override(user_id, today.year, today.month)
    allocs  = generate_plan(credits, user["monthly_budget"], p1_amount, p2_amount,
                             first_month_budget=override)
    await db.save_monthly_plan(user_id, today.year, today.month, allocs)
    cmap = {c["id"]: c for c in credits}
    await callback.message.edit_text(
        _format_plan_text(allocs, cmap, user, today.year, today.month, month_override=override) +
        "\n\n🔄 <i>План пересчитан автоматически.</i>",
        parse_mode="HTML", reply_markup=plan_view_kb()
    )


@router.callback_query(F.data == "plan_edit_mode")
async def plan_edit_mode(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    user    = await db.get_user(user_id)
    credits = await db.get_credits(user_id)
    today   = date_cls.today()
    plan    = await _get_or_gen_plan(user_id, user, credits, today.year, today.month)
    cmap    = {c["id"]: c for c in credits}
    override = await db.get_budget_override(user_id, today.year, today.month)
    await callback.message.edit_text(
        _format_plan_text(plan, cmap, user, today.year, today.month, month_override=override) +
        "\n\n✏️ <i>Нажми на строку, чтобы изменить</i>",
        parse_mode="HTML", reply_markup=plan_edit_kb(plan, cmap)
    )


@router.callback_query(F.data.startswith("plan_edit_item:"))
async def plan_edit_item(callback: CallbackQuery):
    _, cid_s, pd_s = callback.data.split(":")
    credit_id = int(cid_s)
    payday    = int(pd_s)
    c    = await db.get_credit(credit_id)
    user = await db.get_user(callback.from_user.id)
    today = date_cls.today()
    plan  = await db.get_monthly_plan(callback.from_user.id, today.year, today.month)
    cur   = next((a for a in plan if a["credit_id"] == credit_id), None)
    amt   = cur["amount"] if cur else 0
    icon  = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
    pd_label = "① аванс" if payday == 1 else "② зарплата"
    await callback.message.edit_text(
        f"✏️ <b>{icon} {c['name']}</b>\n"
        f"Выплата: <b>{pd_label}</b>   Сумма: <b>{amt:,.0f} ₽</b>\n\n"
        "Что изменить?",
        parse_mode="HTML",
        reply_markup=plan_edit_item_kb(credit_id, payday, bool(user.get("payday2_day")))
    )


@router.callback_query(F.data.startswith("plan_change_amount:"))
async def plan_change_amount_start(callback: CallbackQuery, state: FSMContext):
    _, cid_s, pd_s = callback.data.split(":")
    credit_id = int(cid_s)
    payday    = int(pd_s)
    c    = await db.get_credit(credit_id)
    user = await db.get_user(callback.from_user.id)
    today = date_cls.today()
    plan  = await db.get_monthly_plan(callback.from_user.id, today.year, today.month)

    cur      = next((a for a in plan if a["credit_id"] == credit_id), None)
    cur_amt  = cur["amount"] if cur else 0
    pd_bgt   = user.get(f"payday{payday}_amount") or user["monthly_budget"]
    others   = sum(a["amount"] for a in plan if a["payday"] == payday and a["credit_id"] != credit_id)
    free     = max(0.0, pd_bgt - others)
    icon     = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
    pd_label = "① аванс" if payday == 1 else "② зарплата"

    await state.update_data(plan_credit_id=credit_id, plan_payday=payday,
                             plan_year=today.year, plan_month=today.month,
                             plan_pd_budget=pd_bgt)
    await callback.message.edit_text(
        f"✏️ <b>{icon} {c['name']}</b> — {pd_label}\n\n"
        f"Сейчас: <b>{cur_amt:,.0f} ₽</b>\n"
        f"Бюджет выплаты: <b>{pd_bgt:,.0f} ₽</b>  |  Свободно: <b>{free:,.0f} ₽</b>\n\n"
        "Введи новую сумму (₽):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад",
                                 callback_data=f"plan_edit_item:{credit_id}:{payday}")
        ]])
    )
    await state.set_state(EditPlan.amount)


@router.message(EditPlan.amount)
async def plan_edit_amount_apply(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", ""))
        if amount < 0:
            raise ValueError
        data      = await state.get_data()
        credit_id = data["plan_credit_id"]
        payday    = data["plan_payday"]
        year      = data["plan_year"]
        month     = data["plan_month"]
        pd_bgt    = data["plan_pd_budget"]
        user_id   = message.from_user.id

        await db.update_plan_item(user_id, year, month, credit_id, payday, amount)
        await state.clear()

        plan     = await db.get_monthly_plan(user_id, year, month)
        pd_total = sum(a["amount"] for a in plan if a["payday"] == payday)
        warn = ""
        if pd_bgt and pd_total > pd_bgt + 0.01:
            warn = f"\n⚠️ Бюджет выплаты превышен на <b>{pd_total - pd_bgt:,.0f} ₽</b>"

        c    = await db.get_credit(credit_id)
        icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
        pd_label = "① аванс" if payday == 1 else "② зарплата"
        await message.answer(
            f"✅ Сохранено! {icon} <b>{c['name']}</b> — {pd_label}: <b>{amount:,.0f} ₽</b>{warn}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Продолжить редактирование",
                                      callback_data="plan_edit_mode")],
                [InlineKeyboardButton(text="📅 Посмотреть план",
                                      callback_data="plan_view")],
            ])
        )
    except ValueError:
        await message.answer("Введи сумму в рублях, например: <code>5000</code>",
                             parse_mode="HTML")


@router.callback_query(F.data.startswith("plan_move:"))
async def plan_move(callback: CallbackQuery):
    _, cid_s, pd_s = callback.data.split(":")
    credit_id   = int(cid_s)
    from_payday = int(pd_s)
    to_payday   = 2 if from_payday == 1 else 1
    user_id     = callback.from_user.id
    user        = await db.get_user(user_id)
    today       = date_cls.today()
    plan        = await db.get_monthly_plan(user_id, today.year, today.month)

    cur = next((a for a in plan if a["credit_id"] == credit_id), None)
    if not cur:
        await callback.answer("Не найдено в плане", show_alert=True)
        return

    await db.update_plan_item(user_id, today.year, today.month,
                               credit_id, to_payday, cur["amount"])

    credits  = await db.get_credits(user_id)
    cmap     = {c["id"]: c for c in credits}
    new_plan = await db.get_monthly_plan(user_id, today.year, today.month)

    to_label = "② зарплата" if to_payday == 2 else "① аванс"
    c    = await db.get_credit(credit_id)
    icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")

    to_bgt   = user.get(f"payday{to_payday}_amount")
    to_total = sum(a["amount"] for a in new_plan if a["payday"] == to_payday)
    warn = ""
    if to_bgt and to_total > to_bgt + 0.01:
        warn = f"\n⚠️ Бюджет {to_label} превышен на <b>{to_total - to_bgt:,.0f} ₽</b>"

    override = await db.get_budget_override(user_id, today.year, today.month)
    await callback.message.edit_text(
        _format_plan_text(new_plan, cmap, user, today.year, today.month, month_override=override) +
        f"\n\n↕️ {icon} <b>{c['name']}</b> перенесён в {to_label}.{warn}",
        parse_mode="HTML",
        reply_markup=plan_edit_kb(new_plan, cmap)
    )


# ─── Настройка дат выплат ─────────────────────────────────────────────────────

@router.callback_query(F.data == "plan_setup_paydays")
async def plan_setup_paydays(callback: CallbackQuery, state: FSMContext):
    await state.update_data(payday_source="plan")
    user = await db.get_user(callback.from_user.id)
    p1   = user.get("payday1_day")
    cur  = f"Сейчас: {p1}-е число" if p1 else "Ещё не настроено"
    await callback.message.edit_text(
        f"📆 <b>Настройка дат выплат</b>\n\n{cur}\n\n"
        "Шаг 1 — введи день месяца <b>первой выплаты</b>\n"
        "(аванс или единственная зарплата):\n\n"
        "Например: <code>10</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад к плану", callback_data="plan_view")
        ]])
    )
    await state.set_state(PaydaySetup.p1_day)


@router.message(PaydaySetup.p1_day)
async def payday_p1_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        await state.update_data(p1_day=day)
        data    = await state.get_data()
        source  = data.get("payday_source", "plan")
        cancel_dest = "settings_back" if source == "settings" else "show_plan"
        user    = await db.get_user(message.from_user.id)
        cur_amt = user.get("payday1_amount")
        hint    = f"Сейчас: {cur_amt:,.0f} ₽\n\n" if cur_amt else ""
        await message.answer(
            f"✅ День первой выплаты: <b>{day}-е число</b>\n\n"
            f"{hint}Сколько обычно приходит в эту выплату? (₽)\n"
            "Например: <code>50000</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb(cancel_dest)
        )
        await state.set_state(PaydaySetup.p1_amount)
    except ValueError:
        await message.answer("Введи число от 1 до 31, например: <code>10</code>",
                             parse_mode="HTML")


@router.message(PaydaySetup.p1_amount)
async def payday_p1_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
        await state.update_data(p1_amount=amount)
        await message.answer(
            f"✅ Первая выплата: <b>{amount:,.0f} ₽</b>\n\n"
            "Есть ли <b>вторая выплата</b> в месяц?\n"
            "(аванс + зарплата, или другая регулярная выплата)",
            parse_mode="HTML",
            reply_markup=payday_has_p2_kb()
        )
        await state.set_state(PaydaySetup.has_p2)
    except ValueError:
        await message.answer("Введи сумму в рублях, например: <code>50000</code>",
                             parse_mode="HTML")


@router.callback_query(PaydaySetup.has_p2, F.data.startswith("payday_has_p2:"))
async def payday_has_p2(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":")[1]
    data   = await state.get_data()
    source = data.get("payday_source", "plan")
    if choice == "no":
        await db.update_user_paydays(callback.from_user.id,
                                      p1_day=data["p1_day"], p1_amount=data["p1_amount"])
        today = date_cls.today()
        await db.save_monthly_plan(callback.from_user.id, today.year, today.month, [])
        await state.clear()
        if source == "settings":
            btn_text, btn_cb = "◀️ Настройки", "settings_back"
        else:
            btn_text, btn_cb = "📅 Посмотреть план", "show_plan"
        await callback.message.edit_text(
            f"✅ <b>Готово!</b>\n\n"
            f"📆 Выплата: <b>{data['p1_day']}-е число</b> — <b>{data['p1_amount']:,.0f} ₽</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=btn_text, callback_data=btn_cb)
            ]])
        )
    else:
        cancel_dest = "settings_back" if source == "settings" else "show_plan"
        await callback.message.edit_text(
            "Шаг 3 — введи день месяца <b>второй выплаты</b> (зарплата):\n\n"
            "Например: <code>25</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb(cancel_dest)
        )
        await state.set_state(PaydaySetup.p2_day)


@router.message(PaydaySetup.p2_day)
async def payday_p2_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        await state.update_data(p2_day=day)
        data   = await state.get_data()
        source = data.get("payday_source", "plan")
        cancel_dest = "settings_back" if source == "settings" else "show_plan"
        await message.answer(
            f"✅ День второй выплаты: <b>{day}-е число</b>\n\n"
            "Сколько обычно приходит во вторую выплату? (₽)\n"
            "Например: <code>80000</code>",
            parse_mode="HTML",
            reply_markup=cancel_kb(cancel_dest)
        )
        await state.set_state(PaydaySetup.p2_amount)
    except ValueError:
        await message.answer("Введи число от 1 до 31", parse_mode="HTML")


@router.message(PaydaySetup.p2_amount)
async def payday_p2_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        source = data.get("payday_source", "plan")
        await db.update_user_paydays(message.from_user.id,
                                      p1_day=data["p1_day"], p1_amount=data["p1_amount"],
                                      p2_day=data["p2_day"], p2_amount=amount)
        today = date_cls.today()
        await db.save_monthly_plan(message.from_user.id, today.year, today.month, [])
        await state.clear()
        if source == "settings":
            btn_text, btn_cb = "◀️ Настройки", "settings_back"
        else:
            btn_text, btn_cb = "📅 Посмотреть план", "show_plan"
        await message.answer(
            f"✅ <b>Готово!</b>\n\n"
            f"① Аванс: <b>{data['p1_day']}-е</b> — <b>{data['p1_amount']:,.0f} ₽</b>\n"
            f"② Зарплата: <b>{data['p2_day']}-е</b> — <b>{amount:,.0f} ₽</b>\n"
            f"💰 Итого: <b>{data['p1_amount'] + amount:,.0f} ₽/мес</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=btn_text, callback_data=btn_cb)
            ]])
        )
    except ValueError:
        await message.answer("Введи сумму в рублях, например: <code>80000</code>",
                             parse_mode="HTML")


# ─── Редактирование кредита ───────────────────────────────────────────────────

def _back_edit_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад к редактированию", callback_data=f"edit_menu:{credit_id}")
    ]])


def _current_min_desc(c: dict) -> str:
    if c["min_type"] == "fixed":
        return f"фикс. {c['min_fixed']:,.0f} ₽/мес"
    return f"{c['min_multiplier']:.0f}% от суммы долга"


@router.callback_query(F.data.startswith("edit_menu:"))
async def edit_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return
    is_card = c.get("credit_type") == "card"
    icon = "💳" if is_card else "🏦"
    await callback.message.edit_text(
        f"✏️ <b>Редактирование {icon} {c['name']}</b>\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=edit_credit_kb(credit_id, is_card)
    )


@router.callback_query(F.data.startswith("edit:"))
async def edit_field_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    field = parts[1]
    credit_id = int(parts[2])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return

    await state.update_data(edit_credit_id=credit_id, edit_field=field)
    is_card = c.get("credit_type") == "card"
    icon = "💳" if is_card else "🏦"
    header = f"✏️ <b>{icon} {c['name']}</b>\n"

    if field == "name":
        text = (
            f"{header}"
            f"📝 <b>Изменить название</b>\n"
            f"Сейчас: <b>{c['name']}</b>\n\n"
            "Введи новое название:"
        )

    elif field == "debt":
        text = (
            f"{header}"
            f"💰 <b>Изменить остаток долга</b>\n"
            f"Сейчас: <b>{c['debt']:,.0f} ₽</b>\n\n"
            "Введи актуальную сумму долга в рублях.\n"
            "Посмотри в приложении банка — главный экран карты/кредита.\n\n"
            "Пример: <code>87500</code>"
        )

    elif field == "rate":
        if is_card:
            current_rate = f"{c['rate_month'] * 12 * 100:.1f}% годовых"
            hint = (
                "Посмотри в приложении банка: зайди в карту → «Тариф» или «Условия по карте».\n"
                "Ищи строку «Процентная ставка» или «Ставка по кредиту».\n\n"
                "Вводи <b>годовую</b> ставку, например: <code>39.9</code>"
            )
        else:
            current_rate = f"{c['rate_month'] * 100:.2f}%/мес"
            hint = (
                "Посмотри в договоре или приложении банка → «Условия кредита».\n"
                "Ищи строку «Процентная ставка в месяц».\n\n"
                "Вводи <b>месячную</b> ставку, например: <code>1.5</code>"
            )
        text = (
            f"{header}"
            f"📈 <b>Изменить процентную ставку</b>\n"
            f"Сейчас: <b>{current_rate}</b>\n\n"
            f"{hint}"
        )

    elif field == "payment_day":
        text = (
            f"{header}"
            f"📅 <b>Изменить день платежа</b>\n"
            f"Сейчас: <b>{c['payment_day']}-е число каждого месяца</b>\n\n"
            "Посмотри в приложении банка или договоре — день списания обязательного платежа.\n\n"
            "Введи число от 1 до 31, например: <code>25</code>"
        )

    elif field == "statement_day":
        cur = f"{c['statement_day']}-е число" if c.get("statement_day") else "не задан"
        text = (
            f"{header}"
            f"📋 <b>Изменить день выписки</b>\n"
            f"Сейчас: <b>{cur}</b>\n\n"
            "День выписки — это когда банк подводит итоги месяца и фиксирует задолженность.\n"
            "Обычно за 20–25 дней до даты платежа.\n"
            "Смотри: приложение банка → «Выписка» или «Дата формирования выписки».\n\n"
            "Введи число от 1 до 31, например: <code>1</code>"
        )

    elif field == "priority":
        text = (
            f"{header}"
            f"🔢 <b>Изменить приоритет погашения</b>\n"
            f"Сейчас: <b>{c['priority']}</b>\n\n"
            "1 = гасить первым, 2 = вторым, и т.д.\n"
            "Чем меньше число — тем раньше бот направит туда свободные деньги.\n\n"
            "Введи целое число, например: <code>1</code>"
        )

    elif field == "min":
        cur_min = _current_min_desc(c)
        where = (
            "приложение банка → зайди в карту → «Тариф» или «Условия по карте»"
            if is_card else
            "приложение банка → найди кредит → «Условия» или «График платежей»"
        )
        text = (
            f"{header}"
            f"✅ <b>Изменить минимальный платёж</b>\n"
            f"Сейчас: <b>{cur_min}</b>\n\n"
            f"Открой {where}.\n"
            "Найди строку «Минимальный платёж» или «Обязательный платёж»:\n\n"
            "1️⃣ <b>Процент от долга</b>\n"
            "   Пример: <i>«5% от суммы задолженности»</i>\n"
            "   → сумма каждый месяц разная\n\n"
            "2️⃣ <b>Фиксированная сумма</b>\n"
            "   Пример: <i>«1 500 ₽ в месяц»</i>\n"
            "   → сумма всегда одинакова\n\n"
            "❓ <i>Не можешь найти — введи <code>1</code> (карта) или <code>2</code> (кредит)</i>\n\n"
            "Введи <code>1</code> или <code>2</code>:"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_edit_kb(credit_id))
        await state.set_state(EditCredit.min_type)
        return

    else:
        text = f"Введи новое значение:"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_back_edit_kb(credit_id))
    await state.set_state(EditCredit.value)


@router.message(EditCredit.value)
async def edit_field_value(message: Message, state: FSMContext):
    data = await state.get_data()
    credit_id = data["edit_credit_id"]
    field = data["edit_field"]
    c = await db.get_credit(credit_id)
    raw = message.text.strip()

    try:
        if field == "name":
            if not raw:
                raise ValueError
            await db.update_credit_fields(credit_id, name=raw)
            result = f"Название: <b>{raw}</b>"

        elif field == "debt":
            val = float(raw.replace(" ", "").replace(",", ""))
            if val < 0:
                raise ValueError
            await db.update_credit_fields(credit_id, debt=val)
            result = f"Долг: <b>{val:,.0f} ₽</b>"

        elif field == "rate":
            val = float(raw.replace(",", ".").replace("%", "").replace(" ", ""))
            if val <= 0:
                raise ValueError
            is_card = c.get("credit_type") == "card"
            rate_month = val / 12 / 100 if is_card else val / 100
            await db.update_credit_fields(credit_id, rate_month=rate_month)
            result = f"Ставка: <b>{val}%</b> → {rate_month*100:.4f}%/мес"

        elif field == "payment_day":
            val = int(raw)
            if not 1 <= val <= 31:
                raise ValueError
            await db.update_credit_fields(credit_id, payment_day=val)
            result = f"День платежа: <b>{val}-е</b>"

        elif field == "statement_day":
            val = int(raw)
            if not 1 <= val <= 31:
                raise ValueError
            await db.update_credit_fields(credit_id, statement_day=val)
            result = f"День выписки: <b>{val}-е</b>"

        elif field == "priority":
            val = int(raw)
            if val < 1:
                raise ValueError
            await db.update_credit_fields(credit_id, priority=val)
            result = f"Приоритет: <b>{val}</b>"

        else:
            await message.answer("Неизвестное поле.")
            await state.clear()
            return

    except (ValueError, KeyError):
        await message.answer(
            "⚠️ Неверный формат. Проверь и попробуй ещё раз.",
            reply_markup=_back_edit_kb(credit_id)
        )
        return

    await state.clear()
    c = await db.get_credit(credit_id)
    await message.answer(
        f"✅ Сохранено: {result}\n\n" + _build_credit_detail(c),
        parse_mode="HTML",
        reply_markup=credit_detail_kb(credit_id, c.get("credit_type"))
    )


@router.message(EditCredit.min_type)
async def edit_min_type(message: Message, state: FSMContext):
    data = await state.get_data()
    credit_id = data["edit_credit_id"]
    c = await db.get_credit(credit_id)
    is_card = c.get("credit_type") == "card"
    icon = "💳" if is_card else "🏦"
    header = f"✏️ <b>{icon} {c['name']}</b>\n"
    choice = message.text.strip()

    is_card = c.get("credit_type") == "card"
    if choice == "1":
        new_min_type = "percent" if is_card else "formula"
        await state.update_data(edit_min_type=new_min_type)
        await message.answer(
            f"{header}"
            "✅ <b>Минимальный платёж — процент от долга</b>\n\n"
            "В тарифе найди строку вроде:\n"
            "<i>«Минимальный платёж — <b>5%</b> от суммы задолженности»</i>\n\n"
            "Введи только цифру без знака %\n"
            "Например: <code>5</code>\n\n"
            "❓ <i>Не нашёл — введи <code>5</code> (среднее по рынку)</i>",
            parse_mode="HTML",
            reply_markup=_back_edit_kb(credit_id)
        )
    elif choice == "2":
        await state.update_data(edit_min_type="fixed")
        await message.answer(
            f"{header}"
            "✅ <b>Минимальный платёж — фиксированная сумма</b>\n\n"
            "В тарифе найди строку вроде:\n"
            "<i>«Минимальный платёж — <b>1 500 ₽</b> в месяц»</i>\n\n"
            "Введи сумму в рублях, например: <code>1500</code>",
            parse_mode="HTML",
            reply_markup=_back_edit_kb(credit_id)
        )
    else:
        await message.answer(
            "Введи <code>1</code> или <code>2</code>",
            parse_mode="HTML",
            reply_markup=_back_edit_kb(credit_id)
        )
        return
    await state.set_state(EditCredit.min_val)


@router.message(EditCredit.min_val)
async def edit_min_val(message: Message, state: FSMContext):
    data = await state.get_data()
    credit_id = data["edit_credit_id"]
    try:
        val = float(message.text.replace(",", ".").replace(" ", ""))
        if val <= 0:
            raise ValueError
        min_type = data["edit_min_type"]

        if min_type in ("formula", "percent"):
            await db.update_credit_fields(credit_id, min_type=min_type, min_multiplier=val, min_fixed=None)
            result = f"Минималка: <b>{val:.0f}% от долга</b>"
        else:
            await db.update_credit_fields(credit_id, min_type="fixed", min_fixed=val)
            result = f"Минималка: <b>фикс. {val:,.0f} ₽</b>"

        await state.clear()
        c = await db.get_credit(credit_id)
        await message.answer(
            f"✅ Сохранено: {result}\n\n" + _build_credit_detail(c),
            parse_mode="HTML",
            reply_markup=credit_detail_kb(credit_id, c.get("credit_type"))
        )
    except ValueError:
        await message.answer(
            "⚠️ Введи число, например: <code>5</code> или <code>1500</code>",
            parse_mode="HTML",
            reply_markup=_back_edit_kb(credit_id)
        )


# ─── Расход с кредитной карты ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("card_expense:"))
async def card_expense_start(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден", show_alert=True)
        return
    await callback.answer()
    await state.update_data(expense_credit_id=credit_id, expense_prev_debt=c["debt"])
    await callback.message.edit_text(
        f"💸 <b>Расход с карты «{c['name']}»</b>\n\n"
        f"Текущий долг: <b>{c['debt']:,.0f} ₽</b>\n\n"
        "Введи сумму расхода (сколько потратил с карты):\n"
        "Например: <code>3500</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb(f"credit_detail:{credit_id}")
    )
    await state.set_state(CardExpense.amount)


@router.message(CardExpense.amount)
async def card_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(" ", "").replace(",", "."))
        if amount <= 0:
            raise ValueError
        data = await state.get_data()
        credit_id = data["expense_credit_id"]
        prev_debt = data["expense_prev_debt"]

        c = await db.get_credit(credit_id)
        if not c:
            await state.clear()
            await message.answer("Кредит не найден.", reply_markup=main_menu())
            return

        new_debt = prev_debt + amount
        # Увеличиваем долг (add_paid=0 — расход, не платёж)
        await db.update_credit_debt(credit_id, new_debt, 0)
        # Записываем расход в историю
        await db.record_expense(credit_id, message.from_user.id, amount, new_debt)

        # Сбрасываем кэш плана на текущий месяц
        today = date_cls.today()
        await db.save_monthly_plan(message.from_user.id, today.year, today.month, [])

        # Считаем, как изменился план
        user        = await db.get_user(message.from_user.id)
        credits     = await db.get_credits(message.from_user.id)
        schedule_new = recalculate_schedule(credits, user["monthly_budget"])
        months_new   = len(schedule_new)
        interest_new = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_new)

        # Для сравнения — пересчёт со старым долгом
        old_credits = [dict(cr) for cr in credits]
        for cr in old_credits:
            if cr["id"] == credit_id:
                cr["debt"] = prev_debt
                break
        schedule_old = recalculate_schedule(old_credits, user["monthly_budget"])
        months_old   = len(schedule_old)
        interest_old = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_old)

        months_diff   = months_new - months_old
        interest_diff = interest_new - interest_old

        impact_lines = []
        if months_diff > 0:
            impact_lines.append(f"📅 Срок погашения увеличился на <b>{months_diff} мес.</b>")
        elif months_diff == 0:
            impact_lines.append("📅 На срок погашения не влияет")
        if interest_diff > 0.5:
            impact_lines.append(f"📈 Переплата вырастет на <b>{interest_diff:,.0f} ₽</b>")
        impact_text = "\n".join(impact_lines) if impact_lines else "Изменения незначительны"

        await state.clear()
        await message.answer(
            f"💸 <b>Расход зафиксирован!</b>\n\n"
            f"💳 {c['name']}\n"
            f"📊 Было: <b>{prev_debt:,.0f} ₽</b>  →  Стало: <b>{new_debt:,.0f} ₽</b>  (+{amount:,.0f} ₽)\n\n"
            f"🔄 <b>Что изменилось в плане:</b>\n{impact_text}\n\n"
            f"📅 Итого срок: <b>{months_new} мес.</b>  |  Переплата: <b>{interest_new:,.0f} ₽</b>",
            parse_mode="HTML",
            reply_markup=credit_detail_kb(credit_id, c.get("credit_type"))
        )
    except ValueError:
        await message.answer(
            "Введи сумму расхода, например: <code>3500</code>",
            parse_mode="HTML"
        )


# ─── Досрочное погашение ──────────────────────────────────────────────────────

@router.callback_query(F.data == "early_calc")
async def show_early_calc(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    credits = await db.get_credits(callback.from_user.id)
    active = [c for c in credits if c["debt"] > 0.01]
    if not active:
        await callback.message.edit_text(
            "Нет активных кредитов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Календарь", callback_data="open_calendar")
            ]])
        )
        return
    await callback.message.edit_text(
        "🚀 <b>Досрочное погашение</b>\n\nВыбери кредит:",
        parse_mode="HTML",
        reply_markup=early_credits_kb(active)
    )


@router.callback_query(F.data.startswith("early_credit:"))
async def early_select_credit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден", show_alert=True)
        return
    interest = calc_interest(c, c["debt"])
    icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
    await callback.message.edit_text(
        f"🚀 <b>Досрочное погашение</b>\n\n"
        f"{icon} <b>{c['name']}</b>\n"
        f"Долг: <b>{c['debt']:,.0f} ₽</b>  +  % {interest:,.0f} ₽\n"
        f"Чтобы закрыть полностью: <b>{c['debt'] + interest:,.0f} ₽</b>\n\n"
        "Как гасим?",
        parse_mode="HTML",
        reply_markup=early_type_kb(credit_id)
    )


@router.callback_query(F.data.startswith("early_full:"))
async def early_full(callback: CallbackQuery):
    await callback.answer()
    credit_id = int(callback.data.split(":")[1])
    user    = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    c = next((x for x in credits if x["id"] == credit_id), None)
    if not c:
        await callback.answer("Кредит не найден", show_alert=True)
        return

    override = await _get_month_override(callback.from_user.id)
    result = calc_credit_early_payment(credits, user["monthly_budget"], credit_id,
                                       extra_amount=0, full=True)
    icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
    text = _early_result_text(c, result, icon, full=True, override=override,
                               budget=user["monthly_budget"])
    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=early_calc_result_kb(credit_id))


@router.callback_query(F.data.startswith("early_partial:"))
async def early_partial_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    await state.update_data(early_credit_id=credit_id)
    await state.set_state(EarlyPayment.extra)
    await callback.message.edit_text(
        f"💸 <b>Частичное погашение — {c['name']}</b>\n\n"
        f"Долг: <b>{c['debt']:,.0f} ₽</b>\n\n"
        "Сколько рублей вносишь досрочно?\n"
        "Введи сумму, например: <code>50000</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"early_credit:{credit_id}")
        ]])
    )


@router.message(EarlyPayment.extra)
async def early_partial_result(message: Message, state: FSMContext):
    try:
        extra = float(message.text.replace(" ", "").replace(",", "."))
        if extra < 1:
            await message.answer("Введи сумму больше 0, например: <code>50000</code>",
                                 parse_mode="HTML")
            return

        data      = await state.get_data()
        credit_id = data.get("early_credit_id")
        user      = await db.get_user(message.from_user.id)
        credits   = await db.get_credits(message.from_user.id)
        c = next((x for x in credits if x["id"] == credit_id), None)
        if not c:
            await state.clear()
            return

        override = await _get_month_override(message.from_user.id)
        result = calc_credit_early_payment(credits, user["monthly_budget"], credit_id,
                                           extra_amount=extra, full=False)
        icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
        text = _early_result_text(c, result, icon, full=False, override=override,
                                   budget=user["monthly_budget"])
        await state.clear()
        await message.answer(text, parse_mode="HTML",
                             reply_markup=early_calc_result_kb(credit_id))
    except ValueError:
        await message.answer("Введи число, например: <code>50000</code>", parse_mode="HTML")


def _early_result_text(c: dict, result: dict, icon: str,
                        full: bool, override, budget: float) -> str:
    """Форматирует итоговый текст расчёта досрочного погашения."""
    months_saved   = result["months_saved"]
    interest_saved = result["interest_saved"]

    if full:
        action_line = f"💥 Полное погашение: <b>{result['amount_needed']:,.0f} ₽</b>"
        after_line  = "Долг: <b>0 ₽</b> — кредит закрыт! 🎉"
    else:
        action_line = f"💸 Досрочный взнос: <b>{result['actual_extra']:,.0f} ₽</b>"
        after_line  = f"Остаток долга: <b>{result['new_debt']:,.0f} ₽</b>"

    if months_saved <= 0 and interest_saved < 1:
        verdict = "📌 Досрочный взнос незначительно меняет общий график."
    else:
        verdict = (
            f"⚡ Срок сократится: <b>{result['months_normal']} → {result['months_early']} мес.</b>"
            f"  (−{months_saved} мес.)\n"
            f"💚 Экономия на процентах: <b>{interest_saved:,.0f} ₽</b>"
        )

    budget_label = f"{override:,.0f} ₽ (этот мес.)" if override else f"{budget:,.0f} ₽/мес"
    return (
        f"🚀 <b>Досрочное погашение — {icon} {c['name']}</b>\n\n"
        f"{action_line}\n"
        f"{after_line}\n\n"
        f"📊 Обычный темп (бюджет {budget_label}):\n"
        f"   Срок: <b>{result['months_normal']} мес.</b>  "
        f"Переплата: <b>{result['interest_normal']:,.0f} ₽</b>\n\n"
        f"🚀 После досрочного:\n"
        f"   Срок: <b>{result['months_early']} мес.</b>  "
        f"Переплата: <b>{result['interest_early']:,.0f} ₽</b>\n\n"
        f"{verdict}"
    )


# ─── Поделиться результатом ───────────────────────────────────────────────────

@router.callback_query(F.data == "share_result")
async def share_result(callback: CallbackQuery):
    user    = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    if not credits:
        await callback.answer("Нет данных для публикации", show_alert=True)
        return

    schedule   = recalculate_schedule(credits, user["monthly_budget"])
    total_debt = sum(c["debt"] for c in credits)
    months_left = len(schedule)
    total_paid  = sum(c.get("total_paid", 0) for c in credits)
    original    = total_debt + total_paid
    pct         = total_paid / original * 100 if original > 0 else 0

    share_text = (
        f"💪 Я контролирую свои долги!\n\n"
        f"💸 Долг: {total_debt:,.0f} ₽\n"
        f"✅ Уже выплачено: {total_paid:,.0f} ₽ ({pct:.0f}%)\n"
        f"📅 По плану буду свободен через {months_left} мес.\n\n"
        f"Считаю через @nocreditbot — попробуй и ты!"
    )

    await callback.answer()
    await callback.message.answer(
        f"📤 <b>Твой результат для публикации:</b>\n\n"
        f"<code>{share_text}</code>\n\n"
        f"<i>Скопируй текст и поделись с друзьями!</i>",
        parse_mode="HTML"
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    await db.setup()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    cashflow.bind_db(db)
    dp.include_router(cashflow.router)   # раньше основного: кнопки кешфлоу работают из любого состояния
    dp.include_router(router)
    await start_scheduler(bot, db)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
