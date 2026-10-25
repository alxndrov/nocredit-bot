from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"),           KeyboardButton(text="💸 Кешфлоу")],
            [KeyboardButton(text="✅ Внести платёж"),    KeyboardButton(text="💳 Кредиты и карты")],
            [KeyboardButton(text="📅 Календарь"),        KeyboardButton(text="📋 История")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True
    )


# ── Синхронизация остатков ────────────────────────────────────────────────────

def sync_balance_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Остаток верный", callback_data=f"sync_ok:{credit_id}")],
        [InlineKeyboardButton(text="⏩ Пропустить",     callback_data=f"sync_skip:{credit_id}")],
        [InlineKeyboardButton(text="❌ Завершить",      callback_data="sync_done")],
    ])


# ── Кредиты и карты ───────────────────────────────────────────────────────────

def credits_section_kb(credits: list) -> InlineKeyboardMarkup:
    """Раздел «Кредиты и карты»: список + добавить + стратегии."""
    buttons = []
    for c in credits:
        if c['debt'] < 0.01:
            icon = "✅"
        elif c.get('credit_type') == 'card':
            icon = "💳"
        else:
            icon = "🏦"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {c['name']} — {c['debt']:,.0f} ₽",
            callback_data=f"credit_detail:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Добавить кредит / карту", callback_data="add_credit")])
    buttons.append([InlineKeyboardButton(text="🎯 Стратегии", callback_data="show_strategies")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Календарь ─────────────────────────────────────────────────────────────────

def calendar_kb() -> InlineKeyboardMarkup:
    """Раздел «Календарь»: план + график + досрочное."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 План на месяц",       callback_data="show_plan")],
        [InlineKeyboardButton(text="📊 График погашения",    callback_data="show_schedule:0:0")],
        [InlineKeyboardButton(text="🚀 Досрочное погашение", callback_data="early_calc")],
    ])


# ── Настройки ─────────────────────────────────────────────────────────────────

def settings_kb(month_override: float = None) -> InlineKeyboardMarkup:
    """Раздел «Настройки»."""
    month_btn_text = (
        f"🗓 Бюджет этого месяца: {month_override:,.0f} ₽  ✏️"
        if month_override is not None
        else "🗓 Бюджет на этот месяц"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить бюджет (по умолч.)", callback_data="settings_budget")],
        [InlineKeyboardButton(text=month_btn_text,                   callback_data="settings_month_budget")],
        [InlineKeyboardButton(text="📆 Даты аванса / зарплаты",      callback_data="settings_paydays")],
        [InlineKeyboardButton(text="✏️ Обновить остатки",            callback_data="settings_sync")],
        [InlineKeyboardButton(text="🔔 Напоминания",                 callback_data="settings_reminders")],
    ])


def month_budget_actions_kb(has_override: bool) -> InlineKeyboardMarkup:
    buttons = []
    if has_override:
        buttons.append([InlineKeyboardButton(text="🗑 Сбросить (вернуть стандартный)",
                                             callback_data="clear_month_budget")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Напоминания ───────────────────────────────────────────────────────────────

def reminders_menu_kb(credits: list) -> InlineKeyboardMarkup:
    """Главное меню напоминаний: общие + по каждому кредиту."""
    buttons = [
        [InlineKeyboardButton(text="📆 Общие уведомления", callback_data="general_reminders")],
    ]
    for c in credits:
        icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {c['name']}",
            callback_data=f"remind_list:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def general_reminders_kb(payday_notify: bool, has_p2: bool) -> InlineKeyboardMarkup:
    """Управление общими уведомлениями (аванс/зарплата)."""
    p1_icon = "✅" if payday_notify else "❌"
    p1_text = f"{p1_icon} Уведомление в день аванса/зарплаты"
    buttons = [
        [InlineKeyboardButton(text=p1_text, callback_data="toggle_payday_notify")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings_reminders")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def reminders_kb(credit_id: int, reminders: list) -> InlineKeyboardMarkup:
    buttons = []
    for r in reminders:
        days = r['days_before']
        days_label = "В день" if days == 0 else f"За {days} дн."
        label = f"{days_label} в {r['hour']:02d}:{r['minute']:02d}"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {label}",
            callback_data=f"del_reminder:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton(
        text="➕ Добавить напоминание",
        callback_data=f"add_reminder:{credit_id}"
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="settings_reminders")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Кредит (детали) ───────────────────────────────────────────────────────────

def credits_list_kb(credits: list, action: str) -> InlineKeyboardMarkup:
    buttons = []
    for c in credits:
        if c['debt'] < 0.01:
            icon = "✅"
        elif c.get('credit_type') == 'card':
            icon = "💳"
        else:
            icon = "🏦"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {c['name']} — {c['debt']:,.0f} ₽",
            callback_data=f"{action}:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def credit_detail_kb(credit_id: int, credit_type: str = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✅ Внести платёж",  callback_data=f"pay:{credit_id}")],
    ]
    if credit_type == "card":
        rows.append([InlineKeyboardButton(text="💸 Внести расход с карты", callback_data=f"card_expense:{credit_id}")])
    rows += [
        [InlineKeyboardButton(text="✏️ Редактировать",  callback_data=f"edit_menu:{credit_id}")],
        [InlineKeyboardButton(text="🔔 Напоминания",    callback_data=f"remind_list:{credit_id}")],
        [InlineKeyboardButton(text="🗑 Удалить кредит", callback_data=f"del_credit:{credit_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [
        [InlineKeyboardButton(text="◀️ Назад", callback_data="credits_menu")],
    ])


def edit_credit_kb(credit_id: int, is_card: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="📝 Название",      callback_data=f"edit:name:{credit_id}")],
        [InlineKeyboardButton(text="💰 Остаток долга", callback_data=f"edit:debt:{credit_id}")],
        [InlineKeyboardButton(text="📈 Ставка",        callback_data=f"edit:rate:{credit_id}")],
        [InlineKeyboardButton(text="📅 День платежа",  callback_data=f"edit:payment_day:{credit_id}")],
        [InlineKeyboardButton(text="✅ Минималка",     callback_data=f"edit:min:{credit_id}")],
        [InlineKeyboardButton(text="🔢 Приоритет",     callback_data=f"edit:priority:{credit_id}")],
    ]
    if is_card:
        buttons.insert(4, [InlineKeyboardButton(
            text="📋 День выписки", callback_data=f"edit:statement_day:{credit_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"credit_detail:{credit_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_pay_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить",        callback_data=f"confirm_pay:{credit_id}")],
        [InlineKeyboardButton(text="✍️ Внёс другую сумму", callback_data=f"pay_custom:{credit_id}")],
        [InlineKeyboardButton(text="❌ Отмена",             callback_data=f"credit_detail:{credit_id}")],
    ])


def debt_check_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, всё верно",        callback_data=f"debt_ok:{credit_id}")],
        [InlineKeyboardButton(text="✍️ Нет, другой остаток", callback_data=f"debt_wrong:{credit_id}")],
        [InlineKeyboardButton(text="◀️ Назад",                callback_data=f"credit_detail:{credit_id}")],
    ])


def confirm_delete_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"confirm_del:{credit_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"credit_detail:{credit_id}"),
        ]
    ])


# ── Онбординг / стратегии ─────────────────────────────────────────────────────

def wow_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👉 Выбрать стратегию →", callback_data="ob_wow_done")]
    ])


def early_credits_kb(credits: list) -> InlineKeyboardMarkup:
    """Список кредитов для выбора досрочного погашения."""
    buttons = []
    for c in credits:
        icon = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {c['name']} — {c['debt']:,.0f} ₽",
            callback_data=f"early_credit:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Календарь", callback_data="open_calendar")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def early_type_kb(credit_id: int) -> InlineKeyboardMarkup:
    """Тип досрочного погашения: полное или частичное."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💥 Погасить полностью", callback_data=f"early_full:{credit_id}")],
        [InlineKeyboardButton(text="💸 Частичное погашение", callback_data=f"early_partial:{credit_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="early_calc")],
    ])


