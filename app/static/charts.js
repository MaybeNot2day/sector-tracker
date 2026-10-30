// Shared SVG renderer for the dashboard's non-trading charts.
//
// Call sites embed chartMarkup(spec) in the HTML strings they already build.
// Once a placeholder is attached and has a size, this module draws it at its
// real pixel size (so text and markers never stretch), redraws it on resize,
// and adds a crosshair tooltip. Colors are CSS custom properties, so theme
// switches need no redraw.
//
// Spec:
//   x:        category labels (strings), one per point
//   series:   [{ label, type: "bar" | "line" | "band", values, low, high,
//               color, dashed, area, smooth, endDot, opacity, className,
//               barClass(value, index), format(value), tooltip: false }]
//   y:        { format(value), zero: true | false }
//   height | aspect: CSS height ("200px", "clamp(...)") or aspect-ratio ("19 / 8")
//   axes:     false for compact cards (no labels, no gridlines)
//   references: [{ value, label }] dashed horizontal guides
//   tooltipTitle(index), tooltipExtra(index) -> [[label, value], ...]

const BAR_GAP = 1;
const FONT_WIDTH = 6.2;
const MAX_PENDING = 400;

const pending = new Map();
const charts = new WeakMap();
let nextId = 0;

const resizeObserver =
  typeof ResizeObserver === "function"
    ? new ResizeObserver((entries) => entries.forEach((entry) => draw(entry.target)))
    : null;

if (typeof MutationObserver === "function" && typeof document !== "undefined") {
  new MutationObserver((records) => {
    for (const record of records) {
      record.addedNodes.forEach((node) => forEachChart(node, hydrate));
      record.removedNodes.forEach((node) => forEachChart(node, release));
    }
  }).observe(document.documentElement, { childList: true, subtree: true });
}

export function chartMarkup(spec, { className = "", label = "" } = {}) {
  const id = `viz-${(nextId += 1)}`;
  pending.set(id, spec);
  // Markup that is built but never attached must not accumulate.
  if (pending.size > MAX_PENDING) pending.delete(pending.keys().next().value);
  const size = spec.aspect ? `aspect-ratio:${spec.aspect}` : `height:${spec.height || "240px"}`;
  return `<div class="viz ${esc(className)}" data-viz="${id}" role="img" aria-label="${esc(label)}" style="${size}"></div>`;
}

export function chartLegend(series, extras = []) {
  const items = series
    .filter((item) => item.legend !== false)
    .map((item) => {
      const kind = item.type === "line" ? (item.dashed ? "line dashed" : "line") : item.type;
      const opacity = item.opacity !== undefined ? `;opacity:${item.opacity}` : "";
      return `<span><i class="viz-swatch viz-swatch-${kind}" style="--swatch:${item.color}${opacity}"></i>${esc(item.label)}</span>`;
    });
  return items.concat(extras.map((text) => `<span>${esc(text)}</span>`)).join("");
}

export function niceTicks(min, max, count) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return { lo: 0, hi: 1, ticks: [0, 1] };
  if (min === max) {
    const pad = Math.abs(min) * 0.1 || 1;
    min -= pad;
    max += pad;
  }
  const step = niceStep((max - min) / Math.max(count, 1));
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let value = lo; value <= hi + step / 2; value += step) {
    ticks.push(Number(value.toPrecision(12)));
  }
  return { lo, hi, ticks };
}

function niceStep(raw) {
  const exponent = 10 ** Math.floor(Math.log10(raw));
  const fraction = raw / exponent;
  const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 2.5 ? 2.5 : fraction <= 5 ? 5 : 10;
  return nice * exponent;
}

function forEachChart(node, callback) {
  if (node.nodeType !== 1) return;
  if (node.dataset?.viz) callback(node);
  node.querySelectorAll?.("[data-viz]").forEach(callback);
}

function hydrate(element) {
  const spec = pending.get(element.dataset.viz);
  if (!spec) {
    // A drawn chart that was moved within the page keeps its state.
    if (charts.has(element) && resizeObserver) resizeObserver.observe(element);
    return;
  }
  pending.delete(element.dataset.viz);
  charts.set(element, { spec, width: 0, height: 0, animated: false, layout: null });
  if (resizeObserver) resizeObserver.observe(element);
  draw(element);
}

