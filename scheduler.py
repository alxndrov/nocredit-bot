import calendar
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from cashflow import send_checkin_reminders, send_obligations_onboarding

logger = logging.getLogger(__name__)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


async def check_and_send_reminders(bot: Bot, db):
    """
    Каждую минуту проверяем: есть ли напоминания, которые надо отправить прямо сейчас.
    Логика: если сегодня (payment_day - days_before) и текущее время == (hour:minute).
    """
    now = datetime.now(tz=MOSCOW_TZ)
    today = now.day
    current_hour = now.hour
    current_minute = now.minute

    reminders = await db.get_all_reminders()

    for r in reminders:
        # День, когда надо отправить напоминание
        payment_day = r["payment_day"]
        send_day = payment_day - r["days_before"]

        # Учитываем переход через начало месяца (например, платёж 2-го, напоминание за 3 дня = 29/30/31-е)
        if send_day <= 0:
            # Приблизительно — берём предыдущий месяц
            prev_month = (now.month - 2) % 12 + 1
            prev_year = now.year if now.month > 1 else now.year - 1
            days_in_prev = calendar.monthrange(prev_year, prev_month)[1]
            send_day = days_in_prev + send_day

        if today != send_day:
            continue
        if current_hour != r["hour"] or current_minute != r["minute"]:
            continue

        # Формируем текст напоминания
        credit = await db.get_credit(r["credit_id"])
        if not credit or not credit["is_active"] or credit["debt"] < 0.01:
            continue

        from calculator import calc_interest, calc_min_payment
        interest = calc_interest(credit, credit["debt"])
        min_pay = calc_min_payment(credit, credit["debt"])

        if r["days_before"] == 0:
            prefix = "🔔 <b>Сегодня день платежа!</b>"
        else:
            prefix = f"⏰ <b>До платежа {r['days_before']} дн.</b>"

        text = (
            f"{prefix}\n\n"
            f"💳 <b>{credit['name']}</b>\n"
            f"📅 День платежа: <b>{payment_day}-е</b>\n"
            f"💰 Долг: <b>{credit['debt']:,.0f} ₽</b>\n"
            f"📈 % за месяц: <b>{interest:,.0f} ₽</b>\n"
            f"✅ Минималка: <b>{min_pay:,.0f} ₽</b>"
        )
        pay_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Внести платёж",
                callback_data=f"pay:{credit['id']}"
            )
        ]])

        try:
            await bot.send_message(r["user_id"], text, parse_mode="HTML", reply_markup=pay_kb)
            logger.info(f"Напоминание отправлено: user={r['user_id']} credit={credit['name']}")
        except Exception as e:
            logger.error(f"Ошибка напоминания user={r['user_id']}: {e}")


async def send_payday_notifications(bot: Bot, db):
    """
    Каждый день в 9:00 МСК: если у пользователя сегодня аванс или зарплата —
    отправляем ему список платежей на этот день из плана.
    """
    from calculator import generate_plan

    now = datetime.now(tz=MOSCOW_TZ)
    today_day = now.day
    year = now.year
    month = now.month

    users = await db.get_users_with_payday_today(today_day)

    for u in users:
        if not u.get("payday_notify_enabled", 1):
            continue
        user_id   = u["user_id"]
        payday_num = u["payday_num"]   # 1 = аванс, 2 = зарплата
        budget    = u["monthly_budget"]

        credits = await db.get_credits(user_id)
        if not credits:
            continue

        # Берём план из БД или генерируем
        plan = await db.get_monthly_plan(user_id, year, month)
        if not plan:
            p1_amount = u.get("payday1_amount") or budget
            p2_amount = u.get("payday2_amount")
            override = await db.get_budget_override(user_id, year, month)
            plan = generate_plan(credits, budget, p1_amount, p2_amount,
                                 first_month_budget=override)
            if plan:
                await db.save_monthly_plan(user_id, year, month, plan)

        items = [a for a in plan if a["payday"] == payday_num]
        if not items:
            continue

        cmap = {c["id"]: c for c in credits}
        payday_label = "аванс" if payday_num == 1 else "зарплата"
        total = sum(a["amount"] for a in items)

        lines = [f"💰 <b>Сегодня {payday_label}!</b> Вот что нужно оплатить:\n"]
        for a in items:
            c = cmap.get(a["credit_id"])
            if not c:
                continue
            icon = "💳" if c.get("credit_type") == "card" else "🏦"
            lines.append(f"{icon} <b>{c['name']}</b> — <b>{a['amount']:,.0f} ₽</b>")
        lines.append(f"\n💸 Итого: <b>{total:,.0f} ₽</b>")
        lines.append("\nОткрой бот → ✅ Внести платёж, чтобы отметить оплату.")

        try:
            await bot.send_message(user_id, "\n".join(lines), parse_mode="HTML")
            logger.info(f"Payday notification отправлен: user={user_id} payday={payday_num}")
        except Exception as e:
            logger.error(f"Ошибка payday notification user={user_id}: {e}")


