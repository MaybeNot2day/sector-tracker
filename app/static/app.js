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
const categoryButtons = Array.from(document.querySelectorAll(".category-tabs button"));
const cryptoTapeElement = document.querySelector("#crypto-tape");
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
const macroStrip = document.querySelector("#macro-strip");
const newsPanel = document.querySelector("#news-panel");
const newsList = document.querySelector("#news-list");
const newsStatus = document.querySelector("#news-status");
const newsToggle = document.querySelector("#news-toggle");
const newsClose = document.querySelector("#news-close");

let latestData = null;
let latestCryptoEtfFlows = null;
let latestSnapshots = null;
let watchlistConfig = null;
let activeSymbol = null;
let activeAsset = null;
let activeHistoryContext = null;
let activeRange = "1y";
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
let marketCategory = "tradfi"; // "tradfi" | "crypto"
let tapeSorts = {}; // per-basket { key, direction }
let tapePages = {}; // per-basket page index
let lastTapeRenderKey = "";
let feedMode = "poll"; // "ws" locally, "poll" on serverless deployments
let activeView = "daily";
let pendingChartFromUrl = null;
let restoringUrlState = false;
const BOARD_CACHE_KEY = "board-cache-v1";
let latestNews = null;
let knownNewsIds = new Set();
const NEWS_OPEN_KEY = "news-open";
const BOARD_CACHE_MAX_AGE_MS = 24 * 3600 * 1000;
let dataIsCached = false;

const sourceLabels = {
  yahoo: "YH",
  lighter: "LTR",
  stooq: "STQ",
  finnhub: "FH",
};

// --- Display timezone ------------------------------------------------------
// All human-readable times render in Central European Time regardless of the
// viewer's machine. The IANA zone handles DST, so labels read CET or CEST.
const DISPLAY_TIME_ZONE = "Europe/Berlin";

const displayDateFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const displayTzOffsetFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: DISPLAY_TIME_ZONE,
  timeZoneName: "longOffset",
});
const displayTzOffsetCache = new Map();

