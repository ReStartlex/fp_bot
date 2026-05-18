"""
Фабрики inline-клавиатур и форматтеры карточек для Telegram-бота.

Принцип: всё, что строит клавиатуру или формат сообщения, живёт здесь.
Хендлеры в bot.py — только склейка логики и I/O.

Стандартизированные callback_data префиксы:
    menu:<action>                — главное меню
    pg:<kind>:<sid>:<page>       — пагинация
    act:<kind>:<sid>:<idx>       — действие над элементом страницы
    close                        — спрятать клавиатуру
    noop                         — заглушка (например, "X из Y")
"""
from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


# ─────────────── главное меню ───────────────

MENU_KIND_STATUS = "status"
MENU_KIND_BALANCE = "balance"
MENU_KIND_LOTS = "lots"
MENU_KIND_MAPS = "mappings"
MENU_KIND_NS_CATS = "ns_cats"
MENU_KIND_NS_SEARCH = "ns_search_hint"
MENU_KIND_SYNC = "sync"
MENU_KIND_ORDERS = "orders"
MENU_KIND_RECONNECT = "fp_reconnect"
MENU_KIND_HELP = "help"


def main_menu() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="📊 Статус", callback_data=f"menu:{MENU_KIND_STATUS}"),
            InlineKeyboardButton(text="💰 Балансы", callback_data=f"menu:{MENU_KIND_BALANCE}"),
        ],
        [
            InlineKeyboardButton(text="🛒 Лоты FunPay", callback_data=f"menu:{MENU_KIND_LOTS}"),
            InlineKeyboardButton(text="🗺 Маппинги", callback_data=f"menu:{MENU_KIND_MAPS}"),
        ],
        [
            InlineKeyboardButton(text="🗂 Каталог NS", callback_data=f"menu:{MENU_KIND_NS_CATS}"),
            InlineKeyboardButton(
                text="🔍 Поиск NS", callback_data=f"menu:{MENU_KIND_NS_SEARCH}"
            ),
        ],
        [
            InlineKeyboardButton(text="🔄 Синхронизация", callback_data=f"menu:{MENU_KIND_SYNC}"),
            InlineKeyboardButton(text="📦 Заказы", callback_data=f"menu:{MENU_KIND_ORDERS}"),
        ],
        [
            InlineKeyboardButton(
                text="🔌 FunPay reconnect", callback_data=f"menu:{MENU_KIND_RECONNECT}"
            ),
            InlineKeyboardButton(text="❓ Помощь", callback_data=f"menu:{MENU_KIND_HELP}"),
        ],
    ]
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


# ─────────────── форматтеры карточек ───────────────


def format_ns_service_line(svc: Any) -> str:
    """Одна строка списка NS-услуг."""
    name = (svc.service_name or "").strip()
    name_short = name[:55] + "…" if len(name) > 56 else name
    stock = svc.in_stock if svc.in_stock is not None else "?"
    cur = (svc.currency or "USD").strip() or "USD"
    return (
        f"<code>{svc.service_id:>5}</code> "
        f"<b>{name_short}</b>\n"
        f"   {svc.price:.4f} {cur} • stock: <b>{stock}</b>"
    )


def format_ns_category_line(cat: Any) -> str:
    total = sum(s.in_stock for s in cat.services)
    return (
        f"<code>{cat.category_id:>4}</code> "
        f"<b>{cat.category_name}</b> — "
        f"{len(cat.services)} услуг · stock {total}"
    )


def format_funpay_lot_line(lot: Any) -> str:
    lot_id = (
        getattr(lot, "id", None)
        or getattr(lot, "lot_id", None)
        or getattr(lot, "offer_id", None)
        or "?"
    )
    title = (
        getattr(lot, "description", None)
        or getattr(lot, "title", None)
        or getattr(lot, "name", None)
        or ""
    )
    price = (
        getattr(lot, "price", None)
        or getattr(lot, "cost", None)
        or "—"
    )
    title_short = str(title or "")
    if len(title_short) > 60:
        title_short = title_short[:60] + "…"
    return (
        f"<code>{lot_id}</code> · <b>{price}</b>\n"
        f"   {title_short}"
    )


def format_mapping_line(m: Any) -> str:
    status = "✅" if m.enabled else "⏸"
    markup = f"{m.markup_percent}%" if m.markup_percent is not None else "default"
    cap = m.stock_cap if m.stock_cap is not None else "default"
    label = (m.label or "").strip()
    title = f" • {label}" if label else ""
    return (
        f"{status} <code>{m.funpay_lot_id}</code> → NS#{m.ns_service_id}{title}\n"
        f"   markup: {markup} · cap: {cap}"
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
    header = (
        f"<b>{title}</b>\n"
        f"Всего: <b>{total_items}</b> · "
        f"страница <b>{page + 1}/{total_pages}</b>\n"
        "─" * 8
    )
    body = "\n".join(formatter(it) for it in page_items)
    return f"{header}\n{body}"


# ─────────────── текстовые подсказки ───────────────


HINT_NS_SEARCH = (
    "🔍 <b>Поиск по каталогу NS</b>\n\n"
    "Используй команду:\n"
    "<code>/ns_search apple usa 5</code>\n\n"
    "Можно несколько слов через пробел — найду строки, где встречается "
    "<b>любое</b> из них в названии услуги или категории."
)


MENU_GREETING = (
    "🤖 <b>NS↔FunPay Bridge</b>\n\n"
    "Я слежу за каталогом ns.gifts и обновляю твои лоты на FunPay. "
    "Выбирай раздел кнопкой ниже или используй команды (/help)."
)
