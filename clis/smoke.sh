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
    # Character devices read like files (eryx-org/eryx#1102). Preopen the
    # host's /dev for this one read.
    status=0
    wasmtime run --allow-precompiled -W component-model-async --dir /dev::/dev \
        --argv0 cat "$OUT/coreutils.cwasm" /dev/null || status=$?
    [ "$status" = 0 ] || { echo "::error::coreutils: cat /dev/null exited $status"; exit 1; }
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
    # jq's exit statuses come through, not just 1 (as native jq 1.8.2): -e on
    # false is 1, a usage error 2, a compile error 3, unparseable input 5,
    # and halt_error its own.
    expect_status 1 jqw -n -e false
    expect_status 2 jqw --no-such-flag
    expect_status 3 jqw -n '.['
    expect_status 5 jqw . <<< '{'
    expect_status 6 jqw -n '"x" | halt_error(6)'
    ;;
sed)
    sedw() { wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" "$@"; }
    expect "$(printf 'hello world\n' | sedw 's/world/eryx/')" "hello eryx"
    expect "$(printf 'x1y22\n' | sedw -E 's/([0-9]+)/<\1>/g')" "x<1>y<22>"
    expect "$(printf 'a\nb\nc\n' | sedw -n '$!N;s/\n/-/;p')" "a-b"
    expect "$(printf '1\n2\n3\n' | sedw -n 'H;${x;s/\n/,/g;p}')" ",1,2,3"
    expect "$(printf 'a\nb\na\n' | sedw '0,/a/s//X/' | tr '\n' ' ')" "X b a"
    # In place, with a backup.
    sedw -i.bak 's/apple/APPLE/' /data/fruit.txt
    expect "$(cat "$WORK/data/fruit.txt")" "APPLE"
    expect "$(cat "$WORK/data/fruit.txt.bak")" "apple"
    # `e` can't run a shell here: it must fail with our message, not panic.
    if out="$(echo hi | sedw 'e echo x' 2>&1)"; then
        echo "::error::sed: 'e' succeeded"; exit 1
    fi
    expect "$out" "can't run shell commands in this sandbox"
    ;;
awk)
    awkw() { wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" "$@"; }
    expect "$(printf 'a b c\nd e f\n' | awkw '{print $2}' | tr '\n' ' ')" "b e"
    expect "$(printf '1\n2\n3\n' | awkw '{s+=$1} END{print s, NR}')" "6 3"
    expect "$(printf 'a:b\n' | awkw -F: -v x=42 '{print $2, NF, x}')" "b 2 42"
    expect "$(printf 'a\nb\na\n' | awkw '{c[$1]++} END{print c["a"], c["b"]}')" "2 1"
    expect "$(printf 'foo bar\n' | awkw '{gsub(/o/,"0"); n=split($0,p," "); print p[1], n}')" "f00 2"
    expect "$(awkw 'BEGIN{printf "%-3s|%05.1f\n", "x", 2.5}')" "x  |002.5"
    expect "$(awkw 'function f(n){return n<=1?1:n*f(n-1)} BEGIN{print f(10)}')" 3628800
    expect "$(awkw '{n++} END{print n}' /data/fruit.txt)" 3
    # `exit` longjmps out of the program (the sjlj build), and its status
    # comes through as is (eryx-build's exit-code shim, eryx-org/eryx#1106).
    expect "$(printf 'a\nb\n' | awkw '{print; exit} END{print "end"}' | tr '\n' ' ')" "a end"
    expect_status 1 awkw 'BEGIN{exit 1}'
    expect_status 3 awkw 'BEGIN{exit 3}'
    # Pipes and system() need a shell: they fail, saying why.
    expect "$(awkw 'BEGIN{r = system("true"); print "r=" r}' 2>&1)" "r=-1"
    expect "$(awkw 'BEGIN{r = system("true")}' 2>&1)" "system() isn't supported in this sandbox"
    # `cmd | getline` stops awk: failing softly would return -1 (true), so
    # `while (cmd | getline x)` would never end.
    if out="$(awkw 'BEGIN{"echo hi" | getline x; print "got:" x}' 2>&1)"; then
        echo "::error::awk: cmd | getline succeeded"; exit 1
    fi
    expect "$out" "a pipe from a command isn't supported in this sandbox"
    status=0
    out="$(timeout 5 wasmtime run "$OUT/component.wasm" 'BEGIN{while ("ls" | getline f) n++}' 2>&1)" || status=$?
    if [ "$status" -eq 0 ] || [ "$status" -eq 124 ] || [ "$(wc -l <<< "$out")" -gt 5 ]; then
        printf '%s\n' "$out" | head -5
        echo "::error::awk: while (cmd | getline) succeeded, timed out or flooded stderr (exit $status)"; exit 1
    fi
    expect "$out" "isn't supported in this sandbox"
    if out="$(awkw 'BEGIN{print "a" | "sort"}' 2>&1)"; then
        echo "::error::awk: print | cmd succeeded"; exit 1
    fi
    expect "$out" "a pipe to a command isn't supported in this sandbox"
    ;;
diffutils)
    # One binary: argv[0] picks diff or cmp, as the host registers both.
    dw() { wasmtime run --dir "$WORK/data::/data" --argv0 "$1" "$OUT/component.wasm" "${@:2}"; }
    mkdir -p "$WORK/data/a" "$WORK/data/b"
    printf 'banana\napple\ncherry\n' > "$WORK/data/a/f"
    printf 'banana\nAPPLE\ncherry\n' > "$WORK/data/b/f"
    expect "$(dw diff -u /data/a/f /data/b/f || true)" "+APPLE"
    expect_status 0 dw diff /data/fruit.txt /data/a/f
    expect_status 1 dw diff /data/a/f /data/b/f
    # Trouble is 2 natively, but the Rust lane reports any failure as 1: it
    # doesn't have the C lane's exit-with-code shim yet.
    expect_status 1 dw diff /data/a/f /data/missing
    # stdin as one side
    expect "$(printf 'banana\n' | dw diff - /data/a/f || true)" "> apple"
    # -r, and bundled options (our patch)
    expect "$(dw diff -r -u /data/a /data/b || true)" "+APPLE"
    expect "$(dw diff -ru /data/a /data/b || true)" "+APPLE"
    expect "$(dw diff -rq /data/a /data/b || true)" "differ"
    expect "$(dw diff -Naur /data/a /data/b || true)" "+APPLE"
    expect "$(dw diff -U1 /data/a/f /data/b/f || true)" "@@ -1,3 +1,3 @@"
    expect "$(dw cmp /data/a/f /data/b/f || true)" "differ"
    expect_status 0 dw cmp /data/fruit.txt /data/a/f
    ;;
