"""
Pure-функции, генерящие клавиатуры для shop-бота NeuroDrop.

Вынесены отдельно от bot.py, чтобы:
  - тестировать без поднятия aiogram-бота / БД / Telegram;
  - переиспользовать одну и ту же сетку в разных handler'ах
    (например, после FSM-поиска возвращаем главное меню одной строчкой).

Все callback_data строки централизованы здесь — это «контракт» между
клавиатурой и handler'ом в bot.py. Если меняешь префикс — меняй в обоих
местах одновременно.

Callback ABI:
  cats:{page}      — лента групп каталога, страница page
  grp:{slug}:{page}— drill-down в группу (региональные варианты)
  cat:{cid}:{page} — список услуг конкретной NS-категории
  svc:{sid}        — карточка услуги
  buy:{sid}        — стаб покупки (Sprint 3)
  srh:{sid}:{page} — пагинация результатов поиска по session_id
  bal              — открыть страницу баланса (refresh)
  bal_hist:{page}  — история операций по балансу
  topup:crypto     — пополнение CryptoBot (Sprint 3 stub)
  topup:stars      — пополнение Telegram Stars (Sprint 3 stub)
  topup:card       — пополнение картой (Sprint 4 stub)
  ref              — открыть страницу рефералов (refresh)
  ref_share        — открыть share-меню
  close            — удалить сообщение
  noop             — no-op (центральная кнопка «1/N»)
  cancel           — выход из FSM
  search_prompt    — открыть FSM-поиск
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

from src.shop.taxonomy_icons import (
    brand_emoji,
    featured_badge,
    region_flag,
    stock_bar,
    stock_status_text,
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
    Главный экран каталога: пагинированный список групп с brand-эмодзи
    и featured-бейджами для топа.

    Header:
      🛍 NeuroDrop · Каталог
      <i>23 раздела · стр. 1/3</i>

    Items (10):
      [ 🔥 🍎 Apple Gift Card · 13 регионов · от 91 ₽ ]   (топ-1 → 🔥)
      [ ⭐ 🎮 Steam Wallet Code · 9 регионов · от 29 ₽ ]   (топ-2 → ⭐)
      [ 💎 🎲 Roblox · 5 номиналов · от 238 ₽ ]            (топ-3 → 💎)
      [ 🎮 PlayStation®Store · 2 региона · от 426 ₽ ]
      ...

    Featured-ранжирование на первой странице:
      Топ-3 по variants_count получают бейдж 🔥 ⭐ 💎. Логика «авто»:
      больше вариантов = больше покупателю выбора = более ходовая
      категория. Когда добавим runtime override (operator вручную)
      — этот fallback останется как «default sort».

    Footer:
      [ ‹ ] [ 1/3 ] [ › ]
      [ 🔍 Поиск ]   [ ✖ Закрыть ]
    """
    if not groups:
        return (
            "📭 <b>Каталог пока пуст.</b>\n\n"
            "Загляни через 1–2 минуты — каталог обновляется автоматически.\n"
            "<i>Иногда поставщик NS.gifts кратко уходит на профилактику.</i>",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="cats:0")],
            ]),
        )

    page_items, page, total_pages = _paginate(
        list(groups), page=page, page_size=CATALOG_GROUPS_PAGE_SIZE,
    )
    # Заголовок: бренд + краткая статистика. NeuroDrop вставляем
    # эксплицитно, чтобы пользователь видел identitiy на каждой странице.
    header_lines = [
        "🛍 <b>NeuroDrop · Каталог</b>",
        f"<i>{len(groups)} раздел{_plural_razdel(len(groups))} · "
        f"стр. {page + 1}/{total_pages}</i>",
    ]
    header = "\n".join(header_lines)

    rows: list[list[InlineKeyboardButton]] = []
    # Топ-3 по variants_count (на ВСЕЙ выборке, не только текущей странице).
    # Сортируем descending; ничьи — стабильны по исходному порядку.
    # Берём id из page_items для сопоставления, но badge'и считаем по
    # глобальной позиции, чтобы featured был «на главной».
    sorted_for_rank = sorted(
        list(groups), key=lambda g: -g.variants_count,
    )
    rank_by_slug = {g.group_slug: i for i, g in enumerate(sorted_for_rank)}

    for grp in page_items:
        brand = brand_emoji(grp.base_name)
        cheapest = _format_rub_compact(grp.cheapest_price_kopecks)
        # Middle часть: для одиночных — без «X регионов», иначе видно
        # сколько вариантов есть.
        if grp.variants_count > 1:
            mid = f" · {grp.variants_count} {_plural_region(grp.variants_count)}"
        else:
            mid = ""
        rank = rank_by_slug.get(grp.group_slug, 9999)
        badge = featured_badge(rank)
        # Сборка: [badge] brand base_name · X регионов · от Y ₽
        prefix = f"{badge} {brand} " if badge else f"{brand} "
        label = f"{prefix}{grp.base_name}{mid} · от {cheapest}"
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


