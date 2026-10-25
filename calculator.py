def calc_min_payment(credit: dict, debt: float) -> float:
    """Вычисляет минималку для одного кредита."""
    interest = debt * credit["rate_month"]
    if credit["min_type"] == "fixed":
        return min(credit["min_fixed"], debt + interest)
    elif credit["min_type"] == "percent":
        # X% от остатка долга (типично для кредитных карт)
        return min(debt * credit["min_multiplier"] / 100, debt + interest)
    else:
        # formula: проценты × multiplier (для кредитов с аннуитетом)
        return min(interest * credit["min_multiplier"], debt + interest)


def calc_interest(credit: dict, debt: float) -> float:
    return debt * credit["rate_month"] if debt > 0.01 else 0.0


def calc_min_only_schedule(credits: list, max_months: int = 120) -> list:
    """
    Симуляция: каждый месяц платим только минималки, без доп. взносов.
    Возвращает список месяцев (аналогично recalculate_schedule).
    Если за max_months долг не закрыт — обрезаем (is_capped можно проверить по len == max_months).
    """
    debts = {c["id"]: float(c["debt"]) for c in credits}
    months = []
    for month_num in range(1, max_months + 1):
        month_credits = []
        active = False
        for c in credits:
            cid = c["id"]
            debt = debts.get(cid, 0.0)
            if debt < 0.01:
                continue
            active = True
            interest = calc_interest(c, debt)
            min_pay = calc_min_payment(c, debt)
            pay = min(min_pay, debt + interest)
            new_debt = max(0.0, debt + interest - pay)
            month_credits.append({
                "credit_id": cid,
                "name": c["name"],
                "debt_before": debt,
                "interest": interest,
                "payment": pay,
                "debt_after": new_debt,
            })
            debts[cid] = new_debt
        if not active:
            break
        months.append({"month": month_num, "credits": month_credits})
    return months


def recalculate_schedule(credits: list, monthly_budget: float,
                         first_month_budget: float = None) -> list:
    """
    Рассчитывает помесячный график для списка кредитов.
    credits отсортированы по priority (меньше = важнее).
    Каждый месяц:
      1. Начисляем % по каждому кредиту
      2. Платим минималку по всем
      3. Остаток бюджета кидаем на приоритетный (первый с долгом > 0)
    first_month_budget — переопределение бюджета на 1-й (текущий) месяц;
    все следующие месяцы используют monthly_budget.
    Возвращает список месяцев, каждый — список платежей по кредитам.
    """
    # Копируем долги чтобы не мутировать оригинал
    debts = {c["id"]: float(c["debt"]) for c in credits}
    active_ids = [c["id"] for c in credits if c["debt"] > 0.01]

    if not active_ids:
        return []

    months = []

    for month_num in range(1, 121):
        month_credits = []
        total_min = 0.0
        credit_data = {}

        for c in credits:
            cid = c["id"]
            debt = debts.get(cid, 0.0)
            if debt < 0.01:
                credit_data[cid] = {"interest": 0, "min": 0, "debt": 0}
                continue
            interest = calc_interest(c, debt)
            min_pay = calc_min_payment(c, debt)
            total_min += min_pay
            credit_data[cid] = {"interest": interest, "min": min_pay, "debt": debt}

        budget_this_month = (
            first_month_budget if (month_num == 1 and first_month_budget is not None)
            else monthly_budget
        )
        budget_left = budget_this_month - total_min

        # Если бюджет не покрывает даже минималки — распределяем пропорционально
        if budget_left < 0:
            ratio = monthly_budget / total_min if total_min > 0 else 0
            for c in credits:
                cid = c["id"]
                d = credit_data[cid]
                if d["debt"] < 0.01:
                    continue
                pay = d["min"] * ratio
                new_debt = max(0.0, d["debt"] + d["interest"] - pay)
                month_credits.append({
                    "credit_id": cid,
                    "name": c["name"],
                    "debt_before": d["debt"],
                    "interest": d["interest"],
                    "payment": pay,
                    "debt_after": new_debt,
                })
                debts[cid] = new_debt
        else:
            # Сначала платим минималки, остаток каскадно распределяем по приоритету
            payments = {c["id"]: credit_data[c["id"]]["min"] for c in credits
                        if credit_data[c["id"]]["debt"] >= 0.01}

            # Каскад: идём по приоритету и добавляем extra пока есть бюджет
            for c in credits:
                if budget_left <= 0.01:
                    break
                cid = c["id"]
                d = credit_data[cid]
                if d["debt"] < 0.01:
                    continue
                max_extra = d["debt"] + d["interest"] - d["min"]
                extra = min(budget_left, max_extra)
                payments[cid] = payments.get(cid, 0) + extra
                budget_left -= extra

            for c in credits:
                cid = c["id"]
                d = credit_data[cid]
                if d["debt"] < 0.01:
                    continue
                pay = min(payments.get(cid, d["min"]), d["debt"] + d["interest"])
                new_debt = max(0.0, d["debt"] + d["interest"] - pay)
                month_credits.append({
                    "credit_id": cid,
                    "name": c["name"],
                    "debt_before": d["debt"],
                    "interest": d["interest"],
                    "payment": pay,
                    "debt_after": new_debt,
                })
                debts[cid] = new_debt

        months.append({"month": month_num, "credits": month_credits})

        # Проверяем закрыты ли все
        if all(debts.get(c["id"], 0) < 0.01 for c in credits):
            break

    return months


