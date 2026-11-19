#!/usr/bin/env bash
# Apply clis/patches/find-wasi.patch to find's source, and copy in the
# eryx:process WIT its -exec bindings are generated from. Run from the source
# dir by the eryx-build Rust lane (manifest `vendor_patch`). Idempotent.
set -euo pipefail

dir="$(dirname "${BASH_SOURCE[0]}")"
mkdir -p wit
cp "$dir/../../wit/process.wit" wit/process.wit
if git apply --reverse --check "$dir/find-wasi.patch" 2>/dev/null; then
    echo "patch-find: already applied"
else
    git apply "$dir/find-wasi.patch"
    echo "patch-find: applied find-wasi.patch"
fi
