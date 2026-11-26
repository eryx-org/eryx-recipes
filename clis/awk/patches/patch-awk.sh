#!/usr/bin/env bash
# Prepare onetrue-awk's source for the C lane's single-shot build. Run from the
# awk source dir by eryx-build (manifest `vendor_patch`). Idempotent. It does
# what awk's makefile does before compiling, without running anything built
# from the source natively:
#   - bison generates the parser (awkgram.tab.c, awkgram.tab.h);
#   - maketab, built for wasm and run under the wasmtime CLI, generates
#     proctab.c from awkgram.tab.h;
#   - our <sys/wait.h> (clis/awk/shims/sys), which wasi-libc lacks, goes in
#     eryx-include/ for the build's -I.
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f awkgram.y ] || { echo "patch-awk: awkgram.y not found (wrong dir?)" >&2; exit 1; }
: "${WASI_SDK_PATH:?patch-awk: set WASI_SDK_PATH (mise run ensure-wasi-sdk)}"
command -v bison >/dev/null || { echo "patch-awk: needs bison" >&2; exit 1; }

bison -d awkgram.y

"$WASI_SDK_PATH/bin/clang" --target=wasm32-wasip2 -O2 maketab.c -o maketab.wasm
wasmtime run --dir . maketab.wasm awkgram.tab.h > proctab.c.new
grep -q 'printname' proctab.c.new || { echo "patch-awk: maketab wrote no proctab.c" >&2; exit 1; }
mv proctab.c.new proctab.c
rm -f maketab.wasm

mkdir -p eryx-include/sys
cp "$dir/../shims/sys/wait.h" eryx-include/sys/wait.h
echo "patch-awk: generated awkgram.tab.c and proctab.c"
