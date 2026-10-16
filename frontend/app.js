/* frontend/app.js — Polymarket Bot Command Center SPA */

const API = "";  // Same origin
let pnlChart = null;
let ws = null;
let wsReconnectTimer = null;
window._rankingsFilter = "all";

// ── Router ─────────────────────────────────────────────────────────────────

function navigate(page) {
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  const pageEl = document.getElementById(`page-${page}`);
  const navEl = document.getElementById(`nav-${page}`);
  if (pageEl) pageEl.classList.add("active");
  if (navEl) navEl.classList.add("active");

  const titles = {
    dashboard: ["Dashboard", "Live trading overview"],
    copy: ["Copy Traders", "Mirror top performers"],
    opportunities: ["Opportunities", "All AI-discovered edges — browse by confidence tier"],
    approvals: ["Best Picks", "High-confidence picks closing within 30 days — act now"],
    future: ["Future Trades", "Long-horizon plays (30+ days out) — grouped by time window"],
    rankings: ["Rankings", "Performance & trade outcome history"],
    trades: ["Trade History", "All executed trades"],
    profile: ["My Profile", "On-chain stats, positions & trade history"],
    assistant: ["AI Assistant", "Ask anything about your trades and markets"],
    consensus: ["Consensus Trades", "Markets where multiple top traders share the same position"],
    journal: ["Trade Journal", "Save and annotate trade ideas with notes, tags, and ratings"],
    settings: ["Settings", "Bot configuration"],
  };
  const [title, sub] = titles[page] || ["", ""];
  document.getElementById("page-title").textContent = title;
  document.getElementById("page-subtitle").textContent = sub;

  // Load data for page
  if (page === "dashboard") { loadDashboard(); }
  else if (page === "copy")  { loadLeaderboard(); loadCopyTraders(); loadSignals(); }
  else if (page === "opportunities") { loadOpportunities(); }
  else if (page === "approvals") { loadPendingApprovals(); }
  else if (page === "future") { loadFutureTrades(); }
  else if (page === "rankings") { loadRankings(); }
  else if (page === "trades") { loadTrades(); }
  else if (page === "profile") { loadProfile(); }
  else if (page === "assistant") { loadAssistantPage(); }
  else if (page === "consensus") { loadConsensus(); }
  else if (page === "journal") { loadJournalPage(); }
  else if (page === "settings") { loadStatus(); loadCleanupStats(); }
}

document.querySelectorAll(".nav-item").forEach(item => {
  item.addEventListener("click", e => {
    e.preventDefault();
    navigate(item.dataset.page);
  });
});

// ── WebSocket ───────────────────────────────────────────────────────────────

let _wsBackoff = 2000;

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    _wsBackoff = 2000;
    setWsStatus("connected");
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
  };

  ws.onmessage = e => {
    const data = JSON.parse(e.data);
    handleWsEvent(data);
  };

  ws.onclose = () => {
    setWsStatus("error");
    wsReconnectTimer = setTimeout(() => { _wsBackoff = Math.min(_wsBackoff * 2, 30000); connectWS(); }, _wsBackoff);
  };

  ws.onerror = () => setWsStatus("error");
}

// Keepalive — registered once, not on every reconnect
setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);

function setWsStatus(state) {
  const dot = document.getElementById("ws-dot");
  const label = document.getElementById("ws-label");
  dot.className = `connection-dot ${state}`;
  label.textContent = state === "connected" ? "Connected" : state === "error" ? "Reconnecting..." : "Connecting...";
}

function handleWsEvent(data) {
  const now = new Date().toLocaleTimeString();
  switch (data.event) {
    case "connected":
      updateStatusUI(data.status);
      break;
    case "trade_executed":
      addFeedItem(`Trade: ${data.trade?.side} $${data.trade?.size?.toFixed(2)} — ${truncate(data.trade?.question, 50)}`, "trade", now);
      loadDashboard();
      break;
    case "scan_complete": {
      scanInProgress = false;
      addFeedItem(`Scan complete — ${data.opportunities} opportunities found`, "scan", now);
      const _activePage = currentPage();
      if (_activePage === "opportunities") loadOpportunities();
      else if (_activePage === "approvals") loadPendingApprovals();
      else if (_activePage === "future") loadFutureTrades();
      break;
    }
    case "copy_signals":
      addFeedItem(`${data.count} copy signal(s) received`, "signal", now);
      break;
    case "bot_started":
      updateBotButtons(true);
      addFeedItem(`Bot started in ${data.mode} mode`, "scan", now);
      break;
    case "bot_stopped":
      updateBotButtons(false);
      addFeedItem("Bot stopped", "scan", now);
      break;
    case "mode_changed":
      updateModeBadge(data.mode);
      break;
    case "trade_alert":
      addFeedItem(data.message, "signal", now);
      handleTradeAlert(data);
      break;
    case "error":
      addFeedItem(`Error: ${data.message}`, "error", now);
      break;
  }
}

// ── Pending Trades (persisted) ─────────────────────────────────────────────

let pendingTrades = [];

function handleTradeAlert(data) {
  // Show browser notification immediately for real-time feel
  if (Notification.permission === "granted") {
    new Notification("PolyBot Trade Alert", { body: data.message });
  }
  // Reload from DB so the list is always the authoritative persisted state
  loadDashboardPendingTrades();
}

async function loadDashboardPendingTrades() {
  const container = document.getElementById("pending-trades-list");
  const badge = el("pending-count");
  try {
    const data = await get("/api/pending-trades/dashboard");
    pendingTrades = data.pending || [];
    if (badge) badge.textContent = pendingTrades.length;
    renderPendingTrades();
  } catch (err) {
    console.error("Failed to load pending trades:", err);
    if (container) container.innerHTML = `<div class="feed-empty">Failed to load pending approvals</div>`;
  }
}

function renderPendingTrades() {
  const container = document.getElementById("pending-trades-list");
  if (!container) return;
  if (!pendingTrades.length) {
    container.innerHTML = `<div class="feed-empty">No pending trades requiring approval</div>`;
    return;
  }
  container.innerHTML = pendingTrades.map((t, i) => {
    const opp = t.opportunity || t.signal;
    const side = opp.side || opp.recommended_side || "YES";
    const isResearch = !!t.opportunity;
    const isCopy = !!t.signal;

    // Research: use market_id (DB id not available in WebSocket events)
    // Copy: use _signal_id from the signal record
    let outcomeFeedbackHtml = "";
    if (isResearch && opp.market_id) {
      const safeId = esc(opp.market_id);
      outcomeFeedbackHtml = `
        <div class="outcome-feedback" id="outcome-mkt-${safeId}">
          <span class="outcome-label">Rate this pick:</span>
          <button class="btn-outcome btn-outcome-success" onclick="App.markOutcomeByMarket('${safeId}', 'success', this)">✓ Win</button>
          <button class="btn-outcome btn-outcome-failure" onclick="App.markOutcomeByMarket('${safeId}', 'failure', this)">✗ Loss</button>
        </div>`;
    } else if (isCopy && opp._signal_id) {
      const sigId = opp._signal_id;
      const traderLabel = esc(opp.trader_name || "Trader");
      outcomeFeedbackHtml = `
        <div class="outcome-feedback" id="outcome-sig-${sigId}">
          <span class="outcome-label">Rate ${traderLabel}'s call:</span>
          <button class="btn-outcome btn-outcome-success" onclick="App.markSignalOutcome(${sigId}, 'success', this)">✓ Win</button>
          <button class="btn-outcome btn-outcome-failure" onclick="App.markSignalOutcome(${sigId}, 'failure', this)">✗ Loss</button>
        </div>`;
    }

    return `
      <div class="pending-trade-card">
        <div class="pending-meta">
          <span class="badge badge-research">${t.tier}</span>
          <span class="badge badge-${side.toLowerCase()}">${side}</span>
          ${isCopy ? `<span class="badge badge-copy" style="font-size:0.65rem">${esc(opp.trader_name || "Copy")}</span>` : ""}
        </div>
        <div class="pending-question">${esc(opp.question)}</div>
        <div class="pending-actions">
          <button class="btn btn-success btn-sm" onclick="App.approveTrade(${i})">Approve</button>
          <button class="btn btn-outline btn-sm" onclick="App.dismissTrade(${i})">Dismiss</button>
          ${isResearch
            ? `<button class="btn-journal"
                data-etype="opportunity"
                data-sid="${opp.id || 0}"
                data-q="${escAttr(opp.question)}"
                data-side="${escAttr(side)}"
                data-price="${opp.yes_price || 0.5}"
                data-edge="${opp.edge_pct || 0}"
                data-conf="${escAttr(opp.confidence || '')}"
                data-mid="${escAttr(opp.market_id || '')}"
                onclick="App.openJournalFromCard(this)">📓 Journal</button>`
            : `<button class="btn-journal"
                data-etype="copy_signal"
                data-sid="${opp._signal_id || 0}"
                data-q="${escAttr(opp.question)}"
                data-side="${escAttr(side)}"
                data-price="${opp.price || 0.5}"
                data-edge="0"
                data-conf=""
                data-mid="${escAttr(opp.market_id || '')}"
                onclick="App.openJournalFromCard(this)">📓 Journal</button>`
          }
        </div>
        ${outcomeFeedbackHtml}
      </div>
    `;
  }).join("");
}

async function approveTrade(index) {
  const t = pendingTrades[index];
  if (!t) return;
  const opp = t.opportunity || t.signal;
  const side = opp.side || opp.recommended_side || "YES";
  const size = t.opportunity ? 20.0 : opp.copied_size;
  const price = t.opportunity
    ? (side === "YES" ? opp.yes_price : opp.no_price)
    : opp.price;

  const result = await post("/api/trade", {
    market_id: opp.market_id,
    question: opp.question,
    side: side,
    size_usd: size,
    price: price,
    token_id: side === "YES" ? (opp.yes_token_id || "") : (opp.no_token_id || ""),
  });

  if (result.status === "filled" || result.status === "pending") {
    // For copy signals, also mark as executed in DB so it leaves the pending list
    const sig = t.signal;
    if (sig?._signal_id) {
      await post(`/api/pending-trades/signal/${sig._signal_id}/dismiss`, {});
    }
    addFeedItem(`Approved ${side} $${size} — ${truncate(opp.question, 40)}`, "trade", new Date().toLocaleTimeString());
  } else {
    alert("Trade failed: " + (result.error || "Unknown error"));
  }

  // Always refresh from DB — this is the authoritative source
  await loadDashboardPendingTrades();
}

async function dismissTrade(index) {
  const t = pendingTrades[index];
  if (!t) return;

  // Persist the dismissal so it doesn't reappear on reload
  try {
    const opp = t.opportunity;
    const sig = t.signal;
    if (opp?.id) {
      await post(`/api/pending-trades/research/${opp.id}/dismiss`, {});
    } else if (sig?._signal_id) {
      await post(`/api/pending-trades/signal/${sig._signal_id}/dismiss`, {});
    }
  } catch (err) {
    console.error("Failed to persist dismissal:", err);
  }

  await loadDashboardPendingTrades();
}

// ── API Helpers ─────────────────────────────────────────────────────────────

async function get(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`);
  return r.json();
}

async function post(path, body = {}) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`);
  return r.json();
}

async function del(path) {
  const r = await fetch(`${API}${path}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`);
  return r.json();
}

async function put(path, body = {}) {
  const r = await fetch(`${API}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`);
  return r.json();
}

function showToast(message, type = "success") {
  const colors = { success: "var(--green)", error: "var(--red)", info: "var(--primary)" };
  const toast = document.createElement("div");
  toast.style.cssText = `position:fixed;bottom:88px;right:20px;background:var(--card);border:1px solid ${colors[type]||colors.info};color:var(--text);padding:10px 16px;border-radius:var(--radius-sm);font-size:0.82rem;z-index:10000;box-shadow:0 4px 20px rgba(0,0,0,0.4);transition:opacity 0.3s;pointer-events:none;max-width:320px`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => { toast.style.opacity = "0"; setTimeout(() => toast.remove(), 300); }, 2800);
}

// ── Dashboard ───────────────────────────────────────────────────────────────

async function loadDashboard() {
  try {
    const [portfolio, status, trades, chartResult] = await Promise.all([
      get("/api/portfolio"),
      get("/api/status"),
      get("/api/trades?limit=10"),
      get("/api/me/pnl-chart?days=30").catch(() => null),
    ]);

    // Stat cards
    el("stat-balance").textContent = `$${fmtNum(portfolio.paper_balance)}`;
    const pnl = portfolio.total_pnl || 0;
    el("stat-pnl").textContent = `${pnl >= 0 ? "+" : ""}$${fmtNum(pnl)}`;
    el("stat-pnl").className = `stat-value ${pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : ""}`;
    el("stat-winrate").textContent = `${portfolio.win_rate || 0}% win rate`;
    el("stat-trades").textContent = portfolio.total_trades || 0;
    el("stat-positions").textContent = `${portfolio.open_positions || 0} open positions`;
    el("stat-signals").textContent = status.copy_signals_today || 0;
    el("stat-lastscan").textContent = status.last_scan
      ? `Last scan: ${timeAgo(status.last_scan)}`
      : "No scan yet";

    // Update bot UI
    updateBotButtons(status.running);
    updateModeBadge(status.mode);
    el("model-badge").textContent = status.ai_model || "claude";

    // Chart — prefer on-chain Polymarket data; fall back to paper portfolio data
    const onChainData = chartResult?.data;
    if (onChainData && onChainData.length > 0) {
      renderPnLChart(onChainData, "Polymarket");
    } else {
      renderPnLChart(portfolio.daily_pnl || [], "Paper");
    }

    // Recent trades
    renderTradesTable("recent-trades-body", trades, 7);

    // Always reload pending approvals from DB — survives page reload
    loadDashboardPendingTrades();
  } catch (err) {
    console.error("Failed to load dashboard data:", err);
    addFeedItem("Failed to load dashboard data. Check server status.", "error", new Date().toLocaleTimeString());
  }
}

