# curl's smoke test, sourced by clis/smoke.sh (see there).
net=(-S inherit-network -S allow-ip-name-lookup)
expect "$(wasmtime run "$OUT/component.wasm" --version)" "curl 8.20.0"
expect "$(wasmtime run "${net[@]}" "$OUT/component.wasm" -sSI http://example.com)" "HTTP/1.1 200"
# WASI doesn't follow symlinks, and the system bundle usually is one, so
# copy the real file in.
mkdir -p "$WORK/certs"
cp "$(readlink -f /etc/ssl/certs/ca-certificates.crt)" "$WORK/certs/ca.crt"
expect "$(wasmtime run "${net[@]}" --dir "$WORK/certs::/certs" "$OUT/component.wasm" \
    -sSI --cacert /certs/ca.crt https://example.com)" " 200"
