#!/usr/bin/env bash
# Apply clis/patches/find-wasi.patch to find's source. Run from the source dir by
# the eryx-build Rust lane (manifest `vendor_patch`). Idempotent.
set -euo pipefail

patch="$(dirname "${BASH_SOURCE[0]}")/find-wasi.patch"
if git apply --reverse --check "$patch" 2>/dev/null; then
    echo "patch-find: already applied"
else
    git apply "$patch"
    echo "patch-find: applied find-wasi.patch"
fi
