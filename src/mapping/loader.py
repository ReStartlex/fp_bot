"""Импорт маппингов NS<->FunPay из CSV."""
from __future__ import annotations

import csv
from pathlib import Path

from loguru import logger

from src.db.repo import upsert_mapping
from src.db.session import session_factory


REQUIRED_COLUMNS = {"funpay_lot_id", "ns_service_id"}
OPTIONAL_COLUMNS = {
    "markup_percent",
    "stock_cap",
    "ns_fields_template",
    "enabled",
    "label",
}


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "+"}


def _parse_int_opt(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return int(str(value).strip())


def _parse_float_opt(value: str | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    return float(str(value).strip().replace(",", "."))


async def import_mappings_from_csv(path: Path) -> dict:
    """
    CSV-формат (заголовок обязателен):
        funpay_lot_id,ns_service_id[,markup_percent,stock_cap,ns_fields_template,enabled,label]

    Пример:
        funpay_lot_id,ns_service_id,markup_percent,stock_cap,enabled,label
        12345678,20,15,100,true,Apple Gift Card USA 2 USD
    """
    if not path.exists():
        raise FileNotFoundError(f"CSV не найден: {path}")

    inserted = 0
    updated = 0
    skipped = 0
    errors: list[str] = []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise ValueError(f"В CSV нет обязательных колонок: {missing}")

        async with session_factory()() as session:
            for line_no, row in enumerate(reader, start=2):
                try:
                    funpay_lot_id = int(row["funpay_lot_id"].strip())
                    ns_service_id = int(row["ns_service_id"].strip())
                except (ValueError, KeyError, AttributeError) as exc:
                    errors.append(f"Строка {line_no}: {exc}")
                    skipped += 1
                    continue

                obj = await upsert_mapping(
                    session,
                    funpay_lot_id=funpay_lot_id,
                    ns_service_id=ns_service_id,
                    markup_percent=_parse_float_opt(row.get("markup_percent")),
                    stock_cap=_parse_int_opt(row.get("stock_cap")),
                    ns_fields_template=row.get("ns_fields_template") or None,
                    enabled=_parse_bool(row.get("enabled"), default=True),
                    label=(row.get("label") or "").strip() or None,
                )
                if obj.id is not None and obj.id > 0:
                    # SQLAlchemy не делит INSERT/UPDATE здесь, считаем оптимистично
                    inserted += 1

            await session.commit()

    logger.info(f"Импорт CSV {path}: inserted/updated={inserted}, skipped={skipped}")
    for err in errors:
        logger.warning(err)
    return {
        "inserted_or_updated": inserted,
        "skipped": skipped,
        "errors": errors,
    }
