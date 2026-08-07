"""Асинхронный клиент к публичным API Wildberries с антибот-устойчивостью.

Честный статус (проверено эмпирически с рабочего IP, 2026-08-06):
  - card.wb.ru/cards/v1/detail  -> HTTP 403 всегда (блок на уровне эджа «Angie»).
    Заголовки, куки и даже полная имитация TLS/HTTP2-отпечатка Chrome
    (curl_cffi impersonate) не помогают — это IP-блок, не проверка отпечатка.
  - search.wb.ru/.../v4/search  -> через curl_cffi проходит к приложению
    (HTTP 200), но с заблокированного IP отдаёт пустой фейковый ответ;
    обычные httpx/curl ловят 429 (не проходят эдж по отпечатку).
  - www.wildberries.ru          -> HTTP 498.

Вывод: с IP, заблокированного эджем WB, реальные цены получить нельзя.
Рабочие варианты:
  1) Прокси/VPN с «чистым» IP: задайте WB_PROXY (поддерживается http/socks5).
  2) Официальный Seller API (content-api.wildberries.ru) с токеном продавца.

Клиент реализует максимум, что возможно на уровне HTTP-клиента:
  - транспорт curl_cffi (имитация Chrome) по умолчанию, httpx — как fallback
    (библиотека из ТЗ; на «чистом» IP достаточно и его);
  - браузерные заголовки (Accept-Language, Referer, Origin, Sec-Fetch-*);
  - цепочка эндпоинтов: card.wb.ru -> search.wb.ru (поиск по артикулу);
  - retry на 429/5xx/сетевые ошибки с экспоненциальным backoff (stdlib);
  - поддержка прокси;
  - diagnose() — проверка доступности эндпоинтов для команды /diag.

Цены в ответе приходят в КОПЕЙКАХ (priceU / salePriceU) — делим на 100.
"""
from __future__ import annotations

import asyncio
import json
import logging

import config

logger = logging.getLogger(__name__)

CARD_API_URL = "https://card.wb.ru/cards/v1/detail"
SEARCH_API_URL = "https://search.wb.ru/exactmatch/ru/common/v4/search"
WWW_URL = "https://www.wildberries.ru/"

DEFAULT_PARAMS = {
    "appType": "1",       # тип клиента: веб
    "curr": "rub",        # валюта
    "dest": "-1257786",   # регион доставки (код Москвы); коды могут меняться
    "spp": "30",          # уровень скидки по подписке
}
# Цены WB приходят в КОПЕЙКАХ (priceU / salePriceU) — деление на 100 делаем
# в bot.py при выводе пользователю.

# Заголовки «как у браузера». User-Agent и sec-ch-ua НЕ задаём руками:
# их выставляет имитация Chrome в curl_cffi (ручной UA сломал бы отпечаток).
BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.wildberries.ru/",
    "Origin": "https://www.wildberries.ru",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# Коды, на которые имеет смысл повторить запрос: 429 (rate limit) и 5xx.
# 403/498 (IP-блок эджа) не ретраим — повтор не поможет.
_RETRY_STATUSES = {429, *range(500, 600)}


def _backoff(attempt: int, min_delay: float = 0.5) -> float:
    """Экспоненциальная задержка: 0.5, 1.0, 2.0 … (потолок 10 c)."""
    return min(min_delay * (2 ** (attempt - 1)), 10.0)


# ------------------------------------------------------------------ транспорты

try:
    from curl_cffi.requests import AsyncSession as CurlCffiSession

    HAS_CURL_CFFI = True
except ImportError:  # pragma: no cover
    CurlCffiSession = None
    HAS_CURL_CFFI = False


class HttpxTransport:
    """Транспорт на httpx (библиотека из ТЗ; без имитации отпечатка)."""

    def __init__(self, timeout: float = 20.0, proxy: str = "") -> None:
        import httpx

        self._client = httpx.AsyncClient(timeout=timeout, proxy=proxy or None)

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        resp = await self._client.get(url, params=params, headers=headers)
        return resp.status_code, resp.text

    async def aclose(self) -> None:
        await self._client.aclose()


class CurlCffiTransport:
    """Транспорт на curl_cffi: имитация TLS/HTTP2-отпечатка Chrome.

    Единственное, что прошло через эдж-фильтр WB (search.wb.ru вернул 200
    вместо 429/403 у обычных клиентов). Поддерживает прокси (http/socks5).
    """

    def __init__(
        self,
        timeout: float = 20.0,
        impersonate: str = "chrome",
        proxies: dict | None = None,
    ) -> None:
        self._session = CurlCffiSession(
            impersonate=impersonate,
            timeout=timeout,
            proxies=proxies,
        )

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        resp = await self._session.get(url, params=params, headers=headers)
        return resp.status_code, resp.text

    async def aclose(self) -> None:
        # В curl_cffi 0.16 метод называется close(); aclose() появился позже.
        closer = getattr(self._session, "aclose", None) or self._session.close
        await closer()


def _make_transport() -> HttpxTransport | CurlCffiTransport:
    """Выбирает транспорт по WB_HTTP_CLIENT (curl_cffi по умолчанию).

    Прокси (config.PROXY) применяется в ОБОИХ транспортах, чтобы не было
    ситуации «прокси молча игнорируется».
    """
    if config.HTTP_CLIENT == "curl_cffi":
        if not HAS_CURL_CFFI:
            logger.warning(
                "WB_HTTP_CLIENT=curl_cffi, но библиотека не установлена. "
                "Падаем обратно на httpx. Установите: pip install curl_cffi"
            )
        else:
            proxies = None
            if config.PROXY:
                proxies = {"http": config.PROXY, "https": config.PROXY}
            return CurlCffiTransport(proxies=proxies)
    return HttpxTransport(proxy=config.PROXY)


