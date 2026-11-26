# find's smoke test, sourced by clis/smoke.sh (see there).
# find imports eryx:process/run for -exec, which only eryx provides, so
# stub it with a trap here: -exec and -execdir only run inside eryx.
findw() { wasmtime run -W unknown-imports-trap --dir "$WORK/data::/data" "$OUT/component.wasm" "$@" 2>&1; }
mkdir -p "$WORK/data/sub"
printf 'x\n' > "$WORK/data/sub/notes.log"
: > "$WORK/data/empty.txt"
expect "$(findw /data -name '*.txt' | sort | tr '\n' ' ')" "/data/empty.txt /data/fruit.txt"
expect "$(findw /data -type d | sort | tr '\n' ' ')" "/data /data/sub"
expect "$(findw /data -maxdepth 1 -type f -empty)" "/data/empty.txt"
expect "$(findw /data -regex '.*\.log')" "/data/sub/notes.log"
out="$(findw /data -path /data/sub -prune -o -type f -print)"
expect "$out" "/data/fruit.txt"
if grep -q notes.log <<< "$out"; then echo "::error::find: -prune didn't prune"; exit 1; fi
expect "$(findw /data/sub -type f -printf '%f %s\n')" "notes.log 2"
expect "$(findw /data -name notes.log -print0 | tr '\0' '|')" "/data/sub/notes.log|"
expect "$(wasm-tools component wit "$OUT/component.wasm")" "import eryx:process/run@0.1.0;"
# -ok and -okdir would prompt, so must fail up front, not exit 0.
for action in -ok -okdir; do
    if out="$(findw /data -name fruit.txt "$action" cat {} \;)"; then
        echo "::error::find: $action succeeded"; exit 1
    fi
    expect "$out" "find can't prompt in this sandbox"
done
