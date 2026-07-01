const board = document.querySelector("#board");
const dailyBoard = document.querySelector("#daily-board");
const boardMeta = document.querySelector("#board-meta");
const statusCopy = document.querySelector("#status-copy");
const statusStrip = document.querySelector("#status-strip");
const connectionState = document.querySelector("#connection-state");
const liveFreshness = document.querySelector("#live-freshness");
const feedModeLabel = document.querySelector("#feed-mode");
const refreshButton = document.querySelector("#refresh-button");
const viewButtons = Array.from(document.querySelectorAll(".view-tabs button"));
const dailyView = document.querySelector("#daily-view");
const marketsView = document.querySelector("#markets-view");
const marketSearch = document.querySelector("#market-search");
const marketFilterClear = document.querySelector("#market-filter-clear");
const marketLayoutToggle = document.querySelector("#market-layout-toggle");
const marketFilterStatus = document.querySelector("#market-filter-status");
const modal = document.querySelector("#chart-modal");
const modalShell = document.querySelector("#chart-modal .modal-shell");
const modalClose = document.querySelector("#modal-close");
const chartTitle = document.querySelector("#chart-title");
const chartSubtitle = document.querySelector("#chart-subtitle");
const chartElement = document.querySelector("#chart");
const chartError = document.querySelector("#chart-error");
const profileElement = document.querySelector("#asset-profile");
const intervalButtons = Array.from(document.querySelectorAll(".intervals button"));
const editorModal = document.querySelector("#editor-modal");
const editorOpen = document.querySelector("#editor-open");
const editorClose = document.querySelector("#editor-close");
const editorStatus = document.querySelector("#editor-status");
const groupForm = document.querySelector("#group-form");
const groupNameInput = document.querySelector("#group-name");
const assetForm = document.querySelector("#asset-form");
const assetGroupSelect = document.querySelector("#asset-group");
const assetSymbolInput = document.querySelector("#asset-symbol");
const assetTypeSelect = document.querySelector("#asset-type");
const assetSourceSelect = document.querySelector("#asset-source");
const assetExchangeInput = document.querySelector("#asset-exchange");
const assetNameInput = document.querySelector("#asset-name");
const editorList = document.querySelector("#editor-list");

let latestData = null;
let latestCryptoEtfFlows = null;
let watchlistConfig = null;
let activeSymbol = null;
let activeAsset = null;
let activeHistoryContext = null;
let activeRange = "ytd";
let activeInterval = "1d";
let activeDialog = null;
let lastFocusedElement = null;
let chart = null;
let chartLoadToken = 0;
let chartContextLoading = false;
let chartResizeObserver = null;
let chartResizeFrame = null;
let marketSearchQuery = "";
let activeGroupFilter = "";
let marketSort = { key: "configured", direction: "default" };
let marketLayout = "grouped"; // "grouped" | "flat"
let feedMode = "poll"; // "ws" locally, "poll" on serverless deployments
let activeView = "daily";
let pendingChartFromUrl = null;
let restoringUrlState = false;

const sourceLabels = {
  yahoo: "YH",
  hyperliquid: "HL",
  stooq: "STQ",
  finnhub: "FH",
};

// --- Market session awareness -------------------------------------------
// Client-side session clock per exchange. Timezones handled via Intl, so
// DST is correct without a tz table. Crypto perps trade 24/7.
const EXCHANGE_SESSIONS = {
  NASDAQ: "us",
  NYSE: "us",
  NYSEARCA: "us",
  BATS: "us",
  CBOE: "us",
  KRX: "krx",
};

const SESSION_DEFS = {
  us: {
    label: "US",
    timeZone: "America/New_York",
    days: [1, 2, 3, 4, 5],
    open: 9 * 60 + 30,
    close: 16 * 60,
    pre: 4 * 60,
    post: 20 * 60,
  },
  krx: {
    label: "KRX",
    timeZone: "Asia/Seoul",
    days: [1, 2, 3, 4, 5],
    open: 9 * 60,
    close: 15 * 60 + 30,
  },
};

const WEEKDAY_INDEX = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

function zonedNow(timeZone) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  const get = (type) => parts.find((part) => part.type === type)?.value || "";
  return {
    day: WEEKDAY_INDEX[get("weekday")] ?? 0,
    minutes: (Number(get("hour")) % 24) * 60 + Number(get("minute")),
  };
}

function sessionState(sessionKey) {
  const def = SESSION_DEFS[sessionKey];
  if (!def) return null;
  const now = zonedNow(def.timeZone);
  if (!def.days.includes(now.day)) return { key: sessionKey, label: def.label, state: "closed" };
  if (now.minutes >= def.open && now.minutes < def.close) {
    return { key: sessionKey, label: def.label, state: "open" };
  }
  if (typeof def.pre === "number" && now.minutes >= def.pre && now.minutes < def.open) {
    return { key: sessionKey, label: def.label, state: "pre" };
  }
  if (typeof def.post === "number" && now.minutes >= def.close && now.minutes < def.post) {
    return { key: sessionKey, label: def.label, state: "post" };
  }
  return { key: sessionKey, label: def.label, state: "closed" };
}

function assetSessionKey(asset) {
  if (isCryptoAsset(asset.type)) return "crypto";
  return EXCHANGE_SESSIONS[String(asset.exchange || "").toUpperCase()] || "us";
}

const SESSION_STATE_COPY = {
  open: "Open",
  pre: "Pre",
  post: "Post",
  closed: "Closed",
};

function groupSessionChip(assets) {
  const keys = [...new Set((assets || []).map(assetSessionKey))];
  if (!keys.length) return null;
  if (keys.every((key) => key === "crypto")) return { text: "24/7", state: "open", title: "Crypto trades around the clock" };
  const states = keys
    .filter((key) => key !== "crypto")
    .map(sessionState)
    .filter(Boolean);
  if (!states.length) return null;
  const hasCrypto = keys.includes("crypto");
  const parts = states.map((item) => `${item.label} ${SESSION_STATE_COPY[item.state]}`);
  if (hasCrypto) parts.push("Crypto 24/7");
  const anyOpen = states.some((item) => item.state === "open") || hasCrypto;
  const anyEdge = states.some((item) => item.state === "pre" || item.state === "post");
  return {
    text: parts.join(" · "),
    state: anyOpen ? "open" : anyEdge ? "edge" : "closed",
    title: parts.join(", "),
  };
}

function quoteAge(quote) {
  const stamp = Date.parse(quote?.timestamp || "");
  if (Number.isNaN(stamp)) return null;
  const seconds = Math.max(0, (Date.now() - stamp) / 1000);
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

init();

// --- URL state ------------------------------------------------------------
// View, filters, and open chart mirror into the hash so any board state is
// bookmarkable/shareable. replaceState keeps history clean.
function syncUrlState() {
  if (restoringUrlState) return;
  const params = new URLSearchParams();
  if (activeView !== "daily") params.set("view", activeView);
  if (activeGroupFilter) params.set("group", activeGroupFilter);
  if (marketSearchQuery) params.set("q", marketSearchQuery);
  if (marketLayout === "flat") params.set("layout", "flat");
  if (activeSymbol) {
    params.set("chart", activeSymbol);
    if (activeRange !== "ytd") params.set("range", activeRange);
  }
  const hash = params.toString();
  const next = hash ? `#${hash}` : window.location.pathname + window.location.search;
  if (`#${hash}` === window.location.hash || (!hash && !window.location.hash)) return;
  history.replaceState(null, "", hash ? `#${hash}` : next);
}

function restoreUrlState() {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return;
  const params = new URLSearchParams(raw);
  restoringUrlState = true;
  try {
    const view = params.get("view");
    if (view === "markets") selectView("markets");
    const group = params.get("group");
    if (group) activeGroupFilter = group;
    const query = params.get("q");
    if (query) {
      marketSearchQuery = query;
      marketSearch.value = query;
    }
    if (params.get("layout") === "flat") {
      marketLayout = "flat";
      marketSort = { key: "pct", direction: "desc" };
      marketLayoutToggle.setAttribute("aria-pressed", "true");
      marketLayoutToggle.textContent = "Grouped";
      marketLayoutToggle.title = "Back to sector groups";
    }
    const chartSymbol = params.get("chart");
    if (chartSymbol) {
      pendingChartFromUrl = {
        symbol: chartSymbol.toUpperCase(),
        range: params.get("range") || "ytd",
      };
    }
  } finally {
    restoringUrlState = false;
  }
}

function findAssetConfig(symbol) {
  if (!symbol || !latestData?.groups) return null;
  for (const group of latestData.groups) {
    const asset = (group.assets || []).find((item) => item.symbol === symbol);
    if (asset) return asset;
  }
  return null;
}

function openPendingChartFromUrl() {
  if (!pendingChartFromUrl || !latestData) return;
  const { symbol, range } = pendingChartFromUrl;
  const asset = findAssetConfig(symbol);
  pendingChartFromUrl = null;
  if (asset) openChart(asset, { range });
}

function init() {
  window.lucide?.createIcons();
  setConnection("connecting");
  restoreUrlState();
  fetchQuotes();
  fetchCryptoEtfFlows();
  feedMode = shouldUseWebSocket() ? "ws" : "poll";
  updateFeedModeLabel();
  if (feedMode === "ws") openSocket();
  // Poll only while the tab is visible; a hidden tab otherwise burns
  // ~5.7k serverless invocations/day for nothing.
  window.setInterval(() => {
    if (!document.hidden) fetchQuotes();
  }, 15000);
  window.setInterval(() => {
    if (!document.hidden) fetchCryptoEtfFlows();
  }, 300000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      fetchQuotes();
      fetchCryptoEtfFlows();
    }
  });
  refreshButton.addEventListener("click", () => {
    fetchQuotes();
    fetchCryptoEtfFlows();
  });
  viewButtons.forEach((button) => {
    button.addEventListener("click", () => selectView(button.dataset.view || "daily"));
    button.addEventListener("keydown", handleViewTabKeydown);
  });
  marketSearch.addEventListener("input", () => {
    marketSearchQuery = marketSearch.value.trim();
    renderBoard(latestData);
    syncUrlState();
  });
  marketSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      focusFirstMarketRow();
    }
  });
  marketFilterClear.addEventListener("click", clearMarketFilters);
  marketLayoutToggle.addEventListener("click", toggleMarketLayout);
  modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  setupChartResizeObserver();
  editorOpen.addEventListener("click", openEditor);
  editorClose.addEventListener("click", closeEditor);
  editorModal.addEventListener("click", (event) => {
    if (event.target === editorModal) closeEditor();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && activeDialog) {
      trapDialogFocus(event, activeDialog);
      return;
    }
    if (event.key === "/" && !activeDialog && !isTextInput(event.target)) {
      event.preventDefault();
      selectView("markets");
      marketSearch.focus();
      return;
    }
    if (event.key === "Escape") {
      closeModal();
      closeEditor();
      return;
    }
    if (
      (event.key === "j" || event.key === "k") &&
      !activeDialog &&
      !isTextInput(event.target) &&
      !marketsView.hidden
    ) {
      event.preventDefault();
      moveMarketRowFocus(event.key === "j" ? 1 : -1);
    }
  });
  intervalButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeRange = button.dataset.range || "ytd";
      activeInterval = button.dataset.interval || "1d";
      intervalButtons.forEach((item) => item.classList.toggle("active", item === button));
      if (activeSymbol) loadChart(activeSymbol, activeRange, activeInterval);
      syncUrlState();
    });
  });
  groupForm.addEventListener("submit", addGroup);
  assetForm.addEventListener("submit", addAsset);
  assetTypeSelect.addEventListener("change", syncSourceToType);
}

