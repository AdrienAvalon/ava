#!/usr/bin/env bash
# Copy onnxruntime-web wasm artifacts from node_modules to two locations:
#   - public/onnx/   (served at /onnx/, used by ort.env.wasm.wasmPaths for *.wasm)
#   - public/assets/ (served at /assets/, where onnxruntime-web's *.mjs loader
#                     scripts are dynamically import()-ed from — relative to the
#                     bundle JS which Vite emits under /assets/)
# Both are needed: wasmPaths only redirects the .wasm fetches; the .mjs loaders
# are imported via native import() which resolves relative to the calling JS.
# Called automatically before `npm run dev` and `npm run build`.

set -euo pipefail

cd "$(dirname "$0")/.."

src=node_modules/onnxruntime-web/dist

if [[ ! -d "$src" ]]; then
  echo "[prepare-onnx-wasm] $src missing — run 'npm install' first" >&2
  exit 1
fi

for dst in public/onnx public/assets; do
  mkdir -p "$dst"
  if command -v rsync >/dev/null 2>&1; then
    rsync -aq --checksum --include='*.wasm' --include='*.mjs' --exclude='*' "$src/" "$dst/"
  else
    cp -f "$src"/*.wasm "$src"/*.mjs "$dst/"
  fi
done
echo "[prepare-onnx-wasm] $(ls public/onnx | wc -l) files in public/onnx, $(ls public/assets | wc -l) in public/assets"
