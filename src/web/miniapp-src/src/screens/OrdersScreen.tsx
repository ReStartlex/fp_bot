/**
 * История заказов + детальная карточка с pin'ами.
 *
 * Поллинг: если открыт «Обрабатывается»-заказ — каждые 5 секунд дёргаем
 * /orders/{id}, чтобы pin'ы появились сами, без F5.
 */
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import type { Order } from "../api";
import { formatRub, formatDate, statusEmoji } from "../format";

export function OrdersScreen() {
    const [view, setView] = useState<
        { kind: "list" } | { kind: "card"; order: Order }
    >({ kind: "list" });

    if (view.kind === "list") {
        return <List onPick={(o) => setView({ kind: "card", order: o })} />;
    }
    return (
        <Card
            order={view.order}
            onBack={() => setView({ kind: "list" })}
            onUpdate={(o) => setView({ kind: "card", order: o })}
        />
    );
}

function List(props: { onPick: (o: Order) => void }) {
    const [orders, setOrders] = useState<Order[] | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [page, setPage] = useState(0);
    const [hasMore, setHasMore] = useState(false);

    useEffect(() => {
        api.orders(page).then(
            (r) => {
                setOrders(r.orders);
                setHasMore((page + 1) * r.page_size < r.total);
            },
            (e: unknown) => setError(e instanceof ApiError ? e.message : String(e)),
        );
    }, [page]);

    if (error) return <div class="screen"><div class="error-box">{error}</div></div>;
    if (!orders) return <div class="loading">Загрузка заказов…</div>;
    if (orders.length === 0) {
        return (
            <div class="screen">
                <div class="screen-header"><h1>📦 Мои заказы</h1></div>
                <div class="empty">
                    Пока заказов нет.
                    <br />
                    Загляните в каталог 🛍
                </div>
            </div>
        );
    }
    return (
        <div class="screen">
            <div class="screen-header"><h1>📦 Мои заказы</h1></div>
            {orders.map((o) => {
                const st = statusEmoji(o.status);
                return (
                    <button class="order-card" onClick={() => props.onPick(o)}>
                        <div class="top">
                            <div class="name">{o.ns_service_name}</div>
                            <div class="status" style={{ color: st.color }}>
                                {st.emoji} {st.label}
                            </div>
                        </div>
                        <div class="meta">
                            <span>#{o.id} · {formatDate(o.created_at)}</span>
                            <span>{formatRub(o.total_rub_kopecks)}</span>
                        </div>
                    </button>
                );
            })}
            {(page > 0 || hasMore) && (
                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                    {page > 0 && (
                        <button class="btn btn-secondary" onClick={() => setPage(page - 1)}>
                            ← Предыдущая
                        </button>
                    )}
                    {hasMore && (
                        <button class="btn btn-secondary" onClick={() => setPage(page + 1)}>
                            Следующая →
                        </button>
                    )}
                </div>
            )}
        </div>
    );
}

function Card(props: {
    order: Order;
    onBack: () => void;
    onUpdate: (o: Order) => void;
}) {
    const [order, setOrder] = useState(props.order);

    // Poll if order in flight
    useEffect(() => {
        if (order.status !== "paid" && order.status !== "delivering") return;
        const id = setInterval(async () => {
            try {
                const fresh = await api.order(order.id);
                setOrder(fresh);
                props.onUpdate(fresh);
            } catch {
                // Ignore — оставим прежний state.
            }
        }, 5000);
        return () => clearInterval(id);
    }, [order.id, order.status]);

    const st = statusEmoji(order.status);

    async function copyPin(value: string) {
        try {
            await navigator.clipboard.writeText(value);
            window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred("success");
            window.Telegram?.WebApp?.showAlert?.("Скопировано");
        } catch {
            window.Telegram?.WebApp?.showAlert?.(value);
        }
    }

    return (
        <div class="screen">
            <div class="screen-header">
                <button class="btn-secondary btn" style={{ width: "auto", padding: "8px 14px" }} onClick={props.onBack}>
                    ← Заказы
                </button>
                <h1>Заказ #{order.id}</h1>
            </div>

            <div class="card">
                <div class="service-name">{order.ns_service_name}</div>
                <div style={{ marginTop: 6, fontSize: 18, fontWeight: 600, color: st.color }}>
                    {st.emoji} {st.label}
                </div>
            </div>

            <div class="card">
                <div class="row">
                    <div class="row-title">Сумма</div>
                    <div class="row-aside">{formatRub(order.total_rub_kopecks)}</div>
                </div>
                <div class="row">
                    <div class="row-title">Создан</div>
                    <div class="row-aside">{formatDate(order.created_at)}</div>
                </div>
                {order.delivered_at && (
                    <div class="row">
                        <div class="row-title">Выдан</div>
                        <div class="row-aside">{formatDate(order.delivered_at)}</div>
                    </div>
                )}
            </div>

            {(order.status === "paid" || order.status === "delivering") && (
                <div class="card">
                    <div class="card-title">Ожидание выдачи</div>
                    <p style={{ margin: "8px 0", color: "var(--tg-hint)" }}>
                        Обычно занимает 5–60 секунд. Страница обновляется
                        автоматически.
                    </p>
                </div>
            )}

            {order.pins && order.pins.length > 0 && (
                <div class="card">
                    <div class="card-title">Ваш код</div>
                    {order.pins.map((p, i) => {
                        const pin =
                            (p as Record<string, string>).pin ??
                            (p as Record<string, string>).code ??
                            Object.values(p).join(" / ");
                        return (
                            <div
                                key={i}
                                class="pin-row"
                                onClick={() => copyPin(pin)}
                            >
                                {pin}
                            </div>
                        );
                    })}
                    <div class="pin-copy-hint">Тапни чтобы скопировать</div>
                </div>
            )}

            {order.error && (
                <div class="error-box">
                    <strong>Ошибка выдачи</strong>
                    <div style={{ marginTop: 6, fontSize: 13 }}>{order.error}</div>
                </div>
            )}
        </div>
    );
}
