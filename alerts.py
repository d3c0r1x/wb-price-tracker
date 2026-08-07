"""Логика «уведомлять или нет» — чистая функция, легко тестируемая.

Отделена от Telegram и БД, чтобы можно было покрыть unit-тестами все ветки:
падение цены, заканчивающийся товар, кулдаун между алертами по одному товару.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def should_notify(
    sale_price: int,
    qty: int,
    last_price: Optional[int],
    last_qty: Optional[int],
    last_alert_at: Optional[str],
    *,
    cooldown_hours: float = 6.0,
    low_stock_threshold: int = 5,
    now: Optional[datetime] = None,
) -> tuple[bool, list[str]]:
    """Возвращает (нужно_ли_уведомлять, список_сообщений).

    Уведомляем, если цена упала ИЛИ товар заканчивается, но не чаще одного
    раза в cooldown_hours по одному и тому же товару.
    """
    messages: list[str] = []
    if last_price is not None and sale_price < last_price:
        messages.append(
            f"📉 <b>Цена упала!</b> Было {last_price} ₽ → стало {sale_price} ₽"
        )
    if last_qty is not None and qty <= low_stock_threshold and qty < last_qty:
        messages.append(f"⚠️ <b>Товар заканчивается!</b> Осталось {qty} шт.")

    if not messages:
        return False, []

    if last_alert_at:
        try:
            last_alert_dt = datetime.fromisoformat(last_alert_at)
            now_dt = now or datetime.now(last_alert_dt.tzinfo)
            if now_dt - last_alert_dt < timedelta(hours=cooldown_hours):
                return False, []  # кулдаун ещё не истёк
        except ValueError:
            pass  # битая дата — не мешаем уведомлению
    return True, messages