function selectView(view) {
  activeView = view === "markets" ? "markets" : "daily";
  const showDaily = activeView === "daily";
  dailyView.hidden = !showDaily;
  marketsView.hidden = showDaily;
  viewButtons.forEach((button) => {
    const selected = button.dataset.view === activeView;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  syncUrlState();
}

function handleViewTabKeydown(event) {
  const currentIndex = viewButtons.indexOf(event.currentTarget);
  if (currentIndex < 0) return;
  let nextIndex = currentIndex;
  if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % viewButtons.length;
  else if (event.key === "ArrowLeft") nextIndex = (currentIndex - 1 + viewButtons.length) % viewButtons.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = viewButtons.length - 1;
  else return;

  event.preventDefault();
  const nextButton = viewButtons[nextIndex];
  nextButton.focus();
  selectView(nextButton.dataset.view || "daily");
}

async function fetchQuotes() {
  refreshButton.classList.add("loading");
  try {
    const response = await fetch("/api/quotes");
    if (!response.ok) throw new Error("quotes_failed");
    const payload = await response.json();
    applyQuotes(payload);
    setConnection("live");
  } catch (error) {
    setConnection("error");
    statusCopy.textContent = "Market data unavailable · retrying";
    if (!latestData) {
      board.innerHTML = '<div class="empty-state">Quotes unavailable</div>';
      dailyBoard.innerHTML = '<div class="empty-state">Market read unavailable</div>';
    }
  } finally {
    refreshButton.classList.remove("loading");
  }
}

function shouldUseWebSocket() {
  return ["127.0.0.1", "localhost", "::1"].includes(window.location.hostname);
}

function openSocket() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/quotes`);

  socket.addEventListener("open", () => {
    feedMode = "ws";
    updateFeedModeLabel();
    setConnection("live");
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "quotes") {
      applyQuotes(message.data);
      setConnection("live");
    }
  });
  socket.addEventListener("close", () => {
    feedMode = "poll";
    updateFeedModeLabel();
    setConnection("error");
    window.setTimeout(openSocket, 3000);
  });
  socket.addEventListener("error", () => setConnection("error"));
}

function updateFeedModeLabel() {
  feedModeLabel.textContent = feedMode === "ws" ? "WS Live" : "Poll 15s";
  feedModeLabel.title =
    feedMode === "ws"
      ? "Streaming over WebSocket"
      : "Quotes refresh by HTTP poll every 15 seconds";
}

function applyQuotes(payload) {
  latestData = payload;
  renderBoard(payload);
  renderDailyBoard(payload.overview, latestCryptoEtfFlows);
  updateHeader(payload.overview);
  openPendingChartFromUrl();
}

async function fetchCryptoEtfFlows() {
  try {
    const response = await fetch("/api/crypto-etf-flows");
    if (!response.ok) throw new Error("crypto_etf_flows_failed");
    latestCryptoEtfFlows = await response.json();
  } catch (error) {
    latestCryptoEtfFlows = {
      status: "unavailable",
      source: "farside",
      error: "crypto_etf_flows_failed",
      assets: [],
    };
  }
  if (latestData?.overview) {
    renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
    updateHeader(latestData.overview);
  }
}

function updateHeader(overview) {
  if (!overview) return;
  const universe = overview.universe || {};
  const asOf = new Date(overview.as_of);
  const date = Number.isNaN(asOf.getTime()) ? "--" : formatLocalDate(asOf);
  const time = Number.isNaN(asOf.getTime()) ? "--" : formatClock(asOf);
  boardMeta.textContent = `${date} · ${universe.total || 0} names · universe v2`;
  liveFreshness.textContent = time === "--" ? "Updated --" : `Updated ${time}`;
  const usSession = sessionState("us");
  statusCopy.textContent = [
    feedMode === "ws" ? "LIVE QUOTES" : "POLLED QUOTES",
    usSession ? `US ${SESSION_STATE_COPY[usSession.state].toUpperCase()}` : null,
    `${universe.quoted || 0}/${universe.total || 0} QUOTED`,
    `HISTORY ${universe.history_count || 0}/${universe.total || 0}`,
    `FLOWS ${flowStatusLabel(latestCryptoEtfFlows)}`,
    `UPDATED ${time}`,
  ].filter(Boolean).join(" · ");
}

function renderDailyBoard(overview, cryptoEtfFlows) {
  if (!overview) {
    dailyBoard.innerHTML = '<div class="empty-state">Market read unavailable</div>';
    return;
  }

  const regime = overview.regime || {};
  const universe = overview.universe || {};
  const benchmarks = overview.benchmarks || [];
  const themes = overview.themes || [];
  const rotation = overview.rotation || {};
  const movers = themes
    .filter((theme) => typeof theme.acceleration === "number")
    .sort((a, b) => Math.abs(b.acceleration || 0) - Math.abs(a.acceleration || 0))
    .slice(0, 8);
  const asOf = new Date(overview.as_of);
  const asOfLabel = Number.isNaN(asOf.getTime()) ? "" : `As of ${formatLocalDate(asOf)}`;

  dailyBoard.innerHTML = `
    <section class="analytics-panel">
      ${panelHeading(
        "Regime Read",
        asOfLabel,
        "Risk-on/off from advance %, breadth vs 50/200DMA, and 3%+ movers across the universe"
      )}
      <div class="regime-grid">
        ${regimeCell(
          "Regime",
          regime.label || "--",
          `${formatPlainPct(universe.above_50dma_pct)} > 50DMA · ${formatPlainPct(universe.above_200dma_pct)} > 200DMA`,
          `tone-${regime.tone || "neutral"}`
        )}
        ${themeRegimeCell("Dominant", regime.dominant)}
        ${themeRegimeCell("Emerging", regime.emerging)}
        ${themeRegimeCell("Fading", regime.fading)}
        ${pairRegimeCell(
          "New Highs / Lows",
          universe.highs_20d,
          universe.lows_20d,
          `52W highs: ${universe.highs_52w || 0} · lows: ${universe.lows_52w || 0}`
        )}
        ${pairRegimeCell(
          "Up 3% / Down 3%",
          universe.up_3pct,
          universe.down_3pct,
          `${universe.advancers || 0} advancing · ${universe.decliners || 0} declining`
        )}
      </div>
    </section>

    <div class="analytics-grid">
      <section class="analytics-panel">
        ${panelHeading(
          "Benchmarks",
          "Return / Dist 50DMA / ATR Ext",
          "ETF_MACRO group. ATR ext = distance from 20DMA in ATR(14) units; above +2 is stretched"
        )}
        <div class="benchmark-grid">
          ${benchmarks.map(benchmarkCard).join("") || '<div class="empty-state">Add ETF_MACRO benchmarks</div>'}
        </div>
      </section>

      <section class="analytics-panel">
        ${panelHeading(
          "Breadth",
          `${universe.history_count || 0} names with history`,
          "Share of universe above moving averages; new 20-day highs/lows and 3%+ movers"
        )}
        <div class="breadth-grid">
          ${breadthRow("% > 20DMA", formatPlainPct(universe.above_20dma_pct))}
          ${breadthRow("% > 50DMA", formatPlainPct(universe.above_50dma_pct))}
          ${breadthRow("% > 200DMA", formatPlainPct(universe.above_200dma_pct))}
          ${breadthRow("Total names", formatInteger(universe.total))}
          ${breadthRow("New 20D highs", formatInteger(universe.highs_20d), "positive")}
          ${breadthRow("New 20D lows", formatInteger(universe.lows_20d), "negative")}
          ${breadthRow("Up 3%+", formatInteger(universe.up_3pct), "positive")}
          ${breadthRow("Down 3%+", formatInteger(universe.down_3pct), "negative")}
        </div>
      </section>
    </div>

    <div class="analytics-grid equal">
      <section class="analytics-panel">
        ${panelHeading(
          "Dominant Themes",
          "Score / 1D / 5D / Status",
          "Score = 50 + 7×avg 1D + 2×avg 5D + 0.18×(advance% − 50) + 0.18×(>50DMA% − 50), clamped 0–100. Status: ≥75 dominant, ≥62 strong, ≥52 emerging, ≥45 neutral, ≥30 deteriorating, else fading"
        )}
        ${themeTable(themes.slice(0, 8))}
      </section>
      <section class="analytics-panel">
        ${panelHeading(
          "Momentum Shifts",
          "\u0394 pace = 1D move minus 5D daily pace, pct-pts",
          "Themes accelerating or decelerating the hardest today versus their recent trend"
        )}
        ${themeTable(movers, "momentum")}
      </section>
    </div>

    <section class="analytics-panel">
      ${panelHeading("Crypto ETF Flows", cryptoEtfFlowNote(cryptoEtfFlows))}
      ${cryptoEtfFlowPanel(cryptoEtfFlows)}
    </section>

    <section class="analytics-panel">
      ${panelHeading(
        "Theme Rotation",
        "1D move versus 5D daily pace",
        "Climbers move faster than their 5-day pace today; fallers slower. Pace in pct-pts per day"
      )}
      <div class="rotation-grid">
        ${rotationColumn("↑ Climbers", rotation.climbers || [])}
        ${rotationColumn("↓ Fallers", rotation.fallers || [])}
      </div>
    </section>
  `;

  dailyBoard.querySelectorAll(".benchmark-card").forEach((card) => {
    card.addEventListener("click", () => {
      openChart({
        symbol: card.dataset.symbol,
        name: card.dataset.name,
        type: card.dataset.type || "etf",
        quote: { provider: card.dataset.provider || "" },
        summary: findAssetSummary(card.dataset.symbol),
      });
    });
  });
  dailyBoard.querySelectorAll(".theme-link").forEach((button) => {
    button.addEventListener("click", () => filterMarketsByGroup(button.dataset.group || ""));
  });
}

function panelHeading(title, note, tip = "") {
  const titleAttr = tip ? ` title="${escapeHtml(tip)}"` : "";
  return `<header class="panel-heading"${titleAttr}><h2>${escapeHtml(title)}</h2><span>${escapeHtml(note || "")}</span></header>`;
}

function regimeCell(label, value, detail, tone = "") {
  return `<div class="regime-cell">
    <span class="metric-label">${escapeHtml(label)}</span>
    <strong class="metric-value ${tone}">${escapeHtml(value)}</strong>
    <span class="metric-detail">${escapeHtml(detail || "")}</span>
  </div>`;
}

function themeRegimeCell(label, theme) {
  if (!theme) return regimeCell(label, "--", "Insufficient data");
  const change = formatSignedPct(theme.change_1d);
  return regimeCell(
    label,
    displayGroupName(theme.name),
    `${theme.status} · 1D ${change}`,
    changeClass(theme.change_1d)
  );
}

function pairRegimeCell(label, positive, negative, detail) {
  return `<div class="regime-cell">
    <span class="metric-label">${escapeHtml(label)}</span>
    <div class="metric-value split-value"><span class="tone-positive">↑${formatInteger(positive)}</span><span>/</span><span class="tone-negative">↓${formatInteger(negative)}</span></div>
    <span class="metric-detail">${escapeHtml(detail)}</span>
  </div>`;
}

function benchmarkCard(item) {
  return `<button class="benchmark-card" type="button" data-symbol="${escapeHtml(item.symbol)}" data-name="${escapeHtml(item.name || "")}" data-type="${escapeHtml(item.type || "etf")}" data-provider="">
    <span class="benchmark-symbol">${escapeHtml(item.symbol)}</span>
    <span class="benchmark-name">${escapeHtml(item.name || item.type || "Benchmark")}</span>
    <span class="metric-lines">
      ${metricLine("1D", formatSignedPct(item.change_1d), changeClass(item.change_1d), "Return over the last close")}
      ${metricLine("5D", formatSignedPct(item.change_5d), changeClass(item.change_5d), "Return over the last 5 sessions")}
      ${metricLine(">50DMA", formatSignedPct(item.distance_50dma), changeClass(item.distance_50dma), "Distance from the 50-day moving average")}
      ${metricLine("ATR ext", formatSignedNumber(item.atr_extension), changeClass(item.atr_extension), "Distance from 20DMA in ATR(14) units — above +2 is stretched")}
    </span>
  </button>`;
}

function metricLine(label, value, tone, tip = "") {
  const titleAttr = tip ? ` title="${escapeHtml(tip)}"` : "";
  return `<span class="metric-line"${titleAttr}><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></span>`;
}

function breadthRow(label, value, tone = "") {
  return `<div class="breadth-row"><span>${escapeHtml(label)}</span><strong class="${tone ? `tone-${tone}` : ""}">${escapeHtml(value)}</strong></div>`;
}

function themeTable(themes, variant = "score") {
  const momentum = variant === "momentum";
  const third = momentum ? "\u0394 Pace" : "Score";
  return `<table class="theme-table">
    <thead><tr><th>#</th><th>Theme</th><th>${third}</th><th>1D</th><th>5D</th><th>Status</th></tr></thead>
    <tbody>${themes.map((theme) => themeRow(theme, momentum)).join("") || '<tr><td colspan="6">No themes configured</td></tr>'}</tbody>
  </table>`;
}

function themeRow(theme, momentum = false) {
  const score = scorePercent(theme.score);
  const third = momentum
    ? `<td class="${changeClass(theme.acceleration)}" title="1D move minus 5D daily pace">${formatSignedNumber(theme.acceleration)}</td>`
    : `<td><span class="score-bar" style="--score: ${score}%"><span class="score-value">${formatInteger(theme.score)}</span></span></td>`;
  return `<tr>
    <td>${formatInteger(theme.rank)}</td>
    <td><button class="theme-link" type="button" data-group="${escapeHtml(theme.name)}" title="Show ${escapeHtml(displayGroupName(theme.name))} in Markets">${escapeHtml(displayGroupName(theme.name))}</button><span class="member-count">${formatInteger(theme.count)}</span></td>
    ${third}
    <td class="${changeClass(theme.change_1d)}">${formatSignedPct(theme.change_1d)}</td>
    <td class="${changeClass(theme.change_5d)}">${formatSignedPct(theme.change_5d)}</td>
    <td><span class="status-tag status-${String(theme.status || "neutral").toLowerCase()}">${escapeHtml(theme.status || "NEUTRAL")}</span></td>
  </tr>`;
}

function rotationColumn(label, themes) {
  return `<div class="rotation-column">
    <div class="rotation-label">${escapeHtml(label)}</div>
    ${themes.map(rotationRow).join("") || '<div class="empty-state">No rotation data</div>'}
  </div>`;
}

function rotationRow(theme) {
  return `<div class="rotation-row">
    <strong>${escapeHtml(displayGroupName(theme.name))}</strong>
    <span>${formatSignedPct(theme.change_5d)} 5D</span>
    <span class="${changeClass(theme.acceleration)}">${formatSignedNumber(theme.acceleration)} pace</span>
  </div>`;
}

function cryptoEtfFlowNote(flows) {
  if (!flows) return "Loading";
  if (flows.status !== "ok") return cryptoEtfFlowError(flows.error);
  const updated = new Date(flows.updated_at);
  return Number.isNaN(updated.getTime())
    ? "Farside"
    : `Farside · updated ${formatClock(updated)}`;
}

function flowStatusLabel(flows) {
  if (!flows) return "PENDING";
  if (flows.status !== "ok") return "UNAVAILABLE";
  return String(flows.source || "FARSIDE").toUpperCase();
}

function cryptoEtfFlowPanel(flows) {
  if (!flows) return '<div class="empty-state">Loading ETF flows</div>';
  if (flows.status !== "ok") {
    return `<div class="empty-state">${escapeHtml(cryptoEtfFlowError(flows.error))}</div>`;
  }
  const assets = flows.assets || [];
  const newestDate = assets
    .filter(hasLatestFlowPrint)
    .map((asset) => String(asset.latest_date || ""))
    .sort()
    .pop() || "";
  return `<div class="crypto-flow-grid">
    ${assets.map((asset) => cryptoEtfFlowCard(asset, newestDate)).join("") || '<div class="empty-state">No ETF flow data</div>'}
  </div>`;
}

function cryptoEtfFlowCard(asset, newestDate = "") {
  const hasLatestPrint = hasLatestFlowPrint(asset);
  const assetDate = String(asset.latest_date || "");
  const behind = hasLatestPrint && newestDate && assetDate && assetDate < newestDate;
  const dateTone = behind ? "tone-warn" : "";
  const dateTip = behind ? `Latest print is older than ${formatFlowDate(newestDate)} — table not updated yet` : "";
  return `<div class="crypto-flow-card">
    <div class="crypto-flow-summary">
      <div>
        <span class="metric-label">${escapeHtml(asset.name || `${asset.asset} ETFs`)}</span>
        <strong class="metric-value ${changeClass(hasLatestPrint ? asset.latest_flow_usd : null)}">${hasLatestPrint ? formatUsdFlow(asset.latest_flow_usd) : "No print"}</strong>
      </div>
      <div class="flow-side-metrics">
        ${metricLine("5D", formatUsdFlow(asset.five_day_flow_usd), changeClass(asset.five_day_flow_usd), "Sum of the last 5 daily prints")}
        ${metricLine("10D", formatUsdFlow(asset.ten_day_flow_usd), changeClass(asset.ten_day_flow_usd), "Sum of the last 10 daily prints")}
        ${metricLine("Date", hasLatestPrint ? `${formatFlowDate(asset.latest_date)}${behind ? " ⚠" : ""}` : "--", dateTone, dateTip)}
      </div>
    </div>
    ${cryptoFlowLists(asset)}
  </div>`;
}

function cryptoFlowLists(asset) {
  const leaders = asset.leaders || [];
  const laggards = asset.laggards || [];
  if (!leaders.length && !laggards.length) {
    return '<div class="crypto-flow-lists"><span class="flow-empty solo">No fund-level prints reported</span></div>';
  }
  const columns = [
    leaders.length ? flowList("Inflows", leaders, "change-positive") : "",
    laggards.length ? flowList("Outflows", laggards, "change-negative") : "",
  ].filter(Boolean);
  return `<div class="crypto-flow-lists${columns.length === 1 ? " single" : ""}">${columns.join("")}</div>`;
}

function flowList(label, items, tone) {
  return `<div class="flow-list">
    <span class="flow-list-label">${escapeHtml(label)}</span>
    ${(items || []).map((item) => flowItem(item, tone)).join("")}
  </div>`;
}

function flowItem(item, tone) {
  return `<div class="flow-item">
    <strong>${escapeHtml(item.ticker || "--")}</strong>
    <span class="${tone}">${formatUsdFlow(item.flow_usd)}</span>
  </div>`;
}

function hasLatestFlowPrint(asset) {
  if (typeof asset.latest_flow_usd !== "number") return false;
  if (asset.latest_flow_usd !== 0) return true;
  return Boolean((asset.leaders || []).length || (asset.laggards || []).length);
}

function cryptoEtfFlowError(error) {
  if (error === "farside_fetch_failed") return "Farside flows unavailable";
  return "ETF flows unavailable";
}

function renderBoard(payload) {
  if (!payload) return;
  const groups = marketLayout === "flat" ? flatGroups(payload.groups || []) : visibleGroups(payload.groups || []);
  board.classList.remove("board-loading");
  board.classList.toggle("flat", marketLayout === "flat");
  if (!groups.length) {
    const totalAssets = countAssets(payload.groups || []);
    const hasFilter = activeGroupFilter || marketSearchQuery;
    board.innerHTML = `<div class="empty-state">${hasFilter ? "No matching markets" : "No groups configured"}</div>`;
    updateMarketFilterStatus(0, totalAssets);
    return;
  }

  const totalAssets = countAssets(payload.groups || []);
  const visibleAssets = countAssets(groups);
  const nextGroups = new Set(groups.map((group) => group.name));

  groups.forEach((group) => {
    const panel = ensureGroupPanel(group.name);
    updateGroupSessionChip(panel, group.assets || []);
    const assets = sortedAssets(group.assets || []);
    const nextSymbols = new Set(assets.map((asset) => asset.symbol));

    assets.forEach((asset) => {
      let row = panel.querySelector(`.asset-row[data-symbol="${cssEscape(asset.symbol)}"]`);
      if (!row) {
        row = renderRow(asset);
      } else {
        updateRow(row, asset);
      }
      panel.appendChild(row);
    });

    panel.querySelectorAll(".asset-row").forEach((row) => {
      if (!nextSymbols.has(row.dataset.symbol)) row.remove();
    });
    board.appendChild(panel);
  });

  board.querySelectorAll(".group-panel").forEach((panel) => {
    if (!nextGroups.has(panel.dataset.group)) panel.remove();
  });
  updateSortHeaders();
  updateMarketFilterStatus(visibleAssets, totalAssets);
}

function toggleMarketLayout() {
  marketLayout = marketLayout === "flat" ? "grouped" : "flat";
  if (marketLayout === "flat" && marketSort.key === "configured") {
    marketSort = { key: "pct", direction: "desc" };
  }
  marketLayoutToggle.setAttribute("aria-pressed", String(marketLayout === "flat"));
  marketLayoutToggle.textContent = marketLayout === "flat" ? "Grouped" : "Flat";
  marketLayoutToggle.title =
    marketLayout === "flat"
      ? "Back to sector groups"
      : "Flatten all groups into one sortable movers table";
  renderBoard(latestData);
  syncUrlState();
}

function flatGroups(groups) {
  const seen = new Set();
  const assets = [];
  for (const group of visibleGroups(groups)) {
    for (const asset of group.assets) {
      if (seen.has(asset.symbol)) continue;
      seen.add(asset.symbol);
      assets.push({ ...asset, groupLabel: displayGroupName(group.name) });
    }
  }
  return assets.length ? [{ name: "__ALL__", assets }] : [];
}

function ensureGroupPanel(groupName) {
  let panel = board.querySelector(`.group-panel[data-group="${cssEscape(groupName)}"]`);
  if (panel) return panel;

  panel = document.createElement("section");
  panel.className = "group-panel";
  panel.dataset.group = groupName;

  const header = document.createElement("div");
  header.className = "group-title";
  header.append(
    groupHeaderCell(displayGroupName(groupName), "symbol"),
    groupHeaderCell("Last", "last"),
    groupHeaderCell("1D Abs", "abs"),
    groupHeaderCell("1D %", "pct"),
    groupHeaderCell("Trend", "trend")
  );
  panel.appendChild(header);
  return panel;
}

function updateGroupSessionChip(panel, assets) {
  const firstCell = panel.querySelector(".group-title span");
  if (!firstCell) return;
  let chip = firstCell.querySelector(".session-chip");
  const info = groupSessionChip(assets);
  if (!info) {
    chip?.remove();
    return;
  }
  if (!chip) {
    chip = document.createElement("em");
    chip.className = "session-chip";
    firstCell.appendChild(chip);
  }
  chip.textContent = info.text;
  chip.title = info.title;
  chip.dataset.state = info.state;
}

function groupHeaderCell(label, sortKey) {
  const cell = document.createElement("span");
  if (sortKey === "source" || sortKey === "trend") {
    cell.textContent = label;
    return cell;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.sortKey = sortKey;
  button.textContent = label;
  button.title = `Sort by ${label}`;
  button.addEventListener("click", () => setMarketSort(sortKey));
  cell.appendChild(button);
  return cell;
}

function setMarketSort(sortKey) {
  if (marketSort.key === sortKey) {
    marketSort = {
      key: sortKey,
      direction: marketSort.direction === "asc" ? "desc" : "asc",
    };
  } else {
    marketSort = {
      key: sortKey,
      direction: sortKey === "symbol" ? "asc" : "desc",
    };
  }
  renderBoard(latestData);
}

function updateSortHeaders() {
  board.querySelectorAll(".group-title button").forEach((button) => {
    const active = button.dataset.sortKey === marketSort.key;
    button.classList.toggle("active-sort", active);
    button.setAttribute("aria-sort", active ? (marketSort.direction === "asc" ? "ascending" : "descending") : "none");
  });
}

function sortedAssets(assets) {
  if (marketSort.key === "configured") return [...assets];
  return [...assets].sort((a, b) => {
    const direction = marketSort.direction === "asc" ? 1 : -1;
    if (marketSort.key === "symbol") {
      return a.symbol.localeCompare(b.symbol) * direction;
    }
    const aValue = sortValue(a, marketSort.key);
    const bValue = sortValue(b, marketSort.key);
    if (aValue === bValue) return a.symbol.localeCompare(b.symbol);
    if (aValue === null) return 1;
    if (bValue === null) return -1;
    return (aValue - bValue) * direction;
  });
}

function sortValue(asset, key) {
  const quote = asset.quote || {};
  if (key === "last") return numericOrNull(displayQuoteValue(quote, "last"));
  if (key === "abs") return numericOrNull(displayQuoteValue(quote, "change_abs"));
  if (key === "pct") return numericOrNull(displayQuoteValue(quote, "change_pct"));
  return null;
}

function visibleGroups(groups) {
  return groups
    .filter((group) => !activeGroupFilter || group.name === activeGroupFilter)
    .map((group) => ({
      ...group,
      assets: (group.assets || []).filter((asset) => assetMatchesFilter(asset, group.name)),
    }))
    .filter((group) => group.assets.length);
}

function assetMatchesFilter(asset, groupName) {
  if (!marketSearchQuery) return true;
  const haystack = [
    asset.symbol,
    asset.name,
    asset.exchange,
    asset.type,
    displayGroupName(groupName),
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(marketSearchQuery.toLowerCase());
}

function countAssets(groups) {
  return groups.reduce((total, group) => total + (group.assets || []).length, 0);
}

function updateMarketFilterStatus(visibleCount, totalCount) {
  const filters = [];
  if (activeGroupFilter) filters.push(displayGroupName(activeGroupFilter));
  if (marketSearchQuery) filters.push(`"${marketSearchQuery}"`);
  marketFilterStatus.textContent = filters.length
    ? `${visibleCount}/${totalCount} shown · ${filters.join(" · ")}`
    : `${totalCount} markets`;
  marketFilterClear.hidden = !filters.length;
}

function clearMarketFilters() {
  activeGroupFilter = "";
  marketSearchQuery = "";
  marketSearch.value = "";
  renderBoard(latestData);
  syncUrlState();
}

function focusFirstMarketRow() {
  const row = board.querySelector(".asset-row");
  if (row) row.focus();
}

function moveMarketRowFocus(step) {
  const rows = Array.from(board.querySelectorAll(".asset-row"));
  if (!rows.length) return;
  const currentIndex = rows.indexOf(document.activeElement);
  const nextIndex = currentIndex === -1 ? (step > 0 ? 0 : rows.length - 1) : currentIndex + step;
  const target = rows[Math.max(0, Math.min(rows.length - 1, nextIndex))];
  target.focus();
  target.scrollIntoView({ block: "nearest" });
}

async function openEditor() {
  openDialog(editorModal, groupNameInput);
  setEditorStatus(persistenceNotice());
  await fetchWatchlistConfig();
}

function persistenceNotice() {
  // Local runs persist edits to the YAML file; serverless deployments write
  // to /tmp and lose edits on the next cold start.
  return shouldUseWebSocket() ? "" : "Edits are session-only on this deployment — they reset on redeploy/cold start.";
}

function closeEditor() {
  closeDialog(editorModal);
}

async function fetchWatchlistConfig() {
  try {
    const response = await fetch("/api/groups");
    if (!response.ok) throw new Error("groups_failed");
    watchlistConfig = await response.json();
    renderEditor();
    setEditorStatus("");
  } catch (error) {
    setEditorStatus("Unable to load universe");
  }
}

function renderEditor() {
  const groups = watchlistConfig?.groups || [];
  assetGroupSelect.replaceChildren(
    ...groups.map((group) => {
      const option = document.createElement("option");
      option.value = group.name;
      option.textContent = displayGroupName(group.name);
      return option;
    })
  );
  assetForm.querySelector("button").disabled = groups.length === 0;

  editorList.replaceChildren(
    ...groups.map((group) => {
      const section = document.createElement("section");
      section.className = "editor-group";

      const header = document.createElement("div");
      header.className = "editor-group-header";
      const title = document.createElement("strong");
      title.textContent = displayGroupName(group.name);
      const removeGroup = document.createElement("button");
      removeGroup.type = "button";
      removeGroup.textContent = "Remove";
      removeGroup.dataset.group = group.name;
      removeGroup.addEventListener("click", () => removeGroupByName(group.name));
      header.append(title, removeGroup);
      section.appendChild(header);

      const assets = document.createElement("div");
      assets.className = "editor-assets";
      (group.assets || []).forEach((asset) => assets.appendChild(renderEditorAsset(group.name, asset)));
      if (!group.assets?.length) {
        const empty = document.createElement("div");
        empty.className = "editor-empty";
        empty.textContent = "No assets";
        assets.appendChild(empty);
      }
      section.appendChild(assets);
      return section;
    })
  );
}

function renderEditorAsset(groupName, asset) {
  const row = document.createElement("div");
  row.className = "editor-asset";
  const label = document.createElement("span");
  label.textContent = [asset.symbol, asset.type, asset.source, asset.exchange || "", asset.name || ""]
    .filter(Boolean)
    .join(" / ");
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.dataset.group = groupName;
  remove.dataset.symbol = asset.symbol;
  remove.addEventListener("click", () => removeAsset(groupName, asset.symbol));
  row.append(label, remove);
  return row;
}

async function addGroup(event) {
  event.preventDefault();
  const name = groupNameInput.value.trim();
  if (!name) return;
  await mutateWatchlists("/api/groups", { method: "POST", body: JSON.stringify({ name }) });
  groupNameInput.value = "";
}

async function addAsset(event) {
  event.preventDefault();
  const groupName = assetGroupSelect.value;
  if (!groupName) return;
  await mutateWatchlists(`/api/groups/${encodeURIComponent(groupName)}/assets`, {
    method: "POST",
    body: JSON.stringify({
      symbol: assetSymbolInput.value,
      type: assetTypeSelect.value,
      source: assetSourceSelect.value,
      exchange: assetExchangeInput.value || null,
      name: assetNameInput.value || null,
    }),
  });
  assetSymbolInput.value = "";
  assetExchangeInput.value = "";
  assetNameInput.value = "";
}

async function removeGroupByName(groupName) {
  const group = (watchlistConfig?.groups || []).find((item) => item.name === groupName);
  const count = group?.assets?.length || 0;
  const label = displayGroupName(groupName);
  const detail = count ? ` and its ${count} asset${count === 1 ? "" : "s"}` : "";
  if (!window.confirm(`Remove group "${label}"${detail}? This cannot be undone.`)) return;
  await mutateWatchlists(`/api/groups/${encodeURIComponent(groupName)}`, { method: "DELETE" });
}

async function removeAsset(groupName, symbol) {
  await mutateWatchlists(
    `/api/groups/${encodeURIComponent(groupName)}/assets/${encodeURIComponent(symbol)}`,
    { method: "DELETE" }
  );
}

const EDITOR_ERROR_COPY = {
  symbol_not_found: "Symbol not recognized by the selected source — check spelling and source",
  asset_already_exists: "That symbol is already in this group",
  group_not_found: "Group no longer exists — reload the editor",
  group_already_exists: "A group with that name already exists",
};

function editorErrorCopy(detail) {
  return EDITOR_ERROR_COPY[detail] || detail || "Save failed";
}

async function mutateWatchlists(url, options) {
  setEditorStatus("Saving");
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setEditorStatus(editorErrorCopy(payload.detail));
    return;
  }
  watchlistConfig = await response.json();
  renderEditor();
  const notice = persistenceNotice();
  setEditorStatus(notice ? `Saved (session only) — ${notice}` : "Saved");
  await fetchQuotes();
}

function syncSourceToType() {
  if (assetTypeSelect.value === "crypto_perp") assetSourceSelect.value = "hyperliquid";
  else if (assetSourceSelect.value === "hyperliquid") assetSourceSelect.value = "yahoo";
}

function setEditorStatus(text) {
  editorStatus.textContent = text;
}

function renderRow(asset) {
  const row = document.createElement("button");
  row.type = "button";
  row.addEventListener("click", () => openChart(asset));
  updateRow(row, asset, { initial: true });
  return row;
}

function displayQuote(quote) {
  return {
    last: displayQuoteValue(quote, "last"),
    previous_close: displayQuoteValue(quote, "previous_close"),
    change_abs: displayQuoteValue(quote, "change_abs"),
    change_pct: displayQuoteValue(quote, "change_pct"),
    currency: quote.display_currency || quote.currency,
  };
}

function displayQuoteValue(quote, key) {
  const displayKey = `display_${key}`;
  return typeof quote[displayKey] === "number" ? quote[displayKey] : quote[key];
}

function updateRow(row, asset, options = {}) {
  const quote = asset.quote || {};
  const display = displayQuote(quote);
  row.className = `asset-row${quote.is_stale ? " stale-row" : ""}`;
  row.dataset.symbol = asset.symbol;
  row.dataset.provider = quote.provider || "";
  row.dataset.assetType = asset.type || "";
  row.dataset.name = asset.name || "";
  row.setAttribute("aria-label", `${asset.symbol} chart`);
  const age = quoteAge(quote);
  const ageNote = quote.is_stale ? `Stale quote · last update ${age || "unknown"}` : age ? `Updated ${age}` : "";
  row.title = [`${asset.symbol} ${asset.name || ""}`.trim(), ageNote].filter(Boolean).join(" · ");

  updateSymbolCell(ensureRowCell(row, "symbol"), asset);
  updateValueCell(
    ensureRowCell(row, "last", "last-cell"),
    formatBoardPrice(display.last, quote.error, display.currency),
    display.last,
    "last-cell",
    !options.initial
  );
  updateValueCell(
    ensureRowCell(row, "abs", "change-abs-cell"),
    formatBoardSignedChange(display.change_abs, display.currency),
    display.change_abs,
    "change-abs-cell",
    !options.initial
  );
  updateValueCell(
    ensureRowCell(row, "pct"),
    formatSignedPct(display.change_pct),
    display.change_pct,
    changeClass(display.change_pct),
    !options.initial
  );
  updateSparklineCell(ensureRowCell(row, "trend", "sparkline-cell"), asset.summary?.sparkline || []);
}

function ensureRowCell(row, key, className = "") {
  let cell = row.querySelector(`[data-cell="${key}"]`);
  if (cell) return cell;
  cell = document.createElement("span");
  cell.dataset.cell = key;
  if (className) cell.className = className;
  row.appendChild(cell);
  return cell;
}

function updateSymbolCell(cell, asset) {
  cell.className = "symbol-cell";
  cell.title = `${asset.symbol} ${asset.name || asset.exchange || asset.type || ""}`.trim();
  let symbol = cell.querySelector("strong");
  let name = cell.querySelector("small");
  if (!symbol) {
    symbol = document.createElement("strong");
    cell.appendChild(symbol);
  }
  if (!name) {
    name = document.createElement("small");
    cell.appendChild(name);
  }
  symbol.textContent = asset.symbol;
  const base = asset.name || asset.exchange || asset.type || "";
  name.textContent = asset.groupLabel ? `${base} · ${asset.groupLabel}` : base;
}

function updateValueCell(cell, text, value, className, shouldFlash) {
  const previous = numericOrNull(cell.dataset.value);
  cell.textContent = text;
  cell.className = className;
  cell.title = text;
  if (typeof value === "number") cell.dataset.value = String(value);
  else delete cell.dataset.value;
  if (shouldFlash && previous !== null && typeof value === "number" && value !== previous) {
    flashCell(cell, value - previous);
  }
}

function updateSparklineCell(cell, values) {
  cell.className = "sparkline-cell";
  cell.title = values.length ? "Recent trend" : "No trend history";
  cell.innerHTML = sparklineSvg(values);
}

function flashCell(cell, delta) {
  cell.classList.remove("flash-up", "flash-down");
  void cell.offsetWidth;
  cell.classList.add(delta > 0 ? "flash-up" : "flash-down");
  window.setTimeout(() => cell.classList.remove("flash-up", "flash-down"), 450);
}

function sparklineSvg(values) {
  const points = Array.isArray(values) ? values.map(Number).filter((value) => Number.isFinite(value)) : [];
  if (points.length < 2) return '<span class="sparkline-empty">--</span>';
  const width = 64;
  const height = 22;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const path = points
    .map((value, index) => {
      const x = (index / (points.length - 1)) * width;
      const y = height - ((value - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const tone = points[points.length - 1] >= points[0] ? "positive" : "negative";
  return `<svg class="sparkline sparkline-${tone}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${path}"></polyline></svg>`;
}

function filterMarketsByGroup(groupName) {
  if (!groupName) return;
  activeGroupFilter = groupName;
  marketSearchQuery = "";
  marketSearch.value = "";
  selectView("markets");
  renderBoard(latestData);
  syncUrlState();
}

function openChart(asset, options = {}) {
  const symbol = asset?.symbol || "";
  const name = asset?.name || symbol;
  const provider = asset?.quote?.provider || asset?.provider || "";
  const assetType = asset?.type || asset?.asset_type || "";
  activeSymbol = symbol;
  activeAsset = asset || null;
  activeHistoryContext = null;
  chartContextLoading = false;
  const requestedRange = options.range || "ytd";
  const rangeButton =
    intervalButtons.find((item) => item.dataset.range === requestedRange) ||
    intervalButtons.find((item) => item.dataset.range === "ytd");
  activeRange = rangeButton?.dataset.range || "ytd";
  activeInterval = rangeButton?.dataset.interval || "1d";
  intervalButtons.forEach((item) => item.classList.toggle("active", item === rangeButton));
  updateIntradayAvailability(assetType);
  chartTitle.textContent = symbol;
  chartSubtitle.textContent = [name, sourceLabels[provider] || provider].filter(Boolean).join(" / ");
  openDialog(modal, modalClose);
  loadChart(symbol, activeRange, activeInterval);
  syncUrlState();
  if (isCryptoAsset(assetType)) {
    hideProfilePanel();
  } else {
    showProfilePanel();
    setProfileLoading(symbol, asset);
    loadAssetProfile(symbol);
  }
}

function updateIntradayAvailability(assetType) {
  const crypto = isCryptoAsset(assetType);
  const session = crypto ? null : sessionState(EXCHANGE_SESSIONS[String(activeAsset?.exchange || "").toUpperCase()] || "us");
  const closed = !crypto && session && session.state !== "open";
  intervalButtons.forEach((button) => {
    if (!("intraday" in button.dataset)) return;
    button.classList.toggle("session-closed", Boolean(closed));
    button.title = closed
      ? `${session.label} market ${SESSION_STATE_COPY[session.state].toLowerCase()} — shows last session's bars`
      : "";
  });
}

