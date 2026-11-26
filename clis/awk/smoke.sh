# awk's smoke test, sourced by clis/smoke.sh (see there).
awkw() { wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" "$@"; }
expect "$(printf 'a b c\nd e f\n' | awkw '{print $2}' | tr '\n' ' ')" "b e"
expect "$(printf '1\n2\n3\n' | awkw '{s+=$1} END{print s, NR}')" "6 3"
expect "$(printf 'a:b\n' | awkw -F: -v x=42 '{print $2, NF, x}')" "b 2 42"
expect "$(printf 'a\nb\na\n' | awkw '{c[$1]++} END{print c["a"], c["b"]}')" "2 1"
expect "$(printf 'foo bar\n' | awkw '{gsub(/o/,"0"); n=split($0,p," "); print p[1], n}')" "f00 2"
expect "$(awkw 'BEGIN{printf "%-3s|%05.1f\n", "x", 2.5}')" "x  |002.5"
expect "$(awkw 'function f(n){return n<=1?1:n*f(n-1)} BEGIN{print f(10)}')" 3628800
expect "$(awkw '{n++} END{print n}' /data/fruit.txt)" 3
# `exit` longjmps out of the program (the sjlj build), and its status
# comes through as is (eryx-build's exit-code shim, eryx-org/eryx#1106).
expect "$(printf 'a\nb\n' | awkw '{print; exit} END{print "end"}' | tr '\n' ' ')" "a end"
expect_status 1 awkw 'BEGIN{exit 1}'
expect_status 3 awkw 'BEGIN{exit 3}'
# Pipes and system() need a shell: they fail, saying why.
expect "$(awkw 'BEGIN{r = system("true"); print "r=" r}' 2>&1)" "r=-1"
expect "$(awkw 'BEGIN{r = system("true")}' 2>&1)" "system() isn't supported in this sandbox"
# `cmd | getline` stops awk: failing softly would return -1 (true), so
# `while (cmd | getline x)` would never end.
if out="$(awkw 'BEGIN{"echo hi" | getline x; print "got:" x}' 2>&1)"; then
    echo "::error::awk: cmd | getline succeeded"; exit 1
fi
expect "$out" "a pipe from a command isn't supported in this sandbox"
status=0
out="$(timeout 5 wasmtime run "$OUT/component.wasm" 'BEGIN{while ("ls" | getline f) n++}' 2>&1)" || status=$?
if [ "$status" -eq 0 ] || [ "$status" -eq 124 ] || [ "$(wc -l <<< "$out")" -gt 5 ]; then
    printf '%s\n' "$out" | head -5
    echo "::error::awk: while (cmd | getline) succeeded, timed out or flooded stderr (exit $status)"; exit 1
fi
expect "$out" "isn't supported in this sandbox"
if out="$(awkw 'BEGIN{print "a" | "sort"}' 2>&1)"; then
    echo "::error::awk: print | cmd succeeded"; exit 1
fi
expect "$out" "a pipe to a command isn't supported in this sandbox"
