"""
Разведка создания лота FunPay через POST /lots/offerSave с offer_id=0.

Это подготовка к миграции NS→FunPay: редактирование лотов у нас
отлажено, а создание — нет. Инструмент создаёт ОДИН тестовый лот
в указанном разделе, причём:
  • лот создаётся НЕАКТИВНЫМ (ключ active не отправляется) —
    покупатели его не видят;
  • цена по умолчанию заведомо абсурдная (999999) — двойная страховка;
  • без флага --really-create ничего не отправляется (dry-run:
    печатает payload, который БЫЛ БЫ отправлен).

Workflow:
  1. Узнай схему раздела:
     ./.venv/bin/python -m src.tools.funpay_node_schema <node_id>
  2. Собери команду создания, передав значения полей из схемы:
     ./.venv/bin/python -m src.tools.probe_offer_create --node <node_id> \
         --field "fields[summary][ru]=ТЕСТ НЕ ПОКУПАТЬ" \
         --field "fields[summary][en]=TEST DO NOT BUY" \
         --field "fields[desc][ru]=Тестовый лот, проверка API" \
         --field "fields[desc][en]=Test lot, API probe" \
         --field "<имя_селекта>=<значение_из_схемы>" \
         --amount 1
  3. Проверь dry-run вывод, добавь --really-create.
  4. Инструмент сам найдёт lot_id созданного лота (diff списка лотов
     до/после) и напомнит удалить его после проверки.

Требует ENABLE_REAL_ACTIONS=true в .env для реального создания.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from loguru import logger

from src.config import get_settings
from src.funpay.admin_http import LotFields
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


async def _my_lot_ids(fp: FunPayClient) -> set[int]:
    """Снапшот id всех моих лотов (для diff до/после создания)."""
    ids: set[int] = set()
    try:
        for lot in await fp.get_my_lots():
            raw = getattr(lot, "id", None) or getattr(lot, "lot_id", None)
            try:
                if raw is not None:
                    ids.add(int(raw))
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        logger.warning(f"Не смог получить список лотов: {exc}")
    return ids


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Проба создания лота FunPay (offer_id=0)"
    )
    parser.add_argument("--node", type=int, required=True, help="node_id раздела")
    parser.add_argument(
        "--field", action="append", default=[],
        metavar="NAME=VALUE",
        help="поле формы (повторяемый), имена — из funpay_node_schema",
    )
    # Цена: настоящая страховка от случайной продажи — active=False
    # (неактивный лот купить нельзя). Высокую «заградительную» цену НЕ
    # ставим: FunPay валидирует ПОТОЛОК цены раздела и отвергает форму
    # (errors=[["price","Неверная цена."]]). Дефолт — умеренный и для
    # gift-card разделов обычно проходит; при отказе передай --price с
    # ценой из своего рабочего лота-образца этого раздела.
    parser.add_argument("--price", type=float, default=1000.0)
    parser.add_argument("--amount", type=int, default=1)
    parser.add_argument(
        "--really-create", action="store_true",
        help="реально отправить POST (без флага — dry-run)",
    )
    args = parser.parse_args()

    overrides: dict[str, str] = {}
    for raw in args.field:
        if "=" not in raw:
            logger.error(f"--field ожидает NAME=VALUE, получил: {raw!r}")
            return 1
        name, _, value = raw.partition("=")
        overrides[name.strip()] = value

    settings = get_settings()

    logger.info("=" * 60)
    logger.info(f"Проба создания лота: node={args.node}")
    logger.info("=" * 60)

    async with FunPayClient() as fp:
        await fp.connect()
        admin = fp._admin

        # 1. Пустая форма создания — даёт csrf_token и все дефолты.
        lot_fields = await admin.get_lot_fields(0, node_id=args.node)
        logger.info(
            f"Форма создания получена: {len(lot_fields.raw_fields)} полей"
        )

        # 2. Заполняем.
        for name, value in overrides.items():
            lot_fields.raw_fields[name] = value
        lot_fields.price = args.price
        lot_fields.amount = args.amount
        # КРИТИЧНО: создаём НЕАКТИВНЫМ — покупатели не должны его видеть.
        lot_fields.active = False
        # И никакой автодеактивации после продажи не настраиваем — лот
        # вообще не должен продаваться.

        payload: dict[str, Any] = dict(lot_fields.raw_fields)
        payload.setdefault("offer_id", "0")

        logger.info("── Payload, который будет отправлен в /lots/offerSave ──")
        printable = {
            k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
            for k, v in sorted(payload.items())
        }
        logger.info(json.dumps(printable, ensure_ascii=False, indent=2))

        if not args.really_create:
            logger.warning(
                "DRY-RUN: ничего не отправлено. Проверь payload выше "
                "(особенно селекты и offer_id=0) и добавь --really-create."
            )
            return 0

        if not settings.enable_real_actions:
            logger.error(
                "ENABLE_REAL_ACTIONS=false — реальное создание запрещено. "
                "Включи в .env, если уверен."
            )
            return 1

        # 3. Снапшот лотов ДО.
        before = await _my_lot_ids(fp)
        logger.info(f"Лотов до создания: {len(before)}")

        # 4. POST.
        result = await admin.save_lot(lot_fields)
        logger.info(f"Ответ offerSave: {json.dumps(result, ensure_ascii=False)[:500]}")
        if not result.get("ok"):
            err_repr = json.dumps(
                result.get("funpay_error"), ensure_ascii=False
            ).lower()
            if "price" in err_repr or "цена" in err_repr:
                logger.error(
                    "FunPay отверг ЦЕНУ. Скорее всего, она выше потолка раздела "
                    "(или ниже минимума). Передай --price с ценой из своего "
                    "рабочего лота-образца этого раздела."
                )
            else:
                logger.error(
                    "FunPay НЕ подтвердил создание. Смотри funpay_error выше: в "
                    "errors указано конкретное поле. Сверь его значение со "
                    "схемой раздела (funpay_node_schema)."
                )
            return 1

        # 5. Снапшот лотов ПОСЛЕ → diff.
        await asyncio.sleep(2)
        after = await _my_lot_ids(fp)
        new_ids = sorted(after - before)
        if new_ids:
            logger.success(f"Создан(ы) лот(ы): {new_ids}")
            for lot_id in new_ids:
                logger.info(
                    f"  Проверка: https://funpay.com/lots/offerEdit?"
                    f"node={args.node}&offer={lot_id}&location=offer"
                )
        else:
            logger.warning(
                "offerSave ответил ok, но новый лот не найден в списке "
                "(возможно, get_my_lots не видит неактивные). Проверь "
                f"раздел руками: https://funpay.com/lots/{args.node}/trade"
            )

        logger.warning(
            "⚠ НЕ ЗАБУДЬ удалить тестовый лот после проверки "
            "(кнопка «Удалить» в форме редактирования)."
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
