#!/usr/bin/env bash
# Patches gcx's vendored dependencies for wasip3/wasm32 compilation. Run from
# the gcx directory after `go mod vendor`. Idempotent.
#
# gcx's own wasip1 build (experimental/sandbox/build.sh) needs the same fixes,
# so this reuses its stubs from the pinned tree, retagged for wasip3, and
# narrows the same overly broad build constraints.
set -euo pipefail

V=vendor
stubs=experimental/sandbox/patches

echo "=== Adding wasip3 stub files from $stubs ==="
(cd "$stubs" && find . -name '*_wasip1.go') | while read -r f; do
    out="$V/${f%_wasip1.go}_wasip3.go"
    sed 's|^//go:build wasip1$|//go:build wasip3|; s|on wasip1|on wasip3|' "$stubs/$f" > "$out"
    echo "$out"
done

# Not in gcx's set: wasip1 gets these from upstream's own constraints.
cat > "$V/github.com/mattn/go-isatty/isatty_wasip3.go" << 'GO'
//go:build wasip3

package isatty

func IsTerminal(fd uintptr) bool       { return false }
func IsCygwinTerminal(fd uintptr) bool { return false }
GO
cat > "$V/github.com/sirupsen/logrus/terminal_check_wasip3.go" << 'GO'
//go:build wasip3

package logrus

import "io"

func isTerminal(fd int) bool           { return false }
func checkIfTerminal(w io.Writer) bool { return false }
GO
sed -i '1s|^//go:build \(.*\)$|//go:build (\1) \&\& !wasip3|' \
    "$V/github.com/sirupsen/logrus/terminal_check_notappengine.go"

echo "=== Narrowing build constraints ==="
# Unix implementations that don't work on wasip3, and flock's unsupported
# fallback, which the stub replaces. Same list as gcx's build.sh.
for f in \
    github.com/gofrs/flock/flock_others.go \
    github.com/moby/term/term_unix.go \
    github.com/moby/term/termios_unix.go \
    github.com/moby/term/termios_nonbsd.go \
    github.com/prometheus/prometheus/tsdb/fileutil/mmap_unix.go; do
    sed -i -E 's|^//go:build (.*)$|//go:build (\1) \&\& !wasip3|; /^\/\/ \+build /d' "$V/$f"
done

echo "=== Leaving out what gcx's wasip1 build leaves out ==="
# `gcx dev` (OPA linter, dashboard SDK), otel-checker and glamour. Without
# this the module has too many functions for wasm32's 32-bit code addresses
# and fails to link. Not httputils: its wasip1 transport is a custom host
# import, and wasip3's net/http works as is. The stubs' _wasip1 suffix is
# itself a GOOS constraint, so they're copied to _wasip3. These change gcx's
# own sources, which stay in the checkout between builds, so the constraint
# edit only applies once.
for base in cmd/gcx/root/dev cmd/gcx/instrumentation/local internal/style/glamour; do
    sed 's|^//go:build wasip1$|//go:build wasip3|' "${base}_wasip1.go" > "${base}_wasip3.go"
    sed -i '1s|^//go:build !wasip1$|//go:build !wasip1 \&\& !wasip3|' "$base.go"
done


echo "=== Telemetry off by default ==="
# gcx's usage telemetry is opt-out: it posts an event unless GCX_TELEMETRY,
# DO_NOT_TRACK or its config turns it off. In eryx a sandbox shouldn't phone
# home unasked, and reaching the receiver needs a network rule for it anyway,
# so here it's opt-in: GCX_TELEMETRY=enabled still turns it on. Fails if the
# default moves, so a new version can't skip this.
f=internal/telemetry/telemetry.go
if ! grep -qx 'const defaultMode = ModeDisabled' "$f"; then
    if [ "$(grep -cx 'const defaultMode = ModeEnabled' "$f")" != 1 ]; then
        echo "$f: no single 'const defaultMode = ModeEnabled' to change" >&2
        exit 1
    fi
    sed -i 's|^const defaultMode = ModeEnabled$|const defaultMode = ModeDisabled|' "$f"
fi