function renderPnLChart(data, source) {
  const ctx = document.getElementById("pnl-chart");
  if (!ctx) return;

  // Update the card title to show data source
  const titleEl = ctx.closest(".card")?.querySelector(".card-title");
  if (titleEl) {
    titleEl.textContent = source === "Polymarket"
      ? "P&L Over Time — Polymarket"
      : "P&L Over Time — Paper";
  }

  if (typeof Chart === "undefined") {
    console.warn("Chart.js not loaded. Skipping chart render.");
    ctx.parentElement.innerHTML = '<div class="feed-empty">Chart.js failed to load. Please check your internet connection.</div>';
    return;
  }

  if (pnlChart) pnlChart.destroy();

  const labels = data.map(d => d.date);
  const values = data.map(d => d.pnl);

  // Cumulative P&L
  let cum = 0;
  const cumValues = values.map(v => { cum += v; return cum; });

  const isPositive = (cumValues[cumValues.length - 1] || 0) >= 0;
  const lineColor = isPositive ? "#10B981" : "#F43F5E";
  const fillColor = isPositive ? "rgba(16,185,129,0.08)" : "rgba(244,63,94,0.08)";

  pnlChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: labels.length ? labels : ["—"],
      datasets: [{
        label: "Cumulative P&L",
        data: cumValues.length ? cumValues : [0],
        borderColor: lineColor,
        backgroundColor: fillColor,
        borderWidth: 2,
        fill: true,
        tension: 0.4,
        pointRadius: cumValues.length <= 15 ? 3 : 0,
        pointBackgroundColor: lineColor,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => `$${ctx.parsed.y >= 0 ? "+" : ""}${ctx.parsed.y.toFixed(2)}`,
          },
        },
      },
      scales: {
        x: { grid: { color: "rgba(99,179,237,0.06)" }, ticks: { color: "#64748b", font: { size: 10 } } },
        y: {
          grid: { color: "rgba(99,179,237,0.06)" },
          ticks: { color: "#64748b", font: { size: 10 }, callback: v => `$${v}` },
        },
      },
    },
  });
}

// ── Status ──────────────────────────────────────────────────────────────────

async function loadStatus() {
  const status = await get("/api/status");
  updateStatusUI(status);
}

function updateStatusUI(status) {
  if (!status) return;
  updateBotButtons(status.running);
  updateModeBadge(status.mode);
  el("model-badge").textContent = status.ai_model || "claude";

  const info = el("sys-info");
  if (info) {
    info.innerHTML = [
      ["Status", status.running ? "🟢 Running" : "🔴 Stopped"],
      ["Mode", status.mode],
      ["AI Model", status.ai_model],
      ["Paper Balance", `$${fmtNum(status.paper_balance || 0)}`],
      ["WS Clients", status.connections || 0],
      ["Scan Count", status.scan_count || 0],
      ["Last Scan", status.last_scan ? timeAgo(status.last_scan) : "Never"],
    ].map(([k,v]) => `<div class="info-row"><span>${k}</span><span>${v}</span></div>`).join("");
  }

  // Sync settings mode radios
  const paperR = document.getElementById("mode-paper");
  const liveR = document.getElementById("mode-live");
  if (paperR && liveR) {
    paperR.checked = status.mode === "paper";
    liveR.checked = status.mode === "live";
  }
  const modelSel = document.getElementById("settings-model");
  if (modelSel) modelSel.value = status.ai_model || "claude";
}

// ── Copy Traders ─────────────────────────────────────────────────────────────

async function loadCopyTraders() {
  const traders = await get("/api/copy-traders");
  const tbody = el("traders-body");
  if (!traders.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No traders followed yet. Follow traders from the leaderboard above.</td></tr>`;
    return;
  }
  tbody.innerHTML = traders.filter(t => t.active).map(t => `
    <tr>
      <td>
        <div style="font-weight:500">${esc(t.name || "Unknown")}</div>
        <div style="font-size:0.7rem;color:var(--text-muted);font-family:'JetBrains Mono',monospace">${t.address.slice(0,10)}...${t.address.slice(-6)}</div>
      </td>
      <td class="${t.roi > 0 ? "pnl-pos" : t.roi < 0 ? "pnl-neg" : ""}">${(t.roi * 100).toFixed(1)}%</td>
      <td class="${t.total_profit > 0 ? "pnl-pos" : "pnl-neg"}">$${fmtNum(t.total_profit)}</td>
      <td>${(t.win_rate * 100).toFixed(1)}%</td>
      <td>$${fmtNum(t.volume)}</td>
      <td>${t.active ? '<span class="badge badge-filled">Active</span>' : '<span class="badge badge-failed">Paused</span>'}</td>
      <td><button class="btn btn-del btn-sm" onclick="App.removeTrader(${t.id})">Remove</button></td>
    </tr>
  `).join("");
}

async function loadLeaderboard() {
  const tbody = el("leaderboard-body");
  tbody.innerHTML = `<tr><td colspan="8" class="empty-row">Loading leaderboard...</td></tr>`;
  
  const traders = await get("/api/copy-traders/leaderboard?limit=20");
  if (!traders.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-row">Failed to load leaderboard. Try again.</td></tr>`;
    return;
  }
  
  tbody.innerHTML = traders.map((t, idx) => `
    <tr>
      <td style="font-weight:600;color:var(--primary)">#${idx + 1}</td>
      <td>
        <div style="font-weight:500">${esc(t.name || "Unknown")}</div>
        <div style="font-size:0.7rem;color:var(--text-muted);font-family:'JetBrains Mono',monospace">${t.address.slice(0,10)}...${t.address.slice(-6)}</div>
      </td>
      <td class="${t.roi > 0 ? "pnl-pos" : t.roi < 0 ? "pnl-neg" : ""}">${(t.roi * 100).toFixed(1)}%</td>
      <td class="${t.profit > 0 ? "pnl-pos" : "pnl-neg"}">$${fmtNum(t.profit)}</td>
      <td>${(t.win_rate * 100).toFixed(1)}%</td>
      <td>$${fmtNum(t.volume)}</td>
      <td>${t.positions_count || 0}</td>
      <td>
        ${t.is_followed 
          ? `<span class="badge badge-filled">Following</span>`
          : `<button class="btn btn-primary btn-sm" onclick="App.followTrader('${t.address}')">Follow</button>`
        }
      </td>
    </tr>
  `).join("");
  
  addFeedItem(`Loaded ${traders.length} top traders from leaderboard`, "signal", new Date().toLocaleTimeString());
}

async function followTrader(address) {
  const result = await post("/api/copy-traders/follow", { address });
  if (result.status === "followed") {
    addFeedItem(`Now following ${result.name} (ROI: ${(result.roi * 100).toFixed(1)}%)`, "signal", new Date().toLocaleTimeString());
    loadLeaderboard(); // Refresh to show "Following" badge
    loadCopyTraders(); // Refresh my traders list
  } else {
    alert("Failed to follow trader: " + (result.message || "Unknown error"));
  }
}

async function loadSignals() {
  const signals = await get("/api/copy-signals");
  window._copySignals = signals;
  const tbody = el("signals-body");
  if (!signals.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No signals yet</td></tr>`;
    return;
  }
  tbody.innerHTML = signals.map((s, idx) => `
    <tr class="clickable-row" title="Click for full details" onclick="App.showSignalDetails(${idx})">
      <td>${esc(s.trader_name || "—")}</td>
      <td>${esc(s.question?.slice(0,40) || "—")}...</td>
      <td>${s.side}</td>
      <td>$${s.copied_size}</td>
      <td>${s.price?.toFixed(2) || "—"}</td>
      <td><span class="badge badge-${s.status === "pending" ? "warn" : s.status === "executed" ? "filled" : "failed"}">${s.status}</span></td>
      <td>${s.timestamp ? new Date(s.timestamp).toLocaleString() : "—"}</td>
    </tr>
  `).join("");
}

function signalDetailRow(label, valueHtml) {
  return `<div class="signal-detail-row"><span class="signal-detail-label">${label}</span><span class="signal-detail-value">${valueHtml}</span></div>`;
}

function showSignalDetails(idx) {
  const s = (window._copySignals || [])[idx];
  if (!s) return;

  const statusClass = s.status === "pending" ? "pending" : s.status === "executed" ? "filled" : s.status === "skipped" ? "failed" : "low";
  const timeStr = s.timestamp ? new Date(s.timestamp).toLocaleString() : "—";
  const priceStr = s.price != null ? `$${Number(s.price).toFixed(3)} / share` : "—";
  const origStr = s.original_size != null ? `$${fmtNum(s.original_size)}` : "—";
  const copyStr = s.copied_size != null ? `$${fmtNum(s.copied_size)}` : "—";
  const sharesStr = (s.copied_size != null && s.price > 0) ? `≈ ${fmtNum(s.copied_size / s.price)} shares` : "—";
  const addr = s.trader_address || "—";
  const addrShort = addr.length > 14 ? `${addr.slice(0, 8)}…${addr.slice(-6)}` : addr;
  const mid = s.market_id || "—";
  const midShort = mid.length > 20 ? `${mid.slice(0, 14)}…${mid.slice(-6)}` : mid;
  const ratingVal = s.rating || 0;
  const starsHtml = `<span class="star-rating">${[1, 2, 3, 4, 5].map(v =>
    `<span class="star${v <= ratingVal ? " filled" : ""}" data-val="${v}" title="Rate ${v}/5 — click again to clear" onclick="App.rateSignal(${s.id}, ${v})">${v <= ratingVal ? "★" : "☆"}</span>`
  ).join("")}</span>`;
  // Same Win/Loss outcome marking as the dashboard pending-trade cards
  const traderLabel = esc(s.trader_name || "Trader");
  const outcomeHtml = s.outcome
    ? `<div class="outcome-marked ${s.outcome}">${s.outcome === "success" ? "✓ Marked as Win" : "✗ Marked as Loss"}</div>`
    : `<div class="outcome-feedback">
         <span class="outcome-label">Rate ${traderLabel}'s call:</span>
         <button class="btn-outcome btn-outcome-success" onclick="App.markSignalOutcome(${s.id}, 'success', this)">✓ Win</button>
         <button class="btn-outcome btn-outcome-failure" onclick="App.markSignalOutcome(${s.id}, 'failure', this)">✗ Loss</button>
       </div>`;

  el("signal-details-question").innerHTML = esc(s.question || "Unknown market");
  el("signal-details").innerHTML = [
    signalDetailRow("Trader", `<strong>${esc(s.trader_name || "Unknown")}</strong>&nbsp;<span class="signal-detail-mono" title="${escAttr(addr)}">${esc(addrShort)}</span>`),
    signalDetailRow("Side", `<span class="badge badge-${s.side === "NO" ? "no" : "yes"}">${esc(s.side || "—")}</span>`),
    signalDetailRow("Signal price", esc(priceStr)),
    signalDetailRow("Whale's position size", esc(origStr)),
    signalDetailRow("Suggested copy size", esc(copyStr)),
    signalDetailRow("Estimated shares", esc(sharesStr)),
    signalDetailRow("Status", `<span class="badge badge-${statusClass}">${esc(s.status)}</span>`),
    signalDetailRow("Detected at", esc(timeStr)),
    signalDetailRow("Your rating", starsHtml),
    signalDetailRow("Outcome", outcomeHtml),
    signalDetailRow("Market (condition) ID", `<span class="signal-detail-mono" title="${escAttr(mid)}">${esc(midShort)}</span>`),
    signalDetailRow("Signal ID", `#${s.id}`),
  ].join("");

  el("signal-modal-overlay").style.display = "flex";
}

async function rateSignal(signalId, val) {
  const sig = (window._copySignals || []).find(x => x.id === signalId);
  const newRating = (sig && sig.rating === val) ? null : val; // clicking the active star clears the rating
  try {
    const r = await post(`/api/copy-signals/${signalId}/rating`, { rating: newRating });
    if (r && r.ok) {
      if (sig) sig.rating = r.rating;
      const wrap = document.querySelector("#signal-details .star-rating");
      if (wrap) wrap.querySelectorAll(".star").forEach((st, i) => {
        const filled = i < (r.rating || 0);
        st.textContent = filled ? "★" : "☆";
        st.classList.toggle("filled", filled);
      });
      addFeedItem(`Rated signal #${signalId}: ${r.rating ? r.rating + "/5 ★" : "rating cleared"}`, "signal", new Date().toLocaleTimeString());
    }
  } catch (e) {
    alert("Failed to save rating: " + e.message);
  }
}

function closeSignalDetails() {
  el("signal-modal-overlay").style.display = "none";
}

// ── Pending Approvals ───────────────────────────────────────────────────────

function renderApprovalCard(a, idx, isReviewed = false, approveHandler = "approveTradeDirect") {
  const score = a.quality_score ?? null;
  const tierClass = score !== null
    ? (score >= 80 ? "approval-tier-gold" : score >= 60 ? "approval-tier-silver" : "approval-tier-bronze")
    : "approval-tier-bronze";
  const isUrgent = a.days_until != null && a.days_until <= 7;

  return `
    <div class="approval-card ${tierClass}${isReviewed ? ' approval-reviewed' : ''}" data-market-id="${esc(a.market_id)}">
      <div class="approval-header">
        ${a.tier ? `<span class="approval-tier-badge">${a.tier}</span>` : '<span></span>'}
        ${score !== null ? `<span class="approval-score">${score}</span>` : ''}
      </div>

      <div class="approval-question">${esc(a.question)}</div>

      <div class="approval-meta">
        <span>📊 Edge: <strong>+${(a.edge_pct || 0).toFixed(1)}%</strong></span>
        <span>🎯 Side: <strong>${a.side?.toUpperCase() || '—'}</strong></span>
        <span>💰 Vol: $${fmtNum(a.volume_24h)}</span>
        <span>🤖 ${a.model_used || "AI"}</span>
      </div>

      <div class="approval-prices">
        <span class="approval-yes">YES ${((a.yes_price || 0) * 100).toFixed(1)}¢</span>
        <span class="approval-no">NO ${((a.no_price || 0) * 100).toFixed(1)}¢</span>
        ${a.trader_matches?.length ? `<span style="margin-left:auto;color:var(--primary)">👥 ${a.trader_matches.length} traders aligned</span>` : ""}
      </div>

      ${a.event_date_display || a.end_date ? `
      <div class="approval-event-date ${isUrgent ? "urgent" : ""}">
        <span>📅</span>
        <span><strong>${a.event_date_display || a.end_date}</strong></span>
        ${a.days_until != null ? `<span style="margin-left:auto">${a.days_until <= 0 ? "⏰ Event ended" : a.days_until === 1 ? "⏰ Closes tomorrow!" : `⏰ ${a.days_until} days remaining`}</span>` : ""}
      </div>` : ''}

      <div class="approval-analysis">${esc(a.analysis || "No analysis available")}</div>

      ${!isReviewed ? `
      <div class="approval-actions">
        <button class="btn btn-success" onclick="App.${approveHandler}(${idx})">✓ Approve Trade</button>
        <button class="btn btn-danger" onclick="App.dismissApproval('${esc(a.market_id)}')">✗ Dismiss</button>
        <button class="btn-journal"
          data-etype="opportunity"
          data-sid="${a.id || 0}"
          data-q="${escAttr(a.question)}"
          data-side="${escAttr(a.side || 'YES')}"
          data-price="${a.yes_price || 0.5}"
          data-edge="${a.edge_pct || 0}"
          data-conf="${escAttr(a.confidence || '')}"
          data-mid="${escAttr(a.market_id || '')}"
          onclick="App.openJournalFromCard(this)">📓 Journal</button>
      </div>` : ''}

      <div class="outcome-feedback" id="outcome-${a.id}">
        ${renderOutcomeFeedback(a.id, a.outcome)}
      </div>
    </div>
  `;
}

