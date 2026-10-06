// Quote-cell flashes share one layout read per frame, not one per cell.
const pendingFlashes = new Map();
const flashTimers = new WeakMap();
let flashFrame = null;

export function flashCell(cell, delta) {
  const timer = flashTimers.get(cell);
  if (timer !== undefined) window.clearTimeout(timer);
  cell.classList.remove("flash-up", "flash-down");
  pendingFlashes.set(cell, delta > 0 ? "flash-up" : "flash-down");
  if (flashFrame === null) flashFrame = window.requestAnimationFrame(flushFlashes);
}

function flushFlashes() {
  flashFrame = null;
  const cells = Array.from(pendingFlashes).filter(([cell]) => cell.isConnected);
  pendingFlashes.clear();
  if (!cells.length) return;
  // All removals above precede this single barrier, which restarts CSS flashes
  // even when the next quote moves in the same direction as the previous one.
  void document.documentElement.offsetWidth;
  cells.forEach(([cell, className]) => {
    cell.classList.add(className);
    flashTimers.set(cell, window.setTimeout(() => {
      cell.classList.remove("flash-up", "flash-down");
      flashTimers.delete(cell);
    }, 450));
  });
}