function closeModal() {
  if (!closeDialog(modal)) return;
  chartLoadToken += 1;
  activeSymbol = null;
  activeAsset = null;
  activeHistoryContext = null;
  chartContextLoading = false;
  if (chart) {
    chart.remove();
    chart = null;
  }
  showProfilePanel();
  profileElement.innerHTML = '<div class="profile-empty">Select an asset to load profile data</div>';
  resetProfileScroll();
  syncUrlState();
}

async function loadChart(symbol, range, interval) {
  const requestId = chartLoadToken + 1;
  chartLoadToken = requestId;
  chartContextLoading = true;
  activeHistoryContext = null;
  chartError.hidden = true;
  chartError.textContent = "";
  chartElement.innerHTML = chartLoadingMarkup("Loading chart data");
  updateProfileMarketContext();
  if (chart) {
    chart.remove();
    chart = null;
  }

  try {
    const response = await fetch(
      `/api/history/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}`
    );
    if (!response.ok) throw new Error("history_failed");
    const payload = await response.json();
    if (activeSymbol !== symbol || requestId !== chartLoadToken) return;
    const rawBars = payload.bars || [];
    const bars = rawBars.map((bar) => ({
      time: toChartTime(bar.timestamp, interval),
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
      volume: numericOrNull(bar.volume),
    }));
    if (!bars.length) throw new Error("No history available");
    chartElement.replaceChildren();
    renderChart(bars, interval);
    activeHistoryContext = profileMarketContextFromHistory(rawBars);
    chartContextLoading = false;
    updateProfileMarketContext();
    chartSubtitle.textContent = chartSubtitleText(symbol, range, interval, rawBars, bars.length);
    scheduleChartResize();
  } catch (error) {
    if (activeSymbol !== symbol || requestId !== chartLoadToken) return;
    chartContextLoading = false;
    activeHistoryContext = null;
    updateProfileMarketContext();
    chartElement.replaceChildren();
    chartError.textContent = error.message === "No history available" ? error.message : "Chart unavailable";
    chartError.hidden = false;
  }
}

