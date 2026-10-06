#!/usr/bin/env bash
# Regenerate committed static twins and content-address every ES-module import.
# CachedStaticFiles serves a twin only when its src header matches the source.
# Unmatched twins safely fall back to source, with the same immutable-cache URL.
# Run after changes to JavaScript helpers, app.js, or styles.css:
#
#   scripts/minify-static.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Helpers must not keep an immutable unversioned URL after a deploy. Pin
# imports first, then pin the entrypoint and stylesheet to their final bytes.
node --input-type=module <<'JS'
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
const digest = (path) => createHash("sha256").update(readFileSync(path)).digest("hex").slice(0, 12);
const appPath = "app/static/app.js";
let source = readFileSync(appPath, "utf8");
for (const name of ["formatting", "animations", "quote-freshness"]) {
  const specifier = new RegExp(`\\./${name}\\.js(?:\\?v=[a-f0-9]+)?`, "g");
  source = source.replace(specifier, `./${name}.js?v=${digest(`app/static/${name}.js`)}`);
}
writeFileSync(appPath, source);
const indexPath = "app/static/index.html";
let index = readFileSync(indexPath, "utf8");
for (const name of ["app.js", "styles.css"]) {
  index = index.replace(
    new RegExp(`/static/${name.replace(".", "\\.")}\\?v=[^"]+`, "g"),
    `/static/${name}?v=${digest(`app/static/${name}`)}`,
  );
}
writeFileSync(indexPath, index);
JS

minify() {
  local src="$1" out="$2"
  local hash
  hash=$(shasum -a 256 "$src" | cut -d' ' -f1)
  printf '/*src=%s*/\n' "$hash" > "$out.tmp"
  if [[ "$src" == *.js ]]; then
    npx -y esbuild@0.25.9 "$src" --minify --format=esm >> "$out.tmp"
  else
    npx -y esbuild@0.25.9 "$src" --minify >> "$out.tmp"
  fi
  mv "$out.tmp" "$out"
  printf '%s: %s -> %s bytes\n' "$out" "$(wc -c < "$src" | tr -d ' ')" "$(wc -c < "$out" | tr -d ' ')"
}

minify app/static/app.js app/static/app.min.js
minify app/static/formatting.js app/static/formatting.min.js
minify app/static/animations.js app/static/animations.min.js
minify app/static/quote-freshness.js app/static/quote-freshness.min.js
minify app/static/styles.css app/static/styles.min.css
for module in app/static/{app,formatting,animations,quote-freshness}.min.js; do
  node --input-type=module --check < "$module"
done
echo "OK"
