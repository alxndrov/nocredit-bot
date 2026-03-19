import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database import Database
from scheduler import start_scheduler
from states import AddCredit, AddReminder, ChangeBudget, SetPriority
from keyboards import (
    main_menu, credits_list_kb, credit_detail_kb, confirm_pay_kb,
    reminders_kb, confirm_delete_kb
)
from calculator import recalculate_schedule, format_status, calc_interest, calc_min_payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
db = Database()


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    await db.init_user(message.from_user.id)
    credits = await db.get_credits(message.from_user.id)
    if not credits:
        text = (
            "👋 Привет! Я помогу отслеживать погашение кредитов.\n\n"
            "У тебя пока нет кредитов. Нажми <b>💳 Кредиты → ➕ Добавить</b> чтобы начать."
        )
    else:
        text = "👋 С возвращением! Выбери действие:"
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


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
    text = format_status(active, user["monthly_budget"], total_paid)
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu())


# ─── Кредиты ──────────────────────────────────────────────────────────────────

@router.message(F.text == "💳 Кредиты")
async def credits_menu(message: Message):
    credits = await db.get_credits(message.from_user.id)
    buttons = []
    for c in credits:
        buttons.append([InlineKeyboardButton(
            text=f"💳 {c['name']} — {c['debt']:,.0f} ₽  [приор. {c['priority']}]",
            callback_data=f"credit_detail:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить кредит", callback_data="add_credit")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("💳 <b>Твои кредиты:</b>", parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "credits_menu")
async def credits_menu_cb(callback: CallbackQuery):
    credits = await db.get_credits(callback.from_user.id)
    buttons = []
    for c in credits:
        buttons.append([InlineKeyboardButton(
            text=f"💳 {c['name']} — {c['debt']:,.0f} ₽  [приор. {c['priority']}]",
            callback_data=f"credit_detail:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить кредит", callback_data="add_credit")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("💳 <b>Твои кредиты:</b>", parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("credit_detail:"))
async def credit_detail(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    if not c:
        await callback.answer("Кредит не найден")
        return

    interest = calc_interest(c, c["debt"])
    min_pay = calc_min_payment(c, c["debt"])
    min_desc = (
        f"фикс. {c['min_fixed']:,.0f} ₽" if c["min_type"] == "fixed"
        else f"% × {c['min_multiplier']:.1f} = {min_pay:,.0f} ₽"
    )

    text = (
        f"💳 <b>{c['name']}</b>\n\n"
        f"📉 Долг: <b>{c['debt']:,.0f} ₽</b>\n"
        f"📈 Ставка: <b>{c['rate_month']*100:.2f}%/мес</b>\n"
        f"💰 % за этот месяц: <b>{interest:,.0f} ₽</b>\n"
        f"✅ Минималка: <b>{min_desc}</b>\n"
        f"📅 День платежа: <b>{c['payment_day']}-е</b>\n"
        f"🔢 Приоритет: <b>{c['priority']}</b>\n"
        f"💸 Уплачено всего: <b>{c.get('total_paid', 0):,.0f} ₽</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=credit_detail_kb(credit_id))


# ─── Добавить кредит (FSM) ────────────────────────────────────────────────────

@router.callback_query(F.data == "add_credit")
async def add_credit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ <b>Добавление кредита</b>\n\nШаг 1/6: Введи название банка или карты\n"
        "Например: <code>Тинькофф</code>, <code>ОТП</code>, <code>Сбер</code>",
        parse_mode="HTML"
    )
    await state.set_state(AddCredit.name)


@router.message(AddCredit.name)
async def add_credit_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer(
        "Шаг 2/6: Текущий долг (остаток по кредиту)\nПример: <code>350000</code>",
        parse_mode="HTML"
    )
    await state.set_state(AddCredit.debt)


@router.message(AddCredit.debt)
async def add_credit_debt(message: Message, state: FSMContext):
    try:
        debt = float(message.text.replace(" ", "").replace(",", ""))
        if debt <= 0:
            raise ValueError
        await state.update_data(debt=debt)
        await message.answer(
            "Шаг 3/6: Процентная ставка <b>в месяц</b> (%)\n"
            "Например: <code>4.6</code> или <code>3.33</code>",
            parse_mode="HTML"
        )
        await state.set_state(AddCredit.rate)
    except ValueError:
        await message.answer("Введи число, например: <code>200000</code>", parse_mode="HTML")


@router.message(AddCredit.rate)
async def add_credit_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", ".").replace("%", "").strip())
        if rate <= 0 or rate > 30:
            raise ValueError
        await state.update_data(rate=rate / 100)
        await message.answer(
            "Шаг 4/6: День платежа по договору (число месяца)\n"
            "Например: <code>20</code> или <code>25</code>",
            parse_mode="HTML"
        )
        await state.set_state(AddCredit.payment_day)
    except ValueError:
        await message.answer("Введи число от 0.1 до 30, например: <code>4.6</code>", parse_mode="HTML")


@router.message(AddCredit.payment_day)
async def add_credit_day(message: Message, state: FSMContext):
    try:
        day = int(message.text.strip())
        if not 1 <= day <= 31:
            raise ValueError
        await state.update_data(payment_day=day)
        await message.answer(
            "Шаг 5/6: Как считать минималку?\n\n"
            "<b>1</b> — Формула: проценты × множитель (например ×2)\n"
            "<b>2</b> — Фиксированная сумма\n\n"
            "Введи <code>1</code> или <code>2</code>:",
            parse_mode="HTML"
        )
        await state.set_state(AddCredit.min_type)
    except ValueError:
        await message.answer("Введи число от 1 до 31")


@router.message(AddCredit.min_type)
async def add_credit_min_type(message: Message, state: FSMContext):
    choice = message.text.strip()
    if choice == "1":
        await state.update_data(min_type="formula")
        await message.answer(
            "Введи множитель для процентов\n"
            "Например: <code>2</code> (минималка = проценты × 2)",
            parse_mode="HTML"
        )
    elif choice == "2":
        await state.update_data(min_type="fixed")
        await message.answer(
            "Введи фиксированную сумму минималки в ₽\n"
            "Например: <code>5000</code>",
            parse_mode="HTML"
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
        await message.answer(
            "Шаг 6/6: Приоритет погашения\n\n"
            "<b>1</b> = гасим первым, <b>2</b> = вторым, и т.д.\n"
            "Остаток бюджета после минималок кидается на кредит с наименьшим приоритетом.\n\n"
            "Введи число:",
            parse_mode="HTML"
        )
        await state.set_state(AddCredit.priority)
    except ValueError:
        await message.answer("Введи число, например: <code>2</code>", parse_mode="HTML")


@router.message(AddCredit.priority)
async def add_credit_priority(message: Message, state: FSMContext):
    try:
        priority = int(message.text.strip())
        if priority < 1:
            raise ValueError
        data = await state.get_data()

        min_type = data["min_type"]
        min_multiplier = data["min_value"] if min_type == "formula" else 2.0
        min_fixed = data["min_value"] if min_type == "fixed" else None

        credit_id = await db.add_credit(
            user_id=message.from_user.id,
            name=data["name"],
            debt=data["debt"],
            rate_month=data["rate"],
            payment_day=data["payment_day"],
            min_type=min_type,
            min_multiplier=min_multiplier,
            min_fixed=min_fixed,
            priority=priority
        )

        await state.clear()

        min_desc = (
            f"фикс. {min_fixed:,.0f} ₽" if min_type == "fixed"
            else f"% × {min_multiplier:.1f}"
        )
        await message.answer(
            f"✅ <b>Кредит добавлен!</b>\n\n"
            f"💳 {data['name']}\n"
            f"💰 Долг: {data['debt']:,.0f} ₽\n"
            f"📈 Ставка: {data['rate']*100:.2f}%/мес\n"
            f"📅 День платежа: {data['payment_day']}-е\n"
            f"✅ Минималка: {min_desc}\n"
            f"🔢 Приоритет: {priority}\n\n"
            f"Теперь настрой напоминания через 💳 Кредиты → {data['name']} → 🔔 Напоминания",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except ValueError:
        await message.answer("Введи целое число, например: <code>1</code>", parse_mode="HTML")


# ─── Изменить приоритет ───────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("set_priority:"))
async def set_priority_start(callback: CallbackQuery, state: FSMContext):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
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
    await callback.message.edit_text(
        f"🗑 Удалить кредит <b>{c['name']}</b>?\nВсе напоминания тоже удалятся.",
        parse_mode="HTML",
        reply_markup=confirm_delete_kb(credit_id)
    )


@router.callback_query(F.data.startswith("confirm_del:"))
async def del_credit_apply(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
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
        parse_mode="HTML"
    )
    await state.set_state(AddReminder.days_before)


@router.message(AddReminder.days_before)
async def add_reminder_days(message: Message, state: FSMContext):
    try:
        days = int(message.text.strip())
        if days < 0 or days > 30:
            raise ValueError
        await state.update_data(days_before=days)
        await message.answer(
            "Шаг 2/2: В какое время отправить? (по МСК)\n"
            "Формат: <code>ЧЧ:ММ</code>, например <code>10:00</code> или <code>09:30</code>",
            parse_mode="HTML"
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
    interest = calc_interest(c, c["debt"])
    min_pay = calc_min_payment(c, c["debt"])

    # Получаем бюджет и считаем рекомендуемый платёж
    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    schedule = recalculate_schedule(credits, user["monthly_budget"])

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


@router.callback_query(F.data.startswith("confirm_pay:"))
async def confirm_pay(callback: CallbackQuery):
    credit_id = int(callback.data.split(":")[1])
    c = await db.get_credit(credit_id)
    user = await db.get_user(callback.from_user.id)
    credits = await db.get_credits(callback.from_user.id)
    schedule = recalculate_schedule(credits, user["monthly_budget"])

    interest = calc_interest(c, c["debt"])
    min_pay = calc_min_payment(c, c["debt"])
    payment = min_pay

    if schedule:
        for cr in schedule[0]["credits"]:
            if cr["credit_id"] == credit_id:
                payment = cr["payment"]
                break

    new_debt = max(0.0, c["debt"] + interest - payment)
    await db.update_credit_debt(credit_id, new_debt, payment)
    await db.record_payment(credit_id, callback.from_user.id, payment, interest, new_debt)

    lines = [f"✅ <b>Платёж записан!</b>\n",
             f"💳 <b>{c['name']}</b>",
             f"💸 Внесено: <b>{payment:,.0f} ₽</b> (из них % = {interest:,.0f} ₽)"]

    if new_debt < 0.01:
        lines.append(f"\n🎉 <b>{c['name']} полностью закрыт!</b>")
    else:
        lines.append(f"📉 Остаток: <b>{new_debt:,.0f} ₽</b>")
        # Следующий платёж
        updated = await db.get_credits(callback.from_user.id)
        new_schedule = recalculate_schedule(updated, user["monthly_budget"])
        if new_schedule:
            for cr in new_schedule[0]["credits"]:
                if cr["credit_id"] == credit_id:
                    lines.append(f"📅 Следующий платёж: <b>{cr['payment']:,.0f} ₽</b>")
                    break

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")
    await callback.message.answer("Главное меню:", reply_markup=main_menu())


# ─── История ──────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 История")
async def history_menu(message: Message):
    credits = await db.get_credits(message.from_user.id, active_only=False)
    if not credits:
        await message.answer("Платежей пока нет.", reply_markup=main_menu())
        return
    buttons = [[InlineKeyboardButton(
        text=f"{'✅' if not c['is_active'] else '💳'} {c['name']}",
        callback_data=f"history:{c['id']}"
    )] for c in credits]
    buttons.append([InlineKeyboardButton(text="📋 Все платежи", callback_data="history:all")])
    await message.answer("📋 История по какому кредиту?",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("history:"))
async def show_history(callback: CallbackQuery):
    val = callback.data.split(":")[1]
    if val == "all":
        history = await db.get_history(callback.from_user.id, limit=10)
    else:
        history = await db.get_history(callback.from_user.id, credit_id=int(val), limit=10)

    if not history:
        await callback.message.edit_text("Платежей пока нет.")
        return

    lines = ["📋 <b>История платежей:</b>\n"]
    for h in history:
        lines.append(
            f"{h['date']}  <b>{h['credit_name']}</b>\n"
            f"  Внесено: {h['amount']:,.0f} ₽  |  % = {h['interest']:,.0f} ₽  |  Остаток: {h['debt_after']:,.0f} ₽"
        )
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML")


# ─── Бюджет ───────────────────────────────────────────────────────────────────

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
        budget = float(message.text.replace(" ", "").replace(",", ""))
        if budget < 1000:
            raise ValueError
        await db.update_budget(message.from_user.id, budget)
        credits = await db.get_credits(message.from_user.id)
        schedule = recalculate_schedule(credits, budget)
        await state.clear()
        await message.answer(
            f"✅ Бюджет обновлён: <b>{budget:,.0f} ₽/мес</b>\n"
            f"📅 По новому графику: <b>{len(schedule)} мес.</b>",
            parse_mode="HTML",
            reply_markup=main_menu()
        )
    except ValueError:
        await message.answer("Введи число, например: <code>90000</code>", parse_mode="HTML")


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    await db.setup()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await start_scheduler(bot, db)
    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
