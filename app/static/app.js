const board = document.querySelector("#board");
const dailyBoard = document.querySelector("#daily-board");
const boardMeta = document.querySelector("#board-meta");
const statusCopy = document.querySelector("#status-copy");
const statusStrip = document.querySelector("#status-strip");
const focusChip = document.querySelector("#focus-chip");
const focusSymbolText = document.querySelector("#focus-symbol");
const connectionState = document.querySelector("#connection-state");
const liveFreshness = document.querySelector("#live-freshness");
const feedModeLabel = document.querySelector("#feed-mode");
const refreshButton = document.querySelector("#refresh-button");
const themeToggle = document.querySelector("#theme-toggle");
const viewButtons = Array.from(document.querySelectorAll(".view-tabs button"));
const dailyView = document.querySelector("#daily-view");
const marketsView = document.querySelector("#markets-view");
const fringeView = document.querySelector("#fringe-view");
const trendsView = document.querySelector("#trends-view");
const trendsGrid = document.querySelector("#trends-grid");
const trendsRangeButtons = Array.from(document.querySelectorAll("#trends-range button"));
const componentsTabs = document.querySelector("#components-tabs");
const componentsGrid = document.querySelector("#components-grid");
const earningsView = document.querySelector("#earnings-view");
const earningsBoard = document.querySelector("#earnings-board");
const earningsWeekLabel = document.querySelector("#earnings-week");
const watchView = document.querySelector("#watch-view");
const watchGrid = document.querySelector("#watch-grid");
const watchAddInput = document.querySelector("#watch-add");
const watchStatus = document.querySelector("#watch-status");
const watchIntervalButtons = Array.from(document.querySelectorAll("#watch-intervals button"));
const watchBrowseButton = document.querySelector("#watch-browse");
const watchPicker = document.querySelector("#watch-picker");
const chartWatchToggle = document.querySelector("#chart-watch-toggle");
const fringeBoard = document.querySelector("#fringe-board");
const marketSearch = document.querySelector("#market-search");
const marketFilterClear = document.querySelector("#market-filter-clear");
const marketLayoutToggle = document.querySelector("#market-layout-toggle");
const marketMapToggle = document.querySelector("#market-map-toggle");
const marketMapLegend = document.querySelector("#market-map-legend");
const marketFilterStatus = document.querySelector("#market-filter-status");
const categoryButtons = Array.from(document.querySelectorAll("#markets-view .category-tabs button"));
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
const catalystStrip = document.querySelector("#catalyst-strip");
const newsPanel = document.querySelector("#news-panel");
const newsList = document.querySelector("#news-list");
const newsStatus = document.querySelector("#news-status");
const newsToggle = document.querySelector("#news-toggle");
const newsClose = document.querySelector("#news-close");
const newsChannelsBar = document.querySelector("#news-channels");
const newsSearch = document.querySelector("#news-search");
const newsFilters = document.querySelector("#news-filters");
const newsResultCount = document.querySelector("#news-result-count");
const reportsModal = document.querySelector("#reports-modal");
const reportsOpenButton = document.querySelector("#reports-open");
const reportsCloseButton = document.querySelector("#reports-close");
const reportsBackButton = document.querySelector("#reports-back");
const reportsBadge = document.querySelector("#reports-badge");
const reportsListElement = document.querySelector("#reports-list");
const reportReaderElement = document.querySelector("#report-reader");
const helpTooltip = document.querySelector("#help-tooltip");
let activeHelpTip = null;

let latestData = null;
let latestCryptoEtfFlows = null;
let latestKeyDates = null;
let keyDatesRevision = 0;
let latestFringe = null;
let fringeRevision = 0;
let latestSnapshots = null;
let watchlistConfig = null;
let activeSymbol = null;
let activeAsset = null;
let activeHistoryContext = null;
let activeRange = "1y";
let activeInterval = "1d";
// Open dialogs, bottom to top: Escape and the focus trap act on the TOP
// entry only, and closing a lower dialog must not disturb the ones above.
// Each entry remembers the element that opened it for focus return.
const dialogStack = []; // { dialog, trigger }
let chart = null;
let chartCandleSeries = null; // main modal candles, restyled on theme flips
let chartMovingAverageSeries = []; // [{ series, colorToken }]
let chartVolumeSeries = null;
let chartVolumeBars = [];
let chartPreviousCloseLine = null;
let chartLoadToken = 0;
let chartContextLoading = false;
let chartResizeObserver = null;
let chartResizeFrame = null;
let optionsLoadToken = 0;
let optionsPanelState = null;
let optionsProfileMode = "gex";
let marketSearchQuery = "";
let activeGroupFilter = "";
let marketSort = { key: "configured", direction: "default" };
let marketLayout = "grouped"; // "grouped" | "flat" | "map"
let marketCategory = "tradfi"; // "tradfi" | "crypto"
let tapeSorts = {}; // per-basket { key, direction }
let tapePages = {}; // per-basket page index
let mapResizeTimer = null;
let marketSearchTimer = null;
let feedMode = "poll"; // flips to "ws" only once the socket actually opens
let activeSocket = null; // live WS reference so wake-up checks can close a zombie
let lastWsFrameAt = 0; // Date.now() of the last WS frame, for zombie detection
let wsReconnectDelayMs = 3000; // doubles per failed reconnect, capped at 30s
const WS_STALE_FRAME_MS = 30000;
let activeView = "daily";
const TRENDS_TTL_MS = 5 * 60 * 1000;
const TRENDS_CATEGORY_ORDER = ["tradfi", "crypto", "commodities"];
const EARNINGS_TTL_MS = 10 * 60 * 1000;
let latestEarnings = null;
let earningsFetchedAt = 0;
let earningsLoadToken = 0;
let latestTrends = null;
let trendsDays = 90;
let trendsFetchedAt = 0;
let trendsLoadToken = 0;
let pendingChartFromUrl = null;
let restoringUrlState = false;
const COMPONENTS_TTL_MS = 60 * 60 * 1000;
let latestComponents = null;
let componentsCategory = "memory";
let componentsFetchedAt = 0;
let componentsLoading = false;
const WATCH_STORAGE_KEY = "watch-symbols-v1";
const WATCH_INTERVAL_KEY = "watch-interval-v1";
const WATCH_MAX = 9;
const WATCH_REFRESH_MS = 60000;
const WATCH_RANGES = { "15m": "1d", "1h": "1mo", "4h": "3mo", "1d": "1y" };
let watchSymbols = [];
let watchInterval = "1h";
const watchCharts = new Map();
let watchRenderToken = 0;
let chartLibPromise = null; // lazy lightweight-charts loader state (used from init-time deep links)
let activeReportId = null;
let reportOpenToken = 0;
const BOARD_CACHE_KEY = "board-cache-v1";
const BOARD_CACHE_WRITE_INTERVAL_MS = 30000;
let lastBoardCacheWriteAt = 0;
let pendingBoardCachePayload = null;
let boardCacheWriteTimer = null;
let latestNews = null;
let lastNewsRenderKey = "";
let knownNewsIds = new Set();
let newsFilter = "all";
let newsSearchQuery = "";
let focusedSymbol = null;
const NEWS_OPEN_KEY = "news-open";
// Muted news channels, persisted per browser.
const NEWS_MUTED_KEY = "news-muted-channels-v1";
const THEME_STORAGE_KEY = "board-theme";
const US_OPTIONS_EXCHANGES = new Set(["AMEX", "ARCA", "BATS", "CBOE", "NASDAQ", "NYSE", "NYSEARCA", "US"]);
let mutedNewsChannels = new Set();
try {
  mutedNewsChannels = new Set(JSON.parse(localStorage.getItem(NEWS_MUTED_KEY) || "[]"));
} catch (error) {
  mutedNewsChannels = new Set();
}
const BOARD_CACHE_MAX_AGE_MS = 24 * 3600 * 1000;
let dataIsCached = false;
// Monotonic guard: a slower /api/quotes response must never overwrite a
// fresher one (or a WS frame). Declared here because init() runs above.
let quotesFetchSeq = 0;
let quotesFetchApplied = 0;
// Same monotonic guard, per fetch family (interval + visibility refetches
// overlap; for news a WS frame must also outrank in-flight polls).
let newsFetchSeq = 0;
let newsFetchApplied = 0;
let cryptoEtfFlowsFetchSeq = 0;
let cryptoEtfFlowsFetchApplied = 0;
let keyDatesFetchSeq = 0;
let keyDatesFetchApplied = 0;
let snapshotsFetchSeq = 0;
let snapshotsFetchApplied = 0;
let snapshotRevision = 0;
let fringeFetchSeq = 0;
let fringeFetchApplied = 0;
let reportsBadgeFetchSeq = 0;

const sourceLabels = {
  yahoo: "YH",
  hyperliquid: "HL",
  stooq: "STQ",
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
  CME: "globex",
  COMEX: "globex",
  NYMEX: "globex",
  CBOT: "globex",
  ICE: "globex",
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
  // Wrapping session: opens Sun-Thu 18:00 ET, runs to 17:00 ET next day
  // (the 17-18 maintenance break and the weekend read as closed).
  globex: {
    label: "Globex",
    timeZone: "America/New_York",
    days: [0, 1, 2, 3, 4],
    open: 18 * 60,
    close: 17 * 60,
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
  if (def.open > def.close) {
    // Overnight session: `days` are the days it OPENS in the evening.
    const prevDay = (now.day + 6) % 7;
    const open =
      (def.days.includes(now.day) && now.minutes >= def.open) ||
      (def.days.includes(prevDay) && now.minutes < def.close);
    return { key: sessionKey, label: def.label, state: open ? "open" : "closed" };
  }
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

function setTheme(theme, persist = false) {
  const nextTheme = theme === "light" ? "light" : "dark";
  const light = nextTheme === "light";
  document.documentElement.dataset.theme = nextTheme;
  themeToggle.setAttribute("aria-pressed", String(light));
  themeToggle.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
  themeToggle.title = light ? "Switch to dark theme" : "Switch to light theme";
  // CSS variables flip with the dataset, but live lightweight-charts
  // instances hold their canvas colors until told otherwise.
  restyleLiveCharts();
  if (!persist) return;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
  } catch (error) {
    // Private browsing can deny storage; the active theme still applies.
  }
}

init();

// --- URL state ------------------------------------------------------------
// View and filter changes replace in place; opening a report pushes one
// history entry so browser Back returns from the reader to the library.
function syncUrlState({ push = false } = {}) {
  if (restoringUrlState) return;
  const params = new URLSearchParams();
  if (activeView !== "daily") params.set("view", activeView);
  if (activeGroupFilter) params.set("group", activeGroupFilter);
  if (marketSearchQuery) params.set("q", marketSearchQuery);
  if (marketLayout !== "grouped") params.set("layout", marketLayout);
  if (marketCategory !== "tradfi") params.set("cat", marketCategory);
  if (focusedSymbol) params.set("focus", focusedSymbol);
  if (activeSymbol) {
    params.set("chart", activeSymbol);
    if (activeInterval !== "1d") params.set("tf", activeInterval);
  }
  if (activeReportId !== null) params.set("report", String(activeReportId));
  const hash = params.toString();
  const next = hash ? `#${hash}` : window.location.pathname + window.location.search;
  if (`#${hash}` === window.location.hash || (!hash && !window.location.hash)) return;
  history[push ? "pushState" : "replaceState"](null, "", hash ? `#${hash}` : next);
}

function restoreUrlState() {
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return;
  const params = new URLSearchParams(raw);
  restoringUrlState = true;
  try {
    const view = params.get("view");
    if (["markets", "fringe", "trends", "earnings", "watch"].includes(view || "")) {
      selectView(view);
    }
    const group = params.get("group");
    if (group) activeGroupFilter = group;
    const query = params.get("q");
    if (query) {
      marketSearchQuery = query;
      marketSearch.value = query;
    }
    const cat = params.get("cat");
    if (cat === "crypto" || cat === "commodities") {
      marketCategory = cat;
      updateCategoryButtons();
    }
    const layout = params.get("layout");
    if (layout === "flat" || layout === "map") {
      marketLayout = layout;
      if (layout === "flat") marketSort = { key: "pct", direction: "desc" };
      syncLayoutButtons();
    }
    const focus = params.get("focus");
    if (focus) setFocusedSymbol(focus, { sync: false });
    const chartSymbol = params.get("chart");
    if (chartSymbol) {
      pendingChartFromUrl = {
        symbol: chartSymbol.toUpperCase(),
        interval: params.get("tf") || "1d",
      };
    }
    const reportId = params.get("report");
    if (/^\d+$/.test(reportId || "")) {
      queueMicrotask(() => {
        openReports();
        openReport(Number(reportId), { pushHistory: false });
      });
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

function setFocusedSymbol(symbol, { sync = true } = {}) {
  const next = String(symbol || "").trim().toUpperCase();
  if (!next) return;
  focusedSymbol = next;
  focusSymbolText.textContent = next;
  focusChip.hidden = false;
  focusChip.title = `Open ${next} chart`;
  focusChip.setAttribute("aria-label", `Open focused asset ${next}`);
  if (latestNews) renderNews(latestNews);
  if (sync) syncUrlState();
}

function openPendingChartFromUrl() {
  if (!pendingChartFromUrl || !latestData) return;
  const { symbol, interval } = pendingChartFromUrl;
  const asset = findAssetConfig(symbol);
  if (asset) {
    pendingChartFromUrl = null;
    openChart(asset, { interval });
    return;
  }
  // Tape rows are exactly the perps NOT in any configured group, yet the
  // app writes #chart= URLs for them too — resolve through the tape.
  if ((latestData.crypto_tape || []).some((row) => row.symbol === symbol)) {
    pendingChartFromUrl = null;
    openTapeChart(symbol, { interval });
    return;
  }
  // A cached payload may simply predate the symbol: keep the pending
  // chart armed until the first LIVE payload rules it out.
  if (!dataIsCached) pendingChartFromUrl = null;
}


function init() {
  setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  setupHelpTooltips();
  // icons are inline SVG; no icon library needed
  setConnection("connecting");
  // Watch state loads before URL restore: a #view=watch deep link renders
  // the grid during restoreUrlState and must see the persisted symbols.
  loadWatchState();
  restoreUrlState();
  window.addEventListener("popstate", restoreReportNavigation);
  restoreCachedBoard();
  fetchQuotes();
  fetchCryptoEtfFlows();
  fetchKeyDates();
  fetchSnapshots();
  fetchFringe();
  let newsOpen = false;
  try {
    newsOpen = localStorage.getItem(NEWS_OPEN_KEY) === "1";
  } catch (error) {
    // Private browsing can deny storage reads; default to the closed drawer.
  }
  setNewsOpen(newsOpen, { focus: false });
  fetchNews();
  refreshReportsBadge();
  window.setInterval(() => {
    if (!document.hidden) refreshReportsBadge();
  }, 60000);
  // Stay in "poll" until the socket's open handler flips to "ws" — assigning
  // "ws" here gates off the 10s poll while the socket hangs in CONNECTING.
  updateFeedModeLabel();
  if (shouldUseWebSocket()) openSocket();
  // Poll only while the tab is visible; a hidden tab otherwise burns
  // ~5.7k serverless invocations/day for nothing. On WS hosts frames
  // stream in; polling would double-fetch and risk stale overwrites
  // (feedMode flips to "poll" if the socket dies, resuming this timer).
  window.setInterval(() => {
    if (document.hidden) return;
    if (feedMode === "ws") {
      recoverStaleWebSocket();
    } else {
      fetchQuotes();
    }
  }, 10000);
  window.setInterval(() => {
    if (!document.hidden) {
      fetchCryptoEtfFlows();
      fetchSnapshots();
      fetchKeyDates();
      fetchFringe();
    }
  }, 300000);
  // Release actuals land within ~1 min of the print; when WS is unavailable,
  // a 30s poll runs ONLY while some rendered event is HOT (matched release,
  // actual still null, inside [T-2min, T+45min]) so quiet hours stay on the
  // 5-min baseline above.
  window.setInterval(() => {
    if (!document.hidden && feedMode !== "ws" && hasHotKeyDate()) fetchKeyDates();
  }, 30000);
  // Key-date countdowns tick client-side between refetches so time-remaining
  // stays honest without re-rendering the rail.
  window.setInterval(() => {
    if (!document.hidden) refreshKeyDateCountdowns();
  }, 30000);
  // WS pushes news instantly; polling is the fallback for serverless hosts.
  window.setInterval(() => {
    if (document.hidden) return;
    updateNewsAges();
    if (feedMode !== "ws") fetchNews();
  }, 20000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      flushBoardCache();
      return;
    }
    if (feedMode !== "ws") {
      fetchQuotes();
      // Reconnects are not scheduled while hidden; reopen on return
      // (openSocket no-ops if a socket is already OPEN/CONNECTING).
      if (shouldUseWebSocket()) openSocket();
    } else {
      recoverStaleWebSocket();
    }
    fetchCryptoEtfFlows();
    fetchKeyDates();
    fetchFringe();
    refreshReportsBadge();
  });
  window.addEventListener("pagehide", flushBoardCache);
  newsToggle.addEventListener("click", () => setNewsOpen(!document.body.classList.contains("news-open")));
  newsClose.addEventListener("click", () => setNewsOpen(false));
  newsSearch.addEventListener("input", () => {
    newsSearchQuery = newsSearch.value.trim().toLowerCase();
    newsList.scrollTop = 0;
    if (latestNews) renderNews(latestNews);
  });
  newsFilters.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-news-filter]");
    if (!button || button.disabled) return;
    newsFilter = button.dataset.newsFilter || "all";
    newsList.scrollTop = 0;
    if (latestNews) renderNews(latestNews);
  });
  themeToggle.addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light", true);
  });
  newsChannelsBar.addEventListener("click", (event) => {
    const chip = event.target.closest("button[data-channel]");
    if (!chip) return;
    const channel = chip.dataset.channel;
    if (mutedNewsChannels.has(channel)) {
      mutedNewsChannels.delete(channel);
    } else {
      mutedNewsChannels.add(channel);
    }
    try {
      localStorage.setItem(NEWS_MUTED_KEY, JSON.stringify([...mutedNewsChannels]));
    } catch (error) {
      // Storage is best-effort; channel filters still work for this session.
    }
    if (latestNews) renderNews(latestNews);
  });
  focusChip.addEventListener("click", () => openFringeTicker(focusedSymbol));
  catalystStrip.addEventListener("click", () => {
    selectView("daily");
    const panel = dailyBoard.querySelector('[data-panel="key-dates"]');
    if (panel) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
      panel.querySelector("a, button")?.focus({ preventScroll: true });
    }
  });
  cryptoTapeElement.addEventListener("click", handleCryptoTapeClick);
  refreshButton.addEventListener("click", () => {
    fetchQuotes();
    fetchCryptoEtfFlows();
    fetchSnapshots();
    fetchKeyDates();
    fetchFringe();
    refreshReportsBadge();
  });
  viewButtons.forEach((button) => {
    button.addEventListener("click", () => selectView(button.dataset.view || "daily"));
    button.addEventListener("keydown", handleViewTabKeydown);
  });
  marketSearch.addEventListener("input", scheduleMarketSearch);
  marketSearch.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      flushPendingMarketSearch();
      focusFirstMarketRow();
    }
  });
  marketFilterClear.addEventListener("click", clearMarketFilters);
  marketLayoutToggle.addEventListener("click", toggleMarketLayout);
  marketMapToggle.addEventListener("click", toggleMarketMap);
  board.addEventListener("click", (event) => {
    const tile = event.target.closest(".map-tile");
    if (tile) openFringeTicker(tile.dataset.symbol || "");
  });
  window.addEventListener("resize", () => {
    if (marketLayout !== "map" || marketsView.hidden) return;
    if (mapResizeTimer !== null) window.clearTimeout(mapResizeTimer);
    mapResizeTimer = window.setTimeout(() => renderBoard(latestData), 200);
  });
  categoryButtons.forEach((button) => {
    button.addEventListener("click", () => selectCategory(button.dataset.category || "tradfi"));
  });
  trendsRangeButtons.forEach((button) => {
    button.addEventListener("click", () => selectTrendsRange(Number(button.dataset.days) || 90));
  });
  trendsGrid.addEventListener("click", (event) => {
    const card = event.target.closest(".trend-card");
    if (card) filterMarketsByGroup(card.dataset.group || "");
  });
  componentsTabs.addEventListener("click", (event) => {
    const chip = event.target.closest("button[data-slug]");
    if (chip) selectComponentsCategory(chip.dataset.slug || "");
  });
  watchAddInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      addWatchSymbol(watchAddInput.value);
    }
  });
  watchBrowseButton.addEventListener("click", () => toggleWatchPicker());
  watchPicker.addEventListener("click", (event) => {
    const pick = event.target.closest(".watch-pick");
    if (pick) toggleWatchSymbol(pick.dataset.symbol || "");
  });
  document.addEventListener("click", (event) => {
    if (watchPicker.hidden) return;
    if (!event.target.closest(".watch-picker-wrap")) toggleWatchPicker(false);
  });
  chartWatchToggle.addEventListener("click", () => {
    if (activeSymbol) toggleWatchSymbol(activeSymbol);
  });
  watchIntervalButtons.forEach((button) => {
    button.addEventListener("click", () => selectWatchInterval(button.dataset.interval || "1h"));
  });
  watchGrid.addEventListener("click", (event) => {
    const remove = event.target.closest(".watch-remove");
    if (remove) {
      removeWatchSymbol(remove.dataset.symbol || "");
      return;
    }
    const open = event.target.closest(".watch-open");
    if (open) {
      const symbol = open.dataset.symbol || "";
      const asset = findAssetConfig(symbol);
      if (asset) openChart(asset);
      else openFringeTicker(symbol);
    }
  });
  // In-place candle refresh keeps zoom/scroll; only while the tab is open.
  window.setInterval(() => {
    if (!document.hidden && activeView === "watch" && watchCharts.size) refreshWatchData();
  }, WATCH_REFRESH_MS);
  window.addEventListener("resize", () => {
    if (activeView === "watch") resizeWatchCharts();
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
  reportsOpenButton.addEventListener("click", openReports);
  reportsCloseButton.addEventListener("click", closeReports);
  reportsBackButton.addEventListener("click", () => showReportsList({ focus: true }));
  reportsModal.addEventListener("click", (event) => {
    if (event.target === reportsModal) closeReports();
  });
  // Cross-report links inside a report body ("#report=12" hash, bare or on a
  // full board URL) navigate the reader in place instead of reloading.
  reportReaderElement.addEventListener("click", (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor) return;
    const id = reportIdFromHref(anchor.getAttribute("href") || "");
    if (id === null) return;
    event.preventDefault();
    openReport(id);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Tab" && dialogStack.length) {
      trapDialogFocus(event, topDialog());
      return;
    }
    if (event.key === "/" && !dialogStack.length && !isTextInput(event.target)) {
      event.preventDefault();
      selectView("markets");
      marketSearch.focus();
      return;
    }
    if (event.key === "Escape") {
      if (activeHelpTip) {
        hideHelpTooltip();
        return;
      }
      const top = topDialog();
      if (top) {
        // Peel one dialog per press: a chart opened over the reports
        // library must close alone, leaving the library open.
        if (top === modal) closeModal();
        else if (top === editorModal) closeEditor();
        else if (top === reportsModal) closeReports();
        else closeDialog(top);
        return;
      }
      if (document.body.classList.contains("news-open")) setNewsOpen(false);
      return;
    }
    if (
      (event.key === "j" || event.key === "k") &&
      !dialogStack.length &&
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
    button.setAttribute("aria-pressed", String(active));
  });
}

