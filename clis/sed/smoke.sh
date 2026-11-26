# sed's smoke test, sourced by clis/smoke.sh (see there).
sedw() { wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" "$@"; }
expect "$(printf 'hello world\n' | sedw 's/world/eryx/')" "hello eryx"
expect "$(printf 'x1y22\n' | sedw -E 's/([0-9]+)/<\1>/g')" "x<1>y<22>"
expect "$(printf 'a\nb\nc\n' | sedw -n '$!N;s/\n/-/;p')" "a-b"
expect "$(printf '1\n2\n3\n' | sedw -n 'H;${x;s/\n/,/g;p}')" ",1,2,3"
expect "$(printf 'a\nb\na\n' | sedw '0,/a/s//X/' | tr '\n' ' ')" "X b a"
# In place, with a backup.
sedw -i.bak 's/apple/APPLE/' /data/fruit.txt
expect "$(cat "$WORK/data/fruit.txt")" "APPLE"
expect "$(cat "$WORK/data/fruit.txt.bak")" "apple"
# `e` can't run a shell here: it must fail with our message, not panic.
if out="$(echo hi | sedw 'e echo x' 2>&1)"; then
    echo "::error::sed: 'e' succeeded"; exit 1
fi
expect "$out" "can't run shell commands in this sandbox"