async function loadPendingApprovals() {
  const list = el("approvals-list");
  const countLabel = el("approvals-count");

  list.innerHTML = `<div class="approvals-empty"><div class="loading-spinner"></div><p>Loading best picks...</p></div>`;

  try {
    const [data, reviewedData] = await Promise.all([
      get("/api/pending-approvals"),
      get("/api/reviewed-opportunities"),
    ]);

    const dismissed = new Set(JSON.parse(localStorage.getItem("dismissedApprovals") || "[]"));
    const newPicks = (data.approvals || []).filter(a => !dismissed.has(a.market_id));

    window._approvals = newPicks;

    // Stats reflect only new picks
    let gold = 0, silver = 0, bronze = 0, urgent = 0;
    newPicks.forEach(a => {
      if (a.quality_score >= 80) gold++;
      else if (a.quality_score >= 60) silver++;
      else bronze++;
      if (a.days_until !== null && a.days_until <= 7) urgent++;
    });

    el("approvals-gold").textContent = gold || "—";
    el("approvals-silver").textContent = silver || "—";
    el("approvals-bronze").textContent = bronze || "—";
    el("approvals-urgent").textContent = urgent || "—";

    countLabel.textContent = `${newPicks.length} trade${newPicks.length !== 1 ? 's' : ''}`;

    // ── New Picks list ──
    if (!newPicks.length) {
      list.innerHTML = `<div class="approvals-empty">
        <p>🎯 No new picks at this time</p>
        <p style="font-size:0.75rem;color:var(--text-muted);margin-top:0.5rem">Run an AI scan to find opportunities with edge &gt;3% and medium+ confidence</p>
        <button class="btn btn-primary" style="margin-top:1rem" onclick="App.runScan()">🔍 Run AI Scan Now</button>
      </div>`;
    } else {
      list.innerHTML = newPicks.map((a, idx) => renderApprovalCard(a, idx, false)).join("");
    }

    // ── Reviewed Trades table ──
    const reviewedCount = el("reviewed-count");
    const reviewedBody = el("reviewed-body");
    const reviewed = reviewedData.reviewed || [];

    reviewedCount.textContent = reviewed.length ? `${reviewed.length} trade${reviewed.length !== 1 ? 's' : ''}` : '';
    reviewedBody.innerHTML = reviewed.length
      ? reviewed.map(renderReviewedRow).join("")
      : `<tr><td colspan="7" class="empty-row" style="color:var(--text-muted)">No reviewed trades yet — rate a pick as Win or Loss to see it here</td></tr>`;

  } catch (err) {
    list.innerHTML = `<div class="approvals-empty"><p>❌ Failed to load approvals</p><p style="font-size:0.75rem">${err.message}</p></div>`;
  }
}

function _removeApprovalCard(marketId) {
  const card = document.querySelector(`.approval-card[data-market-id="${marketId}"]`);
  if (card) {
    card.style.transition = 'opacity 0.3s, transform 0.3s';
    card.style.opacity = '0';
    card.style.transform = 'translateX(-30px)';
    setTimeout(() => card.remove(), 320);
  }
}

function dismissApproval(marketId) {
  const dismissed = JSON.parse(localStorage.getItem("dismissedApprovals") || "[]");
  if (!dismissed.includes(marketId)) {
    dismissed.push(marketId);
    localStorage.setItem("dismissedApprovals", JSON.stringify(dismissed));
  }
  _removeApprovalCard(marketId);
  addFeedItem("Approval dismissed", "info", new Date().toLocaleTimeString());
}

// ── Future Trades ────────────────────────────────────────────────────────────

async function loadFutureTrades() {
  const list = el("future-list");
  const countLabel = el("future-count");

  list.innerHTML = `<div class="approvals-empty"><div class="loading-spinner"></div><p>Loading future trades...</p></div>`;

  try {
    const [data, reviewedData] = await Promise.all([
      get("/api/future-trades"),
      get("/api/reviewed-opportunities"),
    ]);

    const dismissed = new Set(JSON.parse(localStorage.getItem("dismissedApprovals") || "[]"));
    const newTrades = (data.trades || []).filter(a => !dismissed.has(a.market_id));

    window._futureTrades = newTrades;

    let gold = 0, silver = 0, bronze = 0, totalDays = 0, daysCount = 0;
    newTrades.forEach(a => {
      if (a.quality_score >= 80) gold++;
      else if (a.quality_score >= 60) silver++;
      else bronze++;
      if (a.days_until != null) { totalDays += a.days_until; daysCount++; }
    });

    el("future-gold").textContent = gold || "—";
    el("future-silver").textContent = silver || "—";
    el("future-bronze").textContent = bronze || "—";
    el("future-avg-days").textContent = daysCount ? Math.round(totalDays / daysCount) : "—";

    countLabel.textContent = `${newTrades.length} trade${newTrades.length !== 1 ? 's' : ''}`;

    if (!newTrades.length) {
      list.innerHTML = `<div class="approvals-empty">
        <p>🔮 No long-horizon trades found</p>
        <p style="font-size:0.75rem;color:var(--text-muted);margin-top:0.5rem">Run an AI scan — this page shows opportunities with events more than 30 days away</p>
        <button class="btn btn-primary" style="margin-top:1rem" onclick="App.runScan()">🔍 Run AI Scan Now</button>
      </div>`;
    } else {
      const horizonOrder = ["30–60 Days", "2–3 Months", "3–6 Months", "6+ Months", "Unknown"];
      const byHorizon = {};
      newTrades.forEach((a, idx) => {
        const h = a.horizon || "Unknown";
        if (!byHorizon[h]) byHorizon[h] = [];
        byHorizon[h].push({ a, idx });
      });

      const horizonIcons = {
        "30–60 Days": "📅",
        "2–3 Months": "🗓️",
        "3–6 Months": "📆",
        "6+ Months": "🔭",
        "Unknown": "❓",
      };
      const horizonDesc = {
        "30–60 Days": "Events 31–60 days out — medium-term positioning",
        "2–3 Months": "Events 2–3 months away — build a thesis now",
        "3–6 Months": "Events 3–6 months out — early mover opportunity",
        "6+ Months": "Long-range plays — high uncertainty, high upside",
        "Unknown": "End date unclear",
      };

      list.innerHTML = horizonOrder
        .filter(h => byHorizon[h]?.length)
        .map(h => `
          <div class="future-horizon-section">
            <div class="future-horizon-header">
              <span class="future-horizon-icon">${horizonIcons[h]}</span>
              <span class="future-horizon-label">${h}</span>
              <span class="opp-tier-count">${byHorizon[h].length}</span>
              <span class="opp-tier-desc">${horizonDesc[h]}</span>
            </div>
            ${byHorizon[h].map(({ a, idx }) => renderApprovalCard(a, idx, false, "approveFutureTrade")).join("")}
          </div>
        `).join("");
    }

    const reviewedCount = el("future-reviewed-count");
    const reviewedBody = el("future-reviewed-body");
    const reviewed = reviewedData.reviewed || [];

    reviewedCount.textContent = reviewed.length ? `${reviewed.length} trade${reviewed.length !== 1 ? 's' : ''}` : '';
    reviewedBody.innerHTML = reviewed.length
      ? reviewed.map(renderReviewedRow).join("")
      : `<tr><td colspan="7" class="empty-row" style="color:var(--text-muted)">No reviewed trades yet — rate a pick as Win or Loss to see it here</td></tr>`;

  } catch (err) {
    list.innerHTML = `<div class="approvals-empty"><p>❌ Failed to load future trades</p><p style="font-size:0.75rem">${err.message}</p></div>`;
  }
}

async function approveFutureTrade(idx) {
  const a = window._futureTrades?.[idx];
  if (!a) return;
  const side = a.side || "YES";
  const price = side === "YES" ? a.yes_price : a.no_price;

  const sizeStr = prompt(`Enter trade size in USD for ${side} (max $100 recommended):`, "20");
  if (!sizeStr) return;
  const size = parseFloat(sizeStr);
  if (isNaN(size) || size <= 0) return;

  const trade = await post("/api/trade", {
    market_id: a.market_id,
    question: a.question,
    side,
    size_usd: size,
    price,
    source: "research",
  });

  if (trade.status === "filled" || trade.status === "pending") {
    addFeedItem(`Approved ${side} $${size} on "${truncate(a.question, 40)}"`, "trade", new Date().toLocaleTimeString());
    _removeApprovalCard(a.market_id);
  } else {
    alert("Trade failed: " + (trade.error || "Unknown error"));
  }
}

// ── Opportunities ────────────────────────────────────────────────────────────

let scanInProgress = false;

async function loadOpportunities() {
  const grid = el("opp-grid");
  if (scanInProgress) {
    grid.innerHTML = `<div class="opp-empty"><div class="loading-spinner"></div>Scanning markets with AI... This may take 10-30 seconds.</div>`;
    return;
  }

  let opps;
  try {
    opps = await get("/api/opportunities");
  } catch (err) {
    grid.innerHTML = `<div class="opp-empty">Failed to load opportunities: ${err.message}</div>`;
    return;
  }

  // Split into fresh (unreviewed) and reviewed
  const fresh = opps.filter(o => !o.outcome);
  const reviewed = opps.filter(o => o.outcome);

  window._opportunities = fresh;

  // ── Main grid: fresh opportunities grouped by confidence tier ──
  if (!fresh.length) {
    grid.innerHTML = `<div class="opp-empty">No new opportunities found. Try lowering the Min Edge % in settings or run a new scan.</div>`;
  } else {
    const tiers = [
      { key: "high",   label: "High Confidence", icon: "🔥", desc: "Strong edge signal with AI conviction" },
      { key: "medium", label: "Medium Confidence", icon: "⚡", desc: "Decent edge — worth monitoring" },
      { key: "low",    label: "Low Confidence", icon: "👀", desc: "Speculative — small edge or unclear signal" },
    ];

    const byConf = { high: [], medium: [], low: [] };
    fresh.forEach((o, idx) => {
      const c = (o.confidence || "low").toLowerCase();
      if (byConf[c]) byConf[c].push({ o, idx });
    });

    const renderOppCard = ({ o, idx }) => `
      <div class="opp-card" data-market-id="${esc(o.market_id)}">
        <div class="opp-top">
          <div class="opp-question">${esc(o.question)}</div>
          <div class="opp-edge">+${(o.edge_pct||0).toFixed(1)}%</div>
        </div>
        <div class="opp-prices">
          <span class="opp-yes">YES ${(o.yes_price*100).toFixed(1)}¢</span>
          <span style="color:var(--text-muted)">|</span>
          <span class="opp-no">NO ${(o.no_price*100).toFixed(1)}¢</span>
        </div>
        <div class="opp-meta">
          <span class="badge badge-${o.side?.toLowerCase()}">${o.side}</span>
          <span class="badge badge-research" style="font-size:0.65rem">${o.model_used}</span>
          <span class="opp-volume">Vol $${fmtNum(o.volume_24h)}</span>
          ${o.days_until != null ? `<span class="opp-volume">${o.days_until === 0 ? '⏰ Today' : o.days_until === 1 ? '⏰ Tomorrow' : `📅 ${o.days_until}d`}</span>` : ''}
        </div>
        ${o.trader_matches?.length ? `
          <div class="opp-trader-alignment">
            <div class="trader-align-badge">👥 ${o.trader_matches.length} top trader${o.trader_matches.length > 1 ? 's' : ''} also ${o.side}</div>
            <div class="trader-list">${o.trader_matches.map(t => `<span class="trader-tag">${esc(t.trader_name)} ($${fmtNum(t.size)})</span>`).join('')}</div>
          </div>
        ` : ''}
        ${o.analysis ? `<div class="opp-analysis">${esc(o.analysis)}</div>` : ""}
        <div class="opp-actions">
          <button class="btn btn-success btn-sm" onclick="App.tradeOpportunity(${idx}, 'YES')">Buy YES</button>
          <button class="btn btn-danger btn-sm" onclick="App.tradeOpportunity(${idx}, 'NO')">Buy NO</button>
          <button class="btn btn-outline btn-sm" onclick="App.dismissOpportunity(${idx})">Dismiss</button>
          <button class="btn-journal"
            data-etype="opportunity" data-sid="${o.id||0}" data-q="${escAttr(o.question)}"
            data-side="${escAttr(o.side||'YES')}" data-price="${o.yes_price||0.5}"
            data-edge="${o.edge_pct||0}" data-conf="${escAttr(o.confidence||'')}"
            data-mid="${escAttr(o.market_id||'')}"
            onclick="App.openJournalFromCard(this)">📓</button>
        </div>
        <div class="outcome-feedback" id="outcome-${o.id}">${renderOutcomeFeedback(o.id, o.outcome)}</div>
        <div style="margin-top:8px;font-size:0.7rem;color:var(--text-muted)">${timeAgo(o.scanned_at)}</div>
      </div>`;

    grid.innerHTML = tiers
      .filter(t => byConf[t.key].length > 0)
      .map(t => `
        <div class="opp-tier-section">
          <div class="opp-tier-header">
            <span class="opp-tier-icon">${t.icon}</span>
            <span class="opp-tier-label">${t.label}</span>
            <span class="opp-tier-count">${byConf[t.key].length}</span>
            <span class="opp-tier-desc">${t.desc}</span>
          </div>
          <div class="opp-tier-grid">${byConf[t.key].map(renderOppCard).join("")}</div>
        </div>
      `).join("");
  }

  // ── Reviewed table ──
  const tbody = el("opp-reviewed-body");
  const countEl = el("opp-reviewed-count");
  countEl.textContent = reviewed.length ? `${reviewed.length} trade${reviewed.length !== 1 ? 's' : ''}` : '';
  if (!reviewed.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-row" style="color:var(--text-muted)">No reviewed opportunities yet — rate a trade as Win or Loss to see it here</td></tr>`;
  } else {
    tbody.innerHTML = reviewed.map(o => {
      const outcomeClass = o.outcome === "success" ? "badge-filled" : "badge-failed";
      const outcomeLabel = o.outcome === "success" ? "✓ Win" : "✗ Loss";
      return `<tr>
        <td style="max-width:260px;font-size:0.8rem">${truncate(o.question, 60)}</td>
        <td><span class="badge badge-${o.side?.toLowerCase()}">${o.side}</span></td>
        <td style="font-family:'JetBrains Mono',monospace">+${(o.edge_pct||0).toFixed(1)}%</td>
        <td style="font-size:0.75rem">${o.model_used || "—"}</td>
        <td><span class="badge ${outcomeClass}">${outcomeLabel}</span></td>
        <td style="font-size:0.75rem;color:var(--text-muted)">${timeAgo(o.scanned_at)}</td>
      </tr>`;
    }).join("");
  }
}

