#!/usr/bin/env bash
# Copy onnxruntime-web wasm artifacts from node_modules into public/onnx/
# so they are served same-origin (avoids depending on jsDelivr from the CSP).
# Called automatically before `npm run dev` and `npm run build`.

set -euo pipefail

cd "$(dirname "$0")/.."

src=node_modules/onnxruntime-web/dist
dst=public/onnx

if [[ ! -d "$src" ]]; then
  echo "[prepare-onnx-wasm] $src missing — run 'npm install' first" >&2
  exit 1
fi

mkdir -p "$dst"
# Copy all .wasm + .mjs (loader scripts pick the right variant at runtime).
# Use rsync -c (checksum) to keep the dst small and skip already-copied files.
if command -v rsync >/dev/null 2>&1; then
  rsync -aq --checksum --include='*.wasm' --include='*.mjs' --exclude='*' "$src/" "$dst/"
else
  cp -f "$src"/*.wasm "$src"/*.mjs "$dst/"
fi
echo "[prepare-onnx-wasm] $(ls "$dst" | wc -l) files in $dst"
