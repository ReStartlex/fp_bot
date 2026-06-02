"""
Тест: если FunPayAPI.Account.send_message бросает исключение,
FunPayClient.send_message должен автоматически перейти на
admin_http.send_chat_message fallback.

Это критический контракт: без него «бот молчит» при любой ошибке
библиотеки.
"""
from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_send_message_uses_funpayapi_first_when_works():
    fp = FunPayClient(_make_settings())
    mock_account = MagicMock()
    mock_account.send_message = MagicMock(return_value={"ok": True})
    fp._account = mock_account

    result = await fp.send_message(123, "test")
    mock_account.send_message.assert_called_once_with(123, "test")
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_send_message_falls_back_to_admin_http_on_funpayapi_exception():
    fp = FunPayClient(_make_settings())

    def _raise(*args, **kwargs):
        raise json.JSONDecodeError("Expecting value", "doc", 0)

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(side_effect=_raise)
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(return_value={"ok": True, "http_status": 200})
    fp._admin_client_cache = fake_admin

    result = await fp.send_message(777, "fallback please")
    fake_admin.send_chat_message.assert_awaited_once_with(777, "fallback please")
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_message_treats_funpayapi_parser_glitch_as_success():
    """
    Известный bug FunPayAPI: POST /runner/ доставлен (сообщение отправлено),
    а потом библиотека парсит HTML ответа: parser.find("div.message-text").text
    → AttributeError 'NoneType' object has no attribute 'text'.

    Сообщение УЖЕ доставлено. Делать fallback (повторно отправлять)
    нельзя, иначе FunPay вернёт «Обновите страницу» на дубль.
    """
    fp = FunPayClient(_make_settings())

    def _glitch(*args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'text'")

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(side_effect=_glitch)
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(return_value={"ok": True})
    fp._admin_client_cache = fake_admin

    result = await fp.send_message(42, "Привет!")

    # Главное: fallback НЕ вызван (иначе будет дубль).
    fake_admin.send_chat_message.assert_not_called()
    assert isinstance(result, dict)
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_send_message_falls_back_on_other_attribute_error():
    """Прочие AttributeError (не glitch parser'а) → fallback всё-таки нужен."""
    fp = FunPayClient(_make_settings())

    def _other_attr_err(*args, **kwargs):
        raise AttributeError("'FunPayClient' object has no attribute 'foo'")

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(side_effect=_other_attr_err)
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(return_value={"ok": True})
    fp._admin_client_cache = fake_admin

    result = await fp.send_message(42, "test")
    fake_admin.send_chat_message.assert_awaited_once()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_send_message_raises_when_both_paths_fail():
    fp = FunPayClient(_make_settings())

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(side_effect=RuntimeError("FunPayAPI broken"))
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(side_effect=RuntimeError("admin broken too"))
    fp._admin_client_cache = fake_admin

    with pytest.raises(RuntimeError, match="admin broken too"):
        await fp.send_message(1, "x")


@pytest.mark.asyncio
async def test_send_message_raises_when_admin_http_returns_ok_false():
    """
    Регрессия (аудит #2): admin_http fallback может вернуть {"ok": False}
    БЕЗ исключения (например HTTP 200 с ошибкой в теле или сетевой timeout
    обработанный внутри admin_http). Раньше processor.py трактовал такой
    return как успех и помечал заказ delivered, хотя сообщение НЕ дошло.

    Контракт: при ok=False fallback должен бросать RuntimeError, чтобы
    вызывающий код (processor, chat handler) видел это как обычное
    исключение и НЕ ставил delivered.
    """
    fp = FunPayClient(_make_settings())

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(
        side_effect=RuntimeError("primary path broken")
    )
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(
        return_value={"ok": False, "http_status": 500, "error": "server died"}
    )
    fp._admin_client_cache = fake_admin

    with pytest.raises(RuntimeError, match="admin_http"):
        await fp.send_message(123, "should not be considered delivered")

    fake_admin.send_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_message_returns_ok_true_from_admin_http_fallback():
    """Граница: ok=True от fallback — всё штатно, exception НЕ поднимаем."""
    fp = FunPayClient(_make_settings())

    mock_account = MagicMock()
    mock_account.send_message = MagicMock(
        side_effect=RuntimeError("primary path broken")
    )
    fp._account = mock_account

    fake_admin = MagicMock()
    fake_admin.send_chat_message = AsyncMock(
        return_value={"ok": True, "http_status": 200}
    )
    fp._admin_client_cache = fake_admin

    result = await fp.send_message(123, "delivered ok")
    assert isinstance(result, dict) and result.get("ok") is True