async function tradeOpportunity(idx, side) {
  const o = window._opportunities?.[idx];
  if (!o) return;
  
  const price = side === "YES" ? o.yes_price : o.no_price;
  const size = prompt(`Enter trade size in USD for ${side} (max $100 recommended):`, "20");
  if (!size || isNaN(size) || size <= 0) return;
  
  const trade = await post("/api/trade", {
    market_id: o.market_id,
    question: o.question,
    side: side,
    size_usd: parseFloat(size),
    price: price,
  });
  
  if (trade.status === "filled" || trade.status === "pending") {
    addFeedItem(`Executed ${side} $${size} on "${truncate(o.question, 40)}"`, "trade", new Date().toLocaleTimeString());
    // Remove from display
    dismissOpportunity(idx);
  } else {
    alert("Trade failed: " + (trade.error || "Unknown error"));
  }
}

async function approveTradeDirect(idx) {
  const a = window._approvals?.[idx];
  if (!a) return;
  const side = a.side || "YES";
  const price = side === "YES" ? a.yes_price : a.no_price;

  const sizeStr = prompt(`Enter trade size in USD for ${side} (max $100 recommended):`, "20");
  if (!sizeStr) return;
  const size = parseFloat(sizeStr);
  if (isNaN(size) || size <= 0) return;

  const trade = await post("/api/trade", {
    market_id: a.market_id,
    question: a.question,
    side: side,
    size_usd: size,
    price: price,
    source: "research",
  });

  if (trade.status === "filled" || trade.status === "pending") {
    addFeedItem(`Approved ${side} $${size} on "${truncate(a.question, 40)}"`, "trade", new Date().toLocaleTimeString());
    _removeApprovalCard(a.market_id);
  } else {
    alert("Trade failed: " + (trade.error || "Unknown error"));
  }
}

function dismissOpportunity(idx) {
  const cards = document.querySelectorAll('.opp-card');
  if (cards[idx]) {
    cards[idx].style.opacity = '0.5';
    cards[idx].style.pointerEvents = 'none';
    setTimeout(() => {
      cards[idx].style.display = 'none';
    }, 300);
  }
}

async function runScan() {
  scanInProgress = true;
  if (currentPage() === "opportunities") loadOpportunities(); // show loading spinner
  await post("/api/scan");
  addFeedItem("AI scan started...", "scan", new Date().toLocaleTimeString());
}

// ── Trades ───────────────────────────────────────────────────────────────────

async function loadTrades(source = "") {
  const url = source ? `/api/trades?limit=200&source=${source}` : "/api/trades?limit=200";
  const trades = await get(url);
  const tbody = el("trades-body");
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-row">No trades yet</td></tr>`;
    return;
  }
  tbody.innerHTML = trades.map((t, i) => {
    const pnl = t.pnl || 0;
    const pnlClass = pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "pnl-zero";
    return `
      <tr>
        <td style="color:var(--text-muted);font-family:'JetBrains Mono',monospace">#${t.id}</td>
        <td style="max-width:200px">${truncate(t.question, 50)}</td>
        <td><span class="badge badge-${t.side?.toLowerCase()}">${t.side}</span></td>
        <td>$${fmtNum(t.size)}</td>
        <td style="font-family:'JetBrains Mono',monospace">${(t.price*100).toFixed(1)}¢</td>
        <td class="${pnlClass}">${pnl !== 0 ? (pnl > 0 ? "+" : "") + "$" + fmtNum(Math.abs(pnl)) : "—"}</td>
        <td><span class="badge badge-${t.source}">${t.source}</span></td>
        <td style="font-size:0.75rem;color:var(--text-muted)">${t.model_used || "—"}</td>
        <td><span class="badge badge-${t.mode}">${t.mode}</span></td>
        <td style="font-size:0.75rem;color:var(--text-muted)">${timeAgo(t.timestamp)}</td>
      </tr>
    `;
  }).join("");
}

function renderTradesTable(tbodyId, trades, cols = 7) {
  const tbody = el(tbodyId);
  if (!trades?.length) {
    tbody.innerHTML = `<tr><td colspan="${cols}" class="empty-row">No trades yet</td></tr>`;
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnl = t.pnl || 0;
    const pnlClass = pnl > 0 ? "pnl-pos" : pnl < 0 ? "pnl-neg" : "pnl-zero";
    return `
      <tr>
        <td style="max-width:220px">${truncate(t.question, 55)}</td>
        <td><span class="badge badge-${t.side?.toLowerCase()}">${t.side}</span></td>
        <td>$${fmtNum(t.size)}</td>
        <td style="font-family:'JetBrains Mono',monospace">${(t.price*100).toFixed(1)}¢</td>
        <td><span class="badge badge-${t.source}">${t.source}</span></td>
        <td><span class="badge badge-${t.status}">${t.status}</span></td>
        <td style="font-size:0.75rem;color:var(--text-muted)">${timeAgo(t.timestamp)}</td>
      </tr>`;
  }).join("");
}

// ── Bot Controls ─────────────────────────────────────────────────────────────

async function startBot() {
  await post("/api/bot/start");
  updateBotButtons(true);
}

async function stopBot() {
  await post("/api/bot/stop");
  updateBotButtons(false);
}

function updateBotButtons(running) {
  const start = el("btn-start");
  const stop = el("btn-stop");
  start.style.display = running ? "none" : "inline-flex";
  stop.style.display = running ? "inline-flex" : "none";
}

function updateModeBadge(mode) {
  const badge = el("mode-badge");
  badge.textContent = mode?.toUpperCase() || "PAPER";
  badge.className = `mode-badge${mode === "live" ? " live" : ""}`;
}

async function setMode(mode) {
  if (mode === "live") {
    const ok = confirm("⚠️ Switch to LIVE TRADING? Real money will be used!");
    if (!ok) {
      document.getElementById("mode-paper").checked = true;
      return;
    }
  }
  await post("/api/bot/mode", { mode });
  updateModeBadge(mode);
}

async function setModel(model) {
  await post("/api/bot/model", { model });
  el("model-badge").textContent = model;
  // Sync both selects
  const sel1 = document.getElementById("model-select");
  const sel2 = document.getElementById("settings-model");
  if (sel1) sel1.value = model;
  if (sel2) sel2.value = model;
}

// ── Trader Management ─────────────────────────────────────────────────────────

async function refreshTopTraders() {
  await post("/api/copy-traders/refresh");
  addFeedItem("Leaderboard refresh queued...", "signal", new Date().toLocaleTimeString());
  setTimeout(() => loadCopyTraders(), 3000);
}

function showAddTraderModal() {
  el("modal-overlay").style.display = "flex";
  el("trader-address").value = "";
  el("trader-name").value = "";
}

function closeModal() {
  el("modal-overlay").style.display = "none";
}

async function addTrader() {
  const address = el("trader-address").value.trim();
  const name = el("trader-name").value.trim();
  if (!address) { alert("Address is required"); return; }
  await post("/api/copy-traders", { address, name: name || null });
  closeModal();
  loadCopyTraders();
}

async function removeTrader(id) {
  if (!confirm("Remove this trader?")) return;
  await del(`/api/copy-traders/${id}`);
  loadCopyTraders();
}

// ── Live Feed ─────────────────────────────────────────────────────────────────

function addFeedItem(text, type, time) {
  const feed = el("live-feed");
  const empty = feed.querySelector(".feed-empty");
  if (empty) empty.remove();

  const div = document.createElement("div");
  div.className = `feed-item ${type}`;
  div.innerHTML = `${esc(text)} <span class="feed-time">${time}</span>`;
  feed.prepend(div);

  // Keep max 30 items
  while (feed.children.length > 30) feed.lastChild.remove();
}

function renderReviewedRow(o) {
  const outcomeClass = o.outcome === "success" ? "badge-filled" : "badge-failed";
  const outcomeLabel = o.outcome === "success" ? "✓ Win" : "✗ Loss";
  return `<tr>
    <td style="max-width:260px;font-size:0.8rem">${truncate(o.question, 60)}</td>
    <td><span class="badge badge-${(o.side||'').toLowerCase()}">${o.side || '—'}</span></td>
    <td style="font-family:'JetBrains Mono',monospace">+${(o.edge_pct||0).toFixed(1)}%</td>
    <td style="font-size:0.75rem">${o.tier || '—'}</td>
    <td style="font-size:0.75rem">${o.model_used || '—'}</td>
    <td><span class="badge ${outcomeClass}">${outcomeLabel}</span></td>
    <td style="font-size:0.75rem;color:var(--text-muted)">${o.event_date_display || o.end_date || '—'}</td>
  </tr>`;
}

// ── Outcome Feedback ──────────────────────────────────────────────────────────

function renderOutcomeFeedback(oppId, currentOutcome) {
  if (currentOutcome) {
    const isSuccess = currentOutcome === "success";
    return `<div class="outcome-marked ${currentOutcome}">
      ${isSuccess ? "✓ Marked as Win" : "✗ Marked as Loss"}
    </div>`;
  }
  return `
    <span class="outcome-label">Rate this pick:</span>
    <button class="btn-outcome btn-outcome-success" onclick="App.markOutcome(${oppId}, 'success', this)">✓ Win</button>
    <button class="btn-outcome btn-outcome-failure" onclick="App.markOutcome(${oppId}, 'failure', this)">✗ Loss</button>
  `;
}

async function markOutcome(oppId, outcome, buttonEl) {
  try {
    const approvalCard = buttonEl.closest('.approval-card');
    const oppCard = buttonEl.closest('.opp-card');
    const card = approvalCard || oppCard;
    const result = await post(`/api/opportunities/${oppId}/outcome`, { outcome });
    if (result.status === "updated") {
      addFeedItem(`Outcome: ${outcome} — pick #${oppId}`, "trade", new Date().toLocaleTimeString());
      if (card) {
        card.style.transition = 'opacity 0.35s, transform 0.35s';
        card.style.opacity = '0';
        card.style.transform = 'translateX(-20px)';
        const _page = currentPage();
        setTimeout(() => {
          if (_page === "future") loadFutureTrades();
          else if (_page === "opportunities") loadOpportunities();
          else loadPendingApprovals();
        }, 400);
      } else {
        _applyOutcomeUI(buttonEl, outcome);
      }
    }
  } catch (err) {
    console.error("Failed to mark outcome:", err);
  }
}

async function markOutcomeByMarket(marketId, outcome, buttonEl, question = "") {
  try {
    const result = await post("/api/outcomes/by-market", { market_id: marketId, outcome, question });
    if (result.status === "updated") {
      _applyOutcomeUI(buttonEl, outcome);
      addFeedItem(`Outcome: ${outcome} — pick saved`, "trade", new Date().toLocaleTimeString());
    }
  } catch (err) {
    console.error("Failed to mark outcome by market:", err);
    showToast("Failed to save outcome", "error");
  }
}

async function markSignalOutcome(signalId, outcome, buttonEl) {
  try {
    const result = await post(`/api/copy-signals/${signalId}/outcome`, { outcome });
    if (result.status === "updated") {
      _applyOutcomeUI(buttonEl, outcome);
      // Keep the copy-page modal cache in sync so the marked state survives reopening
      const cached = (window._copySignals || []).find(x => x.id === signalId);
      if (cached) cached.outcome = outcome;
      const trader = result.trader || "trader";
      addFeedItem(`Outcome: ${outcome} — ${trader}'s signal`, "signal", new Date().toLocaleTimeString());
    }
  } catch (err) {
    console.error("Failed to mark signal outcome:", err);
  }
}

function _applyOutcomeUI(buttonEl, outcome) {
  const feedbackEl = buttonEl.closest(".outcome-feedback");
  if (!feedbackEl) return;
  const label = outcome === "success" ? "✓ Marked as Win" : "✗ Marked as Loss";
  feedbackEl.innerHTML = `<div class="outcome-marked ${outcome}">${label}</div>`;
}

// ── Rankings ──────────────────────────────────────────────────────────────────

