# WB Price & Stock Tracker (Проект 1)

Telegram-бот: пользователь отправляет артикул товара Wildberries → бот получает
текущую цену, скидку и остаток на складах, сохраняет историю в локальную SQLite.
Раз в сутки бот проверяет изменения и отправляет push-уведомление, если цена
упала или товар заканчивается.

## Стек (строго по ТЗ)

| Библиотека   | Зачем                                                        |
|--------------|--------------------------------------------------------------|
| aiogram v3.x | Telegram Bot API                                             |
| httpx        | асинхронные HTTP-запросы к публичному API карточек WB        |
| aiosqlite    | асинхронная SQLite (товары, подписки, история цен)           |
| asyncio      | асинхронная модель                                           |
| apscheduler  | ежедневная проверка (в ТЗ: «Celery или apscheduler» — выбран apscheduler, т.к. не требует брокера) |

## Команды

- `/track АРТИКУЛ` — начать отслеживание
- `/list` — мои товары
- `/untrack АРТИКУЛ` — удалить из отслеживания
- `/history АРТИКУЛ` — история цен

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export WB_BOT_TOKEN=123456:ABC...   # Windows PowerShell: $env:WB_BOT_TOKEN="..."
python bot.py
```

Чтобы не ходить в сеть (антибот-блокировки), задайте `WB_DEMO_MODE=1`.

## Честное примечание о публичном API WB (проверено 2026-08-05)

- Формат запроса — публичный, но **недокументированный** эндпоинт
  `card.wb.ru/cards/v1/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm=АРТИКУЛ`.
  Он подтверждён open-source проектами и туториалами:
  [joitandr/wb_parsing](https://github.com/joitandr/wb_parsing),
  [nickisnotgaara/wildberries-reviews-bot](https://github.com/nickisnotgaara/wildberries-reviews-bot),
  [Proxycove guide](https://proxycove.com/en/blog/python-requests-proxy-setup-guide).
- **При прямой проверке с этого компьютера WB вернул HTTP 403** (антибот),
  даже с браузерным User-Agent. Эндпоинт существует, но может требовать другого
  IP/прокси. Поэтому в проекте есть демо-режим (`WB_DEMO_MODE=1`).
- Цены в ответе приходят в **копейках** (`priceU`, `salePriceU`) — делим на 100.
- Структура ответа исторически менялась (`payload` vs `data`) — парсер устойчив к обоим.

### Защита от rate-limit
- Между запросами при массовой проверке — пауза (`WB_REQUEST_DELAY_SECONDS`, по умолчанию 0.5 с).
- На 429/5xx и сетевые ошибки — retry с экспоненциальным backoff (0.5 → 1 → 2 с, потолок 10 с) средствами stdlib `asyncio` (в ТЗ tenacity не указан, поэтому внешняя библиотека не добавлялась).

## Антибот-обход (что проверено и что работает)

Проверено эмпирически с рабочего IP (2026-08-06):

| Эндпоинт | Результат | Вывод |
|---|---|---|
| `card.wb.ru` v1/v2 | HTTP 403 всегда | **IP-блок на эдже** (`Angie`): заголовки, куки и даже полная имитация TLS/HTTP2-отпечатка Chrome (`curl_cffi impersonate=chrome`) не помогают |
| `search.wb.ru` v4 | через `curl_cffi` — HTTP 200, но пустой фейковый ответ; обычные httpx/curl — 429 | эдж фильтрует **по отпечатку клиента**; с «чистого» IP поиск по артикулу отдаёт реальные цены |
| `www.wildberries.ru` | HTTP 498 | IP-блок |

Реализовано в `wb_api.py`:
1. **Транспорт `curl_cffi`** (`WB_HTTP_CLIENT=curl_cffi`, по умолчанию) — имитация Chrome; `httpx` (библиотека из ТЗ) доступен как `WB_HTTP_CLIENT=httpx`.
2. **Браузерные заголовки** (Accept-Language, Referer, Origin, Sec-Fetch-*) — UA и sec-ch-ua выставляет имитация.
3. **Цепочка эндпоинтов**: `card.wb.ru` → при 403/пустоте → `search.wb.ru` (поиск по артикулу).
4. **Прокси**: `WB_PROXY=http://user:pass@host:port` или `socks5://…` (http/socks5).
5. **`/diag`** — команда бота: проверяет доступность эндпоинтов с вашего IP.

**Если ваш IP заблокирован** (403/429/498 на `/diag`), реальные цены появятся после:
- настройки `WB_PROXY` (прокси/VPN с «чистым» IP) и `WB_DEMO_MODE=0`, **или**
- использования официального **WB Seller API** (`content-api.wildberries.ru`) — требует токен продавца, вне ТЗ.

Запуск из корня с прокси: `WB_PROXY=socks5://127.0.0.1:1080 WB_DEMO_MODE=0 python bot.py`.

