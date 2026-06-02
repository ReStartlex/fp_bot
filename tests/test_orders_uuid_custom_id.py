"""
Регрессионный тест: NS create_order custom_id ДОЛЖЕН быть UUID4.

История: до 2026-05-25 мы использовали детерминированный custom_id
вида f"fp-{funpay_order_id}" (например `fp-WR3MVAKX`). NS обновили
валидацию: теперь /api/v2/create_order возвращает 400 с
{"detail":"custom_id must be a valid UUID4"} на любой не-UUID4 строке.

Этот тест защищает от регрессии: убеждаемся, что генерация
NSClient.new_custom_id() даёт UUID4, а helper _is_valid_uuid4
корректно отличает v4 от прочих форматов.
"""
from __future__ import annotations

import uuid

import pytest

from src.ns.client import NSClient
from src.orders.processor import _is_valid_uuid4


def test_ns_client_generates_uuid4():
    """new_custom_id() возвращает строку, которую NS принимает как UUID4."""
    for _ in range(20):
        cid = NSClient.new_custom_id()
        assert _is_valid_uuid4(cid), f"not a UUID4: {cid!r}"


def test_legacy_fp_id_rejected():
    """Старый формат `fp-WR3MVAKX` НЕ должен пройти валидацию."""
    assert not _is_valid_uuid4("fp-WR3MVAKX")
    assert not _is_valid_uuid4("fp-12345678")
    assert not _is_valid_uuid4("fp-A1B2C3D4")


def test_uuid_v5_rejected():
    """UUID5 (детерминированный namespace-based) тоже должен быть отвергнут."""
    v5 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "test"))
    assert _is_valid_uuid4(v5) is False


def test_uuid_v1_rejected():
    """UUID1 (timestamp-based) тоже должен быть отвергнут."""
    v1 = str(uuid.uuid1())
    assert _is_valid_uuid4(v1) is False


def test_empty_and_garbage_rejected():
    assert not _is_valid_uuid4(None)
    assert not _is_valid_uuid4("")
    assert not _is_valid_uuid4("not-a-uuid")
    assert not _is_valid_uuid4("12345")


def test_canonical_uuid4_accepted():
    """Канонический формат 8-4-4-4-12 hex с version=4."""
    v4 = str(uuid.uuid4())
    assert _is_valid_uuid4(v4) is True
    # А произвольный hex-mess той же длины — нет
    fake = "550e8400-e29b-11d4-a716-446655440000"  # это UUID1
    assert uuid.UUID(fake).version == 1
    assert _is_valid_uuid4(fake) is False
