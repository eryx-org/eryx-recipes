#!/usr/bin/env bash
# Smoke-test a built CLI component under the wasmtime CLI, from the repo root:
#   clis/smoke.sh <name>      (after `mise run build-cli -- <name>`)
#
# This only proves the component runs. eryx's own tests run them inside eryx.
set -euo pipefail

NAME="${1:?Usage: clis/smoke.sh <name>}"
OUT="scratch/$NAME-component"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/data"
printf 'banana\napple\ncherry\n' > "$WORK/data/fruit.txt"

# Fail unless $1 (the output) contains the fixed string $2.
expect() {
    printf '%s\n' "$1"
    grep -qF -- "$2" <<< "$1" || { echo "::error::$NAME: expected '$2' in the output above"; exit 1; }
}

# Fail unless running "${@:2}" exits with status $1, showing its stderr if not.
expect_status() {
    local want="$1" status=0 err
    shift
    err="$("$@" 2>&1 > /dev/null)" || status=$?
    [ "$status" = "$want" ] || {
        printf '%s\n' "$err"
        echo "::error::$NAME: expected exit status $want from $*, got $status"
        exit 1
    }
}

# Each command's test is clis/<name>/smoke.sh (clis/gpl/<name>/ for GPL
# ones), sourced here so it has NAME, OUT, WORK and the helpers above.
script=clis/$NAME/smoke.sh
[ -f "$script" ] || script=clis/gpl/$NAME/smoke.sh
if [ ! -f "$script" ]; then
    echo "no smoke test for '$NAME'" >&2
    exit 1
fi
. "$script"
echo "$NAME: smoke test passed"