function release(element) {
  // Records arrive after the whole mutation; a moved node is connected again.
  if (element.isConnected) return;
  if (resizeObserver) resizeObserver.unobserve(element);
  charts.delete(element);
}

function draw(element) {
  const state = charts.get(element);
  if (!state || !element.isConnected) return;
  const width = Math.round(element.clientWidth);
  const height = Math.round(element.clientHeight);
  // Hidden tabs report 0×0; they are drawn when they become visible.
  if (!width || !height || (width === state.width && height === state.height)) return;
  state.width = width;
  state.height = height;
  const animate = !state.animated && !prefersReducedMotion();
  const { markup, layout } = renderXY(state.spec, width, height, element.dataset.viz);
  element.innerHTML = markup;
  element.classList.toggle("viz-animate", animate);
  state.animated = true;
  state.layout = layout;
  if (layout && state.spec.tooltip !== false && !state.hoverBound) bindHover(element, state);
}

function prefersReducedMotion() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// --- Layout ---------------------------------------------------------------

function renderXY(spec, width, height, id) {
  const labels = spec.x || [];
  const series = (spec.series || []).filter((item) => item && (item.values || item.low));
  const count = labels.length;
  if (!count || !series.length) {
    return { markup: `<div class="viz-empty">${esc(spec.emptyText || "No data yet")}</div>`, layout: null };
  }
  const compact = spec.axes === false;
  const bars = series.filter((item) => item.type === "bar");
  const stacks = stackBars(bars, count);
  const values = [];
  series.forEach((item) => {
    if (item.type === "band") {
      (item.low || []).forEach((value) => values.push(value));
      (item.high || []).forEach((value) => values.push(value));
    } else if (item.type !== "bar") {
      (item.values || []).forEach((value) => values.push(value));
    }
  });
  stacks.forEach((stack) => values.push(stack.positive, stack.negative));
  (spec.references || []).forEach((reference) => values.push(reference.value));
  const finite = values.filter((value) => Number.isFinite(value));
  let min = Math.min(...finite);
  let max = Math.max(...finite);
  if (bars.length || spec.y?.zero) {
    min = Math.min(min, 0);
    max = Math.max(max, 0);
  }
  const format = spec.y?.format || ((value) => String(value));
  const top = compact ? 6 : 12;
  const bottom = compact ? 6 : 24;
  const plotHeight = Math.max(height - top - bottom, 20);
  // Compact cards have no axis to label, so they keep a snug data range.
  const span = max - min || Math.abs(max) * 0.1 || 1;
  const { lo, hi, ticks } = compact
    ? { lo: min - span * 0.06, hi: max + span * 0.06, ticks: [] }
    : niceTicks(min, max, Math.max(2, Math.min(5, Math.floor(plotHeight / 46))));
  const tickLabels = ticks.map((value) => format(value));
  const left = compact ? 4 : Math.max(...tickLabels.map((text) => text.length), 2) * FONT_WIDTH + 14;
  const right = compact ? 4 : 14;
  const plotWidth = Math.max(width - left - right, 20);
  const banded = bars.length > 0;
  const band = plotWidth / count;
  const x = (index) =>
    banded
      ? left + band * (index + 0.5)
      : count === 1
        ? left + plotWidth / 2
        : left + (plotWidth * index) / (count - 1);
  const y = (value) => top + ((hi - value) / (hi - lo || 1)) * plotHeight;
  const baseline = y(Math.min(Math.max(0, lo), hi));
  const layout = { left, right, top, plotWidth, plotHeight, width, height, count, banded, band, x, y, labels, series, stacks, spec };

  const parts = [];
  const defs = [];
  if (!compact) {
    ticks.forEach((value, index) => {
      const ty = y(value).toFixed(1);
      parts.push(`<line class="viz-grid${value === 0 ? " viz-zero" : ""}" x1="${left}" x2="${width - right}" y1="${ty}" y2="${ty}"/>`);
      parts.push(`<text class="viz-axis" x="${left - 8}" y="${ty}" dy="3.5" text-anchor="end">${esc(tickLabels[index])}</text>`);
    });
    parts.push(xAxisLabels(layout, spec.xFormat));
  }
  (spec.references || []).forEach((reference) => {
    const ry = y(reference.value).toFixed(1);
    parts.push(`<line class="viz-reference" x1="${left}" x2="${width - right}" y1="${ry}" y2="${ry}"/>`);
    if (reference.label && !compact) {
      parts.push(`<text class="viz-reference-label" x="${width - right}" y="${ry}" dy="-4" text-anchor="end">${esc(reference.label)}</text>`);
    }
  });
  series.forEach((item, index) => {
    if (item.type === "band") parts.push(bandPath(item, layout));
  });
  if (bars.length) parts.push(barRects(bars, stacks, layout, baseline));
  series.forEach((item, index) => {
    if (item.type === "line") parts.push(linePath(item, layout, `${id}-g${index}`, defs));
  });
  parts.push(`<g class="viz-hover" hidden></g>`);
  const markup = `
    <svg class="viz-svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">
      ${defs.length ? `<defs>${defs.join("")}</defs>` : ""}
      ${parts.join("")}
    </svg>
    <div class="viz-tip" hidden></div>
  `;
  return { markup, layout };
}

