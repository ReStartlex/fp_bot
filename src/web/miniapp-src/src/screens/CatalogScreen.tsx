/**
 * Каталог: 3-уровневая навигация
 *   групп → вариантов группы → сервисов в категории → карточка с buy
 *
 * Стек экранов держим локально через useState, чтобы при выходе из tab'а
 * возвращаться в корень (как iOS Tab Bar).
 */
import { useEffect, useState } from "preact/hooks";
import { api, ApiError } from "../api";
import type { Group, Category, Service, CheckoutResponse } from "../api";
import { formatRub } from "../format";
import { me } from "../App";

type View =
    | { kind: "groups" }
    | { kind: "variants"; group: Group }
    | { kind: "services"; category: Category; group?: Group }
    | { kind: "service"; service: Service };

function brandEmoji(name: string | null | undefined): string {
    const n = (name || "").toLowerCase();
    if (n.includes("apple")) return "🍎";
    if (n.includes("google")) return "🟢";
    if (n.includes("steam")) return "🎮";
    if (n.includes("playstation") || n.includes("psn")) return "🎮";
    if (n.includes("xbox")) return "🎮";
    if (n.includes("netflix")) return "🎬";
    if (n.includes("spotify")) return "🎵";
    if (n.includes("amazon")) return "📦";
    if (n.includes("openai") || n.includes("chatgpt")) return "🤖";
    if (n.includes("telegram") || n.includes("stars")) return "⭐";
    return "🎁";
}

export function CatalogScreen() {
    const [view, setView] = useState<View>({ kind: "groups" });

    if (view.kind === "groups") {
        return <GroupsView onPick={(g) => setView({ kind: "variants", group: g })} />;
    }
    if (view.kind === "variants") {
        return (
            <VariantsView
                group={view.group}
                onPick={(c) =>
                    setView({ kind: "services", category: c, group: view.group })
                }
                onBack={() => setView({ kind: "groups" })}
            />
        );
    }
    if (view.kind === "services") {
        return (
            <ServicesView
                category={view.category}
                onPick={(s) => setView({ kind: "service", service: s })}
                onBack={() =>
                    view.group
                        ? setView({ kind: "variants", group: view.group })
                        : setView({ kind: "groups" })
                }
            />
        );
    }
    return (
        <ServiceView
            service={view.service}
            onBack={() => setView({ kind: "groups" })}
        />
    );
}

// ─── Groups ─────────────────────────────────────────────────────

function GroupsView(props: { onPick: (g: Group) => void }) {
    const [groups, setGroups] = useState<Group[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        api.groups().then(
            (g) => setGroups(g),
            (e: unknown) =>
                setError(e instanceof ApiError ? e.message : String(e)),
        );
    }, []);

    if (error) return <div class="screen"><div class="error-box">{error}</div></div>;
    if (!groups) return <div class="loading">Загрузка каталога…</div>;
    if (groups.length === 0) {
        return (
            <div class="screen">
                <div class="empty">
                    Каталог пуст. Загляните позже —
                    <br />
                    товары обновляются автоматически.
                </div>
            </div>
        );
    }
    return (
        <div class="screen">
            <div class="screen-header">
                <h1>🛍 Каталог</h1>
            </div>
            <div class="group-grid">
                {groups.map((g) => (
                    <button
                        class="group-card"
                        onClick={() => {
                            window.Telegram?.WebApp?.HapticFeedback?.selectionChanged();
                            props.onPick(g);
                        }}
                    >
                        <div class="emoji">{brandEmoji(g.base_name)}</div>
                        <div class="name">{g.base_name}</div>
                        <div class="price">
                            от {formatRub(g.cheapest_price_kopecks)}
                        </div>
                        <div class="meta">{g.variants_count} вариант(ов)</div>
                    </button>
                ))}
            </div>
        </div>
    );
}

// ─── Variants of a group ───────────────────────────────────────