function groupCategory(group) {
  const assets = group.assets || [];
  if (assets.some((asset) => isCryptoAsset(asset.type))) return "crypto";
  if (assets.some((asset) => asset.type === "future")) return "commodities";
  return "tradfi";
}

// --- Earnings view ----------------------------------------------------------
// Weekly research-desk calendar: Mon-Fri day cards, top reports per day
// ranked held-first then by market cap, with EPS consensus, options-implied
// move for held names, and last-4Q beat/miss chips (Nasdaq data).

async function renderEarningsView() {
  if (latestEarnings && Date.now() - earningsFetchedAt < EARNINGS_TTL_MS) {
    renderEarningsBoard(latestEarnings);
    earningsLoadToken += 1; // a late in-flight response must not overwrite
    return;
  }
  const token = ++earningsLoadToken;
  if (!latestEarnings) {
    earningsBoard.innerHTML = '<div class="empty-state">Loading earnings calendar</div>';
  }
  try {
    const response = await fetch("/api/earnings");
    if (!response.ok) throw new Error("earnings_failed");
    const payload = await response.json();
    if (token !== earningsLoadToken) return;
    latestEarnings = payload;
    earningsFetchedAt = Date.now();
    renderEarningsBoard(payload);
  } catch (error) {
    if (token !== earningsLoadToken) return;
    if (latestEarnings) {
      renderEarningsBoard(latestEarnings);
      earningsBoard.insertAdjacentHTML(
        "afterbegin",
        '<div class="trends-note" role="status">Earnings refresh failed — showing cached data</div>'
      );
      return;
    }
    earningsBoard.innerHTML = '<div class="empty-state">Earnings calendar unavailable</div>';
  }
}

function renderEarningsBoard(payload) {
  const days = Array.isArray(payload?.days) ? payload.days : [];
  const weekZone = payload?.week_start
    ? earningsZoneLabel(new Date(`${payload.week_start}T12:00:00Z`))
    : "";
  earningsWeekLabel.textContent =
    payload?.week_start && payload?.week_end
      ? `Week ${payload.week_start} – ${payload.week_end} ${weekZone}${payload.ranking_fallback ? " · ranking fallback" : ""}`
      : "Week unavailable";
  if (!days.length) {
    earningsBoard.innerHTML = '<div class="empty-state">No reports scheduled this week</div>';
    return;
  }
  earningsBoard.innerHTML = days.map(earningsDayMarkup).join("");
}

function earningsDayMarkup(day) {
  const reports = Array.isArray(day.reports) ? day.reports : [];
  const dayNumber = String(day.date || "").slice(8, 10) || "--";
  const rows = reports.length
    ? reports.map((report) => earningsRowMarkup(report, day.date)).join("")
    : '<div class="empty-state small">No reports</div>';
  const more =
    day.more > 0
      ? `<footer class="earnings-more">+${day.more} more report${day.more === 1 ? "" : "s"}</footer>`
      : "";
  const zone = earningsZoneLabel(new Date(`${day.date || ""}T12:00:00Z`));
  return `<section class="earnings-day">
    <div class="earnings-date"><strong>${escapeHtml(dayNumber)}</strong><span>${escapeHtml(day.weekday || "")}</span></div>
    <div class="earnings-rows">
      <div class="earnings-row earnings-head" aria-hidden="true">
        <span>TIME ${zone}</span><span>SYM</span><span></span><span>EST EPS</span><span>IMPL.</span><span>LAST 4Q</span>
      </div>
      ${rows}${more}
    </div>
  </section>`;
}

function earningsZoneLabel(date) {
  const offset = displayTzOffsetSeconds(date);
  if (offset === 7200) return "CEST";
  if (offset === 3600) return "CET";
  return `GMT${offset >= 0 ? "+" : ""}${offset / 3600}`;
}

function earningsReleaseDisplay(value, reportDate) {
  const moment = new Date(value || "");
  if (Number.isNaN(moment.getTime())) return null;
  const time = moment.toLocaleTimeString("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
  });
  const localDate = formatLocalDate(moment);
  const dayPrefix =
    localDate === reportDate
      ? ""
      : `${moment
          .toLocaleDateString("en-US", { timeZone: DISPLAY_TIME_ZONE, weekday: "short" })
          .toUpperCase()} `;
  const zone = earningsZoneLabel(moment);
  return {
    label: `${dayPrefix}${time}`,
    title: `Scheduled / estimated release: ${localDate} ${time} ${zone} · TradingView`,
  };
}

function earningsRowMarkup(report, reportDate) {
  const session = ["bmo", "amc"].includes(report.session) ? report.session : "tns";
  const sessionTitle = { bmo: "Before market open", amc: "After market close", tns: "Time not supplied" }[session];
  const sessionLabel = { bmo: "BMO", amc: "AMC", tns: "TNS" }[session];
  const release = earningsReleaseDisplay(report.release_at, reportDate);
  const eps = typeof report.eps_estimate === "number" ? report.eps_estimate.toFixed(2) : "—";
  const impl =
    typeof report.implied_move_pct === "number" ? `±${report.implied_move_pct.toFixed(1)}%` : "—";
  const marks = Array.from({ length: 4 }, (_, index) => {
    const mark = (report.last4q || [])[index];
    if (mark === true) return '<i class="beat" title="Beat">▲</i>';
    if (mark === false) return '<i class="miss" title="Miss">▼</i>';
    return '<i class="unknown" title="No data">—</i>';
  }).join("");
  return `<div class="earnings-row${report.held ? " earnings-held" : ""}">
    <span class="earnings-session" title="${escapeHtml(release ? `${release.title} · ${sessionTitle}` : sessionTitle)}"><em class="session-icon session-${session}" aria-hidden="true"></em><b>${escapeHtml(release?.label || sessionLabel)}</b></span>
    <strong>${escapeHtml(report.symbol || "")}</strong>
    <span class="earnings-name">${escapeHtml(report.name || "")}${report.held ? '<b class="held-chip">HELD</b>' : ""}</span>
    <span class="earnings-eps">${escapeHtml(eps)}</span>
    <span class="earnings-impl">${escapeHtml(impl)}</span>
    <span class="earnings-marks">${marks}</span>
  </div>`;
}

// --- Trends view -----------------------------------------------------------
// PCPartPicker-style bands per watchlist group: members indexed to 100 at
// the window start, shaded min–max envelope, equal-weight average line.

async function renderTrendsView() {
  loadComponentTrends();
  if (
    latestTrends &&
    latestTrends.days === trendsDays &&
    Date.now() - trendsFetchedAt < TRENDS_TTL_MS
  ) {
    renderTrendsGrid(latestTrends);
    // Invalidate any in-flight fetch (e.g. a range flipped back mid-request):
    // its late response must not overwrite the state just rendered.
    trendsLoadToken += 1;
    return;
  }
  const token = ++trendsLoadToken;
  if (!latestTrends || latestTrends.days !== trendsDays) {
    trendsGrid.innerHTML = '<div class="empty-state">Loading trends</div>';
  }
  try {
    const response = await fetch(`/api/trends?days=${trendsDays}`);
    if (!response.ok) throw new Error("trends_failed");
    const payload = await response.json();
    if (token !== trendsLoadToken) return;
    latestTrends = payload;
    trendsFetchedAt = Date.now();
    renderTrendsGrid(payload);
  } catch (error) {
    if (token !== trendsLoadToken) return;
    if (latestTrends) {
      // Offline refresh: keep the stale-but-real bands on screen with a
      // note instead of blanking the grid.
      renderTrendsGrid(latestTrends);
      trendsGrid.insertAdjacentHTML(
        "afterbegin",
        '<div class="trends-note" role="status">Trends refresh failed — showing cached data</div>'
      );
      return;
    }
    trendsGrid.innerHTML = '<div class="empty-state">Trends unavailable</div>';
  }
}

