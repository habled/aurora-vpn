# aurora vpn

Telegram Mini App с серверной частью, которая выдаёт уникальные VPN-ключи прямо внутри WebApp.

```
                ┌──────────────────┐
   Telegram ◄──►│  bot.py + REST   │◄──► Postgres (Neon)
                │  (Render Free)   │
                └────────▲─────────┘
                         │ HTTPS API
                         │
                ┌────────┴─────────┐
                │   index.html     │
                │  (GitHub Pages)  │
                └──────────────────┘
```

---

## три тарифа

| Тариф         | Статус   | Что входит                      |
|---------------|----------|---------------------------------|
| `01 · free`   | активен  | один бесплатный ключ навсегда   |
| `02 · pro`    | скоро    | премиум-подписка                |
| `03 · atlas`  | скоро    | расширенный доступ              |

Сейчас работает только **free**. Один ключ на пользователя, действует без сроков.

---

## что тебе понадобится

Все сервисы — бесплатные. Подключаешь карту только если сам захочешь.

1. **GitHub** — у тебя уже есть аккаунт `habled`.
2. **Neon.tech** — бесплатная PostgreSQL-база. Регистрация ниже.
3. **Render.com** — бесплатный хостинг для бота. Регистрация ниже.
4. **(опционально) UptimeRobot** — будит Render каждые 5 минут, чтобы не было задержек.

---

# деплой — пошагово

Я разбил на 9 шагов. Делай по порядку, не пропускай.

---

## шаг 1 · обновить файлы в репозитории

В твоём GitHub-репозитории `habled/aurora-vpn` сейчас лежит только старая `index.html`. Нужно положить туда новые файлы.

### что нужно сделать

1. Скачай **все** файлы из этого ответа (я прикреплю их ниже):
   - `bot.py`
   - `index.html`
   - `requirements.txt`
   - `runtime.txt`
   - `render.yaml`
   - `.gitignore`
   - `.env.example`
   - `README.md` (этот файл)

