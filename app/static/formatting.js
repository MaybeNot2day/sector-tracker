// Shared, DOM-independent presentation for boards, charts, lists and reports.
export function formatPrice(value, error) {
  if (error || !Number.isFinite(value)) return "--";
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(value) >= 1) return value.toFixed(2);
  if (Math.abs(value) < 1e-4) {
    return value.toLocaleString(undefined, { maximumSignificantDigits: 4, useGrouping: false });
  }
  return value.toPrecision(4);
}

export function formatBoardPrice(value, error, currency) {
  if (error || !Number.isFinite(value)) return "--";
  if (!currency || currency === "USD" || currency === "USX") return formatPrice(value);
  return `${value < 0 ? "-" : ""}${currencyPrefix(currency)}${formatCompactPrice(value)}`;
}

export function formatCurrencyPrice(value, currency = "USD") {
  if (!Number.isFinite(value)) return "--";
  const prefix = currencyPrefix(currency);
  if (currency && currency !== "USD") return `${value < 0 ? "-" : ""}${prefix}${formatCompactPrice(value)}`;
  return `${prefix}${formatPrice(value)}`;
}

export function formatSigned(value) {
  if (typeof value !== "number") return "--";
  // Round before choosing the sign, normalizing negative zero away.
  const digits = Math.abs(value) >= 100 ? 1 : 2;
  const rounded = Number(value.toFixed(digits)) + 0;
  return `${rounded >= 0 ? "+" : "-"}${Math.abs(rounded).toFixed(digits)}`;
}

export function formatBoardSignedChange(value, currency) {
  if (typeof value !== "number") return "--";
  if (value === 0) return "0.00";
  if (!currency || currency === "USD" || currency === "USX") return formatSigned(value);
  return `${value >= 0 ? "+" : "-"}${currencyPrefix(currency)}${formatCompactPrice(Math.abs(value))}`;
}

export function formatSignedPct(value) {
  if (typeof value !== "number") return "--";
  const rounded = Number(value.toFixed(2)) + 0;
  return `${rounded >= 0 ? "+" : ""}${rounded.toFixed(2)}%`;
}

export function formatPlainPct(value) {
  return typeof value === "number" ? `${value.toFixed(1)}%` : "--";
}

export function formatSignedNumber(value) {
  return typeof value === "number" ? `${value >= 0 ? "+" : ""}${value.toFixed(2)}` : "--";
}

export function formatCompactPrice(value) {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1000) return `${(abs / 1000).toFixed(abs >= 100_000 ? 1 : 2)}K`;
  return formatPrice(abs);
}

export function currencyPrefix(currency) {
  const code = typeof currency === "string" ? currency.trim().toUpperCase() : "";
  const known = { KRW: "₩", JPY: "¥", EUR: "€", GBP: "£", USD: "$", USX: "" };
  if (Object.prototype.hasOwnProperty.call(known, code)) return known[code];
  // Malformed provider strings must never reach innerHTML-based price renderers.
  return /^[A-Z]{3}$/.test(code) ? `${code} ` : "";
}

export function formatUsdFlow(value) {
  if (typeof value !== "number") return "--";
  const abs = Math.abs(value);
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

export function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
