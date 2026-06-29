const board = document.querySelector("#board");
const dailyBoard = document.querySelector("#daily-board");
const boardMeta = document.querySelector("#board-meta");
const statusCopy = document.querySelector("#status-copy");
const connectionState = document.querySelector("#connection-state");
const refreshButton = document.querySelector("#refresh-button");
const viewButtons = Array.from(document.querySelectorAll(".view-tabs button"));
const dailyView = document.querySelector("#daily-view");
const marketsView = document.querySelector("#markets-view");
const modal = document.querySelector("#chart-modal");
const modalClose = document.querySelector("#modal-close");
const chartTitle = document.querySelector("#chart-title");
const chartSubtitle = document.querySelector("#chart-subtitle");
const chartElement = document.querySelector("#chart");
const chartError = document.querySelector("#chart-error");
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
let activeRange = "ytd";
let activeInterval = "1d";
let chart = null;

const sourceLabels = {
  yahoo: "YH",
  hyperliquid: "HL",
  stooq: "STQ",
  finnhub: "FH",
};

init();

function init() {
  window.lucide?.createIcons();
  fetchQuotes();
  fetchCryptoEtfFlows();
  if (shouldUseWebSocket()) openSocket();
  window.setInterval(fetchQuotes, 15000);
  window.setInterval(fetchCryptoEtfFlows, 300000);
  refreshButton.addEventListener("click", () => {
    fetchQuotes();
    fetchCryptoEtfFlows();
  });
  viewButtons.forEach((button) => {
    button.addEventListener("click", () => selectView(button.dataset.view || "daily"));
  });
  modalClose.addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  editorOpen.addEventListener("click", openEditor);
  editorClose.addEventListener("click", closeEditor);
  editorModal.addEventListener("click", (event) => {
    if (event.target === editorModal) closeEditor();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeModal();
      closeEditor();
    }
  });
  intervalButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeRange = button.dataset.range || "ytd";
      activeInterval = button.dataset.interval || "1d";
      intervalButtons.forEach((item) => item.classList.toggle("active", item === button));
      if (activeSymbol) loadChart(activeSymbol, activeRange, activeInterval);
    });
  });
  groupForm.addEventListener("submit", addGroup);
  assetForm.addEventListener("submit", addAsset);
  assetTypeSelect.addEventListener("change", syncSourceToType);
}

function selectView(view) {
  const showDaily = view === "daily";
  dailyView.hidden = !showDaily;
  marketsView.hidden = showDaily;
  viewButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === view));
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

  socket.addEventListener("open", () => setConnection("live"));
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "quotes") {
      applyQuotes(message.data);
      setConnection("live");
    }
  });
  socket.addEventListener("close", () => {
    setConnection("error");
    window.setTimeout(openSocket, 3000);
  });
  socket.addEventListener("error", () => setConnection("error"));
}

function applyQuotes(payload) {
  latestData = payload;
  renderBoard(payload);
  renderDailyBoard(payload.overview, latestCryptoEtfFlows);
  updateHeader(payload.overview);
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
  const date = Number.isNaN(asOf.getTime()) ? "--" : asOf.toISOString().slice(0, 10);
  const time = Number.isNaN(asOf.getTime()) ? "--" : formatClock(asOf);
  boardMeta.textContent = `${date} · ${universe.total || 0} names · universe v2`;
  statusCopy.textContent = [
    "LIVE QUOTES",
    `${universe.quoted || 0}/${universe.total || 0} QUOTED`,
    `HISTORY ${universe.history_count || 0}/${universe.total || 0}`,
    `FLOWS ${flowStatusLabel(latestCryptoEtfFlows)}`,
    `UPDATED ${time}`,
  ].join(" · ");
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
  const movers = [...themes]
    .sort((a, b) => Math.abs(b.acceleration || 0) - Math.abs(a.acceleration || 0))
    .slice(0, 8);
  const asOf = new Date(overview.as_of);
  const asOfLabel = Number.isNaN(asOf.getTime()) ? "" : `As of ${asOf.toISOString().slice(0, 10)}`;

  dailyBoard.innerHTML = `
    <section class="analytics-panel">
      ${panelHeading("Regime Read", asOfLabel)}
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
        ${panelHeading("Benchmarks", "Return / Dist 50DMA / ATR Ext")}
        <div class="benchmark-grid">
          ${benchmarks.map(benchmarkCard).join("") || '<div class="empty-state">Add ETF_MACRO benchmarks</div>'}
        </div>
      </section>

      <section class="analytics-panel">
        ${panelHeading("Breadth", `${universe.history_count || 0} names with history`)}
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
        ${panelHeading("Dominant Themes", "Score / 1D / 5D / Status")}
        ${themeTable(themes.slice(0, 8))}
      </section>
      <section class="analytics-panel">
        ${panelHeading("Momentum Shifts", "Largest momentum shifts")}
        ${themeTable(movers)}
      </section>
    </div>

    <section class="analytics-panel">
      ${panelHeading("Crypto ETF Flows", cryptoEtfFlowNote(cryptoEtfFlows))}
      ${cryptoEtfFlowPanel(cryptoEtfFlows)}
    </section>

    <section class="analytics-panel">
      ${panelHeading("Theme Rotation", "1D move versus 5D daily pace")}
      <div class="rotation-grid">
        ${rotationColumn("↑ Climbers", rotation.climbers || [])}
        ${rotationColumn("↓ Fallers", rotation.fallers || [])}
      </div>
    </section>
  `;

  dailyBoard.querySelectorAll(".benchmark-card").forEach((card) => {
    card.addEventListener("click", () => openChart(card.dataset.symbol, card.dataset.name, ""));
  });
}

