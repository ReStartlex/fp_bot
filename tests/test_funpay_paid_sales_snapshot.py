"""
Тесты FunPayClient.get_paid_sales_snapshot.

FunPay автоматически НЕ закрывает заказы — продавец сам пишет в саппорт
раз в сутки, прикладывая список #order_id из статуса «Оплачен».
Саппорт закрывает их тихо, БЕЗ системного сообщения в чат, поэтому
наш handler никогда не получит admin-confirm для этих 200 заказов
и они навечно остаются в БД с `confirmed_at=NULL`.

Решение: периодически тянуть с FunPay свежий список заказов
в статусе «Оплачен» (через `Account.get_sells(state="paid")`,
страница https://funpay.com/orders/trade) и считать всё, что в БД
помечено `delivered, confirmed_at=NULL`, но в свежем списке paid
ОТСУТСТВУЕТ — автоматически подтверждённым саппортом.

Этот файл проверяет именно обёртку над FunPayAPI, без обращения
к сети — `Account.get_sells` мокаем.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.funpay.client import FunPayClient


def _mk_order(order_id: str, status: str = "paid") -> SimpleNamespace:
    """Минимальный stub под FunPayAPI.types.OrderShortcut."""
    return SimpleNamespace(
        id=order_id.lstrip("#"),
        status=SimpleNamespace(name=status.upper(), value=status),
        buyer_username="buyer",
        buyer_id=1,
        price=100.0,
        date=datetime(2026, 5, 25, 12, 0),
        description="Apple gift card",
        subcategory_name="App Store & iTunes",
        html="<a></a>",
    )


def _make_client_with_account(account_mock) -> FunPayClient:
    fp = FunPayClient.__new__(FunPayClient)
    fp._settings = None  # не используется в этих тестах
    fp._account = account_mock
    fp._lock = None  # не используется
    fp._my_username_cache = "lol228822"
    fp._my_user_id_cache = 1
    return fp


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_filters_by_state_paid():
    """
    Вызов FunPayAPI.Account.get_sells должен идти с state="paid"
    (фильтр на стороне FunPay → меньше HTML, меньше парсинга,
    нет лишнего трафика). Closed/Refunded — не нужны.
    """
    account = MagicMock()
    account.get_sells.return_value = (None, [_mk_order("AAA111")])

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    assert snapshot.ids == ["AAA111"]
    assert snapshot.is_complete is True
    account.get_sells.assert_called_once()
    _, kwargs = account.get_sells.call_args
    assert kwargs.get("state") == "paid"
    assert kwargs.get("include_paid") is True
    assert kwargs.get("include_closed") is False
    assert kwargs.get("include_refunded") is False


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_paginates():
    """
    FunPay отдаёт страницы по ~30 заказов с курсором `continue`.
    Если в первой странице вернулся next_order_id, нужно дозапросить
    следующую страницу с start_from=этот_id и так до пустоты.
    """
    account = MagicMock()
    account.get_sells.side_effect = [
        ("CURSOR1", [_mk_order("A1"), _mk_order("A2")]),
        ("CURSOR2", [_mk_order("B1"), _mk_order("B2")]),
        (None, [_mk_order("C1")]),
    ]

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    assert snapshot.ids == ["A1", "A2", "B1", "B2", "C1"]
    assert snapshot.is_complete is True
    assert account.get_sells.call_count == 3
    call_args_list = account.get_sells.call_args_list
    assert call_args_list[0].kwargs.get("start_from") is None
    assert call_args_list[1].kwargs.get("start_from") == "CURSOR1"
    assert call_args_list[2].kwargs.get("start_from") == "CURSOR2"


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_strips_hash_prefix():
    """
    На случай, если в каких-то форках FunPayAPI id придёт с '#' —
    результат должен быть чистым (без '#'), чтобы матчить с БД.
    """
    account = MagicMock()
    order_with_hash = _mk_order("XYZ999")
    order_with_hash.id = "#XYZ999"
    account.get_sells.return_value = (None, [order_with_hash])

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    assert snapshot.ids == ["XYZ999"]


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_empty_result():
    """Аккаунт без paid-заказов → пустой список."""
    account = MagicMock()
    account.get_sells.return_value = (None, [])

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    assert snapshot.ids == []
    assert snapshot.is_complete is True


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_stops_at_pagination_limit():
    """
    Safety: если FunPay по какой-то причине отдаёт бесконечный курсор
    (баг сервера, эхо), мы должны прекратить пагинацию после
    разумного числа страниц (50 страниц * 30 заказов = 1500 заказов
    с запасом перекрывает реалистичный максимум).
    """
    account = MagicMock()
    # FunPay даёт один и тот же курсор → потенциально бесконечно
    account.get_sells.return_value = ("LOOP_CURSOR", [_mk_order("X1")])

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    # Должно завершиться, не уйти в бесконечный цикл
    assert account.get_sells.call_count <= 50
    assert len(snapshot.ids) <= 50  # один заказ за страницу * лимит страниц
    # Аудит #7: при обрыве пагинации snapshot ОБЯЗАН быть помечен
    # как неполный, иначе sync_pending_confirmation ложно подтвердит
    # оплаченные заказы вне выборки.
    assert snapshot.is_complete is False
    assert snapshot.truncated_reason


@pytest.mark.asyncio
async def test_get_paid_sales_snapshot_marks_incomplete_on_repeated_cursor():
    """Аудит #7: повторный курсор → is_complete=False."""
    account = MagicMock()
    account.get_sells.side_effect = [
        ("REPEAT_CURSOR", [_mk_order("A1")]),
        ("REPEAT_CURSOR", [_mk_order("A2")]),
    ]

    fp = _make_client_with_account(account)
    snapshot = await fp.get_paid_sales_snapshot()

    assert snapshot.is_complete is False
    assert "повторный курсор" in (snapshot.truncated_reason or "")
