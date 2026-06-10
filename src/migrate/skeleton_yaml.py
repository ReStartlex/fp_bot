"""
Генерация YAML-скелета таблицы соответствий NS→FunPay.

Скелет заполнен всем, что выводится из NS (платформа, регион, валюта,
номиналы, цены), и содержит TODO-плейсхолдеры для того, что человек
заполняет по схеме FunPay-раздела: funpay_node, маппинг полей формы,
шаблоны summary/desc (RU+EN).

Шаблоны summary/desc предзаполнены по образцу пользователя с
подстановочными тегами {platform} {nominal} {currency} {region_ru}
{region_en} — останется лишь поправить под раздел.
"""
from __future__ import annotations

from src.migrate.catalog import SkeletonEntry


# Шаблоны по умолчанию (образец пользователя). Теги в фигурных скобках
# подставляются генератором лотов на этапе run.
DEFAULT_SUMMARY_RU = (
    "✈️АВТОВЫДАЧА 🔑 Подарочная карта {platform} 🔵 "
    "{nominal} {currency} ({region_ru}) 🔵"
)
DEFAULT_SUMMARY_EN = (
    "✈️AUTO DELIVERY 🔑 {platform} Gift Card 🔵 "
    "{nominal} {currency} ({region_en}) 🔵"
)
DEFAULT_DESC_RU = """➖➖➖➖➖➖➖☑️После оплаты ☑️➖➖➖➖➖➖➖

🔑Вы получаете код пополнения номиналом {nominal} {currency} для {platform}
🎮Платформа: {platform}
📩Другие номиналы/валюты уточняйте в личных сообщениях / смотрите в профиле

➖➖➖➖🛑 ВАЖНО ЗНАТЬ 🛑➖➖➖➖

✅ ПРОСЬБА К ПОКУПАТЕЛЮ
Пожалуйста, включайте 🎥 запись экрана с момента оплаты и до проверки/активации кода. Видео помогает быстро решить любые спорные ситуации и подтвердить качество товара.

🔒 УСЛОВИЯ ПРОДАЖИ
Обратите внимание: цифровые коды относятся к одноразовым товарам и после передачи покупателю возврату и обмену не подлежат.
"""
DEFAULT_DESC_EN = """➖➖➖➖➖➖➖☑️After Payment ☑️➖➖➖➖➖➖➖

🔑You will receive a {nominal} {currency} top-up code for {platform}
🎮Platform: {platform}
📩For other denominations/currencies, please contact us via private messages / check the profile

➖➖➖➖🛑 IMPORTANT INFORMATION 🛑➖➖➖➖

✅ REQUEST TO THE BUYER
Please enable 🎥 screen recording from the moment of payment until the code is checked/activated. The video helps quickly resolve any disputes and confirm the quality of the product.

🔒 SALES TERMS
Please note: digital codes are one-time-use products and cannot be returned or exchanged after delivery to the buyer.
"""

# Маркер незаполненного поля. validate-команда обязана отвергать запись
# с любым TODO в обязательных полях.
TODO = "TODO_ЗАПОЛНИ"


def _yaml_escape(value: str) -> str:
    """Безопасная подстановка строки в одинарных кавычках YAML."""
    return value.replace("'", "''")


def _block_scalar(text: str, indent: int) -> str:
    """Многострочный текст как YAML literal block scalar (|)."""
    pad = " " * indent
    lines = text.rstrip("\n").split("\n")
    body = "\n".join(f"{pad}{ln}" if ln else "" for ln in lines)
    return "|\n" + body


def render_entry_yaml(entry: SkeletonEntry) -> str:
    """Один YAML-блок для категории (как элемент списка)."""
    out: list[str] = []
    a = out.append

    a(f"- ns_category_id: {entry.ns_category_id}")
    a(f"  ns_category_name: '{_yaml_escape(entry.ns_category_name)}'")
    a(f"  platform: '{_yaml_escape(entry.platform)}'")
    a(f"  currency: {entry.currency or TODO}")
    region_code = entry.region_code or ""
    a(f"  region_code: {region_code or '~'}")
    a(f"  region_ru: '{_yaml_escape(entry.region_ru or TODO)}'")
    a(f"  region_en: '{_yaml_escape(entry.region_en or TODO)}'")

    a("  # === FunPay-сторона: заполни по схеме раздела ===")
    a("  # node_id раздела (funpay.com/lots/<NODE>/trade)")
    a(f"  funpay_node: {TODO}")
    a("  # Цена: NS price_usd × (1 + markup%/100) × курс USD→RUB.")
    a("  markup_percent: 5")
    a("  # Поля формы FunPay (имена/значения — из funpay_node_schema).")
    a("  # Теги {nominal}{currency}{region_ru}{region_en} подставятся на лот.")
    a("  funpay_fields:")
    a(f"    # пример (Apple-нода): \"fields[currency]\": '{entry.currency or TODO}'")
    a("    # пример: \"fields[usd]\": '{nominal} USD'")
    a(f"    {TODO}: {TODO}")

    a("  summary_ru: " + _block_scalar(DEFAULT_SUMMARY_RU, 4))
    a("  summary_en: " + _block_scalar(DEFAULT_SUMMARY_EN, 4))
    a("  desc_ru: " + _block_scalar(DEFAULT_DESC_RU, 4))
    a("  desc_en: " + _block_scalar(DEFAULT_DESC_EN, 4))

    a("  # Услуги NS этой категории (для справки; номинал распознан ботом):")
    a("  services:")
    for s in entry.services:
        nominal = s.nominal if s.nominal is not None else TODO
        a(
            f"    - {{ service_id: {s.service_id}, nominal: {nominal}, "
            f"price_usd: {s.price_usd:.4f}, in_stock: {s.in_stock} }}  "
            f"# {_yaml_escape(s.service_name)}"
        )
    if entry.parse_warnings:
        a("  # ⚠ предупреждения парсера:")
        for w in entry.parse_warnings:
            a(f"  #   - {w}")
    return "\n".join(out)


def render_skeleton(entries: list[SkeletonEntry]) -> str:
    """Полный YAML-файл скелета."""
    header = (
        "# Таблица соответствий NS → FunPay (СКЕЛЕТ).\n"
        "# Заполни поля с маркером " + TODO + " по схеме раздела FunPay\n"
        "# (src.tools.funpay_node_schema <node_id>). Шаблоны summary/desc\n"
        "# уже предзаполнены — поправь под раздел при необходимости.\n"
        "# Затем проверь: src.tools.migrate_validate <файл> --category <id>\n"
        "#\n"
        "# Теги подстановки: {platform} {nominal} {currency} {region_ru} {region_en}\n"
        "\n"
    )
    return header + "\n\n".join(render_entry_yaml(e) for e in entries) + "\n"