const INTRADAY_INTERVALS = new Set(["1m", "5m", "15m", "1h"]);

function chartSubtitleText(symbol, range, interval, rawBars, barCount) {
  const base = `${symbol} / ${range.toUpperCase()} / ${interval} / ${barCount} bars`;
  if (!INTRADAY_INTERVALS.has(interval) || !rawBars.length) return base;
  const first = new Date(rawBars[0].timestamp);
  const last = new Date(rawBars[rawBars.length - 1].timestamp);
  if (Number.isNaN(first.getTime()) || Number.isNaN(last.getTime())) return base;
  const dateFmt = new Intl.DateTimeFormat([], { month: "short", day: "numeric" });
  const timeFmt = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" });
  const sameDay = first.toDateString() === last.toDateString();
  const window = sameDay
    ? `${dateFmt.format(last)} ${timeFmt.format(first)}–${timeFmt.format(last)}`
    : `${dateFmt.format(first)} ${timeFmt.format(first)} – ${dateFmt.format(last)} ${timeFmt.format(last)}`;
  const ageMs = Date.now() - last.getTime();
  const staleNote = ageMs > 2 * 3600 * 1000 ? " · prev session" : "";
  return `${base} · ${window}${staleNote}`;
}

const MA_OVERLAYS = [
  { period: 20, color: "#b8a06a" },
  { period: 50, color: "#5b8dbf" },
  { period: 200, color: "#9a6dbf" },
];

