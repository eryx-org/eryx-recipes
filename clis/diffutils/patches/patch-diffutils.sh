#!/usr/bin/env bash
# Apply clis/diffutils/patches/diffutils-bundled-flags.patch to diffutils' source. Run
# from the source dir by the eryx-build Rust lane (manifest `vendor_patch`).
# Idempotent.
set -euo pipefail

patch="$(dirname "${BASH_SOURCE[0]}")/diffutils-bundled-flags.patch"
if git apply --reverse --check "$patch" 2>/dev/null; then
    echo "patch-diffutils: already applied"
else
    git apply "$patch"
    echo "patch-diffutils: applied diffutils-bundled-flags.patch"
fi