function stackBars(bars, count) {
  return Array.from({ length: count }, (_, index) => {
    let positive = 0;
    let negative = 0;
    bars.forEach((item) => {
      const value = Number(item.values?.[index]);
      if (!Number.isFinite(value)) return;
      if (value >= 0) positive += value;
      else negative += value;
    });
    return { positive, negative };
  });
}

function xAxisLabels(layout, xFormat) {
  const { labels, count, banded, band, plotWidth, x, height } = layout;
  const text = labels.map((label) => (xFormat ? xFormat(label) : String(label)));
  const widest = Math.max(...text.map((label) => label.length), 1) * FONT_WIDTH + 14;
  const slot = banded ? band : plotWidth / Math.max(count - 1, 1);
  const step = Math.max(1, Math.ceil(widest / slot));
  const parts = [];
  // Anchor thinning on the latest point so the most recent label always shows.
  for (let index = count - 1; index >= 0; index -= step) {
    let anchor = "middle";
    if (!banded && count > 1 && index === 0) anchor = "start";
    if (!banded && count > 1 && index === count - 1) anchor = "end";
    parts.push(`<text class="viz-axis" x="${x(index).toFixed(1)}" y="${height - 7}" text-anchor="${anchor}">${esc(text[index])}</text>`);
  }
  return parts.join("");
}

function barRects(bars, stacks, layout, baseline) {
  const { band, x, y, count } = layout;
  const width = Math.max(Math.min(46, band * 0.62), 2);
  const parts = [];
  for (let index = 0; index < count; index += 1) {
    let up = 0;
    let down = 0;
    const left = (x(index) - width / 2).toFixed(1);
    bars.forEach((item) => {
      const value = Number(item.values?.[index]);
      if (!Number.isFinite(value) || value === 0) return;
      const from = value >= 0 ? up : down;
      const to = from + value;
      if (value >= 0) up = to;
      else down = to;
      const y0 = y(from);
      const y1 = y(to);
      const topY = Math.min(y0, y1);
      // A hairline between stacked segments reads as a deliberate split.
      const gap = from !== 0 ? BAR_GAP : 0;
      const segment = Math.max(Math.abs(y1 - y0) - gap, 1);
      const segmentTop = value >= 0 ? topY : topY + gap;
      const classes = ["viz-bar", item.className, item.barClass?.(value, index)].filter(Boolean).join(" ");
      const opacity = item.opacity !== undefined ? `;fill-opacity:${item.opacity}` : "";
      parts.push(`<rect class="${esc(classes)}" x="${left}" y="${segmentTop.toFixed(1)}" width="${width.toFixed(1)}" height="${segment.toFixed(1)}" rx="2" style="fill:${item.colorFor ? item.colorFor(value) : item.color}${opacity};transform-origin:0 ${baseline.toFixed(1)}px;animation-delay:${index * 28}ms"/>`);
    });
  }
  return parts.join("");
}

function bandPath(item, layout) {
  const { x, y, count } = layout;
  const upper = [];
  const lower = [];
  for (let index = 0; index < count; index += 1) {
    const low = Number(item.low?.[index]);
    const high = Number(item.high?.[index]);
    if (!Number.isFinite(low) || !Number.isFinite(high)) continue;
    upper.push([x(index), y(high)]);
    lower.push([x(index), y(low)]);
  }
  if (!upper.length) return "";
  const points = upper.concat(lower.reverse());
  const d = points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join("");
  return `<path class="viz-band ${esc(item.className || "")}" d="${d}Z" style="fill:${item.color};fill-opacity:${item.opacity ?? 0.16}"/>`;
}