def _plural_razdel(n: int) -> str:
    """Русское склонение «раздел/раздела/разделов» для счётчика."""
    if 11 <= (n % 100) <= 14:
        return "ов"
    last = n % 10
    if last == 1:
        return ""
    if 2 <= last <= 4:
        return "а"
    return "ов"


def _plural_region(n: int) -> str:
    """Русское склонение «регион/региона/регионов»."""
    if 11 <= (n % 100) <= 14:
        return "регионов"
    last = n % 10
    if last == 1:
        return "регион"
    if 2 <= last <= 4:
        return "региона"
    return "регионов"


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
      🍎 Apple Gift Card
      <i>13 регионов · от 91 ₽</i>
      Выбери регион:

    Grid (2 col):
      [ 🇺🇸 US · от 1 084 ]  [ 🇪🇺 EU · от 1 350 ]
      [ 🇬🇧 UK · от 1 530 ]  [ 🇩🇪 DE · от 1 453 ]
      ...

    Footer:
      [ « К каталогу ]
    """
    brand = brand_emoji(base_name)
    if not variants:
        return (
            f"📭 В группе {brand} <b>{html.escape(base_name)}</b> "
            "пока ничего нет.\n"
            "<i>Скорее всего, временно нет в наличии у поставщика. "
            "Загляни позже.</i>",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="« К каталогу", callback_data="cats:0"),
            ]]),
        )

    cheapest_overall = min(v.cheapest_price_kopecks for v in variants)
    # Pluralisation: «1 регион», «2 региона», «5 регионов»
    region_word = _plural_region(len(variants))
    header = (
        f"{brand} <b>{html.escape(base_name)}</b>\n"
        f"<i>{len(variants)} {region_word} · "
        f"от {_format_rub_compact(cheapest_overall)}</i>\n"
        f"Выбери регион:"
    )

    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    for v in variants:
        # Хвост после `|` — он же variant. Если разделителя нет, берём
        # имя целиком (тогда base_name уже в заголовке — дубль; нормально
        # для одиночных категорий, чтобы юзер не путался).
        if "|" in v.category_name:
            _, _, tail = v.category_name.partition("|")
            tail = tail.strip()
        else:
            tail = v.category_name
        flag = region_flag(v.category_name)
        price = _format_rub_compact(v.cheapest_price_kopecks)
        # Сборка: [флаг] tail · от X ₽. Если флага нет — без emoji.
        prefix = f"{flag} " if flag else ""
        label = f"{prefix}{tail} · от {price}"
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
            "📭 <b>Тут пока пусто.</b>\n"
            "Поставщик временно без наличия. Загляни позже или "
            "поищи похожее через 🔍 Поиск — другие регионы могут быть в строю."
        )
        markup = InlineKeyboardMarkup(inline_keyboard=[
            _back_button_row(group_slug=group_slug),
        ])
        return text, markup

    total_pages = max(1, (total + SERVICES_PAGE_SIZE - 1) // SERVICES_PAGE_SIZE)
    cat_name = services[0].category_name or "Категория"
    # Подмешиваем brand-эмодзи + флаг страны в заголовок категории.
    # Это даёт мгновенное визуальное распознавание «где я».
    base = getattr(services[0], "base_name", None) or cat_name
    brand = brand_emoji(base)
    flag = region_flag(cat_name)
    flag_part = f"{flag} " if flag else ""
    header = (
        f"{brand} {flag_part}<b>{html.escape(cat_name)}</b>\n"
        f"<i>стр. {page + 1}/{total_pages} · всего {total}</i>"
    )

    rows: list[list[InlineKeyboardButton]] = []
    for svc in services:
        price = _format_rub_compact(svc.rub_price_kopecks)
        # Stock-маркер:
        #   * 0     — отсеяно репозиторием (in_stock=0 не приходит сюда);
        #   * 1..4  — ⚠ + точное число (низкий запас, юзер видит риск);
        #   * 5..9  — без маркера (нормально, не отвлекаем);
        #   * ≥10   — без маркера (тоже нормально).
        if 0 < svc.in_stock < 5:
            label = f"⚠ {svc.service_name} — {price} · {svc.in_stock} шт."
        else:
            label = f"{svc.service_name} — {price}"
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
    similar: Sequence | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка услуги с хлебными крошками, бэйджами, графическим stock-bar
    и блоком «Похожее» (другие номиналы того же бренда).

    Текст:
      🍎 🇹🇷 Apple Gift Card | TR | 10 TRY
      <i>🛍 → Apple Gift Card → Apple Gift Card | TR</i>

      🚀 Мгновенная выдача · ⚡ В наличии · 🛡 Гарантия

      💰 <b>Цена: 205,30 ₽</b>
      🟢 В наличии (доступно: 100)
      🟩🟩🟩🟩🟩

      <i>Оплата с внутреннего баланса. Пополнить —
      💰 Баланс → 🪙 CryptoBot (1 минута).</i>

    Кнопки (если в наличии):
      [ 💳 Купить за 205,30 ₽ ]
      [ 🔄 Похожее: Apple TR 5 TRY · 102 ₽ ]    (similar[0])
      [ 🔄 Похожее: Apple TR 25 TRY · 513 ₽ ]   (similar[1])
      [ « К категории ]   [ 🏪 Каталог ]

    Параметр similar: список ShopCatalogCache — другие услуги с тем же
    base_name. None = «не передавали», блок не показываем. Пустой
    список = «искали, но ничего больше нет».
    """
    price = _format_rub_full(svc.rub_price_kopecks)
    in_stock_n = int(getattr(svc, "in_stock", 0) or 0)
    in_stock = in_stock_n > 0

    base_name = getattr(svc, "base_name", None) or svc.category_name
    brand = brand_emoji(base_name or "")
    flag = region_flag(svc.category_name or "")
    flag_part = f"{flag} " if flag else ""

    # Хлебные крошки: 🛍 → Brand → Category. Без дубля если category==base.
    crumbs = "🛍"
    if base_name:
        crumbs += f" → {html.escape(base_name)}"
    if svc.category_name and svc.category_name != base_name:
        crumbs += f" → {html.escape(svc.category_name)}"

    # Бэйджи (доверие + UX-сигналы). Показываем разные в зависимости
    # от состояния:
    #   - в наличии: 🚀 Мгновенная выдача + ⚡ В наличии + 🛡 Гарантия
    #   - OOS: только 🛡 Гарантия (не врём про мгновенную выдачу)
    if in_stock:
        badges = "🚀 Мгновенная выдача · ⚡ В наличии · 🛡 Гарантия"
    else:
        badges = "🛡 Гарантия · ⏳ Ждём поставку"

    # Stock-секция: текстовый статус + графический bar.
    # Bar показываем только если есть наличие — для OOS будет визуально
    # «пусто, серое» что плохо для морального духа покупателя.
    stock_status = stock_status_text(in_stock_n)
    if in_stock:
        # cap=max(10, текущий запас) — чтобы bar не показывал «overflow»
        # для крупных запасов (200 шт. → full bar, как и должно быть).
        stock_visual = f"\n{stock_bar(in_stock_n, cap=max(10, in_stock_n))}"
    else:
        stock_visual = ""

    text = (
        f"{brand} {flag_part}<b>{html.escape(svc.service_name)}</b>\n"
        f"<i>{crumbs}</i>\n\n"
        f"<i>{badges}</i>\n\n"
        f"💰 <b>Цена: {price}</b>\n"
        f"{stock_status}{stock_visual}\n\n"
        "<i>Оплата с внутреннего баланса NeuroDrop. "
        "Пополнить — 💰 Баланс → 🪙 CryptoBot (1 минута, "
        "криптой USDT/TON/BTC).</i>"
    )

    rows: list[list[InlineKeyboardButton]] = []
    if in_stock:
        # «Купить за X ₽» — цена прямо на кнопке. Это снижает frictiоn:
        # юзер не возвращается к строке выше чтобы свериться с ценой.
        rows.append([InlineKeyboardButton(
            text=f"💳 Купить за {_format_rub_compact(svc.rub_price_kopecks)}",
            callback_data=f"buy:{svc.ns_service_id}",
        )])
    # Блок «Похожее»: до 3 кнопок по 1 в ряд (короткие, информативные).
    # Показываем ТОЛЬКО реально другие услуги — текущую исключает
    # источник данных (см. list_similar_services в repo).
    if similar:
        for s in list(similar)[:3]:
            s_id = getattr(s, "ns_service_id", None)
            if s_id is None or s_id == svc.ns_service_id:
                continue
            s_flag = region_flag(getattr(s, "category_name", "") or "")
            s_price = _format_rub_compact(getattr(s, "rub_price_kopecks", 0) or 0)
            s_name = getattr(s, "service_name", "") or ""
            s_prefix = f"{s_flag} " if s_flag else ""
            rows.append([InlineKeyboardButton(
                text=_truncate(f"🔄 {s_prefix}{s_name} · {s_price}"),
                callback_data=f"svc:{s_id}",
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


# ─── balance ────────────────────────────────────────────────────────


def balance_keyboard(
    *,
    current_kopecks: int,
    earned_kopecks: int,
    spent_kopecks: int,
    operations_count: int,
    invited_count: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Главная страница «💰 Баланс».

    Текст:
      💰 <b>Твой баланс</b>
      <b>0 ₽</b>

      💸 Заработано: 0 ₽
      🛒 Потрачено:  0 ₽
      📊 Операций:   0

      <i>Баланс пополняется от реферальной программы (1% с каждой
      покупки друга) — пригласи друзей в 👥 Рефералы.</i>

    Кнопки:
      [ 🪙 CryptoBot ]  [ ⭐ Telegram Stars ]
      [ 💳 Картой / СБП ]
      [ 📊 История операций ]
      [ 👥 Пригласить друзей ]
    """
    body = (
        f"💰 <b>Твой баланс</b>\n"
        f"<b>{_format_rub_full(current_kopecks)}</b>\n\n"
        f"💸 Заработано:   <b>{_format_rub_full(earned_kopecks)}</b>\n"
        f"🛒 Потрачено:    <b>{_format_rub_full(spent_kopecks)}</b>\n"
        f"📊 Операций:     <b>{operations_count}</b>\n"
    )
    if invited_count == 0:
        body += (
            "\n💡 <i>Пригласи друзей по своей реф-ссылке — получай <b>1%</b> "
            "с каждой их покупки на этот баланс.</i>"
        )
    else:
        body += (
            f"\n👥 Приглашено друзей: <b>{invited_count}</b>"
            f" — спасибо, что развиваешь NeuroDrop!"
        )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🪙 CryptoBot", callback_data="topup:crypto"),
            InlineKeyboardButton(text="⭐ Stars", callback_data="topup:stars"),
        ],
        [InlineKeyboardButton(text="💳 Картой / СБП", callback_data="topup:card")],
        [InlineKeyboardButton(text="📊 История операций",
                              callback_data="bal_hist:0")],
        [InlineKeyboardButton(text="👥 Пригласить друзей", callback_data="ref")],
    ]
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


def balance_history_keyboard(
    *,
    rows_text: str,
    page: int,
    total_pages: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Страница истории операций по балансу (вызывается из balance_history_render)."""
    nav = _nav_row(page=page, total_pages=total_pages, prefix="bal_hist")
    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="« К балансу", callback_data="bal"),
    ])
    return rows_text, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── referrals ──────────────────────────────────────────────────────


def referrals_keyboard(
    *,
    ref_link: str,
    invited_count: int,
    earned_kopecks: int,
    active_referrals_count: int,
    bonus_percent: float,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Главная страница «👥 Рефералы» NeuroDrop.

    Текст с разделами:
      🔗 Ссылка
      📊 Статистика (приглашено / активных / заработано)
      🎁 Бонусная программа (1 уровень: 1%)
      💡 Подсказка как приглашать
    """
    share_text = (
        "%F0%9F%9B%92%20NeuroDrop%20%E2%80%94%20%D0%BC%D0%B0%D0%B3%D0%B0%D0%B7%D0%B8%D0%BD"
        "%20%D0%BF%D0%BE%D0%B4%D0%B0%D1%80%D0%BE%D1%87%D0%BD%D1%8B%D1%85%20%D0%BA%D0%B0%D1"
        "%80%D1%82%20%E2%80%94%20Apple%2C%20Steam%2C%20Spotify%20%D0%B8%20%D0%B4%D1%80%D1"
        "%83%D0%B3%D0%B8%D0%B5.%20%D0%9C%D0%BE%D0%BC%D0%B5%D0%BD%D1%82%D0%B0%D0%BB%D1%8C"
        "%D0%BD%D0%B0%D1%8F%20%D0%B4%D0%BE%D1%81%D1%82%D0%B0%D0%B2%D0%BA%D0%B0."
    )
    text = (
        "👥 <b>Реферальная программа</b>\n\n"
        "Делись ссылкой — получай <b>{bonus}%</b> с каждой покупки приглашённого "
        "друга на свой внутренний баланс. Балансом можно оплачивать заказы.\n\n"
        "🔗 <b>Твоя ссылка</b>\n"
        "<code>{link}</code>\n\n"
        "📊 <b>Статистика</b>\n"
        "👥 Приглашено:        <b>{invited}</b>\n"
        "💚 Активных (30 дн):  <b>{active}</b>\n"
        "💰 Заработано:        <b>{earned}</b>\n\n"
        "🎁 <b>Бонусная программа</b>\n"
        "• Уровень 1 — прямые приглашения · <b>{bonus}%</b> с покупки\n"
        "<i>(многоуровневая система появится позже)</i>\n\n"
        "💡 <b>Как делиться</b>\n"
        "Кидай ссылку в чаты с друзьями, на форумах, в комментариях, "
        "в соцсетях — кому будут нужны подарочные карты или подписки. "
        "Чем больше людей зайдут — тем больше кэшбэк."
    ).format(
        bonus=int(bonus_percent) if bonus_percent == int(bonus_percent) else bonus_percent,
        link=ref_link, invited=invited_count, active=active_referrals_count,
        earned=_format_rub_full(earned_kopecks),
    )

    rows: list[list[InlineKeyboardButton]] = [
        # «Поделиться» через нативный share-link Telegram.
        [InlineKeyboardButton(
            text="📤 Поделиться ссылкой",
            url=f"https://t.me/share/url?url={ref_link}&text={share_text}",
        )],
        # «Скопировать» — switch_inline_query=text заставляет Telegram
        # открыть chooser «куда отправить» с уже заполненным сообщением.
        [InlineKeyboardButton(
            text="📋 Скопировать ссылку",
            switch_inline_query=ref_link,
        )],
        [InlineKeyboardButton(text="💰 К балансу", callback_data="bal")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── search ─────────────────────────────────────────────────────────


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
        base = getattr(svc, "base_name", None) or ""
        brand = brand_emoji(base) if base else ""
        flag = region_flag(getattr(svc, "category_name", "") or "")
        # Сборка: [brand] [flag] service_name — price.
        # Category name в результатах поиска уже дублирует флаг/бренд,
        # так что хвост убираем — кнопка чище.
        prefix_parts = [p for p in (brand, flag) if p]
        prefix = (" ".join(prefix_parts) + " ") if prefix_parts else ""
        label = f"{prefix}{svc.service_name} — {price}"
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


# ─── top-up (CryptoBot) ─────────────────────────────────────────────


# Предустановленные суммы пополнения — наиболее частые номиналы.
# Подобраны эмпирически: 100 ₽ — минимум для микропокупки, 500 — самый
# популярный, 3000 — топ-сегмент. Если нужно больше — «Своя сумма».
TOPUP_PRESET_AMOUNTS_RUB = (100, 300, 500, 1000, 3000)


def topup_amount_keyboard(
    *, min_rub: int, max_rub: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Меню выбора суммы пополнения через CryptoBot.

    Юзер видит:
      [ 100 ₽ ] [ 300 ₽ ] [ 500 ₽ ]
      [ 1000 ₽ ] [ 3000 ₽ ]
      [ ✏ Своя сумма ]
      [ « К балансу ]

    callback_data:
      tp_amt:{kopecks}   — выбрана предустановленная сумма
      tp_amt:custom      — переход в FSM ввода своей суммы
    """
    text = (
        "🪙 <b>Пополнение через CryptoBot</b>\n\n"
        "Платишь криптой (USDT, TON, BTC, ETH и др.) — мы зачисляем "
        "<b>рубли</b> на твой внутренний баланс. Комиссия CryptoBot ≈ <b>3%</b>, "
        "никаких скрытых сборов.\n\n"
        f"💡 Можно от <b>{min_rub} ₽</b> до <b>{max_rub:,} ₽</b>.\n"
        "Выбери сумму:"
    ).replace(",", " ")  # тонкий неразрывный — выглядит чище: 100 000 ₽

    rows: list[list[InlineKeyboardButton]] = []
    # Раскладываем по 3 кнопки в ряд для первых 3, потом по 2.
    presets = [a for a in TOPUP_PRESET_AMOUNTS_RUB if min_rub <= a <= max_rub]
    if presets:
        row: list[InlineKeyboardButton] = []
        for i, amount in enumerate(presets):
            row.append(InlineKeyboardButton(
                text=f"{amount} ₽",
                callback_data=f"tp_amt:{amount * 100}",  # копейки
            ))
            # 3-2 раскладка: первые 3 → одна строка, остальные → по 2
            if (i == 2) or (i > 2 and len(row) == 2):
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton(
        text="✏ Своя сумма", callback_data="tp_amt:custom",
    )])
    rows.append([InlineKeyboardButton(text="« К балансу", callback_data="bal")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def topup_invoice_keyboard(
    *,
    amount_kopecks: int,
    pay_url: str,
    invoice_id: int,
    expires_in_minutes: int = 60,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка созданного invoice'а.

    Юзер видит:
      [ 🚀 Оплатить (открыть CryptoBot) ]   (url=pay_url)
      [ 🔄 Проверить статус ]                (callback_data=tp_check:{iid})
      [ ❌ Отменить ]                        (callback_data=tp_cancel:{iid})

    На странице — сумма, инструкция и срок действия. После оплаты бот
    сам пришлёт «✅ Зачислено» (polling каждые 30с), но юзер может
    форсировать проверку кнопкой.
    """
    text = (
        "🧾 <b>Счёт CryptoBot создан</b>\n\n"
        f"💰 Сумма: <b>{_format_rub_full(amount_kopecks)}</b>\n"
        f"⏱ Действителен: <b>{expires_in_minutes} мин</b>\n"
        f"🆔 Invoice: <code>#{invoice_id}</code>\n\n"
        "<b>Как оплатить:</b>\n"
        "1. Нажми «🚀 Оплатить» — откроется CryptoBot\n"
        "2. Выбери криптовалюту и подтверди платёж\n"
        "3. Вернись сюда — баланс зачислится автоматически\n\n"
        "<i>Зачисление обычно за 10-30 секунд. Если что-то пошло "
        "не так — нажми «🔄 Проверить статус».</i>"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🚀 Оплатить", url=pay_url)],
        [InlineKeyboardButton(
            text="🔄 Проверить статус",
            callback_data=f"tp_check:{invoice_id}",
        )],
        [
            InlineKeyboardButton(
                text="« К балансу", callback_data="bal",
            ),
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"tp_cancel:{invoice_id}",
            ),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ─── checkout: confirm screen ──────────────────────────────────────


def checkout_confirm_keyboard(
    *,
    svc,
    user_balance_kopecks: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Экран подтверждения покупки. Юзер уже видел карточку, нажал «💳 Купить»,
    мы показываем финальную страницу с балансом ДО и ПОСЛЕ.

    Текст:
      💳 <b>Подтверди покупку</b>
      🍎 🇹🇷 Apple TR 10 TRY
      💰 Цена: 205,30 ₽
      ─────────────
      💵 Баланс сейчас:    410,00 ₽
      💸 Спишется:        −205,30 ₽
      💚 Останется:        204,70 ₽
      ─────────────
      🚀 Доставка займёт 30–60 секунд после подтверждения. Коды придут
      в этот чат.

    Кнопки:
      [ ✅ Подтвердить ]
      [ ❌ Отмена ]
    """
    price = int(svc.rub_price_kopecks or 0)
    base = getattr(svc, "base_name", None) or svc.category_name or ""
    brand = brand_emoji(base)
    flag = region_flag(svc.category_name or "")
    flag_part = f"{flag} " if flag else ""

    after = max(0, user_balance_kopecks - price)
    text = (
        "💳 <b>Подтверди покупку</b>\n\n"
        f"{brand} {flag_part}<b>{html.escape(svc.service_name)}</b>\n"
        f"💰 Цена: <b>{_format_rub_full(price)}</b>\n"
        "─────────────\n"
        f"💵 Баланс сейчас:    <b>{_format_rub_full(user_balance_kopecks)}</b>\n"
        f"💸 Спишется:        <b>−{_format_rub_full(price)}</b>\n"
        f"💚 Останется:        <b>{_format_rub_full(after)}</b>\n"
        "─────────────\n\n"
        "🚀 <i>Доставка займёт 30–60 секунд после подтверждения. "
        "Коды придут в этот чат.</i>\n\n"
        "🛡 <i>Если NS-провайдер откажет — деньги вернутся на твой "
        "баланс автоматически.</i>"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text=f"✅ Подтвердить · {_format_rub_compact(price)}",
            callback_data=f"buy_ok:{svc.ns_service_id}",
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def checkout_insufficient_balance_keyboard(
    *,
    need_kopecks: int,
    have_kopecks: int,
    deficit_kopecks: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Экран «не хватает средств» — открывается ВМЕСТО confirm-screen'а,
    если у юзера не хватает баланса.

    Дружелюбно сообщаем сколько не хватает и предлагаем быстро пополнить
    (большая кнопка → переход в топ-ап через CryptoBot).
    """
    text = (
        "💸 <b>Недостаточно баланса</b>\n\n"
        f"🛒 Нужно:      <b>{_format_rub_full(need_kopecks)}</b>\n"
        f"💵 Сейчас:     <b>{_format_rub_full(have_kopecks)}</b>\n"
        f"📉 Не хватает: <b>{_format_rub_full(deficit_kopecks)}</b>\n\n"
        "<i>Пополни баланс одним из способов — деньги придут "
        "за 30–60 секунд (CryptoBot работает в реальном времени).</i>"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="🪙 Пополнить через CryptoBot",
            callback_data="topup:crypto",
        )],
        [InlineKeyboardButton(text="💰 К балансу", callback_data="bal")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def checkout_processing_text() -> str:
    """Текст «обработка заказа» — показывается между confirm и delivery."""
    return (
        "⏳ <b>Обрабатываем заказ...</b>\n\n"
        "🔄 Связываемся с поставщиком — обычно занимает 30-60 секунд.\n"
        "<i>Коды придут отдельным сообщением.</i>"
    )


# ─── orders history ────────────────────────────────────────────────


def orders_list_keyboard(
    *,
    orders: Sequence,
    page: int,
    total: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Страница «📦 Мои заказы».

    Item format:
      [ #42 · ✅ 25.05 · Apple US $5 — 397 ₽ ]
      [ #41 · ⏳ 25.05 · Steam $5 — 410 ₽ ]
      [ #40 · ❌ 24.05 · Roblox 100 — 238 ₽ ]

    Статус-emoji:
      ✅ delivered
      ⏳ paid/delivering
      🔄 refunded
      ❌ failed
      📝 draft (не должны быть видны юзеру, но на всякий)
    """
    if not orders:
        text = (
            "📦 <b>Мои заказы</b>\n\n"
            "У тебя пока нет покупок. Загляни в 🛍 Каталог — там есть много "
            "интересных карт, подписок и игровых валют!"
        )
        return text, InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="cats:0")],
        ])

    page_size = 8
    total_pages = max(1, (total + page_size - 1) // page_size)
    text_lines = [
        "📦 <b>Мои заказы</b>",
        f"<i>всего {total} · стр. {page + 1}/{total_pages}</i>",
        "",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for o in orders:
        emoji = _order_status_emoji(o.status)
        date = o.created_at.strftime("%d.%m") if getattr(o, "created_at", None) else "—"
        price = _format_rub_compact(o.total_rub_kopecks)
        # Имя товара ужмём
        name = (o.ns_service_name or "—")[:30]
        label = f"#{o.id} · {emoji} {date} · {name} — {price}"
        rows.append([InlineKeyboardButton(
            text=_truncate(label),
            callback_data=f"ord:{o.id}",
        )])
    nav = _nav_row(page=page, total_pages=total_pages, prefix="orders")
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(text="🛍 Каталог", callback_data="cats:0"),
    ])
    return "\n".join(text_lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _order_status_emoji(status: str) -> str:
    """Эмодзи для статуса заказа в списке."""
    return {
        "delivered": "✅",
        "paid": "⏳",
        "delivering": "⏳",
        "refunded": "🔄",
        "failed": "❌",
        "draft": "📝",
    }.get(status, "❔")


def order_card_keyboard(
    *,
    order,
    pins: list | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка одного заказа из истории. Показывает pins для delivered,
    error для failed, статус-таймлайн.
    """
    emoji = _order_status_emoji(order.status)
    status_human = {
        "delivered": "✅ Доставлен",
        "paid": "⏳ Оплачен, ждём поставщика",
        "delivering": "⏳ Доставляется",
        "refunded": "🔄 Возврат сделан",
        "failed": "❌ Не выполнен",
        "draft": "📝 Черновик",
    }.get(order.status, order.status)

    date = order.created_at.strftime("%d.%m.%Y %H:%M") if getattr(order, "created_at", None) else "—"

    lines = [
        f"🧾 <b>Заказ #{order.id}</b>",
        f"<i>{date}</i>",
        "",
        f"🛒 <b>{html.escape(order.ns_service_name or '—')}</b>",
        f"💰 Цена: <b>{_format_rub_full(order.total_rub_kopecks)}</b>",
        f"📍 Статус: <b>{status_human}</b>",
    ]
    if pins:
        lines.append("")
        lines.append("🔑 <b>Коды активации:</b>")
        for i, p in enumerate(pins, start=1):
            if isinstance(p, dict):
                code = p.get("pin") or p.get("code") or p.get("content") or "?"
                serial = p.get("serial")
                if serial:
                    lines.append(
                        f"  {i}. <code>{code}</code> · serial: <code>{serial}</code>"
                    )
                else:
                    lines.append(f"  {i}. <code>{code}</code>")
            else:
                lines.append(f"  {i}. <code>{p}</code>")
    if order.status == "failed" and getattr(order, "error", None):
        lines.append("")
        lines.append(f"<i>Причина: {html.escape(order.error[:200])}</i>")

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            text="« К заказам", callback_data="orders:0",
        )],
    ]
    # Кнопка «Купить ещё» если delivered/failed → быстрый re-order
    if order.status in ("delivered", "failed", "refunded"):
        rows.insert(0, [InlineKeyboardButton(
            text="🛒 Купить ещё",
            callback_data=f"svc:{order.ns_service_id}",
        )])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def topup_custom_amount_prompt(*, min_rub: int, max_rub: int) -> str:
    """Текст промпта для FSM 'своя сумма'. Отдельная функция для теста."""
    return (
        "✏ <b>Введи сумму пополнения</b>\n\n"
        f"От <b>{min_rub} ₽</b> до <b>{max_rub:,} ₽</b>. "
        "Можно дробное число, например <code>250.50</code>.\n\n"
        "<i>Отмена — /cancel или кнопка ниже.</i>"
    ).replace(",", " ")
