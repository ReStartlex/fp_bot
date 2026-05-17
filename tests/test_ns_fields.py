"""Тесты сборки NS-fields из шаблона маппинга."""
from __future__ import annotations

import pytest

from src.orders.processor import _build_ns_fields


def test_no_template_default():
    fields = _build_ns_fields(None, quantity=3)
    assert fields == [{"key": "quantity", "value": 3}]


def test_template_quantity_substitution():
    tpl = '{"quantity":"@QUANTITY"}'
    fields = _build_ns_fields(tpl, quantity=5)
    assert fields == [{"key": "quantity", "value": 5}]


def test_template_static_fields():
    tpl = '{"quantity":"@QUANTITY","email":"buyer@test.com"}'
    fields = _build_ns_fields(tpl, quantity=2)
    by_key = {f["key"]: f["value"] for f in fields}
    assert by_key == {"quantity": 2, "email": "buyer@test.com"}


def test_invalid_json_raises():
    with pytest.raises(ValueError, match="не валидный JSON"):
        _build_ns_fields("{not json", quantity=1)


def test_non_object_raises():
    with pytest.raises(ValueError, match="JSON-объектом"):
        _build_ns_fields('["array","instead"]', quantity=1)
