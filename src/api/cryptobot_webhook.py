"""
Webhook endpoint для CryptoBot Crypto Pay.

URL: POST /api/cryptobot/webhook
Header: crypto-pay-api-signature: <hex>

При получении события 'invoice_paid' идемпотентно начисляет баланс
(через apply_paid_invoice). Подпись HMAC-SHA256 обязательна, иначе 401.

Опционально (по умолчанию ВЫКЛЮЧЕНО) — polling-воркер уже даёт надёжное
зачисление каждые 30с. Включить можно через nginx-reverse-proxy с SSL:

    location /api/cryptobot/webhook {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
    }

И затем в @CryptoBot → /pay → выбрать app → «Webhook» → указать
https://<domain>/api/cryptobot/webhook.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from src.config import get_settings
from src.db.session import session_factory
from src.shop.payments.cryptobot import verify_webhook_signature
from src.shop.repo import apply_paid_invoice, mark_payment_failed

router = APIRouter(prefix="/api/cryptobot", tags=["cryptobot"])


@router.post("/webhook")
async def cryptobot_webhook(request: Request) -> dict:
    settings = get_settings()
    token = settings.cryptobot_api_token
    if not token:
        # Если токен не настроен — webhook отключён.
        raise HTTPException(status_code=503, detail="cryptobot not configured")

    raw_body = await request.body()
    signature = request.headers.get("crypto-pay-api-signature") or ""
    if not verify_webhook_signature(
        api_token=token.get_secret_value(),
        raw_body=raw_body,
        signature_hex=signature,
    ):
        # 401, но логируем как warning — это либо подделка, либо ошибка
        # настройки на стороне CryptoBot. Полезно знать.
        logger.warning(
            "cryptobot webhook: signature mismatch from "
            f"{request.client.host if request.client else '?'}"
        )
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        logger.warning(f"cryptobot webhook: invalid JSON: {exc}")
        raise HTTPException(status_code=400, detail="invalid json")

    update_type = payload.get("update_type")
    invoice = payload.get("payload") or {}

    if update_type == "invoice_paid":
        invoice_id = str(invoice.get("invoice_id") or "")
        amount = invoice.get("amount") or "0"
        if not invoice_id:
            raise HTTPException(status_code=400, detail="missing invoice_id")
        # amount у CryptoBot — Decimal-строка в RUB
        from decimal import Decimal
        try:
            kopecks = int((Decimal(str(amount)) * Decimal(100)).to_integral_value())
        except (ValueError, ArithmeticError):
            raise HTTPException(status_code=400, detail="invalid amount")
        async with session_factory()() as session:
            payment, applied = await apply_paid_invoice(
                session,
                provider="cryptobot",
                provider_invoice_id=invoice_id,
                paid_amount_kopecks=kopecks,
                raw_payload_json=json.dumps(invoice, ensure_ascii=False),
            )
            await session.commit()
        logger.info(
            f"cryptobot webhook: invoice_paid id={invoice_id} "
            f"amount={kopecks}kop applied={applied}"
        )
        # CryptoBot не интересует наш JSON; 200 — достаточно.
        return {"ok": True, "applied": bool(applied)}

    if update_type == "invoice_expired":
        invoice_id = str(invoice.get("invoice_id") or "")
        if invoice_id:
            async with session_factory()() as session:
                await mark_payment_failed(
                    session,
                    provider="cryptobot",
                    provider_invoice_id=invoice_id,
                    reason="expired",
                )
                await session.commit()
        return {"ok": True}

    logger.info(f"cryptobot webhook: ignored update_type={update_type}")
    return {"ok": True, "ignored": True}
