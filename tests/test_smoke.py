"""Smoke-тесты: БД (aiosqlite) и клиент WB (mock). Запуск: python -m pytest tests"""
import asyncio
import json
import os
import tempfile

from db import Database
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


def test_mock_card_prices_in_kopecks() -> None:
    """Цены WB приходят в копейках — проверяем деление на 100."""
    card = asyncio.run(MockWBClient().get_card(ARTICUL))
    assert card["salePriceU"] // 100 == 699
    assert card["priceU"] // 100 == 999


def test_db_roundtrip() -> None:
    """Полный цикл: добавление карточки, трекинг, история, отписка."""
    async def run() -> None:
        db = Database(os.path.join(tempfile.gettempdir(), "pytest_tracker.db"))
        if os.path.exists(db.path):
            os.remove(db.path)
        await db.init()
        card = await MockWBClient().get_card(ARTICUL)
        await db.upsert_item(card)
        await db.track(111, str(ARTICUL))
        assert await db.list_tracked(111)
        assert await db.history(str(ARTICUL))
        assert await db.users_for_articul(str(ARTICUL)) == [111]
        assert await db.untrack(111, str(ARTICUL))

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
    assert card["id"] == ARTICUL
    assert len(transport.calls) == 2  # сначала card, затем search
    assert "search.wb.ru" in transport.calls[1]


def test_no_product_returns_none() -> None:
    """Если оба эндпоинта не дали товара — возвращаем None без исключения."""
    transport = FakeTransport([(403, "blocked"), (200, json.dumps({"data": {"products": []}}))])
    client = WBClient(transport=transport, max_retries=2)
    assert asyncio.run(client.get_card(ARTICUL)) is None
