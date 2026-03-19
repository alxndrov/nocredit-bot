from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"),      KeyboardButton(text="✅ Внести платёж")],
            [KeyboardButton(text="💳 Кредиты"),     KeyboardButton(text="🔔 Напоминания")],
            [KeyboardButton(text="📋 История"),     KeyboardButton(text="⚙️ Бюджет")],
        ],
        resize_keyboard=True
    )


def credits_list_kb(credits: list, action: str) -> InlineKeyboardMarkup:
    """Список кредитов как инлайн-кнопки. action = префикс callback_data."""
    buttons = []
    for c in credits:
        label = f"{'✅' if c['debt'] < 0.01 else '💳'} {c['name']} — {c['debt']:,.0f} ₽"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"{action}:{c['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def credit_detail_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Внести платёж",   callback_data=f"pay:{credit_id}")],
        [InlineKeyboardButton(text="🔔 Напоминания",     callback_data=f"remind_list:{credit_id}")],
        [InlineKeyboardButton(text="🔢 Изменить приоритет", callback_data=f"set_priority:{credit_id}")],
        [InlineKeyboardButton(text="🗑 Удалить кредит",  callback_data=f"del_credit:{credit_id}")],
        [InlineKeyboardButton(text="◀️ Назад",           callback_data="credits_menu")],
    ])


def confirm_pay_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_pay:{credit_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"credit_detail:{credit_id}"),
        ]
    ])


def reminders_kb(credit_id: int, reminders: list) -> InlineKeyboardMarkup:
    buttons = []
    for r in reminders:
        label = (
            f"{'В день' if r['days_before'] == 0 else f'За {r[\"days_before\"]} дн.'}"
            f" в {r['hour']:02d}:{r['minute']:02d}"
        )
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {label}",
            callback_data=f"del_reminder:{r['id']}"
        )])
    buttons.append([InlineKeyboardButton(
        text="➕ Добавить напоминание",
        callback_data=f"add_reminder:{credit_id}"
    )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"credit_detail:{credit_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_delete_kb(credit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Да, удалить",  callback_data=f"confirm_del:{credit_id}"),
            InlineKeyboardButton(text="❌ Отмена",       callback_data=f"credit_detail:{credit_id}"),
        ]
    ])