function linePath(item, layout, gradientId, defs) {
  const { x, y, count, top, plotHeight } = layout;
  const segments = [];
  let current = [];
  for (let index = 0; index < count; index += 1) {
    const value = Number(item.values?.[index]);
    if (item.values?.[index] === null || item.values?.[index] === undefined || !Number.isFinite(value)) {
      if (current.length) segments.push(current);
      current = [];
      continue;
    }
    current.push([x(index), y(value)]);
  }
  if (current.length) segments.push(current);
  if (!segments.length) return "";
  const path = segments.map((points) => (item.smooth ? smoothPath(points) : linearPath(points))).join("");
  const parts = [];
  if (item.area) {
    defs.push(`<linearGradient id="${gradientId}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" style="stop-color:${item.color};stop-opacity:0.28"/><stop offset="1" style="stop-color:${item.color};stop-opacity:0"/></linearGradient>`);
    const floor = (top + plotHeight).toFixed(1);
    segments.forEach((points) => {
      const first = points[0];
      const last = points[points.length - 1];
      const body = item.smooth ? smoothPath(points) : linearPath(points);
      parts.push(`<path class="viz-area" d="${body}L${last[0].toFixed(1)},${floor}L${first[0].toFixed(1)},${floor}Z" style="fill:url(#${gradientId})"/>`);
    });
  }
  const classes = ["viz-line", item.dashed ? "viz-dashed" : "viz-draw", item.className].filter(Boolean).join(" ");
  // pathLength normalizes the draw-in animation; a dash pattern would be
  // measured in those units too, so dashed lines keep their real length.
  const length = item.dashed ? "" : ' pathLength="1"';
  parts.push(`<path class="${esc(classes)}" d="${path}"${length} style="stroke:${item.color};stroke-width:${item.width || 2}"/>`);
  if (item.endDot) {
    const last = segments[segments.length - 1].at(-1);
    parts.push(`<circle class="viz-end-dot" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="3.5" style="fill:${item.color}"/>`);
  }
  return parts.join("");
}

function linearPath(points) {
  return points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join("");
}

// Monotone cubic interpolation (Fritsch–Carlson): smooth, but never overshoots
// the data, so peaks and troughs stay where the values are.
function smoothPath(points) {
  if (points.length < 3) return linearPath(points);
  const count = points.length;
  const slopes = [];
  for (let index = 0; index < count - 1; index += 1) {
    const [x0, y0] = points[index];
    const [x1, y1] = points[index + 1];
    slopes.push((y1 - y0) / (x1 - x0 || 1));
  }
  const tangents = [slopes[0]];
  for (let index = 1; index < count - 1; index += 1) {
    const before = slopes[index - 1];
    const after = slopes[index];
    tangents.push(before * after <= 0 ? 0 : (before + after) / 2);
  }
  tangents.push(slopes[count - 2]);
  for (let index = 0; index < count - 1; index += 1) {
    if (slopes[index] === 0) {
      tangents[index] = 0;
      tangents[index + 1] = 0;
      continue;
    }
    const a = tangents[index] / slopes[index];
    const b = tangents[index + 1] / slopes[index];
    const sum = a * a + b * b;
    if (sum > 9) {
      const scale = 3 / Math.sqrt(sum);
      tangents[index] = scale * a * slopes[index];
      tangents[index + 1] = scale * b * slopes[index];
    }
  }
  let d = `M${points[0][0].toFixed(1)},${points[0][1].toFixed(1)}`;
  for (let index = 0; index < count - 1; index += 1) {
    const [x0, y0] = points[index];
    const [x1, y1] = points[index + 1];
    const dx = (x1 - x0) / 3;
    d += `C${(x0 + dx).toFixed(1)},${(y0 + tangents[index] * dx).toFixed(1)} ${(x1 - dx).toFixed(1)},${(y1 - tangents[index + 1] * dx).toFixed(1)} ${x1.toFixed(1)},${y1.toFixed(1)}`;
  }
  return d;
}

// --- Hover ----------------------------------------------------------------