function selectTrendsRange(days) {
  if (days === trendsDays) return;
  trendsDays = days;
  trendsRangeButtons.forEach((button) => {
    const active = Number(button.dataset.days) === days;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderTrendsView();
}

// --- PCPartPicker component prices ------------------------------------------
// The source publishes daily-regenerated PNG charts (no data API); the
// backend scrapes each category's gallery and we hotlink the CDN images.
async function loadComponentTrends() {
  if (latestComponents && Date.now() - componentsFetchedAt < COMPONENTS_TTL_MS) return;
  if (componentsLoading) return;
  componentsLoading = true;
  try {
    const response = await fetch("/api/component-trends");
    if (!response.ok) throw new Error("components_failed");
    const payload = await response.json();
    latestComponents = payload;
    componentsFetchedAt = Date.now();
    if (!payload.categories?.some((category) => category.slug === componentsCategory)) {
      componentsCategory = payload.categories?.[0]?.slug || componentsCategory;
    }
    renderComponentsSection();
  } catch (error) {
    if (!latestComponents) {
      componentsGrid.innerHTML =
        '<div class="empty-state">PCPartPicker trends unavailable</div>';
    }
  } finally {
    componentsLoading = false;
  }
}

function renderComponentsSection() {
  const categories = latestComponents?.categories || [];
  if (!categories.length) {
    componentsTabs.innerHTML = "";
    componentsGrid.innerHTML =
      '<div class="empty-state">PCPartPicker trends unavailable</div>';
    return;
  }
  componentsTabs.innerHTML = categories
    .map(
      (category) =>
        `<button type="button" data-slug="${escapeHtml(category.slug)}" class="${category.slug === componentsCategory ? "active" : ""}" aria-pressed="${category.slug === componentsCategory}">${escapeHtml(category.label)}</button>`
    )
    .join("");
  const active = categories.find((category) => category.slug === componentsCategory);
  const charts = active?.charts || [];
  // Scraped/config URLs are untrusted like news links: only http(s) may
  // reach an href (blocks javascript: and friends); else a plain card.
  const safeUrl = /^https?:\/\//i.test(active?.url || "") ? active.url : "";
  const [cardOpen, cardClose] = safeUrl
    ? [
        `<a class="component-card" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer" title="Open ${escapeHtml(active.label)} trends on PCPartPicker">`,
        "</a>",
      ]
    : ['<div class="component-card">', "</div>"];
  componentsGrid.innerHTML = charts
    .map(
      (chart) =>
        `${cardOpen}
          <header><strong>${escapeHtml(chart.title)}</strong></header>
          <img src="/api/component-image?src=${encodeURIComponent(chart.image)}" alt="${escapeHtml(`${chart.title} price trend`)}" loading="lazy" decoding="async">
        ${cardClose}`
    )
    .join("");
}

function selectComponentsCategory(slug) {
  if (!slug || slug === componentsCategory) return;
  componentsCategory = slug;
  renderComponentsSection();
}

// --- Watch view --------------------------------------------------------------
// DexScreener-style grid: up to six symbols, each on its own interactive
// lightweight-charts candlestick tile, persisted per browser. Charts share
// one timeframe; data refreshes in place every minute while the tab is open.
function loadWatchState() {
  try {
    const raw = JSON.parse(localStorage.getItem(WATCH_STORAGE_KEY) || "[]");
    if (Array.isArray(raw)) {
      watchSymbols = raw
        .map((symbol) => String(symbol).trim().toUpperCase())
        .filter((symbol) => /^[A-Z0-9.\-=^]{1,24}$/.test(symbol))
        .slice(0, WATCH_MAX);
    }
    const interval = localStorage.getItem(WATCH_INTERVAL_KEY) || "";
    if (interval in WATCH_RANGES) watchInterval = interval;
  } catch (error) {
    // Private browsing: the grid still works for the session.
  }
  watchIntervalButtons.forEach((button) => {
    const active = button.dataset.interval === watchInterval;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function persistWatchState() {
  try {
    localStorage.setItem(WATCH_STORAGE_KEY, JSON.stringify(watchSymbols));
    localStorage.setItem(WATCH_INTERVAL_KEY, watchInterval);
  } catch (error) {
    /* best-effort */
  }
}

function setWatchStatus(text) {
  watchStatus.textContent = text || "";
}

function addWatchSymbol(rawSymbol) {
  const symbol = String(rawSymbol || "").trim().toUpperCase();
  if (!symbol) return;
  if (!/^[A-Z0-9.\-=^]{1,24}$/.test(symbol)) {
    setWatchStatus(`"${symbol}" is not a valid symbol`);
    return;
  }
  if (watchSymbols.includes(symbol)) {
    setWatchStatus(`${symbol} is already on the grid`);
    return;
  }
  if (watchSymbols.length >= WATCH_MAX) {
    setWatchStatus(`Grid is full — remove a tile first (max ${WATCH_MAX})`);
    return;
  }
  watchSymbols.push(symbol);
  persistWatchState();
  setWatchStatus("");
  watchAddInput.value = "";
  renderWatchGrid();
}

function removeWatchSymbol(symbol) {
  watchSymbols = watchSymbols.filter((item) => item !== symbol);
  persistWatchState();
  setWatchStatus("");
  renderWatchGrid();
}

function selectWatchInterval(interval) {
  if (!(interval in WATCH_RANGES) || interval === watchInterval) return;
  watchInterval = interval;
  persistWatchState();
  watchIntervalButtons.forEach((button) => {
    const active = button.dataset.interval === interval;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  renderWatchGrid();
}

// --- Watch picker: browse the board's sectors and toggle tickers in. -------
function toggleWatchPicker(force) {
  const open = typeof force === "boolean" ? force : watchPicker.hidden;
  watchPicker.hidden = !open;
  watchBrowseButton.setAttribute("aria-expanded", String(open));
  if (open) renderWatchPicker();
}

function renderWatchPicker() {
  const groups = latestData?.groups || [];
  if (!groups.length) {
    watchPicker.innerHTML = '<div class="empty-state">Board data still loading</div>';
    return;
  }
  const byCategory = new Map();
  groups.forEach((group) => {
    const category = groupCategory(group);
    if (!byCategory.has(category)) byCategory.set(category, []);
    byCategory.get(category).push(group);
  });
  const sections = TRENDS_CATEGORY_ORDER.filter((category) => byCategory.has(category))
    .map((category) => {
      const panels = byCategory
        .get(category)
        .map(
          (group) => `<div class="watch-picker-group">
            <span>${escapeHtml(displayGroupName(group.name))}</span>
            <div>${(group.assets || [])
              .map((asset) => {
                const active = watchSymbols.includes(asset.symbol);
                return `<button type="button" class="watch-pick${active ? " active" : ""}" data-symbol="${escapeHtml(asset.symbol)}" aria-pressed="${active}" title="${escapeHtml(asset.name || asset.symbol)}">${escapeHtml(asset.symbol)}</button>`;
              })
              .join("")}</div>
          </div>`
        )
        .join("");
      return `<div class="watch-picker-category"><em>${escapeHtml(category)}</em>${panels}</div>`;
    })
    .join("");
  watchPicker.innerHTML = `<header>${watchSymbols.length}/${WATCH_MAX} on the grid · click to toggle</header>${sections}`;
}

function toggleWatchSymbol(symbol) {
  if (watchSymbols.includes(symbol)) {
    removeWatchSymbol(symbol);
  } else {
    addWatchSymbol(symbol);
  }
  renderWatchPicker();
  updateChartWatchToggle();
}

// The chart modal's star: every Markets/tape/treemap row opens the modal,
// so this is the "add from Markets" path without nesting buttons in rows.
function updateChartWatchToggle() {
  const symbol = activeSymbol || "";
  const active = Boolean(symbol) && watchSymbols.includes(symbol);
  chartWatchToggle.classList.toggle("watch-starred", active);
  chartWatchToggle.setAttribute("aria-pressed", String(active));
  const label = active ? `Remove ${symbol} from watch grid` : `Add ${symbol} to watch grid`;
  chartWatchToggle.setAttribute("aria-label", label);
  chartWatchToggle.title = label;
}

function destroyWatchCharts() {
  watchCharts.forEach((entry) => entry.instance.remove());
  watchCharts.clear();
}

function watchQuoteLine(symbol) {
  const asset = findAssetConfig(symbol);
  const quote = asset?.quote;
  if (!quote) return "";
  const last = numericOrNull(displayQuoteValue(quote, "last"));
  const pct = numericOrNull(displayQuoteValue(quote, "change_pct"));
  const tone = pct === null ? "" : pct > 0 ? "positive" : pct < 0 ? "negative" : "";
  const parts = [];
  if (last !== null) parts.push(escapeHtml(formatPrice(last)));
  if (pct !== null) parts.push(`<em class="${tone}">${escapeHtml(formatSignedPct(pct))}</em>`);
  return parts.join(" ");
}

function renderWatchGrid() {
  // Charts measure their container: never build while the panel is hidden
  // (clientWidth 0), selectView re-enters here once the view is visible.
  if (watchView.hidden) return;
  const token = ++watchRenderToken;
  destroyWatchCharts();
  if (!watchSymbols.length) {
    watchGrid.innerHTML =
      '<div class="empty-state">Add up to 9 symbols to build your chart wall — Browse the board sectors or type anything Yahoo knows (SPY, NVDA, BTC, CL=F)</div>';
    return;
  }
  watchGrid.innerHTML = watchSymbols
    .map(
      (symbol) => `<article class="watch-tile" data-watch-symbol="${escapeHtml(symbol)}">
        <header>
          <button type="button" class="watch-open" data-symbol="${escapeHtml(symbol)}" title="Open full ${escapeHtml(symbol)} chart"><strong>${escapeHtml(symbol)}</strong></button>
          <span class="watch-quote">${watchQuoteLine(symbol)}</span>
          <button type="button" class="watch-remove" data-symbol="${escapeHtml(symbol)}" aria-label="Remove ${escapeHtml(symbol)} from watch grid">&times;</button>
        </header>
        <div class="watch-chart" data-watch-chart="${escapeHtml(symbol)}"><span class="loading-spinner" aria-hidden="true"></span></div>
      </article>`
    )
    .join("");
  watchSymbols.forEach((symbol) => loadWatchChart(symbol, token));
}

async function loadWatchChart(symbol, token) {
  const container = watchGrid.querySelector(`[data-watch-chart="${CSS.escape(symbol)}"]`);
  if (!container) return;
  try {
    const range = WATCH_RANGES[watchInterval];
    const [response] = await Promise.all([
      fetch(`/api/history/${encodeURIComponent(symbol)}?interval=${watchInterval}&range=${range}`),
      ensureChartLibrary(),
    ]);
    if (!response.ok) throw new Error("history_failed");
    const payload = await response.json();
    if (token !== watchRenderToken) return;
    const bars = (payload.bars || [])
      .map((bar) => ({
        time: toChartTime(bar.timestamp, watchInterval),
        open: numericOrNull(bar.open),
        high: numericOrNull(bar.high),
        low: numericOrNull(bar.low),
        close: numericOrNull(bar.close),
      }))
      .filter(
        (bar) =>
          Number.isFinite(bar.open) &&
          Number.isFinite(bar.high) &&
          Number.isFinite(bar.low) &&
          Number.isFinite(bar.close)
      );
    if (!bars.length) throw new Error("no_history");
    container.replaceChildren();
    const colors = chartThemeColors();
    const instance = window.LightweightCharts.createChart(container, {
      width: container.clientWidth || 420,
      height: container.clientHeight || 240,
      layout: { background: { color: colors.background }, textColor: colors.text },
      grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
      rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
      timeScale: {
        borderColor: colors.border,
        timeVisible: !DATE_ONLY_INTERVALS.has(watchInterval),
      },
      crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
    });
    const series = instance.addCandlestickSeries({
      upColor: colors.up,
      downColor: colors.down,
      borderUpColor: colors.up,
      borderDownColor: colors.down,
      wickUpColor: colors.up,
      wickDownColor: colors.down,
    });
    series.setData(bars);
    instance.timeScale().fitContent();
    watchCharts.set(symbol, { instance, series, container });
  } catch (error) {
    if (token !== watchRenderToken) return;
    // Only a genuinely empty payload means the symbol has no history;
    // network/HTTP failures are transient and retryable.
    const copy =
      error?.message === "no_history"
        ? "No history for this symbol"
        : "Chart data unavailable — retry from the timeframe buttons";
    container.innerHTML = `<div class="watch-error">${copy}</div>`;
  }
}

// In-place data refresh: zoom/scroll survive, instances are reused.
async function refreshWatchData() {
  const token = watchRenderToken;
  const range = WATCH_RANGES[watchInterval];
  await Promise.all(
    Array.from(watchCharts.entries()).map(async ([symbol, entry]) => {
      try {
        const response = await fetch(
          `/api/history/${encodeURIComponent(symbol)}?interval=${watchInterval}&range=${range}`
        );
        if (!response.ok) return;
        const payload = await response.json();
        if (token !== watchRenderToken) return;
        const bars = (payload.bars || [])
          .map((bar) => ({
            time: toChartTime(bar.timestamp, watchInterval),
            open: numericOrNull(bar.open),
            high: numericOrNull(bar.high),
            low: numericOrNull(bar.low),
            close: numericOrNull(bar.close),
          }))
          .filter((bar) => Number.isFinite(bar.close) && Number.isFinite(bar.open));
        if (bars.length) entry.series.setData(bars);
      } catch (error) {
        /* keep the last good candles */
      }
    })
  );
}

function resizeWatchCharts() {
  watchCharts.forEach((entry) => {
    entry.instance.applyOptions({ width: entry.container.clientWidth || 420 });
  });
}

function renderTrendsGrid(payload) {
  const groups = Array.isArray(payload?.groups) ? payload.groups : [];
  if (!groups.length) {
    trendsGrid.innerHTML =
      '<div class="empty-state">No cached daily bars yet — trends build from history refreshes</div>';
    return;
  }
  const ordered = [...groups].sort(
    (a, b) =>
      TRENDS_CATEGORY_ORDER.indexOf(a.category) - TRENDS_CATEGORY_ORDER.indexOf(b.category)
  );
  trendsGrid.innerHTML = ordered.map(trendCard).join("");
}

function trendCard(group) {
  const series = Array.isArray(group.series) ? group.series : [];
  if (series.length < 2) return "";
  const last = series[series.length - 1];
  const delta = numericOrNull(last?.avg);
  const deltaPct = delta === null ? null : delta - 100;
  const tone = deltaPct === null ? "" : deltaPct > 0 ? "positive" : deltaPct < 0 ? "negative" : "";
  const spanLabel = `${keyDateShort(series[0].date)} \u2192 ${keyDateShort(last.date)}`;
  return `<button type="button" class="trend-card" data-group="${escapeHtml(group.name)}" title="Open ${escapeHtml(displayGroupName(group.name))} in Markets">
    <header>
      <strong>${escapeHtml(displayGroupName(group.name))}</strong>
      <em class="${tone}">${deltaPct === null ? "--" : escapeHtml(formatSignedPct(deltaPct))}</em>
    </header>
    ${trendBandSvg(series)}
    <footer><span>${escapeHtml(spanLabel)}</span><span>${group.members} member${group.members === 1 ? "" : "s"} \u00b7 ${escapeHtml(String(group.category || ""))}</span></footer>
  </button>`;
}

function trendBandSvg(series) {
  const W = 320;
  const H = 110;
  const pad = 4;
  const values = series.flatMap((point) => [point.min, point.max]).filter(Number.isFinite);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return "";
  if (hi - lo < 0.5) {
    hi += 0.25;
    lo -= 0.25;
  }
  const x = (i) => pad + (i * (W - pad * 2)) / (series.length - 1);
  const y = (value) => pad + ((hi - value) / (hi - lo)) * (H - pad * 2);
  const upper = series.map((point, i) => `${x(i).toFixed(1)},${y(point.max).toFixed(1)}`);
  const lowerBack = [...series]
    .reverse()
    .map((point, i) => `${x(series.length - 1 - i).toFixed(1)},${y(point.min).toFixed(1)}`);
  const avgPoints = series
    .map((point, i) => `${x(i).toFixed(1)},${y(point.avg).toFixed(1)}`)
    .join(" ");
  const baseline = lo <= 100 && hi >= 100 ? y(100).toFixed(1) : null;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Performance band" preserveAspectRatio="none">
    <path d="M${upper.join(" L")} L${lowerBack.join(" L")} Z" fill="var(--accent)" fill-opacity="0.13"/>
    ${baseline !== null ? `<line x1="${pad}" y1="${baseline}" x2="${W - pad}" y2="${baseline}" stroke="var(--line)" stroke-dasharray="3 3"/>` : ""}
    <polyline points="${avgPoints}" fill="none" stroke="var(--accent)" stroke-width="1.6" stroke-linejoin="round"/>
  </svg>`;
}

function selectView(view) {
  activeView = ["markets", "fringe", "trends", "earnings", "watch"].includes(view)
    ? view
    : "daily";
  dailyView.hidden = activeView !== "daily";
  marketsView.hidden = activeView !== "markets";
  fringeView.hidden = activeView !== "fringe";
  trendsView.hidden = activeView !== "trends";
  earningsView.hidden = activeView !== "earnings";
  watchView.hidden = activeView !== "watch";
  viewButtons.forEach((button) => {
    const selected = button.dataset.view === activeView;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (activeView === "fringe") renderFringeView();
  if (activeView === "trends") renderTrendsView();
  if (activeView === "earnings") renderEarningsView();
  if (activeView === "watch") renderWatchGrid();
  // The treemap sizes itself from board.clientWidth, which is 0 while the
  // markets view is hidden (a refresh landing on Daily still renders the
  // board): re-render on entry so the map lays out at the real width.
  if (activeView === "markets" && marketLayout === "map") renderBoard(latestData);
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
  pendingBoardCachePayload = payload;
  const elapsed = Date.now() - lastBoardCacheWriteAt;
  if (!lastBoardCacheWriteAt || elapsed >= BOARD_CACHE_WRITE_INTERVAL_MS) {
    flushBoardCache();
    return;
  }
  if (boardCacheWriteTimer === null) {
    boardCacheWriteTimer = window.setTimeout(
      flushBoardCache,
      BOARD_CACHE_WRITE_INTERVAL_MS - elapsed
    );
  }
}

function flushBoardCache() {
  if (boardCacheWriteTimer !== null) {
    window.clearTimeout(boardCacheWriteTimer);
    boardCacheWriteTimer = null;
  }
  if (!pendingBoardCachePayload) return;
  const payload = pendingBoardCachePayload;
  pendingBoardCachePayload = null;
  try {
    const now = Date.now();
    localStorage.setItem(BOARD_CACHE_KEY, JSON.stringify({ at: now, payload }));
    lastBoardCacheWriteAt = now;
  } catch (error) {
    /* quota exceeded / private mode — cache is best-effort */
  }
}


async function fetchQuotes() {
  const seq = ++quotesFetchSeq;
  refreshButton.classList.add("loading");
  try {
    const response = await fetch("/api/quotes");
    if (!response.ok) throw new Error("quotes_failed");
    const payload = await response.json();
    // A slower response must never overwrite a fresher one (overlapping
    // interval poll + manual refresh + visibility refetch can all be in
    // flight; on WS hosts frames land between them).
    if (seq <= quotesFetchApplied) return;
    quotesFetchApplied = seq;
    dataIsCached = false;
    applyQuotes(payload);
    persistBoardCache(payload);
    setConnection("live");
  } catch (error) {
    if (seq <= quotesFetchApplied) return;
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
  // A pending reconnect timer and a visibility reopen can race; never
  // stack a second socket on a live one.
  if (
    activeSocket &&
    (activeSocket.readyState === WebSocket.OPEN || activeSocket.readyState === WebSocket.CONNECTING)
  ) {
    return;
  }
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/quotes`);
  activeSocket = socket;

  socket.addEventListener("open", () => {
    lastWsFrameAt = Date.now(); // a fresh socket is not a zombie yet
    wsReconnectDelayMs = 3000; // healthy again: restart backoff from base
    feedMode = "ws";
    updateFeedModeLabel();
    setConnection("live");
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch (error) {
      // Malformed frame: the socket is demonstrably alive, so stamp the
      // zombie detector and drop only this frame.
      lastWsFrameAt = Date.now();
      return;
    }
    lastWsFrameAt = Date.now();
    if (message.type === "quotes") {
      dataIsCached = false;
      // A WS frame is the freshest state; any poll still in flight
      // (visibility refetch, manual refresh) must not overwrite it.
      quotesFetchApplied = quotesFetchSeq;
      applyQuotes(message.data);
      persistBoardCache(message.data);
      setConnection("live");
    } else if (message.type === "news") {
      // WS news is freshest; drop any poll response still in flight.
      newsFetchApplied = newsFetchSeq;
      renderNews(message.data);
    } else if (message.type === "key_dates") {
      // WS key-dates frame is freshest; drop any poll response still in flight.
      keyDatesFetchApplied = keyDatesFetchSeq;
      applyKeyDates(message.data);
    }
  });
  socket.addEventListener("close", () => {
    feedMode = "poll";
    updateFeedModeLabel();
    setConnection("error");
    // Back off doubling to 30s so a dead server isn't hammered; hidden tabs
    // skip the retry entirely — the visibilitychange handler reopens.
    if (!document.hidden) {
      window.setTimeout(openSocket, wsReconnectDelayMs);
      wsReconnectDelayMs = Math.min(wsReconnectDelayMs * 2, 30000);
    }
  });
  socket.addEventListener("error", () => setConnection("error"));
}

function recoverStaleWebSocket() {
  if (
    feedMode !== "ws" ||
    Date.now() - lastWsFrameAt <= WS_STALE_FRAME_MS
  ) {
    return false;
  }
  // Suppress duplicate recovery polls while close propagation is pending.
  lastWsFrameAt = Date.now();
  fetchQuotes();
  try {
    if (activeSocket && activeSocket.readyState === WebSocket.OPEN) {
      activeSocket.close();
    }
  } catch (error) {
    // Socket already dying; the close handler's reconnect covers it.
  }
  return true;
}

function updateFeedModeLabel() {
  feedModeLabel.textContent = feedMode === "ws" ? "WS Live" : "Poll 10s";
  feedModeLabel.title =
    feedMode === "ws"
      ? "Streaming over WebSocket"
      : "Quotes refresh by HTTP poll every 10 seconds";
}

let uiVersion = null;

// A deploy bumps index.html's asset pins and restarts the server; its hash
// rides every board payload. A long-lived tab that sees the hash change
// reloads itself once instead of running weeks-old code against fresh data.
function checkUiVersion(version) {
  if (!version || dataIsCached) return; // restored payloads carry old stamps
  if (uiVersion === null) {
    uiVersion = version;
    return;
  }
  if (version === uiVersion) return;
  let guard = null;
  try {
    guard = sessionStorage.getItem("ui-reload-version");
    sessionStorage.setItem("ui-reload-version", version);
  } catch {
    // Storage unavailable: still reload — worst case is one extra reload.
  }
  if (guard === version) return; // this version already triggered a reload
  window.location.reload();
}

function applyQuotes(payload) {
  // A restored localStorage payload may be hours old: patch it from memory,
  // but never let it SEED the memory as fresh — that defeats the 30-min
  // age cap and re-persists ever-older funding across reload cycles.
  rememberAndPatchFunding(payload, { remember: !dataIsCached });
  latestData = payload;
  checkUiVersion(payload.ui_version);
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

function rememberAndPatchFunding(payload, { remember = true } = {}) {
  const now = Date.now();
  const patch = (target, symbol) => {
    if (!target || !symbol) return;
    if (typeof target.funding_rate === "number") {
      if (!remember) return;
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
function setNewsOpen(open, { focus = true } = {}) {
  document.body.classList.toggle("news-open", open);
  newsPanel.inert = !open;
  newsPanel.setAttribute("aria-hidden", String(!open));
  newsToggle.setAttribute("aria-pressed", String(open));
  try {
    localStorage.setItem(NEWS_OPEN_KEY, open ? "1" : "0");
  } catch (error) {
    // Private browsing can deny storage; the panel still opens for this session.
  }
  if (open && focus) {
    window.requestAnimationFrame(() => newsClose.focus());
  } else if (!open && newsPanel.contains(document.activeElement)) {
    newsToggle.focus();
  }
}

// --- Agent reports -----------------------------------------------------
// Markdown reports pushed by external agent cron jobs (POST /api/reports).
const REPORTS_SEEN_KEY = "reports-last-seen-v1";

function openReports() {
  openDialog(reportsModal, reportsCloseButton);
  showReportsList();
  resetReportsQuery();
  fetchReports();
}

function closeReports() {
  if (activeReportId !== null) reportOpenToken += 1;
  activeReportId = null;
  syncUrlState();
  closeDialog(reportsModal);
}

function showReportsList({ focus = false } = {}) {
  const hadOpenReport = activeReportId !== null;
  if (hadOpenReport) reportOpenToken += 1;
  activeReportId = null;
  if (hadOpenReport) syncUrlState();
  reportReaderElement.hidden = true;
  reportReaderElement.removeAttribute("aria-busy");
  reportsListElement.hidden = false;
  reportsBackButton.hidden = true;
  if (focus) {
    window.requestAnimationFrame(() => reportsListElement.querySelector(".report-card")?.focus());
  }
}

// --- Report library state: facet filter + cursor over the full archive ----
const REPORTS_PAGE_SIZE = 60;
let reportsFilterSlug = null;
let reportsItems = [];
let reportsHasMore = false;
let reportsFacets = [];
let reportsNextCursor = null;
let reportsFetchSeq = 0;
let reportsQueryGeneration = 0;

function resetReportsQuery() {
  reportsQueryGeneration += 1;
  reportsFetchSeq += 1;
  reportsItems = [];
  reportsHasMore = false;
  reportsNextCursor = null;
  reportsListElement.innerHTML = '<div class="empty-state">Loading reports…</div>';
}

async function fetchReports({ append = false } = {}) {
  const generation = reportsQueryGeneration;
  const filterSlug = reportsFilterSlug;
  const cursor = append ? reportsNextCursor : null;
  if (append && !cursor) return;
  const seq = ++reportsFetchSeq;
  try {
    const params = new URLSearchParams({ limit: String(REPORTS_PAGE_SIZE) });
    if (cursor) {
      params.set("before_date", cursor.date);
      params.set("before_created_at", cursor.created_at);
      params.set("before_id", String(cursor.id));
    }
    if (filterSlug) params.set("slug", filterSlug);
    const response = await fetch(`/api/reports?${params}`);
    if (!response.ok) throw new Error("reports_failed");
    const payload = await response.json();
    if (
      seq !== reportsFetchSeq ||
      generation !== reportsQueryGeneration ||
      filterSlug !== reportsFilterSlug
    ) return;
    const incoming = Array.isArray(payload?.reports) ? payload.reports : [];
    if (append) {
      const byId = new Map(reportsItems.map((item) => [item.id, item]));
      for (const item of incoming) byId.set(item.id, item);
      reportsItems = [...byId.values()];
    } else {
      reportsItems = incoming;
    }
    reportsNextCursor = payload?.next_cursor || null;
    reportsHasMore = Boolean(payload?.has_more && reportsNextCursor);
    if (Array.isArray(payload?.filters)) reportsFacets = payload.filters;
    renderReportsList();
    markReportsSeen(reportsItems, payload?.latest_update);
  } catch (error) {
    if (
      seq !== reportsFetchSeq ||
      generation !== reportsQueryGeneration ||
      filterSlug !== reportsFilterSlug
    ) return;
    if (append && reportsItems.length) {
      renderReportsList();
      return;
    }
    reportsListElement.innerHTML = '<div class="empty-state">Reports unavailable</div>';
  }
}

function renderReportsList() {
  const sections = [];
  if (reportsFacets.length > 1 || reportsFilterSlug) {
    const chips = [
      `<button type="button" class="report-filter${reportsFilterSlug ? "" : " active"}" data-slug="">All</button>`,
    ];
    for (const facet of reportsFacets) {
      const active = facet.slug === reportsFilterSlug ? " active" : "";
      chips.push(
        `<button type="button" class="report-filter${active}" data-slug="${escapeHtml(facet.slug)}">${escapeHtml(facet.title)}</button>`
      );
    }
    sections.push(`<div class="report-filters" role="group" aria-label="Filter reports">${chips.join("")}</div>`);
  }
  if (!reportsItems.length) {
    sections.push(
      '<div class="empty-state">No reports yet — point an agent cron job at POST /api/reports</div>'
    );
    reportsListElement.innerHTML = sections.join("");
    wireReportsListEvents();
    return;
  }
  const byDate = new Map();
  for (const item of reportsItems) {
    if (!byDate.has(item.date)) byDate.set(item.date, []);
    byDate.get(item.date).push(item);
  }
  for (const [date, items] of byDate) {
    sections.push(`<h3 class="reports-date">${escapeHtml(formatReportDate(date))}</h3>`);
    for (const item of items) {
      const stamp = formatReportStamp(item.created_at, date);
      sections.push(`
        <button type="button" class="report-card" data-report-id="${Number(item.id)}">
          <strong>${escapeHtml(item.title)}</strong>
          ${stamp ? `<time class="report-stamp" datetime="${escapeHtml(item.created_at)}" title="Landed ${escapeHtml(formatLandedFull(item.created_at))}">${escapeHtml(stamp)}</time>` : ""}
          <span>${escapeHtml(item.preview || "")}</span>
        </button>`);
    }
  }
  if (reportsHasMore) {
    sections.push(
      '<button type="button" class="report-load-more" id="reports-load-more">Load older reports</button>'
    );
  }
  reportsListElement.innerHTML = sections.join("");
  wireReportsListEvents();
}

function wireReportsListEvents() {
  reportsListElement.querySelectorAll(".report-card").forEach((card) => {
    card.addEventListener("click", () => openReport(Number(card.dataset.reportId)));
  });
  reportsListElement.querySelectorAll(".report-filter").forEach((chip) => {
    chip.addEventListener("click", () => {
      reportsFilterSlug = chip.dataset.slug || null;
      resetReportsQuery();
      fetchReports();
    });
  });
  document.querySelector("#reports-load-more")?.addEventListener("click", (event) => {
    event.target.disabled = true;
    fetchReports({ append: true });
  });
}



// "#report=12" (bare or trailing a full board URL) -> 12, else null.
function reportIdFromHref(href) {
  const hashIndex = href.indexOf("#");
  if (hashIndex === -1) return null;
  const value = new URLSearchParams(href.slice(hashIndex + 1)).get("report");
  if (!/^\d+$/.test(value || "")) return null;
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

function restoreReportNavigation() {
  const reportId = reportIdFromHref(window.location.href);
  restoringUrlState = true;
  try {
    if (reportId !== null) {
      openDialog(reportsModal, reportsCloseButton);
      openReport(reportId, { pushHistory: false });
    } else if (activeReportId !== null) {
      showReportsList();
    }
  } finally {
    restoringUrlState = false;
  }
}

async function openReport(reportId, { pushHistory = true } = {}) {
  reportId = Number(reportId);
  if (!Number.isSafeInteger(reportId) || reportId <= 0) return;
  const changed = activeReportId !== reportId;
  activeReportId = reportId;
  syncUrlState({ push: pushHistory && changed });
  // Rapid clicks race: the slower response must not render over the newer.
  const token = ++reportOpenToken;
  reportsListElement.hidden = true;
  reportsBackButton.hidden = false;
  reportReaderElement.hidden = false;
  reportReaderElement.setAttribute("aria-busy", "true");
  reportReaderElement.tabIndex = -1;
  reportReaderElement.innerHTML = '<div class="empty-state">Loading report</div>';
  reportReaderElement.focus();
  try {
    const response = await fetch(`/api/reports/${reportId}`);
    if (!response.ok) throw new Error("report_failed");
    const item = await response.json();
    if (token !== reportOpenToken) return;
    reportReaderElement.innerHTML = `
      <header class="report-head">
        <h2 tabindex="-1">${escapeHtml(item.title)}</h2>
        <p>${escapeHtml(formatReportDate(item.date))} · ${escapeHtml(item.slug)}${item.created_at ? escapeHtml(` · landed ${formatLandedFull(item.created_at)}`) : ""}</p>
      </header>
      <div class="report-body">${renderMarkdown(item.body)}</div>`;
    reportReaderElement.removeAttribute("aria-busy");
    reportReaderElement.scrollTop = 0;
    reportReaderElement.querySelector(".report-head h2")?.focus();
  } catch (error) {
    if (token !== reportOpenToken) return;
    reportReaderElement.removeAttribute("aria-busy");
    reportReaderElement.innerHTML = '<div class="empty-state" tabindex="-1">Report unavailable</div>';
    reportReaderElement.querySelector(".empty-state")?.focus();
  }
}

async function refreshReportsBadge() {
  const seq = ++reportsBadgeFetchSeq;
  try {
    const response = await fetch("/api/reports?limit=1");
    const payload = response.ok ? await response.json() : null;
    if (seq !== reportsBadgeFetchSeq) return;
    const newest = payload?.reports?.[0];
    const revision = payload?.latest_update || newest?.updated_at || newest?.created_at;
    if (!revision) {
      reportsBadge.hidden = true;
      return;
    }
    const seen = localStorage.getItem(REPORTS_SEEN_KEY) || "";
    reportsBadge.hidden = revision <= seen;
  } catch (error) {
    // Keep the prior badge state; a failed poll must not mark reports seen.
  }
}

function markReportsSeen(reports, latestUpdate = null) {
  // Watermark only advances: a filtered or paged view of older reports must
  // never regress "seen" and resurrect the badge.
  const newest = latestUpdate || reports[0]?.updated_at || reports[0]?.created_at;
  let seen = "";
  try {
    seen = localStorage.getItem(REPORTS_SEEN_KEY) || "";
  } catch (error) {
    // Private browsing can deny storage; treat everything as unseen.
  }
  if (newest && newest > seen) {
    try {
      localStorage.setItem(REPORTS_SEEN_KEY, newest);
    } catch (error) {
      // Storage denied: the badge still hides for this session.
    }
    reportsBadge.hidden = true;
  }
}

function formatReportDate(value) {
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-US", {
    weekday: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

// Exact landing time of a brief in the display zone. Overnight briefs are
// dated for the next session but land the evening before, so the calendar
// day is prefixed whenever it differs from the report's dated day.
function formatReportStamp(createdAt, reportDate) {
  const parsed = new Date(createdAt || "");
  if (Number.isNaN(parsed.getTime())) return "";
  const time = parsed.toLocaleTimeString("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
  });
  if (formatLocalDate(parsed) === reportDate) return time;
  const day = parsed.toLocaleDateString("en-US", {
    timeZone: DISPLAY_TIME_ZONE,
    month: "short",
    day: "numeric",
  });
  return `${day} · ${time}`;
}

function formatLandedFull(createdAt) {
  const parsed = new Date(createdAt || "");
  if (Number.isNaN(parsed.getTime())) return "";
  return `${formatLocalDate(parsed)} ${formatClock(parsed)}`;
}

// Minimal escape-first markdown renderer for trusted-ish agent reports:
// fences, headings, lists, tables, quotes, hr, bold/italic/code/links.
function renderMarkdown(source) {
  const lines = String(source || "").replaceAll("\r\n", "\n").split("\n");
  const html = [];
  let index = 0;
  // Skip a leading Obsidian/Jekyll YAML frontmatter block (--- ... ---).
  if (lines[0]?.trim() === "---") {
    const closing = lines.findIndex((line, i) => i > 0 && line.trim() === "---");
    if (closing > 0 && closing <= 40) index = closing + 1;
  }
  while (index < lines.length) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      const info = line.replace(/^\s*```/, "").trim().toLowerCase();
      const code = [];
      index += 1;
      while (index < lines.length && !/^\s*```/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1; // closing fence
      // ```chart fences carry a declarative spec rendered as inline SVG; a
      // malformed spec falls back to the plain code block, never a broken report.
      const chart = info === "chart" ? mdChartBlock(code.join("\n")) : null;
      html.push(chart ?? `<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      html.push(`<h${level}>${mdInline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      html.push("<hr>");
      index += 1;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quote.push(mdInline(lines[index].replace(/^\s*>\s?/, "")));
        index += 1;
      }
      html.push(`<blockquote>${quote.join("<br>")}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows = [];
      while (index < lines.length && /^\s*\|.*\|\s*$/.test(lines[index])) {
        rows.push(lines[index]);
        index += 1;
      }
      html.push(mdTable(rows));
      continue;
    }
    if (/^\s*(?:[-*+]|\d+[.)])\s+/.test(line)) {
      const list = mdListBlock(lines, index);
      html.push(list.html);
      index = list.next;
      continue;
    }
    const paragraph = [];
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^\s*(#{1,6}\s|```|>|\||[-*+]\s|\d+[.)]\s)/.test(lines[index])
    ) {
      // Hard-wrapped source lines are soft breaks (join with a space);
      // a trailing double space is a Markdown hard break.
      const raw = lines[index];
      const text = mdInline(raw.trim());
      paragraph.push(/\S {2,}$/.test(raw) ? `${text}<br>` : text);
      index += 1;
    }
    if (paragraph.length) {
      html.push(`<p>${paragraph.join(" ")}</p>`);
    } else {
      // Line matched a block prefix but produced no paragraph content;
      // consume it to guarantee forward progress.
      html.push(`<p>${mdInline(lines[index].trim())}</p>`);
      index += 1;
    }
  }
  return html.join("\n");
}

// Consume a run of list lines starting at `start`, honoring indentation-based
// nesting (Obsidian: 2 spaces or a tab per level) and hard-wrapped item
// continuations. Returns rendered HTML plus the index after the run.
function mdListBlock(lines, start) {
  const bullet = /^(\s*)(?:([-*+])|\d+[.)])\s+(.*)$/;
  const items = []; // { indent, ordered, parts }
  let index = start;
  while (index < lines.length) {
    const match = lines[index].match(bullet);
    if (match) {
      const indent = match[1].replaceAll("\t", "  ").length;
      items.push({ indent, ordered: match[2] === undefined, parts: [match[3]] });
      index += 1;
      continue;
    }
    // Indented continuation lines belong to the item above (hard-wrapped bullets).
    if (/^\s+\S/.test(lines[index]) && !/^\s*(#{1,6}\s|```|>|\|)/.test(lines[index])) {
      items[items.length - 1].parts.push(lines[index].trim());
      index += 1;
      continue;
    }
    break;
  }
  let pos = 0;
  function level(indent) {
    const tag = items[pos].ordered ? "ol" : "ul";
    const rendered = [];
    while (pos < items.length && items[pos].indent >= indent) {
      if (items[pos].indent > indent) {
        // Deeper item: nest a sublist inside the previous <li>.
        const nested = level(items[pos].indent);
        if (rendered.length) {
          rendered[rendered.length - 1] = rendered[rendered.length - 1].replace(/<\/li>$/, `${nested}</li>`);
        } else {
          rendered.push(`<li>${nested}</li>`);
        }
        continue;
      }
      rendered.push(`<li>${mdInline(items[pos].parts.join(" "))}</li>`);
      pos += 1;
    }
    return `<${tag}>${rendered.join("")}</${tag}>`;
  }
  // Items dedented below the run's first bullet end its level() walk; start
  // a fresh list for each remaining run so no consumed bullet is dropped.
  const blocks = [];
  while (pos < items.length) blocks.push(level(items[pos].indent));
  return { html: blocks.join(""), next: index };
}

function mdTable(rows) {
  const parsed = rows.map((row) =>
    row
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split("|")
      .map((cell) => mdInline(cell.trim()))
  );
  let header = null;
  if (parsed.length >= 2 && /^[\s:|-]+$/.test(rows[1].trim())) {
    header = parsed[0];
    parsed.splice(0, 2);
  }
  const head = header
    ? `<thead><tr>${header.map((cell) => `<th>${cell}</th>`).join("")}</tr></thead>`
    : "";
  const body = parsed
    .map((cells) => `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
    .join("");
  // Wide tables scroll inside the reader instead of stretching the modal.
  return `<div class="table-scroll"><table>${head}<tbody>${body}</tbody></table></div>`;
}

// --- report charts ---------------------------------------------------------
// Declarative ```chart blocks: "type/title/unit/labels/series" key-value
// lines, values comma-separated, an optional "Name:" prefix per series.
// Escape-first like the rest of the renderer; returns null on any parse
// problem so the caller can fall back to a plain code block.
const CHART_SERIES_COLORS = ["var(--accent)", "var(--green)", "var(--red)", "var(--neutral)"];

function mdChartBlock(source) {
  const spec = { type: "line", title: "", unit: "", labels: [], series: [] };
  for (const raw of String(source).split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const match = line.match(/^([a-z]+)\s*:\s*(.*)$/i);
    if (!match) return null;
    const key = match[1].toLowerCase();
    const value = match[2].trim();
    if (key === "type") {
      spec.type = value.toLowerCase();
    } else if (key === "title") {
      spec.title = value;
    } else if (key === "unit") {
      spec.unit = value;
    } else if (key === "labels") {
      spec.labels = value.split(",").map((item) => item.trim());
    } else if (key === "series") {
      const parsed = parseChartSeries(value);
      if (!parsed) return null;
      spec.series.push(parsed);
    } else {
      return null;
    }
  }
  const minPoints = spec.type === "bar" ? 1 : 2;
  if (
    !["line", "bar"].includes(spec.type) ||
    !spec.series.length ||
    spec.series.length > CHART_SERIES_COLORS.length ||
    (spec.type === "bar" && spec.series.length > 1) ||
    spec.series.some((series) => series.values.length < minPoints || series.values.length > 120)
  ) {
    return null;
  }
  return chartSvg(spec);
}

function parseChartSeries(value) {
  // "Name: 1, 2" names the series; the prefix only counts as a name when
  // the remainder still parses, so bare "1, 2" or "-0.5, 3" stay unnamed.
  const colon = value.indexOf(":");
  let name = "";
  let numbersRaw = value;
  if (colon !== -1) {
    const candidate = value.slice(colon + 1).trim();
    if (candidate && parseChartNumbers(candidate)) {
      name = value.slice(0, colon).trim();
      numbersRaw = candidate;
    }
  }
  const values = parseChartNumbers(numbersRaw);
  return values ? { name, values } : null;
}

function parseChartNumbers(raw) {
  const values = raw.split(",").map((item) => Number(item.trim()));
  return values.length && values.every(Number.isFinite) ? values : null;
}

function chartNumber(value) {
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${trimZeros((value / 1e9).toFixed(2))}B`;
  if (abs >= 1e6) return `${trimZeros((value / 1e6).toFixed(2))}M`;
  if (abs >= 1e4) return `${trimZeros((value / 1e3).toFixed(1))}K`;
  return trimZeros(value.toFixed(2));
}

function trimZeros(text) {
  return text.replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
}

function chartSvg(spec) {
  const W = 640;
  const H = 220;
  const padTop = 10;
  const padLeft = 10;
  const padRight = 10;
  const plotBottom = H - (spec.labels.length ? 26 : 12);
  const plotW = W - padLeft - padRight;
  const plotH = plotBottom - padTop;
  const all = spec.series.flatMap((series) => series.values);
  let min = Math.min(...all);
  let max = Math.max(...all);
  if (spec.type === "bar") {
    min = Math.min(min, 0);
    max = Math.max(max, 0);
  }
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const y = (value) => padTop + ((max - value) / (max - min)) * plotH;
  const parts = [];

  if (min < 0 && max > 0) {
    const zero = y(0).toFixed(1);
    parts.push(`<line x1="${padLeft}" y1="${zero}" x2="${W - padRight}" y2="${zero}" stroke="var(--line)" stroke-dasharray="3 3"/>`);
  }

  const count = Math.max(...spec.series.map((series) => series.values.length));
  if (spec.type === "bar") {
    const values = spec.series[0].values;
    const slot = plotW / values.length;
    const barW = Math.max(slot * 0.62, 1);
    values.forEach((value, i) => {
      const x = padLeft + i * slot + (slot - barW) / 2;
      const y0 = y(0);
      const yv = y(value);
      const top = Math.min(y0, yv);
      const height = Math.max(Math.abs(y0 - yv), 1);
      const tone = value < 0 ? "negative" : "positive";
      const fill = value < 0 ? "var(--red)" : "var(--green)";
      parts.push(
        `<rect class="chart-bar ${tone}" x="${x.toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${height.toFixed(1)}" fill="${fill}"/>`
      );
    });
  } else {
    spec.series.forEach((series, seriesIndex) => {
      const step = plotW / (series.values.length - 1);
      const points = series.values
        .map((value, i) => `${(padLeft + i * step).toFixed(1)},${y(value).toFixed(1)}`)
        .join(" ");
      parts.push(
        `<polyline points="${points}" fill="none" stroke="${CHART_SERIES_COLORS[seriesIndex]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`
      );
    });
  }

  const unitSuffix = spec.unit ? ` ${escapeHtml(spec.unit)}` : "";
  parts.push(
    `<text x="${padLeft + 2}" y="${padTop + 10}" class="chart-axis">${escapeHtml(chartNumber(max))}${unitSuffix}</text>`,
    `<text x="${padLeft + 2}" y="${(plotBottom - 4).toFixed(1)}" class="chart-axis">${escapeHtml(chartNumber(min))}${unitSuffix}</text>`
  );

  if (spec.labels.length) {
    const shown = Math.min(spec.labels.length, 8);
    for (let i = 0; i < shown; i += 1) {
      const sourceIndex = Math.round((i * (spec.labels.length - 1)) / Math.max(shown - 1, 1));
      const x = spec.labels.length === 1
        ? padLeft + plotW / 2
        : padLeft + (sourceIndex * plotW) / (spec.labels.length - 1);
      parts.push(
        `<text x="${x.toFixed(1)}" y="${H - 8}" class="chart-axis" text-anchor="middle">${escapeHtml(spec.labels[sourceIndex] || "")}</text>`
      );
    }
  }

  const legend = spec.series.some((series) => series.name)
    ? `<div class="report-chart-legend">${spec.series
        .map((series, i) => {
          const last = series.values[series.values.length - 1];
          return `<span><i style="background:${CHART_SERIES_COLORS[i]}"></i>${escapeHtml(series.name || `Series ${i + 1}`)} ${escapeHtml(chartNumber(last))}${unitSuffix}</span>`;
        })
        .join("")}</div>`
    : "";
  const caption = spec.title ? `<figcaption>${escapeHtml(spec.title)}</figcaption>` : "";
  const label = escapeHtml(spec.title || "Report chart");
  return `<figure class="report-chart">${caption}<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}" preserveAspectRatio="none">${parts.join("")}</svg>${legend}</figure>`;
}

function mdInline(text) {
  let out = escapeHtml(text);
  // Obsidian wiki-links/embeds -> plain text; [[Target|Alias]] keeps the alias.
  out = out.replace(/!?\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2");
  out = out.replace(/!?\[\[([^\]]+)\]\]/g, "$1");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  out = out.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  out = out.replace(
    /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  // Hash-only links ([Mon](#report=12)) stay in-app: the report reader
  // intercepts anchors carrying a report id and navigates without a reload.
  out = out.replace(/\[([^\]]+)\]\((#[^)\s]+)\)/g, '<a href="$2">$1</a>');
  // Bare URLs (agents cite sources as "Name (https://...)") become links
  // too. Split around anchors/code the passes above produced so hrefs and
  // code spans are never re-linkified. Parens and trailing punctuation stay
  // outside the link; text is already escaped, so &amp; in queries is safe.
  out = out
    .split(/(<a [^>]*>.*?<\/a>|<code>.*?<\/code>)/g)
    .map((part) =>
      part.startsWith("<a ") || part.startsWith("<code>")
        ? part
        : part.replace(
            /https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"]/g,
            (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
          )
    )
    .join("");
  return out;
}

async function fetchNews() {
  const seq = ++newsFetchSeq;
  try {
    const response = await fetch("/api/news");
    if (!response.ok) throw new Error("news_failed");
    const payload = await response.json();
    // A slower poll must never overwrite fresher news (a WS frame bumps
    // newsFetchApplied, so stale in-flight polls drop here).
    if (seq <= newsFetchApplied) return;
    newsFetchApplied = seq;
    renderNews(payload);
  } catch (error) {
    if (seq <= newsFetchApplied) return;
    if (!latestNews) {
      newsList.innerHTML = '<div class="empty-state">News feed unavailable</div>';
    }
  }
}

function renderNews(payload) {
  const items = payload?.items || [];
  latestNews = payload;
  const updated = payload?.updated_at ? new Date(payload.updated_at) : null;
  const updatedText = updated && !Number.isNaN(updated.getTime()) ? formatClock(updated) : "";
  const stale = Boolean(payload?.is_stale);
  newsStatus.textContent = [updatedText, stale ? "STALE" : ""].filter(Boolean).join(" · ");
  newsStatus.classList.toggle("stale", stale);
  const failedChannels = Array.isArray(payload?.failed_channels) ? payload.failed_channels : [];
  newsStatus.title = failedChannels.length
    ? `Unavailable: ${failedChannels.map((channel) => `@${channel}`).join(", ")}`
    : "";
  const renderKey = newsRenderKey(payload);
  if (renderKey === lastNewsRenderKey) {
    updateNewsAges();
    knownNewsIds = new Set(items.map((item) => item.id));
    return;
  }
  lastNewsRenderKey = renderKey;
  renderNewsChannels(payload);
  renderNewsControls();
  if (!items.length) {
    newsResultCount.textContent = "0";
    newsList.innerHTML = '<div class="empty-state">No posts yet</div>';
    knownNewsIds = new Set();
    return;
  }
  const unmuted = items.filter((item) => !mutedNewsChannels.has(item.channel));
  const deduplicated = deduplicateNewsItems(unmuted);
  const visible = deduplicated.filter(newsMatchesActiveFilter);
  newsResultCount.textContent = `${visible.length}/${items.length}`;
  if (!visible.length) {
    const channels = payload?.channels || [];
    const allMuted = channels.length > 0 && channels.every((ch) => mutedNewsChannels.has(ch));
    const filtered = newsFilter !== "all" || newsSearchQuery;
    newsList.innerHTML = `<div class="empty-state">${
      allMuted
        ? "All channels muted"
        : filtered
          ? "No matching news"
          : "No recent posts from unmuted channels"
    }</div>`;
  } else {
    const seenBefore = knownNewsIds.size > 0;
    // New posts prepend above the reading position; anchor the first visible
    // row so an update never swaps the article under the reader.
    const anchor = newsScrollAnchor();
    newsList.innerHTML = visible.map((item) => newsItemMarkup(item, seenBefore)).join("");
    restoreNewsScrollAnchor(anchor);
  }
  // Track ALL ids (muted, filtered, and deduplicated included) so changing a
  // view never fakes a "new" flash.
  knownNewsIds = new Set(items.map((item) => item.id));
}

function newsRenderKey(payload) {
  const items = payload?.items || [];
  const channels = payload?.channels || [];
  let hash = 2166136261;
  const include = (value) => {
    const text = String(value ?? "");
    hash = Math.imul(hash ^ text.length, 16777619);
    for (let index = 0; index < text.length; index += 1) {
      hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
    }
  };
  include(items.length);
  items.forEach((item) => {
    include(item.id);
    include(item.channel);
    include(item.channel_title);
    include(item.timestamp);
    include(item.link);
    include(item.text);
  });
  include(channels.length);
  channels.forEach(include);
  [...mutedNewsChannels].sort().forEach(include);
  include(newsFilter);
  include(newsSearchQuery);
  include(focusedSymbol);
  return String(hash >>> 0);
}

function renderNewsControls() {
  newsFilters.querySelectorAll("button[data-news-filter]").forEach((button) => {
    const filter = button.dataset.newsFilter || "all";
    const active = filter === newsFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    if (filter === "focus") {
      button.disabled = !focusedSymbol;
      button.title = focusedSymbol ? `News mentioning ${focusedSymbol}` : "Open an asset to set focus";
    }
  });
}

function newsMatchesActiveFilter(item) {
  const searchable = `${item.channel_title || item.channel || ""} ${item.text || ""}`.toLowerCase();
  if (newsSearchQuery && !searchable.includes(newsSearchQuery)) return false;
  if (newsFilter === "macro") return newsIsMacro(item);
  if (newsFilter === "universe") return newsMentionsAny(item, newsUniverseSymbols());
  if (newsFilter === "focus") return newsMentionsAny(item, symbolAliases(focusedSymbol));
  return true;
}

const NEWS_MACRO_PATTERN =
  /\b(?:fed|fomc|central bank|ecb|boj|boe|rates?|yield|treasur|inflation|cpi|ppi|payroll|jobs report|unemployment|gdp|pmi|dollar|dxy|tariff|sanction|crude|oil|gold)\b/i;

function newsIsMacro(item) {
  return NEWS_MACRO_PATTERN.test(`${item.channel_title || item.channel || ""} ${item.text || ""}`);
}

function newsUniverseSymbols() {
  const symbols = new Set();
  for (const group of latestData?.groups || []) {
    for (const asset of group.assets || []) {
      symbolAliases(asset.symbol).forEach((symbol) => symbols.add(symbol));
    }
  }
  for (const item of latestData?.crypto_tape || []) {
    symbolAliases(item.symbol).forEach((symbol) => symbols.add(symbol));
  }
  return symbols;
}

function symbolAliases(symbol) {
  const value = String(symbol || "").toUpperCase().replace(/^\^/, "");
  if (!value) return new Set();
  const aliases = new Set([value]);
  const root = value.split(/[=.-]/, 1)[0];
  if (root.length > 1) aliases.add(root);
  return aliases;
}

function newsMentionsAny(item, symbols) {
  if (!symbols.size) return false;
  const tokens = String(item.text || "").match(/\$?\^?[A-Z][A-Z0-9.=-]{1,14}/g) || [];
  return tokens.some((token) => symbols.has(token.replace(/^\$?\^/, "")));
}

function deduplicateNewsItems(items) {
  const kept = [];
  const fingerprints = [];
  for (const item of items) {
    const normalized = normalizeNewsText(item.text);
    const words = new Set(normalized.split(" ").filter((word) => word.length > 2));
    const stamp = Date.parse(item.timestamp || "");
    const duplicate = fingerprints.some((prior) => {
      if (
        !Number.isNaN(stamp) &&
        !Number.isNaN(prior.stamp) &&
        Math.abs(stamp - prior.stamp) > 12 * 3600 * 1000
      ) {
        return false;
      }
      if (normalized && normalized === prior.normalized) return true;
      if (words.size < 7 || prior.words.size < 7) return false;
      let overlap = 0;
      words.forEach((word) => {
        if (prior.words.has(word)) overlap += 1;
      });
      return overlap / Math.max(words.size, prior.words.size) >= 0.94;
    });
    if (duplicate) continue;
    kept.push(item);
    fingerprints.push({ normalized, words, stamp });
  }
  return kept;
}

function normalizeNewsText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\b(?:breaking|update|just in|alert)\b/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim()
    .replace(/\s+/g, " ");
}


function updateNewsAges() {
  newsList.querySelectorAll("time[data-news-timestamp]").forEach((element) => {
    element.textContent = newsAge(element.dataset.newsTimestamp || "");
  });
}

function newsScrollAnchor() {
  if (newsList.scrollTop <= 0) return null; // pinned to top: stay on newest
  const listTop = newsList.getBoundingClientRect().top;
  for (const item of newsList.querySelectorAll(".news-item[data-news-id]")) {
    const offset = item.getBoundingClientRect().top - listTop;
    if (offset + item.getBoundingClientRect().height > 0) {
      return { id: item.dataset.newsId, offset };
    }
  }
  return null;
}

function restoreNewsScrollAnchor(anchor) {
  if (!anchor) return;
  const item = newsList.querySelector(`.news-item[data-news-id="${cssEscape(anchor.id)}"]`);
  if (!item) return; // item evicted from the feed window; keep raw offset
  const listTop = newsList.getBoundingClientRect().top;
  newsList.scrollTop += item.getBoundingClientRect().top - listTop - anchor.offset;
}

function renderNewsChannels(payload) {
  const channels = payload?.channels || [];
  if (!channels.length) {
    newsChannelsBar.innerHTML = "";
    return;
  }
  const titles = new Map((payload?.items || []).map((item) => [item.channel, item.channel_title]));
  newsChannelsBar.innerHTML = channels
    .map((channel) => {
      const muted = mutedNewsChannels.has(channel);
      const label = titles.get(channel) || channel;
      return `<button type="button" class="news-channel-chip${muted ? " muted" : ""}"
        data-channel="${escapeHtml(channel)}" aria-pressed="${muted ? "false" : "true"}"
        title="${muted ? "Unmute" : "Mute"} @${escapeHtml(channel)}">${escapeHtml(label)}</button>`;
    })
    .join("");
}

function newsItemMarkup(item, seenBefore) {
  const fresh = seenBefore && !knownNewsIds.has(item.id);
  // Scraped links are untrusted: only http(s) may reach an href (blocks
  // javascript: and friends).
  const safeLink = /^https?:\/\//i.test(item.link || "") ? item.link : "#";
  return `<a class="news-item${fresh ? " news-new" : ""}" data-news-id="${escapeHtml(item.id)}" href="${escapeHtml(safeLink)}" target="_blank" rel="noopener">
    <div class="news-meta">
      <strong>${escapeHtml(item.channel_title || item.channel)}</strong>
      <time data-news-timestamp="${escapeHtml(item.timestamp)}" title="${escapeHtml(item.timestamp)}">${escapeHtml(newsAge(item.timestamp))}</time>
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
  const seq = ++cryptoEtfFlowsFetchSeq;
  try {
    const response = await fetch("/api/crypto-etf-flows");
    if (!response.ok) throw new Error("crypto_etf_flows_failed");
    const payload = await response.json();
    // A slower response must never overwrite a fresher one.
    if (seq <= cryptoEtfFlowsFetchApplied) return;
    cryptoEtfFlowsFetchApplied = seq;
    latestCryptoEtfFlows = payload;
  } catch (error) {
    if (seq <= cryptoEtfFlowsFetchApplied) return;
    // Keep the last good payload: a transient fetch error must not blank
    // panels that already show flows.
    if (!latestCryptoEtfFlows || latestCryptoEtfFlows.status !== "ok") {
      latestCryptoEtfFlows = {
        status: "unavailable",
        source: "farside",
        error: "crypto_etf_flows_failed",
        assets: [],
      };
    }
  }
  if (latestData?.overview) {
    renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
    updateHeader(latestData.overview);
  }
}

async function fetchKeyDates() {
  const seq = ++keyDatesFetchSeq;
  try {
    const response = await fetch("/api/key-dates?days=90");
    if (!response.ok) throw new Error("key_dates_failed");
    const payload = await response.json();
    // A slower response must never overwrite a fresher one.
    if (seq <= keyDatesFetchApplied) return;
    keyDatesFetchApplied = seq;
    applyKeyDates(payload);
  } catch (error) {
    if (seq <= keyDatesFetchApplied) return;
    // Keep the last good payload: a transient fetch error must not blank
    // a calendar that already renders events.
    if (!latestKeyDates || latestKeyDates.error) {
      applyKeyDates({ key_dates: [], as_of: "", error: "key_dates_failed" });
    }
  }
}

// Store a key-dates payload and repaint the daily board — the single apply
// path shared by the HTTP poll and the WS "key_dates" push.
function applyKeyDates(payload) {
  latestKeyDates = payload;
  keyDatesRevision += 1;
  renderCatalystStrip(payload);
  if (latestData?.overview) renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
}

// HOT window shared with the backend enrichment: from 2 minutes before the
// scheduled print until 45 minutes after, while the actual has not landed.
function hasHotKeyDate() {
  const items = Array.isArray(latestKeyDates?.key_dates) ? latestKeyDates.key_dates : [];
  const now = Date.now();
  return items.some((item) => {
    const release = item.release;
    if (!release || release.actual != null) return false;
    const at = Date.parse(release.time_utc || "");
    if (Number.isNaN(at)) return false;
    return now >= at - 120000 && now <= at + 2700000;
  });
}

async function fetchSnapshots() {
  const seq = ++snapshotsFetchSeq;
  try {
    const response = await fetch("/api/snapshots?days=30");
    if (!response.ok) throw new Error("snapshots_failed");
    const payload = await response.json();
    // A slower response must never overwrite a fresher one.
    if (seq <= snapshotsFetchApplied) return;
    snapshotsFetchApplied = seq;
    latestSnapshots = Array.isArray(payload.snapshots) ? payload.snapshots : [];
    snapshotRevision += 1;
  } catch (error) {
    if (seq <= snapshotsFetchApplied) return;
    latestSnapshots = latestSnapshots || [];
  }
  if (latestData?.overview) renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
}

async function fetchFringe() {
  const seq = ++fringeFetchSeq;
  try {
    const response = await fetch("/api/fringe");
    if (!response.ok) throw new Error("fringe_failed");
    const payload = await response.json();
    // A slower response must never overwrite a fresher one.
    if (seq <= fringeFetchApplied) return;
    fringeFetchApplied = seq;
    latestFringe = payload;
    fringeRevision += 1;
    if (latestData?.overview) renderDailyBoard(latestData.overview, latestCryptoEtfFlows);
    renderFringeView();
  } catch (error) {
    // Keep the last good book: a transient fetch error must not blank a
    // panel that already renders ideas, and while nothing ever loaded the
    // panel simply stays hidden — the rest of the board is untouched.
  }
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
  macroStrip.innerHTML = items.map((item) => macroItem(item, macroFlashClass(item))).join("");
  macroStrip.hidden = false;
}

// The macro strip is innerHTML-swapped each poll, so a flash class on the
// fresh node replays the CSS animation exactly when the value moved.
const prevMacroLasts = new Map();

function macroFlashClass(item) {
  const key = item.label || "";
  const previous = prevMacroLasts.get(key);
  const current = typeof item.last === "number" ? item.last : null;
  if (current !== null) prevMacroLasts.set(key, current);
  if (typeof previous !== "number" || current === null || current === previous) return "";
  return current > previous ? " flash-up" : " flash-down";
}

function macroItem(item, flashClass = "") {
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
  return `<span class="macro-item${item.is_stale ? " stale" : ""}${flashClass}" title="${escapeHtml(title)}"><label>${escapeHtml(item.label)}</label><strong>${escapeHtml(value)}</strong><em class="${tone}">${escapeHtml(change)}</em></span>`;
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
// First data paint gets a one-time staggered rise-in; every later reconcile
// must stay motionless so 10s refreshes never pulse the board.
let dailyBoardBooted = false;
// Per-chunk HTML cache for reconcileDailyPanels: only top-level chunks
// whose markup actually changed get replaced. The old full innerHTML swap
// rebuilt every DOM node on each ~10s overview refresh, which killed
// in-flight scroll momentum (worst when scrolling up through the board)
// and reset inner scrollers.
let dailyPanelHtml = new Map();

function renderDailyBoard(overview, cryptoEtfFlows) {
  if (!overview) {
    dailyBoard.innerHTML = '<div class="empty-state">Market read unavailable</div>';
    lastDailyRenderKey = "";
    dailyPanelHtml = new Map();
    return;
  }
  // The overview timestamp and flow revision are supplied by their producers;
  // snapshotRevision also invalidates same-date snapshot corrections.
  const renderKey = [
    overview.as_of || "",
    cryptoEtfFlows?.status || "",
    cryptoEtfFlows?.updated_at || "",
    snapshotRevision,
    keyDatesRevision,
    fringeRevision,
  ].join("|");
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

  const chunks = [
    ["regime", `<section class="analytics-panel">
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
          `tone-${classToken(regime.tone)}`
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
    </section>`],

    ["benchmarks", `<div class="analytics-grid triple">
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
    </div>`],

    ["themes", `<div class="analytics-grid equal">
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
    </div>`],

    ["flows", `<section class="analytics-panel">
      ${panelHeading(
        "Crypto ETF Flows",
        cryptoEtfFlowNote(cryptoEtfFlows),
        "Daily net money moving into (+) or out of (\u2212) the US spot Bitcoin, Ether and Solana ETFs, from Farside data. Inflows mean investors are buying ETF shares and the funds must buy the coins. 5D/10D = flows summed over the last 5 and 10 trading days."
      )}
      ${cryptoEtfFlowPanel(cryptoEtfFlows)}
    </section>`],

    ["key-dates", keyDatesSection(latestKeyDates)],

    ["fringe", fringeSection(latestFringe)],

    ["rotation", `<section class="analytics-panel">
      ${panelHeading(
        "Theme Rotation",
        "1D move versus 5D daily pace",
        "Money rotating between sectors. Climbers trade faster than their own 5-day pace today - attention is arriving; fallers trade slower - attention is leaving. Pace is in %-points per day, so it spots turns earlier than raw returns."
      )}
      <div class="rotation-grid">
        ${rotationColumn("↑ Climbers", rotation.climbers || [])}
        ${rotationColumn("↓ Fallers", rotation.fallers || [])}
      </div>
    </section>`],
  ].filter(([, html]) => Boolean(html));

  // Engines without scroll anchoring (Safari) can clamp the page scroll
  // while containers are swapped; pin it across the reconcile. Inner
  // scrollers of replaced chunks are restored by tag.
  const scroller = document.scrollingElement || document.documentElement;
  const pageScroll = scroller.scrollTop;
  const innerScroll = {};
  dailyBoard.querySelectorAll("[data-scroll-keep]").forEach((el) => {
    if (el.scrollTop > 0) innerScroll[el.dataset.scrollKeep] = el.scrollTop;
  });

  reconcileDailyPanels(chunks);

  if (!dailyBoardBooted) {
    dailyBoardBooted = true;
    dailyBoard.classList.add("board-boot");
    window.setTimeout(() => dailyBoard.classList.remove("board-boot"), 1400);
  }

  if (scroller.scrollTop !== pageScroll) scroller.scrollTop = pageScroll;
  Object.entries(innerScroll).forEach(([key, top]) => {
    const el = dailyBoard.querySelector(`[data-scroll-keep="${CSS.escape(key)}"]`);
    if (el && el.scrollTop !== top) el.scrollTop = top;
  });
}

function reconcileDailyPanels(chunks) {
  const previous = dailyPanelHtml;
  dailyPanelHtml = new Map(chunks);
  const keep = new Map();
  for (const child of Array.from(dailyBoard.children)) {
    const key = child.dataset ? child.dataset.panel : undefined;
    if (key && dailyPanelHtml.has(key) && !keep.has(key)) keep.set(key, child);
    else child.remove(); // loading placeholder, removed panel, or duplicate
  }
  const template = document.createElement("template");
  let anchor = null;
  for (const [key, html] of chunks) {
    let node = keep.get(key);
    if (node === undefined || previous.get(key) !== html) {
      template.innerHTML = html;
      const fresh = template.content.firstElementChild;
      if (fresh === null) continue;
      fresh.dataset.panel = key;
      if (node) node.replaceWith(fresh);
      else if (anchor) anchor.after(fresh);
      else dailyBoard.prepend(fresh);
      node = fresh;
    }
    anchor = node;
  }
}

// Chart and filter clicks are delegated once: reconciled panels swap their
// subtrees, so per-render listener attachment would either leak or miss.
dailyBoard.addEventListener("click", (event) => {
  const card = event.target.closest(".benchmark-card");
  if (card) {
    openChart({
      symbol: card.dataset.symbol,
      name: card.dataset.name,
      type: card.dataset.type || "etf",
      quote: { provider: card.dataset.provider || "" },
      summary: findAssetSummary(card.dataset.symbol),
    });
    return;
  }
  const themeLink = event.target.closest(".theme-link");
  if (themeLink) {
    filterMarketsByGroup(themeLink.dataset.group || "");
    return;
  }
  // Fringe tickers are arbitrary Hermes symbols: resolve through the
  // watchlist first (full asset config), then the crypto tape (perp quote
  // context), else fall back to a bare Yahoo equity — the same resolution
  // order deep links use. A failed chart load already shows chart-error.
  const fringeButton = event.target.closest(".fringe-ticker");
  if (fringeButton) openFringeTicker(fringeButton.dataset.symbol || "");
});

function openFringeTicker(symbol) {
  if (!symbol) return;
  const asset = findAssetConfig(symbol);
  if (asset) {
    openChart(asset);
  } else if ((latestData?.crypto_tape || []).some((row) => row.symbol === symbol)) {
    openTapeChart(symbol);
  } else {
    openChart({ symbol, type: "equity", quote: { provider: "yahoo" } });
  }
}

fringeBoard?.addEventListener("click", (event) => {
  const fringeButton = event.target.closest(".fringe-ticker");
  if (fringeButton) openFringeTicker(fringeButton.dataset.symbol || "");
});

function panelHeading(title, note, tip = "") {
  const help = tip
    ? `<button type="button" class="help-tip" aria-label="What is ${escapeHtml(title)}?" data-tip="${escapeHtml(tip)}"><span class="help-tip-mark" aria-hidden="true">?</span></button>`
    : "";
  return `<header class="panel-heading"><h2>${escapeHtml(title)}${help}</h2><span>${escapeHtml(note || "")}</span></header>`;
}

function setupHelpTooltips() {
  document.addEventListener("pointerover", (event) => {
    const trigger = event.target.closest?.(".help-tip");
    if (trigger) showHelpTooltip(trigger);
  });
  document.addEventListener("pointerout", (event) => {
    const trigger = event.target.closest?.(".help-tip");
    if (
      trigger &&
      !trigger.contains(event.relatedTarget) &&
      document.activeElement !== trigger
    ) {
      hideHelpTooltip(trigger);
    }
  });
  document.addEventListener("focusin", (event) => {
    const trigger = event.target.closest?.(".help-tip");
    if (trigger) showHelpTooltip(trigger);
  });
  document.addEventListener("focusout", (event) => {
    const trigger = event.target.closest?.(".help-tip");
    if (trigger && !trigger.matches(":hover")) hideHelpTooltip(trigger);
  });
  document.addEventListener("scroll", () => hideHelpTooltip(), true);
  window.addEventListener("resize", () => hideHelpTooltip());
}

function showHelpTooltip(trigger) {
  if (!trigger.dataset.tip) return;
  if (activeHelpTip !== trigger) hideHelpTooltip();
  activeHelpTip = trigger;
  trigger.classList.add("tooltip-active");
  trigger.setAttribute("aria-describedby", helpTooltip.id);
  helpTooltip.textContent = trigger.dataset.tip;
  helpTooltip.hidden = false;
}

function hideHelpTooltip(trigger = activeHelpTip) {
  if (!trigger || trigger !== activeHelpTip) return;
  trigger.classList.remove("tooltip-active");
  trigger.removeAttribute("aria-describedby");
  activeHelpTip = null;
  helpTooltip.hidden = true;
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
    `tone-${classToken(vix.tone)}`
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
      `${formatInteger(cb.total)} Hyperliquid perps`,
      "Same participation check for the whole crypto market: every perp listed on Hyperliquid. Median 1D = the typical coin's day. Funding > 0 = share of markets where longs pay shorts, a proxy for bullish positioning. High advance % with high funding = crowded optimism."
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

// The breadth sparkline draws itself in once; chunk reconciles must not
// replay the stroke animation on every overview refresh.
let breadthSparkDrawn = false;

function breadthTrendRow() {
  const series = (latestSnapshots || [])
    .map((snap) => numericOrNull(snap.universe?.above_50dma_pct))
    .filter((value) => value !== null);
  if (series.length < 2) return "";
  const animate = !breadthSparkDrawn;
  breadthSparkDrawn = true;
  return `<div class="breadth-row" title="% of universe above 50DMA across the last ${series.length} daily snapshots"><span>50DMA trend</span><span class="breadth-spark">${sparklineSvg(series, animate)}</span></div>`;
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
    <td><span class="status-tag status-${classToken(theme.status)}">${escapeHtml(theme.status || "NEUTRAL")}</span></td>
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

const KEY_DATE_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function renderCatalystStrip(payload) {
  const items = Array.isArray(payload?.key_dates) ? payload.key_dates : [];
  const asOf = payload?.as_of || "";
  const upcoming = items
    .filter((item) => {
      const diff = keyDateDayDiff(item.date, asOf);
      return diff > 0 || (diff === 0 && item.release?.actual == null);
    })
    .slice(0, 4);
  catalystStrip.hidden = upcoming.length === 0;
  if (!upcoming.length) {
    catalystStrip.innerHTML = "";
    return;
  }
  catalystStrip.innerHTML = `<span class="catalyst-label">Next</span>
    <div class="catalyst-items">${upcoming.map((item) => catalystItemMarkup(item, asOf)).join("")}</div>
    <span class="catalyst-more">Key dates \u2192</span>`;
}

function catalystItemMarkup(item, asOf) {
  const diff = keyDateDayDiff(item.date, asOf);
  const when =
    diff === 0
      ? "Today"
      : diff === 1
        ? "Tomorrow"
        : keyDateShort(item.date);
  const time = item.time ? ` \u00b7 ${item.time}` : "";
  return `<span class="catalyst-item">
    <strong>${escapeHtml(when + time)}</strong>
    <span>${escapeHtml(item.title || "Market event")}</span>
    <em>${escapeHtml(item.category || "EVENT")}</em>
  </span>`;
}

function keyDatesSection(payload) {
  const items = Array.isArray(payload?.key_dates) ? payload.key_dates : [];
  return `<section class="analytics-panel">
    ${panelHeading(
      "Key Dates",
      keyDatesNote(items),
      'Upcoming market events fed by agent reports. Live figures are matched at runtime to TradingView economic-calendar series; each enriched row links to the exact series. Times show the timezone the agent wrote; days count on the US Eastern trading date.'
    )}
    ${keyDatesList(items, payload)}
  </section>`;
}

function keyDatesNote(items) {
  if (!items.length) return "Agent-fed calendar";
  const span =
    items.length === 1
      ? keyDateShort(items[0].date)
      : `${keyDateShort(items[0].date)} \u2013 ${keyDateShort(items[items.length - 1].date)}`;
  return `${span} \u00b7 ${items.length} event${items.length === 1 ? "" : "s"}`;
}

function keyDateShort(date) {
  const [, month, day] = String(date || "").split("-").map(Number);
  if (!month || !day) return "";
  return `${KEY_DATE_MONTHS[month - 1]} ${day}`;
}

function keyDatesList(items, payload) {
  if (!items.length) {
    const copy = !payload
      ? "Loading key dates"
      : payload.error
        ? "Key dates unavailable"
        : 'No key dates yet \u2014 add a "## Key Dates" section to an agent report';
    return `<div class="empty-state">${copy}</div>`;
  }
  const asOf = payload?.as_of || "";
  return `<div class="key-dates-list" data-scroll-keep="key-dates">${items.map((item) => keyDateRow(item, asOf)).join("")}</div>`;
}


function keyDateRow(item, asOf) {
  const [, month, day] = String(item.date || "").split("-").map(Number);
  const diff = keyDateDayDiff(item.date, asOf);
  const relative =
    diff === 0 ? "today" : diff === 1 ? "tomorrow" : diff > 1 ? `in ${diff} days` : "";
  const release = item.release || null;
  const series = [release?.country, release?.matched_title].filter(Boolean).join(" \u00b7 ");
  const meta = [relative, item.time, series].filter(Boolean).join(" \u00b7 ");
  const countdown = keyDateCountdownText(item.time_utc);
  const countdownHtml = countdown
    ? `<em class="key-date-countdown" data-countdown-utc="${escapeHtml(item.time_utc)}">${escapeHtml(countdown)}</em>`
    : "";
  // The indicator description rides as a native title: the list scrolls
  // (overflow-y: auto), which would clip the help-tip CSS popover.
  const tooltipText = [
    release?.comment,
    release?.source ? `Data source: ${release.source}` : "",
  ].filter(Boolean).join(" \u00b7 ");
  const tooltip = tooltipText ? ` title="${escapeHtml(tooltipText)}"` : "";
  const high = release?.importance === 1;
  // Calendar URLs arrive from scraped/config data: only http(s) may become
  // an href (blocks javascript: and friends), same guard as the news feed.
  const rawHref = release?.series_url || "";
  const href = /^https?:\/\//i.test(rawHref) ? rawHref : "";
  const [tagOpen, tagClose] = href
    ? [`<a href="${escapeHtml(href)}" target="_blank" rel="noopener"`, "</a>"]
    : ["<div", "</div>"];
  return `${tagOpen} class="key-date-row${diff === 0 ? " is-today" : ""}"${tooltip}>
    <span class="key-date-chip"><em>${month ? KEY_DATE_MONTHS[month - 1] : "--"}</em><strong>${day || "--"}</strong></span>
    <div class="key-date-main">
      <strong>${escapeHtml(item.title)}</strong>
      ${countdownHtml || meta ? `<span>${countdownHtml}${escapeHtml(meta)}</span>` : ""}
      ${release ? keyDateFigures(release) : ""}
    </div>
    <span class="key-date-tags">${high ? '<span class="key-date-tag key-tag-high">HIGH</span>' : ""}<span class="key-date-tag key-tag-${classToken(item.category, "event")}">${escapeHtml(item.category || "EVENT")}</span></span>
  ${tagClose}`;
}

// Countdown for the Key Dates rail: readers in any timezone see "in 1h 42m"
// instead of converting the event's printed zone by hand. Instants come from
// the payload's per-item time_utc (matched calendar row, else the stored
// zoned time). Only near events tick — beyond 48h the day label reads better.
function keyDateCountdownText(timeUtc) {
  const at = Date.parse(timeUtc || "");
  if (Number.isNaN(at)) return null;
  const delta = at - Date.now();
  // Keep "due" on screen through the hot window so a reader arriving right
  // at release time sees the print is in flight, not a vanished timer.
  if (delta <= 0) return delta > -45 * 60000 ? "due" : null;
  if (delta > 48 * 3600000) return null;
  const minutes = Math.round(delta / 60000);
  if (minutes < 1) return "due";
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `in ${hours}h ${rest}m` : `in ${hours}h`;
}

function refreshKeyDateCountdowns() {
  document.querySelectorAll(".key-date-countdown").forEach((element) => {
    const text = keyDateCountdownText(element.dataset.countdownUtc);
    if (text) {
      element.textContent = text;
      element.hidden = false;
    } else {
      element.hidden = true;
    }
  });
}

function keyDateFigures(release) {
  const dash = "\u2014";
  const est = escapeHtml(release.forecast ?? dash);
  const prev = escapeHtml(release.previous ?? dash);
  let act = dash;
  if (release.actual != null) {
    // Beat/miss direction depends on the indicator (a hot CPI is bad news,
    // hot payrolls good), so the surprise delta keeps the neutral accent —
    // never green/red. Sub-cent surprises round to +0.00 noise; skip them.
    const delta =
      typeof release.surprise === "number" && Number(release.surprise.toFixed(2)) !== 0
        ? ` <em class="key-date-surprise">${escapeHtml(formatSigned(release.surprise))}</em>`
        : "";
    act = `<strong>${escapeHtml(release.actual)}</strong>${delta}`;
  }
  return `<span class="key-date-figures">EST ${est} \u00b7 PREV ${prev} \u00b7 ACT ${act}</span>`;
}

function keyDateDayDiff(date, asOf) {
  // Date-only ISO strings parse as UTC midnight, so the difference is an
  // exact whole number of days — no DST wobble.
  const event = Date.parse(date || "");
  const anchor = Date.parse(asOf || "");
  if (Number.isNaN(event) || Number.isNaN(anchor)) return NaN;
  return Math.round((event - anchor) / 86400000);
}

// --- Fringe Corner ---------------------------------------------------------
// Hermes' trading-ideas book, parsed server-side from uploaded reports.
// Entry prices are stamped at ingest; open ideas mark to market on the
// backend, so this is pure presentation. An empty (or never-loaded) book
// renders nothing at all — no empty shell next to the key-dates rail.
function fringeSection(payload) {
  const open = Array.isArray(payload?.open) ? payload.open : [];
  const closed = Array.isArray(payload?.closed) ? payload.closed : [];
  if (!open.length && !closed.length) return "";
  // The header always derives from the rows this panel actually renders.
  // The server summary averages EVERY closed idea in history, so once older
  // closes age out of the recent list the two silently diverge — the visible
  // table is the only truth worth printing.
  let total = 0;
  let marked = 0;
  for (const idea of open) {
    const pct = numericOrNull(idea.unrealized_pct);
    if (pct === null) continue;
    total += pct;
    marked += 1;
  }
  for (const idea of closed) {
    const pct = numericOrNull(idea.realized_pct);
    if (pct === null) continue;
    total += pct;
    marked += 1;
  }
  const overallPnl = marked ? total / marked : null;
  const portfolio = payload?.summary?.portfolio || null;
  const equity = numericOrNull(portfolio?.equity);
  const returnPct = numericOrNull(portfolio?.return_pct);
  const note = [
    equity !== null
      ? `equity $${Math.round(equity).toLocaleString("en-US")}${returnPct !== null ? ` (${formatSignedPct(returnPct)})` : ""}`
      : "",
    open.length ? `${open.length} open` : "",
    closed.length ? `${closed.length} closed` : "",
    equity === null
      ? `overall P&L ${overallPnl === null ? "\u2014" : formatSignedPct(overallPnl)}`
      : "",
  ]
    .filter(Boolean)
    .join(" \u00b7 ");
  return `<section class="analytics-panel">
    ${panelHeading(
      "Fringe Corner",
      note,
      "Hermes' paper trading book: $10,000 starting capital, positions sized by half-Kelly from the agent's declared conviction ([conf]), stop, and target — floored at 2%, capped at 25% of the bankroll and 100% gross exposure. Sizes are fixed when the entry is stamped. Equity = capital + realized + marked-to-market open P&L. NOT REFRESHED means the newest report did not mention a still-open idea. Click a ticker to open its chart."
    )}
    ${fringeOpenTable(open)}
    ${fringeClosedList(closed)}
  </section>`;
}

function fringeOpenTable(open) {
  if (!open.length) return "";
  return `<div class="fringe-scroll" data-scroll-keep="fringe"><table class="fringe-table">
    <thead><tr>
      <th class="fringe-cell-ticker">Idea</th>
      <th class="fringe-cell-chip"></th>
      <th>Thesis</th>
      <th class="fringe-num">Entry</th>
      <th class="fringe-num">Last</th>
      <th class="fringe-num fringe-size">Size</th>
      <th class="fringe-num">P&amp;L</th>
      <th class="fringe-num">Target</th>
      <th class="fringe-num">To go</th>
      <th class="fringe-cell-horizon">Hrzn</th>
    </tr></thead>
    <tbody>${open.map(fringeOpenRow).join("")}</tbody>
  </table></div>`;
}

// One desktop blotter row per idea; phone CSS turns the same semantic table
// into a compact card so the thesis remains the primary content.
function fringeOpenRow(idea) {
  const direction = String(idea.direction || "").toUpperCase() === "SHORT" ? "SHORT" : "LONG";
  const ticker = escapeHtml(idea.ticker || "");
  const pct = numericOrNull(idea.unrealized_pct);
  const usd = numericOrNull(idea.unrealized_usd);
  const pnlTitle = usd === null ? "" : ` title="${escapeHtml(formatSignedUsd(usd))} on the position"`;
  // A null mark (provider miss; the backend retries lazily) reads as a
  // muted em dash — never a fake 0.00%.
  const pnl =
    pct === null
      ? '<strong class="fringe-pnl fringe-missing">\u2014</strong>'
      : `<strong class="fringe-pnl ${pct > 0 ? "tone-positive" : pct < 0 ? "tone-negative" : ""}"${pnlTitle}>${escapeHtml(formatSignedPct(pct))}</strong>`;
  const entry = numericOrNull(idea.entry_price);
  const last = numericOrNull(idea.last);
  const targetPrice = numericOrNull(idea.target_price);
  const toGo = numericOrNull(idea.to_target_pct);
  // A free-text target without a parseable price still shows verbatim;
  // the raw text always rides as the title.
  const target = idea.target
    ? `<span class="fringe-target" title="${escapeHtml(idea.target)}">${escapeHtml(targetPrice === null ? idea.target : formatPrice(targetPrice))}</span>`
    : '<span class="fringe-missing">\u2014</span>';
  const size = numericOrNull(idea.size_notional);
  const conf = numericOrNull(idea.confidence);
  const stopPrice = numericOrNull(idea.stop_price);
  const sizeTitle = [
    conf !== null ? `conf ${conf}%` : "",
    stopPrice !== null ? `stop ${formatPrice(stopPrice)}` : "",
    "half-Kelly sized",
  ]
    .filter(Boolean)
    .join(" \u00b7 ");
  const meta = [idea.opened ? `opened ${idea.opened}` : ""].filter(Boolean).join(" \u00b7 ");
  return `<tr class="fringe-row">
    <td class="fringe-cell-ticker"><button type="button" class="fringe-ticker" data-symbol="${ticker}" title="Open ${ticker} chart">${ticker}</button></td>
    <td class="fringe-cell-chip"><span class="fringe-chip fringe-${direction.toLowerCase()}">${direction}</span></td>
    <td class="fringe-cell-thesis">
      <span class="fringe-thesis">${escapeHtml(idea.thesis || "")}</span>
      <span class="fringe-meta">${escapeHtml(meta)}${idea.stale ? '<em class="fringe-stale">not refreshed</em>' : ""}</span>
    </td>
    <td class="fringe-num fringe-entry">${entry === null ? "\u2014" : escapeHtml(formatPrice(entry))}</td>
    <td class="fringe-num fringe-last">${last === null ? "\u2014" : escapeHtml(formatPrice(last))}</td>
    <td class="fringe-num fringe-size" data-mobile-label="Size" title="${escapeHtml(sizeTitle)}">${size === null ? "\u2014" : `$${Math.round(size).toLocaleString("en-US")}`}</td>
    <td class="fringe-num fringe-pnl-cell" data-mobile-label="P&amp;L">${pnl}</td>
    <td class="fringe-num fringe-target-cell" data-mobile-label="Target">${target}</td>
    <td class="fringe-num fringe-togo" data-mobile-label="To go">${toGo === null ? "\u2014" : escapeHtml(formatSignedPct(toGo))}</td>
    <td class="fringe-cell-horizon">${escapeHtml(idea.horizon || "\u2014")}</td>
  </tr>`;
}

// Compact realized-P&L footer: only the freshest 5 of the payload's 10
// closed ideas — a ledger tail, not a history browser. The full close
// reason rides as a native title since the row truncates it.
function fringeClosedList(closed) {
  if (!closed.length) return "";
  const rows = closed.slice(0, 5).map((idea) => {
    const pct = numericOrNull(idea.realized_pct);
    const tone = pct === null ? "" : pct > 0 ? "tone-positive" : pct < 0 ? "tone-negative" : "";
    const entry = numericOrNull(idea.entry_price);
    const exit = numericOrNull(idea.exit_price);
    const trip = `${entry === null ? "\u2014" : formatPrice(entry)} \u2192 ${exit === null ? "\u2014" : formatPrice(exit)}`;
    return `<div class="fringe-closed-row" title="${escapeHtml(idea.close_reason || "")}">
      <strong>${escapeHtml(idea.ticker || "")}</strong>
      <span>${escapeHtml(String(idea.direction || "").toUpperCase())}</span>
      <span class="fringe-closed-trip">${escapeHtml(trip)}</span>
      <em class="${tone}">${pct === null ? "\u2014" : escapeHtml(formatSignedPct(pct))}${(() => { const usd = numericOrNull(idea.realized_usd); return usd === null ? "" : ` <span class="fringe-closed-usd">${escapeHtml(formatSignedUsd(usd))}</span>`; })()}</em>
      <span class="fringe-closed-reason">${escapeHtml(idea.close_reason || "")}</span>
    </div>`;
  });
  return `<div class="fringe-closed">
    <span class="fringe-closed-label">Recently closed</span>
    ${rows.join("")}
  </div>`;
}

function formatSignedUsd(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "";
  const digits = Math.abs(value) >= 100 ? 0 : 2;
  const abs = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${value < 0 ? "\u2212" : "+"}$${abs}`;
}

// --- Fringe tab -------------------------------------------------------------
// The paper book's full record: stat cards, equity curve, the live open
// book, and every close. Renders from the same /api/fringe payload as the
// Daily panel; hidden-tab renders are cheap innerHTML swaps.
function renderFringeView() {
  if (!fringeBoard) return;
  const payload = latestFringe;
  if (!payload) {
    fringeBoard.innerHTML = '<div class="empty-state">Loading Fringe book</div>';
    return;
  }
  const open = Array.isArray(payload.open) ? payload.open : [];
  const closed = Array.isArray(payload.closed) ? payload.closed : [];
  if (!open.length && !closed.length) {
    fringeBoard.innerHTML =
      '<div class="empty-state">No Fringe book yet — ideas land with the daily Fringe Corner report</div>';
    return;
  }
  const portfolio = payload.summary?.portfolio || {};
  const stats = payload.stats || {};
  const curve = Array.isArray(payload.equity_curve) ? payload.equity_curve : [];
  const equity = numericOrNull(portfolio.equity);
  const returnPct = numericOrNull(portfolio.return_pct);
  const realized = numericOrNull(portfolio.realized_usd);
  const unrealized = numericOrNull(portfolio.unrealized_usd);
  const invested = numericOrNull(portfolio.invested_notional);
  const exposure = numericOrNull(portfolio.exposure_pct);
  const winRate = numericOrNull(stats.win_rate_pct);
  const maxDd = numericOrNull(stats.max_drawdown_pct);
  const profitFactor = numericOrNull(stats.profit_factor);
  const holdDays = numericOrNull(stats.avg_hold_days);
  const sharpe = numericOrNull(stats.sharpe_ratio);
  const tradeCount = numericOrNull(stats.trade_count) || 0;
  fringeBoard.innerHTML = `
    <section class="analytics-panel">
      ${panelHeading(
        "Paper Book",
        `since ${escapeHtml(curve[0]?.date || "\u2014")} \u00b7 $10,000 start`,
        "Hermes' half-Kelly paper portfolio. Equity = starting capital + realized dollars from every sized close + open positions marked to market. Realized compounds the bankroll; unrealized does not buy new size until banked."
      )}
      <div class="regime-grid">
        ${regimeCell("Equity", formatUsdWhole(equity), returnPct === null ? "" : `${formatSignedPct(returnPct)} since inception`, usdTone(returnPct))}
        ${regimeCell("Realized", realized === null ? "\u2014" : formatSignedUsd(realized), `${tradeCount} closed trade${tradeCount === 1 ? "" : "s"}`, usdTone(realized))}
        ${regimeCell("Unrealized", unrealized === null ? "\u2014" : formatSignedUsd(unrealized), `${open.length} open position${open.length === 1 ? "" : "s"}`, usdTone(unrealized))}
        ${regimeCell("Invested", formatUsdWhole(invested), exposure === null ? "" : `${exposure}% of book exposed`)}
        ${regimeCell("Win rate", winRate === null ? "\u2014" : `${winRate}%`, profitFactor === null ? "" : `profit factor ${profitFactor}`, usdTone(winRate === null ? null : winRate - 50))}
        ${regimeCell("Sharpe", sharpe === null ? "\u2014" : sharpe.toFixed(2), "annualized \u00b7 rf 0%", usdTone(sharpe))}
        ${regimeCell("Max drawdown", maxDd === null ? "\u2014" : `${maxDd}%`, holdDays === null ? "" : `avg hold ${holdDays}d`, maxDd ? "tone-negative" : "")}
      </div>
    </section>
    <section class="analytics-panel">
      ${panelHeading(
        "Equity Curve",
        curve.length ? `${curve.length} daily marks` : "",
        "Book equity by day. History before the portfolio regime is a realized-step reconstruction (capital plus closes on their close dates); from the regime start every day carries a true mark-to-market snapshot. The dashed line is the $10,000 baseline."
      )}
      ${fringeEquityChart(curve)}
    </section>
    <section class="analytics-panel">
      ${panelHeading(
        "Open Positions",
        `${open.length} open \u00b7 ${formatUsdWhole(invested)} at work`,
        "The live book — identical to the Daily panel. Hover a size for the declared conf/stop inputs behind its Kelly fraction."
      )}
      ${open.length ? fringeOpenTable(open) : '<div class="empty-state">No open positions</div>'}
    </section>
    <section class="analytics-panel">
      ${panelHeading(
        "Trade History",
        `${closed.length} closed trade${closed.length === 1 ? "" : "s"}`,
        "Every close in the book's life, newest first. Dollar results exist from the capital regime onward; pre-capital closes were grandfathered at a flat $1,000."
      )}
      ${fringeHistoryTable(closed)}
    </section>`;
}

function usdTone(value) {
  if (typeof value !== "number" || value === 0) return "";
  return value > 0 ? "tone-positive" : "tone-negative";
}

function formatUsdWhole(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "\u2014";
  return `$${Math.round(value).toLocaleString("en-US")}`;
}

function fringeEquityChart(curve) {
  if (curve.length < 2) {
    return '<div class="empty-state">The curve builds as daily marks accrue</div>';
  }
  const width = 720;
  const height = 200;
  const pad = 8;
  const equities = curve.map((point) => Number(point.equity));
  const min = Math.min(...equities, 10000);
  const max = Math.max(...equities, 10000);
  const span = max - min || 1;
  const x = (index) => pad + (index / (curve.length - 1)) * (width - pad * 2);
  const y = (value) => height - pad - ((value - min) / span) * (height - pad * 2);
  const pts = equities.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`);
  const base = y(10000).toFixed(1);
  const last = equities[equities.length - 1];
  const tone = last >= 10000 ? "positive" : "negative";
  const area = `M ${pts[0]} L ${pts.join(" L ")} L ${x(curve.length - 1).toFixed(1)},${base} L ${x(0).toFixed(1)},${base} Z`;
  return `<div class="equity-chart-wrap">
    <svg class="equity-chart equity-${tone}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Equity curve, now ${escapeHtml(formatUsdWhole(last))}">
      <path class="equity-area" d="${area}"></path>
      <line class="equity-base" x1="${pad}" y1="${base}" x2="${width - pad}" y2="${base}"></line>
      <polyline class="equity-line" points="${pts.join(" ")}"></polyline>
      <circle class="equity-dot" cx="${x(curve.length - 1).toFixed(1)}" cy="${y(last).toFixed(1)}" r="3"></circle>
    </svg>
    <div class="equity-axis">
      <span>${escapeHtml(curve[0].date)}</span>
      <span>peak ${escapeHtml(formatUsdWhole(Math.max(...equities)))} \u00b7 trough ${escapeHtml(formatUsdWhole(Math.min(...equities)))} \u00b7 now ${escapeHtml(formatUsdWhole(last))}</span>
      <span>${escapeHtml(curve[curve.length - 1].date)}</span>
    </div>
  </div>`;
}

function fringeHistoryTable(closed) {
  if (!closed.length) return '<div class="empty-state">No closed trades yet</div>';
  const rows = closed.map((idea) => {
    const pct = numericOrNull(idea.realized_pct);
    const usd = numericOrNull(idea.realized_usd);
    const tone = pct === null ? "" : pct > 0 ? "tone-positive" : pct < 0 ? "tone-negative" : "";
    const entry = numericOrNull(idea.entry_price);
    const exit = numericOrNull(idea.exit_price);
    const size = numericOrNull(idea.size_notional);
    const days =
      idea.opened && idea.closed
        ? Math.max(0, Math.round((Date.parse(idea.closed) - Date.parse(idea.opened)) / 86400000))
        : null;
    const direction = String(idea.direction || "").toUpperCase() === "SHORT" ? "SHORT" : "LONG";
    const ticker = escapeHtml(idea.ticker || "");
    return `<tr class="trade-row">
      <td><button type="button" class="fringe-ticker" data-symbol="${ticker}" title="Open ${ticker} chart">${ticker}</button></td>
      <td><span class="fringe-chip fringe-${direction.toLowerCase()}">${direction}</span></td>
      <td class="trade-reason" title="${escapeHtml(idea.close_reason || "")}">${escapeHtml(idea.close_reason || idea.thesis || "")}</td>
      <td class="fringe-num">${escapeHtml(idea.opened || "\u2014")}</td>
      <td class="fringe-num">${escapeHtml(idea.closed || "\u2014")}</td>
      <td class="fringe-num">${days === null ? "\u2014" : `${days}d`}</td>
      <td class="fringe-num">${size === null ? "\u2014" : escapeHtml(formatUsdWhole(size))}</td>
      <td class="fringe-num">${entry === null ? "\u2014" : escapeHtml(formatPrice(entry))} \u2192 ${exit === null ? "\u2014" : escapeHtml(formatPrice(exit))}</td>
      <td class="fringe-num"><strong class="${tone}">${pct === null ? "\u2014" : escapeHtml(formatSignedPct(pct))}</strong></td>
      <td class="fringe-num">${usd === null ? "\u2014" : `<strong class="${tone}">${escapeHtml(formatSignedUsd(usd))}</strong>`}</td>
    </tr>`;
  });
  return `<div class="trade-scroll"><table class="trade-table">
    <thead><tr>
      <th>Idea</th><th></th><th>Close reason</th>
      <th class="fringe-num">Opened</th><th class="fringe-num">Closed</th>
      <th class="fringe-num">Days</th><th class="fringe-num">Size</th>
      <th class="fringe-num">Entry \u2192 Exit</th><th class="fringe-num">P&amp;L %</th><th class="fringe-num">P&amp;L $</th>
    </tr></thead>
    <tbody>${rows.join("")}</tbody>
  </table></div>`;
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
  board.classList.toggle("board-map", marketLayout === "map");
  if (marketLayout === "map") {
    cryptoTapeElement.hidden = true;
    if (cryptoTapeElement.parentElement === board) cryptoTapeElement.remove();
    renderMarketMap(categoryGroups, payload);
    return;
  }
  board.querySelector(":scope > .market-map")?.remove();
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
    board
      .querySelectorAll(":scope > .group-panel:not(.tape-panel)")
      .forEach((panel) => panel.remove());
    if (showTape && tapeCounts.visible > 0) {
      board.querySelector(":scope > .empty-state")?.remove();
      if (cryptoTapeElement.parentElement !== board) board.appendChild(cryptoTapeElement);
    } else {
      if (cryptoTapeElement.parentElement === board) cryptoTapeElement.remove();
      const copy = hasFilter ? "No matching markets" : "No groups configured";
      let empty = board.querySelector(":scope > .empty-state");
      if (!empty) {
        empty = document.createElement("div");
        empty.className = "empty-state";
        board.appendChild(empty);
      }
      empty.textContent = copy;
    }
    updateMarketFilterStatus(tapeCounts.visible, totalAssets);
    return;
  }

  const totalAssets = countAssets(categoryGroups) + tapeCounts.total;
  const visibleAssets = countAssets(groups) + tapeCounts.visible;
  const nextGroups = new Set(groups.map((group) => group.name));
  board.querySelector(":scope > .empty-state")?.remove();
  const existingGroupPanels = Array.from(
    board.querySelectorAll(":scope > .group-panel:not(.tape-panel)")
  );
  let previousGroupPanel = null;

  groups.forEach((group) => {
    const panel = ensureGroupPanel(group.name, marketCategory === "crypto");
    updateGroupSessionChip(panel, group.assets || []);
    const assets = sortedAssets(group.assets || []);
    const nextSymbols = new Set(assets.map((asset) => asset.symbol));
    const existingRows = Array.from(panel.querySelectorAll(".asset-row"));
    const rowsBySymbol = new Map(existingRows.map((row) => [row.dataset.symbol, row]));

    let previousRow = null;
    assets.forEach((asset) => {
      let row = rowsBySymbol.get(asset.symbol);
      if (!row) {
        row = renderRow(asset);
      } else {
        updateRow(row, asset);
      }
      // Re-appending an already-placed row blurs any focused row on every
      // quote tick; only (re)insert rows whose position actually changed.
      const anchor = previousRow ? previousRow.nextElementSibling : existingRows[0] || null;
      if (row !== anchor) panel.insertBefore(row, anchor);
      previousRow = row;
    });

    existingRows.forEach((row) => {
      if (!nextSymbols.has(row.dataset.symbol)) row.remove();
    });
    const panelAnchor = previousGroupPanel
      ? previousGroupPanel.nextElementSibling
      : existingGroupPanels[0] || board.firstElementChild;
    if (panel !== panelAnchor) board.insertBefore(panel, panelAnchor);
    previousGroupPanel = panel;
  });

  board.querySelectorAll(".group-panel:not(.tape-panel)").forEach((panel) => {
    if (!nextGroups.has(panel.dataset.group)) panel.remove();
  });
  if (showTape) {
    const tapeAnchor = previousGroupPanel
      ? previousGroupPanel.nextElementSibling
      : board.firstElementChild;
    if (cryptoTapeElement !== tapeAnchor) board.insertBefore(cryptoTapeElement, tapeAnchor);
  }
  updateSortHeaders();
  updateMarketFilterStatus(visibleAssets, totalAssets);
}

// --- Markets treemap --------------------------------------------------------
// Finviz-style map: sectors squarified by summed traded dollar notional
// (volume x price; assets without volume take their group's median so they
// stay visible), tiles colored by the same 1D% the row view shows. Crypto
// includes the perp tape as one PERPS sector sized by 24h notional.
function renderMarketMap(categoryGroups, payload) {
  const sectors = visibleGroups(categoryGroups).map((group) => ({
    name: displayGroupName(group.name),
    tiles: group.assets.map(mapTileData),
  }));
  const query = marketSearchQuery.toLowerCase();
  let tapeTotal = 0;
  if (marketCategory === "crypto") {
    const configured = new Set();
    categoryGroups.forEach((group) =>
      (group.assets || []).forEach((asset) => configured.add(asset.symbol))
    );
    const rows = (payload.crypto_tape || []).filter((row) => !configured.has(row.symbol));
    tapeTotal = rows.length;
    const perps = rows
      .filter((row) => !query || matchesTapeQuery(row, query))
      .map((row) => ({
        symbol: row.symbol,
        name: `${row.symbol} perp`,
        pct: numericOrNull(row.change_pct),
        value: numericOrNull(row.day_volume_usd),
      }));
    if (perps.length) sectors.push({ name: "Perps", tiles: perps });
  }
  const populated = sectors
    .map((sector) => ({
      ...sector,
      tiles: scaleTileValues(fillMissingTileValues(sector.tiles)),
    }))
    .filter((sector) => sector.tiles.length);

  board.querySelectorAll(":scope > .group-panel:not(.tape-panel)").forEach((panel) => panel.remove());
  board.querySelector(":scope > .empty-state")?.remove();
  let host = board.querySelector(":scope > .market-map");
  if (!host) {
    host = document.createElement("div");
    host.className = "market-map";
    board.appendChild(host);
  }
  const visibleTiles = populated.reduce((sum, sector) => sum + sector.tiles.length, 0);
  updateMarketFilterStatus(visibleTiles, countAssets(categoryGroups) + tapeTotal);
  if (!visibleTiles) {
    host.style.height = "auto";
    host.innerHTML = '<div class="empty-state">No matching markets</div>';
    host.dataset.renderKey = ""; // stale key must not suppress the next real render
    return;
  }

  const width = Math.max(board.clientWidth, 320);
  const height = Math.min(Math.max(Math.round(window.innerHeight * 0.72), 440), 860);
  // Quotes tick every few seconds but the layout inputs rarely move: skip
  // the full innerHTML rebuild (two squarify passes plus hover/focus
  // teardown) when the rounded inputs match the previous render. Keyed on
  // the host element so a rebuilt host always repaints.
  const renderKey = [
    `${Math.round(width)}x${height}`,
    ...populated.map(
      (sector) =>
        `${sector.name}=` +
        sector.tiles
          .map((tile) => `${tile.symbol}:${tile.pct === null ? "" : tile.pct.toFixed(2)}:${Math.round(tile.value)}`)
          .join(",")
    ),
  ].join("|");
  if (host.dataset.renderKey === renderKey) return;
  host.dataset.renderKey = renderKey;
  const sectorItems = populated.map((sector) => ({
    ...sector,
    value: sector.tiles.reduce((sum, tile) => sum + tile.value, 0),
  }));
  const chunks = [];
  for (const placed of squarify(sectorItems, { x: 0, y: 0, w: width, h: height })) {
    const sector = placed.item;
    const labelled = placed.h >= 56 && placed.w >= 72;
    const labelHeight = labelled ? 15 : 0;
    chunks.push(
      `<div class="map-group" style="left:${placed.x.toFixed(1)}px;top:${placed.y.toFixed(1)}px;width:${placed.w.toFixed(1)}px;height:${placed.h.toFixed(1)}px">` +
        (labelled ? `<span class="map-group-label">${escapeHtml(sector.name)}</span>` : "")
    );
    const inner = squarify(sector.tiles, {
      x: 0,
      y: labelHeight,
      w: placed.w,
      h: Math.max(placed.h - labelHeight, 0),
    });
    for (const tile of inner) {
      const item = tile.item;
      const tiny = tile.w < 34 || tile.h < 16;
      const small = tile.w < 58 || tile.h < 30;
      const pctText = item.pct === null ? "" : formatSignedPct(item.pct);
      chunks.push(
        `<button type="button" class="map-tile${tiny ? " map-tile-tiny" : small ? " map-tile-small" : ""}"` +
          ` style="left:${tile.x.toFixed(1)}px;top:${tile.y.toFixed(1)}px;width:${tile.w.toFixed(1)}px;height:${tile.h.toFixed(1)}px;background:${mapTileColor(item.pct)}"` +
          ` data-symbol="${escapeHtml(item.symbol)}"` +
          ` title="${escapeHtml(`${item.symbol} · ${item.name}${pctText ? ` · ${pctText}` : ""}`)}"` +
          ` aria-label="Open ${escapeHtml(item.symbol)} chart">` +
          `<span>${escapeHtml(item.symbol)}</span>${pctText ? `<em>${escapeHtml(pctText)}</em>` : ""}</button>`
      );
    }
    chunks.push("</div>");
  }
  host.style.height = `${height}px`;
  host.innerHTML = chunks.join("");
}

function mapTileData(asset) {
  const quote = asset.quote || {};
  const last = numericOrNull(displayQuoteValue(quote, "last"));
  const pct = isCryptoAsset(asset.type)
    ? numericOrNull(asset.summary?.open_change_pct)
    : numericOrNull(displayQuoteValue(quote, "change_pct"));
  const volume = numericOrNull(quote.volume);
  return {
    symbol: asset.symbol,
    name: asset.name || asset.symbol,
    pct,
    value: volume !== null && last !== null && volume > 0 ? volume * last : null,
  };
}

// Assets Yahoo serves without volume (some futures) take the sector median
// notional: visible, honestly unglamorous, never dominant.
function fillMissingTileValues(tiles) {
  const known = tiles
    .map((tile) => tile.value)
    .filter((value) => typeof value === "number" && value > 0)
    .sort((a, b) => a - b);
  const median = known.length ? known[Math.floor(known.length / 2)] : 1;
  return tiles.map((tile) => ({
    ...tile,
    value: typeof tile.value === "number" && tile.value > 0 ? tile.value : median,
  }));
}


// Raw notional is brutally skewed (BTC alone out-trades the whole alt
// complex; gold dwarfs platinum), which renders as one mega-tile plus
// unreadable dust in degenerate slivers. Square-root area keeps the
// activity ORDERING while compressing the ratios into a readable mosaic,
// and a small floor keeps the tail visible and clickable. Sector areas sum
// the same scaled values, so the compression applies at both levels.
function scaleTileValues(tiles) {
  const scaled = tiles.map((tile) => ({ ...tile, value: Math.sqrt(tile.value) }));
  const total = scaled.reduce((sum, tile) => sum + tile.value, 0);
  if (total <= 0) return scaled;
  const floor = total * Math.min(0.015, 0.5 / tiles.length);
  return scaled.map((tile) => ({ ...tile, value: Math.max(tile.value, floor) }));
}

// Green/red wash deepening toward a +/-3% clamp; null marks stay neutral.
function mapTileColor(pct) {
  if (typeof pct !== "number") return "var(--surface-soft)";
  const alpha = Math.min(Math.abs(pct) / 3, 1) * 0.58 + 0.1;
  return pct >= 0
    ? `rgba(45, 148, 106, ${alpha.toFixed(3)})`
    : `rgba(196, 78, 74, ${alpha.toFixed(3)})`;
}

// Squarified treemap (Bruls et al.): places value-weighted items into rect,
// keeping tile aspect ratios near 1. Returns {item, x, y, w, h} rows.
function squarify(items, rect) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  if (total <= 0 || rect.w <= 4 || rect.h <= 4) return [];
  const scale = (rect.w * rect.h) / total;
  const sorted = [...items].sort((a, b) => b.value - a.value);
  const placed = [];
  let x = rect.x;
  let y = rect.y;
  let w = rect.w;
  let h = rect.h;
  let row = [];
  let rowArea = 0;

  const worst = (areaSum, minArea, maxArea, side) => {
    const sumSq = areaSum * areaSum;
    const sideSq = side * side;
    return Math.max((sideSq * maxArea) / sumSq, sumSq / (sideSq * minArea));
  };
  const layoutRow = () => {
    const side = Math.min(w, h);
    if (side <= 0 || rowArea <= 0) {
      row = [];
      rowArea = 0;
      return;
    }
    const thickness = rowArea / side;
    let offset = 0;
    for (const entry of row) {
      const length = (entry.area / rowArea) * side;
      if (w <= h) {
        placed.push({ item: entry.item, x: x + offset, y, w: length, h: thickness });
      } else {
        placed.push({ item: entry.item, x, y: y + offset, w: thickness, h: length });
      }
      offset += length;
    }
    if (w <= h) {
      y += thickness;
      h -= thickness;
    } else {
      x += thickness;
      w -= thickness;
    }
    row = [];
    rowArea = 0;
  };

  for (const item of sorted) {
    const area = Math.max(item.value * scale, 0.5);
    const side = Math.min(w, h);
    if (row.length) {
      const areas = row.map((entry) => entry.area);
      const minArea = Math.min(...areas);
      const maxArea = Math.max(...areas);
      const current = worst(rowArea, minArea, maxArea, side);
      const next = worst(
        rowArea + area,
        Math.min(minArea, area),
        Math.max(maxArea, area),
        side
      );
      if (next > current) layoutRow();
    }
    row.push({ item, area });
    rowArea += area;
  }
  layoutRow();
  return placed;
}

// --- Crypto tape -----------------------------------------------------------
// Every crypto perp on Hyperliquid, auto-synced from the exchange: no YAML entry
// needed, new listings appear on their own. Rows are quote-only (funding, OI,
// 24h volume); clicking one opens the chart via on-demand Hyperliquid candles.
const TAPE_SORT_KEYS = {
  symbol: (row) => row.symbol,
  last: (row) => numericOrNull(row.last),
  pct: (row) => numericOrNull(row.change_pct),
  funding: (row) => numericOrNull(row.funding_rate),
  oi: (row) => numericOrNull(row.open_interest_usd),
  volume: (row) => numericOrNull(row.day_volume_usd),
};

// Panel order mirrors the exchange app baskets; "Other" catches untagged tails.
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

  if (!visible.length) {
    const copy = rows.length ? "No matching perps" : "";
    const currentEmpty = cryptoTapeElement.querySelector(":scope > .empty-state");
    if (!copy) {
      if (cryptoTapeElement.childElementCount) cryptoTapeElement.replaceChildren();
    } else if (
      cryptoTapeElement.childElementCount !== 1 ||
      !currentEmpty ||
      currentEmpty.textContent !== copy
    ) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = copy;
      cryptoTapeElement.replaceChildren(empty);
    }
    return counts;
  }

  cryptoTapeElement.querySelector(":scope > .empty-state")?.remove();
  const baskets = new Map();
  visible.forEach((row) => {
    const basket = TAPE_BASKET_ORDER.includes(row.basket) ? row.basket : "Other";
    if (!baskets.has(basket)) baskets.set(basket, []);
    baskets.get(basket).push(row);
  });
  const existingPanels = new Map(
    Array.from(cryptoTapeElement.querySelectorAll(":scope > .tape-panel")).map((panel) => [
      panel.dataset.basket,
      panel,
    ])
  );
  let previousPanel = null;
  TAPE_BASKET_ORDER.filter((basket) => baskets.has(basket)).forEach((basket) => {
    const sortedRows = sortedTapeRows(baskets.get(basket), basketSort(basket));
    const panel = existingPanels.get(basket) || createTapePanel(basket, sortedRows);
    existingPanels.delete(basket);
    reconcileTapePanel(panel, basket, sortedRows);
    const anchor = previousPanel
      ? previousPanel.nextElementSibling
      : cryptoTapeElement.firstElementChild;
    if (panel !== anchor) cryptoTapeElement.insertBefore(panel, anchor);
    previousPanel = panel;
  });
  existingPanels.forEach((panel) => panel.remove());
  return counts;
}

function handleCryptoTapeClick(event) {
  const button = event.target instanceof Element ? event.target.closest("button") : null;
  if (!button || !cryptoTapeElement.contains(button)) return;
  const panel = button.closest(".tape-panel");
  if (!panel) return;
  const basket = panel.dataset.basket || "Other";
  if (button.matches(".group-title button[data-sort-key]")) {
    setTapeSort(basket, button.dataset.sortKey || "volume");
    return;
  }
  if (button.matches(".tape-pager button[data-step]")) {
    tapePages[basket] = Math.max(
      0,
      (tapePages[basket] || 0) + Number(button.dataset.step || 0)
    );
    renderBoard(latestData);
    return;
  }
  if (button.classList.contains("asset-row")) {
    openTapeChart(button.dataset.symbol || "");
  }
}

function createTapePanel(basket, rows) {
  const template = document.createElement("template");
  template.innerHTML = tapeBasketMarkup(basket, rows).trim();
  return template.content.firstElementChild;
}

function reconcileTapePanel(panel, basket, rows) {
  const pageCount = Math.max(1, Math.ceil(rows.length / TAPE_PAGE_SIZE));
  const page = Math.min(tapePages[basket] || 0, pageCount - 1);
  tapePages[basket] = page;
  const start = page * TAPE_PAGE_SIZE;
  const pageRows = rows.slice(start, start + TAPE_PAGE_SIZE);
  const sort = basketSort(basket);
  const headerDefinitions = [
    [basket, "symbol"],
    ["Last", "last"],
    ["24h %", "pct"],
    ["Fund", "funding"],
    ["OI", "oi"],
    ["24h Vol", "volume"],
  ];
  const header = panel.querySelector(":scope > .group-title");
  header.querySelectorAll("button[data-sort-key]").forEach((button, index) => {
    const [label, sortKey] = headerDefinitions[index];
    const active = sort.key === sortKey;
    const direction = sort.direction === "asc" ? "ascending" : "descending";
    button.dataset.sortKey = sortKey;
    button.classList.toggle("active-sort", active);
    button.setAttribute("aria-pressed", String(active));
    button.setAttribute(
      "aria-label",
      active ? `Sorted by ${label}, ${direction} — click to flip` : `Sort by ${label}`
    );
    button.title = `Sort by ${label}`;
    button.textContent = label;
  });
  const sessionChip = header.querySelector(".session-chip");
  sessionChip.textContent = String(rows.length);
  sessionChip.title = `${rows.length} perps · Hyperliquid basket · trades 24/7`;

  const existingRows = Array.from(panel.querySelectorAll(":scope > .asset-row"));
  const rowsBySymbol = new Map(existingRows.map((row) => [row.dataset.symbol, row]));
  const nextSymbols = new Set(pageRows.map((row) => row.symbol));
  let previousRow = header;
  pageRows.forEach((data) => {
    const row = rowsBySymbol.get(data.symbol) || createTapeRow(data);
    updateTapeRow(row, data);
    const anchor = previousRow.nextElementSibling;
    if (row !== anchor) panel.insertBefore(row, anchor);
    previousRow = row;
  });
  existingRows.forEach((row) => {
    if (!nextSymbols.has(row.dataset.symbol)) row.remove();
  });

  let pager = panel.querySelector(":scope > .tape-pager");
  if (pageCount === 1) {
    pager?.remove();
    return;
  }
  if (!pager) {
    pager = document.createElement("div");
    pager.className = "tape-pager";
    pager.innerHTML =
      '<button type="button" data-step="-1" aria-label="Previous page">‹</button>' +
      "<span></span>" +
      '<button type="button" data-step="1" aria-label="Next page">›</button>';
  }
  const pagerButtons = pager.querySelectorAll("button");
  pagerButtons[0].disabled = page === 0;
  pager.querySelector("span").textContent =
    `${start + 1}–${start + pageRows.length} of ${rows.length}`;
  pagerButtons[1].disabled = page >= pageCount - 1;
  if (pager !== panel.lastElementChild) panel.appendChild(pager);
}

function createTapeRow(data) {
  const template = document.createElement("template");
  template.innerHTML = tapeRowMarkup(data).trim();
  return template.content.firstElementChild;
}

function updateTapeRow(row, data) {
  const apr =
    typeof data.funding_rate === "number" ? data.funding_rate * 24 * 365 * 100 : null;
  const aprText = apr === null ? "--" : `${apr >= 0 ? "+" : ""}${apr.toFixed(1)}%`;
  const aprClass =
    apr === null ? "" : apr >= 20 ? "tone-negative" : apr < 0 ? "tone-positive" : "";
  const cells = row.children;
  row.className = "asset-row";
  row.dataset.symbol = data.symbol;
  row.setAttribute("aria-label", `${data.symbol} chart`);
  cells[0].className = "symbol-cell";
  cells[0].querySelector("strong").textContent = data.symbol;
  const previousLast = numericOrNull(cells[1].dataset.value);
  cells[1].className = "last-cell";
  cells[1].title = "Last trade";
  cells[1].textContent = formatPrice(data.last);
  if (typeof data.last === "number") cells[1].dataset.value = String(data.last);
  else delete cells[1].dataset.value;
  if (previousLast !== null && typeof data.last === "number" && data.last !== previousLast) {
    flashCell(cells[1], data.last - previousLast);
  }
  cells[2].className = changeClass(data.change_pct);
  cells[2].textContent = formatSignedPct(data.change_pct);
  cells[3].className = `tape-funding ${aprClass}`;
  cells[3].title = "Funding, annualized";
  cells[3].textContent = aprText;
  cells[4].className = "tape-oi";
  cells[4].title = "Open interest";
  cells[4].textContent = data.open_interest_usd
    ? `$${formatCompactPrice(data.open_interest_usd)}`
    : "--";
  cells[5].className = "tape-volume";
  cells[5].title = "24h notional volume";
  cells[5].textContent = data.day_volume_usd
    ? `$${formatCompactPrice(data.day_volume_usd)}`
    : "--";
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
      <span>${header(basket, "symbol")}<em class="session-chip" data-state="open" title="${rows.length} perps · Hyperliquid basket · trades 24/7">${rows.length}</em></span>
      <span>${header("Last", "last")}</span>
      <span>${header("24h %", "pct")}</span>
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
  // aria-sort is only valid on columnheader roles; on buttons announce the
  // active sort via aria-pressed + a direction-aware label instead.
  const dir = sort.direction === "asc" ? "ascending" : "descending";
  const aria = active ? `Sorted by ${label}, ${dir} — click to flip` : `Sort by ${label}`;
  return `<button type="button" data-sort-key="${sortKey}" class="${active ? "active-sort" : ""}" aria-pressed="${active}" aria-label="${escapeHtml(aria)}" title="Sort by ${escapeHtml(label)}">${escapeHtml(label)}</button>`;
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

function openTapeChart(symbol, options = {}) {
  if (!symbol) return;
  const row = (latestData?.crypto_tape || []).find((entry) => entry.symbol === symbol);
  const last = numericOrNull(row?.last);
  const changePct = numericOrNull(row?.change_pct);
  openChart(
    {
      symbol,
      type: "crypto_perp",
      quote: {
        provider: "hyperliquid",
        last,
        change_pct: changePct,
        previous_close:
          last !== null && changePct !== null && changePct > -100
            ? last / (1 + changePct / 100)
            : null,
        funding_rate: row?.funding_rate ?? null,
        open_interest_usd: row?.open_interest_usd ?? null,
      },
    },
    options
  );
}

function toggleMarketLayout() {
  marketLayout = marketLayout === "flat" ? "grouped" : "flat";
  if (marketLayout === "flat" && marketSort.key === "configured") {
    marketSort = { key: "pct", direction: "desc" };
  }
  syncLayoutButtons();
  renderBoard(latestData);
  syncUrlState();
}

function toggleMarketMap() {
  marketLayout = marketLayout === "map" ? "grouped" : "map";
  syncLayoutButtons();
  renderBoard(latestData);
  syncUrlState();
}

function syncLayoutButtons() {
  marketLayoutToggle.setAttribute("aria-pressed", String(marketLayout === "flat"));
  marketLayoutToggle.textContent = marketLayout === "flat" ? "Grouped" : "Flat";
  marketLayoutToggle.title =
    marketLayout === "flat"
      ? "Back to sector groups"
      : "Flatten all groups into one sortable movers table";
  marketMapToggle.setAttribute("aria-pressed", String(marketLayout === "map"));
  marketMapToggle.textContent = marketLayout === "map" ? "Grouped" : "Map";
  marketMapToggle.title =
    marketLayout === "map"
      ? "Back to sector groups"
      : "Treemap: tile area tracks traded dollar volume (square-root scale), color is the 1D% move";
  marketMapLegend.hidden = marketLayout !== "map";
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

function ensureGroupPanel(groupName, isCrypto = false) {
  let panel = board.querySelector(`.group-panel[data-group="${cssEscape(groupName)}"]`);
  if (!panel) {
    panel = document.createElement("section");
    panel.className = "group-panel";
    panel.dataset.group = groupName;

    const header = document.createElement("div");
    header.className = "group-title";
    header.append(
      groupHeaderCell(displayGroupName(groupName), "symbol"),
      groupHeaderCell("Last", "last"),
      groupHeaderCell("Abs", "abs"),
      groupHeaderCell("1D %", "pct"),
      groupHeaderCell("\u0394Open", "open"),
      groupHeaderCell("RVOL", "rvol"),
      groupHeaderCell("Trend", "trend")
    );
    panel.appendChild(header);
  }
  syncDayChangeHeaders(panel, isCrypto);
  return panel;
}

function syncDayChangeHeaders(panel, isCrypto) {
  // Crypto panels replace ΔOpen with the exchange's rolling 24h change and
  // anchor "1D %" to the UTC day. Synced on every render because the flat
  // "__ALL__" panel is reused across category tabs.
  const variant = isCrypto ? "crypto" : "tradfi";
  if (panel.dataset.dayVariant === variant) return;
  panel.dataset.dayVariant = variant;
  const pctButton = panel.querySelector('.group-title button[data-sort-key="pct"]');
  const openButton = panel.querySelector('.group-title button[data-sort-key="open"]');
  if (pctButton) {
    pctButton.title = isCrypto ? "Move since UTC midnight · click to sort" : "Sort by 1D %";
  }
  if (openButton) {
    openButton.textContent = isCrypto ? "24h %" : "\u0394Open";
    openButton.title = isCrypto
      ? "Rolling 24h change (exchange window) · click to sort"
      : "Change since today's open · click to sort";
  }
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
  button.title = sortKey === "open" ? "Change since today's open · click to sort" : `Sort by ${label}`;
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
    button.setAttribute("aria-pressed", String(active));
    button.removeAttribute("aria-sort");
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
  // Crypto rows swap the two day-change columns: "1D %" holds the UTC-day
  // move and the old ΔOpen slot holds the exchange's rolling 24h change
  // (Hyperliquid's rolling 24h change) — mirror that here so sorting follows
  // the displayed values.
  const crypto = isCryptoAsset(asset.type);
  if (key === "last") return numericOrNull(displayQuoteValue(quote, "last"));
  if (key === "abs") return numericOrNull(displayQuoteValue(quote, "change_abs"));
  if (key === "pct") {
    return crypto
      ? numericOrNull(asset.summary?.open_change_pct)
      : numericOrNull(displayQuoteValue(quote, "change_pct"));
  }
  if (key === "rvol") return numericOrNull(asset.summary?.rvol);
  if (key === "open") {
    return crypto
      ? numericOrNull(displayQuoteValue(quote, "change_pct"))
      : numericOrNull(asset.summary?.open_change_pct);
  }
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

function scheduleMarketSearch() {
  if (marketSearchTimer !== null) window.clearTimeout(marketSearchTimer);
  marketSearchTimer = window.setTimeout(flushPendingMarketSearch, 120);
}

function flushPendingMarketSearch() {
  if (marketSearchTimer !== null) {
    window.clearTimeout(marketSearchTimer);
    marketSearchTimer = null;
  }
  const query = marketSearch.value.trim();
  if (query === marketSearchQuery) return;
  marketSearchQuery = query;
  renderBoard(latestData);
  syncUrlState();
}

function cancelPendingMarketSearch() {
  if (marketSearchTimer === null) return;
  window.clearTimeout(marketSearchTimer);
  marketSearchTimer = null;
}

function clearMarketFilters() {
  cancelPendingMarketSearch();
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
    // Keep the session-only warning visible on serverless deployments;
    // clearing it here made it flash for ~100ms and vanish.
    setEditorStatus(persistenceNotice());
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
  const saved = await mutateWatchlists("/api/groups", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  // A failed save (validation, wrong token) keeps the typed name so one
  // character can be fixed instead of retyping the form.
  if (saved) groupNameInput.value = "";
}

async function addAsset(event) {
  event.preventDefault();
  const groupName = assetGroupSelect.value;
  if (!groupName) return;
  const saved = await mutateWatchlists(`/api/groups/${encodeURIComponent(groupName)}/assets`, {
    method: "POST",
    body: JSON.stringify({
      symbol: assetSymbolInput.value,
      type: assetTypeSelect.value,
      source: assetSourceSelect.value,
      exchange: assetExchangeInput.value || null,
      name: assetNameInput.value || null,
    }),
  });
  if (!saved) return;
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
  group_name_reserved: "That name is reserved for the built-in macro tape",
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
        ...(sessionStorage.getItem(EDIT_TOKEN_KEY)
          ? { "X-Edit-Token": sessionStorage.getItem(EDIT_TOKEN_KEY) }
          : {}),
      },
    });
  try {
    let response = await send();
    if (response.status === 401) {
      // The server has an EDIT_TOKEN configured; ask once and retry.
      const token = window.prompt("This board is protected. Enter the edit token:");
      if (token) {
        sessionStorage.setItem(EDIT_TOKEN_KEY, token.trim());
        response = await send();
      }
    }
    if (!response.ok) {
      if (response.status === 401) sessionStorage.removeItem(EDIT_TOKEN_KEY);
      const payload = await response.json().catch(() => ({}));
      setEditorStatus(editorErrorCopy(payload.detail));
      return false;
    }
    watchlistConfig = await response.json();
    renderEditor();
    const notice = persistenceNotice();
    setEditorStatus(notice ? `Saved (session only) — ${notice}` : "Saved");
    await fetchQuotes();
    return true;
  } catch (error) {
    // fetch rejects on network failure; without this the editor wedges on
    // "Saving" with an unhandled rejection.
    setEditorStatus("Network error — changes not saved. Check your connection and retry.");
    return false;
  }
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
  // Resolve at click time: rows are created once (often from the cached
  // payload) and reused across polls, so the closure's asset snapshot goes
  // stale while the cells stay live. Tape rows already resolve this way.
  row.addEventListener("click", () => openChart(findAssetConfig(asset.symbol) || asset));
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
  // Crypto rows: "1D %" anchors to the UTC day (what ΔOpen used to show)
  // and the old ΔOpen column carries Hyperliquid's rolling 24h change, so each
  // number sits under a truthful label. TradFi rows are unchanged.
  const openChange = numericOrNull(asset.summary?.open_change_pct);
  const rowIsCrypto = isCryptoAsset(asset.type);
  const dayPct = rowIsCrypto ? openChange : numericOrNull(display.change_pct);
  const openCellPct = rowIsCrypto ? numericOrNull(display.change_pct) : openChange;
  updateValueCell(
    ensureRowCell(row, "pct"),
    formatSignedPct(dayPct),
    dayPct,
    changeClass(dayPct),
    !options.initial
  );
  updateValueCell(
    ensureRowCell(row, "open", "open-cell"),
    formatSignedPct(openCellPct),
    openCellPct,
    `open-cell ${changeClass(openCellPct)}`,
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
  updateSparklineCell(
    ensureRowCell(row, "trend", "sparkline-cell"),
    asset.summary?.sparkline || [],
    Boolean(options.initial)
  );
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

function updateSparklineCell(cell, values, animate = false) {
  const key = values.join(",");
  if (cell.dataset.sparkKey === key) return;
  cell.dataset.sparkKey = key;
  cell.className = "sparkline-cell";
  cell.title = values.length ? "Recent trend" : "No trend history";
  cell.innerHTML = sparklineSvg(values, animate);
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

// Perp funding chip for crypto rows: hourly Hyperliquid rate annualized.
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

function sparklineSvg(values, animate = false) {
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
  return `<svg class="sparkline sparkline-${tone}${animate ? " spark-draw" : ""}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${path}"></polyline></svg>`;
}

function filterMarketsByGroup(groupName) {
  if (!groupName) return;
  cancelPendingMarketSearch();
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
  setFocusedSymbol(symbol, { sync: false });
  activeAsset = asset || null;
  activeHistoryContext = null;
  chartContextLoading = false;
  optionsLoadToken += 1;
  optionsPanelState = null;
  optionsProfileMode = "gex";
  const requestedInterval = options.interval || "1d";
  const timeframeButton =
    intervalButtons.find((item) => item.dataset.interval === requestedInterval) ||
    intervalButtons.find((item) => item.dataset.interval === "1d");
  activeInterval = timeframeButton?.dataset.interval || "1d";
  activeRange = timeframeButton?.dataset.range || "1y";
  intervalButtons.forEach((item) => item.classList.toggle("active", item === timeframeButton));
  updateIntradayAvailability(assetType);
  chartTitle.textContent = symbol;
  updateChartWatchToggle();
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
    if (supportsOptionsAsset(asset)) {
      setOptionsLoading(symbol);
      loadOptionsSnapshot(symbol);
    }
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
  optionsLoadToken += 1;
  optionsPanelState = null;
  activeSymbol = null;
  activeAsset = null;
  activeHistoryContext = null;
  chartContextLoading = false;
  if (chart) {
    chart.remove();
    chart = null;
    chartCandleSeries = null;
    chartMovingAverageSeries = [];
    chartVolumeSeries = null;
    chartVolumeBars = [];
    chartPreviousCloseLine = null;
  }
  showProfilePanel();
  profileElement.innerHTML = '<div class="profile-empty">Select an asset to load profile data</div>';
  resetProfileScroll();
  syncUrlState();
}

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
    chartCandleSeries = null;
    chartMovingAverageSeries = [];
    chartVolumeSeries = null;
    chartVolumeBars = [];
    chartPreviousCloseLine = null;
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
    const bars = rawBars
      .map((bar) => ({
        time: toChartTime(bar.timestamp, interval),
        open: numericOrNull(bar.open),
        high: numericOrNull(bar.high),
        low: numericOrNull(bar.low),
        close: numericOrNull(bar.close),
        volume: numericOrNull(bar.volume),
      }))
      // Number(null) is 0 and would drag the price scale to zero, so OHLC
      // goes through numericOrNull and incomplete bars drop before setData.
      .filter((bar) => Number.isFinite(bar.open) && Number.isFinite(bar.high) && Number.isFinite(bar.low) && Number.isFinite(bar.close));
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
    // renderChart assigns the global before wiring series; a throw mid-build
    // must dispose it here or the instance leaks (token check above proves
    // this call still owns it — a stale call never reaches renderChart).
    if (chart) {
      chart.remove();
      chart = null;
      chartCandleSeries = null;
      chartMovingAverageSeries = [];
      chartVolumeSeries = null;
      chartVolumeBars = [];
      chartPreviousCloseLine = null;
    }
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
  { period: 20, colorToken: "--chart-ma-20" },
  { period: 50, colorToken: "--chart-ma-50" },
  { period: 200, colorToken: "--chart-ma-200" },
];

function themeColor(token) {
  return getComputedStyle(document.documentElement).getPropertyValue(token).trim();
}

function chartThemeColors() {
  return {
    background: themeColor("--chart-bg"),
    text: themeColor("--chart-text"),
    grid: themeColor("--chart-grid"),
    border: themeColor("--chart-border"),
    previous: themeColor("--chart-previous"),
    up: themeColor("--chart-up"),
    down: themeColor("--chart-down"),
    volumeUp: themeColor("--chart-volume-up"),
    volumeDown: themeColor("--chart-volume-down"),
  };
}

// Theme flips restyle every live canvas series in place, preserving the
// current zoom, scroll position, and data.
function restyleLiveCharts() {
  if (!chart && !watchCharts.size) return;
  const colors = chartThemeColors();
  const chartOptions = {
    layout: { background: { color: colors.background }, textColor: colors.text },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border },
    timeScale: { borderColor: colors.border },
  };
  const candleOptions = {
    upColor: colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
  };
  if (chart) {
    chart.applyOptions(chartOptions);
    chartCandleSeries?.applyOptions(candleOptions);
    chartMovingAverageSeries.forEach((entry) => {
      entry.series.applyOptions({ color: themeColor(entry.colorToken) });
    });
    if (chartVolumeSeries && chartVolumeBars.length) {
      chartVolumeSeries.setData(volumeSeriesData(chartVolumeBars, colors));
    }
    chartPreviousCloseLine?.applyOptions({ color: colors.previous });
    chartElement.querySelectorAll(".chart-legend i[data-color-token]").forEach((swatch) => {
      swatch.style.background = themeColor(swatch.dataset.colorToken);
    });
  }
  watchCharts.forEach((entry) => {
    entry.instance.applyOptions(chartOptions);
    entry.series.applyOptions(candleOptions);
  });
}

function renderChart(bars, interval) {
  if (!window.LightweightCharts) throw new Error("Chart library unavailable");
  const chartWidth = chartElement.clientWidth || 900;
  // 260 matches the phone CSS minimum (.chart min-height); a 320 floor
  // inside a 260px container clipped the time axis off-screen.
  const chartHeight = Math.max(chartElement.clientHeight, 260);
  const colors = chartThemeColors();
  chart = window.LightweightCharts.createChart(chartElement, {
    width: chartWidth,
    height: chartHeight,
    layout: { background: { color: colors.background }, textColor: colors.text },
    grid: { vertLines: { color: colors.grid }, horzLines: { color: colors.grid } },
    rightPriceScale: { borderColor: colors.border, scaleMargins: { top: 0.05, bottom: 0.22 } },
    timeScale: { borderColor: colors.border, timeVisible: !DATE_ONLY_INTERVALS.has(interval) },
    crosshair: { mode: window.LightweightCharts.CrosshairMode.Normal },
  });
  const series = chart.addCandlestickSeries({
    upColor: colors.up,
    downColor: colors.down,
    borderUpColor: colors.up,
    borderDownColor: colors.down,
    wickUpColor: colors.up,
    wickDownColor: colors.down,
  });
  chartCandleSeries = series;
  series.setData(bars);

  const drawnMas = drawMovingAverages(bars);
  drawVolumePane(bars, colors);
  drawPreviousCloseLine(series, interval, colors);
  renderChartLegend(drawnMas);

  chart.timeScale().fitContent();
  scheduleChartResize();
}

function drawMovingAverages(bars) {
  const closes = bars.map((bar) => bar.close);
  const drawn = [];
  MA_OVERLAYS.forEach(({ period, colorToken }) => {
    const color = themeColor(colorToken);
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
    chartMovingAverageSeries.push({ series: line, colorToken });
    drawn.push({ period, color, colorToken });
  });
  return drawn;
}

function drawVolumePane(bars, colors) {
  if (!bars.some((bar) => typeof bar.volume === "number" && bar.volume > 0)) return;
  chartVolumeBars = bars;
  chartVolumeSeries = chart.addHistogramSeries({
    priceScaleId: "volume",
    priceFormat: { type: "volume" },
    priceLineVisible: false,
    lastValueVisible: false,
  });
  chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  chartVolumeSeries.setData(volumeSeriesData(bars, colors));
}

function volumeSeriesData(bars, colors) {
  return bars
    .filter((bar) => typeof bar.volume === "number")
    .map((bar) => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? colors.volumeUp : colors.volumeDown,
    }));
}

function drawPreviousCloseLine(series, interval, colors) {
  if (!INTRADAY_INTERVALS.has(interval)) return;
  const quote = activeAsset?.quote || {};
  const prevClose = numericOrNull(
    typeof quote.display_previous_close === "number" ? quote.display_previous_close : quote.previous_close
  );
  if (prevClose === null || prevClose <= 0) return;
  chartPreviousCloseLine = series.createPriceLine({
    price: prevClose,
    color: colors.previous,
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
    .map(
      ({ period, color, colorToken }) =>
        `<span><i data-color-token="${escapeHtml(colorToken)}" style="background:${color}"></i>MA${period}</span>`
    )
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
  const height = Math.max(260, Math.floor(chartElement.clientHeight || 0));
  chart.applyOptions({ width, height });
}

function supportsOptionsAsset(asset) {
  const assetType = String(asset?.type || asset?.asset_type || "");
  const exchange = String(asset?.exchange || "").toUpperCase();
  return ["equity", "etf"].includes(assetType) && (!exchange || US_OPTIONS_EXCHANGES.has(exchange));
}

function setOptionsLoading(symbol) {
  optionsPanelState = { status: "loading", symbol };
  renderOptionsPanel();
}

async function loadOptionsSnapshot(symbol, expiration = "") {
  const requestId = optionsLoadToken + 1;
  optionsLoadToken = requestId;
  try {
    const suffix = expiration ? `?expiration=${encodeURIComponent(expiration)}` : "";
    const response = await fetch(`/api/options/${encodeURIComponent(symbol)}${suffix}`);
    if (!response.ok) {
      let code = "options_unavailable";
      try {
        const errorPayload = await response.json();
        code = errorPayload?.detail || code;
      } catch {
        // The status code is enough; never surface a provider response body.
      }
      throw new Error(code);
    }
    const payload = await response.json();
    if (activeSymbol !== symbol || requestId !== optionsLoadToken) return;
    optionsPanelState = { status: "ready", symbol, payload };
    renderOptionsPanel();
  } catch (error) {
    if (activeSymbol !== symbol || requestId !== optionsLoadToken) return;
    const code = error instanceof Error ? error.message : "options_unavailable";
    // No server-side token means options are intentionally off: drop the
    // section quietly instead of nagging in every equity chart modal.
    if (code === "options_not_configured") {
      optionsPanelState = null;
      renderOptionsPanel();
      return;
    }
    optionsPanelState = { status: "error", symbol, code };
    renderOptionsPanel();
  }
}

function renderOptionsPanel() {
  profileElement.querySelector("[data-options-panel]")?.remove();
  if (!optionsPanelState || !supportsOptionsAsset(activeAsset)) return;

  let html = "";
  if (optionsPanelState.status === "loading") {
    html = `
      <section class="options-snapshot options-loading" data-options-panel aria-label="Options positioning">
        <span class="loading-spinner" aria-hidden="true"></span>
        <span>Loading ${escapeHtml(optionsPanelState.symbol)} option chain</span>
      </section>
    `;
  } else if (optionsPanelState.status === "error") {
    html = optionsErrorMarkup(optionsPanelState.code);
  } else {
    html = optionsSnapshotMarkup(optionsPanelState.payload);
  }
  profileElement.insertAdjacentHTML("afterbegin", html);
  bindOptionsControls();
  resetProfileScroll();
  scheduleChartResize();
}

function optionsErrorMarkup(code) {
  const messages = {
    marketdata_auth_failed: "MarketData.app rejected the configured token.",
    marketdata_entitlement_required: "The MarketData.app account does not include this options data.",
    marketdata_rate_limited: "MarketData.app request limit reached. Try again shortly.",
    marketdata_no_data: "MarketData.app has no chain data for this expiration.",
    options_expirations_unavailable: "No listed option expirations were found.",
    options_expiration_not_found: "That expiration is no longer listed.",
    options_chain_empty: "MarketData.app returned no contracts for this expiration.",
  };
  const message = messages[code] || "The option chain could not be loaded.";
  return `
    <section class="options-snapshot options-unavailable" data-options-panel aria-label="Options positioning unavailable">
      <div>
        <span class="options-kicker">Options positioning</span>
        <strong>Snapshot unavailable</strong>
      </div>
      <span>${escapeHtml(message)}</span>
    </section>
  `;
}

function optionsSnapshotMarkup(payload) {
  const metrics = payload?.metrics || {};
  const quality = payload?.quality || {};
  const expirations = Array.isArray(payload?.expirations) ? payload.expirations : [];
  const selected = payload?.expiration || "";
  const expiryOptions = expirations
    .map(
      (expiration) =>
        `<option value="${escapeHtml(expiration)}"${expiration === selected ? " selected" : ""}>${escapeHtml(formatOptionsExpiry(expiration))}</option>`,
    )
    .join("");
  const netGex = numericOrNull(metrics.net_gex);
  const atmIv = numericOrNull(metrics.atm_iv);
  const putCall = numericOrNull(metrics.put_call_oi);
  const stale = Boolean(payload?.is_stale);
  const sourceLabel = payload?.source === "marketdata" ? "MarketData.app" : "Options data";

  return `
    <section class="options-snapshot${stale ? " is-stale" : ""}" data-options-panel aria-label="Options positioning">
      <header class="options-head">
        <div>
          <span class="options-kicker">Options positioning</span>
          <strong>GEX Snapshot</strong>
          <small>${sourceLabel} · ${escapeHtml(String(quality.greeks_coverage_pct ?? "--"))}% Greeks${stale ? " · stale" : ""}</small>
        </div>
        <label>
          <span>Expiry</span>
          <select data-options-expiration aria-label="Option expiration">${expiryOptions}</select>
        </label>
      </header>
      <div class="options-metrics">
        ${optionsMetric("Spot", formatCurrencyPrice(numericOrNull(payload?.spot), "USD"))}
        ${optionsMetric("ATM IV", atmIv === null ? "--" : formatPlainPct(atmIv * 100))}
        ${optionsMetric("Put / Call OI", putCall === null ? "--" : putCall.toFixed(2))}
        ${optionsMetric("Net GEX", formatSignedCompactDollars(netGex), changeClass(netGex))}
        ${optionsMetric("Call Wall", formatPrice(numericOrNull(metrics.call_wall)), "positive")}
        ${optionsMetric("Put Wall", formatPrice(numericOrNull(metrics.put_wall)), "negative")}
        ${optionsMetric("Max Pain", formatPrice(numericOrNull(metrics.max_pain)))}
      </div>
      <div class="options-profile-head">
        <span>Strike profile</span>
        <div role="group" aria-label="Options profile metric">
          <button type="button" data-options-mode="gex" class="${optionsProfileMode === "gex" ? "active" : ""}" aria-pressed="${optionsProfileMode === "gex"}">GEX</button>
          <button type="button" data-options-mode="oi" class="${optionsProfileMode === "oi" ? "active" : ""}" aria-pressed="${optionsProfileMode === "oi"}">OI</button>
        </div>
      </div>
      ${optionsStrikeProfile(payload)}
      <p class="options-method">Dealer gamma proxy · calls + / puts − · GEX is USD per 1% spot move · open interest updates daily</p>
    </section>
  `;
}

function optionsMetric(label, value, tone = "") {
  return `
    <div class="options-metric">
      <span>${escapeHtml(label)}</span>
      <strong class="${tone}">${escapeHtml(value)}</strong>
    </div>
  `;
}

function optionsStrikeProfile(payload) {
  const allRows = (Array.isArray(payload?.strikes) ? payload.strikes : [])
    .filter((row) => numericOrNull(row?.strike) !== null)
    .sort((left, right) => Number(left.strike) - Number(right.strike));
  if (!allRows.length) return '<div class="options-profile-empty">Strike profile unavailable</div>';

  const spot = numericOrNull(payload?.spot) ?? Number(allRows[0].strike);
  let nearestIndex = 0;
  allRows.forEach((row, index) => {
    if (Math.abs(Number(row.strike) - spot) < Math.abs(Number(allRows[nearestIndex].strike) - spot)) {
      nearestIndex = index;
    }
  });
  const visibleCount = Math.min(13, allRows.length);
  const start = Math.max(0, Math.min(allRows.length - visibleCount, nearestIndex - Math.floor(visibleCount / 2)));
  const rows = allRows.slice(start, start + visibleCount);
  const values =
    optionsProfileMode === "gex"
      ? rows.map((row) => Math.abs(numericOrNull(row.net_gex) ?? 0))
      : rows.flatMap((row) => [numericOrNull(row.call_oi) ?? 0, numericOrNull(row.put_oi) ?? 0]);
  const maxValue = Math.max(...values, 1);
  const atmStrike = Number(rows.reduce((nearest, row) =>
    Math.abs(Number(row.strike) - spot) < Math.abs(Number(nearest.strike) - spot) ? row : nearest,
  ).strike);

  const columns = rows
    .map((row) => {
      const strike = Number(row.strike);
      const isAtm = strike === atmStrike;
      let bars = "";
      let title = "";
      if (optionsProfileMode === "gex") {
        const value = numericOrNull(row.net_gex) ?? 0;
        const magnitude = value === 0 ? 0 : Math.max(2, Math.abs(value) / maxValue * 48);
        bars = `<i class="options-gex-bar ${value >= 0 ? "positive" : "negative"}" style="--bar:${magnitude.toFixed(2)}%"></i>`;
        title = `${formatPrice(strike)}: ${formatSignedCompactDollars(value)} net GEX`;
      } else {
        const callOi = numericOrNull(row.call_oi) ?? 0;
        const putOi = numericOrNull(row.put_oi) ?? 0;
        bars = `
          <i class="options-oi-bar call" style="--bar:${callOi === 0 ? 0 : Math.max(2, callOi / maxValue * 100).toFixed(2)}%"></i>
          <i class="options-oi-bar put" style="--bar:${putOi === 0 ? 0 : Math.max(2, putOi / maxValue * 100).toFixed(2)}%"></i>
        `;
        title = `${formatPrice(strike)}: ${formatCompactPrice(callOi)} call OI / ${formatCompactPrice(putOi)} put OI`;
      }
      return `
        <div class="options-strike${isAtm ? " is-atm" : ""}" title="${escapeHtml(title)}">
          <div class="options-bar-field">${bars}</div>
          <span>${escapeHtml(formatPrice(strike))}</span>
        </div>
      `;
    })
    .join("");

  return `
    <div class="options-profile-chart ${optionsProfileMode}" role="img" aria-label="${optionsProfileMode === "gex" ? "Net gamma exposure by strike" : "Call and put open interest by strike"}">
      ${columns}
    </div>
    <div class="options-profile-legend">
      ${
        optionsProfileMode === "gex"
          ? '<span><i class="positive"></i>Positive</span><span><i class="negative"></i>Negative</span>'
          : '<span><i class="call"></i>Calls</span><span><i class="put"></i>Puts</span>'
      }
      <span>ATM highlighted</span>
    </div>
  `;
}

function bindOptionsControls() {
  const expiration = profileElement.querySelector("[data-options-expiration]");
  expiration?.addEventListener("change", () => {
    if (!activeSymbol) return;
    const symbol = activeSymbol;
    const selected = expiration.value;
    setOptionsLoading(symbol);
    loadOptionsSnapshot(symbol, selected);
  });
  profileElement.querySelectorAll("[data-options-mode]").forEach((button) => {
    button.addEventListener("click", () => {
      const mode = button.dataset.optionsMode;
      if (!["gex", "oi"].includes(mode) || mode === optionsProfileMode) return;
      optionsProfileMode = mode;
      renderOptionsPanel();
    });
  });
}

function formatOptionsExpiry(value) {
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "2-digit",
    timeZone: "UTC",
  });
}

function formatSignedCompactDollars(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return `${value >= 0 ? "+" : "-"}$${formatCompactPrice(Math.abs(value))}`;
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
    renderOptionsPanel();
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
  renderOptionsPanel();
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
  renderOptionsPanel();
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

function topDialog() {
  return dialogStack.length ? dialogStack[dialogStack.length - 1].dialog : null;
}

function openDialog(dialog, focusTarget) {
  const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  // Re-opening a stacked dialog moves it to the top but keeps its original
  // trigger, so closing still returns focus to where the user started.
  const existingIndex = dialogStack.findIndex((entry) => entry.dialog === dialog);
  const entry = existingIndex !== -1 ? dialogStack.splice(existingIndex, 1)[0] : { dialog, trigger };
  dialogStack.push(entry);
  dialog.classList.add("open");
  dialog.setAttribute("aria-hidden", "false");
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
  const index = dialogStack.findIndex((entry) => entry.dialog === dialog);
  const wasTop = index === dialogStack.length - 1;
  const entry = index !== -1 ? dialogStack.splice(index, 1)[0] : null;
  if (!document.querySelector(".modal.open")) document.body.classList.remove("modal-open");
  // Focus returns to the closer's trigger only when the TOP dialog closed;
  // a lower dialog leaving the stack must not steal focus from the one
  // still open above it.
  if (wasTop && entry) {
    const remaining = topDialog();
    let returnTarget = dialogReturnTarget(entry.trigger);
    if (remaining && returnTarget && !remaining.contains(returnTarget)) returnTarget = null;
    const fallback = remaining ? firstFocusableElement(remaining) : null;
    (returnTarget || fallback)?.focus();
  }
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
    // closest() also rejects focusables nested inside [hidden] subtrees,
    // which computed style alone misses when the ancestor is display:none.
    if (element.closest("[hidden]")) return false;
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
  // Number(null) and Number("") are 0 — treat absent as absent.
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value));
  // Backslashes FIRST: the reverse order doubled the escape characters the
  // quote pass just inserted, producing invalid selectors.
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
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

// Server-provided strings become CSS class tokens; escapeHtml cannot help in
// class context, so anything beyond a plain lowercase word falls back safe.
function classToken(value, fallback = "neutral") {
  const token = String(value || fallback).toLowerCase();
  return /^[a-z][a-z0-9-]*$/.test(token) ? token : fallback;
}

function formatPrice(value, error) {
  if (error || !Number.isFinite(value)) return "--";
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(value) >= 1) return value.toFixed(2);
  // toPrecision flips to scientific notation for micro prices ("4.200e-7");
  // expand them to fixed decimals with 4 significant digits instead.
  if (Math.abs(value) < 1e-4) {
    return value.toLocaleString(undefined, { maximumSignificantDigits: 4, useGrouping: false });
  }
  return value.toPrecision(4);
}


function formatBoardPrice(value, error, currency) {
  if (error || !Number.isFinite(value)) return "--";
  // USX = US cents (CBOT/ICE): full precision, bare, like USD.
  if (!currency || currency === "USD" || currency === "USX") return formatPrice(value);
  // formatCompactPrice is abs-based, so restore the sign for negatives.
  return `${value < 0 ? "-" : ""}${currencyPrefix(currency)}${formatCompactPrice(value)}`;
}

function formatCurrencyPrice(value, currency = "USD") {
  if (!Number.isFinite(value)) return "--";
  const prefix = currencyPrefix(currency);
  // formatCompactPrice is abs-based, so restore the sign for negatives.
  if (currency && currency !== "USD") return `${value < 0 ? "-" : ""}${prefix}${formatCompactPrice(value)}`;
  return `${prefix}${formatPrice(value)}`;
}

function formatSigned(value) {
  if (typeof value !== "number") return "--";
  // Round FIRST, then take the sign from the rounded value: -0.0004 must
  // read +0.00, not -0.00 (the +0 normalizes -0 away).
  const digits = Math.abs(value) >= 100 ? 1 : 2;
  const rounded = Number(value.toFixed(digits)) + 0;
  return `${rounded >= 0 ? "+" : "-"}${Math.abs(rounded).toFixed(digits)}`;
}

function formatBoardSignedChange(value, currency) {
  if (typeof value !== "number") return "--";
  // Zero is flat and unsigned; keep it out of the signed non-USD path,
  // which would otherwise render "+₩0.00".
  if (value === 0) return "0.00";
  if (!currency || currency === "USD" || currency === "USX") return formatSigned(value);
  return `${value >= 0 ? "+" : "-"}${currencyPrefix(currency)}${formatCompactPrice(Math.abs(value))}`;
}

function formatSignedPct(value) {
  if (typeof value !== "number") return "--";
  // Round FIRST, then take the sign from the rounded value (see formatSigned).
  const rounded = Number(value.toFixed(2)) + 0;
  return `${rounded >= 0 ? "+" : ""}${rounded.toFixed(2)}%`;
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
  const code = typeof currency === "string" ? currency.trim().toUpperCase() : "";
  const known = {
    KRW: "₩",
    JPY: "¥",
    EUR: "€",
    GBP: "£",
    USD: "$",
    // US-cents quotes (CBOT/ICE ags): shown bare, the futures convention.
    USX: "",
  };
  if (Object.prototype.hasOwnProperty.call(known, code)) return known[code];
  // Unknown ISO-style codes are safe text; malformed provider strings never
  // cross into the many innerHTML-based price renderers.
  return /^[A-Z]{3}$/.test(code) ? `${code} ` : "";
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
  // The offset is the zone's CURRENT one, applied uniformly: a per-bar
  // offset goes backward across a DST fall-back (02:30 repeats), producing
  // non-monotonic times that silently corrupt the series. With a fixed
  // shift, bars from the other DST regime label ±1h off instead — the
  // standard fixed-offset display tradeoff.
  const date = new Date(value);
  return Math.floor(date.getTime() / 1000) + displayTzOffsetSeconds(new Date());
}
