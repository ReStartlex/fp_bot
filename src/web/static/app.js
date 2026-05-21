const state = {
  view: "dashboard",
  token: localStorage.getItem("funpayApiToken") || "",
};

const viewMeta = {
  dashboard: ["Dashboard", "Операционный обзор магазина"],
  orders: ["Orders", "Последние заказы и статусы обработки"],
  problems: ["Problems", "Ошибки, pins_ready и выключенные маппинги"],
  mappings: ["Mappings", "Связки FunPay лотов с NS сервисами"],
  profit: ["Profit", "Выручка, себестоимость и маржа"],
};

const el = (id) => document.getElementById(id);

function money(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toLocaleString("ru-RU", { maximumFractionDigits: 0 })} ₽`;
}

function pct(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(1)}%`;
}

function esc(value) {
  return String(value ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function statusBadge(status) {
  const cls = status === "delivered" ? "good" : status === "failed" ? "bad" : "warn";
  return `<span class="badge ${cls}">${esc(status)}</span>`;
}

function showAlert(message, kind = "warn") {
  const box = el("alert");
  box.textContent = message;
  box.className = `alert ${kind === "error" ? "error" : ""}`;
}

function hideAlert() {
  el("alert").className = "alert hidden";
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text.slice(0, 180)}`);
  }
  return response.json();
}

async function checkHealth() {
  try {
    await fetch("/healthz");
    el("apiStatus").className = "status-dot good";
    el("apiStatusText").textContent = "API online";
  } catch {
    el("apiStatus").className = "status-dot";
    el("apiStatusText").textContent = "API offline";
  }
}

function setLoading() {
  el("view").innerHTML = '<div class="card"><div class="muted">Загрузка...</div></div>';
}

function renderCards(items) {
  return `<div class="grid cards">${items.map((item) => `
    <div class="card">
      <div class="card-title">${esc(item.title)}</div>
      <div class="card-value">${esc(item.value)}</div>
      <div class="card-note">${esc(item.note || "")}</div>
    </div>`).join("")}</div>`;
}

function renderTable(columns, rows) {
  return `<div class="table-wrap"><table>
    <thead><tr>${columns.map((col) => `<th>${esc(col.label)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((row) => `<tr>${columns.map((col) => `<td>${col.render(row)}</td>`).join("")}</tr>`).join("")}</tbody>
  </table></div>`;
}

async function renderDashboard() {
  const data = await api("/api/dashboard");
  const sync = data.sync || {};
  const orders = data.orders || {};
  const mappings = data.mappings || {};
  const guardrails = data.guardrails || {};
  el("view").innerHTML = `
    ${renderCards([
      { title: "Orders", value: orders.total ?? 0, note: `active ${orders.active ?? 0}, problem ${orders.problem ?? 0}` },
      { title: "Mappings", value: mappings.total ?? 0, note: `enabled ${mappings.enabled ?? 0}, disabled ${mappings.disabled ?? 0}` },
      { title: "Last Sync", value: sync.last_status || "—", note: `checked ${sync.last_checked ?? 0}, updated ${sync.last_updated ?? 0}` },
      { title: "Guardrails", value: `≥ ${guardrails.min_margin_percent ?? "?"}%`, note: `max jump ${guardrails.max_price_change_percent ?? "?"}%` },
    ])}
    <div class="toolbar">
      <div class="muted">Операции запускаются через API и не требуют Telegram.</div>
      <div>
        <button class="button secondary" data-action="syncDry">Dry-run sync</button>
        <button class="button secondary" data-action="reconcile">Reconcile</button>
      </div>
    </div>
  `;
}

async function renderOrders() {
  const rows = await api("/api/orders?limit=100");
  el("view").innerHTML = renderTable([
    { label: "Order", render: (r) => `<span class="mono">${esc(r.funpay_order_id)}</span>` },
    { label: "Status", render: (r) => statusBadge(r.status) },
    { label: "Lot", render: (r) => `<span class="mono">${esc(r.funpay_lot_id)}</span>` },
    { label: "Buyer", render: (r) => esc(r.buyer_username) },
    { label: "Revenue", render: (r) => money(r.funpay_price_rub) },
    { label: "Profit", render: (r) => money(r.profit_rub) },
    { label: "Updated", render: (r) => esc(r.updated_at) },
  ], rows);
}

async function renderProblems() {
  const data = await api("/api/problems");
  const orders = data.orders || [];
  const mappings = data.disabled_mappings || [];
  el("view").innerHTML = `
    ${renderCards([
      { title: "Problem orders", value: orders.length, note: "failed / pins_ready" },
      { title: "Disabled mappings", value: mappings.length, note: "требуют проверки" },
    ])}
    <div class="toolbar">
      <h2>Orders</h2>
      <button class="button secondary" data-action="reconcile">Reconcile</button>
    </div>
    ${renderTable([
      { label: "Order", render: (r) => `<span class="mono">${esc(r.funpay_order_id)}</span>` },
      { label: "Status", render: (r) => statusBadge(r.status) },
      { label: "Lot", render: (r) => esc(r.funpay_lot_id) },
      { label: "Error", render: (r) => esc(r.error) },
    ], orders)}
    <div class="toolbar"><h2>Disabled mappings</h2></div>
    ${renderTable([
      { label: "Lot", render: (r) => `<span class="mono">${esc(r.funpay_lot_id)}</span>` },
      { label: "NS", render: (r) => `<span class="mono">${esc(r.ns_service_id)}</span>` },
      { label: "Label", render: (r) => esc(r.label) },
      { label: "Group", render: (r) => esc(r.group_name) },
    ], mappings)}
  `;
}

async function renderMappings() {
  const rows = await api("/api/mappings?limit=300");
  el("view").innerHTML = renderTable([
    { label: "Lot", render: (r) => `<span class="mono">${esc(r.funpay_lot_id)}</span>` },
    { label: "NS service", render: (r) => `<span class="mono">${esc(r.ns_service_id)}</span>` },
    { label: "Status", render: (r) => `<span class="badge ${r.enabled ? "good" : "bad"}">${r.enabled ? "enabled" : "disabled"}</span>` },
    { label: "Label", render: (r) => esc(r.label) },
    { label: "Group", render: (r) => esc(r.group_name) },
    { label: "Markup", render: (r) => r.markup_percent === null ? "default" : pct(r.markup_percent) },
    { label: "Stock cap", render: (r) => esc(r.stock_cap ?? "default") },
  ], rows);
}

async function renderProfit() {
  const data = await api("/api/profit?days=7");
  el("view").innerHTML = renderCards([
    { title: "Orders", value: data.orders_counted ?? 0, note: `exact fx ${data.exact_orders ?? 0}` },
    { title: "Revenue", value: money(data.revenue_rub), note: "за 7 дней" },
    { title: "NS Cost", value: money(data.cost_rub), note: `fx fallback ${Number(data.fallback_fx_rate || 0).toFixed(2)}` },
    { title: "FunPay Withdrawal", value: money(data.withdrawal_fee_rub), note: `${pct(data.withdrawal_fee_percent)} от продаж` },
    { title: "Net Profit", value: money(data.profit_rub), note: `margin ${pct(data.margin_percent)}` },
  ]);
}

async function render() {
  hideAlert();
  const [title, subtitle] = viewMeta[state.view];
  el("pageTitle").textContent = title;
  el("pageSubtitle").textContent = subtitle;
  setLoading();
  try {
    if (!state.token) showAlert("Вставь WEB_API_TOKEN и нажми «Сохранить токен».");
    if (state.view === "dashboard") await renderDashboard();
    if (state.view === "orders") await renderOrders();
    if (state.view === "problems") await renderProblems();
    if (state.view === "mappings") await renderMappings();
    if (state.view === "profit") await renderProfit();
  } catch (error) {
    showAlert(error.message, "error");
    el("view").innerHTML = '<div class="card"><div class="muted">Не удалось загрузить данные.</div></div>';
  }
}

async function runAction(action) {
  try {
    setLoading();
    if (action === "syncDry") {
      const result = await api("/api/sync?dry_run=true", { method: "POST" });
      showAlert(`Dry-run sync: checked=${result.checked}, updated=${result.updated}, skipped=${result.skipped}`);
    }
    if (action === "reconcile") {
      const result = await api("/api/reconcile", { method: "POST" });
      showAlert(`Reconcile: checked=${result.checked}, recovered=${result.recovered}, failed=${result.failed}`);
    }
    await render();
  } catch (error) {
    showAlert(error.message, "error");
  }
}

document.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-view]");
  if (nav) {
    state.view = nav.dataset.view;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    nav.classList.add("active");
    render();
    return;
  }
  const action = event.target.closest("[data-action]");
  if (action) runAction(action.dataset.action);
});

el("saveToken").addEventListener("click", () => {
  state.token = el("apiToken").value.trim();
  localStorage.setItem("funpayApiToken", state.token);
  render();
});

el("refresh").addEventListener("click", render);
el("apiToken").value = state.token;
checkHealth();
render();