find)
    # find imports eryx:process/run for -exec, which only eryx provides, so
    # stub it with a trap here: -exec and -execdir only run inside eryx.
    findw() { wasmtime run -W unknown-imports-trap --dir "$WORK/data::/data" "$OUT/component.wasm" "$@" 2>&1; }
    mkdir -p "$WORK/data/sub"
    printf 'x\n' > "$WORK/data/sub/notes.log"
    : > "$WORK/data/empty.txt"
    expect "$(findw /data -name '*.txt' | sort | tr '\n' ' ')" "/data/empty.txt /data/fruit.txt"
    expect "$(findw /data -type d | sort | tr '\n' ' ')" "/data /data/sub"
    expect "$(findw /data -maxdepth 1 -type f -empty)" "/data/empty.txt"
    expect "$(findw /data -regex '.*\.log')" "/data/sub/notes.log"
    out="$(findw /data -path /data/sub -prune -o -type f -print)"
    expect "$out" "/data/fruit.txt"
    if grep -q notes.log <<< "$out"; then echo "::error::find: -prune didn't prune"; exit 1; fi
    expect "$(findw /data/sub -type f -printf '%f %s\n')" "notes.log 2"
    expect "$(findw /data -name notes.log -print0 | tr '\0' '|')" "/data/sub/notes.log|"
    expect "$(wasm-tools component wit "$OUT/component.wasm")" "import eryx:process/run@0.1.0;"
    # -ok and -okdir would prompt, so must fail up front, not exit 0.
    for action in -ok -okdir; do
        if out="$(findw /data -name fruit.txt "$action" cat {} \;)"; then
            echo "::error::find: $action succeeded"; exit 1
        fi
        expect "$out" "find can't prompt in this sandbox"
    done
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
