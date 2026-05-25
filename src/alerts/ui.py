"""
Фабрики inline-клавиатур и форматтеры карточек для Telegram-бота.

Принцип: всё, что строит клавиатуру или формат сообщения, живёт здесь.
Хендлеры в bot.py — только склейка логики и I/O.

Стандартизированные callback_data префиксы:
    menu:<action>                — главное меню
    pg:<kind>:<sid>:<page>       — пагинация
    act:<kind>:<sid>:<idx>       — действие над элементом страницы
    target:clear                 — сбросить выбранный лот
    close                        — спрятать клавиатуру
    noop                         — заглушка (например, "X из Y")
"""
from __future__ import annotations

import re
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# ─────────────── главное меню ───────────────

MENU_KIND_STATUS = "status"
MENU_KIND_BALANCE = "balance"
MENU_KIND_LOTS = "lots"
MENU_KIND_MAPS = "mappings"
MENU_KIND_GROUPS = "groups"
MENU_KIND_NS_CATS = "ns_cats"
MENU_KIND_NS_SEARCH = "ns_search_hint"
MENU_KIND_SYNC = "sync"
MENU_KIND_ORDERS = "orders"
MENU_KIND_PROBLEMS = "problems"
MENU_KIND_PENDING = "pending_confirm"  # заказы старше 24ч без подтверждения
MENU_KIND_STATS = "stats"
MENU_KIND_RECONNECT = "fp_reconnect"
MENU_KIND_HELP = "help"


def main_menu(target_lot_label: str | None = None) -> InlineKeyboardMarkup:
    """
    Главное меню. Если задан target_lot_label — добавляется верхняя строка
    с информацией о выбранном целевом лоте и кнопкой сброса.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if target_lot_label:
        rows.append([
            InlineKeyboardButton(
                text=f"🎯 Цель: {target_lot_label}",
                callback_data="noop",
            ),
            InlineKeyboardButton(text="✖ Сбросить", callback_data="target:clear"),
        ])
    rows.extend([
        [
            InlineKeyboardButton(text="📊 Статус", callback_data=f"menu:{MENU_KIND_STATUS}"),
            InlineKeyboardButton(text="💰 Балансы", callback_data=f"menu:{MENU_KIND_BALANCE}"),
        ],
        [
            InlineKeyboardButton(text="🛒 Лоты FunPay", callback_data=f"menu:{MENU_KIND_LOTS}"),
            InlineKeyboardButton(text="🗺 Маппинги", callback_data=f"menu:{MENU_KIND_MAPS}"),
        ],
        [
            InlineKeyboardButton(text="📁 Группы лотов", callback_data=f"menu:{MENU_KIND_GROUPS}"),
            InlineKeyboardButton(text="🗂 Каталог NS", callback_data=f"menu:{MENU_KIND_NS_CATS}"),
        ],
        [
            InlineKeyboardButton(
                text="🔍 Поиск NS", callback_data=f"menu:{MENU_KIND_NS_SEARCH}"
            ),
            InlineKeyboardButton(text="🔄 Синхронизация", callback_data=f"menu:{MENU_KIND_SYNC}"),
        ],
        [
            InlineKeyboardButton(text="📦 Заказы", callback_data=f"menu:{MENU_KIND_ORDERS}"),
            InlineKeyboardButton(
                text="⏳ Ждут подтв.",
                callback_data=f"menu:{MENU_KIND_PENDING}",
            ),
        ],
        [
            InlineKeyboardButton(text="🧯 Проблемы", callback_data=f"menu:{MENU_KIND_PROBLEMS}"),
            InlineKeyboardButton(text="📈 Прибыль", callback_data=f"menu:{MENU_KIND_STATS}"),
        ],
        [
            InlineKeyboardButton(
                text="🔌 FunPay reconnect", callback_data=f"menu:{MENU_KIND_RECONNECT}"
            ),
        ],
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data=f"menu:{MENU_KIND_HELP}"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────── общие кнопки ───────────────


def _close_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="✖ Закрыть", callback_data="close")


def _back_to_menu_btn() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="🏠 Меню", callback_data=f"menu:home")


def _noop_btn(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data="noop")


def pagination_row(kind: str, sid: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    """Строка кнопок «◀  X/Y  ▶»."""
    if total_pages <= 1:
        return []
    prev_page = (page - 1) % total_pages
    next_page = (page + 1) % total_pages
    return [
        InlineKeyboardButton(text="◀", callback_data=f"pg:{kind}:{sid}:{prev_page}"),
        _noop_btn(f"{page + 1}/{total_pages}"),
        InlineKeyboardButton(text="▶", callback_data=f"pg:{kind}:{sid}:{next_page}"),
    ]


# ─────────────── списки с действиями ───────────────


def list_keyboard(
    *,
    kind: str,
    sid: str,
    page: int,
    total_pages: int,
    item_buttons: list[list[InlineKeyboardButton]] | None = None,
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
    include_close: bool = True,
    include_menu: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if item_buttons:
        rows.extend(item_buttons)
    pg = pagination_row(kind, sid, page, total_pages)
    if pg:
        rows.append(pg)
    if extra_rows:
        rows.extend(extra_rows)
    bottom: list[InlineKeyboardButton] = []
    if include_menu:
        bottom.append(_back_to_menu_btn())
    if include_close:
        bottom.append(_close_btn())
    if bottom:
        rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(
    *,
    yes_data: str,
    no_data: str = "close",
    yes_text: str = "✅ Подтвердить",
    no_text: str = "✖ Отмена",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=yes_text, callback_data=yes_data),
                InlineKeyboardButton(text=no_text, callback_data=no_data),
            ]
        ]
    )


def single_close_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_back_to_menu_btn(), _close_btn()]])


def pending_confirm_kb() -> InlineKeyboardMarkup:
    """
    Клавиатура под последним сообщением /pending_confirm.

    Добавляет верхнюю кнопку «🔄 Sync с FunPay» — она запускает
    `sync_pending_confirmation` (см. src/orders/sync_paid.py),
    чтобы вычистить из БД фантомные заказы, которые саппорт
    FunPay уже подтвердил тихо. Кнопка нужна именно здесь:
    пользователь видит мусорный список → жмёт sync → видит чистый.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Sync с FunPay (чистка фантомов)",
                callback_data="sync_pending_confirm",
            )],
            [_back_to_menu_btn(), _close_btn()],
        ]
    )