2. Открой [https://github.com/habled/aurora-vpn](https://github.com/habled/aurora-vpn)

3. Нажми **Add file → Upload files**

4. Перетащи туда **все** новые файлы (включая обновлённую `index.html`). Если GitHub спросит «заменить существующий файл?» — соглашайся.

5. Внизу страницы напиши в `Commit changes`: `aurora v3` и нажми **Commit changes**.

После коммита в репозитории должны лежать все 8 файлов.

---

## шаг 2 · создать базу данных в Neon

1. Открой [https://neon.tech](https://neon.tech) и нажми **Sign up**. Войти можно через GitHub — это самый быстрый способ.

2. После регистрации Neon предложит создать первый проект. Заполни:
   - **Project name:** `aurora-vpn`
   - **Database name:** `aurora`
   - **Region:** выбери ближайший к тебе (например, `EU (Frankfurt)`)
   - **PostgreSQL version:** оставь по умолчанию (17 или новее)

3. Нажми **Create project**.

4. После создания Neon покажет страницу **Connection Details**. Найди блок **Connection string**, выбери в выпадающем списке `psql` или `Pooled connection`, и **скопируй строку целиком**. Она выглядит так:
   ```
   postgresql://aurora_owner:abcDEF123@ep-cool-name-12345.eu-central-1.aws.neon.tech/aurora?sslmode=require
   ```

5. Сохрани эту строку в заметку — она понадобится через несколько шагов.

> Таблицы создавать вручную не нужно — бот сам их создаст при первом запуске.

---

## шаг 3 · создать сервис на Render

1. Открой [https://render.com](https://render.com) и нажми **Get Started**. Войти можно через GitHub — это самый удобный способ, потому что Render сразу получит доступ к твоим репозиториям.

2. После входа нажми **New → Web Service**.

3. На странице **Create a new Web Service** в разделе **Source Code** выбери **Public Git Repository** или подключи свой `habled/aurora-vpn` (если GitHub-аккаунт уже привязан, он появится в списке). Если запросит разрешение — дай его только для нужного репозитория.

4. После выбора репозитория Render автоматически прочитает файл `render.yaml` и заполнит большинство полей. Проверь:
   - **Name:** `aurora-vpn-bot` (или любое другое — это станет частью URL)
   - **Region:** `Frankfurt (EU Central)` или ближайший к тебе
   - **Branch:** `main`
   - **Runtime:** `Python`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
   - **Instance Type:** `Free`

5. Прокрути вниз до раздела **Environment Variables**. Нужно заполнить 6 переменных (`WEBHOOK_SECRET` Render сгенерирует сам, `LOG_LEVEL` уже задан):

   | Key            | Value                                                                |
   |----------------|----------------------------------------------------------------------|
   | `BOT_TOKEN`    | твой токен от @BotFather                                             |
   | `ADMIN_IDS`    | `2046949246`  (твой Telegram ID)                                     |
   | `DATABASE_URL` | строка из Neon (шаг 2)                                               |
   | `WEBAPP_URL`   | `https://habled.github.io/aurora-vpn/`                               |
   | `PUBLIC_URL`   | оставь пока пустым — Render даст URL после деплоя                    |
   | `CORS_ORIGIN`  | `https://habled.github.io`                                           |

6. Нажми **Create Web Service** внизу.

7. Render начнёт собирать и деплоить сервис. Это занимает 3-5 минут. В реальном времени видно логи. В конце должно появиться:
   ```
   ==> Your service is live 🎉
   Available at: https://aurora-vpn-bot.onrender.com
   ```

8. **СКОПИРУЙ этот URL** — он понадобится в следующем шаге.

---

## шаг 4 · добавить PUBLIC_URL и редеплоить

На том же сервисе на Render:

1. Сверху открой вкладку **Environment**.

2. Найди переменную `PUBLIC_URL` и нажми **Edit**.

3. Вставь свой Render-URL без слеша в конце:
   ```
   https://aurora-vpn-bot.onrender.com
   ```

4. Нажми **Save changes**. Render автоматически перезапустит сервис.

5. Подожди 1-2 минуты, пока редеплой завершится. В логах должно появиться:
   ```
   Database pool ready.
   Webhook installed at https://aurora-vpn-bot.onrender.com/webhook
   ```

Если видишь это — backend работает.

---

## шаг 5 · вписать API URL в WebApp

WebApp сейчас не знает, куда стучаться. Нужно подставить URL твоего Render-сервиса в `index.html`.

1. Открой [https://github.com/habled/aurora-vpn/blob/main/index.html](https://github.com/habled/aurora-vpn/blob/main/index.html)

2. Нажми карандашик ✏️ в правом верхнем углу.

3. Нажми **Ctrl+F** (или Cmd+F) и найди строку:
   ```js
   const API_BASE = 'https://CHANGE-ME.onrender.com';
   ```

4. Замени `CHANGE-ME.onrender.com` на свой реальный URL Render-сервиса. Должно получиться:
   ```js
   const API_BASE = 'https://aurora-vpn-bot.onrender.com';
   ```

5. Прокрути вниз → **Commit changes** → **Commit changes**.

6. Подожди 1-2 минуты — GitHub Pages обновит страницу.

---

## шаг 6 · обновить WEBAPP_URL для cache-busting

Telegram кэширует WebApp страницы агрессивно. Чтобы он точно подхватил новую версию, добавь к URL версию:

1. На Render открой **Environment** твоего сервиса.

2. Отредактируй `WEBAPP_URL`:
   ```
   https://habled.github.io/aurora-vpn/?v=7
   ```

3. **Save changes**. Render редеплоится.

---

## шаг 7 · положить в базу первые тестовые ключи

1. Открой Telegram, зайди в чат со своим ботом.

2. Отправь команду:
   ```
   /add TEST-AURORA-001 TEST-AURORA-002 TEST-AURORA-003
   ```

3. Бот должен ответить:
   ```
   добавлено новых кодов: 3
   всего доступно: 3
   ```

> Если ответа нет — проверь логи на Render. Скорее всего, сервис «спит» (см. шаг 9 ниже).

---

## шаг 8 · тестируем

1. Полностью закрой Telegram на айфоне (двойной свайп → смахнуть приложение).

2. Открой Telegram, найди своего бота, нажми `/start`.

3. Должна прийти карточка `aurora vpn · free · ключ навсегда` с кнопкой **открыть aurora**.

4. Жми кнопку. Откроется WebApp:
   - На главной — `добрый день / вечер`, твоё имя, четыре чипа, три карточки тарифов.
   - Карточки PRO и ATLAS — с пустыми обведёнными иконками и бейджем «скоро».

5. Тапни карточку **aurora free**.

6. Откроется детальный экран. Внизу — «карточка ключа» в состоянии **«ожидает получения»**.

7. Жми **получить ключ**. Через 1-2 секунды состояние сменится: появится твой `TEST-AURORA-001` крупным моноширинным шрифтом в «билете».

8. Тапни на билет — ключ скопируется в буфер обмена. Toast подтвердит: «ключ скопирован».

9. Закрой WebApp, открой снова → детальный экран `aurora free` → видишь свой ключ в состоянии **«активен · бессрочно»**. Это твой ключ навсегда.

---

## шаг 9 · (опционально) разбудить Render через UptimeRobot

Render Free засыпает после 15 минут простоя — первый запрос будит, но это занимает ~30 секунд. Чтобы пользователи не ждали:

1. Зарегистрируйся на [https://uptimerobot.com](https://uptimerobot.com) (бесплатно).

2. Создай новый монитор:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** `aurora-keepalive`
   - **URL:** `https://aurora-vpn-bot.onrender.com/health`
   - **Monitoring Interval:** `5 minutes`

3. Сохрани. UptimeRobot будет дёргать `/health` каждые 5 минут — Render не уснёт.

---

# команды администратора

В чате со своим ботом доступны:

| Команда   | Что делает                                                |
|-----------|-----------------------------------------------------------|
| `/add`    | Добавить ключи. Можно по одному в строку или через пробел |
| `/stats`  | Показать остаток и историю выдачи                         |

Пример:
```
/add
KEY-1234-ABCD
KEY-5678-EFGH
KEY-9012-IJKL
```

---

# архитектура (для будущего разработчика)

## бэкенд

`bot.py` — один файл, который одновременно:
- слушает webhook от Telegram (через `aiogram.webhook.aiohttp_server`)
- отдаёт REST API на `/api/status` и `/api/issue-code`
- общается с Postgres через пул `asyncpg`
- проверяет HMAC-SHA256 подпись каждого API-запроса от WebApp

Структура:
- `validate_init_data()` — критический security-слой. Все API-эндпоинты проходят через `_authenticate()`, который проверяет `initData` HMAC-подписью с использованием `BOT_TOKEN` как секретного ключа. Без этого WebApp мог бы подделать любого пользователя.
- `repo_*` — единственное место с SQL. Если нужно что-то поменять в схеме, всё здесь.
- `issue_code_flow` / `repo_issue_code` — атомарная выдача через `FOR UPDATE SKIP LOCKED`. Конкурентные запросы не могут получить один и тот же ключ.

## фронтенд

`webapp/index.html` — single-file SPA, без сборки, без зависимостей кроме шрифтов Google.

Машина состояний для карточки ключа:
- `loading` → `idle` → `ready` (после получения)
- `loading` → `active` (если ключ уже выдан ранее)
- `loading` → `empty` / `error`

Навигация — между четырьмя экранами (home / access / faq / support) через JS, без перезагрузки. Кнопка «назад» интегрирована с нативной `tg.BackButton` Telegram.

## база данных

Две таблицы:
- `vpn_codes` — пул выданных и невыданных кодов
- `user_requests` — журнал «кому что выдали»

Индексы на `user_requests.telegram_id` (быстрый lookup «есть ли у пользователя ключ») и частичный индекс на `vpn_codes` для невыданных (быстрый поиск следующего).

## безопасность

- Бэкенд проверяет HMAC-SHA256 подпись `initData` на каждом API-запросе.
- Webhook от Telegram защищён `secret_token` (передаётся в заголовке, проверяется aiogram).
- CORS жёстко привязан к `CORS_ORIGIN` — никто кроме GitHub Pages не может вызвать API из браузера.
- Токен бота никогда не появляется в коде WebApp — только на бэкенде.

---

# когда придёт время добавлять PRO и ATLAS

Архитектура к этому готова. Что нужно будет сделать:

1. Добавить в `vpn_codes` колонку `tier` (`'free' | 'pro' | 'atlas'`).
2. Добавить таблицу `user_subscriptions` (`telegram_id`, `tier`, `expires_at`).
3. В `bot.py` сделать `COOLDOWN_HOURS_BETWEEN_CODES` зависимым от тарифа пользователя.
4. Подключить эквайринг (Telegram Stars / ЮKassa / Stripe) для приёма платежей.
5. В WebApp активировать карточки `aurora pro` и `aurora atlas`, добавить экраны деталей.

Бизнес-логика выдачи (`repo_issue_code`) меняется минимально — только условие выбора кода добавит `WHERE tier = $1`.
