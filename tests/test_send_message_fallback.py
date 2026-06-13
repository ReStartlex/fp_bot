"""
Контракт FunPayClient.send_message.

P1-2 этап 1 (выпил FunPayAPI): ОСНОВНОЙ путь — admin_http.send_chat_message
(прямой POST /runner/, без хрупкого парсинга HTML-ответа). FunPayAPI —
РЕЗЕРВ. При неуспехе основного пути пробуем резерв (для доставки пропуск
страшнее дубля); при провале обоих — RuntimeError (вызывающий код не
должен счесть это доставкой, аудит #2).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import Settings
from src.funpay.client import FunPayClient


def _make_settings():
    return Settings(
        funpay_golden_key="g" * 40,
        funpay_phpsessid="p" * 26,
        telegram_bot_token="123:abc",
        telegram_chat_id="456",
        ns_api_key="ns" * 16,
        ns_api_secret="ns" * 30,
    )


def _client(admin_result=None, admin_exc=None, account_mock=None) -> FunPayClient:
    """FunPayClient с подменёнными admin_http и FunPayAPI.account."""
    fp = FunPayClient(_make_settings())

    fake_admin = MagicMock()
    if admin_exc is not None:
        fake_admin.send_chat_message = AsyncMock(side_effect=admin_exc)
    else:
        fake_admin.send_chat_message = AsyncMock(return_value=admin_result)
    fp._admin_client_cache = fake_admin

    fp._account = account_mock if account_mock is not None else MagicMock()
    return fp


@pytest.mark.asyncio
async def test_send_message_uses_admin_http_first_when_works():
    """admin_http отдал ok=True → возвращаем его, FunPayAPI НЕ трогаем."""
    account = MagicMock()
    account.send_message = MagicMock(return_value={"ok": True, "via": "funpayapi"})
    fp = _client(admin_result={"ok": True, "http_status": 200}, account_mock=account)

    result = await fp.send_message(123, "test")

    fp._admin.send_chat_message.assert_awaited_once_with(123, "test")
    account.send_message.assert_not_called()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_message_falls_back_to_funpayapi_on_admin_exception():
    """admin_http бросил исключение → пробуем резерв FunPayAPI."""
    account = MagicMock()
    account.send_message = MagicMock(return_value={"ok": True})
    fp = _client(admin_exc=RuntimeError("admin down"), account_mock=account)

    result = await fp.send_message(777, "fallback please")

    fp._admin.send_chat_message.assert_awaited_once()
    account.send_message.assert_called_once_with(777, "fallback please")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_message_falls_back_to_funpayapi_on_admin_ok_false():
    """admin_http вернул ok=False (не доставлено) → резерв FunPayAPI."""
    account = MagicMock()
    account.send_message = MagicMock(return_value={"ok": True})
    fp = _client(
        admin_result={"ok": False, "http_status": 400, "funpay_error": "x"},
        account_mock=account,
    )

    result = await fp.send_message(42, "via fallback")

    account.send_message.assert_called_once_with(42, "via fallback")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_message_treats_funpayapi_parser_glitch_as_success():
    """
    Известный glitch FunPayAPI: POST /runner/ доставлен (сообщение ушло),
    а потом библиотека парсит HTML ответа: parser.find(...).text →
    AttributeError 'NoneType' object has no attribute 'text'.

    Сообщение УЖЕ доставлено резервом → считаем успехом, без повторов.
    """
    account = MagicMock()
    account.send_message = MagicMock(
        side_effect=AttributeError("'NoneType' object has no attribute 'text'")
    )
    # admin_http не смог → ушли в резерв, где словили glitch
    fp = _client(admin_exc=RuntimeError("admin down"), account_mock=account)

    result = await fp.send_message(42, "Привет!")

    assert isinstance(result, dict)
    assert result.get("ok") is True
    assert result.get("via") == "funpayapi_with_parser_glitch"


@pytest.mark.asyncio
async def test_send_message_other_attribute_error_after_admin_fail_raises():
    """Прочий AttributeError резерва (не glitch) + упавший admin → RuntimeError."""
    account = MagicMock()
    account.send_message = MagicMock(
        side_effect=AttributeError("'FunPayClient' object has no attribute 'foo'")
    )
    fp = _client(admin_exc=RuntimeError("admin down"), account_mock=account)

    with pytest.raises(RuntimeError, match="admin_http \\+ FunPayAPI"):
        await fp.send_message(42, "test")


@pytest.mark.asyncio
async def test_send_message_raises_when_both_paths_fail():
    """admin бросил И FunPayAPI бросил → RuntimeError (не доставлено)."""
    account = MagicMock()
    account.send_message = MagicMock(side_effect=RuntimeError("FunPayAPI broken"))
    fp = _client(admin_exc=RuntimeError("admin broken"), account_mock=account)

    with pytest.raises(RuntimeError, match="admin_http \\+ FunPayAPI"):
        await fp.send_message(1, "x")


@pytest.mark.asyncio
async def test_send_message_admin_ok_false_and_funpayapi_fail_raises():
    """
    Регрессия (аудит #2): если ОБА пути не доставили — должно быть
    исключение, чтобы processor НЕ пометил заказ delivered.
    admin ok=False + FunPayAPI бросает → RuntimeError.
    """
    account = MagicMock()
    account.send_message = MagicMock(side_effect=RuntimeError("funpayapi died too"))
    fp = _client(
        admin_result={"ok": False, "http_status": 500, "funpay_error": "server died"},
        account_mock=account,
    )

    with pytest.raises(RuntimeError, match="admin_http \\+ FunPayAPI"):
        await fp.send_message(123, "should not be considered delivered")

    account.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_returns_ok_true_from_admin_http():
    """Граница: ok=True от admin_http — штатно, резерв не трогаем."""
    account = MagicMock()
    account.send_message = MagicMock(return_value={"ok": True})
    fp = _client(admin_result={"ok": True, "http_status": 200}, account_mock=account)

    result = await fp.send_message(123, "delivered ok")
    assert isinstance(result, dict) and result.get("ok") is True
    account.send_message.assert_not_called()
