"""
Дамп схемы формы лота для раздела FunPay (read-only).

Зачем: миграция NS→FunPay требует знать, какие поля есть в форме
конкретного раздела (node): селекты «тип валюты» / «способ пополнения»
и их допустимые значения, обязательные текстовые поля, чекбоксы.
Этот инструмент тянет ПУСТУЮ форму создания лота и печатает её
машиночитаемый «паспорт» — без какой-либо записи на FunPay.

Запуск (node_id виден в URL раздела: funpay.com/lots/1234/trade → 1234):
    ./.venv/bin/python -m src.tools.funpay_node_schema 1234
    ./.venv/bin/python -m src.tools.funpay_node_schema 1234 --offer 69300023
    ./.venv/bin/python -m src.tools.funpay_node_schema 1234 --json-out schema_1234.json

--offer N  — разобрать форму СУЩЕСТВУЮЩЕГО лота (полезно, чтобы увидеть,
             какие значения выбраны у твоего ручного лота-образца).
--json-out — сохранить полную схему в JSON (для importer'а миграции).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger

from src.config import get_settings
from src.funpay.admin_http import FunPayAdminClient, FunPayAuthError
from src.logging_setup import setup_logging


def _build_admin(settings) -> FunPayAdminClient:
    return FunPayAdminClient(
        golden_key=settings.funpay_golden_key.get_secret_value(),
        phpsessid=(
            settings.funpay_phpsessid.get_secret_value()
            if settings.funpay_phpsessid else None
        ),
    )


def parse_form_schema(html: str, url: str) -> dict[str, Any]:
    """
    Разбирает HTML формы offerEdit в схему:
        {
          "url": ...,
          "inputs":    [{name, type, value, checked?}],
          "selects":   [{name, label?, options: [{value, text, selected}]}],
          "textareas": [{name, value_preview}],
        }
    """
    soup = BeautifulSoup(html, "html.parser")

    if soup.find("form", action=re.compile(r"/account/login")):
        raise FunPayAuthError(
            f"FunPay перебросил на форму логина на {url} — обнови golden_key."
        )

    form = (
        soup.find("form", action=re.compile(r"/lots/offerSave"))
        or soup.find("form", id="lots-offer-edit")
        or soup.find("form", class_="js-lots-edit")
    )
    if form is None:
        offer_input = soup.find("input", attrs={"name": "offer_id"})
        if offer_input is not None:
            form = offer_input.find_parent("form")
    if form is None:
        preview = html[:300].replace("\n", " ")
        raise RuntimeError(
            f"Не нашёл форму лота в HTML {url}. Preview: {preview!r}"
        )

    def _label_for(el) -> str | None:
        """Подпись поля: ближайший <label> или заголовок form-group."""
        node = el
        for _ in range(4):
            node = node.parent
            if node is None:
                break
            label = node.find("label")
            if label is not None:
                text = label.get_text(strip=True)
                if text:
                    return text
        return None

    schema: dict[str, Any] = {
        "url": url,
        "inputs": [],
        "selects": [],
        "textareas": [],
    }

    for el in form.find_all("input"):
        name = el.get("name")
        if not name:
            continue
        itype = (el.get("type") or "text").lower()
        if itype in ("submit", "button"):
            continue
        item: dict[str, Any] = {
            "name": name,
            "type": itype,
            "value": el.get("value") or "",
        }
        if itype in ("checkbox", "radio"):
            item["checked"] = el.has_attr("checked")
        label = _label_for(el)
        if label:
            item["label"] = label
        schema["inputs"].append(item)

    for el in form.find_all("select"):
        name = el.get("name")
        if not name:
            continue
        options = []
        for opt in el.find_all("option"):
            options.append({
                "value": opt.get("value", ""),
                "text": opt.get_text(strip=True),
                "selected": opt.has_attr("selected"),
            })
        item = {"name": name, "options": options}
        label = _label_for(el)
        if label:
            item["label"] = label
        schema["selects"].append(item)

    for el in form.find_all("textarea"):
        name = el.get("name")
        if not name:
            continue
        value = el.get_text() or ""
        item = {"name": name, "value_preview": value[:200]}
        label = _label_for(el)
        if label:
            item["label"] = label
        schema["textareas"].append(item)

    return schema


def _print_schema(schema: dict[str, Any]) -> None:
    logger.info(f"URL: {schema['url']}")

    logger.info(f"── inputs ({len(schema['inputs'])}) ──")
    for item in schema["inputs"]:
        extra = ""
        if item["type"] in ("checkbox", "radio"):
            extra = f" checked={item.get('checked')}"
        label = f"  «{item['label']}»" if item.get("label") else ""
        logger.info(
            f"  {item['name']}  [{item['type']}]"
            f"  value={item['value']!r}{extra}{label}"
        )

    logger.info(f"── selects ({len(schema['selects'])}) ──")
    for item in schema["selects"]:
        label = f" «{item['label']}»" if item.get("label") else ""
        logger.info(f"  {item['name']}{label}:")
        for opt in item["options"]:
            marker = " ← selected" if opt["selected"] else ""
            logger.info(f"      value={opt['value']!r}  «{opt['text']}»{marker}")

    logger.info(f"── textareas ({len(schema['textareas'])}) ──")
    for item in schema["textareas"]:
        label = f" «{item['label']}»" if item.get("label") else ""
        logger.info(
            f"  {item['name']}{label}  preview={item['value_preview'][:60]!r}"
        )


async def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Дамп схемы формы лота FunPay-раздела (read-only)"
    )
    parser.add_argument("node_id", type=int, help="node_id раздела FunPay")
    parser.add_argument(
        "--offer", type=int, default=0,
        help="lot_id существующего лота (разобрать его форму вместо пустой)",
    )
    parser.add_argument(
        "--json-out", type=str, default=None,
        help="путь для сохранения схемы в JSON",
    )
    args = parser.parse_args()

    settings = get_settings()
    admin = _build_admin(settings)

    params = [f"node={args.node_id}"]
    if args.offer:
        params.append(f"offer={args.offer}")
        params.append("location=offer")
    url = f"{admin.BASE}/lots/offerEdit?" + "&".join(params)

    logger.info("=" * 60)
    logger.info(f"Схема формы лота: node={args.node_id}, offer={args.offer or '—'}")
    logger.info("=" * 60)

    r = await asyncio.to_thread(admin._sync_get, url)
    schema = parse_form_schema(r.text, url)
    _print_schema(schema)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(schema, f, ensure_ascii=False, indent=2)
        logger.success(f"Схема сохранена: {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