def generate_plan(credits: list, budget: float, p1_amount: float, p2_amount,
                  first_month_budget: float = None) -> list:
    """
    Умное распределение кредитов по выплатам.
    Бюджет делится пропорционально суммам выплат (аванс/зарплата).
    Если один кредит не влезает целиком — он разбивается на две части.
    first_month_budget — переопределение бюджета на текущий месяц.
    """
    effective_budget = first_month_budget if first_month_budget is not None else budget
    schedule = recalculate_schedule(credits, budget, first_month_budget=first_month_budget)
    recommended = {}
    if schedule:
        for cr in schedule[0]["credits"]:
            recommended[cr["credit_id"]] = cr["payment"]
    for c in credits:
        if c["id"] not in recommended:
            recommended[c["id"]] = calc_min_payment(c, c["debt"])

    if p2_amount is None:
        return [{"credit_id": c["id"], "payday": 1, "amount": recommended.get(c["id"], 0)}
                for c in credits if recommended.get(c["id"], 0) > 0.01]

    total_income = p1_amount + p2_amount
    p1_credit_budget = round(effective_budget * p1_amount / total_income, 2)

    result = []
    p1_left = p1_credit_budget
    p2_left = effective_budget - p1_credit_budget

    for c in credits:
        amt = recommended.get(c["id"], 0)
        if amt < 0.01:
            continue
        if p1_left >= amt - 0.01:
            result.append({"credit_id": c["id"], "payday": 1, "amount": round(amt, 2)})
            p1_left -= amt
        elif p1_left > 0.01:
            part1 = round(p1_left, 2)
            part2 = round(amt - part1, 2)
            result.append({"credit_id": c["id"], "payday": 1, "amount": part1})
            p1_left = 0.0
            if part2 > 0.01:
                result.append({"credit_id": c["id"], "payday": 2, "amount": part2})
                p2_left -= part2
        else:
            result.append({"credit_id": c["id"], "payday": 2, "amount": round(amt, 2)})
            p2_left -= amt

    return result


def _next_payment_date(payment_day: int) -> str:
    from datetime import date, timedelta
    import calendar as _cal
    today = date.today()
    # Ближайшая дата платежа
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    pay_day = min(payment_day, days_in_month)
    next_dt = today.replace(day=pay_day)
    if next_dt < today:
        # Переходим на следующий месяц
        if today.month == 12:
            next_dt = next_dt.replace(year=today.year + 1, month=1)
        else:
            next_dt = next_dt.replace(month=today.month + 1)
    days_left = (next_dt - today).days
    months_ru = ["янв", "фев", "мар", "апр", "май", "июн",
                 "июл", "авг", "сен", "окт", "ноя", "дек"]
    date_str = f"{next_dt.day} {months_ru[next_dt.month - 1]}"
    if days_left == 0:
        return f"🔴 {date_str} (сегодня!)"
    elif days_left <= 3:
        return f"🟠 {date_str} (через {days_left} дн.)"
    elif days_left <= 7:
        return f"🟡 {date_str} (через {days_left} дн.)"
    else:
        return f"🟢 {date_str}"


def calc_credit_early_payment(credits: list, budget: float,
                               credit_id: int, extra_amount: float,
                               full: bool = False) -> dict:
    """
    Считает экономию от досрочного взноса extra_amount в конкретный кредит.
    full=True — полное погашение (долг → 0).
    Возвращает тот же формат, что и calc_early_payment_savings, плюс:
      amount_needed (для full), new_debt (для partial).
    """
    target = next((c for c in credits if c["id"] == credit_id), None)
    if not target:
        return {}

    interest = calc_interest(target, target["debt"])
    amount_needed = target["debt"] + interest  # сколько нужно чтобы закрыть

    # Модифицируем список кредитов: меняем долг целевого
    if full:
        new_debt = 0.0
        actual_extra = amount_needed
    else:
        new_debt = max(0.0, target["debt"] + interest - extra_amount)
        actual_extra = extra_amount

    modified = []
    for c in credits:
        if c["id"] == credit_id:
            modified.append({**c, "debt": new_debt})
        else:
            modified.append(c)

    schedule_normal = recalculate_schedule(credits, budget)
    schedule_early  = recalculate_schedule(modified, budget)

    months_normal   = len(schedule_normal)
    months_early    = len(schedule_early)
    interest_normal = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_normal)
    interest_early  = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_early)

    return {
        "months_normal":   months_normal,
        "months_early":    months_early,
        "months_saved":    months_normal - months_early,
        "interest_normal": interest_normal,
        "interest_early":  interest_early,
        "interest_saved":  interest_normal - interest_early,
        "amount_needed":   amount_needed,
        "new_debt":        new_debt,
        "actual_extra":    actual_extra,
    }


