import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
            import calendar
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
            f"✅ Минималка: <b>{min_pay:,.0f} ₽</b>\n\n"
            f"Зайди в бот и отметь платёж кнопкой <b>✅ Внести платёж</b>"
        )

        try:
            await bot.send_message(r["user_id"], text, parse_mode="HTML")
            logger.info(f"Напоминание отправлено: user={r['user_id']} credit={credit['name']}")
        except Exception as e:
            logger.error(f"Ошибка напоминания user={r['user_id']}: {e}")


async def start_scheduler(bot: Bot, db):
    # Проверяем каждую минуту
    scheduler.add_job(
        check_and_send_reminders,
        trigger=CronTrigger(minute="*"),
        args=[bot, db],
        id="reminder_check",
        replace_existing=True
    )
    scheduler.start()
    logger.info("Планировщик напоминаний запущен (проверка каждую минуту)")
