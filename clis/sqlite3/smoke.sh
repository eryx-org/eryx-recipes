# sqlite3's smoke test, sourced by clis/smoke.sh (see there).
expect "$(wasmtime run "$OUT/component.wasm" :memory: 'select 6*7;')" 42
