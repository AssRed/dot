# Beauty Master Bot

Telegram-бот на **aiogram 3** для одного бьюти-мастера. Бот обслуживает клиентов
(запись, отмена, перенос, услуги, информация о мастере) и одновременно даёт
мастеру инструменты бухгалтерии, прогноза заработка, управления расписанием и
услугами.

Все клиентские сообщения тёплые, с эмодзи и учётом истории общения
(новый клиент / постоянный).

## Возможности

### Клиент
- `/start` — умное приветствие (новое / возвратное).
- Главное меню с inline-кнопками: «Записаться», «Мои записи»,
  «Отменить/перенести запись», «Услуги и цены», «О мастере».
- Многошаговая запись: услуга → описание → дата (14 дней) → время →
  имя → телефон → подтверждение.
- Просмотр предстоящих записей, отмена с указанием причины,
  перенос на свободный слот.
- Просмотр услуг с полным описанием, ценой и длительностью.
- Раздел «О мастере» с текстом и фото.
- Напоминания за ~2 часа до записи.

### Мастер (доступно только пользователю с `MASTER_TG_ID`)
- `/set_schedule` — интерактивная настройка дней и времени работы.
- `/add_service`, `/edit_service <id>`, `/delete_service <id>`,
  `/list_services`.
- `/appointments` — список предстоящих записей с inline-отменой и
  завершением.
- `/complete <id>` — пометить запись выполненной, ввести фактическую сумму;
  автоматически создаётся транзакция дохода.
- `/finance` — inline-меню:
  - добавить доход / расход (с фото чека),
  - отчёт за период (день / неделя / месяц / квартал / произвольный) с
    выгрузкой в **Excel** (`openpyxl`),
  - прогноз заработка на текущий месяц.
- `/set_master_info` — текст и фото для раздела «О мастере».
- `/set_tax_rate 4` или `/set_tax_rate 6`.
- `/set_welcome_text` — обновить приветствие для новых или постоянных.
- `/clients` — CRM: база клиентов с фильтрами (все / спящие 30+ /
  топ по тратам / VIP).
- `/client <tg_id>` — карточка клиента: история визитов, потраченная сумма,
  переключаемые теги (VIP, аллергия, сложный, новичок), заметка.
- `/broadcast` — рассылка по аудитории (все клиенты / спящие 30+ / VIP) с
  предпросмотром и подтверждением.
- `/waitlist` — список ожидания: кто стоит в очереди на занятые слоты.
- `/analytics` — аналитика за 90 дней: топ-услуг, heatmap по дням и часам,
  процент отмен, конверсия запись → визит.
- `/landing` — выдаёт ссылку-визитку `t.me/<bot>?start=master` для соцсетей.

### Дополнительные авто-функции
- При отмене или переносе записи бот автоматически уведомляет всех
  клиентов в waitlist на тот же день/услугу.
- Каждый день в 11:00 проверяет «спящих» клиентов (30+ дней без визита) и
  шлёт мягкий пинг (с cooldown 30 дней между сообщениями).

### Прогноз заработка
Рассчитывается как:
1. Сумма цен активных записей (`status='active'`) в текущем месяце.
2. + (оставшиеся рабочие дни месяца × среднее число записей в день ×
   средний чек).
   - Среднее число записей берётся за последние 30 дней (выполненные записи),
     fallback — `settings.average_daily_bookings` (по умолчанию 3).
   - Средний чек — среднее по выполненным записям, fallback — средняя
     цена услуг.
3. Рабочие дни считаются по таблице `schedule`; если расписания нет —
   принимаются Пн–Пт.

## Стек

