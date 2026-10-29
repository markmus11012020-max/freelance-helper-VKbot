# Freelance Monitor — VK-бот для поиска заказов под вайбкодинг

ИИ-агент мониторит фриланс-биржи, отбирает заказы, которые реально делаются быстро с
помощью ИИ (боты, парсеры, скрипты, автоматизация, интеграции API), и присылает карточки
в VK-беседу. Работает из России без прокси и без Telegram.

## Что уже работает

| Источник | Статус | Как забираем |
|---|---|---|
| FL.ru | ✅ | RSS `https://www.fl.ru/rss/all.xml` + страница заказа для бюджета |
| freelance.ru | ✅ | HTML-лента `https://freelance.ru/task?page=N`, бюджет прямо в карточке |
| Kwork | ✅ | внутренний JSON `POST https://kwork.ru/projects` (без логина) |
| Habr Freelance | ❌ | Сервис закрыт навсегда (HTTP 410, 29.10.2026) |
| Weblancer | ⚠️ | Отдаёт 403 автоматическим клиентам, нужен другой способ доступа |
| Freelancehunt | ❌ | 403 для ботов |
| Workzilla | ❌ | Сайт недоступен |
| VK-паблики заказов | 🔜 | Нужен user token и список сообществ |

Проверка источников идёт каждые `SCAN_INTERVAL_SECONDS` (по умолчанию 10 минут).
Новые заказы (которых ещё нет в БД) проходят: правила → LLM-реранкер → порог `MIN_SCORE`.
Повторно одни и те же заказы в LLM не отправляются и в VK не дублируются.

## Архитектура

```
monitor (раз в 10 минут)
  parsers: fl.ru RSS, freelance.ru HTML
  -> нормализация + стемминг (русские словоформы)
  -> правила: ключевые/стоп-слова, бюджет, свежесть
  -> LLM: score 0-100 + причина (OpenAI-совместимый API)
  -> SQLite (orders, peers, deliveries)
  -> notifier: карточки в беседу VK (fan-out, rate limit, retry)

VK Long Poll (постоянно)
  /start, /connect, /status, /last, /digest, /ask, /pause, /add, /remove
  заявки -> кнопка «Добавить в беседу» у админа

HTTP :8080/healthz  -> docker healthcheck / внешний аптайм-монитор
backup каждые 6 часов -> /data/backups (последние 8 копий)
```

Один контейнер, три asyncio-задачи, SQLite в WAL-режиме с одним писателем.

