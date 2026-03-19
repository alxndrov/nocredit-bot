from aiogram.fsm.state import State, StatesGroup


class AddCredit(StatesGroup):
    name         = State()
    debt         = State()
    rate         = State()
    payment_day  = State()
    min_type     = State()
    min_value    = State()
    priority     = State()


class AddReminder(StatesGroup):
    days_before  = State()
    time         = State()


class ChangeBudget(StatesGroup):
    amount = State()


class SetPriority(StatesGroup):
    value = State()
