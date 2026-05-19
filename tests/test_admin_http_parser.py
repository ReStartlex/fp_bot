"""
Тесты парсера сообщений FunPay HTML.

Зачем: тема FunPay меняется, и без unit-тестов парсера мы ломаемся
тихо — без логов, просто сообщения пропадают. Здесь фиксируем
несколько реальных вариантов HTML и проверяем, что message_id, текст
и автор извлекаются.
"""
from __future__ import annotations

from src.funpay.admin_http import FunPayAdminClient


def _admin() -> FunPayAdminClient:
    return FunPayAdminClient(golden_key="x", phpsessid=None)


def test_extract_classic_chat_msg_item(monkeypatch):
    """
    HTML образца «как было всегда»:
        <div class="chat-msg-item" data-id="123" data-author="456">
          <a class="chat-msg-author-link">buyer1</a>
          <div class="chat-msg-text">тестовое сообщение</div>
        </div>
    """
    from bs4 import BeautifulSoup
    html = """
    <div class="chat-msg-item" data-id="123" data-author="456">
      <a class="chat-msg-author-link">buyer1</a>
      <div class="chat-msg-text">тестовое сообщение</div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".chat-msg-item")
    assert FunPayAdminClient._extract_message_id(el) == 123
    assert FunPayAdminClient._extract_author_id(el) == 456
    assert FunPayAdminClient._extract_author_username(el) == "buyer1"
    assert FunPayAdminClient._extract_message_text(el) == "тестовое сообщение"


def test_extract_modern_message_item():
    """
    HTML современный вариант:
        <div class="message" id="msg-7890" data-author-id="222">
          <span class="chat-msg-username">buyer2</span>
          <div class="message-body">!помощь</div>
        </div>
    """
    from bs4 import BeautifulSoup
    html = """
    <div class="message" id="msg-7890" data-author-id="222">
      <span class="chat-msg-username">buyer2</span>
      <div class="message-body">!помощь</div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".message")
    assert FunPayAdminClient._extract_message_id(el) == 7890
    assert FunPayAdminClient._extract_author_id(el) == 222
    assert FunPayAdminClient._extract_author_username(el) == "buyer2"
    assert FunPayAdminClient._extract_message_text(el) == "!помощь"


def test_extract_fallback_no_body_selector():
    """
    Если у нас нет ни одного из «body» селекторов, fallback должен
    отрезать author-link и timestamp и оставить только текст.
    """
    from bs4 import BeautifulSoup
    html = """
    <div class="chat-msg-item" data-id="555">
      <a class="chat-msg-author-link">buyer3</a>
      <span class="chat-msg-time">13:33</span>
      Привет, нужна помощь
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".chat-msg-item")
    text = FunPayAdminClient._extract_message_text(el)
    assert "Привет, нужна помощь" in text
    assert "buyer3" not in text
    assert "13:33" not in text


def test_extract_message_id_from_id_attribute_only():
    """data-id отсутствует, но есть id='message-99999'."""
    from bs4 import BeautifulSoup
    html = '<div class="chat-msg-item" id="message-99999"></div>'
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".chat-msg-item")
    assert FunPayAdminClient._extract_message_id(el) == 99999


def test_extract_author_id_from_nested_link():
    """Автор вложен в <a data-user-id='42'>"""
    from bs4 import BeautifulSoup
    html = """
    <div class="chat-msg-item">
      <a data-user-id="42" class="chat-msg-author-link">buyer4</a>
      <div class="chat-msg-text">привет</div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".chat-msg-item")
    assert FunPayAdminClient._extract_author_id(el) == 42
