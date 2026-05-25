"""
Pure-функции, генерящие клавиатуры для shop-бота.

Вынесены отдельно от bot.py, чтобы:
  - тестировать без поднятия aiogram-бота / БД / Telegram;
  - переиспользовать одну и ту же сетку в разных handler'ах
    (например, после FSM-поиска возвращаем главное меню одной строчкой).

Все callback_data строки централизованы здесь — это «контракт» между
клавиатурой и handler'ом в bot.py. Если меняешь префикс — меняй в обоих
местах одновременно.

Callback ABI:
  cats:{page}      — лента групп каталога, страница page
  grp:{slug}:{page}— drill-down в группу (page — страница её services'ов
                     если в группе 1 категория)
  cat:{cid}:{page} — список услуг конкретной NS-категории
  svc:{sid}        — карточка услуги
  buy:{sid}        — стаб покупки (Sprint 3)
  srh:{sid}:{page} — пагинация результатов поиска по session_id
  close            — удалить сообщение
  noop             — no-op (центральная кнопка «1/N»)
  cancel           — выход из FSM
"""
from __future__ import annotations

import html
from typing import Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


# ─── public sizing constants ────────────────────────────────────────

# Сколько групп каталога на страницу (вертикальная лента).
# 10 — поместится без скролла на большинстве экранов.
CATALOG_GROUPS_PAGE_SIZE = 10

# Сколько услуг на страницу внутри категории.
SERVICES_PAGE_SIZE = 8

# В сколько столбцов раскладываем региональные варианты группы.
# 2 — оптимум: короткие коды стран (US/EU/UK/DE) умещаются, кнопки
# удобно тапать большим пальцем.
VARIANTS_GRID_COLS = 2

# Длина текста на inline-кнопке (Telegram лимит — 256 байт UTF-8,
# но в реальности всё что >64 символов плохо рендерится).
BUTTON_TEXT_MAX = 64


# ─── reply (главное меню) ───────────────────────────────────────────


# Тексты reply-кнопок. Они же используются как match'ер в bot.py
# (F.text == BTN_CATALOG и т.д.) — поэтому константы.
BTN_CATALOG = "🛍 Каталог"
BTN_SEARCH = "🔍 Поиск"
BTN_BALANCE = "💰 Баланс"
BTN_ORDERS = "📦 Заказы"
BTN_REF = "👥 Рефералы"
BTN_SUPPORT = "🆘 Поддержка"
BTN_CANCEL = "✖ Отмена"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    Главное reply-меню магазина — 3 ряда по 2 кнопки.

    is_persistent=True заставляет Telegram держать клавиатуру свёрнутой
    в нижней панели даже после скрытия — пользователь раз увидел и
    клавиатура остаётся доступна.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CATALOG), KeyboardButton(text=BTN_SEARCH)],
            [KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_ORDERS)],
            [KeyboardButton(text=BTN_REF), KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Минимальная reply-клавиатура с одной кнопкой «Отмена» — для FSM-сценариев."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        is_persistent=False,
        one_time_keyboard=False,
    )


# ─── helpers ────────────────────────────────────────────────────────


def _format_rub_compact(kopecks: int) -> str:
    """1234500 → '12 345 ₽' (округление до рубля, без копеек)."""
    rub_int = (kopecks + 50) // 100
    groups = []
    s = str(rub_int)
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    return "\u00a0".join(reversed(groups)) + "\u00a0₽"


def _format_rub_full(kopecks: int) -> str:
    """1234567 → '12 345,67 ₽' (с копейками — для карточки товара)."""
    rub_int = kopecks // 100
    rub_frac = kopecks % 100
    groups = []
    s = str(rub_int)
    while s:
        groups.append(s[-3:])
        s = s[:-3]
    integer_part = "\u00a0".join(reversed(groups))
    if rub_frac:
        return f"{integer_part},{rub_frac:02d}\u00a0₽"
    return f"{integer_part}\u00a0₽"


def _truncate(text: str, max_len: int = BUTTON_TEXT_MAX) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _paginate(items: Sequence, page: int, page_size: int) -> tuple[Sequence, int, int]:
    """Возвращает (срез, нормализованный page, total_pages)."""
    if not items:
        return [], 0, 0
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    return items[start : start + page_size], page, total_pages


def _nav_row(
    *,
    page: int,
    total_pages: int,
    prefix: str,
) -> list[InlineKeyboardButton]:
    """
    Универсальный navigation-блок ‹ N/M › для пагинации.
    prefix — что подставлять перед номером страницы в callback_data,
    например 'cats' → 'cats:1', или 'srh:abc' → 'srh:abc:1'.

    Возвращает [] если total_pages ≤ 1 (показывать нечего).
    """
    if total_pages <= 1:
        return []
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(
            text="‹", callback_data=f"{prefix}:{page - 1}",
        ))
    row.append(InlineKeyboardButton(
        text=f"{page + 1}/{total_pages}", callback_data="noop",
    ))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton(
            text="›", callback_data=f"{prefix}:{page + 1}",
        ))
    return row


