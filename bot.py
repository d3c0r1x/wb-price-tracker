"""Telegram-бот "WB Price & Stock Tracker".

Стек (строго по ТЗ): aiogram (v3.x), httpx, aiosqlite, asyncio, apscheduler
(в ТЗ предложено "Celery или apscheduler" — выбран apscheduler, т.к. работает
в том же процессе и не требует брокера вроде Redis).

Продвинутый уровень:
  - middlewares: троттлинг (защита от спама) и логирование времени обработки;
  - TTL-кэш карточек WB — повторный запрос того же артикула мгновенный;
  - кулдаун уведомлений (не чаще раза в ALERT_COOLDOWN_HOURS по товару);
  - /stats — сводка по базе; /cleanup N — очистка истории старше N дней (админ);
  - индексы SQLite и плановая очистка истории — БД не растёт бесконечно.

Запуск:  python bot.py   (предварительно задайте WB_BOT_TOKEN)
"""
from __future__ import annotations

import asyncio
import datetime
import html as _html
import logging
import os
import re

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
from alerts import should_notify
from db import Database
from middlewares import LoggingMiddleware, ThrottlingMiddleware
from utils import TTLCache
from wb_api import MockWBClient, WBClient

# Логирование в консоль и в файл bot.log рядом с ботом
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(config.BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

router = Router()
db = Database(config.DB_PATH)
wb: WBClient | MockWBClient | None = None
card_cache = TTLCache(ttl_seconds=config.CACHE_TTL_SECONDS)


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
        "• /diag — диагностика доступа к API Wildberries\n"
        "• /stats — сводка по базе\n"
        "• /cleanup <b>ДНИ</b> — очистить историю старше N дней (админ)\n\n"
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
        card = await card_cache.get_or_set(articul, lambda: _fetch_card(articul))
    except Exception as exc:  # сеть, 403 антибот и т.п.
        await message.answer(f"⚠️ Не удалось получить данные Wildberries: {exc}")
        return
    if card is None:
        await message.answer(f"Товар с артикулом <b>{articul}</b> не найден.")
        return
    await db.upsert_item(card)
    await db.track(message.from_user.id, str(articul))
    price, sale_price = _prices_from_card(card)
    await message.answer(
        "✅ Товар добавлен в отслеживание:\n"
        f"<b>{_html.escape(str(card.get('name')), quote=False)}</b>\n"
        f"Артикул: <code>{articul}</code>\n"
        f"Цена: <s>{price} ₽</s> <b>{sale_price} ₽</b>\n"
        f"Остаток: {card.get('qty', 0)} шт."
    )


async def _fetch_card(articul: int):
    """Обёртка для TTL-кэша: обращается к клиенту WB (mock или реальному)."""
    return await wb.get_card(articul)


def _prices_from_card(card: dict) -> tuple[int, int]:
    """(обычная цена, цена со скидкой) в рублях из карточки WB (копейки).

    Если salePriceU равен 0 или отсутствует — берём priceU (товар без скидки).
    Иначе цена «упадёт» до 0 и бот будет слать ложные алерты о падении.
    """
    price = int(card.get("priceU") or 0) // 100
    sale_price = int(card.get("salePriceU") or card.get("priceU") or 0) // 100
    return price, sale_price


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


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    s = await db.stats()
    await message.answer(
        "📊 <b>Сводка по базе</b>\n\n"
        f"• Товаров в базе: <b>{s['items']}</b>\n"
        f"• Подписок (user×артикул): <b>{s['tracked']}</b>\n"
        f"• Пользователей: <b>{s['users']}</b>\n"
        f"• Записей истории цен: <b>{s['history']}</b>\n"
        f"• Записей в TTL-кэше: <b>{card_cache.size}</b>"
    )


@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message) -> None:
    """Очистка истории старше N дней (доступна администраторам из ADMIN_IDS)."""
    if config.ADMIN_IDS and message.from_user.id not in config.ADMIN_IDS:
        await message.answer("⛔ Команда доступна только администраторам.")
        return
    args = message.text.split()
    days = int(args[1]) if len(args) > 1 and args[1].isdigit() else config.HISTORY_KEEP_DAYS
    if days < 1:
        await message.answer("Дни должны быть больше нуля.")
        return
    deleted = await db.cleanup_history(days)
    await message.answer(f"🧹 Удалено записей истории старше {days} дн.: <b>{deleted}</b>")