function panelHeading(title, note) {
  return `<header class="panel-heading"><h2>${escapeHtml(title)}</h2><span>${escapeHtml(note || "")}</span></header>`;
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
    <div class="metric-value split-value"><span class="tone-positive">${formatInteger(positive)}</span><span>/</span><span class="tone-negative">${formatInteger(negative)}</span></div>
    <span class="metric-detail">${escapeHtml(detail)}</span>
  </div>`;
}

function benchmarkCard(item) {
  return `<button class="benchmark-card" type="button" data-symbol="${escapeHtml(item.symbol)}" data-name="${escapeHtml(item.name || "")}">
    <span class="benchmark-symbol">${escapeHtml(item.symbol)}</span>
    <span class="benchmark-name">${escapeHtml(item.name || item.type || "Benchmark")}</span>
    <span class="metric-lines">
      ${metricLine("1D", formatSignedPct(item.change_1d), changeClass(item.change_1d))}
      ${metricLine("5D", formatSignedPct(item.change_5d), changeClass(item.change_5d))}
      ${metricLine(">50DMA", formatSignedPct(item.distance_50dma), changeClass(item.distance_50dma))}
      ${metricLine("ATR ext", formatSignedNumber(item.atr_extension), changeClass(item.atr_extension))}
    </span>
  </button>`;
}

function metricLine(label, value, tone) {
  return `<span class="metric-line"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></span>`;
}

function breadthRow(label, value, tone = "") {
  return `<div class="breadth-row"><span>${escapeHtml(label)}</span><strong class="${tone ? `tone-${tone}` : ""}">${escapeHtml(value)}</strong></div>`;
}

function themeTable(themes) {
  return `<table class="theme-table">
    <thead><tr><th>#</th><th>Theme</th><th>Score</th><th>1D</th><th>5D</th><th>Status</th></tr></thead>
    <tbody>${themes.map(themeRow).join("") || '<tr><td colspan="6">No themes configured</td></tr>'}</tbody>
  </table>`;
}

function themeRow(theme) {
  return `<tr>
    <td>${formatInteger(theme.rank)}</td>
    <td>${escapeHtml(displayGroupName(theme.name))}<span class="member-count">${formatInteger(theme.count)}</span></td>
    <td><span class="score-value">${formatInteger(theme.score)}</span></td>
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
  return `<div class="crypto-flow-grid">
    ${assets.map(cryptoEtfFlowCard).join("") || '<div class="empty-state">No ETF flow data</div>'}
  </div>`;
}

function cryptoEtfFlowCard(asset) {
  return `<div class="crypto-flow-card">
    <div class="crypto-flow-summary">
      <div>
        <span class="metric-label">${escapeHtml(asset.name || `${asset.asset} ETFs`)}</span>
        <strong class="metric-value ${changeClass(asset.latest_flow_usd)}">${formatUsdFlow(asset.latest_flow_usd)}</strong>
      </div>
      <div class="flow-side-metrics">
        ${metricLine("5D", formatUsdFlow(asset.five_day_flow_usd), changeClass(asset.five_day_flow_usd))}
        ${metricLine("10D", formatUsdFlow(asset.ten_day_flow_usd), changeClass(asset.ten_day_flow_usd))}
        ${metricLine("Date", formatFlowDate(asset.latest_date), "")}
      </div>
    </div>
    <div class="crypto-flow-lists">
      ${flowList("Inflows", asset.leaders || [], "change-positive")}
      ${flowList("Outflows", asset.laggards || [], "change-negative")}
    </div>
  </div>`;
}

function flowList(label, items, tone) {
  return `<div class="flow-list">
    <span class="flow-list-label">${escapeHtml(label)}</span>
    ${(items || []).map((item) => flowItem(item, tone)).join("") || '<span class="flow-empty">None reported</span>'}
  </div>`;
}

function flowItem(item, tone) {
  return `<div class="flow-item">
    <strong>${escapeHtml(item.ticker || "--")}</strong>
    <span class="${tone}">${formatUsdFlow(item.flow_usd)}</span>
  </div>`;
}

function cryptoEtfFlowError(error) {
  if (error === "farside_fetch_failed") return "Farside flows unavailable";
  return "ETF flows unavailable";
}

function renderBoard(payload) {
  const groups = payload.groups || [];
  if (!groups.length) {
    board.innerHTML = '<div class="empty-state">No groups configured</div>';
    return;
  }

  board.replaceChildren(
    ...groups.map((group) => {
      const panel = document.createElement("section");
      panel.className = "group-panel";

      const header = document.createElement("div");
      header.className = "group-title";
      header.innerHTML = "<span></span><span>Last</span><span>Abs</span><span>%</span><span>Src</span>";
      header.firstChild.textContent = displayGroupName(group.name);
      panel.appendChild(header);

      (group.assets || []).forEach((asset) => panel.appendChild(renderRow(asset)));
      return panel;
    })
  );
}

