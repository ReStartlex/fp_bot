"""
Тесты команды /shop_reply owner-бота: владелец отвечает покупателю в
shop-бот на обращение из «🆘 Поддержка» (двусторонняя поддержка).
"""
from __future__ import annotations

from types import SimpleNamespace

from src.alerts.bot import TelegramBot


class _FakeMsg:
    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


def _bot(sender=None) -> TelegramBot:
    tb = TelegramBot(settings=SimpleNamespace())
    tb.set_shop_reply_sender(sender)
    return tb


async def test_reply_without_sender_warns():
    m = _FakeMsg()
    await _bot(None)._do_shop_reply(m, "555 привет")
    assert any("не запущен" in a for a in m.answers)


async def test_reply_delivers_to_buyer():
    sent: list[tuple[int, str]] = []

    async def sender(tg_id, text):
        sent.append((tg_id, text))

    m = _FakeMsg()
    await _bot(sender)._do_shop_reply(m, "555 Код выслан, проверьте почту")
    assert len(sent) == 1
    assert sent[0][0] == 555
    assert "Код выслан, проверьте почту" in sent[0][1]
    assert "поддержки" in sent[0][1].lower()
    assert any("отправлен" in a for a in m.answers)


async def test_reply_usage_on_missing_text():
    async def sender(i, t):
        pass

    m = _FakeMsg()
    await _bot(sender)._do_shop_reply(m, "555")
    assert any("Использование" in a for a in m.answers)


async def test_reply_rejects_non_numeric_id():
    async def sender(i, t):
        pass

    m = _FakeMsg()
    await _bot(sender)._do_shop_reply(m, "abc привет")
    assert any("числов" in a for a in m.answers)


async def test_reply_escapes_html():
    sent: list[tuple[int, str]] = []

    async def sender(tg_id, text):
        sent.append((tg_id, text))

    m = _FakeMsg()
    await _bot(sender)._do_shop_reply(m, "555 <b>x</b>")
    assert "<b>x</b>" not in sent[0][1]
    assert "&lt;b&gt;" in sent[0][1]
