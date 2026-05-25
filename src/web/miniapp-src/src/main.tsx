/**
 * Entry-point Telegram Mini App.
 *
 * Telegram inject'ит `window.Telegram.WebApp` через telegram-web-app.js;
 * это даёт нам:
 *   - initData (передаём в API в заголовке X-Telegram-Init-Data)
 *   - themeParams (динамические CSS-переменные, light/dark)
 *   - viewport-events (раскрытие на весь экран)
 *   - HapticFeedback, MainButton, BackButton — будем юзать постепенно.
 *
 * Минимально-необходимый bootstrap:
 *   1. tg.ready() — сообщаем Telegram что фронт загрузился (скрывает spinner)
 *   2. tg.expand() — раскрываем во всю высоту
 *   3. Применяем themeParams → CSS-переменные
 *   4. Рендерим <App/>
 */
import { render } from "preact";
import { App } from "./App";
import { applyTelegramTheme } from "./theme";
import "./styles.css";

declare global {
    interface Window {
        Telegram?: {
            WebApp: TelegramWebApp;
        };
    }
}

export interface TelegramWebApp {
    initData: string;
    initDataUnsafe: Record<string, unknown>;
    colorScheme: "light" | "dark";
    themeParams: Record<string, string>;
    isExpanded: boolean;
    viewportHeight: number;
    ready(): void;
    expand(): void;
    close(): void;
    onEvent(eventType: string, handler: () => void): void;
    showAlert(message: string, callback?: () => void): void;
    showConfirm(
        message: string,
        callback: (confirmed: boolean) => void,
    ): void;
    HapticFeedback?: {
        impactOccurred(style: "light" | "medium" | "heavy"): void;
        notificationOccurred(type: "error" | "success" | "warning"): void;
        selectionChanged(): void;
    };
    BackButton?: {
        show(): void;
        hide(): void;
        onClick(handler: () => void): void;
        offClick(handler: () => void): void;
    };
}

const tg = window.Telegram?.WebApp;
if (tg) {
    tg.ready();
    try {
        tg.expand();
    } catch {
        // Игнорируем — старый Telegram клиент может не поддерживать.
    }
    applyTelegramTheme(tg);
    tg.onEvent("themeChanged", () => applyTelegramTheme(tg));
}

const root = document.getElementById("app")!;
render(<App />, root);
