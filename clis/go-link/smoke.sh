# go-link's smoke test, sourced by clis/smoke.sh (see there).
# go identifies its tools by their -V=full line, which must name them
# without the go- prefix they're registered under.
tool="${NAME#go-}"
toolw() {
    wasmtime run -S p3 -W component-model-async -W component-model-more-async-builtins \
        -W max-wasm-stack=8388608 --dir "$WORK/data::/data" --argv0 "$NAME" "$OUT/component.wasm" "$@" 2>&1
}
expect "$(toolw -V=full)" "$tool version go1.27.4"