// Offset of the display zone at `date`, in seconds. Memoized per hour since
// DST transitions land on hour boundaries; called once per chart bar.
function displayTzOffsetSeconds(date) {
  const hourKey = Math.floor(date.getTime() / 3600000);
  const cached = displayTzOffsetCache.get(hourKey);
  if (cached !== undefined) return cached;
  const name =
    displayTzOffsetFmt.formatToParts(date).find((part) => part.type === "timeZoneName")?.value ||
    "";
  const match = name.match(/GMT([+-])(\d{2}):(\d{2})/);
  const seconds = match
    ? (match[1] === "-" ? -1 : 1) * (Number(match[2]) * 3600 + Number(match[3]) * 60)
    : 0;
  displayTzOffsetCache.set(hourKey, seconds);
  return seconds;
}

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
  // All-crypto groups (Majors) get no session chip: 24/7 is the default
  // state for perps and the label was just noise.
  if (keys.every((key) => key === "crypto")) return null;
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
  if (marketCategory !== "tradfi") params.set("cat", marketCategory);
  if (activeSymbol) {
    params.set("chart", activeSymbol);
    if (activeInterval !== "1d") params.set("tf", activeInterval);
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
    if (params.get("cat") === "crypto") {
      marketCategory = "crypto";
      updateCategoryButtons();
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
        interval: params.get("tf") || "1d",
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
  const { symbol, interval } = pendingChartFromUrl;
  const asset = findAssetConfig(symbol);
  pendingChartFromUrl = null;
  if (asset) openChart(asset, { interval });
}


function init() {
  // icons are inline SVG; no icon library needed
  setConnection("connecting");
  restoreUrlState();
  restoreCachedBoard();
  fetchQuotes();
  fetchCryptoEtfFlows();
  fetchSnapshots();
  setNewsOpen(localStorage.getItem(NEWS_OPEN_KEY) === "1");
  fetchNews();
  feedMode = shouldUseWebSocket() ? "ws" : "poll";
  updateFeedModeLabel();
  if (feedMode === "ws") openSocket();
  // Poll only while the tab is visible; a hidden tab otherwise burns
  // ~5.7k serverless invocations/day for nothing.
  window.setInterval(() => {
    if (!document.hidden) fetchQuotes();
  }, 10000);
  window.setInterval(() => {
    if (!document.hidden) {
      fetchCryptoEtfFlows();
      fetchSnapshots();
    }
  }, 300000);
  // WS pushes news instantly; polling is the fallback for serverless hosts.
  window.setInterval(() => {
    if (!document.hidden && feedMode !== "ws") fetchNews();
  }, 20000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      fetchQuotes();
      fetchCryptoEtfFlows();
    }
  });
  newsToggle.addEventListener("click", () => setNewsOpen(!document.body.classList.contains("news-open")));
  newsClose.addEventListener("click", () => setNewsOpen(false));
  refreshButton.addEventListener("click", () => {
    fetchQuotes();
    fetchCryptoEtfFlows();
    fetchSnapshots();
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
  categoryButtons.forEach((button) => {
    button.addEventListener("click", () => selectCategory(button.dataset.category || "tradfi"));
  });
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
      activeRange = button.dataset.range || "1y";
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

function selectCategory(category) {
  if (category === marketCategory) return;
  marketCategory = category;
  activeGroupFilter = "";
  updateCategoryButtons();
  renderBoard(latestData);
  syncUrlState();
}

function updateCategoryButtons() {
  categoryButtons.forEach((button) => {
    const active = (button.dataset.category || "tradfi") === marketCategory;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

function groupCategory(group) {
  return (group.assets || []).some((asset) => isCryptoAsset(asset.type)) ? "crypto" : "tradfi";
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
// --- Instant first paint -------------------------------------------------
// A cold serverless instance can take many seconds to answer the first
// /api/quotes (Yahoo throttling + fresh fetch). Persist the last good board
// and paint it immediately on load, flagged as cached until live data lands.
function restoreCachedBoard() {
  try {
    const raw = localStorage.getItem(BOARD_CACHE_KEY);
    if (!raw) return;
    const { at, payload } = JSON.parse(raw);
    if (!payload?.groups || Date.now() - at > BOARD_CACHE_MAX_AGE_MS) return;
    dataIsCached = true;
    applyQuotes(payload);
  } catch (error) {
    /* corrupt cache is not worth surfacing */
  }
}

function persistBoardCache(payload) {
  try {
    localStorage.setItem(BOARD_CACHE_KEY, JSON.stringify({ at: Date.now(), payload }));
  } catch (error) {
    /* quota exceeded / private mode — cache is best-effort */
  }
}


async function fetchQuotes() {
  refreshButton.classList.add("loading");
  try {
    const response = await fetch("/api/quotes");
    if (!response.ok) throw new Error("quotes_failed");
    const payload = await response.json();
    dataIsCached = false;
    applyQuotes(payload);
    persistBoardCache(payload);
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
  // Serverless (Vercel) can't hold sockets; every long-lived host can.
  // On Railway/VPS the board streams over WS instead of 10s polling.
  return !window.location.hostname.endsWith(".vercel.app");
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
      dataIsCached = false;
      applyQuotes(message.data);
      persistBoardCache(message.data);
      setConnection("live");
    } else if (message.type === "news") {
      renderNews(message.data);
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
  feedModeLabel.textContent = feedMode === "ws" ? "WS Live" : "Poll 10s";
  feedModeLabel.title =
    feedMode === "ws"
      ? "Streaming over WebSocket"
      : "Quotes refresh by HTTP poll every 10 seconds";
}

function applyQuotes(payload) {
  rememberAndPatchFunding(payload);
  latestData = payload;
  renderBoard(payload);
  renderMacroStrip(payload.macro);
  renderDailyBoard(payload.overview, latestCryptoEtfFlows);
  updateHeader(payload.overview);
  openPendingChartFromUrl();
}

// --- Funding stickiness ----------------------------------------------------
// On serverless deployments each poll can hit a different instance, and any
// instance whose /funding-rates fetch got rate-limited serves nulls — so
// funding flickered in and out. Funding moves hourly; retain the last known
// value per symbol and patch payloads that arrive without it.
const fundingMemory = new Map(); // symbol -> { rate, oi, at }
const FUNDING_MEMORY_MAX_AGE_MS = 30 * 60 * 1000;

function rememberAndPatchFunding(payload) {
  const now = Date.now();
  const patch = (target, symbol) => {
    if (!target || !symbol) return;
    if (typeof target.funding_rate === "number") {
      fundingMemory.set(symbol, {
        rate: target.funding_rate,
        oi: typeof target.open_interest_usd === "number" ? target.open_interest_usd : null,
        at: now,
      });
      return;
    }
    const kept = fundingMemory.get(symbol);
    if (!kept || now - kept.at > FUNDING_MEMORY_MAX_AGE_MS) return;
    target.funding_rate = kept.rate;
    if (typeof target.open_interest_usd !== "number" && kept.oi !== null) {
      target.open_interest_usd = kept.oi;
    }
  };
  (payload.groups || []).forEach((group) => {
    (group.assets || []).forEach((asset) => {
      if (isCryptoAsset(asset.type)) patch(asset.quote, asset.symbol);
    });
  });
  (payload.crypto_tape || []).forEach((row) => patch(row, row.symbol));
  // The backend computes the breadth funding share from its own (possibly
  // funding-less) tape; recompute it from the patched rows when missing.
  const breadth = payload.overview?.crypto_breadth;
  if (breadth && typeof breadth.positive_funding_pct !== "number") {
    const rates = (payload.crypto_tape || [])
      .map((row) => row.funding_rate)
      .filter((value) => typeof value === "number");
    if (rates.length) {
      breadth.positive_funding_pct =
        Math.round((rates.filter((value) => value > 0).length / rates.length) * 1000) / 10;
    }
  }
}

// --- Live news drawer -------------------------------------------------------
// Merged feed of public Telegram channels, scraped server-side from their
// t.me previews. New posts arrive over the quotes WebSocket within one poll
// interval; HTTP polling covers hosts without a socket.
function setNewsOpen(open) {
  document.body.classList.toggle("news-open", open);
  newsToggle.setAttribute("aria-pressed", String(open));
  localStorage.setItem(NEWS_OPEN_KEY, open ? "1" : "0");
}

async function fetchNews() {
  try {
    const response = await fetch("/api/news");
    if (!response.ok) throw new Error("news_failed");
    renderNews(await response.json());
  } catch (error) {
    if (!latestNews) {
      newsList.innerHTML = '<div class="empty-state">News feed unavailable</div>';
    }
  }
}

function renderNews(payload) {
  const items = payload?.items || [];
  latestNews = payload;
  const updated = new Date(payload?.updated_at || Date.now());
  newsStatus.textContent = Number.isNaN(updated.getTime()) ? "" : formatClock(updated);
  if (!items.length) {
    newsList.innerHTML = '<div class="empty-state">No posts yet</div>';
    return;
  }
  const seenBefore = knownNewsIds.size > 0;
  newsList.innerHTML = items.map((item) => newsItemMarkup(item, seenBefore)).join("");
  knownNewsIds = new Set(items.map((item) => item.id));
}

function newsItemMarkup(item, seenBefore) {
  const fresh = seenBefore && !knownNewsIds.has(item.id);
  return `<a class="news-item${fresh ? " news-new" : ""}" href="${escapeHtml(item.link)}" target="_blank" rel="noopener">
    <div class="news-meta">
      <strong>${escapeHtml(item.channel_title || item.channel)}</strong>
      <time title="${escapeHtml(item.timestamp)}">${escapeHtml(newsAge(item.timestamp))}</time>
    </div>
    <p>${escapeHtml(item.text)}</p>
  </a>`;
}

function newsAge(timestamp) {
  const stamp = Date.parse(timestamp || "");
  if (Number.isNaN(stamp)) return "";
  const seconds = Math.max(0, (Date.now() - stamp) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
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

async function fetchSnapshots() {
  try {
    const response = await fetch("/api/snapshots?days=30");
    if (!response.ok) throw new Error("snapshots_failed");
    const payload = await response.json();
    latestSnapshots = Array.isArray(payload.snapshots) ? payload.snapshots : [];
  } catch (error) {
    latestSnapshots = latestSnapshots || [];
  }
  if (latestData?.overview) renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
}

// --- Macro tape ------------------------------------------------------------
// VIX / DXY / US10Y context strip. These symbols are polled alongside the
// watchlists but stay out of the universe, so breadth metrics are unaffected.
function renderMacroStrip(items) {
  if (!macroStrip) return;
  if (!Array.isArray(items) || !items.length) {
    macroStrip.hidden = true;
    return;
  }
  macroStrip.innerHTML = items.map(macroItem).join("");
  macroStrip.hidden = false;
}

function macroItem(item) {
  const isYield = item.unit === "yield";
  const value =
    typeof item.last === "number"
      ? isYield
        ? `${item.last.toFixed(2)}%`
        : formatPrice(item.last)
      : "--";
  const change = isYield
    ? typeof item.change_abs === "number"
      ? `${formatSigned(item.change_abs * 100)}bp`
      : "--"
    : formatSignedPct(item.change_pct);
  // VIX up is risk-off: flip the tone so red means rising volatility.
  let tone = changeClass(isYield ? item.change_abs : item.change_pct);
  if (item.invert_tone) {
    tone =
      tone === "change-positive"
        ? "change-negative"
        : tone === "change-negative"
          ? "change-positive"
          : tone;
  }
  const title = isYield ? `${item.label} yield · 1D change in bp` : `${item.label} · 1D change`;
  return `<span class="macro-item${item.is_stale ? " stale" : ""}" title="${escapeHtml(title)}"><label>${escapeHtml(item.label)}</label><strong>${escapeHtml(value)}</strong><em class="${tone}">${escapeHtml(change)}</em></span>`;
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
    dataIsCached ? "CACHED VIEW · REFRESHING" : feedMode === "ws" ? "LIVE QUOTES" : "POLLED QUOTES",
    usSession ? `US ${SESSION_STATE_COPY[usSession.state].toUpperCase()}` : null,
    `${universe.quoted || 0}/${universe.total || 0} QUOTED`,
    `HISTORY ${universe.history_count || 0}/${universe.total || 0}`,
    `FLOWS ${flowStatusLabel(latestCryptoEtfFlows)}`,
    `UPDATED ${time}`,
  ].filter(Boolean).join(" · ");
}

let lastDailyRenderKey = "";

function renderDailyBoard(overview, cryptoEtfFlows) {
  if (!overview) {
    dailyBoard.innerHTML = '<div class="empty-state">Market read unavailable</div>';
    lastDailyRenderKey = "";
    return;
  }
  // Rebuilding ~6 panels of innerHTML every poll costs parse + layout and
  // drops hover state; skip when the data is byte-identical.
  const renderKey = JSON.stringify([overview, cryptoEtfFlows?.status, cryptoEtfFlows?.updated_at, latestSnapshots]);
  if (renderKey === lastDailyRenderKey) return;
  lastDailyRenderKey = renderKey;

  const prevScores = previousThemeScores();

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
        "The market's overall mood, read from the whole watchlist. RISK-ON = most names rising, RISK-OFF = most falling, MIXED = no clear side. BROAD means most stocks join the move; NARROW means only a few drive it. The small numbers show the share of names trading above their 50- and 200-day average prices — a health check of the trend."
      )}
      <div class="regime-grid">
        ${regimeCell(
          "Regime",
          regime.label || "--",
          `${formatPlainPct(universe.above_50dma_pct)} > 50DMA · ${formatPlainPct(universe.above_200dma_pct)} > 200DMA`,
          `tone-${regime.tone || "neutral"}`
        )}
        ${vixRegimeCell(regime.vix)}
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

    <div class="analytics-grid triple">
      <section class="analytics-panel">
        ${panelHeading(
          "Benchmarks",
          "Return / Dist 50DMA / ATR Ext",
          "The big reference ETFs (S&P 500, Nasdaq, semis, bonds, gold, oil). 1D/5D = return over one/five days. >50DMA = how far price sits from its 50-day average — above zero means uptrend. ATR ext = distance from the 20-day average measured in units of typical daily movement; beyond ±2 the move is stretched and often due for a pause."
        )}
        <div class="benchmark-grid">
          ${benchmarks.map(benchmarkCard).join("") || '<div class="empty-state">Add ETF_MACRO benchmarks</div>'}
        </div>
      </section>

      <section class="analytics-panel">
        ${panelHeading(
          "Breadth",
          `${universe.history_count || 0} names with history`,
          "How many of the tracked names take part in the move — one strong stock can mask a weak market. % > 20/50/200DMA = share of names above their short/medium/long-term average price. New 20-day highs/lows and ±3% movers show how forceful today is. Healthy rallies have broad participation."
        )}
        <div class="breadth-grid">
          ${breadthRow("% > 20DMA", formatPlainPct(universe.above_20dma_pct))}
          ${breadthRow("% > 50DMA", formatPlainPct(universe.above_50dma_pct))}
          ${breadthRow("% > 200DMA", formatPlainPct(universe.above_200dma_pct))}
          ${breadthTrendRow()}
          ${breadthRow("Total names", formatInteger(universe.total))}
          ${breadthRow("New 20D highs", formatInteger(universe.highs_20d), "positive")}
          ${breadthRow("New 20D lows", formatInteger(universe.lows_20d), "negative")}
          ${breadthRow("Up 3%+", formatInteger(universe.up_3pct), "positive")}
          ${breadthRow("Down 3%+", formatInteger(universe.down_3pct), "negative")}
        </div>
      </section>

      ${cryptoBreadthPanel(overview.crypto_breadth)}
    </div>

    <div class="analytics-grid equal">
      <section class="analytics-panel">
        ${panelHeading(
          "Dominant Themes",
          `Top ${Math.min(8, themes.length)} of ${themes.length} by score`,
          "Each watchlist sector scored 0-100: today's move and the 5-day move carry most weight, plus how many members are rising and in uptrends. \u0394 = score change vs yesterday. Labels: \u226575 DOMINANT, \u226562 STRONG, \u226552 EMERGING, \u226545 NEUTRAL, below that DETERIORATING / FADING."
        )}
        ${themeTable(themes.slice(0, 8), "score", prevScores)}
      </section>
      <section class="analytics-panel">
        ${panelHeading(
          "Momentum Shifts",
          "Largest \u0394 pace today",
          "Which sectors are speeding up or slowing down right now. \u0394 pace = today's move minus the average daily move of the last five days, in %-points. Positive = accelerating beyond its recent trend; negative = losing steam even if still up on the week."
        )}
        ${themeTable(movers, "momentum")}
      </section>
    </div>

    <section class="analytics-panel">
      ${panelHeading(
        "Crypto ETF Flows",
        cryptoEtfFlowNote(cryptoEtfFlows),
        "Daily net money moving into (+) or out of (\u2212) the US spot Bitcoin, Ether and Solana ETFs, from Farside data. Inflows mean investors are buying ETF shares and the funds must buy the coins. 5D/10D = flows summed over the last 5 and 10 trading days."
      )}
      ${cryptoEtfFlowPanel(cryptoEtfFlows)}
    </section>

    <section class="analytics-panel">
      ${panelHeading(
        "Theme Rotation",
        "1D move versus 5D daily pace",
        "Money rotating between sectors. Climbers trade faster than their own 5-day pace today - attention is arriving; fallers trade slower - attention is leaving. Pace is in %-points per day, so it spots turns earlier than raw returns."
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
  // Explanations were invisible native title-tooltips; a visible ? badge
  // with a styled popover makes them discoverable (hover, or tap on phones
  // via tabindex focus).
  const help = tip
    ? `<button type="button" class="help-tip" aria-label="What is ${escapeHtml(title)}?" data-tip="${escapeHtml(tip)}">?</button>`
    : "";
  return `<header class="panel-heading"><h2>${escapeHtml(title)}${help}</h2><span>${escapeHtml(note || "")}</span></header>`;
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

function vixRegimeCell(vix) {
  if (!vix) return regimeCell("Volatility", "--", "No VIX read");
  const level = typeof vix.level === "number" ? vix.level.toFixed(1) : "--";
  return regimeCell(
    "Volatility",
    `VIX ${level}`,
    `${vix.state || ""} · 1D ${formatSignedPct(vix.change_pct)}`,
    `tone-${vix.tone || "neutral"}`
  );
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

function cryptoBreadthPanel(breadth) {
  const cb = breadth || {};
  const medianTone =
    typeof cb.median_change === "number"
      ? cb.median_change > 0
        ? "positive"
        : cb.median_change < 0
          ? "negative"
          : ""
      : "";
  return `<section class="analytics-panel">
    ${panelHeading(
      "Crypto Breadth",
      `${formatInteger(cb.total)} Lighter perps`,
      "Same participation check for the whole crypto market: every perp listed on Lighter. Median 1D = the typical coin's day. Funding > 0 = share of markets where longs pay shorts, a proxy for bullish positioning. High advance % with high funding = crowded optimism."
    )}
    <div class="breadth-grid">
      ${breadthRow("Median 1D", formatSignedPct(cb.median_change), medianTone)}
      ${breadthRow("Advance %", formatPlainPct(cb.advance_pct))}
      ${breadthRow("Up 3%+", formatInteger(cb.up_3pct), "positive")}
      ${breadthRow("Down 3%+", formatInteger(cb.down_3pct), "negative")}
      ${breadthRow("Up 10%+", formatInteger(cb.up_10pct), "positive")}
      ${breadthRow("Down 10%+", formatInteger(cb.down_10pct), "negative")}
      ${breadthRow("24h volume", typeof cb.volume_usd === "number" ? `$${formatCompactPrice(cb.volume_usd)}` : "--")}
      ${breadthRow("Funding > 0", formatPlainPct(cb.positive_funding_pct))}
    </div>
  </section>`;
}

function breadthTrendRow() {
  const series = (latestSnapshots || [])
    .map((snap) => numericOrNull(snap.universe?.above_50dma_pct))
    .filter((value) => value !== null);
  if (series.length < 2) return "";
  return `<div class="breadth-row" title="% of universe above 50DMA across the last ${series.length} daily snapshots"><span>50DMA trend</span><span class="breadth-spark">${sparklineSvg(series)}</span></div>`;
}

function previousThemeScores() {
  if (!Array.isArray(latestSnapshots) || !latestSnapshots.length) return null;
  const today = String(latestData?.overview?.as_of || "").slice(0, 10);
  for (let index = latestSnapshots.length - 1; index >= 0; index -= 1) {
    const snap = latestSnapshots[index];
    if (!snap?.date || (today && snap.date >= today)) continue;
    if (!Array.isArray(snap.themes) || !snap.themes.length) continue;
    const scores = {};
    for (const theme of snap.themes) {
      if (theme?.name && typeof theme.score === "number") scores[theme.name] = theme.score;
    }
    return scores;
  }
  return null;
}

function themeTable(themes, variant = "score", prevScores = null) {
  const momentum = variant === "momentum";
  const third = momentum ? "\u0394 Pace" : "Score";
  const deltaHead = momentum
    ? ""
    : '<th title="Score change vs the prior session snapshot">\u0394</th>';
  const columns = momentum ? 6 : 7;
  return `<table class="theme-table">
    <thead><tr><th>#</th><th>Theme</th><th>${third}</th>${deltaHead}<th>1D</th><th>5D</th><th>Status</th></tr></thead>
    <tbody>${themes.map((theme) => themeRow(theme, momentum, prevScores)).join("") || `<tr><td colspan="${columns}">No themes configured</td></tr>`}</tbody>
  </table>`;
}

function themeRow(theme, momentum = false, prevScores = null) {
  const score = scorePercent(theme.score);
  const third = momentum
    ? `<td class="${changeClass(theme.acceleration)}" title="1D move minus 5D daily pace">${formatSignedNumber(theme.acceleration)}</td>`
    : `<td><span class="score-bar" style="--score: ${score}%"><span class="score-value">${formatInteger(theme.score)}</span></span></td>`;
  const deltaCell = momentum ? "" : themeDeltaCell(theme, prevScores);
  return `<tr>
    <td>${formatInteger(theme.rank)}</td>
    <td><button class="theme-link" type="button" data-group="${escapeHtml(theme.name)}" title="Show ${escapeHtml(displayGroupName(theme.name))} in Markets">${escapeHtml(displayGroupName(theme.name))}</button><span class="member-count">${formatInteger(theme.count)}</span></td>
    ${third}
    ${deltaCell}
    <td class="${changeClass(theme.change_1d)}">${formatSignedPct(theme.change_1d)}</td>
    <td class="${changeClass(theme.change_5d)}">${formatSignedPct(theme.change_5d)}</td>
    <td><span class="status-tag status-${String(theme.status || "neutral").toLowerCase()}">${escapeHtml(theme.status || "NEUTRAL")}</span></td>
  </tr>`;
}

function themeDeltaCell(theme, prevScores) {
  const prev =
    prevScores && typeof prevScores[theme.name] === "number" ? prevScores[theme.name] : null;
  if (prev === null || typeof theme.score !== "number") {
    return '<td class="theme-delta">--</td>';
  }
  const delta = theme.score - prev;
  const text = delta > 0 ? `+${delta}` : String(delta);
  return `<td class="theme-delta ${changeClass(delta)}" title="Score vs prior session (${prev})">${text}</td>`;
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
  const categoryGroups = (payload.groups || []).filter(
    (group) => groupCategory(group) === marketCategory
  );
  const groups = marketLayout === "flat" ? flatGroups(categoryGroups) : visibleGroups(categoryGroups);
  board.classList.remove("board-loading");
  board.classList.toggle("flat", marketLayout === "flat");
  const showTape = marketCategory === "crypto";
  // Wider masonry columns fit the tape's six data columns; the tape flows
  // through the same multicol container as the Majors panel (display:
  // contents), so basket heights pack instead of leaving grid holes.
  board.classList.toggle("board-crypto", showTape);
  cryptoTapeElement.hidden = !showTape;
  const tapeCounts = showTape ? renderCryptoTape(payload.crypto_tape || []) : { visible: 0, total: 0 };
  if (!groups.length) {
    const totalAssets = countAssets(categoryGroups) + tapeCounts.total;
    const hasFilter = activeGroupFilter || marketSearchQuery;
    board.innerHTML =
      tapeCounts.visible > 0
        ? ""
        : `<div class="empty-state">${hasFilter ? "No matching markets" : "No groups configured"}</div>`;
    if (showTape && tapeCounts.visible > 0) board.appendChild(cryptoTapeElement);
    updateMarketFilterStatus(tapeCounts.visible, totalAssets);
    return;
  }

  const totalAssets = countAssets(categoryGroups) + tapeCounts.total;
  const visibleAssets = countAssets(groups) + tapeCounts.visible;
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

  board.querySelectorAll(".group-panel:not(.tape-panel)").forEach((panel) => {
    if (!nextGroups.has(panel.dataset.group)) panel.remove();
  });
  if (showTape) board.appendChild(cryptoTapeElement);
  updateSortHeaders();
  updateMarketFilterStatus(visibleAssets, totalAssets);
}

// --- Crypto tape -----------------------------------------------------------
// Every crypto perp on Lighter, auto-synced from the exchange: no YAML entry
// needed, new listings appear on their own. Rows are quote-only (funding, OI,
// 24h volume); clicking one opens the chart via on-demand Lighter candles.
const TAPE_SORT_KEYS = {
  symbol: (row) => row.symbol,
  last: (row) => numericOrNull(row.last),
  pct: (row) => numericOrNull(row.change_pct),
  funding: (row) => numericOrNull(row.funding_rate),
  oi: (row) => numericOrNull(row.open_interest_usd),
  volume: (row) => numericOrNull(row.day_volume_usd),
};

// Panel order mirrors Lighter's app baskets; "Other" catches untagged tails.
const TAPE_BASKET_ORDER = ["L1", "DeFi", "AI", "L2", "Memes", "Other"];

// Big baskets (DeFi ~31, L1 ~24) paginate so panels stay scannable.
const TAPE_PAGE_SIZE = 15;
const DEFAULT_TAPE_SORT = { key: "volume", direction: "desc" };

function basketSort(basket) {
  return tapeSorts[basket] || DEFAULT_TAPE_SORT;
}

function renderCryptoTape(tape) {
  const configured = new Set();
  (latestData?.groups || []).forEach((group) => {
    if (groupCategory(group) === "crypto") {
      (group.assets || []).forEach((asset) => configured.add(asset.symbol));
    }
  });
  const rows = tape.filter((row) => !configured.has(row.symbol));
  const query = marketSearchQuery.toLowerCase();
  const visible = query
    ? rows.filter((row) => matchesTapeQuery(row, query))
    : rows;
  const counts = { visible: visible.length, total: rows.length };

  const renderKey = JSON.stringify([tape, tapeSorts, tapePages, query, configured.size]);
  if (renderKey === lastTapeRenderKey) return counts;
  lastTapeRenderKey = renderKey;

  if (!visible.length) {
    cryptoTapeElement.innerHTML = rows.length
      ? '<div class="empty-state">No matching perps</div>'
      : "";
    return counts;
  }

  const baskets = new Map();
  visible.forEach((row) => {
    const basket = TAPE_BASKET_ORDER.includes(row.basket) ? row.basket : "Other";
    if (!baskets.has(basket)) baskets.set(basket, []);
    baskets.get(basket).push(row);
  });
  cryptoTapeElement.innerHTML = TAPE_BASKET_ORDER.filter((basket) => baskets.has(basket))
    .map((basket) => tapeBasketMarkup(basket, sortedTapeRows(baskets.get(basket), basketSort(basket))))
    .join("");
  cryptoTapeElement.querySelectorAll(".group-title button").forEach((button) => {
    button.addEventListener("click", () => {
      const basket = button.closest(".tape-panel")?.dataset.basket || "Other";
      setTapeSort(basket, button.dataset.sortKey || "volume");
    });
  });
  cryptoTapeElement.querySelectorAll(".tape-pager button").forEach((button) => {
    button.addEventListener("click", () => {
      const basket = button.closest(".tape-panel")?.dataset.basket || "Other";
      tapePages[basket] = Math.max(0, (tapePages[basket] || 0) + Number(button.dataset.step || 0));
      renderBoard(latestData);
    });
  });
  cryptoTapeElement.querySelectorAll(".asset-row").forEach((row) => {
    row.addEventListener("click", () => openTapeChart(row.dataset.symbol || ""));
  });
  return counts;
}

function matchesTapeQuery(row, query) {
  return (
    row.symbol.toLowerCase().includes(query) ||
    String(row.basket || "").toLowerCase().includes(query)
  );
}

function tapeBasketMarkup(basket, rows) {
  const pageCount = Math.max(1, Math.ceil(rows.length / TAPE_PAGE_SIZE));
  const page = Math.min(tapePages[basket] || 0, pageCount - 1);
  tapePages[basket] = page;
  const start = page * TAPE_PAGE_SIZE;
  const pageRows = rows.slice(start, start + TAPE_PAGE_SIZE);
  const sort = basketSort(basket);
  const header = (label, sortKey) => tapeHeaderButton(label, sortKey, sort);
  const pager =
    pageCount > 1
      ? `<div class="tape-pager">
          <button type="button" data-step="-1" ${page === 0 ? "disabled" : ""} aria-label="Previous page">‹</button>
          <span>${start + 1}–${start + pageRows.length} of ${rows.length}</span>
          <button type="button" data-step="1" ${page >= pageCount - 1 ? "disabled" : ""} aria-label="Next page">›</button>
        </div>`
      : "";
  return `<section class="group-panel tape-panel" data-basket="${escapeHtml(basket)}">
    <div class="group-title">
      <span>${header(basket, "symbol")}<em class="session-chip" data-state="open" title="${rows.length} perps · Lighter basket · trades 24/7">${rows.length}</em></span>
      <span>${header("Last", "last")}</span>
      <span>${header("1D %", "pct")}</span>
      <span>${header("Fund", "funding")}</span>
      <span>${header("OI", "oi")}</span>
      <span>${header("24h Vol", "volume")}</span>
    </div>
    ${pageRows.map(tapeRowMarkup).join("")}
    ${pager}
  </section>`;
}

function tapeHeaderButton(label, sortKey, sort) {
  const active = sort.key === sortKey;
  const ariaSort = active ? (sort.direction === "asc" ? "ascending" : "descending") : "none";
  return `<button type="button" data-sort-key="${sortKey}" class="${active ? "active-sort" : ""}" aria-sort="${ariaSort}" title="Sort by ${escapeHtml(label)}">${escapeHtml(label)}</button>`;
}

function setTapeSort(basket, sortKey) {
  const current = basketSort(basket);
  tapeSorts[basket] =
    current.key === sortKey
      ? { key: sortKey, direction: current.direction === "asc" ? "desc" : "asc" }
      : { key: sortKey, direction: sortKey === "symbol" ? "asc" : "desc" };
  tapePages[basket] = 0;
  renderBoard(latestData);
}

function sortedTapeRows(rows, sort) {
  const accessor = TAPE_SORT_KEYS[sort.key] || TAPE_SORT_KEYS.volume;
  const direction = sort.direction === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const aValue = accessor(a);
    const bValue = accessor(b);
    if (typeof aValue === "string" || typeof bValue === "string") {
      return String(aValue).localeCompare(String(bValue)) * direction;
    }
    if (aValue === bValue) return a.symbol.localeCompare(b.symbol);
    if (aValue === null) return 1;
    if (bValue === null) return -1;
    return (aValue - bValue) * direction;
  });
}

function tapeRowMarkup(row) {
  const apr =
    typeof row.funding_rate === "number" ? row.funding_rate * 24 * 365 * 100 : null;
  const aprText = apr === null ? "--" : `${apr >= 0 ? "+" : ""}${apr.toFixed(1)}%`;
  const aprClass = apr === null ? "" : apr >= 20 ? "tone-negative" : apr < 0 ? "tone-positive" : "";
  return `<button type="button" class="asset-row" data-symbol="${escapeHtml(row.symbol)}" aria-label="${escapeHtml(row.symbol)} chart">
    <span class="symbol-cell"><strong>${escapeHtml(row.symbol)}</strong></span>
    <span class="last-cell" title="Last trade">${escapeHtml(formatPrice(row.last))}</span>
    <span class="${changeClass(row.change_pct)}">${escapeHtml(formatSignedPct(row.change_pct))}</span>
    <span class="tape-funding ${aprClass}" title="Funding, annualized">${escapeHtml(aprText)}</span>
    <span class="tape-oi" title="Open interest">${row.open_interest_usd ? `$${escapeHtml(formatCompactPrice(row.open_interest_usd))}` : "--"}</span>
    <span class="tape-volume" title="24h notional volume">${row.day_volume_usd ? `$${escapeHtml(formatCompactPrice(row.day_volume_usd))}` : "--"}</span>
  </button>`;
}

function openTapeChart(symbol) {
  if (!symbol) return;
  const row = (latestData?.crypto_tape || []).find((entry) => entry.symbol === symbol);
  const last = numericOrNull(row?.last);
  const changePct = numericOrNull(row?.change_pct);
  openChart({
    symbol,
    type: "crypto_perp",
    quote: {
      provider: "lighter",
      last,
      change_pct: changePct,
      previous_close:
        last !== null && changePct !== null && changePct > -100
          ? last / (1 + changePct / 100)
          : null,
      funding_rate: row?.funding_rate ?? null,
      open_interest_usd: row?.open_interest_usd ?? null,
    },
  });
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
    groupHeaderCell("\u0394Open", "open"),
    groupHeaderCell("RVOL", "rvol"),
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
  button.title =
    sortKey === "open"
      ? "Change since today's open (UTC day for crypto) · click to sort"
      : `Sort by ${label}`;
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
  board.querySelectorAll(".group-panel:not(.tape-panel) .group-title button").forEach((button) => {
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
  if (key === "rvol") return numericOrNull(asset.summary?.rvol);
  if (key === "open") return numericOrNull(asset.summary?.open_change_pct);
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
  edit_token_required: "Wrong or missing edit token — watchlists are read-only",
};

function editorErrorCopy(detail) {
  return EDITOR_ERROR_COPY[detail] || detail || "Save failed";
}

const EDIT_TOKEN_KEY = "board-edit-token";

async function mutateWatchlists(url, options) {
  setEditorStatus("Saving");
  const send = () =>
    fetch(url, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(localStorage.getItem(EDIT_TOKEN_KEY)
          ? { "X-Edit-Token": localStorage.getItem(EDIT_TOKEN_KEY) }
          : {}),
      },
    });
  let response = await send();
  if (response.status === 401) {
    // The server has an EDIT_TOKEN configured; ask once and retry.
    const token = window.prompt("This board is protected. Enter the edit token:");
    if (token) {
      localStorage.setItem(EDIT_TOKEN_KEY, token.trim());
      response = await send();
    }
  }
  if (!response.ok) {
    if (response.status === 401) localStorage.removeItem(EDIT_TOKEN_KEY);
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
  if (assetTypeSelect.value === "crypto_perp") assetSourceSelect.value = "lighter";
  else if (assetSourceSelect.value === "lighter") assetSourceSelect.value = "yahoo";
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

  const symbolCell = ensureRowCell(row, "symbol");
  updateSymbolCell(symbolCell, asset);
  updateFundingChip(symbolCell, quote);
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
  const openChange = numericOrNull(asset.summary?.open_change_pct);
  updateValueCell(
    ensureRowCell(row, "open", "open-cell"),
    formatSignedPct(openChange),
    openChange,
    `open-cell ${changeClass(openChange)}`,
    false
  );
  const rvol = numericOrNull(asset.summary?.rvol);
  updateValueCell(
    ensureRowCell(row, "rvol", "rvol-cell"),
    formatRvol(rvol),
    rvol,
    rvolClass(rvol),
    false
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
  const key = values.join(",");
  if (cell.dataset.sparkKey === key) return;
  cell.dataset.sparkKey = key;
  cell.className = "sparkline-cell";
  cell.title = values.length ? "Recent trend" : "No trend history";
  cell.innerHTML = sparklineSvg(values);
}

function formatRvol(value) {
  return typeof value === "number" ? `${value.toFixed(1)}\u00d7` : "--";
}

function rvolClass(value) {
  if (typeof value !== "number") return "rvol-cell";
  if (value >= 2) return "rvol-cell rvol-hot";
  if (value >= 1.5) return "rvol-cell rvol-warm";
  return "rvol-cell";
}

// Perp funding chip for crypto rows: hourly Lighter rate annualized.
// Negative funding (shorts pay) reads green; hot positive funding reads red.
function updateFundingChip(cell, quote) {
  const rate = typeof quote.funding_rate === "number" ? quote.funding_rate : null;
  let chip = cell.querySelector(".funding-chip");
  if (rate === null) {
    chip?.remove();
    return;
  }
  if (!chip) {
    chip = document.createElement("em");
    chip.className = "funding-chip";
    cell.appendChild(chip);
  }
  const apr = rate * 24 * 365 * 100;
  const oi = typeof quote.open_interest_usd === "number" ? quote.open_interest_usd : null;
  const aprText = `${apr >= 0 ? "+" : ""}${apr.toFixed(1)}%`;
  chip.textContent = `F ${aprText}${oi ? ` · OI $${formatCompactPrice(oi)}` : ""}`;
  chip.classList.toggle("funding-hot", apr >= 20);
  chip.classList.toggle("funding-negative", apr < 0);
  chip.title =
    `Perp funding ${(rate * 100).toFixed(4)}%/h (${aprText} APR annualized)` +
    (oi ? ` · open interest $${formatCompactPrice(oi)}` : "");
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
  const target = (latestData?.groups || []).find((group) => group.name === groupName);
  if (target) {
    marketCategory = groupCategory(target);
    updateCategoryButtons();
  }
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
  const requestedInterval = options.interval || "1d";
  const timeframeButton =
    intervalButtons.find((item) => item.dataset.interval === requestedInterval) ||
    intervalButtons.find((item) => item.dataset.interval === "1d");
  activeInterval = timeframeButton?.dataset.interval || "1d";
  activeRange = timeframeButton?.dataset.range || "1y";
  intervalButtons.forEach((item) => item.classList.toggle("active", item === timeframeButton));
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

let chartLibPromise = null;

function ensureChartLibrary() {
  // lightweight-charts (~52KB gz) is only needed once a chart opens;
  // loading it lazily keeps it off the initial page load entirely.
  if (window.LightweightCharts) return Promise.resolve();
  if (!chartLibPromise) {
    chartLibPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/static/vendor/lightweight-charts.standalone.production.js?v=20260702-8";
      script.onload = () => resolve();
      script.onerror = () => {
        chartLibPromise = null;
        reject(new Error("Chart library unavailable"));
      };
      document.head.appendChild(script);
    });
  }
  return chartLibPromise;
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
    const [response] = await Promise.all([
      fetch(`/api/history/${encodeURIComponent(symbol)}?interval=${interval}&range=${range}`),
      ensureChartLibrary(),
    ]);
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

const INTRADAY_INTERVALS = new Set(["1m", "5m", "15m", "30m", "1h", "4h"]);

const TIMEFRAME_LABELS = {
  "1m": "1m",
  "5m": "5m",
  "15m": "15m",
  "30m": "30m",
  "1h": "1H",
  "4h": "4H",
  "1d": "1D",
  "1wk": "1W",
  "1mo": "1M",
};

function chartSubtitleText(symbol, range, interval, rawBars, barCount) {
  const timeframe = TIMEFRAME_LABELS[interval] || interval;
  const base = `${symbol} / ${timeframe} candles / ${barCount} bars`;
  if (!INTRADAY_INTERVALS.has(interval) || !rawBars.length) return base;
  const first = new Date(rawBars[0].timestamp);
  const last = new Date(rawBars[rawBars.length - 1].timestamp);
  if (Number.isNaN(first.getTime()) || Number.isNaN(last.getTime())) return base;
  const dateFmt = new Intl.DateTimeFormat([], {
    timeZone: DISPLAY_TIME_ZONE,
    month: "short",
    day: "numeric",
  });
  const timeFmt = new Intl.DateTimeFormat([], {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
  });
  const sameDay = formatLocalDate(first) === formatLocalDate(last);
  const window = sameDay
    ? `${dateFmt.format(last)} ${timeFmt.format(first)}–${timeFmt.format(last)}`
    : `${dateFmt.format(first)} ${timeFmt.format(first)} – ${dateFmt.format(last)} ${timeFmt.format(last)}`;
  const ageMs = Date.now() - last.getTime();
  // The last bucket's open time lags by up to one bar width; only flag
  // staleness once the gap clearly exceeds the timeframe itself.
  const barMs =
    { "1m": 6e4, "5m": 3e5, "15m": 9e5, "30m": 18e5, "1h": 36e5, "4h": 144e5 }[interval] || 36e5;
  const staleNote = ageMs > Math.max(2 * 3600 * 1000, 3 * barMs) ? " · prev session" : "";
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
    timeScale: { borderColor: "#23262a", timeVisible: !DATE_ONLY_INTERVALS.has(interval) },
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
  return date.toLocaleDateString([], {
    timeZone: DISPLAY_TIME_ZONE,
    month: "short",
    day: "numeric",
  });
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
  const time = date.toLocaleTimeString([], {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  // Some locales render Europe/Berlin's short name as "GMT+2"; label the
  // zone explicitly so it always reads CET/CEST.
  const offset = displayTzOffsetSeconds(date);
  const zone = offset === 7200 ? "CEST" : offset === 3600 ? "CET" : `GMT+${offset / 3600}`;
  return `${time} ${zone}`;
}

function formatLocalDate(date) {
  // en-CA renders YYYY-MM-DD; evaluated in the display zone.
  return displayDateFmt.format(date);
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

const DATE_ONLY_INTERVALS = new Set(["1d", "1wk", "1mo"]);

function toChartTime(value, interval) {
  if (DATE_ONLY_INTERVALS.has(interval)) return value.slice(0, 10);
  // lightweight-charts renders epoch labels in UTC; shift by the display
  // zone's offset so the axis matches the CET times in the subtitle.
  const date = new Date(value);
  return Math.floor(date.getTime() / 1000) + displayTzOffsetSeconds(date);
}
