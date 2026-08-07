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