# ─── catalog: groups list (страница) ────────────────────────────────


def catalog_groups_keyboard(
    *,
    groups: Sequence,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Главный экран каталога: пагинированный список групп.

    Header:
      🛍 Каталог
      стр. 1 из 23 · всего 230

    Items (10):
      [ Apple Gift Card · 13 регионов · от 91 ₽ ]
      ...

    Footer:
      [ ‹ ] [ 1/23 ] [ › ]
      [ 🔍 Поиск ] [ ✖ Закрыть ]
    """
    if not groups:
        return (
            "📭 <b>Каталог пока пуст.</b>\n\n"
            "Загляни через 1–2 минуты — каталог обновляется автоматически.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="cats:0")],
            ]),
        )

    page_items, page, total_pages = _paginate(
        list(groups), page=page, page_size=CATALOG_GROUPS_PAGE_SIZE,
    )
    header = (
        f"🛍 <b>Каталог</b>\n"
        f"<i>стр. {page + 1} из {total_pages} · всего {len(groups)} разделов</i>"
    )

    rows: list[list[InlineKeyboardButton]] = []
    for grp in page_items:
        cheapest = _format_rub_compact(grp.cheapest_price_kopecks)
        if grp.variants_count > 1:
            mid = f"· {grp.variants_count} регионов "
        else:
            mid = ""
        label = f"{grp.base_name} {mid}· от {cheapest}"
        rows.append([InlineKeyboardButton(
            text=_truncate(label),
            callback_data=f"grp:{grp.group_slug}:0",
        )])

    nav = _nav_row(page=page, total_pages=total_pages, prefix="cats")
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🔍 Поиск", callback_data="search_prompt"),
        InlineKeyboardButton(text="✖ Закрыть", callback_data="close"),
    ])
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── catalog: variants of a group (grid) ────────────────────────────


def variants_grid_keyboard(
    *,
    variants: Sequence,
    base_name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Drill-down в группу: показ региональных/платформенных вариантов
    в grid VARIANTS_GRID_COLS × N.

    Header:
      🛍 Apple Gift Card
      13 регионов · от 91 ₽

    Grid (2 col):
      [ US · от 1 084 ]  [ EU · от 1 350 ]
      [ UK · от 1 530 ]  [ DE · от 1 453 ]
      ...

    Footer:
      [ « К каталогу ]
    """
    if not variants:
        return (
            f"📭 В группе <b>{html.escape(base_name)}</b> пока ничего нет.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="« К каталогу", callback_data="cats:0"),
            ]]),
        )

    cheapest_overall = min(v.cheapest_price_kopecks for v in variants)
    header = (
        f"🛍 <b>{html.escape(base_name)}</b>\n"
        f"<i>{len(variants)} вариантов · от {_format_rub_compact(cheapest_overall)}</i>\n"
        f"Выбери регион/платформу:"
    )

    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for v in variants:
        # Хвост после `|` — он же variant. Если разделителя нет, показываем
        # имя целиком (но base_name уже в заголовке — будет дубль; так и
        # должно быть для одиночных категорий, чтобы юзер не путался).
        if "|" in v.category_name:
            _, _, tail = v.category_name.partition("|")
            tail = tail.strip()
        else:
            tail = v.category_name
        price = _format_rub_compact(v.cheapest_price_kopecks)
        label = f"{tail} · от {price}"
        current_row.append(InlineKeyboardButton(
            text=_truncate(label, BUTTON_TEXT_MAX // VARIANTS_GRID_COLS + 16),
            callback_data=f"cat:{v.category_id}:0",
        ))
        if len(current_row) >= VARIANTS_GRID_COLS:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([
        InlineKeyboardButton(text="« К каталогу", callback_data="cats:0"),
    ])
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── services within a category (page) ──────────────────────────────


def services_page_keyboard(
    *,
    services: Sequence,
    total: int,
    category_id: int,
    page: int,
    group_slug: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Список услуг внутри NS-категории с пагинацией.

    Header:
      🛍 Apple Gift Card | US
      стр. 1 из 3 · всего 20

    Items (8):
      [ Apple US $5 — 397 ₽ ]
      [ Apple US $10 — 794 ₽ ]
      ...

    Footer:
      [ ‹ 1/3 › ]
      [ « Назад к группе ] (или к каталогу)
    """
    if not services:
        text = (
            "📭 Здесь временно ничего нет — поставщик не подвёз. "
            "Загляни позже или поищи похожее через 🔍 Поиск."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            _back_button_row(group_slug=group_slug),
        ])
        return text, markup

    total_pages = max(1, (total + SERVICES_PAGE_SIZE - 1) // SERVICES_PAGE_SIZE)
    cat_name = services[0].category_name or "Категория"
    header = (
        f"🛍 <b>{html.escape(cat_name)}</b>\n"
        f"<i>стр. {page + 1} из {total_pages} · всего {total}</i>"
    )

    rows: list[list[InlineKeyboardButton]] = []
    for svc in services:
        price = _format_rub_compact(svc.rub_price_kopecks)
        label = f"{svc.service_name} — {price}"
        if svc.in_stock < 5:
            label = f"⚠ {label} (мало: {svc.in_stock})"
        rows.append([InlineKeyboardButton(
            text=_truncate(label),
            callback_data=f"svc:{svc.ns_service_id}",
        )])

    nav = _nav_row(
        page=page, total_pages=total_pages,
        prefix=f"cat:{category_id}",
    )
    if nav:
        rows.append(nav)
    rows.append(_back_button_row(group_slug=group_slug))
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


def _back_button_row(group_slug: str | None) -> list[InlineKeyboardButton]:
    """Кнопка возврата: на группу (если пришли через drill-down) или
    в каталог (если это одиночная группа)."""
    if group_slug:
        return [InlineKeyboardButton(
            text="« Назад к группе",
            callback_data=f"grp:{group_slug}:0",
        )]
    return [InlineKeyboardButton(text="« К каталогу", callback_data="cats:0")]


# ─── service card ───────────────────────────────────────────────────


def service_card_keyboard(
    *,
    svc,
    group_slug: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка услуги с хлебными крошками и кнопкой «Купить» (если в наличии).
    """
    price = _format_rub_full(svc.rub_price_kopecks)
    in_stock = svc.in_stock > 0
    stock_line = (
        f"📦 В наличии: <b>{svc.in_stock}</b> шт."
        if in_stock else "🚫 <b>Нет в наличии</b>"
    )

    crumbs = "🛍"
    base_name = getattr(svc, "base_name", None)
    if base_name:
        crumbs += f" → {html.escape(base_name)}"
    if svc.category_name and svc.category_name != base_name:
        crumbs += f" → {html.escape(svc.category_name)}"

    text = (
        f"<i>{crumbs}</i>\n\n"
        f"🛒 <b>{html.escape(svc.service_name)}</b>\n\n"
        f"💰 Цена: <b>{price}</b>\n"
        f"{stock_line}\n\n"
        "<i>Оплата откроется в ближайшие дни. "
        "Пока изучи ассортимент и пригласи друзей по реф-ссылке "
        "(👥 Рефералы) — получишь 1% с их покупок.</i>"
    )

    rows: list[list[InlineKeyboardButton]] = []
    if in_stock:
        rows.append([InlineKeyboardButton(
            text="💳 Купить", callback_data=f"buy:{svc.ns_service_id}",
        )])
    cat_id = getattr(svc, "category_id", 0) or 0
    rows.append([
        InlineKeyboardButton(
            text="« К категории",
            callback_data=f"cat:{cat_id}:0",
        ),
        InlineKeyboardButton(text="🏪 Каталог", callback_data="cats:0"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── search results page ────────────────────────────────────────────


def search_results_keyboard(
    *,
    page_items: Sequence,
    total: int,
    page: int,
    session_id: str,
    query: str,
    truncated_at: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Результаты FSM-поиска с пагинацией.

    Header:
      🔍 Поиск: «apple»
      найдено 47, стр. 1 из 6
      Показаны первые 50 — уточни запрос для большей точности.  (если truncated_at)

    Items (8):
      [ Apple US $5 — 397 ₽ · Apple Gift Card | US ]
      ...

    Footer:
      [ ‹ 1/6 › ]
      [ 🏪 К каталогу ]
    """
    total_pages = max(1, (total + SERVICES_PAGE_SIZE - 1) // SERVICES_PAGE_SIZE)
    trunc_note = ""
    if truncated_at and total >= truncated_at:
        trunc_note = (
            f"\n<i>Показаны первые {truncated_at} — уточни запрос для большей точности.</i>"
        )
    header = (
        f"🔍 <b>Поиск:</b> «{html.escape(query)}»\n"
        f"<i>найдено {total}, стр. {page + 1} из {total_pages}</i>"
        f"{trunc_note}"
    )

    rows: list[list[InlineKeyboardButton]] = []
    for svc in page_items:
        price = _format_rub_compact(svc.rub_price_kopecks)
        cat_tail = ""
        if getattr(svc, "category_name", None):
            cat_tail = f" · {svc.category_name}"
        label = f"{svc.service_name} — {price}{cat_tail}"
        rows.append([InlineKeyboardButton(
            text=_truncate(label),
            callback_data=f"svc:{svc.ns_service_id}",
        )])

    nav = _nav_row(
        page=page, total_pages=total_pages,
        prefix=f"srh:{session_id}",
    )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏪 К каталогу", callback_data="cats:0")])
    return header, InlineKeyboardMarkup(inline_keyboard=rows)
