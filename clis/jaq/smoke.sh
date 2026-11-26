# jaq's smoke test, sourced by clis/smoke.sh (see there).
expect "$(printf '{"a":1}' | wasmtime run "$OUT/component.wasm" .a)" 1
expect "$(printf '[1,2,3]' | wasmtime run "$OUT/component.wasm" -c 'map(. * 2)')" "[2,4,6]"
# In-place edit: the file changes and jaq exits 0.
printf '{"a":1}' > "$WORK/data/edit.json"
wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -c -i '.a = 2' /data/edit.json
expect "$(cat "$WORK/data/edit.json")" '{"a":2}'
