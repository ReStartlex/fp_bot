# NeuroDrop Mini App

Telegram Mini App (Preact + Vite + TypeScript) для FunPay ↔ NS shop-бота.

## Архитектура

- **Frontend**: Preact 10 + signals + Vite + TypeScript. Bundle ~36KB (13KB gzip).
- **Backend**: FastAPI `src/api/shop_router.py` (Sprint 6). Auth — Telegram
  WebApp `initData` через `X-Telegram-Init-Data` header.
- **Bot integration**: `src/shop/bot.py::_set_webapp_menu_button` регистрирует
  Mini App как Menu Button. URL берётся из `SHOP_WEBAPP_URL`.

## Структура

```
src/web/miniapp-src/     ← исходники (этот каталог)
src/web/miniapp/         ← build artifacts; FastAPI отдаёт под /app/*
```

## Локальная разработка

```powershell
cd src/web/miniapp-src
npm install
npm run dev              # Vite dev server, http://localhost:5173
                         # /api/* проксируется на :8000 (FastAPI)
```

Запускать Mini App вне Telegram нельзя — `initData` будет пустым.
Для разработки используй [Telegram BotFather → setupminiapp](https://core.telegram.org/bots/webapps#botfather)
с `https://localhost:5173` (через ngrok / cloudflare tunnel).

## Production build

```powershell
npm run build            # → ../miniapp/index.html + assets/
```

Build artifacts коммитятся в репу, чтобы VPS-deploy не требовал Node.js.
После деплоя FastAPI автоматически отдаёт их под `/app/*` (см. `src/api/app.py`).

## Деплой

1. В `.env` на VPS установить:
   ```
   SHOP_WEBAPP_URL=https://your-domain.ru/app/
   ```
2. Перезапустить shop-бот — он зарегистрирует Menu Button.
3. nginx / cloudflare должен проксировать `/api/shop/*` на FastAPI.

## Безопасность

- `initData` валидируется на бэке HMAC-SHA256 от `bot_token`. См.
  `src/api/webapp_auth.py` + 16 unit-тестов в `tests/test_webapp_auth.py`.
- `auth_date` устаревает за 12 часов (anti-replay).
- Чужие заказы возвращают 404 (не 403, чтобы не раскрывать существование).