async def send_monthly_summary(bot: Bot, db):
    """
    Ежемесячный итог: запускается ежедневно в 21:00 МСК,
    но отправляет сообщение только в последний день месяца.
    """
    import calendar as _cal
    from calculator import recalculate_schedule

    now = datetime.now(tz=MOSCOW_TZ)
    last_day = _cal.monthrange(now.year, now.month)[1]
    if now.day != last_day:
        return

    months_ru = ["январе", "феврале", "марте", "апреле", "мае", "июне",
                 "июле", "августе", "сентябре", "октябре", "ноябре", "декабре"]
    month_name = months_ru[now.month - 1]

    users = await db.get_all_users()

    for u in users:
        user_id = u["user_id"]
        credits = await db.get_credits(user_id)
        if not credits:
            continue

        payments = await db.get_payments_for_month(user_id, now.year, now.month)
        if not payments:
            continue

        total_paid     = sum(p["amount"]   for p in payments)
        total_interest = sum(p["interest"] for p in payments)
        principal_paid = total_paid - total_interest

        schedule   = recalculate_schedule(credits, u["monthly_budget"],
                                          first_month_budget=await db.get_budget_override(
                                              user_id, now.year, now.month))
        months_left = len(schedule)
        total_debt  = sum(c["debt"] for c in credits)

        text = (
            f"📊 <b>Итоги за {month_name}!</b>\n\n"
            f"💰 Внесено платежей: <b>{total_paid:,.0f} ₽</b>\n"
            f"   из них % банку: <b>{total_interest:,.0f} ₽</b>\n"
            f"   тело долга: <b>{principal_paid:,.0f} ₽</b>\n\n"
            f"📉 Текущий долг: <b>{total_debt:,.0f} ₽</b>\n"
            f"📅 До свободы осталось: <b>{months_left} мес.</b>\n\n"
            f"💪 Так держать! Каждый платёж приближает тебя к финансовой свободе."
        )
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
            logger.info(f"Monthly summary отправлен: user={user_id}")
        except Exception as e:
            logger.error(f"Ошибка monthly summary user={user_id}: {e}")


async def start_scheduler(bot: Bot, db):
    scheduler.add_job(
        check_and_send_reminders,
        trigger=CronTrigger(minute="*"),
        args=[bot, db],
        id="reminder_check",
        replace_existing=True
    )
    scheduler.add_job(
        send_payday_notifications,
        trigger=CronTrigger(hour=9, minute=0),
        args=[bot, db],
        id="payday_notify",
        replace_existing=True
    )
    scheduler.add_job(
        send_monthly_summary,
        trigger=CronTrigger(hour=21, minute=0),
        args=[bot, db],
        id="monthly_summary",
        replace_existing=True
    )
    scheduler.add_job(
        send_checkin_reminders,
        trigger=CronTrigger(hour=10, minute=0),
        args=[bot],
        id="checkin_reminder",
        replace_existing=True
    )
    # Разовый вопрос про обязательные расходы — вскоре после запуска
    scheduler.add_job(
        send_obligations_onboarding,
        trigger=DateTrigger(run_date=datetime.now(tz=MOSCOW_TZ) + timedelta(seconds=15)),
        args=[bot],
        id="obligations_onboarding",
        replace_existing=True
    )
    scheduler.start()
    logger.info("Планировщик запущен (напоминания каждую минуту, зарплатные уведомления в 9:00, ежемесячный итог в 21:00, проверка кешфлоу в 10:00)")