function renderChart(bars, interval) {
  if (!window.LightweightCharts) throw new Error("Chart library unavailable");
  const chartWidth = chartElement.clientWidth || 900;
  const chartHeight = Math.max(chartElement.clientHeight, 320);
  chart = window.LightweightCharts.createChart(chartElement, {
    width: chartWidth,
    height: chartHeight,
    layout: { background: { color: "#0a0b0c" }, textColor: "#a4abb3" },
    grid: { vertLines: { color: "#181a1d" }, horzLines: { color: "#181a1d" } },
    rightPriceScale: { borderColor: "#23262a", scaleMargins: { top: 0.05, bottom: 0.22 } },
    timeScale: { borderColor: "#23262a", timeVisible: interval !== "1d" },
    crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
  });
  const series = chart.addCandlestickSeries({
    upColor: "#4db38a",
    downColor: "#e0635f",
    borderUpColor: "#4db38a",
    borderDownColor: "#e0635f",
    wickUpColor: "#4db38a",
    wickDownColor: "#e0635f",
  });
  series.setData(bars);

  const drawnMas = drawMovingAverages(bars);
  drawVolumePane(bars);
  drawPreviousCloseLine(series, interval);
  renderChartLegend(drawnMas);

  chart.timeScale().fitContent();
  scheduleChartResize();
}

