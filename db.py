"""Локальная SQLite-БД через aiosqlite: товары, подписки, история цен.

Продвинутый уровень:
  - индексы на колонки, по которым идёт поиск (articul, checked_at);
  - last_alert_at в карточке — для кулдауна уведомлений (см. alerts.py);
  - cleanup_history() — плановая очистка истории старше N дней;
  - stats() — сводка по базе для команды /stats.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        """Создаёт таблицы и индексы при первом запуске."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
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
                    last_alert_at TEXT,    -- когда последний раз уведомили
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
            # Индексы: поиск по артикулу в истории и по tracked.articul
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_articul "
                "ON price_history (articul)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tracked_articul ON tracked (articul)"
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
                    (articul, title, price, sale_price, qty, last_checked,
                     last_price, last_qty, last_alert_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
                "INSERT INTO price_history (articul, price, sale_price, qty, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (articul, price, sale_price, qty, _now()),
            )
            await db.commit()

    async def update_last_notified(self, articul: str, sale_price: int, qty: int) -> None:
        """После отправки уведомления запоминает текущие значения и время."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE items SET last_price = ?, last_qty = ?, last_alert_at = ? "
                "WHERE articul = ?",
                (sale_price, qty, _now(), articul),
            )
            await db.commit()

    async def get_last_alert(self, articul: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT last_alert_at FROM items WHERE articul = ?", (articul,)
            )
            row = await cur.fetchone()
        return row[0] if row else None

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

    async def cleanup_history(self, keep_days: int) -> int:
        """Удаляет историю цен старше keep_days дней, возвращает число строк."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
            timespec="seconds"
        )
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM price_history WHERE checked_at < ?", (cutoff,)
            )
            await db.commit()
            return cur.rowcount

    async def cleanup_orphans(self) -> int:
        """Удаляет карточки товаров, за которыми никто не следит.

        После /untrack карточка остаётся в items (история цен чистится отдельно
        по возрасту) — без этой очистки таблица items росла бы бесконечно.
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM items WHERE articul NOT IN "
                "(SELECT DISTINCT articul FROM tracked)"
            )
            await db.commit()
            return cur.rowcount

    async def stats(self) -> dict:
        """Сводка по базе: товары, подписки, записи истории, пользователи."""
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT COUNT(*) FROM items") as cur:
                items = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM tracked") as cur:
                tracked = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM price_history") as cur:
                history = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(DISTINCT user_id) FROM tracked"
            ) as cur:
                users = (await cur.fetchone())[0]
        return {"items": items, "tracked": tracked, "history": history, "users": users}