async function loadRankings(filter) {
  if (filter) window._rankingsFilter = filter;

  // Sync tab active state
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.filter === window._rankingsFilter);
  });

  const topTradesEl = el("rankings-top-trades");
  const aiBodyEl = el("rankings-ai-body");
  const tradersBodyEl = el("rankings-traders-body");

  if (topTradesEl) {
    topTradesEl.innerHTML = `<div class="rank-empty"><div class="loading-spinner"></div><p>Loading rankings...</p></div>`;
  }

  try {
    const data = await get(`/api/rankings?time_filter=${window._rankingsFilter}`);
    const stats = data.stats || {};

    // Stat cards
    el("rank-total").textContent = stats.total_marked ?? "—";
    el("rank-rate").textContent = stats.total_marked ? `${stats.success_rate}%` : "—";
    el("rank-counts").textContent = `${stats.success_count || 0} wins / ${stats.failure_count || 0} losses`;
    el("rank-best-model").textContent = stats.best_model || "—";
    el("rank-best-accuracy").textContent = stats.best_model
      ? `${stats.best_model_accuracy}% accuracy`
      : "No data yet";
    el("rank-wins").textContent = stats.success_count ?? "—";

    // Top Winning Trades
    const topTrades = data.top_trades || [];
    if (!topTrades.length) {
      topTradesEl.innerHTML = `
        <div class="rank-empty">
          <div style="font-size:2.5rem;margin-bottom:12px">🏆</div>
          <p>No winning trades ranked yet</p>
          <p class="rank-empty-hint">Mark opportunities as ✓ Win on any page to see them ranked here</p>
        </div>`;
    } else {
      topTradesEl.innerHTML = `<div class="rank-trades-list">${
        topTrades.map((t, i) => {
          const tierClass = t.quality_score >= 80 ? "rank-tier-gold"
                          : t.quality_score >= 60 ? "rank-tier-silver"
                          : "rank-tier-bronze";
          const tierLabel = t.quality_score >= 80 ? "🥇 GOLD"
                          : t.quality_score >= 60 ? "🥈 SILVER"
                          : "🥉 BRONZE";
          return `
            <div class="rank-trade-card ${tierClass}">
              <div class="rank-trade-header">
                <span class="rank-num">#${i + 1}</span>
                <span class="rank-tier-badge">${tierLabel}</span>
                <span class="rank-edge">+${(t.edge_pct || 0).toFixed(1)}%</span>
              </div>
              <div class="rank-trade-question">${esc(t.question)}</div>
              <div class="rank-trade-meta">
                <span class="badge badge-${(t.side || "").toLowerCase()}">${t.side || "?"}</span>
                ${t.source === "copy"
                  ? `<span class="badge badge-copy" style="font-size:0.65rem">Copy</span>
                     ${t.trader_name ? `<span style="font-size:0.65rem;color:var(--text-sub)">${esc(t.trader_name)}</span>` : ""}`
                  : `<span class="badge badge-${t.confidence}">${t.confidence}</span>
                     <span class="badge badge-research" style="font-size:0.65rem">${esc(t.model_used || "AI")}</span>`
                }
                <span style="margin-left:auto;font-size:0.7rem;color:var(--text-muted)">${timeAgo(t.outcome_marked_at)}</span>
              </div>
            </div>
          `;
        }).join("")
      }</div>`;
    }

    // AI Model Rankings
    const aiRankings = data.ai_model_rankings || [];
    if (!aiRankings.length) {
      aiBodyEl.innerHTML = `<tr><td colspan="5" class="empty-row">No data — mark outcomes to track model accuracy</td></tr>`;
    } else {
      aiBodyEl.innerHTML = aiRankings.map((m, i) => {
        const accColor = m.accuracy >= 60 ? "var(--green)" : m.accuracy >= 40 ? "var(--yellow)" : "var(--red)";
        return `
          <tr>
            <td>
              <div style="display:flex;align-items:center;gap:6px">
                <span style="color:var(--primary);font-weight:700;font-family:'JetBrains Mono',monospace">#${i + 1}</span>
                <span class="badge badge-research">${esc(m.model)}</span>
              </div>
            </td>
            <td>
              <div style="font-weight:700;color:${accColor};font-family:'JetBrains Mono',monospace">${m.accuracy}%</div>
              <div style="height:4px;border-radius:2px;background:var(--border);margin-top:4px;overflow:hidden">
                <div style="height:100%;width:${m.accuracy}%;background:${accColor};border-radius:2px"></div>
              </div>
            </td>
            <td class="pnl-pos">${m.success_count}</td>
            <td class="pnl-neg">${m.failure_count}</td>
            <td style="font-family:'JetBrains Mono',monospace;color:var(--text-sub)">${m.avg_edge}%</td>
          </tr>
        `;
      }).join("");
    }

    // Trader Leaderboard
    const traderRankings = data.trader_rankings || [];
    if (!traderRankings.length) {
      tradersBodyEl.innerHTML = `<tr><td colspan="6" class="empty-row">No tracked traders — follow traders on the Copy Traders page</td></tr>`;
    } else {
      tradersBodyEl.innerHTML = traderRankings.map((t, i) => {
        const roiStr = t.roi != null ? `${(t.roi * 100).toFixed(1)}%` : "—";
        const roiCls = t.roi > 0 ? "pnl-pos" : t.roi < 0 ? "pnl-neg" : "";
        const polyWr = t.win_rate != null ? `${(t.win_rate * 100).toFixed(1)}%` : "—";
        const profitStr = t.total_profit != null ? `$${fmtNum(t.total_profit)}` : "—";
        const profitCls = t.total_profit > 0 ? "pnl-pos" : t.total_profit < 0 ? "pnl-neg" : "";

        let userRatingHtml;
        if (t.user_rated > 0) {
          const accColor = t.user_accuracy >= 60 ? "var(--green)" : t.user_accuracy >= 40 ? "var(--yellow)" : "var(--red)";
          userRatingHtml = `
            <div style="font-weight:700;color:${accColor};font-family:'JetBrains Mono',monospace;font-size:0.8rem">${t.user_accuracy}%</div>
            <div style="font-size:0.68rem;color:var(--text-muted)">${t.user_wins}W / ${t.user_losses}L</div>`;
        } else {
          userRatingHtml = `<span style="color:var(--text-muted);font-size:0.75rem">No ratings yet</span>`;
        }

        return `
          <tr>
            <td style="font-weight:700;color:var(--primary)">#${i + 1}</td>
            <td>
              <div style="font-weight:500">${esc(t.name || "Unknown")}</div>
              <div style="font-size:0.7rem;color:var(--text-muted);font-family:'JetBrains Mono',monospace">${t.address.slice(0,8)}…${t.address.slice(-4)}</div>
            </td>
            <td class="${roiCls}">${roiStr}</td>
            <td>${polyWr}</td>
            <td>${userRatingHtml}</td>
            <td class="${profitCls}">${profitStr}</td>
          </tr>
        `;
      }).join("");
    }

  } catch (err) {
    if (topTradesEl) topTradesEl.innerHTML = `<div class="rank-empty">Failed to load rankings: ${esc(err.message)}</div>`;
    console.error("Failed to load rankings:", err);
  }
}

