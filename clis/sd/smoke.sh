# sd's smoke test, sourced by clis/smoke.sh (see there).
expect "$(printf 'hello world\n' | wasmtime run "$OUT/component.wasm" world eryx)" "hello eryx"
