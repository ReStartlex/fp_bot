/**
 * Профиль: баланс + рефералы + ссылка на бот для top-up.
 */
import { me } from "../App";
import { formatRub, formatRubExact } from "../format";

export function ProfileScreen() {
    const u = me.value;
    if (!u) return <div class="loading">Загрузка…</div>;

    const refLink = u.username
        ? `https://t.me/${u.username}`
        : `tg://user?id=${u.telegram_user_id}`;

    return (
        <div class="screen">
            <div class="screen-header">
                <h1>👤 Профиль</h1>
            </div>

            <div class="card">
                <div class="card-title">Баланс</div>
                <div class="balance-amount">{formatRub(u.balance_kopecks)}</div>
                <p style={{ margin: "8px 0 0", color: "var(--tg-hint)", fontSize: 13 }}>
                    Пополнение и вывод — через бот: 💰 Баланс → 🪙 CryptoBot
                </p>
            </div>

            <div class="card">
                <div class="card-title">Статистика</div>
                <div class="row">
                    <div class="row-title">Всего пополнено</div>
                    <div class="row-aside">{formatRub(u.total_earned_kopecks)}</div>
                </div>
                <div class="row">
                    <div class="row-title">Всего потрачено</div>
                    <div class="row-aside">{formatRub(u.total_spent_kopecks)}</div>
                </div>
                <div class="row">
                    <div class="row-title">Операций</div>
                    <div class="row-aside">{u.operations_count}</div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">Рефералы</div>
                <div class="row">
                    <div class="row-title">Приглашено</div>
                    <div class="row-aside">{u.invited_count}</div>
                </div>
                <div class="row">
                    <div class="row-title">Активных</div>
                    <div class="row-aside">{u.active_referrals_count}</div>
                </div>
                <div class="row">
                    <div class="row-title">Кешбэк</div>
                    <div class="row-aside" style={{ color: "var(--ok)" }}>
                        +{formatRubExact(u.earned_via_referrals_kopecks)}
                    </div>
                </div>
                <div class="row">
                    <div class="row-title">Процент</div>
                    <div class="row-aside">{u.referral_percent}%</div>
                </div>
                <p style={{ margin: "12px 0 0", color: "var(--tg-hint)", fontSize: 13 }}>
                    Реф-ссылка — в боте: 👥 Рефералы. Каждый раз, когда ваш
                    приглашённый покупает — вам {u.referral_percent}% от суммы
                    на баланс.
                </p>
            </div>

            <a
                href={refLink}
                style={{ display: "block", textDecoration: "none" }}
            >
                <div class="btn btn-secondary">Открыть бот @NeuroDrop</div>
            </a>
        </div>
    );
}
