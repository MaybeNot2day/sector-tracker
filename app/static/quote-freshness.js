// Transport heartbeats and board-computation timestamps are not quote updates.
// Providers decide staleness (including exchange-session rules); show their
// state and actual quote timestamps without assuming an open socket is fresh.
export function quoteFreshness(payload, cached = false) {
  const quotes = (payload?.groups || []).flatMap((group) =>
    (group.assets || []).map((asset) => asset.quote)
  );
  let unavailable = 0;
  let stale = 0;
  let timestamp = null;
  quotes.forEach((quote) => {
    const stamp = Date.parse(quote?.timestamp || "");
    if (!quote || quote.error || !Number.isFinite(quote.last) || !Number.isFinite(stamp)) {
      unavailable += 1;
      return;
    }
    if (quote.is_stale) stale += 1;
    if (timestamp === null || stamp > timestamp) timestamp = stamp;
  });
  if (cached) return { state: "cached", label: "Cached quotes · refreshing", timestamp };
  if (!quotes.length || unavailable === quotes.length) {
    return { state: "unavailable", label: "Quotes unavailable", timestamp };
  }
  if (stale || unavailable) {
    return { state: "stale", label: `${stale} stale · ${unavailable} unavailable of ${quotes.length} quotes`, timestamp };
  }
  return { state: "fresh", label: "Fresh quotes", timestamp };
}