# ------------------------------------------------------------------- клиент

class WBClient:
    """Клиент к публичным API WB с цепочкой эндпоинтов и retry.

    get_card(): card.wb.ru -> при 403/пустоте пробует search.wb.ru.
    Отдаёт карточку товара в формате «продукта» WB (поля id/name/priceU/
    salePriceU/qty) или None.
    """

    def __init__(self, transport=None, max_retries: int | None = None) -> None:
        self._transport = transport if transport is not None else _make_transport()
        self._max_retries = max_retries if max_retries is not None else config.MAX_RETRIES

    async def _get(self, url: str, *, params=None, retries: int | None = None) -> tuple[int, str]:
        """GET с retry на 429/5xx и сетевые ошибки; возвращает (status, text).

        retries=None — использовать WB_MAX_RETRIES; для диагностики можно
        передать 1 (одиночный запрос без ретраев, чтобы /diag не висел).
        """
        max_retries = self._max_retries if retries is None else retries
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                status, text = await self._transport.get(
                    url, params=params, headers=BROWSER_HEADERS
                )
            except Exception as exc:  # сетевая ошибка любого транспорта
                last_exc = exc
                if attempt == self._max_retries:
                    raise
                await asyncio.sleep(_backoff(attempt))
                continue

            if status in _RETRY_STATUSES and attempt < max_retries:
                await asyncio.sleep(_backoff(attempt))
                continue
            return status, text

        raise last_exc if last_exc is not None else RuntimeError("unreachable")

    @staticmethod
    def _extract_product(data: dict) -> dict | None:
        """Достаёт первый продукт из ответа WB (устойчив к payload/data)."""
        products = (data.get("payload") or data.get("data") or {}).get("products") or []
        return products[0] if products else None

    async def _try_card_api(self, articul: int) -> dict | None:
        params = {**DEFAULT_PARAMS, "nm": str(articul)}
        status, text = await self._get(CARD_API_URL, params=params)
        if status != 200:
            logger.info("card.wb.ru -> HTTP %s для %s (вероятный IP-блок)", status, articul)
            return None
        try:
            return self._extract_product(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            logger.warning("card.wb.ru вернул не-JSON для %s", articul)
            return None

    async def _try_search(self, articul: int) -> dict | None:
        params = {
            **DEFAULT_PARAMS,
            "query": str(articul),
            "resultset": "catalog",
            "sort": "popular",
            "page": "1",
        }
        status, text = await self._get(SEARCH_API_URL, params=params)
        if status != 200:
            logger.warning("search.wb.ru -> HTTP %s для %s", status, articul)
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("search.wb.ru вернул не-JSON для %s", articul)
            return None
        product = self._extract_product(data)
        if product is None:
            # HTTP 200 + пустой «state» — известный софт-блок WB для чужих IP
            logger.warning(
                "search.wb.ru вернул пустой ответ (200) для %s — софт-блок IP. "
                "Решение: WB_PROXY с «чистым» IP или официальный Seller API.",
                articul,
            )
        return product

    async def get_card(self, articul: int) -> dict | None:
        product = await self._try_card_api(articul)
        if product is not None:
            return product
        return await self._try_search(articul)

    async def diagnose(self) -> list[tuple[str, str]]:
        """Проверяет доступность основных эндпоинтов WB (для команды /diag).

        Одиночные запросы без ретраев, чтобы команда не висела на 429.
        Для search.wb.ru распознаём софт-блок: HTTP 200, но пустой ответ
        (с заблокированного IP WB отдаёт «фейковую» пустоту вместо данных).
        """
        checks = [
            ("card.wb.ru", CARD_API_URL, {**DEFAULT_PARAMS, "nm": "100358932"}),
            (
                "search.wb.ru",
                SEARCH_API_URL,
                {**DEFAULT_PARAMS, "query": "100358932", "resultset": "catalog"},
            ),
            ("www.wildberries.ru", WWW_URL, None),
        ]
        results: list[tuple[str, str]] = []
        for name, url, params in checks:
            try:
                status, text = await self._get(url, params=params, retries=1)
                if status == 403:
                    results.append((name, "HTTP 403 — IP заблокирован эджем"))
                elif status == 429:
                    results.append((name, "HTTP 429 — rate limit"))
                elif status == 200 and name.startswith("search"):
                    try:
                        data = json.loads(text)
                        has_products = bool((data.get("data") or {}).get("products"))
                    except (json.JSONDecodeError, TypeError):
                        has_products = False
                    if has_products:
                        results.append((name, "HTTP 200 — доступен, данные есть"))
                    else:
                        results.append((name, "HTTP 200, но пустой ответ — софт-блок IP"))
                elif status == 200:
                    results.append((name, "HTTP 200 — доступен"))
                else:
                    results.append((name, f"HTTP {status} — недоступен"))
            except Exception as exc:
                results.append((name, f"ошибка: {type(exc).__name__}: {exc}"))
        return results

    async def aclose(self) -> None:
        await self._transport.aclose()


# ------------------------------------------------------------ демо-режим

class MockWBClient:
    """Демо-режим: не ходит в сеть, отдаёт выдуманную карточку товара."""

    async def get_card(self, articul: int) -> dict:
        return {
            "id": articul,
            "name": f"Тестовый товар {articul} (mock)",
            "brand": "MockBrand",
            "priceU": 99900,
            "salePriceU": 69900,
            "sale": 30,
            "qty": 12,
            "totalQuantity": 12,
            "root": articul + 100000,
        }

    async def aclose(self) -> None:
        return None
