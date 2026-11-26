# go's smoke test, sourced by clis/smoke.sh (see there).
# go runs its tools through eryx:process/run, which only eryx provides,
# so builds only run inside eryx: stub it with a trap, and check what
# needs no tools, with the GOROOT and stdlib cache it ships with.
tar -xzf "$OUT/goroot.tar.gz" -C "$WORK"
gow() {
    wasmtime run -S p3 -W component-model-async -W component-model-more-async-builtins \
        -W max-wasm-stack=8388608 -W unknown-imports-trap \
        --dir "$WORK/go::/go" --dir "$WORK/gocache::/gocache" --dir "$WORK/data::/data" \
        --env GOROOT=/go --env GOCACHE=/gocache --env HOME=/data "$OUT/component.wasm" "$@" 2>&1
}
expect "$(gow version)" "go version go1.27.4 wasip3/wasm32"
expect "$(gow env GOOS GOARCH GOFLAGS | tr '\n' ' ')" "wasip3 wasm32 -mod=vendor -trimpath -p=1"
expect "$(gow list std)" "net/http"
# No telemetry: it would start an upload process.
out="$(gow env GOVERSION)"
if grep -q telemetry <<< "$out"; then echo "::error::go: telemetry output: $out"; exit 1; fi
expect "$(wasm-tools component wit "$OUT/component.wasm")" "import eryx:process/run@0.1.0;"
