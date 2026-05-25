"""
Payment-providers для shop-бота NeuroDrop.

Каждый провайдер — отдельный модуль с собственным клиентом, моделями и
helpers'ами. Подключение конкретного провайдера к бизнес-логике — через
src/shop/repo.py (создание ShopPayment) и polling/webhook worker.

Принципы:
  - Все провайдеры идемпотентны по `provider_invoice_id`: повторное
    «оплачено»-событие = no-op.
  - Сумма в БД хранится в копейках (Integer), чтобы не было float-погрешности.
  - Любая мутация баланса юзера — через apply_balance_change (ledger-invariant).
"""