// ── Utils ─────────────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }
function esc(str) { return (str || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function escAttr(str) { return (str || "").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/'/g,"&#39;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function truncate(str, n) { return str && str.length > n ? esc(str.slice(0, n)) + "..." : esc(str || ""); }
function fmtNum(n) { return Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
function timeAgo(iso) {
  if (!iso) return "—";
  const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}
function currentPage() {
  const active = document.querySelector(".page.active");
  return active ? active.id.replace("page-", "") : "dashboard";
}

async function fundAccount() {
  const amount = prompt("Enter amount to add to paper balance ($):", "1000");
  if (!amount || isNaN(amount)) return;
  await post("/api/bot/fund", { amount: parseFloat(amount) });
  loadDashboard();
}

// ── Cleanup / Maintenance ───────────────────────────────────────────────────

async function loadCleanupStats() {
  const statsDiv = el("cleanup-stats");
  const btn = el("cleanup-btn");
  if (!statsDiv) return;
  
  statsDiv.innerHTML = '<p style="font-size:0.85rem;color:var(--text-muted)">Loading...</p>';
  if (btn) btn.disabled = true; // Disable while loading
  
  try {
    const stats = await get("/api/cleanup/stats");
    console.log("Cleanup stats:", stats);
    
    const canCleanup = stats.can_cleanup;
    if (btn) btn.disabled = !canCleanup;
    
    statsDiv.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:0.5rem;font-size:0.8rem">
        <div class="stat-card" style="padding:8px">
          <div class="stat-value" style="font-size:1.2rem">${stats.total_opportunities}</div>
          <div class="stat-label">Total Opportunities</div>
        </div>
        <div class="stat-card" style="padding:8px;${stats.expired_events > 0 ? 'border-color:var(--red)' : ''}">
          <div class="stat-value" style="font-size:1.2rem;color:${stats.expired_events > 0 ? 'var(--red)' : 'inherit'}">${stats.expired_events}</div>
          <div class="stat-label">Expired Events</div>
        </div>
        <div class="stat-card" style="padding:8px;${stats.stale_scans > 0 ? 'border-color:orange' : ''}">
          <div class="stat-value" style="font-size:1.2rem;color:${stats.stale_scans > 0 ? 'orange' : 'inherit'}">${stats.stale_scans}</div>
          <div class="stat-label">Stale (7+ days)</div>
        </div>
        <div class="stat-card" style="padding:8px;border-color:var(--green)">
          <div class="stat-value" style="font-size:1.2rem;color:var(--green)">${stats.active_recent}</div>
          <div class="stat-label">Active Recent</div>
        </div>
      </div>
      ${stats.closing_soon?.length ? `
        <div style="margin-top:0.75rem;padding:0.5rem;background:rgba(99,102,241,0.1);border-radius:var(--radius-sm)">
          <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.25rem">⏰ Closing Soon:</div>
          ${stats.closing_soon.map(c => `<div style="font-size:0.7rem">• ${esc(c.question)} (${c.days_until}d)</div>`).join('')}
        </div>
      ` : ''}
    `;
  } catch (err) {
    statsDiv.innerHTML = `<p style="font-size:0.85rem;color:var(--red)">Error loading stats: ${err.message}</p>`;
  }
}

async function runCleanup() {
  const btn = el("cleanup-btn");
  const result = el("cleanup-result");
  
  console.log("runCleanup clicked, btn:", btn, "disabled:", btn?.disabled);
  
  if (!btn) {
    alert("Error: Cleanup button not found!");
    return;
  }
  
  if (btn.disabled) {
    alert("Button is disabled - no expired data to clean");
    return;
  }
  
  if (!confirm("This will permanently delete expired opportunities and stale data. Continue?")) return;
  
  btn.disabled = true;
  btn.textContent = "🗑️ Cleaning...";
  result.innerHTML = '<span style="color:var(--text-muted)">Running cleanup...</span>';
  
  try {
    const data = await post("/api/cleanup/expired", {});
    result.innerHTML = `
      <div style="color:var(--green)">✅ Cleanup complete!</div>
      <div style="font-size:0.75rem;margin-top:0.25rem">
        Removed ${data.removed} total (${data.expired_events} expired + ${data.stale_scans} stale)
      </div>
    `;
    addFeedItem(`Cleaned up ${data.removed} expired/stale opportunities`, "system", new Date().toLocaleTimeString());
    loadCleanupStats(); // Refresh stats
  } catch (err) {
    result.innerHTML = `<span style="color:var(--red)">❌ Cleanup failed: ${err.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "🗑️ Clean Expired Data";
  }
}

// ── My Profile ────────────────────────────────────────────────────────────────

let _cachedWallet = "";

async function loadProfile() {
  // Show spinners immediately
  el("profile-positions-body").innerHTML = `<tr><td colspan="6" class="empty-row"><div class="loading-spinner" style="margin:8px auto"></div></td></tr>`;
  el("profile-trades-body").innerHTML    = `<tr><td colspan="7" class="empty-row"><div class="loading-spinner" style="margin:8px auto"></div></td></tr>`;
  el("profile-insights").innerHTML       = `<div class="feed-empty"><div class="loading-spinner" style="margin:8px auto"></div></div>`;

  const [profile, positions, trades] = await Promise.all([
    get("/api/me/profile").catch(() => null),
    get("/api/me/positions").catch(() => []),
    get("/api/me/trades?limit=50").catch(() => []),
  ]);

  el("profile-refresh-time").textContent = `Updated ${new Date().toLocaleTimeString()}`;

  // Hero — just needs wallet address
  const addr = profile?.address || "";
  _cachedWallet = addr;
  el("profile-wallet-addr").textContent = addr ? `${addr.slice(0,8)}...${addr.slice(-6)}` : "—";
  el("profile-avatar").textContent = addr ? addr[2].toUpperCase() : "?";
  el("profile-name").textContent = profile?.name || "My Wallet";

  // All stats computed from real data only
  renderProfileStatCards(positions, trades);
  renderOpenPositions(positions);
  renderProfileInsights(positions, trades);
  renderProfileTrades(trades);
}

function renderProfileStatCards(positions, trades) {
  // ── Compute everything from positions + trades ──
  const enriched = positions.map(p => {
    const avg = p.avg_price || 0.5;
    const unreal = (p.current_price - avg) * (p.size / avg);
    return { ...p, unreal };
  });

  const totalDeployed = positions.reduce((s, p) => s + (p.size || 0), 0);
  const totalUnreal   = enriched.reduce((s, p) => s + p.unreal, 0);
  const winning       = enriched.filter(p => p.unreal > 0).length;
  const losing        = enriched.filter(p => p.unreal < 0).length;
  const tradeVol      = trades.reduce((s, t) => s + (t.size || 0), 0);
  const avgTrade      = trades.length ? tradeVol / trades.length : 0;

  const unrealCls  = totalUnreal > 0 ? "pnl-pos" : totalUnreal < 0 ? "pnl-neg" : "";
  const unrealSign = totalUnreal >= 0 ? "+" : "-";
  const healthCls  = winning > losing ? "pnl-pos" : winning < losing ? "pnl-neg" : "";

  el("profile-stats-grid").innerHTML = `
    <div class="stat-card accent-blue">
      <div class="stat-label">Capital Deployed</div>
      <div class="stat-value" style="font-size:1.5rem">$${fmtNum(totalDeployed)}</div>
      <div class="stat-sub">across ${positions.length} open position${positions.length !== 1 ? "s" : ""}</div>
    </div>
    <div class="stat-card ${totalUnreal > 0 ? "accent-green" : ""}">
      <div class="stat-label">Unrealized P&amp;L</div>
      <div class="stat-value ${unrealCls}" style="font-size:1.5rem">${unrealSign}$${fmtNum(Math.abs(totalUnreal))}</div>
      <div class="stat-sub">on open positions</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Position Health</div>
      <div class="stat-value ${healthCls}" style="font-size:1.5rem">${winning} / ${positions.length}</div>
      <div class="stat-sub">${winning} winning · ${losing} losing</div>
    </div>
    <div class="stat-card accent-purple">
      <div class="stat-label">Trade Volume</div>
      <div class="stat-value" style="font-size:1.5rem">$${fmtNum(tradeVol)}</div>
      <div class="stat-sub">${trades.length} trades · avg $${fmtNum(avgTrade)}</div>
    </div>
  `;
}

function renderOpenPositions(positions) {
  const badge = el("pos-count-badge");
  if (badge) badge.textContent = `${positions.length} open`;

  const tbody = el("profile-positions-body");
  if (!positions.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-row">No open positions found</td></tr>`;
    return;
  }

  const enriched = positions.map(p => {
    const avg = p.avg_price || 0.5;
    const unreal     = (p.current_price - avg) * (p.size / avg);
    const unreal_pct = ((p.current_price / avg) - 1) * 100;
    return { ...p, unreal, unreal_pct };
  }).sort((a, b) => b.unreal - a.unreal);  // best first

  const maxAbs = Math.max(...enriched.map(p => Math.abs(p.unreal)), 0.01);

  const sideCls = s => {
    const u = (s || "").toUpperCase();
    return u === "YES" ? "yes" : u === "NO" ? "no" : "research";
  };

  tbody.innerHTML = enriched.map(p => {
    const pnlCls  = p.unreal >= 0 ? "pnl-pos" : "pnl-neg";
    const barCls  = p.unreal >= 0 ? "pos" : "neg";
    const barW    = Math.min(100, (Math.abs(p.unreal) / maxAbs) * 100).toFixed(0);
    const sign    = p.unreal >= 0 ? "+" : "-";
    return `<tr>
      <td style="max-width:220px;font-size:0.8rem;line-height:1.35">${truncate(p.question, 55)}</td>
      <td><span class="badge badge-${sideCls(p.side)}">${esc(p.side)}</span></td>
      <td style="font-family:'JetBrains Mono',monospace">$${fmtNum(p.size)}</td>
      <td style="font-family:'JetBrains Mono',monospace">${(p.avg_price * 100).toFixed(1)}¢</td>
      <td style="font-family:'JetBrains Mono',monospace">${(p.current_price * 100).toFixed(1)}¢</td>
      <td>
        <div class="${pnlCls}" style="font-family:'JetBrains Mono',monospace;font-size:0.8rem;white-space:nowrap">
          ${sign}$${fmtNum(Math.abs(p.unreal))}
          <span style="font-size:0.7rem;opacity:0.7">(${p.unreal_pct >= 0 ? "+" : ""}${p.unreal_pct.toFixed(1)}%)</span>
        </div>
        <div class="position-pnl-bar"><div class="position-pnl-fill ${barCls}" style="width:${barW}%"></div></div>
      </td>
    </tr>`;
  }).join("");
}

function renderProfileInsights(positions, trades) {
  const panel = el("profile-insights");
  const rows  = [];

  // ── Position breakdown ──
  if (positions.length) {
    const enriched = positions.map(p => {
      const avg = p.avg_price || 0.5;
      return { ...p, unreal: (p.current_price - avg) * (p.size / avg) };
    });
    const yesPos = enriched.filter(p => p.side === "YES");
    const noPos  = enriched.filter(p => p.side === "NO");
    const yesExp = yesPos.reduce((s, p) => s + p.size, 0);
    const noExp  = noPos.reduce((s, p) => s + p.size, 0);
    const best   = [...enriched].sort((a,b) => b.unreal - a.unreal)[0];
    const worst  = [...enriched].sort((a,b) => a.unreal - b.unreal)[0];

    rows.push({ divider: "Positions" });
    rows.push({ icon: "YES", label: `YES exposure (${yesPos.length} pos)`, value: `$${fmtNum(yesExp)}`, cls: "pnl-pos" });
    rows.push({ icon: "NO",  label: `NO exposure (${noPos.length} pos)`,  value: `$${fmtNum(noExp)}`,  cls: "pnl-neg" });
    rows.push({
      icon: "↑", label: truncate(best.question, 32),
      value: `${best.unreal >= 0 ? "+" : "-"}$${fmtNum(Math.abs(best.unreal))}`,
      cls: best.unreal >= 0 ? "pnl-pos" : "pnl-neg", sub: "Best position",
    });
    if (enriched.length > 1) {
      rows.push({
        icon: "↓", label: truncate(worst.question, 32),
        value: `${worst.unreal >= 0 ? "+" : "-"}$${fmtNum(Math.abs(worst.unreal))}`,
        cls: worst.unreal >= 0 ? "pnl-pos" : "pnl-neg", sub: "Worst position",
      });
    }
  }

  // ── Trade breakdown ──
  if (trades.length) {
    const buys  = trades.filter(t => t.action === "BUY").length;
    const sells = trades.filter(t => t.action === "SELL").length;
    const yesTrades = trades.filter(t => t.side === "YES").length;
    const noTrades  = trades.filter(t => t.side === "NO").length;
    const buyPct = trades.length ? Math.round((buys / trades.length) * 100) : 0;
    const yesPct = trades.length ? Math.round((yesTrades / trades.length) * 100) : 0;

    // Most-traded market by frequency
    const mktCount = {};
    trades.forEach(t => { mktCount[t.question] = (mktCount[t.question] || 0) + 1; });
    const topMkt = Object.entries(mktCount).sort((a,b) => b[1]-a[1])[0];
    const uniqueMkts = Object.keys(mktCount).length;

    rows.push({ divider: "Trades" });
    rows.push({
      icon: "⚡", label: `BUY ${buys} / SELL ${sells}`,
      value: `${buyPct}% buys`, cls: "", bar: buyPct, barLabel: ["BUY","SELL"],
    });
    rows.push({
      icon: "⚖️", label: `YES ${yesTrades} / NO ${noTrades}`,
      value: `${yesPct}% YES`, cls: "", bar: yesPct, barLabel: ["YES","NO"],
    });
    rows.push({ icon: "🗂", label: "Unique markets", value: uniqueMkts, cls: "" });
    if (topMkt) {
      rows.push({ icon: "🔥", label: truncate(topMkt[0], 32), value: `${topMkt[1]}x`, cls: "pnl-pos", sub: "Most traded" });
    }
  }

  if (!rows.length) {
    panel.innerHTML = `<div class="feed-empty">No data — click ↺ Refresh</div>`;
    return;
  }

  panel.innerHTML = rows.map(r => {
    if (r.divider) return `<div class="insight-divider">${r.divider}</div>`;
    if (r.bar !== undefined) {
      return `<div class="insight-item" style="flex-direction:column;gap:5px">
        <div style="display:flex;justify-content:space-between">
          <span class="insight-label">${esc(r.icon)} ${esc(String(r.label))}</span>
          <span class="insight-value ${r.cls}">${r.value}</span>
        </div>
        <div class="bias-bar-wrap"><div class="bias-bar-yes" style="width:${r.bar}%"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:0.65rem;color:var(--text-muted)">
          <span>${r.barLabel[0]}</span><span>${r.barLabel[1]}</span>
        </div>
      </div>`;
    }
    return `<div class="insight-item">
      <span class="insight-label" style="flex-direction:column;align-items:flex-start;gap:1px">
        ${r.sub ? `<span style="font-size:0.65rem;color:var(--text-muted)">${esc(r.sub)}</span>` : ""}
        <span>${esc(r.icon)} ${esc(String(r.label))}</span>
      </span>
      <span class="insight-value ${r.cls}">${r.value}</span>
    </div>`;
  }).join("");
}

function renderProfileTrades(trades) {
  const countEl = el("trade-hist-count");
  if (countEl) countEl.textContent = `${trades.length} most recent`;

  const tbody = el("profile-trades-body");
  if (!trades.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-row">No on-chain trade history found</td></tr>`;
    return;
  }

  const sideCls = s => (s || "").toUpperCase() === "YES" ? "yes" : (s || "").toUpperCase() === "NO" ? "no" : "research";
  const actCls  = a => (a || "").toUpperCase() === "BUY"  ? "filled" : "failed";

  tbody.innerHTML = trades.map((t, i) => `
    <tr>
      <td style="color:var(--text-muted);font-family:'JetBrains Mono',monospace;font-size:0.75rem">${i + 1}</td>
      <td style="max-width:240px;font-size:0.8rem">${truncate(t.question, 55)}</td>
      <td><span class="badge badge-${sideCls(t.side)}">${esc(t.side || "?")}</span></td>
      <td><span class="badge badge-${actCls(t.action)}">${esc(t.action || "?")}</span></td>
      <td style="font-family:'JetBrains Mono',monospace">$${fmtNum(t.size)}</td>
      <td style="font-family:'JetBrains Mono',monospace">${(t.price * 100).toFixed(1)}¢</td>
      <td style="font-size:0.75rem;color:var(--text-muted)">${timeAgo(t.timestamp)}</td>
    </tr>`).join("");
}

function copyWallet() {
  if (!_cachedWallet) return;
  navigator.clipboard.writeText(_cachedWallet).then(() => {
    const btn = document.querySelector(".copy-btn");
    if (btn) { btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = "Copy"; }, 1500); }
  });
}

// ── AI Trading Assistant ──────────────────────────────────────────────────────

const AssistantUI = (() => {
  let _open = false;
  let _loading = false;
  let _sessionId = null;
  let _rateRemaining = 12;

  function _getSessionId() {
    if (!_sessionId) {
      _sessionId = localStorage.getItem("chat_session_id");
      if (!_sessionId) {
        _sessionId = `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
        localStorage.setItem("chat_session_id", _sessionId);
      }
    }
    return _sessionId;
  }

  function toggle() {
    _open = !_open;
    const panel = el("chat-panel");
    const fab   = el("chat-fab");
    panel.classList.toggle("open", _open);
    fab.classList.toggle("open", _open);
    panel.setAttribute("aria-hidden", String(!_open));
    if (_open) {
      el("chat-unread").style.display = "none";
      setTimeout(() => el("chat-input")?.focus(), 260);
    }
  }

  function _addMsg(role, text, isError = false) {
    const messages = el("chat-messages");

    // Remove welcome state on first real message
    const welcome = messages.querySelector(".chat-welcome");
    if (welcome) welcome.remove();

    // Hide chips after first send
    if (role === "user") el("chat-chips").style.display = "none";

    const now = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const div = document.createElement("div");
    div.className = `chat-msg chat-msg--${isError ? "error" : role}`;

    const bubbleContent = _formatBubble(text);
    div.innerHTML = `
      <div class="chat-bubble">${bubbleContent}</div>
      <span class="chat-msg-time">${now}</span>
    `;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
    return div;
  }

  function _formatBubble(text) {
    // Convert **bold**, *italic*, bullet lists, and code spans for readability
    let s = esc(text);
    // Bold
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    // Inline code
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Bullet lists: lines starting with - or •
    s = s.replace(/^[-•]\s+(.+)$/gm, "<li>$1</li>");
    if (s.includes("<li>")) s = s.replace(/(<li>.*<\/li>)/s, "<ul>$1</ul>");
    // Numbered lists
    s = s.replace(/^\d+\.\s+(.+)$/gm, "<li>$1</li>");
    // Paragraphs: double newlines
    s = s.split(/\n\n+/).map(p => p.trim()).filter(Boolean).map(p => `<p>${p}</p>`).join("");
    // Single newlines within paragraphs
    s = s.replace(/\n/g, "<br>");
    return s;
  }

  function _showTyping() {
    const messages = el("chat-messages");
    const div = document.createElement("div");
    div.className = "chat-typing";
    div.id = "chat-typing-indicator";
    div.innerHTML = `<div class="chat-typing-dots">
      <div class="chat-typing-dot"></div>
      <div class="chat-typing-dot"></div>
      <div class="chat-typing-dot"></div>
    </div>`;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function _hideTyping() {
    el("chat-typing-indicator")?.remove();
  }

  function _setLoading(isLoading) {
    _loading = isLoading;
    const btn  = el("chat-send-btn");
    const input = el("chat-input");
    if (btn)  btn.disabled  = isLoading;
    if (input) input.disabled = isLoading;
  }

  function _updateRateBar(remaining) {
    _rateRemaining = remaining;
    const bar = el("chat-rate-bar");
    const txt = el("chat-rate-text");
    if (!bar || !txt) return;
    if (remaining <= 12) {
      bar.style.display = "flex";
      txt.textContent = `${remaining} message${remaining !== 1 ? "s" : ""} remaining this minute`;
      bar.className = "chat-rate-bar" + (remaining <= 3 ? " critical" : remaining <= 6 ? " warn" : "");
    } else {
      bar.style.display = "none";
    }
  }

  function _setStatus(text) {
    const lbl = el("chat-status-label");
    if (lbl) lbl.textContent = text;
  }

  async function send() {
    if (_loading) return;
    const input = el("chat-input");
    const message = (input?.value || "").trim();
    if (!message) return;

    input.value = "";
    autoResize(input);

    _addMsg("user", message);
    _setLoading(true);
    _showTyping();
    _setStatus("Thinking...");

    try {
      const data = await post("/api/assistant/chat", {
        message,
        session_id: _getSessionId(),
      });
      _hideTyping();
      _addMsg("assistant", data.reply);
      _updateRateBar(data.rate_limit_remaining);
      _setStatus("Ask me anything about your trades");
    } catch (err) {
      _hideTyping();
      const msg = err.message || "Request failed";
      const isRateLimit = msg.toLowerCase().includes("rate limit");
      _addMsg(isRateLimit ? "assistant" : "error",
        isRateLimit
          ? "You've sent a lot of messages! I need a moment — please wait about a minute and try again."
          : `Sorry, I ran into an error: ${msg}`,
        !isRateLimit
      );
      _setStatus("Ask me anything about your trades");
      // Show unread badge on fab if panel is closed
      if (!_open) el("chat-unread").style.display = "flex";
    } finally {
      _setLoading(false);
    }
  }

  function sendQuick(text) {
    const input = el("chat-input");
    if (input) input.value = text;
    send();
  }

  function onKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function autoResize(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 120) + "px";
  }

  async function clearChat() {
    if (!confirm("Clear conversation history?")) return;
    try {
      await del(`/api/assistant/session/${_getSessionId()}`);
    } catch (_) {}
    // Reset session so next message starts fresh
    _sessionId = `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    localStorage.setItem("chat_session_id", _sessionId);

    const messages = el("chat-messages");
    messages.innerHTML = `
      <div class="chat-welcome">
        <div class="chat-welcome-icon">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="28" height="28"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8zm-1-5h2v2h-2zm0-8h2v6h-2z"/></svg>
        </div>
        <p class="chat-welcome-title">Trading Assistant</p>
        <p class="chat-welcome-sub">I have live context on your portfolio, trades, and AI-discovered opportunities. Ask me anything.</p>
      </div>`;
    el("chat-chips").style.display = "flex";
    el("chat-rate-bar").style.display = "none";
    _setStatus("Ask me anything about your trades");
  }

  // ── Page-level chat (full assistant page) ──────────────────────────────────

  let _pageLoading = false;

  function _addPageMsg(role, text, isError = false) {
    const messages = el("page-chat-messages");
    const welcome = messages.querySelector(".assistant-welcome");
    if (welcome) welcome.remove();
    if (role === "user") el("page-chat-chips").style.display = "none";

    const now = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const div = document.createElement("div");
    div.className = `chat-msg chat-msg--${isError ? "error" : role}`;
    div.innerHTML = `<div class="chat-bubble">${_formatBubble(text)}</div><span class="chat-msg-time">${now}</span>`;
    messages.appendChild(div);
    messages.scrollTop = messages.scrollHeight;
  }

  function _showPageTyping() {
    const m = el("page-chat-messages");
    const div = document.createElement("div");
    div.className = "chat-typing";
    div.id = "page-typing-indicator";
    div.innerHTML = `<div class="chat-typing-dots"><div class="chat-typing-dot"></div><div class="chat-typing-dot"></div><div class="chat-typing-dot"></div></div>`;
    m.appendChild(div);
    m.scrollTop = m.scrollHeight;
  }

  function _hidePageTyping() { el("page-typing-indicator")?.remove(); }

  function _setPageLoading(v) {
    _pageLoading = v;
    const btn = el("page-chat-send");
    const inp = el("page-chat-input");
    if (btn) btn.disabled = v;
    if (inp) inp.disabled = v;
  }

  function _updatePageRateBar(remaining) {
    const bar = el("page-chat-rate-bar");
    const txt = el("page-chat-rate-text");
    if (!bar || !txt) return;
    if (remaining <= 12) {
      bar.style.display = "flex";
      txt.textContent = `${remaining} message${remaining !== 1 ? "s" : ""} remaining this minute`;
      bar.className = "chat-rate-bar" + (remaining <= 3 ? " critical" : remaining <= 6 ? " warn" : "");
    } else {
      bar.style.display = "none";
    }
  }

  async function sendPage() {
    if (_pageLoading) return;
    const input = el("page-chat-input");
    const message = (input?.value || "").trim();
    if (!message) return;

    input.value = "";
    autoResizePage(input);
    _addPageMsg("user", message);
    _setPageLoading(true);
    _showPageTyping();
    const statusEl = el("page-chat-status");
    if (statusEl) statusEl.textContent = "Thinking...";

    try {
      const data = await post("/api/assistant/chat", { message, session_id: _getSessionId() });
      _hidePageTyping();
      _addPageMsg("assistant", data.reply);
      _updatePageRateBar(data.rate_limit_remaining);
      if (statusEl) statusEl.textContent = "Powered by OpenRouter · Context-aware";
    } catch (err) {
      _hidePageTyping();
      const msg = err.message || "Request failed";
      const isRateLimit = msg.toLowerCase().includes("rate limit");
      _addPageMsg(
        isRateLimit ? "assistant" : "error",
        isRateLimit ? "You've hit the rate limit. Wait a moment and try again." : `Error: ${msg}`,
        !isRateLimit
      );
      if (statusEl) statusEl.textContent = "Powered by OpenRouter · Context-aware";
    } finally {
      _setPageLoading(false);
    }
  }

  function sendPageQuick(text) {
    const input = el("page-chat-input");
    if (input) input.value = text;
    sendPage();
  }

  function onPageKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendPage(); }
  }

  function autoResizePage(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 140) + "px";
  }

  async function clearPageChat() {
    if (!confirm("Clear conversation history?")) return;
    try { await del(`/api/assistant/session/${_getSessionId()}`); } catch (_) {}
    _sessionId = `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    localStorage.setItem("chat_session_id", _sessionId);
    const m = el("page-chat-messages");
    m.innerHTML = `<div class="assistant-welcome">
      <div class="assistant-welcome-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" width="32" height="32"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8zm-1-5h2v2h-2zm0-8h2v6h-2z"/></svg></div>
      <h3 class="assistant-welcome-title">Ready to help with your trades</h3>
      <p class="assistant-welcome-sub">I have live access to your portfolio, open positions, recent trades, and AI-discovered opportunities. Ask me anything.</p>
    </div>`;
    el("page-chat-chips").style.display = "flex";
    el("page-chat-rate-bar").style.display = "none";
  }

  return {
    toggle, send, sendQuick, onKeyDown, autoResize, clearChat,
    sendPage, sendPageQuick, onPageKeyDown, autoResizePage, clearPageChat,
  };
})();

// ── Assistant Page Context Panel ──────────────────────────────────────────────

async function loadAssistantPage() {
  const panel = el("assistant-context-panel");
  if (!panel) return;
  panel.innerHTML = `<div class="feed-empty"><div class="loading-spinner" style="margin:8px auto"></div></div>`;

  try {
    const [profile, positions, trades, opps, status, journalData] = await Promise.allSettled([
      get("/api/me/profile"),
      get("/api/me/positions"),
      get("/api/me/trades?limit=6"),
      get("/api/opportunities?limit=20"),
      get("/api/status"),
      get("/api/journal?limit=5"),
    ]).then(results => results.map(r => r.status === "fulfilled" ? r.value : null));

    let html = "";

    // ── On-chain account ──
    if (profile && !profile._no_history) {
      const roiCls = (profile.roi || 0) >= 0 ? "pnl-pos" : "pnl-neg";
      const profitCls = (profile.profit || 0) >= 0 ? "pnl-pos" : "pnl-neg";
      html += `<div class="context-section">
        <div class="context-section-title">Polymarket Account</div>
        <div class="context-row"><span class="context-row-label">ROI</span><span class="context-row-value ${roiCls}">${(profile.roi || 0).toFixed(1)}%</span></div>
        <div class="context-row"><span class="context-row-label">Realized Profit</span><span class="context-row-value ${profitCls}">$${fmtNum(Math.abs(profile.profit || 0))}</span></div>
        <div class="context-row"><span class="context-row-label">Win Rate</span><span class="context-row-value">${(profile.win_rate || 0).toFixed(1)}%</span></div>
        <div class="context-row"><span class="context-row-label">Volume</span><span class="context-row-value">$${fmtNum(profile.volume || 0)}</span></div>
        <div class="context-row"><span class="context-row-label">Open Positions</span><span class="context-row-value">${profile.positions_count || 0}</span></div>
      </div>`;
    } else {
      html += `<div class="context-section">
        <div class="context-section-title">Polymarket Account</div>
        <div style="font-size:0.75rem;color:var(--text-muted);padding:4px 0">${profile?._no_history ? "No trading history yet" : "Not configured — set POLY_PRIVATE_KEY"}</div>
      </div>`;
    }

    // ── Open positions ──
    if (positions && positions.length) {
      html += `<div class="context-section"><div class="context-section-title">Open Positions (${positions.length})</div>`;
      positions.slice(0, 5).forEach(p => {
        const avg = p.avg_price || 0.5;
        const cur = p.current_price || 0.5;
        const unrealized = (cur - avg) / avg * (p.size || 0);
        const pnlCls = unrealized >= 0 ? "pnl-pos" : "pnl-neg";
        html += `<div class="context-opp">
          <div class="context-opp-q">${truncate(p.question, 50)}</div>
          <div class="context-opp-meta">
            <span class="badge badge-${p.side === 'YES' ? 'yes' : 'no'}">${esc(p.side || "?")}</span>
            <span class="badge badge-research">${(avg * 100).toFixed(0)}¢ → ${(cur * 100).toFixed(0)}¢</span>
            <span class="${pnlCls}" style="font-size:0.7rem;font-family:'JetBrains Mono',monospace">${unrealized >= 0 ? "+" : ""}$${fmtNum(Math.abs(unrealized))}</span>
          </div>
        </div>`;
      });
      html += `</div>`;
    }

    // ── Recent on-chain trades ──
    if (trades && trades.length) {
      html += `<div class="context-section"><div class="context-section-title">Recent Trades</div>`;
      trades.slice(0, 5).forEach(t => {
        html += `<div class="context-row" style="align-items:flex-start;gap:6px">
          <span class="badge badge-${t.side === 'YES' ? 'yes' : 'no'}" style="flex-shrink:0">${esc(t.side || "?")}</span>
          <span class="context-row-label" style="flex:1;line-height:1.35">${truncate(t.question, 40)}</span>
          <span class="context-row-value" style="flex-shrink:0">$${fmtNum(t.size || 0)}</span>
        </div>`;
      });
      html += `</div>`;
    }

    // ── Bot status ──
    if (status) {
      html += `<div class="context-section">
        <div class="context-section-title">Bot Status</div>
        <div class="context-row"><span class="context-row-label">Mode</span><span class="context-row-value">${(status.mode || "paper").toUpperCase()}</span></div>
        <div class="context-row"><span class="context-row-label">Scan model</span><span class="context-row-value">${status.ai_model || "—"}</span></div>
        <div class="context-row"><span class="context-row-label">Running</span><span class="context-row-value">${status.running ? "Yes" : "No"}</span></div>
      </div>`;
    }

    // ── Top AI opportunities ──
    const highConf = (opps || []).filter(o => o.confidence === "high").slice(0, 3);
    if (highConf.length) {
      html += `<div class="context-section"><div class="context-section-title">Top AI Opportunities</div>`;
      highConf.forEach(o => {
        html += `<div class="context-opp">
          <div class="context-opp-q">${truncate(o.question, 50)}</div>
          <div class="context-opp-meta">
            <span class="badge badge-${o.side === 'YES' ? 'yes' : 'no'}">${esc(o.side)}</span>
            <span class="badge badge-research">${(o.edge_pct || 0).toFixed(1)}% edge</span>
          </div>
        </div>`;
      });
      html += `</div>`;
    }

    // ── Journal entries ──
    const journalEntries = Array.isArray(journalData) ? journalData : [];
    if (journalEntries.length) {
      html += `<div class="context-section"><div class="context-section-title">Journal (${journalEntries.length} recent)</div>`;
      journalEntries.forEach(j => {
        const stars = j.rating ? '★'.repeat(j.rating) + '☆'.repeat(5 - j.rating) : '';
        html += `<div class="context-opp">
          <div class="context-opp-q">${truncate(j.question, 50)}</div>
          <div class="context-opp-meta">
            <span class="badge badge-${j.side === 'YES' ? 'yes' : 'no'}">${esc(j.side || '?')}</span>
            ${j.edge_pct ? `<span class="badge badge-research">${j.edge_pct.toFixed(1)}% edge</span>` : ''}
            ${stars ? `<span style="font-size:0.72rem;color:var(--yellow)">${stars}</span>` : ''}
          </div>
          ${j.notes ? `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px;font-style:italic">"${j.notes.slice(0,80)}${j.notes.length>80?'…':''}"</div>` : ''}
        </div>`;
      });
      html += `</div>`;
    }

    html += `<div class="context-section">
      <div style="font-size:0.72rem;color:var(--text-muted);line-height:1.6">
        Press <kbd style="background:var(--surface);border:1px solid var(--border);padding:1px 5px;border-radius:4px;font-size:0.67rem">Enter</kbd> to send &nbsp;·&nbsp;
        <kbd style="background:var(--surface);border:1px solid var(--border);padding:1px 5px;border-radius:4px;font-size:0.67rem">Shift+Enter</kbd> for new line
      </div>
    </div>`;

    panel.innerHTML = html;
  } catch (err) {
    panel.innerHTML = `<div class="feed-empty" style="color:var(--text-muted)">Context unavailable</div>`;
  }
}

// ── Trade Journal ─────────────────────────────────────────────────────────────

let _journalCache = [];
let _journalDraft = {};
let _journalEditId = null;
let _journalRating = 0;
let _journalFilterType = "";

async function loadJournalPage() {
  const list = el("journal-list");
  const count = el("journal-count");
  if (!list) return;

  try {
    const url = _journalFilterType ? `/api/journal?limit=200&entry_type=${_journalFilterType}` : "/api/journal?limit=200";
    _journalCache = await get(url);

    if (count) count.textContent = `${_journalCache.length} entr${_journalCache.length === 1 ? 'y' : 'ies'}`;

    if (!_journalCache.length) {
      list.innerHTML = `<div class="feed-empty" style="padding:40px 0;text-align:center">
        <div style="font-size:2rem;margin-bottom:12px">📓</div>
        <div style="color:var(--text-sub);font-size:0.9rem;margin-bottom:6px">Your journal is empty</div>
        <div style="color:var(--text-muted);font-size:0.8rem">Add entries from Best Picks, Opportunities, or use "+ Add Entry"</div>
      </div>`;
      return;
    }

    list.innerHTML = _journalCache.map(j => _renderJournalCard(j)).join("");
  } catch (err) {
    if (list) list.innerHTML = `<div class="feed-empty">Failed to load journal: ${err.message}</div>`;
  }
}

function _renderJournalCard(j) {
  const typeLabels = { opportunity: "Opportunity", trade: "Trade", copy_signal: "Copy Signal", manual: "Manual" };
  const typeLabel = typeLabels[j.entry_type] || j.entry_type || "Entry";
  const date = j.created_at ? new Date(j.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "—";
  const stars = j.rating ? '★'.repeat(j.rating) + '☆'.repeat(5 - j.rating) : '';
  const tags = (j.tags || []).map(t => `<span class="journal-tag">${esc(t)}</span>`).join("");

  return `<div class="journal-card" data-id="${j.id}" data-type="${esc(j.entry_type)}">
    <div class="journal-card-header">
      <div class="journal-card-question">${esc(j.question || "—")}</div>
      <div class="journal-card-date">${date}</div>
    </div>
    <div class="journal-card-meta">
      <span class="badge badge-${j.side === 'YES' ? 'yes' : 'no'}">${esc(j.side || '?')}</span>
      <span class="journal-type-label">${esc(typeLabel)}</span>
      ${j.edge_pct ? `<span class="badge badge-research">${j.edge_pct.toFixed(1)}% edge</span>` : ''}
      ${j.confidence ? `<span class="badge badge-${j.confidence}">${j.confidence}</span>` : ''}
    </div>
    ${j.notes ? `<div class="journal-notes">${esc(j.notes)}</div>` : ''}
    ${tags ? `<div class="journal-tags">${tags}</div>` : ''}
    <div class="journal-card-footer">
      ${stars ? `<span class="journal-stars-display" style="color:var(--yellow)">${stars}</span>` : '<span></span>'}
      <div class="journal-actions">
        <button class="journal-action-btn" onclick="App.editJournalEntry(${j.id})">✏️ Edit</button>
        <button class="journal-action-btn delete" onclick="App.deleteJournalEntry(${j.id})">🗑️ Delete</button>
      </div>
    </div>
  </div>`;
}

function openJournalModal(entry_type, source_id, question, side, price, edge_pct, confidence, market_id) {
  const isManual = entry_type === 'manual' && !question;
  _journalDraft = { entry_type, source_id: source_id || null, market_id: market_id || "", question, side, price, edge_pct, confidence };
  _journalEditId = null;
  _journalRating = 0;

  const overlay = el("journal-modal-overlay");
  if (!overlay) return;

  el("journal-modal-title").textContent = "Add to Journal";
  el("journal-notes").value = "";
  el("journal-tags").value = "";
  _updateJournalStars(0);

  const marketDisplay = el("journal-modal-market");
  const manualFields = el("journal-manual-market");
  const manualSideGroup = el("journal-manual-side-group");

  if (isManual) {
    marketDisplay.style.display = "none";
    manualFields.style.display = "block";
    manualSideGroup.style.display = "block";
    el("journal-manual-question").value = "";
    _setJournalSide("YES");
    _journalDraft.side = "YES";
  } else {
    manualFields.style.display = "none";
    manualSideGroup.style.display = "none";
    marketDisplay.style.display = "block";
    const q = question ? question.slice(0, 120) + (question.length > 120 ? '…' : '') : '';
    marketDisplay.innerHTML = `<strong>${esc(side || '?')}</strong> — ${esc(q)}${edge_pct ? ` &nbsp;<span style="color:var(--primary)">+${Number(edge_pct).toFixed(1)}% edge</span>` : ''}`;
  }

  overlay.style.display = "flex";
}

function openJournalFromCard(btn) {
  const d = btn.dataset;
  openJournalModal(
    d.etype,
    parseInt(d.sid) || 0,
    d.q,
    d.side,
    parseFloat(d.price) || 0.5,
    parseFloat(d.edge) || 0,
    d.conf || "",
    d.mid || ""
  );
}

function _setJournalSide(s) {
  _journalDraft.side = s;
  const yesBtn = el("journal-side-yes");
  const noBtn = el("journal-side-no");
  if (yesBtn) { yesBtn.classList.toggle("btn-primary", s === "YES"); yesBtn.classList.toggle("btn-outline", s !== "YES"); }
  if (noBtn)  { noBtn.classList.toggle("btn-primary", s === "NO");  noBtn.classList.toggle("btn-outline", s !== "NO"); }
}

function _setJournalRating(val) {
  _journalRating = val;
  _updateJournalStars(val);
}

function _updateJournalStars(val) {
  const stars = document.querySelectorAll("#journal-rating-stars .star");
  stars.forEach((s, i) => {
    const filled = i < val;
    s.textContent = filled ? '★' : '☆';
    s.classList.toggle("filled", filled);
  });
}

function closeJournalModal() {
  const overlay = el("journal-modal-overlay");
  if (overlay) overlay.style.display = "none";
  _journalEditId = null;
  _journalDraft = {};
  _journalRating = 0;
}

async function saveJournal() {
  const notes = (el("journal-notes")?.value || "").trim();
  const tagsRaw = (el("journal-tags")?.value || "").trim();
  const tags = tagsRaw ? tagsRaw.split(",").map(t => t.trim()).filter(Boolean) : [];

  // For manual entries, grab question/side from input fields
  if (el("journal-manual-market")?.style.display !== "none") {
    _journalDraft.question = (el("journal-manual-question")?.value || "").trim();
    _journalDraft.market_id = _journalDraft.question.slice(0, 40).toLowerCase().replace(/\s+/g, "-");
    if (!_journalDraft.question) { showToast("Please enter a market question", "error"); return; }
  }

  const payload = {
    ..._journalDraft,
    notes,
    tags,
    rating: _journalRating || null,
  };

  try {
    if (_journalEditId) {
      await put(`/api/journal/${_journalEditId}`, { notes, tags, rating: _journalRating || null });
      showToast("Journal entry updated ✓", "success");
    } else {
      await post("/api/journal", payload);
      showToast("Saved to Journal ✓", "success");
    }
    closeJournalModal();
    if (currentPage() === "journal") loadJournalPage();
  } catch (err) {
    showToast("Failed to save: " + err.message, "error");
  }
}

function editJournalEntry(id) {
  const entry = _journalCache.find(j => j.id === id);
  if (!entry) return;

  _journalEditId = id;
  _journalDraft = { ...entry };
  _journalRating = entry.rating || 0;

  const overlay = el("journal-modal-overlay");
  if (!overlay) return;

  el("journal-modal-title").textContent = "Edit Journal Entry";

  const marketDisplay = el("journal-modal-market");
  if (marketDisplay) {
    marketDisplay.style.display = "block";
    const q = (entry.question || "").slice(0, 120);
    marketDisplay.innerHTML = `<strong>${esc(entry.side || '?')}</strong> — ${esc(q)}`;
  }
  const manualFields = el("journal-manual-market");
  const manualSideGroup = el("journal-manual-side-group");
  if (manualFields) manualFields.style.display = "none";
  if (manualSideGroup) manualSideGroup.style.display = "none";

  el("journal-notes").value = entry.notes || "";
  el("journal-tags").value = (entry.tags || []).join(", ");
  _updateJournalStars(_journalRating);

  overlay.style.display = "flex";
}

async function deleteJournalEntry(id) {
  if (!confirm("Remove this journal entry?")) return;
  try {
    await del(`/api/journal/${id}`);
    const card = document.querySelector(`.journal-card[data-id="${id}"]`);
    if (card) {
      card.style.transition = "opacity 0.25s, transform 0.25s";
      card.style.opacity = "0";
      card.style.transform = "translateX(-10px)";
      setTimeout(() => card.remove(), 280);
    }
    _journalCache = _journalCache.filter(j => j.id !== id);
    const count = el("journal-count");
    if (count) count.textContent = `${_journalCache.length} entr${_journalCache.length === 1 ? 'y' : 'ies'}`;
    showToast("Journal entry deleted", "info");
  } catch (err) {
    showToast("Failed to delete: " + err.message, "error");
  }
}

function filterJournal(btn, type) {
  _journalFilterType = type;
  document.querySelectorAll(".journal-filter-pill").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  loadJournalPage();
}

// ── Consensus Trades ──────────────────────────────────────────────────────────

let _consensusData = [];

async function loadConsensus(silent = false) {
  const list = el("consensus-list");
  const minTraders = parseInt(el("consensus-min-traders")?.value || "3", 10);
  if (list && !silent) list.innerHTML = `<div class="approvals-empty"><div class="loading-spinner" style="margin:16px auto"></div><p style="color:var(--text-muted);margin-top:8px">Fetching positions from top traders...</p></div>`;

  try {
    const data = await get(`/api/consensus-trades?min_traders=${minTraders}&leaderboard_n=20`);
    _consensusData = data.consensus || [];

    // Stats bar
    const maxTraders = _consensusData.length ? Math.max(..._consensusData.map(c => c.trader_count)) : 0;
    const cacheAge = data.cache_age_seconds;
    if (el("consensus-total")) el("consensus-total").textContent = data.total ?? 0;
    if (el("consensus-pool")) el("consensus-pool").textContent = data.trader_pool_size ?? "—";
    if (el("consensus-max-traders")) el("consensus-max-traders").textContent = maxTraders || "—";
    if (el("consensus-cache-age")) el("consensus-cache-age").textContent = cacheAge != null ? `${Math.round(cacheAge / 60)}m ago` : "fresh";
    if (el("consensus-subtitle")) el("consensus-subtitle").textContent = `${_consensusData.length} market${_consensusData.length !== 1 ? "s" : ""} · ${data.trader_pool_size ?? 0} traders checked`;

    if (!list) return;
    if (!_consensusData.length) {
      list.innerHTML = `<div class="approvals-empty">
        <p>No consensus found with ${minTraders}+ traders on the same side.</p>
        <p style="font-size:0.75rem;color:var(--text-muted);margin-top:0.5rem">Try lowering the minimum traders threshold, or click "Force Reload Positions" to bypass the cache.</p>
      </div>`;
      return;
    }

    list.innerHTML = _consensusData.map((c, i) => renderConsensusCard(c, i)).join("");
  } catch (err) {
    if (list) list.innerHTML = `<div class="approvals-empty" style="color:var(--red)">Failed to load consensus: ${err.message}</div>`;
  }
}

function renderConsensusCard(c, idx) {
  const sideCls = c.side === "YES" ? "yes" : "no";
  const price = c.avg_price || 0.5;
  const pricePct = (price * 100).toFixed(1);
  const safeMarketId = esc(c.market_id);

  const traderRows = (c.traders || []).map(t =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:0.78rem">
      <span style="color:var(--text)">${esc(t.name)}</span>
      <span style="display:flex;gap:10px;color:var(--text-muted)">
        <span>$${fmtNum(t.size)}</span>
        <span style="font-family:'JetBrains Mono',monospace">${(t.avg_price * 100).toFixed(1)}¢</span>
      </span>
    </div>`
  ).join("");

  return `<div class="pending-trade-card" id="consensus-card-${idx}" style="border-left:3px solid var(--${sideCls === 'yes' ? 'green' : 'red'})">
    <div class="pending-meta" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span class="badge" style="background:var(--primary);color:#fff;font-size:0.7rem;padding:2px 8px">🤝 ${c.trader_count} traders agree</span>
      <span class="badge badge-${sideCls}">${c.side}</span>
      <span style="font-size:0.75rem;color:var(--text-muted)">avg ${pricePct}¢ · total $${fmtNum(c.total_size)}</span>
    </div>

    <div class="pending-question" style="margin:8px 0">${esc(c.question || c.market_id)}</div>

    <details style="margin-bottom:10px">
      <summary style="font-size:0.78rem;color:var(--text-muted);cursor:pointer;user-select:none">
        ${c.trader_count} trader${c.trader_count !== 1 ? "s" : ""} — click to expand
      </summary>
      <div style="margin-top:6px;padding:0 4px">
        ${traderRows}
      </div>
    </details>

    <div class="pending-actions">
      <button class="btn btn-success btn-sm" onclick="App.approveConsensusTrade(${idx})">Approve</button>
      <button class="btn btn-outline btn-sm" onclick="App.dismissConsensusTrade(${idx})">Dismiss</button>
      <button class="btn-journal"
        data-etype="opportunity"
        data-sid="0"
        data-q="${escAttr(c.question || c.market_id)}"
        data-side="${escAttr(c.side)}"
        data-price="${price}"
        data-edge="0"
        data-conf=""
        data-mid="${escAttr(c.market_id)}"
        onclick="App.openJournalFromCard(this)">📓 Journal</button>
    </div>

    <div class="outcome-feedback" id="outcome-consensus-${safeMarketId}">
      <span class="outcome-label">Rate this pick:</span>
      <button class="btn-outcome btn-outcome-success" onclick="App.markOutcomeByMarket('${safeMarketId}', 'success', this, '${escAttr(c.question || "")}')">✓ Win</button>
      <button class="btn-outcome btn-outcome-failure" onclick="App.markOutcomeByMarket('${safeMarketId}', 'failure', this, '${escAttr(c.question || "")}')">✗ Loss</button>
    </div>
  </div>`;
}

async function approveConsensusTrade(idx) {
  const c = _consensusData[idx];
  if (!c) return;

  const sizeStr = prompt(`Enter trade size in USD for ${c.side} on:\n"${c.question.slice(0, 80)}"\n\nAvg trader price: ${(c.avg_price * 100).toFixed(1)}¢`, "20");
  if (!sizeStr) return;
  const size = parseFloat(sizeStr);
  if (isNaN(size) || size <= 0) { alert("Invalid size."); return; }

  try {
    const trade = await post(`/api/consensus-trades/${encodeURIComponent(c.market_id)}/approve`, {
      side: c.side,
      question: c.question,
      size_usd: size,
      price: c.avg_price,
    });

    if (trade.status === "filled" || trade.status === "pending") {
      addFeedItem(`Consensus: ${c.side} $${size} — ${truncate(c.question, 45)}`, "trade", new Date().toLocaleTimeString());
      showToast(`Trade approved: ${c.side} $${size}`, "success");
      const card = el(`consensus-card-${idx}`);
      if (card) {
        card.style.transition = "opacity 0.25s, transform 0.25s";
        card.style.opacity = "0";
        card.style.transform = "translateX(-10px)";
        setTimeout(() => card.remove(), 280);
      }
      // Suppress from server-side cache so it doesn't reappear on next refresh
      try { await post(`/api/consensus-trades/${encodeURIComponent(c.market_id)}/dismiss`, { side: c.side }); } catch (_) {}
      _consensusData[idx] = null;
    } else {
      alert("Trade failed: " + (trade.error || "Unknown error"));
    }
  } catch (err) {
    alert("Error: " + err.message);
  }
}

async function dismissConsensusTrade(idx) {
  const c = _consensusData[idx];
  if (!c) return;
  try {
    await post(`/api/consensus-trades/${encodeURIComponent(c.market_id)}/dismiss`, { side: c.side });
  } catch (_) {}
  const card = el(`consensus-card-${idx}`);
  if (card) {
    card.style.transition = "opacity 0.25s, transform 0.25s";
    card.style.opacity = "0";
    card.style.transform = "translateX(-10px)";
    setTimeout(() => card.remove(), 280);
  }
  _consensusData[idx] = null;
}

async function refreshConsensusCache() {
  try {
    await post("/api/consensus-trades/refresh", {});
    showToast("Position cache cleared — next refresh fetches live data", "info");
    await loadConsensus();
  } catch (err) {
    showToast("Failed to refresh cache: " + err.message, "error");
  }
}

// ── Public API (used by inline onclick handlers) ────────────────────────────

const App = {
  startBot, stopBot, runScan, setMode, setModel,
  refreshTopTraders, loadLeaderboard, showAddTraderModal, closeModal, addTrader, removeTrader, followTrader,
  loadSignals, showSignalDetails, closeSignalDetails, rateSignal,
  loadPendingApprovals, dismissApproval, approveTradeDirect,
  loadFutureTrades, approveFutureTrade,
  loadTrades, fundAccount, approveTrade, dismissTrade,
  tradeOpportunity, dismissOpportunity,
  loadCleanupStats, runCleanup,
  loadProfile, copyWallet,
  markOutcome, markOutcomeByMarket, markSignalOutcome, loadRankings,
  loadDashboardPendingTrades,
  openJournalModal, openJournalFromCard, closeJournalModal, saveJournal,
  editJournalEntry, deleteJournalEntry, filterJournal,
  _setJournalSide, _setJournalRating,
  loadConsensus, approveConsensusTrade, dismissConsensusTrade, refreshConsensusCache,
};

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  connectWS();
  navigate("dashboard");

  // Auto-refresh every 30s
  setInterval(() => {
    const page = currentPage();
    if (page === "dashboard") loadDashboard();
    else if (page === "copy") { loadLeaderboard(); loadCopyTraders(); loadSignals(); }
    else if (page === "opportunities") loadOpportunities();
    else if (page === "approvals") loadPendingApprovals();
    else if (page === "future") loadFutureTrades();
    else if (page === "rankings") loadRankings();
    else if (page === "trades") loadTrades(el("trade-source-filter")?.value || "");
    else if (page === "profile") loadProfile();
    else if (page === "consensus") loadConsensus(true);
    else if (page === "journal") loadJournalPage();
  }, 30000);
});
