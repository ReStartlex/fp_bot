/**
 * Root-компонент Mini App.
 *
 * Архитектура: tab-bar внизу, 4 экрана (Каталог / Заказы / Профиль).
 * Простая router-машинка через signals — без react-router, чтобы не тащить
 * 30kb ради 4 вкладок.
 *
 * При старте: api.init() → если 401, показываем error screen с подсказкой
 * «открой бот → Menu → запусти заново». При успехе кладём Me в global signal.
 */
import { useEffect } from "preact/hooks";
import { signal } from "@preact/signals";
import { api, ApiError } from "./api";
import type { Me } from "./api";
import { CatalogScreen } from "./screens/CatalogScreen";
import { OrdersScreen } from "./screens/OrdersScreen";
import { ProfileScreen } from "./screens/ProfileScreen";

type Tab = "catalog" | "orders" | "profile";

export const activeTab = signal<Tab>("catalog");
export const me = signal<Me | null>(null);
export const authError = signal<string | null>(null);

async function refreshMe(): Promise<void> {
    try {
        const data = await api.me();
        me.value = data;
    } catch (e) {
        if (e instanceof ApiError) {
            authError.value = e.message;
        } else {
            authError.value = String(e);
        }
    }
}

export function App() {
    useEffect(() => {
        (async () => {
            try {
                const data = await api.init();
                me.value = data;
                authError.value = null;
            } catch (e) {
                authError.value =
                    e instanceof ApiError ? e.message : String(e);
            }
        })();
    }, []);

    // Reload me on tab switch (балланс, заказы могли поменяться).
    useEffect(() => {
        refreshMe();
    }, [activeTab.value]);

    if (authError.value) {
        return (
            <div class="screen">
                <div class="error-box">
                    <strong>Ошибка авторизации</strong>
                    <p style={{ margin: "8px 0 0" }}>{authError.value}</p>
                </div>
                <p style={{ color: "var(--tg-hint)" }}>
                    Откройте Mini App из бота заново — кнопка слева от поля
                    ввода («🛍 Открыть магазин»).
                </p>
            </div>
        );
    }

    if (!me.value) {
        return <div class="loading">Загрузка…</div>;
    }

    return (
        <>
            {activeTab.value === "catalog" && <CatalogScreen />}
            {activeTab.value === "orders" && <OrdersScreen />}
            {activeTab.value === "profile" && <ProfileScreen />}
            <nav class="tabs">
                <button
                    class="tab"
                    data-active={activeTab.value === "catalog"}
                    onClick={() => (activeTab.value = "catalog")}
                >
                    <span class="tab-icon">🛍</span>
                    Каталог
                </button>
                <button
                    class="tab"
                    data-active={activeTab.value === "orders"}
                    onClick={() => (activeTab.value = "orders")}
                >
                    <span class="tab-icon">📦</span>
                    Заказы
                </button>
                <button
                    class="tab"
                    data-active={activeTab.value === "profile"}
                    onClick={() => (activeTab.value = "profile")}
                >
                    <span class="tab-icon">👤</span>
                    Профиль
                </button>
            </nav>
        </>
    );
}
