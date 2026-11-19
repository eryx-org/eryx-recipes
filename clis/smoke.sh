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

case "$NAME" in
coreutils)
    # The precompiled cwasm needs the engine flags it was compiled with.
    cu() {
        wasmtime run --allow-precompiled -W component-model-async \
            --dir "$WORK/data::/data" --argv0 "$1" "$OUT/coreutils.cwasm" "${@:2}"
    }
    expect "$(cu sort /data/fruit.txt | head -1)" apple
    expect "$(cu wc -l /data/fruit.txt)" 3
    expect "$(cu ls /data)" fruit.txt
    ;;
rg)
    expect "$(wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -n an /data/fruit.txt)" "1:banana"
    ;;
sd)
    expect "$(printf 'hello world\n' | wasmtime run "$OUT/component.wasm" world eryx)" "hello eryx"
    ;;
jaq)
    expect "$(printf '{"a":1}' | wasmtime run "$OUT/component.wasm" .a)" 1
    expect "$(printf '[1,2,3]' | wasmtime run "$OUT/component.wasm" -c 'map(. * 2)')" "[2,4,6]"
    # In-place edit: the file changes and jaq exits 0.
    printf '{"a":1}' > "$WORK/data/edit.json"
    wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -c -i '.a = 2' /data/edit.json
    expect "$(cat "$WORK/data/edit.json")" '{"a":2}'
    ;;
jq)
    jqw() { wasmtime run "$OUT/component.wasm" "$@"; }
    expect "$(printf '{"a":1}' | jqw .a)" 1
    expect "$(jqw --version)" "jq-1.8.2"
    # Regex filters need oniguruma linked in.
    expect "$(jqw -n '"baaa" | test("a+")')" true
    # ...and it must be the pinned [[deps]] build: when its probe fails, jq's
    # configure quietly builds the copy it vendors instead.
    grep -q '^ac_cv_lib_onig_onig_version=yes' "$OUT/build/config.log" \
        || { echo "::error::jq: configure didn't find the pinned oniguruma"; exit 1; }
    if [ -e "$OUT/build/vendor/oniguruma" ]; then
        echo "::error::jq: built its vendored oniguruma instead of the pinned one"
        exit 1
    fi
    # Upstream jq truncates a fractional index (jaq rejects it).
    expect "$(jqw -n '[1,2] | .[1.5]')" 2
    ;;
sqlite3)
    expect "$(wasmtime run "$OUT/component.wasm" :memory: 'select 6*7;')" 42
    ;;
curl)
    net=(-S inherit-network -S allow-ip-name-lookup)
    expect "$(wasmtime run "$OUT/component.wasm" --version)" "curl 8.20.0"
    expect "$(wasmtime run "${net[@]}" "$OUT/component.wasm" -sSI http://example.com)" "HTTP/1.1 200"
    # WASI doesn't follow symlinks, and the system bundle usually is one, so
    # copy the real file in.
    mkdir -p "$WORK/certs"
    cp "$(readlink -f /etc/ssl/certs/ca-certificates.crt)" "$WORK/certs/ca.crt"
    expect "$(wasmtime run "${net[@]}" --dir "$WORK/certs::/certs" "$OUT/component.wasm" \
        -sSI --cacert /certs/ca.crt https://example.com)" " 200"
    ;;
gh)
    # gh resolves names itself, so it needs the real resolv.conf (WASI won't
    # follow the usual symlink) and a CA bundle under /etc.
    mkdir -p "$WORK/etc" "$WORK/home"
    cp "$(readlink -f /etc/resolv.conf)" "$WORK/etc/resolv.conf"
    cp "$(readlink -f /etc/ssl/certs/ca-certificates.crt)" "$WORK/etc/ca-certificates.crt"
    ghw() {
        wasmtime run -S p3 -W component-model-async -W component-model-more-async-builtins \
            -W max-wasm-stack=8388608 -S tcp -S udp -S inherit-network -S allow-ip-name-lookup \
            --dir "$WORK/etc::/etc" --dir "$WORK/home::/home/gh" \
            --env HOME=/home/gh --env SSL_CERT_FILE=/etc/ca-certificates.crt \
            --env GH_NO_UPDATE_NOTIFIER=1 --env TERM=dumb "$@" 2>&1
    }
    expect "$(ghw "$OUT/component.wasm" --version)" "gh version 2.87.3"
    # A placeholder token gets a real 401 from api.github.com: DNS, TCP and TLS
    # all work, and no secret is needed.
    expect "$(ghw --env GH_TOKEN=placeholder "$OUT/component.wasm" api /zen)" "HTTP 401"
    # gh's own exit statuses come through, not just 1: with no token, a command
    # that needs auth exits 4. The Go fork reports them with exit-with-code.
    status=0
    ghw "$OUT/component.wasm" issue list -R cli/cli > /dev/null || status=$?
    [ "$status" = 4 ] || { echo "::error::$NAME: expected exit status 4 with no token, got $status"; exit 1; }
    ;;
git)
    # git imports eryx:process/run and pipe, for the subcommands and remote
    # helpers it runs itself, which only eryx provides, so it can't run under
    # the wasmtime CLI. Check its shape here: those imports, and libcurl and
    # its remote helper built in. eryx-org/eryx's opt-in git tests
    # (git_stash_runs_git_itself, git_clones_fetches_and_pushes_over_http_with_a_secret,
    # git_clones_from_github_over_https) run it.
    wit="$(wasm-tools component wit "$OUT/component.wasm")"
    expect "$wit" "import eryx:process/run@0.1.0;"
    expect "$wit" "import eryx:process/pipe@0.1.0;"
    expect "$wit" "export wasi:cli/run@0.2"
    for built_in in remote-https /etc/ssl/certs/ca-certificates.crt; do
        strings "$OUT/component.wasm" | grep -qF -- "$built_in" \
            || { echo "::error::$NAME: no '$built_in' in the component"; exit 1; }
    done
    ;;
*)
    echo "no smoke test for '$NAME'" >&2
    exit 1
    ;;
esac
echo "$NAME: smoke test passed"
