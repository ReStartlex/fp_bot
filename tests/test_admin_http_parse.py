"""
Юнит-тесты на парсер HTML формы /lots/offerEdit.

Реальный FunPay HTML очень длинный (32k bytes), поэтому в фикстуре
ниже — минимальный, но синтаксически корректный кусок с теми же
ключевыми элементами, что FunPay действительно отдаёт.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.funpay.admin_http import (
    FunPayAdminClient,
    FunPayParseError,
    LotFields,
)


FAKE_HTML_OK = """
<!DOCTYPE html>
<html><head><title>Редактирование предложения / FunPay</title></head>
<body data-user-id="617001">
<form action="/lots/offerSave" method="post" id="lots-offer-edit" class="js-lots-edit">
  <input type="hidden" name="csrf_token" value="abc123def456">
  <input type="hidden" name="offer_id" value="69300023">
  <input type="hidden" name="node_id" value="1316">
  <input type="text" name="price" value="250.916497">
  <input type="number" name="amount" value="5">
  <textarea name="fields[summary][ru]">Apple Gift Card | USA | 2 USD</textarea>
  <textarea name="fields[desc][ru]">Подробное описание лота</textarea>
  <input type="checkbox" name="active" value="on" checked>
  <input type="checkbox" name="deactivate_after_sale" value="on">
  <select name="renewal_days">
    <option value="0">Без авто-обновления</option>
    <option value="7" selected>Каждые 7 дней</option>
  </select>
  <button type="submit">Сохранить</button>
</form>
</body></html>
"""

FAKE_HTML_LOGIN_REDIRECT = """
<!DOCTYPE html>
<html><body>
<form action="/account/login" method="post">
  <input name="login"><input name="password">
</form>
</body></html>
"""

FAKE_HTML_NO_FORM = """
<!DOCTYPE html>
<html><body><p>Какая-то страница без формы</p></body></html>
"""


def _make_client():
    return FunPayAdminClient(golden_key="x" * 32, phpsessid="y" * 32)


@pytest.mark.asyncio
async def test_parse_form_extracts_all_fields():
    client = _make_client()
    with patch.object(client, "_sync_get") as mock_get:
        class FakeResp:
            text = FAKE_HTML_OK
            status_code = 200
            headers = {}
        mock_get.return_value = FakeResp()

        fields = await client.get_lot_fields(69300023, node_id=1316)

    assert isinstance(fields, LotFields)
    assert fields.lot_id == 69300023
    assert fields.node_id == 1316
    assert fields.price == pytest.approx(250.916497)
    assert fields.amount == 5
    assert fields.active is True
    assert fields.deactivate_after_sale is False
    # CSRF и hidden поля — должны лежать в raw_fields
    assert fields.raw_fields["csrf_token"] == "abc123def456"
    assert fields.raw_fields["offer_id"] == "69300023"
    # selected option должен записаться
    assert fields.raw_fields["renewal_days"] == "7"
    # textarea (multibyte)
    assert "Apple Gift Card" in fields.raw_fields["fields[summary][ru]"]


@pytest.mark.asyncio
async def test_price_setter_normalizes_format():
    client = _make_client()
    with patch.object(client, "_sync_get") as mock_get:
        class FakeResp:
            text = FAKE_HTML_OK
            status_code = 200
            headers = {}
        mock_get.return_value = FakeResp()

        fields = await client.get_lot_fields(69300023)

    fields.price = 158.34567
    assert fields.raw_fields["price"] == "158.35"  # округление до 2 знаков

    fields.amount = 0
    assert fields.raw_fields["amount"] == "0"

    fields.active = False
    assert "active" not in fields.raw_fields  # неотмеченный чекбокс выпадает


@pytest.mark.asyncio
async def test_login_redirect_raises_auth_error():
    from src.funpay.admin_http import FunPayAuthError

    client = _make_client()
    with patch.object(client, "_sync_get") as mock_get:
        class FakeResp:
            text = FAKE_HTML_LOGIN_REDIRECT
            status_code = 200
            headers = {}
        mock_get.return_value = FakeResp()

        with pytest.raises(FunPayAuthError):
            await client.get_lot_fields(69300023)


@pytest.mark.asyncio
async def test_no_form_raises_parse_error():
    client = _make_client()
    with patch.object(client, "_sync_get") as mock_get:
        class FakeResp:
            text = FAKE_HTML_NO_FORM
            status_code = 200
            headers = {}
        mock_get.return_value = FakeResp()

        with pytest.raises(FunPayParseError):
            await client.get_lot_fields(69300023)


@pytest.mark.asyncio
async def test_save_lot_returns_ok_for_msg_ok():
    client = _make_client()
    fields = LotFields(lot_id=1, node_id=1, raw_fields={"price": "100"})
    with patch.object(client, "_sync_post") as mock_post:
        class FakeResp:
            status_code = 200
            text = '{"msg": "ok"}'
            ok = True
            headers = {"Content-Type": "application/json"}

            def json(self):
                return {"msg": "ok"}
        mock_post.return_value = FakeResp()

        result = await client.save_lot(fields)
    assert result["ok"] is True
    assert result["json"]["msg"] == "ok"


@pytest.mark.asyncio
async def test_save_lot_returns_error_for_msg_err():
    client = _make_client()
    fields = LotFields(lot_id=1, node_id=1, raw_fields={"price": "100"})
    with patch.object(client, "_sync_post") as mock_post:
        class FakeResp:
            status_code = 200
            text = '{"msg": "Цена должна быть выше"}'
            ok = True
            headers = {"Content-Type": "application/json"}

            def json(self):
                return {"msg": "Цена должна быть выше"}
        mock_post.return_value = FakeResp()

        result = await client.save_lot(fields)
    assert result["ok"] is False
    assert "Цена" in result["funpay_error"]
