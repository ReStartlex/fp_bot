"""
CLI smoke-test для отладки watcher'а в production.

Запускается ОДИН цикл poll'а и выводит подробную диагностику:
- список чатов и preview;
- курсоры из БД;
- какие сообщения пошли бы в dispatch;
- ошибки HTTP/парсинга/БД, если они есть.

Использование:
    .venv/bin/python -m src.tools.watcher_smoke [--chat CHAT_ID]

Если указан --chat, выводит только этот один чат с полной историей
сообщений и подробностями парсинга (полезно когда «бот не отвечает»).

Этот инструмент НЕ модифицирует БД — все cursor-операции read-only.
Реальный watcher после этого работает как обычно.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from src.config import get_settings
from src.db.repo import get_chat_cursor, list_chat_cursors
from src.db.session import init_db, session_factory
from src.funpay.client import FunPayClient
from src.logging_setup import setup_logging


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    setup_logging(settings)
    await init_db()

    fp = FunPayClient(settings)
    await fp.connect()
    print()
    print("=" * 70)
    print(f"  WHOAMI: id={fp.my_user_id}  username={fp.my_username}")
    print("=" * 70)

    admin = fp._admin

    # ── 1) snapshot всех чатов ──
    print()
    print("── 1) Snapshot /chat/ ──")
    try:
        items = await admin.get_chats_snapshot()
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"Всего чатов в snapshot: {len(items)}")
    if args.chat:
        items = [i for i in items if i["chat_id"] == args.chat]
        print(f"Фильтр --chat {args.chat}: осталось {len(items)} чатов")
    for it in items[:30]:
        unread_mark = " 🔴UNREAD" if it.get("unread") else ""
        print(
            f"  chat_id={it['chat_id']:>10}  "
            f"user={it.get('username','?'):<20}  "
            f"preview={(it.get('preview') or '')[:60]!r}{unread_mark}"
        )
    if len(items) > 30 and not args.chat:
        print(f"  ... и ещё {len(items) - 30} чатов")

    # ── 2) Курсоры из БД ──
    print()
    print("── 2) Курсоры в БД ──")
    async with session_factory()() as session:
        cursors = await list_chat_cursors(session)
    cursor_by_chat = {c.chat_id: c for c in cursors}
    print(f"Всего курсоров в БД: {len(cursors)}")
    if args.chat:
        c = cursor_by_chat.get(args.chat)
        if c is None:
            print(f"  chat={args.chat}: КУРСОРА НЕТ (будет первый pickup)")
        else:
            print(
                f"  chat={args.chat}: last_message_id={c.last_message_id}, "
                f"updated_at={c.updated_at}"
            )

    # ── 3) Подробная разборка одного чата ──
    if args.chat:
        print()
        print(f"── 3) Сообщения в чате {args.chat} ──")
        try:
            messages = await admin.get_chat_messages(args.chat, last_id=None)
        except Exception as exc:
            print(f"FAIL: {type(exc).__name__}: {exc}")
            return 1
        print(f"Всего сообщений в выборке: {len(messages)}")
        for m in messages[-20:]:
            mine = (
                "🤖 (это я)"
                if m.get("author_username") == fp.my_username
                else "👤"
            )
            print(
                f"  id={m.get('message_id'):<10} "
                f"author_id={m.get('author_id'):<10} "
                f"user={m.get('author_username','?'):<20} "
                f"{mine}  "
                f"text={(m.get('text') or '')[:80]!r}"
            )

        # Что бы сделал watcher (без реального dispatch)
        from src.funpay.watcher import FunPayWatcher
        w = FunPayWatcher(fp)
        cursor_last_id = await w._load_cursor(args.chat)
        new_msgs, new_last_id = w._select_new_messages(
            messages=messages, cursor_last_id=cursor_last_id, is_first_run=False
        )
        print()
        print(f"── 4) ЧТО БЫ СДЕЛАЛ WATCHER ──")
        print(f"  cursor_last_id (из БД) = {cursor_last_id}")
        print(f"  Сообщений для dispatch: {len(new_msgs)}")
        for m in new_msgs:
            print(
                f"    → id={m.get('message_id')}  "
                f"@{m.get('author_username')}  "
                f"text={(m.get('text') or '')[:80]!r}"
            )
        print(f"  Новый last_id для курсора: {new_last_id}")

    print()
    print("=" * 70)
    print("  smoke-test завершён. Сервис funpay-ns-bot НЕ остановлен,")
    print("  курсоры в БД НЕ изменены.")
    print("=" * 70)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--chat", type=int, default=None,
        help="Конкретный chat_id для подробной разборки",
    )
    return ap


if __name__ == "__main__":
    args = _build_parser().parse_args()
    sys.exit(asyncio.run(main(args)))