def calc_early_payment_savings(credits: list, budget: float, extra_amount: float) -> dict:
    """
    Считает экономию при единоразовом досрочном взносе extra_amount ₽.
    Предполагает, что взнос идёт к обычному бюджету в первый месяц.
    """
    schedule_normal = recalculate_schedule(credits, budget)
    schedule_early  = recalculate_schedule(credits, budget + extra_amount)

    months_normal   = len(schedule_normal)
    months_early    = len(schedule_early)
    interest_normal = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_normal)
    interest_early  = sum(sum(cr["interest"] for cr in m["credits"]) for m in schedule_early)

    return {
        "months_normal":   months_normal,
        "months_early":    months_early,
        "months_saved":    months_normal - months_early,
        "interest_normal": interest_normal,
        "interest_early":  interest_early,
        "interest_saved":  interest_normal - interest_early,
    }


def format_status(credits: list, monthly_budget: float, total_paid_all: float,
                  first_month_budget: float = None) -> str:
    schedule = recalculate_schedule(credits, monthly_budget, first_month_budget=first_month_budget)
    active = [c for c in credits if c["debt"] > 0.01]
    original_total = sum(c["debt"] for c in credits) + total_paid_all

    if not active:
        return (
            "🏆 <b>Все долги закрыты!</b>\n\n"
            "Ты освободился от долговой нагрузки! 🎉\n"
            f"💸 Всего выплачено: <b>{total_paid_all:,.0f} ₽</b>"
        )

    total_debt = sum(c["debt"] for c in active)
    paid_off = original_total - total_debt
    pct = paid_off / original_total * 100 if original_total > 0 else 0

    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = "▓" * filled + "░" * (bar_len - filled)

    # Мотивирующая фраза по прогрессу
    if pct < 10:
        mood = "💪 Только начинаем — главное не останавливаться!"
    elif pct < 30:
        mood = "🔥 Хороший старт, продолжай в том же духе!"
    elif pct < 50:
        mood = "🚀 Почти треть пути позади — так держать!"
    elif pct < 75:
        mood = "⚡ Больше половины уже позади — финишная прямая близко!"
    elif pct < 90:
        mood = "🌟 Почти у цели — ещё немного!"
    else:
        mood = "🏁 Финишная прямая — осталось чуть-чуть!"

    lines = [
        f"🚀 <b>Прогресс к финансовой свободе</b>",
        f"[{bar}] {pct:.1f}%",
        f"💸 Уплачено: <b>{total_paid_all:,.0f} ₽</b>  |  Осталось: <b>{total_debt:,.0f} ₽</b>",
        f"<i>{mood}</i>\n",
    ]

    # По каждому кредиту
    for c in sorted(credits, key=lambda x: x["priority"]):
        if c["debt"] < 0.01:
            lines.append(f"✅ <b>{c['name']}</b> — закрыт")
            continue
        interest = calc_interest(c, c["debt"])
        min_pay = calc_min_payment(c, c["debt"])
        icon = "💳" if c.get("credit_type") == "card" else "🏦"
        next_pay = _next_payment_date(c["payment_day"])
        lines.append(
            f"{icon} <b>{c['name']}</b> [приоритет {c['priority']}]\n"
            f"   Долг: {c['debt']:,.0f} ₽  |  % в мес: {interest:,.0f} ₽\n"
            f"   Минималка: {min_pay:,.0f} ₽\n"
            f"   📅 Ближайший платёж: {next_pay}"
        )

    if schedule:
        total_interest = sum(
            sum(cr["interest"] for cr in m["credits"])
            for m in schedule
        )
        lines.append(
            f"\n📅 Закроешь всё за <b>{len(schedule)} мес.</b> "
            f"({len(schedule)//12}г {len(schedule)%12}м)"
        )
        lines.append(f"📈 Переплата впереди: <b>{total_interest:,.0f} ₽</b>")
        if first_month_budget is not None:
            lines.append(
                f"🗓 Бюджет на этот месяц: <b>{first_month_budget:,.0f} ₽</b>  "
                f"<i>(стандарт: {monthly_budget:,.0f} ₽/мес)</i>"
            )
        else:
            lines.append(f"💰 Бюджет: <b>{monthly_budget:,.0f} ₽/мес</b>")

    return "\n".join(lines)