def early_calc_result_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Рассчитать ещё раз",   callback_data=f"early_credit:{credit_id}")],
        [InlineKeyboardButton(text="◀️ Выбрать другой кредит", callback_data="early_calc")],
    ])


def credit_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏦 Кредит / Рассрочка", callback_data="cr_type:loan")],
        [InlineKeyboardButton(text="💳 Кредитная карта",     callback_data="cr_type:card")],
        [InlineKeyboardButton(text="🤝 Долг (другу / знакомому)", callback_data="cr_type:debt")],
    ])


def add_more_credits_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё",  callback_data="ob_add_more")],
        [InlineKeyboardButton(text="✅ Всё, закончил", callback_data="ob_done_credits")],
    ])


def strategy_kb(recommended: str) -> InlineKeyboardMarkup:
    av_text = "🏔 Выбрать Лавину" + (" ⭐" if recommended == "avalanche" else "")
    sn_text = "⛄ Выбрать Снежный ком" + (" ⭐" if recommended == "snowball" else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=av_text, callback_data="strategy:avalanche")],
        [InlineKeyboardButton(text=sn_text, callback_data="strategy:snowball")],
    ])


def change_strategy_kb(recommended: str) -> InlineKeyboardMarkup:
    av_text = "🏔 Применить Лавину" + (" ⭐" if recommended == "avalanche" else "")
    sn_text = "⛄ Применить Снежный ком" + (" ⭐" if recommended == "snowball" else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=av_text, callback_data="change_strategy:avalanche")],
        [InlineKeyboardButton(text=sn_text, callback_data="change_strategy:snowball")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="credits_section")],
    ])


