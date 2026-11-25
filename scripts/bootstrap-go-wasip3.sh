#!/usr/bin/env bash
# Bootstrap the pinned wasip3 Go toolchain (our controlled fork of
# jellevandenhooff/go, ported to WASI 0.3.0 as implemented by the wasmtime
# version eryx uses). Clones + builds it from source, then prints the
# resulting GOROOT on the last line so callers can `export ERYX_GO_WASIP3_ROOT`.
#
# Building from source takes a few minutes; CI caches the output keyed on this
# script's hash, which covers GO_WASIP3_COMMIT and GO_WASIP3_GOWASM.
#
# The fork's VERSION is a release string (go1.27.4), so Go's build cache
# doesn't notice when the commit changes: after a bump, build with a fresh
# GOCACHE (or `go clean -cache`) to be sure the new compiler is used.
#
# Usage:
#   GOROOT_BOOTSTRAP=$(go env GOROOT) scripts/bootstrap-go-wasip3.sh [dest-dir]
set -euo pipefail

# --- Pinned toolchain (bump these together when moving the fork) ---
# Moving the fork also means: each Go manifest's [build.go_toolchain] (while it
# differs from eryx-build's default), the std_tool manifests' [source] ref and
# commit, and the Go entries in third_party/manual.toml.
GO_WASIP3_REPO="https://github.com/sd2k/go.git"
GO_WASIP3_REF="wasip3-0.3.0-go1.27.4-branches"
GO_WASIP3_COMMIT="f2006e361e755199dce61ff823137c8711d20ea9"
# Wasm features the compiler may use, written to the toolchain's go.env so
# every build with this GOROOT (eryx-build, notices.py) gets them.
# widearith (wide-arithmetic: 128-bit add/sub, 64x64->128 multiply) speeds up
# math/bits and the crypto behind TLS; wasmtime enables it by default.
GO_WASIP3_GOWASM="widearith"

DEST="${1:-scratch/go-wasip3}"

log() { echo "[bootstrap-go-wasip3] $*" >&2; }

# Reset go.env to the fork's and append our defaults. go.env is tracked, so
# this also keeps the checkout below from tripping over a local change.
write_go_env() {
    git -C "$DEST" checkout --quiet -- go.env
    printf '\n# Added by eryx-commands scripts/bootstrap-go-wasip3.sh.\nGOWASM=%s\n' \
        "$GO_WASIP3_GOWASM" >> "$DEST/go.env"
}

if [ -x "$DEST/bin/go" ] && [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null)" = "$GO_WASIP3_COMMIT" ]; then
    log "toolchain already present at $DEST @ $GO_WASIP3_COMMIT"
    write_go_env
    readlink -f "$DEST"
    exit 0
fi

if [ ! -d "$DEST/.git" ]; then
    log "cloning $GO_WASIP3_REPO ($GO_WASIP3_REF) into $DEST"
    git clone --branch "$GO_WASIP3_REF" "$GO_WASIP3_REPO" "$DEST"
fi
git -C "$DEST" fetch origin "$GO_WASIP3_REF"
git -C "$DEST" checkout --quiet -- go.env
git -C "$DEST" checkout --quiet "$GO_WASIP3_COMMIT"

log "building the toolchain (make.bash)…"
# make.bash logs to stdout; redirect to stderr so this script's stdout is
# only the final GOROOT path (callers capture it).
( cd "$DEST/src" && GOTOOLCHAIN=local bash make.bash ) >&2

if [ ! -x "$DEST/bin/go" ]; then
    log "ERROR: build did not produce $DEST/bin/go"
    exit 1
fi
write_go_env
log "done (GOWASM=$GO_WASIP3_GOWASM)"
readlink -f "$DEST"
