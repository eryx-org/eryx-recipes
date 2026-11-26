# go-compile's smoke test, sourced by clis/smoke.sh (see there).
# go identifies its tools by their -V=full line, which must name them
# without the go- prefix they're registered under.
tool="${NAME#go-}"
toolw() {
    wasmtime run -S p3 -W component-model-async -W component-model-more-async-builtins \
        -W max-wasm-stack=8388608 --dir "$WORK/data::/data" --argv0 "$NAME" "$OUT/component.wasm" "$@" 2>&1
}
expect "$(toolw -V=full)" "$tool version go1.27.4"
printf 'package main\n\nfunc main() { println("hi") }\n' > "$WORK/data/main.go"
toolw -p main -o /data/main.a -pack /data/main.go
[ -s "$WORK/data/main.a" ] || { echo "::error::go-compile: no /data/main.a"; exit 1; }
