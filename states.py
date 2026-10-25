from aiogram.fsm.state import State, StatesGroup


class AddCredit(StatesGroup):
    type         = State()   # loan / card  (выбирается кнопкой)
    name         = State()
    debt         = State()
    rate         = State()   # для карты: годовых; для кредита: ежемесячный платёж
    payment_day  = State()
    min_type     = State()   # только для карты
    min_value    = State()   # только для карты
    priority     = State()


class AddReminder(StatesGroup):
    days_before  = State()
    time         = State()


class ChangeBudget(StatesGroup):
    amount = State()


class ChangeMonthBudget(StatesGroup):
    amount = State()


class SetPriority(StatesGroup):
    value = State()


class Onboarding(StatesGroup):
    budget           = State()
    wow              = State()   # экран «Вау» после ввода бюджета
    cr_name          = State()
    cr_type          = State()   # loan / card
    cr_debt          = State()
    cr_rate          = State()
    cr_day           = State()
    cr_statement_day = State()   # только для карты: день выписки
    cr_min_type      = State()
    cr_min_value     = State()
    more_credits     = State()
    strategy         = State()
    rem_days         = State()
    rem_time         = State()
    rem_more         = State()


class EarlyPayment(StatesGroup):
    extra = State()   # сумма досрочного взноса


class CardExpense(StatesGroup):
    amount = State()  # сумма расхода с карты


class StatementCheck(StatesGroup):
    amount = State()


class EditCredit(StatesGroup):
    value    = State()   # одиночные поля: название, долг, ставка, день, приоритет
    min_type = State()   # шаг 1 редактирования минималки
    min_val  = State()   # шаг 2 редактирования минималки


class Payment(StatesGroup):
    custom_amount = State()
    custom_debt   = State()


class PaydaySetup(StatesGroup):
    p1_day    = State()
    p1_amount = State()
    has_p2    = State()
    p2_day    = State()
    p2_amount = State()


class EditPlan(StatesGroup):
    amount = State()


class SyncBalances(StatesGroup):
    balance = State()