function bindHover(element, state) {
  state.hoverBound = true;
  const move = (event) => {
    const layout = state.layout;
    if (!layout) return;
    const bounds = element.getBoundingClientRect();
    const px = event.clientX - bounds.left;
    const { left, plotWidth, count, banded, band } = layout;
    if (px < left - 4 || px > left + plotWidth + 4) {
      hide(element);
      return;
    }
    const index = banded
      ? Math.min(count - 1, Math.max(0, Math.floor((px - left) / band)))
      : Math.min(count - 1, Math.max(0, Math.round(((px - left) / plotWidth) * Math.max(count - 1, 1))));
    show(element, layout, index);
  };
  element.addEventListener("pointermove", move);
  element.addEventListener("pointerdown", move);
  element.addEventListener("pointerleave", () => hide(element));
}

function hide(element) {
  const hover = element.querySelector(".viz-hover");
  const tip = element.querySelector(".viz-tip");
  if (hover) hover.setAttribute("hidden", "");
  if (tip) tip.hidden = true;
}

function show(element, layout, index) {
  const hover = element.querySelector(".viz-hover");
  const tip = element.querySelector(".viz-tip");
  if (!hover || !tip) return;
  const { x, y, top, plotHeight, banded, band, series, spec, labels, stacks } = layout;
  const px = x(index);
  const marks = [];
  if (banded) {
    marks.push(`<rect class="viz-hover-band" x="${(px - band / 2).toFixed(1)}" y="${top}" width="${band.toFixed(1)}" height="${plotHeight}"/>`);
  } else {
    marks.push(`<line class="viz-crosshair" x1="${px.toFixed(1)}" x2="${px.toFixed(1)}" y1="${top}" y2="${top + plotHeight}"/>`);
  }
  const format = spec.y?.format || ((value) => String(value));
  const rows = [];
  series.forEach((item) => {
    if (item.tooltip === false) return;
    const formatValue = item.format || format;
    if (item.type === "band") {
      const low = Number(item.low?.[index]);
      const high = Number(item.high?.[index]);
      if (Number.isFinite(low) && Number.isFinite(high)) {
        rows.push(tipRow(item, `${formatValue(low)} – ${formatValue(high)}`));
      }
      return;
    }
    const raw = item.values?.[index];
    const value = Number(raw);
    if (raw === null || raw === undefined || !Number.isFinite(value)) return;
    if (item.type === "bar" && value === 0) return;
    rows.push(tipRow(item, formatValue(value)));
    if (item.type === "line") {
      marks.push(`<circle class="viz-hover-dot" cx="${px.toFixed(1)}" cy="${y(value).toFixed(1)}" r="4" style="stroke:${item.color}"/>`);
    }
  });
  const barCount = series.filter((item) => item.type === "bar" && Number(item.values?.[index])).length;
  if (barCount > 1) {
    const total = stacks[index].positive + stacks[index].negative;
    rows.push(`<div class="viz-tip-row viz-tip-total"><span>Total</span><strong>${esc(format(total))}</strong></div>`);
  }
  (spec.tooltipExtra?.(index) || []).forEach(([label, value]) => {
    rows.push(`<div class="viz-tip-row viz-tip-note"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`);
  });
  hover.innerHTML = marks.join("");
  hover.removeAttribute("hidden");
  const title = spec.tooltipTitle ? spec.tooltipTitle(index) : labels[index];
  tip.innerHTML = `<div class="viz-tip-title">${esc(title)}</div>${rows.join("")}`;
  tip.hidden = false;
  const tipWidth = tip.offsetWidth;
  const leftPos = px + 14 + tipWidth > layout.width ? px - 14 - tipWidth : px + 14;
  tip.style.left = `${Math.max(4, leftPos)}px`;
  tip.style.top = `${top}px`;
}

function tipRow(item, value) {
  const kind = item.type === "line" ? (item.dashed ? "line dashed" : "line") : item.type;
  const opacity = item.opacity !== undefined ? `;opacity:${item.opacity}` : "";
  return `<div class="viz-tip-row"><span><i class="viz-swatch viz-swatch-${kind}" style="--swatch:${item.color}${opacity}"></i>${esc(item.label)}</span><strong>${esc(value)}</strong></div>`;
}

// charts.js deliberately has no imports: minify-static.sh content-pins module
// specifiers only inside app.js, so importing formatting.js here would load a
// second, unpinned copy under an immutable cache header.
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