- Python 3.10+ (рекомендуется 3.11)
- [aiogram 3](https://docs.aiogram.dev/) — асинхронный фреймворк для Telegram.
- [asyncpg](https://magicstack.github.io/asyncpg/) — асинхронный драйвер PostgreSQL.
- [APScheduler](https://apscheduler.readthedocs.io/) — напоминания.
- [openpyxl](https://openpyxl.readthedocs.io/) — Excel-отчёты.
- [python-dotenv](https://pypi.org/project/python-dotenv/) — конфигурация.

## Структура проекта

```
.
├── main.py
├── config.py
├── database.py
├── keyboards.py
├── handlers/
│   ├── common.py
│   ├── client.py
│   └── master.py
├── utils/
│   ├── slots.py
│   ├── scheduler.py
│   ├── forecast.py
│   └── report.py
├── requirements.txt
├── Procfile
├── railway.json
└── .env.example
```

## Локальный запуск

1. Установите Python 3.10+.
2. Клонируйте репозиторий и установите зависимости:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Создайте `.env` на основе `.env.example`:
   ```
   BOT_TOKEN=токен_от_BotFather
   MASTER_TG_ID=ваш_telegram_id
   DATABASE_URL=postgresql://user:password@host/db?sslmode=require
   ```
   - Telegram ID можно узнать у `@userinfobot`.
   - Бесплатный Postgres: [Neon](https://neon.tech) (без карты), Supabase, Railway.
4. Запустите:
   ```bash
   python main.py
   ```

Схема и таблицы создаются автоматически при первом подключении.

## Первая настройка (после запуска)

Откройте чат с ботом со своего мастер-аккаунта и выполните:

1. `/set_master_info` — описание и фото мастера.
2. `/set_schedule` — рабочие дни и часы.
3. `/add_service` — добавьте 1-3 услуги.
4. (опционально) `/set_tax_rate 4` — для НПД.
5. (опционально) `/set_welcome_text` — поменять приветствия.

После этого можно делиться ссылкой на бота с клиентами.

## Деплой

Подходит любой контейнерный хост, поддерживающий `Dockerfile`. Бот
работает в **webhook-режиме** через FastAPI (`app:app`), поэтому хост
может усыпать инстанс при простое — Telegram разбудит его новым
update'ом.

### Render (бесплатный, без карты)
1. https://dashboard.render.com → **New +** → **Web Service**.
2. **Connect a repository** → выбрать `AssRed/dot`.
3. Settings:
   - **Name**: `beauty-master-bot` (URL будет `https://beauty-master-bot.onrender.com`).
   - **Region**: `Frankfurt` или `Oregon`.
   - **Branch**: `devin/1777743135-bootstrap-bot`.
   - **Runtime**: `Docker` (auto-detected по `Dockerfile`).
   - **Instance Type**: `Free`.
4. Environment variables:
   - `BOT_TOKEN`
   - `MASTER_TG_ID`
   - `DATABASE_URL` (Postgres URL от [Neon](https://neon.tech))
   - `WEBHOOK_BASE_URL=https://beauty-master-bot.onrender.com`
   - (опционально) `TZ=Europe/Moscow`
5. **Create Web Service** → подождать ~3 мин до `Live`.

> Free Web Service на Render засыпает после 15 мин простоя; первый запрос после сна
> занимает ~30 сек, но Telegram повторит webhook, так что сообщения не теряются.

### Koyeb (бесплатный nano)
1. https://app.koyeb.com → **Create Service** → **GitHub** → выбрать репозиторий.
2. Build: **Dockerfile** (auto-detected).
3. Region: `was` или `fra`. Instance: `Eco/Free`.
4. Environment variables:
   - `BOT_TOKEN`
   - `MASTER_TG_ID`
   - `DATABASE_URL` (Postgres URL, например от [Neon](https://neon.tech))
   - `WEBHOOK_BASE_URL=https://<your-app>.koyeb.app`
   - (опционально) `TZ=Europe/Moscow`
5. Health check: HTTP GET `/health` на порту `8080`.

### Fly.io
```bash
fly secrets set BOT_TOKEN=... MASTER_TG_ID=... DATABASE_URL=...
fly deploy
```
Webhook URL подхватывается автоматически из `FLY_APP_NAME`.

## Деплой на VPS (systemd)

```ini
# /etc/systemd/system/beauty-bot.service
[Unit]
Description=Beauty Master Bot
After=network.target

[Service]
WorkingDirectory=/opt/beauty-master-bot
EnvironmentFile=/opt/beauty-master-bot/.env
ExecStart=/opt/beauty-master-bot/.venv/bin/python main.py
Restart=on-failure
User=bot

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now beauty-bot
journalctl -u beauty-bot -f
```

## Логи

Все события пишутся в `bot.log` (ротация по 1 МБ, 3 файла) и в stdout.

## Лицензия

MIT.
