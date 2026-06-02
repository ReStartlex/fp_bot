/**
 * Применяет themeParams от Telegram WebApp к CSS-переменным.
 *
 * Telegram присылает hex-цвета юзерской темы:
 *   bg_color, text_color, hint_color, link_color, button_color,
 *   button_text_color, secondary_bg_color, header_bg_color, accent_text_color,
 *   section_bg_color, section_header_text_color, subtitle_text_color,
 *   destructive_text_color.
 *
 * Маппим их в --tg-* CSS-переменные, чтобы дизайн автоматически адаптировался
 * под user's dark/light theme.
 */
import type { TelegramWebApp } from "./main";

export function applyTelegramTheme(tg: TelegramWebApp): void {
    const root = document.documentElement;
    const params = tg.themeParams ?? {};
    const map: Record<string, string> = {
        bg_color: "--tg-bg",
        text_color: "--tg-text",
        hint_color: "--tg-hint",
        link_color: "--tg-link",
        button_color: "--tg-button",
        button_text_color: "--tg-button-text",
        secondary_bg_color: "--tg-secondary-bg",
        section_bg_color: "--tg-section-bg",
        section_header_text_color: "--tg-section-header-text",
        accent_text_color: "--tg-accent",
        destructive_text_color: "--tg-destructive",
    };
    for (const [src, dst] of Object.entries(map)) {
        const value = params[src];
        if (value) root.style.setProperty(dst, value);
    }
    root.dataset.themeScheme = tg.colorScheme;
}
