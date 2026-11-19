#!/usr/bin/env bash
# Apply clis/patches/sed-wasi.patch to sed's source. Run from the source dir by
# the eryx-build Rust lane (manifest `vendor_patch`). Idempotent.
set -euo pipefail

patch="$(dirname "${BASH_SOURCE[0]}")/sed-wasi.patch"
if git apply --reverse --check "$patch" 2>/dev/null; then
    echo "patch-sed: already applied"
else
    git apply "$patch"
    echo "patch-sed: applied sed-wasi.patch"
fi