function drawMovingAverages(bars) {
  const closes = bars.map((bar) => bar.close);
  const drawn = [];
  MA_OVERLAYS.forEach(({ period, color }) => {
    if (closes.length < period) return;
    const points = [];
    let sum = 0;
    for (let index = 0; index < closes.length; index += 1) {
      sum += closes[index];
      if (index >= period) sum -= closes[index - period];
      if (index >= period - 1) {
        points.push({ time: bars[index].time, value: sum / period });
      }
    }
    const line = chart.addLineSeries({
      color,
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });
    line.setData(points);
    drawn.push({ period, color });
  });
  return drawn;
}

function drawVolumePane(bars) {
  if (!bars.some((bar) => typeof bar.volume === "number" && bar.volume > 0)) return;
  const volumeSeries = chart.addHistogramSeries({
    priceScaleId: "volume",
    priceFormat: { type: "volume" },
    priceLineVisible: false,
    lastValueVisible: false,
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  volumeSeries.setData(
    bars
      .filter((bar) => typeof bar.volume === "number")
      .map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? "rgba(77, 179, 138, 0.35)" : "rgba(224, 99, 95, 0.35)",
      }))
  );
}

function drawPreviousCloseLine(series, interval) {
  if (!INTRADAY_INTERVALS.has(interval)) return;
  const quote = activeAsset?.quote || {};
  const prevClose = numericOrNull(
    typeof quote.display_previous_close === "number" ? quote.display_previous_close : quote.previous_close
  );
  if (prevClose === null || prevClose <= 0) return;
  series.createPriceLine({
    price: prevClose,
    color: "#8a9098",
    lineWidth: 1,
    lineStyle: window.LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true,
    title: "prev close",
  });
}

