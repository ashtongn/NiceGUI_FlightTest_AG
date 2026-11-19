#!/usr/bin/env bash
# Build the ctypes helpers in native/ (rxdrain.c into lib/librxdrain.so).
#
#   bash native/build.sh            writes native/lib/librxdrain.so
#   bash native/build.sh OUTDIR     writes OUTDIR/librxdrain.so instead
#
# The library must match the radio's architecture, so on the radio build it
# inside the dpmg image (it has gcc), from the code folder:
#   docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/app_svc -w /app_svc \
#       dpmg:latest bash native/build.sh
# skiq_bridge.load_rxdrain looks in native/lib/ first, then the repo root.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$HERE/lib}"
CC="${CC:-gcc}"
mkdir -p "$OUT"
"$CC" -O2 -shared -fPIC -o "$OUT/librxdrain.so" "$HERE/rxdrain.c"
echo "built $OUT/librxdrain.so"
