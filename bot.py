"""Telegram-бот "WB Price & Stock Tracker".

Стек (строго по ТЗ): aiogram (v3.x), httpx, aiosqlite, asyncio, apscheduler
(в ТЗ предложено "Celery или apscheduler" — выбран apscheduler, т.к. работает
в том же процессе и не требует брокера вроде Redis).

Запуск:  python bot.py   (предварительно задайте WB_BOT_TOKEN)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Экранирование HTML для Telegram. aiogram.utils.html удалён из aiogram 3.30,
# поэтому используем стандартный html.escape из stdlib.
import html as _html

from config import (
    BASE_DIR,
    BOT_TOKEN,
    CHECK_INTERVAL_MINUTES,
    DB_PATH,
    DEMO_MODE,
    LOW_STOCK_THRESHOLD,
    REQUEST_DELAY_SECONDS,
)
from db import Database
from wb_api import MockWBClient, WBClient

# Логирование в консоль и в файл bot.log рядом с ботом
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

router = Router()
db = Database(DB_PATH)
wb: WBClient | MockWBClient | None = None


# ---------------------------------------------------------------- команды

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот <b>WB Price &amp; Stock Tracker</b>.\n\n"
        "Команды:\n"
        "• /track <b>АРТИКУЛ</b> — начать отслеживание товара\n"
        "• /list — мои товары\n"
        "• /untrack <b>АРТИКУЛ</b> — удалить из отслеживания\n"
        "• /history <b>АРТИКУЛ</b> — история цен\n"
        "• /diag — диагностика доступа к API Wildberries\n\n"
        "Раз в сутки бот проверит цены и пришлёт уведомление, если цена упала "
        "или товар заканчивается."
    )


@router.message(Command("track"))
async def cmd_track(message: Message) -> None:
    articul = _extract_articul(message.text or "")
    if articul is None:
        await message.answer("Формат: /track АРТИКУЛ (например, /track 17457977)")
        return
    try:
        card = await wb.get_card(articul)
    except Exception as exc:  # сеть, 403 антибот и т.п.
        await message.answer(f"⚠️ Не удалось получить данные Wildberries: {exc}")
        return
    if card is None:
        await message.answer(f"Товар с артикулом <b>{articul}</b> не найден.")
        return
    await db.upsert_item(card)
    await db.track(message.from_user.id, str(articul))
    price = int(card.get("priceU", 0) or 0) // 100
    sale_price = int(card.get("salePriceU", price) or price) // 100
    await message.answer(
        "✅ Товар добавлен в отслеживание:\n"
        f"<b>{_html.escape(str(card.get('name')), quote=False)}</b>\n"
        f"Артикул: <code>{articul}</code>\n"
        f"Цена: <s>{price} ₽</s> <b>{sale_price} ₽</b>\n"
        f"Остаток: {card.get('qty', 0)} шт."
    )


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    items = await db.list_tracked(message.from_user.id)
    if not items:
        await message.answer("У вас пока нет отслеживаемых товаров. /track АРТИКУЛ")
        return
    lines = [
        f"• <code>{i['articul']}</code> — {_html.escape(i['title'][:40], quote=False)}: "
        f"<b>{i['sale_price']} ₽</b>, остаток {i['qty']} шт."
        for i in items
    ]
    await message.answer("📦 <b>Отслеживаемые товары:</b>\n" + "\n".join(lines))


@router.message(Command("untrack"))
async def cmd_untrack(message: Message) -> None:
    articul = _extract_articul(message.text or "")
    if articul is None:
        await message.answer("Формат: /untrack АРТИКУЛ")
        return
    removed = await db.untrack(message.from_user.id, str(articul))
    if removed:
        await message.answer(f"Артикул <code>{articul}</code> удалён из отслеживания.")
    else:
        await message.answer(f"Артикул <code>{articul}</code> у вас не отслеживался.")


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    articul = _extract_articul(message.text or "")
    if articul is None:
        await message.answer("Формат: /history АРТИКУЛ")
        return
    rows = await db.history(str(articul))
    if not rows:
        await message.answer(f"Истории по артикулу <code>{articul}</code> нет.")
        return
    lines = [
        f"• {r['checked_at']} — <b>{r['sale_price']} ₽</b> (было {r['price']} ₽), "
        f"остаток {r['qty']} шт."
        for r in rows[:10]
    ]
    await message.answer(f"📈 <b>История цен</b> ({articul}):\n" + "\n".join(lines))


@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    """Диагностика доступности эндпоинтов WB (антибот-статус с этого IP)."""
    mode_note = (
        "бот в <b>демо-режиме</b> (WB_DEMO_MODE=1): данные выдуманные, "
        "но диагностика ходит в сеть по-настоящему"
        if DEMO_MODE
        else "бот в <b>реальном режиме</b>"
    )
    await message.answer(f"🔍 <b>Диагностика WB API</b>\n{mode_note}\n\nПроверяю…")
    tmp: WBClient | None = None
    client = wb
    if isinstance(client, MockWBClient):
        tmp = WBClient()  # в демо-режиме создаём реальный клиент только для диагностики
        client = tmp
    try:
        rows = await client.diagnose()
        lines = "\n".join(f"• <code>{name}</code> → <b>{status}</b>" for name, status in rows)
        await message.answer(
            lines
            + "\n\nЕсли везде 403/429/498 — ваш IP заблокирован эджем WB. "
            "Решение: задайте <code>WB_PROXY</code> (прокси с «чистым» IP) и "
            "перезапустите бота, либо используйте официальный Seller API."
        )
    except Exception as exc:
        await message.answer(f"⚠️ Ошибка диагностики: {exc}")
    finally:
        if tmp is not None:
            await tmp.aclose()


# ------------------------------------------------------------- фоновая проверка

async def check_prices(bot: Bot) -> None:
    """Ежедневная проверка: уведомляем, если цена упала или товар заканчивается."""
    items = await db.all_items()
    if not items:
        return
    for articul, title, last_price, last_qty in items:
        try:
            card = await wb.get_card(int(articul))
        except Exception as exc:
            logger.warning("Ошибка проверки %s: %s", articul, exc)
            continue
        if card is None:
            continue
        price = int(card.get("priceU", 0) or 0) // 100
        sale_price = int(card.get("salePriceU", price) or price) // 100
        qty = int(card.get("qty", 0) or 0)

        alerts: list[str] = []
        if last_price is not None and sale_price < last_price:
            alerts.append(f"📉 <b>Цена упала!</b> Было {last_price} ₽ → стало {sale_price} ₽")
        if last_qty is not None and qty <= LOW_STOCK_THRESHOLD and qty < last_qty:
            alerts.append(f"⚠️ <b>Товар заканчивается!</b> Осталось {qty} шт.")

        if alerts:
            for user_id in await db.users_for_articul(articul):
                try:
                    await bot.send_message(
                        user_id,
                        f"🔔 <b>{_html.escape(title, quote=False)}</b> ({articul})\n" + "\n".join(alerts),
                    )
                except Exception as exc:
                    logger.warning("Не удалось отправить уведомление %s: %s", user_id, exc)
            await db.update_last_notified(articul, sale_price, qty)

        await db.upsert_item(card)  # фиксируем новый снимок цены в истории
        # Пауза между запросами к WB, чтобы не словить rate-limit/бан
        await asyncio.sleep(REQUEST_DELAY_SECONDS)


# --------------------------------------------------------------------- utils

def _extract_articul(text: str) -> int | None:
    """Достаёт артикул из текста (число из 4+ цифр, выдерживает ссылки вида nm=1234)."""
    match = re.search(r"(?:\bnm=)?(\d{4,})", text)
    return int(match.group(1)) if match else None


async def main() -> None:
    global wb
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан WB_BOT_TOKEN. Скопируйте .env.example и задайте токен "
            "(например: export WB_BOT_TOKEN=... в bash или setx в Windows)."
        )
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    wb = MockWBClient() if DEMO_MODE else WBClient()
    await db.init()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_prices,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="daily_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Бот запущен. Демо-режим: %s. Проверка каждые %s мин.",
                DEMO_MODE, CHECK_INTERVAL_MINUTES)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await wb.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