## Быстрый старт локально

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
# заполни VK_* и OPENAI_*, либо оставь пустым для dry-run
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m app.main --once
```

`--once` без `--send` всегда работает в dry-run: карточки печатаются в лог, в VK ничего
не уходит. Локальная БД по умолчанию — `data/bot.db`.

Проверка провайдера ИИ и VK-токена:

```powershell
.\.venv\Scripts\python -m scripts.check_llm
.\.venv\Scripts\python -m scripts.check_vk
```

## Настройка VK

1. Создай сообщество (тип «Сообщество» или «Публичная страница»), включи
   **Управление → Сообщения → Сообщения сообщества**.
2. **Управление → Работа с API → Ключи доступа**: создай ключ с правами
   «Сообщения сообщества» и «Управление сообществом». Это `VK_GROUP_TOKEN`.
3. **Управление → Работа с API → Long Poll API**: включи и отметь события
   `message_new`, `message_event`, `message_allow` (можно `group_join`, `group_leave`).
4. Числовой id сообщества — `VK_GROUP_ID`; твой user id — `VK_ADMIN_USER_ID`
   (узнать: `vk.com/id...` или команда `/whoami` после запуска).
5. Создай беседу, добавь туда сообщество, отправь в беседе `/connect` (ты — админ).
   Бот запомнит `peer_id` и будет писать туда.
6. Новые люди пишут сообществу `/start` → админу приходит заявка с кнопкой
   «Добавить в беседу». VK может запретить автоматическое добавление — тогда бот
   попросит добавить вручную, писать в беседу он продолжит.

Важно: бот не может первым написать человеку, пока тот не разрешил сообщения
сообщества (событие `message_allow`).

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `VK_GROUP_TOKEN` | токен сообщества (секрет) |
| `VK_GROUP_ID` | числовой id сообщества |
| `VK_ADMIN_USER_ID` / `VK_ADMIN_USER_IDS` | id админов через запятую |
| `VK_CHAT_PEER_ID` | необязательно: `2000000000 + chat_id`, если не хочешь `/connect` |
| `DATABASE_PATH` | путь к SQLite (в Docker/Amvera `/data/bot.db`) |
| `PUBLIC_BASE_URL` | публичный HTTPS-адрес для кнопки «Откликнуться» (`/go/<order_id>`); пусто = прямые ссылки |
| `SCAN_INTERVAL_SECONDS` | период опроса бирж, по умолчанию 600 |
| `DRY_RUN=1` | не отправлять в VK |
| `FL_RU_ENABLED`, `FREELANCE_RU_ENABLED`, `KWORK_ENABLED` | включение источников |
| `KEYWORDS_JSON`, `STOPWORDS_JSON` | списки в JSON-массиве |
| `MIN_SCORE` | порог отправки (по умолчанию 40) |
| `BUDGET_MIN_RUB` | «жирный» бюджет, по умолчанию 15 000 ₽ |
| `MAX_ORDERS_PER_CYCLE` | максимум карточек и LLM-вызовов за цикл |
| `OPENAI_BASE_URL` | по умолчанию `https://api.aitunnel.ru/v1` |
| `OPENAI_API_KEY` | ключ провайдера |
| `OPENAI_MODEL` | по умолчанию `deepseek-v4.1-flash-1017` |
| `LLM_ENABLED`, `LLM_DAILY_TOKEN_LIMIT` | выключить ИИ / лимит токенов в сутки |

Модель и base URL меняются только через env — код править не нужно. Любой
OpenAI-совместимый провайдер подойдёт.

## Проверка Telegram до разработки (probe)

Полный Telegram-модуль ещё не написан. Сначала проверяем, доступен ли Bot API из Amvera
напрямую — VPN на домашнем ПК для этого не подходит.

**Локально (с VPN):**

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:AA..."
.\.venv\Scripts\python -m scripts.check_telegram
```

**На Amvera (главная проверка):**

1. Создай отдельный проект `freelance-tg-probe` (Docker, этот же репозиторий).
2. Переменные окружения:
   ```
   PROBE_MODE=telegram
   TELEGRAM_BOT_TOKEN=<токен от BotFather>
   TELEGRAM_PROXY=            # пусто = прямой доступ
   ```
3. Открой `https://<проект>.amvera.io/healthz`. Ответ:
   ```json
   {
     "ok": true,
     "route": "direct",
     "success": 3,
     "attempts": [{"attempt": 1, "ok": true, "latency_ms": 420}],
     "poll": {"ok": true, "updates": 1, "last_update_id": 101},
     "egress_ip": "203.0.113.10"
   }
   ```
4. Отправь боту `/start` и обнови страницу: `poll.updates` должен вырасти.
5. Быстрая проверка без getUpdates: `/healthz?attempts=1&poll=0`.

**Критерии go/no-go:** 5/5 успешных `getMe` с задержкой < 3 с, `getUpdates` получает
сообщение < 10 с, 15 минут опроса с успехом > 95%. Если Amvera не видит Telegram
напрямую — модуль туда не поставить без прокси или зарубежного VPS.

После проверки убери `PROBE_MODE` из env проекта.

## Деплой на Amvera

1. Зарегистрируйся на `cloud.amvera.ru`, создай проект **Docker**.
2. Залей код (GitHub или git push в Amvera):
   ```powershell
   git remote add amvera https://git.amvera.ru/<логин>/freelance-monitor
   git push amvera master
   ```
3. В **Конфигурации → Переменные окружения** добавь все значения из `.env`.
   Обязательно `DATABASE_PATH=/data/bot.db`.
