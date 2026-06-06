"""
Тесты сценария «Поддержка» shop-бота: обращение должно доставляться
владельцу (owner_notify) и подтверждаться пользователю.

Регрессия: раньше «Поддержка» лишь печатала статичный текст «оператор
увидит сообщение», но ничего не пересылала — оператору не приходило
уведомление, пользователю не было подтверждения.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.shop.bot import ShopBot
from src.shop.states import SupportState


class _FakeMessage:
    def __init__(self, text, user):
        self.text = text
        self.caption = None
        self.from_user = user
        self.answers: list[str] = []

    async def answer(self, text, reply_markup=None):
        self.answers.append(text)


class _FakeState:
    def __init__(self):
        self.cleared = False
        self.state = None

    async def clear(self):
        self.cleared = True

    async def set_state(self, s):
        self.state = s


def _bot(owner_notify=None) -> ShopBot:
    return ShopBot(settings=SimpleNamespace(), owner_notify=owner_notify)


def _user(uid=555, username="vasya", first_name="Вася"):
    return SimpleNamespace(id=uid, username=username, first_name=first_name)


async def test_support_cmd_enters_state_and_prompts():
    bot = _bot()
    msg = _FakeMessage(None, _user())
    st = _FakeState()
    await bot._on_support_cmd(msg, st)
    assert st.state == SupportState.waiting_for_message
    assert msg.answers and "Поддержка" in msg.answers[0]


async def test_support_message_forwards_to_owner_and_acks():
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    bot = _bot(notify)
    msg = _FakeMessage("Не приходит код после оплаты", _user())
    st = _FakeState()
    await bot._on_support_message(msg, st)

    assert st.cleared
    assert len(sent) == 1
    assert "Не приходит код после оплаты" in sent[0]
    assert "555" in sent[0]
    assert "@vasya" in sent[0]
    assert any("отправлено оператору" in a for a in msg.answers)


async def test_support_message_escapes_html():
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    bot = _bot(notify)
    msg = _FakeMessage("<script>alert(1)</script>", _user())
    await bot._on_support_message(msg, _FakeState())
    assert "<script>" not in sent[0]
    assert "&lt;script&gt;" in sent[0]


async def test_support_message_without_owner_notify_warns_user():
    bot = _bot(None)
    msg = _FakeMessage("проблема", _user(uid=1, username=None, first_name=None))
    await bot._on_support_message(msg, _FakeState())
    assert any("Не получилось" in a for a in msg.answers)


async def test_support_message_handles_empty_text():
    sent: list[str] = []

    async def notify(text):
        sent.append(text)

    bot = _bot(notify)
    msg = _FakeMessage(None, _user())  # вложение без текста
    await bot._on_support_message(msg, _FakeState())
    assert "вложение без текста" in sent[0]