async function openEditor() {
  editorModal.classList.add("open");
  editorModal.setAttribute("aria-hidden", "false");
  await fetchWatchlistConfig();
}

function closeEditor() {
  editorModal.classList.remove("open");
  editorModal.setAttribute("aria-hidden", "true");
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
  await mutateWatchlists(`/api/groups/${encodeURIComponent(groupName)}`, { method: "DELETE" });
}

async function removeAsset(groupName, symbol) {
  await mutateWatchlists(
    `/api/groups/${encodeURIComponent(groupName)}/assets/${encodeURIComponent(symbol)}`,
    { method: "DELETE" }
  );
}

async function mutateWatchlists(url, options) {
  setEditorStatus("Saving");
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    setEditorStatus(payload.detail || "Save failed");
    return;
  }
  watchlistConfig = await response.json();
  renderEditor();
  setEditorStatus("Saved");
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
  const quote = asset.quote || {};
  const row = document.createElement("button");
  row.type = "button";
  row.className = `asset-row${quote.is_stale ? " stale-row" : ""}`;
  row.dataset.symbol = asset.symbol;
  row.dataset.provider = quote.provider || "";
  row.setAttribute("aria-label", `${asset.symbol} chart`);
  row.addEventListener("click", () => openChart(asset.symbol, asset.name, quote.provider));

  row.appendChild(symbolCell(asset));
  row.appendChild(textCell(formatPrice(quote.last, quote.error)));
  row.appendChild(changeCell(formatSigned(quote.change_abs), quote.change_pct));
  row.appendChild(changeCell(formatSignedPct(quote.change_pct), quote.change_pct));
  row.appendChild(sourceCell(quote));
  return row;
}

function symbolCell(asset) {
  const cell = document.createElement("span");
  cell.className = "symbol-cell";
  const symbol = document.createElement("strong");
  symbol.textContent = asset.symbol;
  const name = document.createElement("small");
  name.textContent = asset.name || asset.exchange || asset.type || "";
  cell.append(symbol, name);
  return cell;
}

function textCell(text) {
  const cell = document.createElement("span");
  cell.textContent = text;
  return cell;
}

function changeCell(text, changePct) {
  const cell = textCell(text);
  cell.classList.add(changeClass(changePct));
  return cell;
}

function sourceCell(quote) {
  const cell = textCell(quote.is_stale ? "STALE" : sourceLabels[quote.provider] || "--");
  cell.className = "source-cell";
  return cell;
}

function openChart(symbol, name, provider) {
  activeSymbol = symbol;
  activeRange = "ytd";
  activeInterval = "1d";
  intervalButtons.forEach((item) => item.classList.toggle("active", item.dataset.range === "ytd"));
  chartTitle.textContent = symbol;
  chartSubtitle.textContent = [name, sourceLabels[provider] || provider].filter(Boolean).join(" / ");
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  loadChart(symbol, activeRange, activeInterval);
}

function closeModal() {
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  activeSymbol = null;
  if (chart) {
    chart.remove();
    chart = null;
  }
}

async function loadChart(symbol, range, interval) {
  chartError.hidden = true;
  chartError.textContent = "";
  chartElement.replaceChildren();
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
    const bars = (payload.bars || []).map((bar) => ({
      time: toChartTime(bar.timestamp, interval),
      open: Number(bar.open),
      high: Number(bar.high),
      low: Number(bar.low),
      close: Number(bar.close),
    }));
    if (!bars.length) throw new Error("No history available");
    renderChart(bars, interval);
    chartSubtitle.textContent = `${symbol} / ${range.toUpperCase()} / ${interval} / ${bars.length} bars`;
  } catch (error) {
    chartError.textContent = error.message === "No history available" ? error.message : "Chart unavailable";
    chartError.hidden = false;
  }
}

function renderChart(bars, interval) {
  if (!window.LightweightCharts) throw new Error("Chart library unavailable");
  const chartWidth = chartElement.clientWidth || 900;
  const chartHeight = Math.max(chartElement.clientHeight, 340);
  chart = window.LightweightCharts.createChart(chartElement, {
    width: chartWidth,
    height: chartHeight,
    layout: { background: { color: "#0a0b0c" }, textColor: "#a4abb3" },
    grid: { vertLines: { color: "#181a1d" }, horzLines: { color: "#181a1d" } },
    rightPriceScale: { borderColor: "#23262a" },
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
  chart.timeScale().fitContent();
}

function setConnection(state) {
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

function formatSigned(value) {
  if (typeof value !== "number") return "--";
  const abs = Math.abs(value);
  const formatted = abs >= 100 ? abs.toFixed(1) : abs.toFixed(2);
  return `${value >= 0 ? "+" : "-"}${formatted}`;
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

function formatClock(date) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function displayGroupName(value) {
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
