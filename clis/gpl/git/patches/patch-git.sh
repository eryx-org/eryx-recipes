#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Patch the git source tree for the wasip2 C lane: apply git-wasi.patch (the
# run-command.c backend over eryx:process/run, and two small WASI fixes),
# then git-https.patch on top (live pipes, start_async and remote-curl, for
# HTTP(S)). Run from the git source dir by the eryx-build C lane (manifest
# `vendor_patch`). Idempotent. GPL-2.0-only, like git; see ../NOTICE.md.
set -euo pipefail

dir="$(dirname "${BASH_SOURCE[0]}")"
# git-https.patch applies on top of git-wasi.patch, so it only reverses
# cleanly once both are in, and git-wasi.patch only on its own: check each,
# and apply whatever is missing (a scratch/git from before git-https.patch
# has just the first).
if git apply --reverse --check "$dir/git-https.patch" 2>/dev/null; then
    echo "patch-git: already patched"
    exit 0
fi
if ! git apply --reverse --check "$dir/git-wasi.patch" 2>/dev/null; then
    git apply "$dir/git-wasi.patch"
fi
git apply "$dir/git-https.patch"
