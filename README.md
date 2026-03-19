# 💳 CreditBot v2 — умный трекер кредитов

## Возможности

- **Любое кол-во кредитов** — добавляй/удаляй сам
- **Гибкие напоминания** — для каждого кредита своё время и за сколько дней до платежа
- **Приоритеты** — сам задаёшь, кого гасить первым
- **Минималка** — формула (% × множитель) или фиксированная сумма
- **График погашения** — пересчитывается автоматически при каждом платеже

---

## Быстрый старт на VPS

### 1. Создай бота

Открой @BotFather → /newbot → получи токен.

### 2. Залей файлы

```bash
scp -r ./creditbot2 user@server:/home/user/creditbot
# или git clone
```

### 3. Установи зависимости

```bash
cd creditbot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Создай .env

```bash
echo "BOT_TOKEN=1234567890:AAxxxxxxxxxx" > .env
```

### 5. Запусти

```bash
# Тест
python bot.py

# Навсегда через systemd
sudo nano /etc/systemd/system/creditbot.service
```

```ini
[Unit]
Description=CreditBot
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/creditbot
ExecStart=/home/YOUR_USER/creditbot/venv/bin/python bot.py
Restart=always
RestartSec=10
Environment=TZ=Europe/Moscow

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now creditbot
sudo systemctl status creditbot
journalctl -u creditbot -f   # логи
```

---

## Как пользоваться

### Добавить кредит
**💳 Кредиты → ➕ Добавить кредит**

Вводишь по шагам:
1. Название (Тинькофф, ОТП, Сбер...)
2. Текущий долг
3. Ставка % в месяц
4. День платежа по договору
5. Минималка: формула (% × 2) или фиксированная сумма
6. Приоритет (1 = гасить первым)

### Настроить напоминания
**🔔 Напоминания → выбрать кредит → ➕ Добавить напоминание**

- Вводишь: за сколько дней до платежа (0 = в день платежа)
- Вводишь: время в формате `10:00`
- Можно добавить несколько напоминаний на один кредит

### Внести платёж
**✅ Внести платёж → выбрать кредит → подтвердить**

Бот сам посчитает рекомендуемую сумму по стратегии лавины.

### Изменить приоритет
**💳 Кредиты → кредит → 🔢 Изменить приоритет**

---

## Структура файлов

```
creditbot/
├── bot.py          — хендлеры и логика
├── calculator.py   — расчёт графика погашения
├── database.py     — SQLite (кредиты, напоминания, платежи)
├── keyboards.py    — кнопки
├── scheduler.py    — проверка напоминаний каждую минуту
├── states.py       — FSM состояния
├── config.py       — токен из .env
├── requirements.txt
└── .env            — BOT_TOKEN (не коммить!)
```