function renderChartLegend(mas) {
  if (!mas.length) return;
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  legend.innerHTML = mas
    .map(({ period, color }) => `<span><i style="background:${color}"></i>MA${period}</span>`)
    .join("");
  chartElement.appendChild(legend);
}

function setupChartResizeObserver() {
  window.addEventListener("resize", scheduleChartResize);
  if (!("ResizeObserver" in window)) return;
  chartResizeObserver = new ResizeObserver(scheduleChartResize);
  [chartElement, modalShell, profileElement].forEach((element) => {
    if (element) chartResizeObserver.observe(element);
  });
}

function scheduleChartResize() {
  if (!chart || !modal.classList.contains("open")) return;
  if (chartResizeFrame !== null) return;
  chartResizeFrame = window.requestAnimationFrame(() => {
    chartResizeFrame = null;
    resizeChartToContainer();
  });
}

function resizeChartToContainer() {
  if (!chart) return;
  const width = Math.max(1, Math.floor(chartElement.clientWidth || 0));
  const height = Math.max(320, Math.floor(chartElement.clientHeight || 0));
  chart.applyOptions({ width, height });
}

async function loadAssetProfile(symbol) {
  try {
    const response = await fetch(`/api/profile/${encodeURIComponent(symbol)}`);
    if (!response.ok) throw new Error("profile_failed");
    const payload = await response.json();
    if (activeSymbol !== symbol) return;
    renderAssetProfile(payload);
  } catch {
    if (activeSymbol !== symbol) return;
    const summary = activeAsset?.summary || findAssetSummary(symbol);
    profileElement.innerHTML = `
      <div class="profile-empty">
        <strong>Profile unavailable</strong>
        <span>Company data could not be loaded for ${escapeHtml(symbol)}.</span>
      </div>
      ${profileMarketContext(mergedProfileMarketContext(summary), { loading: chartContextLoading })}
    `;
    resetProfileScroll();
    scheduleChartResize();
  }
}

function setProfileLoading(symbol, asset) {
  const summary = asset?.summary || findAssetSummary(symbol);
  profileElement.innerHTML = `
    <div class="profile-empty">
      <span class="loading-spinner" aria-hidden="true"></span>
      <strong>${escapeHtml(symbol)}</strong>
      <span>Loading profile and fundamentals</span>
    </div>
    ${profileMarketContext(mergedProfileMarketContext(summary), { loading: chartContextLoading })}
  `;
  resetProfileScroll();
  scheduleChartResize();
}

function renderAssetProfile(profile) {
  if (isCryptoAsset(profile.asset_type)) {
    hideProfilePanel();
    return;
  }
  showProfilePanel();
  const metrics = Array.isArray(profile.metrics) ? profile.metrics : [];
  const summary = activeAsset?.summary || findAssetSummary(profile.symbol);
  const name = profile.name || profile.symbol || "Asset";
  const meta = [
    profile.sector,
    profile.industry,
    profile.exchange,
  ].filter(Boolean).join(" / ");
  const description = profile.description || "Company description is not available from the current data source.";
  const hasLongDescription = description.length > 340;

  profileElement.innerHTML = `
    <div class="profile-summary">
      <div class="profile-kicker">Profile</div>
      <h3>${escapeHtml(name)} <span>${escapeHtml(profile.symbol || "")}</span></h3>
      <p class="profile-meta">${escapeHtml(meta || profile.asset_type || "Asset")}</p>
      <p id="profile-description-text" class="profile-description">${escapeHtml(description)}</p>
      ${
        hasLongDescription
          ? '<button class="profile-description-toggle" type="button" aria-expanded="false" aria-controls="profile-description-text">More</button>'
          : ""
      }
    </div>
    <div class="profile-metrics">
      ${
        metrics.length
          ? metrics.map(profileMetric).join("")
          : '<div class="profile-empty small">Fundamentals unavailable for this asset.</div>'
      }
    </div>
    ${profileMarketContext(mergedProfileMarketContext(summary), { loading: chartContextLoading })}
  `;
  resetProfileScroll();
  bindProfileDescriptionToggle();
  scheduleChartResize();
}

function resetProfileScroll() {
  profileElement.scrollTop = 0;
  profileElement.querySelectorAll(".profile-summary, .profile-metrics").forEach((element) => {
    element.scrollTop = 0;
  });
}

function profileMetric(metric) {
  return `
    <div class="profile-metric">
      <span>${escapeHtml(metric.label || "")}</span>
      <strong>${escapeHtml(metric.value || "--")}</strong>
    </div>
  `;
}

function profileMarketContext(summary, options = {}) {
  const range = summary?.range_52w;
  const performance = summary?.performance || {};
  const hasRange = range && typeof range.low === "number" && typeof range.high === "number";
  const perfKeys = ["1D", "1W", "1M", "3M", "YTD", "1Y"];
  const hasPerformance = perfKeys.some((key) => typeof performance[key] === "number");
  const isLoading = Boolean(options.loading);
  if (!hasRange && !hasPerformance && !isLoading) return "";

  return `
    <div class="profile-market-context${isLoading ? " is-loading" : ""}" data-profile-context>
      ${hasRange ? profileRangeBar(range) : ""}
      ${
        hasPerformance
          ? `<div class="profile-performance" aria-label="Performance by timeframe">${perfKeys.map((key) => profilePerformanceCell(key, performance[key])).join("")}</div>`
          : ""
      }
      ${isLoading ? profileContextLoadingMarkup(hasRange || hasPerformance ? "Updating chart context" : "Loading chart context") : ""}
    </div>
  `;
}

function mergedProfileMarketContext(summary) {
  const base = summary && typeof summary === "object" ? summary : {};
  const merged = {
    ...base,
    performance: {
      ...(base.performance || {}),
    },
  };
  if (activeHistoryContext?.performance) {
    merged.performance = {
      ...merged.performance,
      ...activeHistoryContext.performance,
    };
  }
  if (activeHistoryContext?.range_52w) {
    merged.range_52w = activeHistoryContext.range_52w;
  }
  return merged;
}

function updateProfileMarketContext() {
  if (!activeSymbol || profileElement.hidden) return;
  const existing = profileElement.querySelector("[data-profile-context]");
  const summary = activeAsset?.summary || findAssetSummary(activeSymbol);
  const html = profileMarketContext(mergedProfileMarketContext(summary), { loading: chartContextLoading });
  if (existing) {
    if (html) {
      existing.outerHTML = html;
    } else {
      existing.remove();
    }
  } else if (html) {
    profileElement.insertAdjacentHTML("beforeend", html);
  }
  scheduleChartResize();
}

function profileMarketContextFromHistory(rawBars) {
  const rows = normalizeHistoryRows(rawBars);
  if (!rows.length) return {};
  const quote = activeAsset?.quote || {};
  const current = numericOrNull(quote.display_last) ?? numericOrNull(quote.last) ?? rows[rows.length - 1].close;
  const performance = {};
  const quoteChangePct = numericOrNull(quote.display_change_pct) ?? numericOrNull(quote.change_pct);
  if (quoteChangePct !== null) {
    performance["1D"] = quoteChangePct;
  } else {
    addLookbackReturn(performance, "1D", rows, current, 1);
  }
  addLookbackReturn(performance, "1W", rows, current, 7);
  addLookbackReturn(performance, "1M", rows, current, 31);
  addLookbackReturn(performance, "3M", rows, current, 93);
  addYtdReturn(performance, rows, current);
  addLookbackReturn(performance, "1Y", rows, current, 366);
  return { performance };
}

