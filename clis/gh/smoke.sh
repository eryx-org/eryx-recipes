# gh's smoke test, sourced by clis/smoke.sh (see there).
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
