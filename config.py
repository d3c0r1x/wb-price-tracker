"""Конфигурация бота через переменные окружения (stdlib os.getenv)."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BOT_TOKEN = os.getenv("WB_BOT_TOKEN", "")
DB_PATH = os.getenv("WB_DB_PATH", os.path.join(BASE_DIR, "tracker.db"))
# Демо-режим: не ходит в сеть, отдаёт выдуманные данные (полезно, если WB блокирует запросы)
DEMO_MODE = os.getenv("WB_DEMO_MODE", "0") == "1"
# Периодичность проверки цен (раз в сутки = 1440 минут, как в ТЗ)
CHECK_INTERVAL_MINUTES = int(os.getenv("WB_CHECK_INTERVAL_MINUTES", "1440"))
# Порог остатка: если остаток меньше или равен — считаем, что товар заканчивается
LOW_STOCK_THRESHOLD = int(os.getenv("WB_LOW_STOCK_THRESHOLD", "5"))
# Пауза между запросами к WB при массовой проверке (антибот-защита)
REQUEST_DELAY_SECONDS = float(os.getenv("WB_REQUEST_DELAY_SECONDS", "0.5"))

# --- Антибот-обход (см. README, раздел «Антибот-обход») ---
# Транспорт HTTP-запросов к WB:
#   curl_cffi — имитация TLS/HTTP2-отпечатка Chrome (обходит эдж-фильтр по отпечатку,
#               по умолчанию; единственное, что прошло к приложению с заблокированного IP)
#   httpx     — стандартный клиент (библиотека из ТЗ; подходит с «чистого» IP)
HTTP_CLIENT = os.getenv("WB_HTTP_CLIENT", "curl_cffi")
# Прокси для запросов к WB, напр. http://user:pass@host:port или socks5://host:1080
# Нужен, если ваш IP заблокирован эджем WB (HTTP 403/498 на card.wb.ru)
PROXY = os.getenv("WB_PROXY", "")
# Сколько попыток сделать на 429/5xx/сетевые ошибки (экспоненциальный backoff)
MAX_RETRIES = int(os.getenv("WB_MAX_RETRIES", "3"))

# --- Продвинутый уровень: кулдаун алертов, очистка БД, троттлинг ---
# Не уведомлять об одном товаре чаще, чем раз в N часов (защита от спама)
ALERT_COOLDOWN_HOURS = float(os.getenv("WB_ALERT_COOLDOWN_HOURS", "6"))
# Хранить историю цен N дней, потом чистить (контроль роста БД)
HISTORY_KEEP_DAYS = int(os.getenv("WB_HISTORY_KEEP_DAYS", "30"))
# Минимальный интервал между сообщениями одного пользователя (секунды)
THROTTLE_MIN_INTERVAL = float(os.getenv("WB_THROTTLE_MIN_INTERVAL", "0.7"))
# ID администраторов (через запятую) — доступ к /cleanup; пусто = всем можно
ADMIN_IDS = [int(x) for x in os.getenv("WB_ADMIN_IDS", "").split(",") if x.strip().isdigit()]
# TTL кэша карточек WB (секунды): повторный /track того же артикула мгновенный
CACHE_TTL_SECONDS = float(os.getenv("WB_CACHE_TTL_SECONDS", "300"))
