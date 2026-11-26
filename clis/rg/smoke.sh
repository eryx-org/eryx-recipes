# rg's smoke test, sourced by clis/smoke.sh (see there).
expect "$(wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -n an /data/fruit.txt)" "1:banana"
