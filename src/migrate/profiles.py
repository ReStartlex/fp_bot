"""
Профили платформ — переопределение шаблонов summary/desc на платформу.

Зачем: описания платформо-специфичны (Apple-ссылка на активацию,
Battle.net-шаги, фирменное наименование «Apple ID | iTunes | App Store»).
Без профилей пришлось бы править desc в каждой из десятков категорий
платформы. С профилем ты задаёшь шаблоны для «Apple» ОДИН раз, и скелет
подставляет их во все Apple-категории.

Формат файла (YAML), ключ = имя платформы (как его выдаёт detect_platform,
регистр не важен):

    Apple:
      summary_ru: "✈️АВТОВЫДАЧА 🔑 ... {nominal} {currency} ({region_ru}) 🔵"
      summary_en: "..."
      desc_ru: |
        ...твой полный текст с {nominal} {currency} {region_ru}...
        🧾 Инструкция: https://support.apple.com/ru-ru/HT201209
      desc_en: |
        ...
    Steam:
      desc_ru: |
        ...

Любое поле опционально: чего нет в профиле — берётся дефолтный шаблон.
Теги подстановки те же: {platform} {nominal} {currency} {region_ru} {region_en}.
"""
from __future__ import annotations

from typing import Any

import yaml

PROFILE_KEYS = ("summary_ru", "summary_en", "desc_ru", "desc_en")


def load_profiles(path: str) -> dict[str, dict[str, str]]:
    """Загружает профили; ключи платформ нормализует в lower."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Профили в {path} должны быть YAML-словарём platform→поля")
    out: dict[str, dict[str, str]] = {}
    for platform, fields in data.items():
        if not isinstance(fields, dict):
            continue
        out[str(platform).strip().lower()] = {
            k: str(v).rstrip("\n") for k, v in fields.items() if k in PROFILE_KEYS
        }
    return out


def profile_for(profiles: dict[str, dict[str, str]], platform: str) -> dict[str, str]:
    """Профиль платформы (пустой dict, если профиля нет)."""
    return profiles.get(platform.strip().lower(), {})
