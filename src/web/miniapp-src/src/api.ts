/**
 * Тонкий API-клиент для Mini App.
 *
 * Все запросы автоматически:
 *   - идут на /api/shop/* (относительный URL, чтобы работало и под /app/
 *     и через CORS dev-proxy);
 *   - несут X-Telegram-Init-Data из window.Telegram.WebApp.initData;
 *   - бросают ApiError(401/403/404/500) при не-2xx ответе.
 *
 * Все DTO дублируются от Python Pydantic — поля snake_case, чтобы не возиться
 * с case-конвертацией. UI-форматирование делаем в `format.ts`.
 */

const API_BASE = "/api/shop";

export class ApiError extends Error {
    constructor(public status: number, message: string) {
        super(message);
    }
}

function getInitData(): string {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData) {
        // Случай разработки локально вне Telegram. Кидаем понятную ошибку,
        // вместо silent failure'а с 401.
        throw new ApiError(
            0,
            "Mini App запущен вне Telegram (initData недоступен)",
        );
    }
    return initData;
}

async function call<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
): Promise<T> {
    const initData = getInitData();
    const headers: Record<string, string> = {
        "X-Telegram-Init-Data": initData,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const resp = await fetch(API_BASE + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
        let detail = `${resp.status} ${resp.statusText}`;
        try {
            const j = await resp.json();
            if (j?.detail) detail = j.detail;
        } catch {
            // Не JSON — оставляем плейн текст.
        }
        throw new ApiError(resp.status, detail);
    }
    return resp.json() as Promise<T>;
}

// ─── DTO ────────────────────────────────────────────────────────

export interface Me {
    user_id: number;
    telegram_user_id: number;
    username: string | null;
    first_name: string | null;
    balance_kopecks: number;
    total_earned_kopecks: number;
    total_spent_kopecks: number;
    operations_count: number;
    invited_count: number;
    active_referrals_count: number;
    earned_via_referrals_kopecks: number;
    referral_percent: number;
}

export interface Group {
    group_slug: string;
    base_name: string;
    variants_count: number;
    cheapest_price_kopecks: number;
}

export interface Category {
    category_id: number;
    category_name: string;
    cheapest_price_kopecks: number;
}

export interface Service {
    ns_service_id: number;
    category_id: number | null;
    category_name: string | null;
    service_name: string;
    base_name: string | null;
    group_slug: string | null;
    rub_price_kopecks: number;
    in_stock: number;
}

export type CheckoutOutcome =
    | "ok"
    | "insufficient_balance"
    | "out_of_stock"
    | "service_not_found"
    | "service_disabled"
    | "user_blocked"
    | "requires_fields";

export interface CheckoutResponse {
    outcome: CheckoutOutcome;
    order_id?: number | null;
    new_balance_kopecks?: number | null;
    need_kopecks?: number | null;
    have_kopecks?: number | null;
    deficit_kopecks?: number | null;
}

export interface Order {
    id: number;
    ns_service_id: number;
    ns_service_name: string;
    total_rub_kopecks: number;
    status:
        | "draft"
        | "paid"
        | "delivering"
        | "delivered"
        | "failed"
        | "refunded";
    created_at: string;
    delivered_at: string | null;
    pins: Array<Record<string, string>> | null;
    error: string | null;
}

export interface OrderListResponse {
    orders: Order[];
    total: number;
    page: number;
    page_size: number;
}

// ─── endpoints ──────────────────────────────────────────────────

export const api = {
    init: () => call<Me>("POST", "/init"),
    me: () => call<Me>("GET", "/me"),
    groups: () => call<Group[]>("GET", "/catalog/groups"),
    groupVariants: (slug: string) =>
        call<Category[]>("GET", `/catalog/groups/${encodeURIComponent(slug)}`),
    categoryServices: (categoryId: number) =>
        call<Service[]>("GET", `/catalog/categories/${categoryId}`),
    service: (nsServiceId: number) =>
        call<Service>("GET", `/catalog/services/${nsServiceId}`),
    checkout: (nsServiceId: number) =>
        call<CheckoutResponse>("POST", "/checkout", {
            ns_service_id: nsServiceId,
        }),
    orders: (page = 0, pageSize = 20) =>
        call<OrderListResponse>(
            "GET",
            `/orders?page=${page}&page_size=${pageSize}`,
        ),
    order: (orderId: number) => call<Order>("GET", `/orders/${orderId}`),
};
