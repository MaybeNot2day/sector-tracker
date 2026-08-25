#!/usr/bin/env bash
# Regenerate the committed minified twins of the big static assets.
#
# The server (CachedStaticFiles in app/main.py) serves app.min.js in place of
# app.js ONLY when the sha256 recorded in the twin's header matches the
# current source, so forgetting to run this after editing app.js safely
# degrades to serving the unminified original. Run after any app.js or
# styles.css change:
#
#   scripts/minify-static.sh
set -euo pipefail
cd "$(dirname "$0")/.."

minify() {
  local src="$1" out="$2"
  local hash
  hash=$(shasum -a 256 "$src" | cut -d' ' -f1)
  printf '/*src=%s*/\n' "$hash" > "$out.tmp"
  npx -y esbuild "$src" --minify >> "$out.tmp"
  mv "$out.tmp" "$out"
  printf '%s: %s -> %s bytes\n' "$out" "$(wc -c < "$src" | tr -d ' ')" "$(wc -c < "$out" | tr -d ' ')"
}

minify app/static/app.js app/static/app.min.js
minify app/static/styles.css app/static/styles.min.css
node --check app/static/app.min.js
echo "OK"