function normalizeHistoryRows(rawBars) {
  return (Array.isArray(rawBars) ? rawBars : [])
    .map((bar) => ({
      timestamp: new Date(bar.timestamp),
      close: Number(bar.close),
    }))
    .filter((bar) => Number.isFinite(bar.timestamp.getTime()) && Number.isFinite(bar.close) && bar.close > 0)
    .sort((a, b) => a.timestamp.getTime() - b.timestamp.getTime());
}

function addLookbackReturn(performance, label, rows, current, lookbackDays) {
  const value = returnFromLookback(rows, current, lookbackDays);
  if (typeof value === "number") performance[label] = value;
}

function returnFromLookback(rows, current, lookbackDays) {
  if (!rows.length || typeof current !== "number" || current <= 0) return null;
  const lastTimestamp = rows[rows.length - 1].timestamp.getTime();
  const target = lastTimestamp - lookbackDays * 24 * 60 * 60 * 1000;
  let reference = null;
  for (const row of rows) {
    if (row.timestamp.getTime() <= target) {
      reference = row.close;
    } else {
      break;
    }
  }
  if (typeof reference !== "number" || reference <= 0) return null;
  return ((current - reference) / reference) * 100;
}

function addYtdReturn(performance, rows, current) {
  if (!rows.length || typeof current !== "number" || current <= 0) return;
  const year = rows[rows.length - 1].timestamp.getUTCFullYear();
  const firstYearIndex = rows.findIndex((row) => row.timestamp.getUTCFullYear() === year);
  if (firstYearIndex < 0) return;
  const reference = firstYearIndex > 0 ? rows[firstYearIndex - 1].close : rows[firstYearIndex].close;
  if (reference > 0) performance.YTD = ((current - reference) / reference) * 100;
}

function chartLoadingMarkup(message) {
  return `
    <div class="chart-loading" role="status" aria-live="polite">
      <span class="loading-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function profileContextLoadingMarkup(message) {
  return `
    <div class="profile-context-loading" role="status" aria-live="polite">
      <span class="loading-spinner" aria-hidden="true"></span>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
}

function profileRangeBar(range) {
  const position = clampNumber(range.position_pct, 0, 100);
  const currency = activeAsset?.quote?.display_currency || activeAsset?.quote?.currency || "USD";
  return `
    <div class="profile-range" style="--range-position: ${position}%">
      <div class="profile-range-head">
        <span>52W Range</span>
        <strong>${formatCurrencyPrice(range.current, currency)}</strong>
      </div>
      <div class="range-track" aria-hidden="true"><span></span></div>
      <div class="range-labels">
        <span>${formatCurrencyPrice(range.low, currency)}</span>
        <span>${formatPlainPct(range.off_low_pct)} above low · ${formatPlainPct(range.off_high_pct)} below high</span>
        <span>${formatCurrencyPrice(range.high, currency)}</span>
      </div>
    </div>
  `;
}

function profilePerformanceCell(label, value) {
  return `
    <span class="performance-cell ${changeClass(value)}" title="${escapeHtml(label)} ${escapeHtml(formatSignedPct(value))}">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(formatSignedPct(value))}</strong>
    </span>
  `;
}

function showProfilePanel() {
  profileElement.hidden = false;
  modalShell.classList.remove("profile-hidden");
  scheduleChartResize();
}

function hideProfilePanel() {
  profileElement.hidden = true;
  modalShell.classList.add("profile-hidden");
  profileElement.replaceChildren();
  scheduleChartResize();
}

function isCryptoAsset(assetType) {
  return String(assetType || "").startsWith("crypto");
}

function bindProfileDescriptionToggle() {
  const toggle = profileElement.querySelector(".profile-description-toggle");
  const description = profileElement.querySelector(".profile-description");
  if (!toggle || !description) return;
  toggle.addEventListener("click", () => {
    const expanded = description.classList.toggle("expanded");
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.textContent = expanded ? "Less" : "More";
    scheduleChartResize();
  });
}

function openDialog(dialog, focusTarget) {
  lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  dialog.classList.add("open");
  dialog.setAttribute("aria-hidden", "false");
  activeDialog = dialog;
  document.body.classList.add("modal-open");
  window.requestAnimationFrame(() => {
    const target = focusTarget || firstFocusableElement(dialog);
    target?.focus();
  });
}

function closeDialog(dialog) {
  if (!dialog.classList.contains("open")) return false;
  dialog.classList.remove("open");
  dialog.setAttribute("aria-hidden", "true");
  if (activeDialog === dialog) activeDialog = null;
  if (!document.querySelector(".modal.open")) document.body.classList.remove("modal-open");
  const returnTarget = dialogReturnTarget(lastFocusedElement);
  if (returnTarget) {
    returnTarget.focus();
  }
  lastFocusedElement = null;
  return true;
}

function dialogReturnTarget(element) {
  if (!element) return null;
  if (document.contains(element)) return element;
  const symbol = element.dataset?.symbol;
  if (!symbol) return null;
  return (
    Array.from(document.querySelectorAll(".asset-row")).find((row) => row.dataset.symbol === symbol) || null
  );
}

function trapDialogFocus(event, dialog) {
  const focusable = focusableElements(dialog);
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!dialog.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function firstFocusableElement(container) {
  return focusableElements(container)[0] || null;
}

function focusableElements(container) {
  const selector = [
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "a[href]",
    '[tabindex]:not([tabindex="-1"])',
  ].join(",");
  return Array.from(container.querySelectorAll(selector)).filter((element) => {
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden";
  });
}

function findAssetSummary(symbol) {
  if (!symbol || !latestData?.groups) return {};
  for (const group of latestData.groups) {
    const asset = (group.assets || []).find((item) => item.symbol === symbol);
    if (asset) return asset.summary || {};
  }
  return {};
}

function numericOrNull(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value));
  return String(value).replaceAll('"', '\\"').replaceAll("\\", "\\\\");
}

function isTextInput(target) {
  if (!(target instanceof HTMLElement)) return false;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable;
}

function setConnection(state) {
  statusStrip.classList.toggle("live", state === "live");
  statusStrip.classList.toggle("error", state === "error");
  statusStrip.classList.toggle("connecting", state === "connecting");
  connectionState.classList.toggle("live", state === "live");
  connectionState.classList.toggle("error", state === "error");
}

function changeClass(value) {
  if (typeof value === "number" && value > 0) return "change-positive";
  if (typeof value === "number" && value < 0) return "change-negative";
  return "change-flat";
}

function formatPrice(value, error) {
  if (error || typeof value !== "number" || value === 0) return "--";
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(value) >= 1) return value.toFixed(2);
  return value.toPrecision(4);
}

function formatUsdPrice(value) {
  const price = formatPrice(value);
  return price === "--" ? price : `$${price}`;
}

function formatBoardPrice(value, error, currency) {
  if (error || typeof value !== "number" || value === 0) return "--";
  if (!currency || currency === "USD") return formatPrice(value);
  return `${currencyPrefix(currency)}${formatCompactPrice(value)}`;
}

function formatCurrencyPrice(value, currency = "USD") {
  if (typeof value !== "number" || value === 0) return "--";
  const prefix = currencyPrefix(currency);
  if (currency && currency !== "USD") return `${prefix}${formatCompactPrice(value)}`;
  return `${prefix}${formatPrice(value)}`;
}

function formatSigned(value) {
  if (typeof value !== "number") return "--";
  const abs = Math.abs(value);
  const formatted = abs >= 100 ? abs.toFixed(1) : abs.toFixed(2);
  return `${value >= 0 ? "+" : "-"}${formatted}`;
}

function formatBoardSignedChange(value, currency) {
  if (typeof value !== "number") return "--";
  if (!currency || currency === "USD") return formatSigned(value);
  return `${value >= 0 ? "+" : "-"}${currencyPrefix(currency)}${formatCompactPrice(Math.abs(value))}`;
}

function formatSignedPct(value) {
  if (typeof value !== "number") return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function formatPlainPct(value) {
  if (typeof value !== "number") return "--";
  return `${value.toFixed(1)}%`;
}

function formatSignedNumber(value) {
  if (typeof value !== "number") return "--";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

function formatCompactPrice(value) {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1000) return `${(abs / 1000).toFixed(abs >= 100_000 ? 1 : 2)}K`;
  return formatPrice(abs);
}

function currencyPrefix(currency) {
  return {
    KRW: "₩",
    JPY: "¥",
    EUR: "€",
    GBP: "£",
    USD: "$",
  }[currency] || `${currency} `;
}

function formatUsdFlow(value) {
  if (typeof value !== "number") return "--";
  const abs = Math.abs(value);
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function formatFlowDate(value) {
  if (!value) return "--";
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function formatInteger(value) {
  return typeof value === "number" ? Math.round(value).toString() : "--";
}

function scorePercent(value) {
  if (typeof value !== "number") return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

function clampNumber(value, min, max) {
  if (typeof value !== "number" || !Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

function formatClock(date) {
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZoneName: "short",
  });
}

function formatLocalDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function displayGroupName(value) {
  if (value === "__ALL__") return "All Markets";
  return String(value || "--").replaceAll("_", " ");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toChartTime(value, interval) {
  if (interval === "1d") return value.slice(0, 10);
  return Math.floor(new Date(value).getTime() / 1000);
}