# ─────────────── обрезка названий ───────────────

# Эмодзи и спецсимволы из FunPay-названий: убираем для кнопок
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"  # символы и пиктограммы
    "\U0001F600-\U0001F64F"  # эмоции
    "\U0001F680-\U0001F6FF"  # транспорт
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\u200d\ufe0f"  # ZWJ и variation selector
    "]+",
    flags=re.UNICODE,
)


def _clean_title(text: str | None) -> str:
    """Чистит название от эмодзи, повторяющихся разделителей и хвостов."""
    if not text:
        return ""
    s = _EMOJI_PATTERN.sub(" ", str(text))
    s = re.sub(r"\s+", " ", s).strip()
    # частые мусорные хвосты в funpay-описаниях
    s = re.sub(r"\s*[•·|]\s*$", "", s)
    return s


def short_title(text: str | None, limit: int = 40) -> str:
    """Урезать название до limit символов, добавив многоточие."""
    s = _clean_title(text)
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def format_money(value: Any, suffix: str = "") -> str:
    """Деньги: показываем не больше 2 знаков, без хвостовых нулей."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value or "—")
    if v >= 100:
        text = f"{v:.0f}"
    elif v >= 1:
        text = f"{v:.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}" if suffix else text


# ─────────────── label'ы для кнопок ───────────────


def ns_service_label(svc: Any, max_len: int = 36) -> str:
    """
    Текст кнопки выбора NS-услуги. Стараемся вместить:
    «<значимая часть имени> · <цена><валюта>».
    """
    name = short_title(svc.service_name, limit=max_len - 14)
    price = format_money(svc.price, suffix=(svc.currency or "USD"))
    label = f"{name} · {price}" if name else price
    if len(label) > max_len:
        label = label[: max_len - 1] + "…"
    return label


def funpay_lot_label(lot: Any, max_len: int = 30) -> str:
    """Текст кнопки выбора FunPay-лота: только название, без ID."""
    title = (
        getattr(lot, "description", None)
        or getattr(lot, "title", None)
        or getattr(lot, "name", None)
        or ""
    )
    return short_title(title, limit=max_len) or "—"


def mapping_label(m: Any, max_len: int = 32) -> str:
    """Текст для кнопки маппинга — берём label, если его нет — id."""
    if getattr(m, "label", None):
        return short_title(m.label, limit=max_len)
    return f"#{m.funpay_lot_id}"


# ─────────────── форматтеры карточек ───────────────


def format_ns_service_line(svc: Any) -> str:
    """Одна компактная строка в списке NS-услуг."""
    name = short_title(svc.service_name, limit=60)
    stock = svc.in_stock if svc.in_stock is not None else "?"
    cur = (svc.currency or "USD").strip() or "USD"
    price = format_money(svc.price)
    return (
        f"<code>#{svc.service_id}</code> "
        f"<b>{name}</b>\n"
        f"   {price} {cur} · stock: <b>{stock}</b>"
    )


def format_ns_category_line(cat: Any) -> str:
    total = sum(s.in_stock for s in cat.services)
    return (
        f"<code>#{cat.category_id}</code> "
        f"<b>{cat.category_name}</b>\n"
        f"   {len(cat.services)} услуг · stock {total}"
    )


def format_funpay_lot_line(lot: Any) -> str:
    idx = getattr(lot, "_ui_index", None)
    prefix = f"<b>#{idx}</b> · " if idx is not None else ""
    lot_id = (
        getattr(lot, "id", None)
        or getattr(lot, "lot_id", None)
        or getattr(lot, "offer_id", None)
        or "?"
    )
    title = short_title(
        getattr(lot, "description", None)
        or getattr(lot, "title", None)
        or getattr(lot, "name", None)
        or "",
        limit=110,
    )
    price = (
        getattr(lot, "price", None)
        or getattr(lot, "cost", None)
        or "—"
    )
    return (
        f"{prefix}<code>{lot_id}</code> · <b>{format_money(price)}</b>\n"
        f"   {title}"
    )


def _format_markup_percent(value: Any) -> str:
    """Целые без '.0', дробные с обрезкой хвостовых нулей."""
    if value is None:
        return "default"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return f"{value}%"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}%"
    text = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def format_mapping_line(m: Any) -> str:
    status = "✅" if m.enabled else "⏸"
    markup = _format_markup_percent(m.markup_percent)
    label = short_title(getattr(m, "label", None), limit=60)
    title = f" — {label}" if label else ""
    group = getattr(m, "_group_name", None)
    group_text = f" · group <b>{short_title(group, 24)}</b>" if group else ""
    return (
        f"{status} <code>{m.funpay_lot_id}</code> → "
        f"NS#{m.ns_service_id}{title} · markup <b>{markup}</b>{group_text}"
    )


def format_lot_group_line(group: Any) -> str:
    status = "✅" if getattr(group, "enabled", True) else "⏸"
    name = short_title(getattr(group, "name", None), limit=48)
    markup = _format_markup_percent(getattr(group, "markup_percent", None))
    stock_cap = getattr(group, "stock_cap", None)
    count = getattr(group, "_mappings_count", 0)
    active = getattr(group, "_active_mappings_count", 0)
    stock = stock_cap if stock_cap is not None else "global"
    return (
        f"{status} <b>{name}</b>\n"
        f"   markup <b>{markup}</b> · stock <b>{stock}</b> · lots <b>{active}/{count}</b>"
    )


def render_list(
    *,
    page_items: list[Any],
    formatter,
    title: str,
    page: int,
    total_pages: int,
    total_items: int,
    empty_text: str = "Ничего не найдено.",
) -> str:
    if total_items == 0:
        return f"<b>{title}</b>\n\n{empty_text}"
    # компактный одностроковый заголовок: меньше повторов на скрине,
    # больше места под собственно карточки
    if total_pages > 1:
        header = f"<b>{title}</b>  ·  {total_items}  ·  {page + 1}/{total_pages}"
    else:
        header = f"<b>{title}</b>  ·  {total_items}"
    body = "\n".join(formatter(it) for it in page_items)
    return f"{header}\n\n{body}"


# ─────────────── текстовые подсказки ───────────────


HINT_NS_SEARCH = (
    "🔍 <b>Поиск по каталогу NS</b>\n\n"
    "Используй команду:\n"
    "<code>/ns_search apple usa 5</code>\n\n"
    "Можно несколько слов через пробел — найду строки, в названии "
    "услуги или категории которых встречаются <b>все</b> указанные слова. "
    "Чем больше слов — тем точнее результат."
)


MENU_GREETING = (
    "🤖 <b>NS↔FunPay Bridge</b>\n\n"
    "Я слежу за каталогом ns.gifts и обновляю твои лоты на FunPay. "
    "Выбирай раздел кнопкой ниже или используй команды (/help)."
)
