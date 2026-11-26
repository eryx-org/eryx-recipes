# gcx's smoke test, sourced by clis/smoke.sh (see there).
# Like gh: the real resolv.conf and a CA bundle under /etc.
mkdir -p "$WORK/etc" "$WORK/home"
cp "$(readlink -f /etc/resolv.conf)" "$WORK/etc/resolv.conf"
cp "$(readlink -f /etc/ssl/certs/ca-certificates.crt)" "$WORK/etc/ca-certificates.crt"
gcxw() {
    timeout 120 wasmtime run -S p3 -W component-model-async -W component-model-more-async-builtins \
        -W max-wasm-stack=8388608 -S tcp -S udp -S inherit-network -S allow-ip-name-lookup \
        --dir "$WORK/etc::/etc" --dir "$WORK/home::/home/gcx" \
        --env HOME=/home/gcx --env SSL_CERT_FILE=/etc/ca-certificates.crt \
        --env TERM=dumb "$@" 2>&1
}
expect "$(gcxw --env GCX_TELEMETRY=off "$OUT/component.wasm" --version)" "gcx version 1.9.1"
# An unauthenticated request to a public Grafana: config loading, DNS, TCP
# and TLS all work, and no secret is needed.
expect "$(gcxw --env GCX_TELEMETRY=off --env GRAFANA_SERVER=https://play.grafana.org \
    "$OUT/component.wasm" api /api/health)" '"database": "ok"'
# With telemetry on, gcx posts a usage event before it exits. That and
# reading its config both hung while os/signal's receiver kept the
# scheduler from polling (scripts/bootstrap-go-wasip3.sh). It's off by
# default here (patches/patch-gcx-vendor.sh), so turn it on to check that.
expect "$(gcxw --env GCX_TELEMETRY=enabled "$OUT/component.wasm" config view)" "current-context"