@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    """Диагностика доступности эндпоинтов WB (антибот-статус с этого IP)."""
    mode_note = (
        "бот в <b>демо-режиме</b> (WB_DEMO_MODE=1): данные выдуманные, "
        "но диагностика ходит в сеть по-настоящему"
        if config.DEMO_MODE
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
    """Ежедневная проверка: уведомляем о падении цены/заканчивающемся товаре.

    Логика решения вынесена в чистую функцию alerts.should_notify() —
    покрыта unit-тестами. Кулдаун: один товар не «пикает» чаще раза в
    ALERT_COOLDOWN_HOURS.
    """
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
        _, sale_price = _prices_from_card(card)
        qty = int(card.get("qty", 0) or 0)

        notify, messages = should_notify(
            sale_price=sale_price,
            qty=qty,
            last_price=last_price,
            last_qty=last_qty,
            last_alert_at=await db.get_last_alert(articul),
            cooldown_hours=config.ALERT_COOLDOWN_HOURS,
            low_stock_threshold=config.LOW_STOCK_THRESHOLD,
        )
        if notify:
            for user_id in await db.users_for_articul(articul):
                try:
                    await bot.send_message(
                        user_id,
                        f"🔔 <b>{_html.escape(title, quote=False)}</b> ({articul})\n"
                        + "\n".join(messages),
                    )
                except Exception as exc:
                    logger.warning("Не удалось отправить уведомление %s: %s", user_id, exc)
            await db.update_last_notified(articul, sale_price, qty)

        await db.upsert_item(card)  # фиксируем новый снимок цены в истории
        await asyncio.sleep(config.REQUEST_DELAY_SECONDS)


# --------------------------------------------------------------------- utils

def _extract_articul(text: str) -> int | None:
    """Достаёт артикул из текста (число из 4+ цифр, выдерживает ссылки вида nm=1234)."""
    match = re.search(r"(?:\bnm=)?(\d{4,})", text)
    return int(match.group(1)) if match else None


async def main() -> None:
    global wb
    if not config.BOT_TOKEN:
        raise SystemExit(
            "Не задан WB_BOT_TOKEN. Скопируйте .env.example и задайте токен "
            "(например: export WB_BOT_TOKEN=... в bash или setx в Windows)."
        )
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    # Продвинутый уровень: мидлвары — троттлинг и логирование
    dp.message.middleware(ThrottlingMiddleware(min_interval=config.THROTTLE_MIN_INTERVAL))
    dp.update.middleware(LoggingMiddleware())

    wb = MockWBClient() if config.DEMO_MODE else WBClient()
    await db.init()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_prices,
        "interval",
        minutes=config.CHECK_INTERVAL_MINUTES,
        args=[bot],
        id="daily_check",
        replace_existing=True,
    )
    # Плановая очистка истории старше HISTORY_KEEP_DAYS дней (раз в сутки)
    scheduler.add_job(
        _scheduled_cleanup,
        "interval",
        hours=24,
        id="history_cleanup",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Бот запущен. Демо-режим: %s. Проверка каждые %s мин. Кулдаун алертов: %s ч.",
        config.DEMO_MODE, config.CHECK_INTERVAL_MINUTES, config.ALERT_COOLDOWN_HOURS,
    )
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await wb.aclose()
        await bot.session.close()


async def _scheduled_cleanup() -> None:
    deleted = await db.cleanup_history(config.HISTORY_KEEP_DAYS)
    if deleted:
        logger.info("Плановая очистка истории: удалено %s записей", deleted)


if __name__ == "__main__":
    asyncio.run(main())
