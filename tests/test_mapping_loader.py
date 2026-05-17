"""Тесты импорта CSV-маппингов."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import Mapping
from src.db.session import close_db, init_db, session_factory
from src.mapping.loader import import_mappings_from_csv


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    p = tmp_path / "m.csv"
    p.write_text(
        "funpay_lot_id,ns_service_id,markup_percent,stock_cap,enabled,label\n"
        "12345678,20,15,100,true,Apple 2 USD\n"
        "87654321,21,,,false,Disabled\n"
        "11111111,22,,50,,\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def csv_invalid(tmp_path: Path) -> Path:
    p = tmp_path / "bad.csv"
    p.write_text(
        "funpay_lot_id,ns_service_id\n"
        "not-a-number,20\n"
        "1,not-a-number-either\n"
        "99,33\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Изолируем БД в tmp_path, чтобы не пачкать data/bridge.db."""
    import src.db.session as sess_mod

    sess_mod._engine = None
    sess_mod._session_factory = None

    from src.config import Settings, get_settings

    settings = get_settings()
    orig_data = settings.__class__.data_path.fget
    monkeypatch.setattr(
        Settings, "data_path", property(lambda self: tmp_path), raising=True
    )
    yield
    monkeypatch.setattr(Settings, "data_path", property(orig_data), raising=True)
    sess_mod._engine = None
    sess_mod._session_factory = None


@pytest.mark.asyncio
async def test_import_csv_inserts_rows(csv_file: Path):
    await init_db()
    try:
        result = await import_mappings_from_csv(csv_file)
        async with session_factory()() as session:
            rows = (await session.execute(select(Mapping))).scalars().all()

        assert {r.funpay_lot_id for r in rows} == {12345678, 87654321, 11111111}
        by_id = {r.funpay_lot_id: r for r in rows}
        assert by_id[12345678].ns_service_id == 20
        assert by_id[12345678].markup_percent == 15.0
        assert by_id[12345678].stock_cap == 100
        assert by_id[12345678].enabled is True

        assert by_id[87654321].enabled is False
        assert by_id[87654321].label == "Disabled"

        assert by_id[11111111].markup_percent is None
        assert by_id[11111111].stock_cap == 50

        assert result["skipped"] == 0
    finally:
        await close_db()


@pytest.mark.asyncio
async def test_import_csv_skips_bad_rows(csv_invalid: Path):
    await init_db()
    try:
        result = await import_mappings_from_csv(csv_invalid)
        async with session_factory()() as session:
            rows = (await session.execute(select(Mapping))).scalars().all()
        assert len(rows) == 1
        assert rows[0].funpay_lot_id == 99
        assert result["skipped"] == 2
        assert len(result["errors"]) == 2
    finally:
        await close_db()