function VariantsView(props: {
    group: Group;
    onPick: (c: Category) => void;
    onBack: () => void;
}) {
    const [variants, setVariants] = useState<Category[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        api.groupVariants(props.group.group_slug).then(
            setVariants,
            (e: unknown) =>
                setError(e instanceof ApiError ? e.message : String(e)),
        );
    }, [props.group.group_slug]);

    return (
        <div class="screen">
            <div class="screen-header">
                <button class="btn-secondary btn" style={{ width: "auto", padding: "8px 14px" }} onClick={props.onBack}>
                    ← Назад
                </button>
                <h1>{brandEmoji(props.group.base_name)} {props.group.base_name}</h1>
            </div>
            {error && <div class="error-box">{error}</div>}
            {!variants && <div class="loading">Загрузка…</div>}
            {variants && (
                <div class="card">
                    {variants.map((v) => (
                        <button
                            class="row"
                            style={{
                                width: "100%",
                                background: "transparent",
                                border: "none",
                                color: "var(--tg-text)",
                                textAlign: "left",
                                cursor: "pointer",
                            }}
                            onClick={() => props.onPick(v)}
                        >
                            <div>
                                <div class="row-title">{v.category_name}</div>
                            </div>
                            <div class="row-aside">
                                от {formatRub(v.cheapest_price_kopecks)} →
                            </div>
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}

// ─── Services in a category ────────────────────────────────────

function ServicesView(props: {
    category: Category;
    onPick: (s: Service) => void;
    onBack: () => void;
}) {
    const [services, setServices] = useState<Service[] | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        api.categoryServices(props.category.category_id).then(
            setServices,
            (e: unknown) =>
                setError(e instanceof ApiError ? e.message : String(e)),
        );
    }, [props.category.category_id]);

    return (
        <div class="screen">
            <div class="screen-header">
                <button class="btn-secondary btn" style={{ width: "auto", padding: "8px 14px" }} onClick={props.onBack}>
                    ← Назад
                </button>
                <h1>{props.category.category_name}</h1>
            </div>
            {error && <div class="error-box">{error}</div>}
            {!services && <div class="loading">Загрузка…</div>}
            {services && services.length === 0 && (
                <div class="empty">В этой категории пусто.</div>
            )}
            {services && services.map((s) => (
                <button
                    class="order-card"
                    onClick={() => props.onPick(s)}
                >
                    <div class="top">
                        <div class="name">{s.service_name}</div>
                        <div class="status" style={{ color: "var(--tg-accent)" }}>
                            {formatRub(s.rub_price_kopecks)}
                        </div>
                    </div>
                    <div class="meta">
                        <span>В наличии: {s.in_stock} шт</span>
                        <span style={{ color: "var(--tg-link)" }}>Купить →</span>
                    </div>
                </button>
            ))}
        </div>
    );
}

// ─── Service card with buy ─────────────────────────────────────

function ServiceView(props: { service: Service; onBack: () => void }) {
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<CheckoutResponse | null>(null);
    const [error, setError] = useState<string | null>(null);
    const s = props.service;
    const userBalance = me.value?.balance_kopecks ?? 0;
    const enough = userBalance >= s.rub_price_kopecks;

    async function handleBuy() {
        const tg = window.Telegram?.WebApp;
        if (!enough) {
            tg?.HapticFeedback?.notificationOccurred("warning");
            return;
        }
        const confirmed = await new Promise<boolean>((resolve) => {
            if (tg?.showConfirm) {
                tg.showConfirm(
                    `Купить «${s.service_name}» за ${formatRub(s.rub_price_kopecks)}?`,
                    resolve,
                );
            } else {
                resolve(window.confirm(`Купить за ${formatRub(s.rub_price_kopecks)}?`));
            }
        });
        if (!confirmed) return;
        setBusy(true);
        setError(null);
        try {
            const r = await api.checkout(s.ns_service_id);
            setResult(r);
            if (r.outcome === "ok") {
                tg?.HapticFeedback?.notificationOccurred("success");
                // обновляем me чтобы баланс отразился
                api.me().then((m) => (me.value = m));
            } else {
                tg?.HapticFeedback?.notificationOccurred("error");
            }
        } catch (e) {
            setError(e instanceof ApiError ? e.message : String(e));
            tg?.HapticFeedback?.notificationOccurred("error");
        } finally {
            setBusy(false);
        }
    }

    const stockPercent = Math.min(100, Math.max(2, (s.in_stock / 50) * 100));

    return (
        <div class="screen">
            <div class="screen-header">
                <button class="btn-secondary btn" style={{ width: "auto", padding: "8px 14px" }} onClick={props.onBack}>
                    ← Назад
                </button>
                <h1>Покупка</h1>
            </div>
            <div class="card">
                <div class="service-name">{s.service_name}</div>
                {s.category_name && (
                    <div style={{ color: "var(--tg-hint)", fontSize: 13 }}>
                        {s.category_name}
                    </div>
                )}
                <div class="service-price">{formatRub(s.rub_price_kopecks)}</div>
                <div class="stock-bar">
                    <div class="stock-bar-fill" style={{ width: `${stockPercent}%` }} />
                </div>
                <div style={{ color: "var(--tg-hint)", fontSize: 13 }}>
                    В наличии: <strong>{s.in_stock} шт</strong>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Оплата с баланса</div>
                <div class="row">
                    <div class="row-title">Ваш баланс</div>
                    <div class="row-aside">{formatRub(userBalance)}</div>
                </div>
                <div class="row">
                    <div class="row-title">К списанию</div>
                    <div class="row-aside" style={{ color: "var(--tg-accent)" }}>
                        −{formatRub(s.rub_price_kopecks)}
                    </div>
                </div>
            </div>

            {!enough && (
                <div class="error-box">
                    Недостаточно средств. Не хватает{" "}
                    <strong>{formatRub(s.rub_price_kopecks - userBalance)}</strong>.
                    Пополните в боте: 💰 Баланс → 🪙 CryptoBot.
                </div>
            )}

            {result?.outcome === "ok" && (
                <div class="success-box">
                    ✅ Заказ #{result.order_id} оплачен. Открой вкладку «Заказы»
                    — выдача занимает 5–60 секунд.
                </div>
            )}
            {result?.outcome === "insufficient_balance" && (
                <div class="error-box">Недостаточно средств на балансе.</div>
            )}
            {result && result.outcome !== "ok" && result.outcome !== "insufficient_balance" && (
                <div class="error-box">
                    Ошибка: {result.outcome}
                </div>
            )}
            {error && <div class="error-box">{error}</div>}

            <button
                class="btn"
                disabled={busy || !enough || s.in_stock <= 0}
                onClick={handleBuy}
            >
                {busy ? "Обрабатываем…" : `Купить за ${formatRub(s.rub_price_kopecks)}`}
            </button>
        </div>
    );
}