# ── Напоминания (добавление) ──────────────────────────────────────────────────

def rem_days_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="В день платежа", callback_data="rem_days:0"),
            InlineKeyboardButton(text="За 1 день",      callback_data="rem_days:1"),
        ],
        [
            InlineKeyboardButton(text="За 3 дня",  callback_data="rem_days:3"),
            InlineKeyboardButton(text="За 7 дней", callback_data="rem_days:7"),
        ],
        [InlineKeyboardButton(text="Пропустить →", callback_data="rem_days:skip")],
    ])


def rem_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="09:00", callback_data="rem_time:09:00"),
            InlineKeyboardButton(text="12:00", callback_data="rem_time:12:00"),
            InlineKeyboardButton(text="18:00", callback_data="rem_time:18:00"),
            InlineKeyboardButton(text="21:00", callback_data="rem_time:21:00"),
        ],
    ])


def rem_more_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё напоминание",  callback_data="rem_more:yes")],
        [InlineKeyboardButton(text="▶️ Следующий кредит", callback_data="rem_more:next")],
    ])


def statement_check_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, верно",          callback_data=f"stmt_ok:{credit_id}")],
        [InlineKeyboardButton(text="✍️ Нет, другая сумма", callback_data=f"stmt_wrong:{credit_id}")],
        [InlineKeyboardButton(text="◀️ Назад",              callback_data=f"credit_detail:{credit_id}")],
    ])


# ── План / Даты выплат ────────────────────────────────────────────────────────

def plan_view_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать план",        callback_data="plan_edit_mode")],
        [InlineKeyboardButton(text="📆 Настроить даты выплат",     callback_data="plan_setup_paydays")],
        [InlineKeyboardButton(text="🔄 Пересчитать автоматически", callback_data="plan_reset")],
        [InlineKeyboardButton(text="📤 Поделиться результатом",    callback_data="share_result")],
        [InlineKeyboardButton(text="◀️ Календарь",                 callback_data="open_calendar")],
    ])


def plan_edit_kb(allocations: list, credits_map: dict) -> InlineKeyboardMarkup:
    buttons = []
    for a in allocations:
        c = credits_map.get(a["credit_id"])
        if not c:
            continue
        icon    = "💳" if c.get("credit_type") == "card" else ("🤝" if c.get("credit_type") == "debt" else "🏦")
        pd_icon = "①" if a["payday"] == 1 else "②"
        buttons.append([InlineKeyboardButton(
            text=f"{pd_icon} {icon} {c['name']} — {a['amount']:,.0f} ₽  ✏️",
            callback_data=f"plan_edit_item:{a['credit_id']}:{a['payday']}"
        )])
    buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="plan_view")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def plan_edit_item_kb(credit_id: int, payday: int, has_payday2: bool) -> InlineKeyboardMarkup:
    other_label = "② зарплата" if payday == 1 else "① аванс"
    buttons = [
        [InlineKeyboardButton(text="💰 Изменить сумму",
                              callback_data=f"plan_change_amount:{credit_id}:{payday}")],
    ]
    if has_payday2:
        buttons.append([InlineKeyboardButton(
            text=f"↕️ Перенести в {other_label}",
            callback_data=f"plan_move:{credit_id}:{payday}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="plan_edit_mode")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def payday_has_p2_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, есть вторая выплата", callback_data="payday_has_p2:yes")],
        [InlineKeyboardButton(text="❌ Нет, одна выплата",       callback_data="payday_has_p2:no")],
        [InlineKeyboardButton(text="◀️ Назад",                   callback_data="cancel_fsm:settings_back")],
    ])


def cancel_kb(dest: str, label: str = "◀️ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"cancel_fsm:{dest}")
    ]])
