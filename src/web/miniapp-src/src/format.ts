/**
 * Утилиты форматирования. Цена везде хранится в копейках, юзеру показываем
 * в рублях с разделителями тысяч.
 */

export function formatRub(kopecks: number): string {
    const rub = Math.round(kopecks / 100);
    return rub.toLocaleString("ru-RU") + " ₽";
}

export function formatRubExact(kopecks: number): string {
    // С копейками, для точных операций (рефералы).
    const rub = (kopecks / 100).toLocaleString("ru-RU", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    });
    return rub + " ₽";
}

export function formatDate(iso: string | null): string {
    if (!iso) return "—";
    try {
        const d = new Date(iso);
        return d.toLocaleString("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
        });
    } catch {
        return iso;
    }
}

export function statusEmoji(
    status: string,
): { emoji: string; label: string; color: string } {
    switch (status) {
        case "delivered":
            return { emoji: "✅", label: "Выполнен", color: "var(--ok)" };
        case "paid":
        case "delivering":
            return {
                emoji: "⏳",
                label: "Обрабатывается",
                color: "var(--warn)",
            };
        case "failed":
            return { emoji: "❌", label: "Ошибка", color: "var(--err)" };
        case "refunded":
            return {
                emoji: "💸",
                label: "Возврат",
                color: "var(--tg-hint)",
            };
        default:
            return { emoji: "⚪", label: status, color: "var(--tg-hint)" };
    }
}
