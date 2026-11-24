#!/usr/bin/env bash
# Write the GOROOT that the `go` command (clis/go.toml) needs at runtime, as
# goroot.tar.gz in its component dir:
#
#   go/         the GOROOT: the patched fork's standard library source
#               (src/, without cmd/, tests or testdata), pkg/include for the
#               assembler, empty stubs for pkg/tool/wasip3_wasm32/{compile,asm,
#               link} (go checks they exist, then runs go-compile and the rest
#               through eryx:process/run), VERSION, and go.env
#   gocache/    a GOCACHE holding the whole standard library, prebuilt
#
# The cache is built natively, with the fork's toolchain cross-compiling to
# wasip3/wasm32 with -trimpath. go accepts it as its own: for a release, a
# tool's identity in the cache key is its version line ("compile version
# go1.27.4"), which the wasm tools print too, and -trimpath (in go.env's
# GOFLAGS) keeps the GOROOT's path out of the keys. Mount go/ anywhere and
# point GOROOT and GOCACHE at the two.
#
# Run from the repo root after `mise run build-cli -- go`, which leaves the
# patched checkout in scratch/go-src:
#   scripts/go-root.sh [component-dir]   (default scratch/go-component)
set -euo pipefail

OUT="$(realpath "${1:-scratch/go-component}")"
SRC="$(realpath scratch/go-src)"
GOROOT_TOOLCHAIN="$(realpath "${ERYX_GO_WASIP3_ROOT:-scratch/go-wasip3}")"
GO="$GOROOT_TOOLCHAIN/bin/go"
[ -x "$GO" ] || { echo "go-root: no Go toolchain at $GOROOT_TOOLCHAIN (scripts/bootstrap-go-wasip3.sh)" >&2; exit 1; }
# The checkout must be the one go was just built from: patched, at the fork's
# commit, which is the toolchain's.
[ "$(git -C "$SRC" rev-parse HEAD)" = "$(git -C "$GOROOT_TOOLCHAIN" rev-parse HEAD)" ] ||
    { echo "go-root: $SRC isn't at the toolchain's commit; build go first" >&2; exit 1; }
PATCH="$(realpath "$(dirname "${BASH_SOURCE[0]}")/../clis/patches/go-wasip3.patch")"
git -C "$SRC" apply --check -R "$PATCH" 2>/dev/null ||
    { echo "go-root: $SRC isn't patched with go-wasip3.patch; build go first" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ROOT="$WORK/go"
mkdir -p "$ROOT/pkg/tool/wasip3_wasm32"
mkdir -p "$ROOT/src"
tar -C "$SRC/src" --exclude=./cmd --exclude=testdata --exclude='*_test.go' -cf - . | tar -C "$ROOT/src" -xf -
cp -RL "$GOROOT_TOOLCHAIN/pkg/include" "$ROOT/pkg/"
touch "$ROOT/pkg/tool/wasip3_wasm32/"{compile,asm,link}
cp "$SRC/VERSION" "$ROOT/"
# The toolchain's go.env (with its GOWASM), and the sandbox's defaults.
cp "$GOROOT_TOOLCHAIN/go.env" "$ROOT/go.env"
cat >> "$ROOT/go.env" <<'ENV'

# Added by eryx-commands scripts/go-root.sh for eryx sandboxes: no module
# downloads or toolchain switching, vendored modules, and one package at a
# time (each tool runs as a child command). -trimpath matches the prebuilt
# GOCACHE's keys.
GOTOOLCHAIN=local
GOPROXY=off
GOFLAGS=-mod=vendor -trimpath -p=1
ENV

# Built from the checkout, which the Go lane gave the toolchain's native
# tools: its standard library sources are the ones just copied, which is what
# the cache's keys hash.
echo "go-root: building the standard library cache" >&2
(cd "$SRC/src" && GOROOT="$SRC" GOCACHE="$WORK/gocache" GOOS=wasip3 GOARCH=wasm32 \
    GOFLAGS=-trimpath GOTOOLCHAIN=local "$GO" build std)
# The cache's own housekeeping, which differs run to run.
rm -f "$WORK/gocache/trim.txt"

mkdir -p "$OUT"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$WORK" -cf - go gocache |
    gzip -n -9 > "$OUT/goroot.tar.gz"
echo "go-root: wrote $OUT/goroot.tar.gz ($(du -h "$OUT/goroot.tar.gz" | cut -f1))" >&2
