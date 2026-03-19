def calc_min_payment(credit: dict, debt: float) -> float:
    """Вычисляет минималку для одного кредита."""
    interest = debt * credit["rate_month"]
    if credit["min_type"] == "fixed":
        return min(credit["min_fixed"], debt + interest)
    else:
        # formula: проценты × multiplier
        return min(interest * credit["min_multiplier"], debt + interest)


def calc_interest(credit: dict, debt: float) -> float:
    return debt * credit["rate_month"] if debt > 0.01 else 0.0


def recalculate_schedule(credits: list, monthly_budget: float) -> list:
    """
    Рассчитывает помесячный график для списка кредитов.
    credits отсортированы по priority (меньше = важнее).
    Каждый месяц:
      1. Начисляем % по каждому кредиту
      2. Платим минималку по всем
      3. Остаток бюджета кидаем на приоритетный (первый с долгом > 0)
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

        budget_left = monthly_budget - total_min

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
            # Определяем приоритетный кредит (первый активный по priority)
            priority_cid = None
            for c in credits:
                if debts.get(c["id"], 0) > 0.01:
                    priority_cid = c["id"]
                    break

            for c in credits:
                cid = c["id"]
                d = credit_data[cid]
                if d["debt"] < 0.01:
                    continue

                if cid == priority_cid:
                    extra = min(budget_left, d["debt"] + d["interest"] - d["min"])
                    pay = d["min"] + extra
                    budget_left -= extra
                else:
                    pay = d["min"]

                pay = min(pay, d["debt"] + d["interest"])
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


def format_status(credits: list, monthly_budget: float, total_paid_all: float) -> str:
    schedule = recalculate_schedule(credits, monthly_budget)
    active = [c for c in credits if c["debt"] > 0.01]
    original_total = sum(c["debt"] for c in credits) + total_paid_all

    if not active:
        return "🏆 <b>Все долги закрыты!</b>"

    total_debt = sum(c["debt"] for c in active)
    paid_off = original_total - total_debt
    pct = paid_off / original_total * 100 if original_total > 0 else 0

    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    lines = [
        f"📊 <b>Общий статус</b>",
        f"[{bar}] {pct:.1f}%",
        f"💸 Уплачено: <b>{total_paid_all:,.0f} ₽</b> | Осталось: <b>{total_debt:,.0f} ₽</b>\n",
    ]

    # По каждому кредиту
    for c in sorted(credits, key=lambda x: x["priority"]):
        if c["debt"] < 0.01:
            lines.append(f"✅ <b>{c['name']}</b> — закрыт")
            continue
        interest = calc_interest(c, c["debt"])
        min_pay = calc_min_payment(c, c["debt"])
        lines.append(
            f"💳 <b>{c['name']}</b> [приоритет {c['priority']}]\n"
            f"   Долг: {c['debt']:,.0f} ₽  |  % в мес: {interest:,.0f} ₽\n"
            f"   Минималка: {min_pay:,.0f} ₽  |  День платежа: {c['payment_day']}-е"
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
        lines.append(f"💰 Бюджет: <b>{monthly_budget:,.0f} ₽/мес</b>")

    return "\n".join(lines)
