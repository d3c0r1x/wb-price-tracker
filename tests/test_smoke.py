"""Тесты P1: БД (aiosqlite), клиент WB, продвинутые утилиты и мидлвары.

Запуск: python -m pytest tests -q
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from alerts import should_notify
from db import Database
from middlewares import ThrottlingMiddleware
from utils import TTLCache
from wb_api import MockWBClient, WBClient

ARTICUL = 17457977


class FakeTransport:
    """Транспорт-заглушка: отдаёт заранее заданные ответы по порядку."""

    def __init__(self, responses: list[tuple[int, str]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def get(self, url: str, *, params=None, headers=None) -> tuple[int, str]:
        self.calls.append(url)
        if not self.responses:
            raise RuntimeError("FakeTransport: пустой список ответов")
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]

    async def aclose(self) -> None:
        return None


# ------------------------------------------------------------ базовые smoke-тесты

def test_mock_card_prices_in_kopecks() -> None:
    """Цены WB приходят в копейках — проверяем деление на 100."""
    card = asyncio.run(MockWBClient().get_card(ARTICUL))
    assert card["salePriceU"] // 100 == 699
    assert card["priceU"] // 100 == 999


def test_db_roundtrip(tmp_path) -> None:
    """Полный цикл: добавление карточки, трекинг, история, отписка, статистика."""
    async def run() -> None:
        db = Database(str(tmp_path / "tracker.db"))
        await db.init()
        card = await MockWBClient().get_card(ARTICUL)
        await db.upsert_item(card)
        await db.track(111, str(ARTICUL))
        assert await db.list_tracked(111)
        assert await db.history(str(ARTICUL))
        assert await db.users_for_articul(str(ARTICUL)) == [111]
        assert await db.untrack(111, str(ARTICUL))
        s = await db.stats()
        assert s["items"] >= 1 and s["history"] >= 1

    asyncio.run(run())


def test_extract_product_payload_and_data() -> None:
    """Парсер устойчив к смене структуры ответа WB (payload vs data)."""
    client = WBClient(transport=FakeTransport([]))
    assert client._extract_product({"payload": {"products": [{"id": 1}]}}) == {"id": 1}
    assert client._extract_product({"data": {"products": [{"id": 2}]}}) == {"id": 2}
    assert client._extract_product({"data": {"products": []}}) is None
    assert client._extract_product({"error": "blocked"}) is None


def test_fallback_to_search_when_card_blocked() -> None:
    """Если card.wb.ru отдаёт 403 (IP-блок), клиент пробует search.wb.ru."""
    card_blocked = (403, "403 Forbidden")
    search_ok = (
        200,
        json.dumps(
            {
                "data": {
                    "products": [
                        {
                            "id": ARTICUL,
                            "name": "Настоящий товар",
                            "priceU": 99900,
                            "salePriceU": 69900,
                            "qty": 3,
                        }
                    ]
                }
            }
        ),
    )
    transport = FakeTransport([card_blocked, search_ok])
    client = WBClient(transport=transport, max_retries=2)
    card = asyncio.run(client.get_card(ARTICUL))
    assert card is not None
    assert card["salePriceU"] == 69900  # копейки
    assert len(transport.calls) == 2  # сначала card, затем search
    assert "search.wb.ru" in transport.calls[1]


def test_no_product_returns_none() -> None:
    """Если оба эндпоинта не дали товара — возвращаем None без исключения."""
    transport = FakeTransport([(403, "blocked"), (200, json.dumps({"data": {"products": []}}))])
    client = WBClient(transport=transport, max_retries=2)
    assert asyncio.run(client.get_card(ARTICUL)) is None


# ------------------------------------------------------------ продвинутый уровень

def test_ttl_cache() -> None:
    """Значение живёт ttl секунд, потом пересчитывается."""
    async def run() -> None:
        cache = TTLCache(ttl_seconds=0.05)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return calls

        assert await cache.get_or_set("k", factory) == 1
        assert await cache.get_or_set("k", factory) == 1  # из кэша
        assert calls == 1
        await asyncio.sleep(0.08)
        assert await cache.get_or_set("k", factory) == 2  # TTL истёк
        cache.invalidate("k")
        assert await cache.get_or_set("k", factory) == 3  # явный сброс

    asyncio.run(run())


class _FakeUser:
    id = 1


class _FakeEvent:
    from_user = _FakeUser()


async def _dummy_handler(event, data):
    return "ok"


def test_throttling_middleware() -> None:
    """Второе сообщение в пределах интервала дропается."""
    async def run() -> None:
        mw = ThrottlingMiddleware(min_interval=60.0)
        assert await mw(_dummy_handler, _FakeEvent(), {}) == "ok"
        assert await mw(_dummy_handler, _FakeEvent(), {}) is None  # дропнут

    asyncio.run(run())


def test_should_notify_logic() -> None:
    """Чистая логика алертов: падение цены, заканчивается, кулдаун."""
    now = datetime.now(timezone.utc)

    # падение цены — уведомляем
    notify, msgs = should_notify(100, 10, last_price=120, last_qty=10,
                                 last_alert_at=None, now=now)
    assert notify and len(msgs) == 1 and "упала" in msgs[0]

    # товар заканчивается
    notify, msgs = should_notify(100, 2, last_price=100, last_qty=10,
                                 last_alert_at=None, now=now)
    assert notify and "заканчивается" in msgs[0]

    # цена выросла и остаток норм — тишина
    notify, _ = should_notify(130, 10, last_price=120, last_qty=10,
                              last_alert_at=None, now=now)
    assert not notify

    # кулдаун: уведомляли час назад — молчим
    alert_1h_ago = (now - timedelta(hours=1)).isoformat()
    notify, _ = should_notify(100, 10, last_price=120, last_qty=10,
                              last_alert_at=alert_1h_ago, now=now, cooldown_hours=6)
    assert not notify

    # кулдаун истёк (7 часов назад) — уведомляем снова
    alert_7h_ago = (now - timedelta(hours=7)).isoformat()
    notify, _ = should_notify(100, 10, last_price=120, last_qty=10,
                              last_alert_at=alert_7h_ago, now=now, cooldown_hours=6)
    assert notify


def test_cleanup_history(tmp_path) -> None:
    """История старше N дней удаляется, свежая остаётся."""
    async def run() -> None:
        db = Database(str(tmp_path / "tracker.db"))
        await db.init()
        # пишем запись напрямую с «древней» датой
        import aiosqlite
        async with aiosqlite.connect(db.path) as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
            await conn.execute(
                "INSERT INTO price_history (articul, price, sale_price, qty, checked_at) "
                "VALUES ('111', 100, 90, 5, ?)", (old,)
            )
            await conn.commit()
        card = await MockWBClient().get_card(ARTICUL)
        await db.upsert_item(card)  # свежая запись

        deleted = await db.cleanup_history(keep_days=30)
        assert deleted == 1  # удалилась только древняя
        assert len(await db.history(str(ARTICUL))) == 1  # свежая на месте

    asyncio.run(run())

def test_prices_from_card_falls_back_when_sale_price_zero() -> None:
    """salePriceU=0 (нет скидки) не должен давать цену 0 — fallback на priceU."""
    from bot import _prices_from_card

    price, sale_price = _prices_from_card({"priceU": 99900, "salePriceU": 0})
    assert (price, sale_price) == (999, 999)  # копейки -> рубли, без «0 ₽»

    price, sale_price = _prices_from_card({"priceU": 99900, "salePriceU": 69900})
    assert (price, sale_price) == (999, 699)

    # salePriceU вовсе отсутствует — тоже fallback на priceU
    price, sale_price = _prices_from_card({"priceU": 123400})
    assert (price, sale_price) == (1234, 1234)
