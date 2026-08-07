"""Локальная SQLite-БД через aiosqlite: товары, подписки пользователей, история цен."""
from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        """Создаёт таблицы при первом запуске."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS items (
                    articul      TEXT PRIMARY KEY,
                    title        TEXT,
                    price        INTEGER,
                    sale_price   INTEGER,
                    qty          INTEGER,
                    last_checked TEXT,
                    last_price   INTEGER,  -- цена, о которой уже уведомили
                    last_qty     INTEGER,  -- остаток, о котором уже уведомили
                    created_at   TEXT
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked (
                    user_id INTEGER,
                    articul TEXT,
                    PRIMARY KEY (user_id, articul)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS price_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    articul    TEXT,
                    price      INTEGER,
                    sale_price INTEGER,
                    qty        INTEGER,
                    checked_at TEXT
                )
                """
            )
            await db.commit()

    async def upsert_item(self, card: dict) -> None:
        """Сохраняет/обновляет карточку товара и пишет запись в историю цен."""
        articul = str(card["id"])
        price = int(card.get("priceU", 0) or 0) // 100
        sale_price = int(card.get("salePriceU", price) or price) // 100
        qty = int(card.get("qty", 0) or 0)
        title = card.get("name") or ""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO items
                    (articul, title, price, sale_price, qty, last_checked, last_price, last_qty, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(articul) DO UPDATE SET
                    title = excluded.title,
                    price = excluded.price,
                    sale_price = excluded.sale_price,
                    qty = excluded.qty,
                    last_checked = excluded.last_checked
                """,
                (articul, title, price, sale_price, qty, _now(), sale_price, qty, _now()),
            )
            await db.execute(
                "INSERT INTO price_history (articul, price, sale_price, qty, checked_at) VALUES (?, ?, ?, ?, ?)",
                (articul, price, sale_price, qty, _now()),
            )
            await db.commit()

    async def update_last_notified(self, articul: str, sale_price: int, qty: int) -> None:
        """После отправки уведомления запоминает текущие значения, чтобы не спамить."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE items SET last_price = ?, last_qty = ? WHERE articul = ?",
                (sale_price, qty, articul),
            )
            await db.commit()

    async def track(self, user_id: int, articul: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO tracked (user_id, articul) VALUES (?, ?)",
                (user_id, articul),
            )
            await db.commit()

    async def untrack(self, user_id: int, articul: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM tracked WHERE user_id = ? AND articul = ?",
                (user_id, articul),
            )
            await db.commit()
            return cur.rowcount > 0

    async def list_tracked(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """
                SELECT i.articul, i.title, i.price, i.sale_price, i.qty
                FROM tracked t JOIN items i ON i.articul = t.articul
                WHERE t.user_id = ?
                ORDER BY i.created_at DESC
                """,
                (user_id,),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def users_for_articul(self, articul: str) -> list[int]:
        """Все пользователи, отслеживающие артикул."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT user_id FROM tracked WHERE articul = ?", (articul,)
            )
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def all_items(self) -> list[tuple]:
        """(articul, title, last_price, last_qty) по всем отслеживаемым товарам."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                """
                SELECT DISTINCT i.articul, i.title, i.last_price, i.last_qty
                FROM items i JOIN tracked t ON t.articul = i.articul
                """
            )
            rows = await cur.fetchall()
        return [(str(r[0]), str(r[1] or ""), r[2], r[3]) for r in rows]

    async def history(self, articul: str, limit: int = 20) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM price_history WHERE articul = ? ORDER BY id DESC LIMIT ?",
                (articul, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]