4. Тариф: «Пробный» ~172 ₽/мес (0,24 ₽/час) или «Начальный» ~288 ₽/мес.
   Постоянное хранилище монтируется в `/data`, бэкапы делает платформа.
5. После сборки проверь `https://<проект>.amvera.io/healthz`.

`amvera.yml` уже в корне: Docker, `persistenceMount: /data`, порт 8080.

## Docker на своём сервере

```powershell
Copy-Item .env.example .env   # заполнить
docker compose up -d --build
docker compose logs -f
```

Проверка: `http://127.0.0.1:8080/healthz`. База и бэкапы — в `./data`.

## Команды бота

| Команда | Кто | Что делает |
|---|---|---|
| `/start` | все | регистрация / заявка на добавление |
| `/connect` | админ, в беседе | подключить беседу |
| `/status` | все | состояние, источники, расход токенов |
| `/last 5` | все | последние заказы |
| `/digest` | все | сводка за 24 часа (LLM) |
| `/ask вопрос` | все | вопрос ИИ-ассистенту |
| `/pause`, `/resume` | админ | пауза мониторинга |
| `/add 12345`, `/remove 12345` | админ | состав беседы |
| `/whoami` | все | user_id / peer_id / chat_id |
| `/help` | все | справка |

Кнопки в карточке: «✍️ Черновик» — LLM пишет черновик отклика в личку,
«🚀 Откликнуться» — открывает заказ в браузере. Ссылка идёт через
`PUBLIC_BASE_URL/go/<order_id>`, поэтому работает и для Kwork, который VK
не пускает в кнопки напрямую. Редирект разрешён только для заказов из БД
и доменов fl.ru / freelance.ru / kwork.ru.

## Как работает отбор

1. **Префильтр** — в заголовке/описании есть ключевое слово (стемминг: «скрипта» =
   «скрипт», «автоматизации» = «автоматизация»).
2. **Правила** — совпадения ключей, бюджет (проект или ставка/час), свежесть,
   штрафы за стоп-слова (1С, WordPress, SEO, крипта, дизайн, вёрстка, QA, аудит).
3. **LLM-реранкер** — оценивает 0–100 и объясняет причину; финальная оценка =
   60% LLM + 40% правил.
4. **Порог** — в беседу уходит только `score >= MIN_SCORE` и не больше
   `MAX_ORDERS_PER_CYCLE` за цикл.

Тюнинг: правь `KEYWORDS_JSON`, `STOPWORDS_JSON`, `MIN_SCORE` в env и смотри
`/last`, чтобы понять, что проходит.

## Структура проекта

```
app/
  config.py        настройки из env
  db.py            SQLite: orders, peers, deliveries, kv, llm_cache
  filters.py       префильтр и правила
  textutil.py      нормализация, стемминг, бюджет, даты
  llm.py           OpenAI-совместимый клиент (aitunnel)
  monitor.py       цикл опроса и отбора
  notifier.py      карточки, клавиатура, fan-out
  health.py        /healthz
  backup.py        ротация бэкапов SQLite
  telegram_probe.py проверка доступности Telegram Bot API
  vk/              client, longpoll, handlers
  parsers/         fl_ru, freelance_ru, kwork
scripts/           check_llm, check_vk, check_telegram
tests/             фикстуры и unit-тесты
```

## Ограничения и заметки

- Kwork использует внутренний JSON-эндпоинт (не официальный API): если он изменится,
  парсер нужно будет поправить. Запросы идут с паузами, без авторизации.
- Парсинг публичных страниц — на свой риск; запросы идут с паузами 1 с, без логина
  и обхода капчи. Если биржа предложит официальный API — лучше перейти на него.
- Бюджет «по договорённости» не отсекается: такие заказы может вытянуть LLM.
- Автоотклик на биржах не делается (нарушает правила площадок): бот только готовит
  черновик, отправляешь ты.
- Секреты живут только в env платформы. Не коммить `.env`.
