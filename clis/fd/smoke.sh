# fd's smoke test, sourced by clis/smoke.sh (see there).
fdw() { wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" "$@" 2>&1; }
mkdir -p "$WORK/data/sub/inner" "$WORK/data/ign"
printf 'x\n' > "$WORK/data/sub/notes.log"
: > "$WORK/data/empty.txt"
: > "$WORK/data/sub/inner/sub.txt"
: > "$WORK/data/ign/keep.me"
: > "$WORK/data/ign/skip.me"
printf 'skip.me\n' > "$WORK/data/ign/.ignore"

# Globs and regexes, with the walk's results sorted as upstream sorts them.
expect "$(fdw -g '*.txt' /data | tr '\n' ' ')" "/data/empty.txt /data/fruit.txt /data/sub/inner/sub.txt "
expect "$(fdw '^notes\.log$' /data)" "/data/sub/notes.log"
expect "$(fdw -e log . /data)" "/data/sub/notes.log"
expect "$(fdw -t d . /data | tr '\n' ' ')" "/data/ign/ /data/sub/ /data/sub/inner/ "
expect "$(fdw -t e -t f . /data/sub /data/ign)" "/data/sub/inner/sub.txt"
expect "$(fdw --max-depth 1 -t f . /data | tr '\n' ' ')" "/data/empty.txt /data/fruit.txt "
# Searches the working directory by default, printing paths relative to it.
expect "$(wasmtime run --env PWD=/data --dir "$WORK/data::/data" "$OUT/component.wasm" -g 'fr*' 2>&1)" "fruit.txt"

# --prune and --exclude (the patch's own skipping, as Walk can't skip).
out="$(fdw --prune sub /data)"
expect "$out" "/data/sub/"
if grep -q inner <<< "$out"; then echo "::error::fd: --prune didn't prune"; exit 1; fi
out="$(fdw -E sub -t f . /data)"
expect "$out" "/data/fruit.txt"
if grep -q notes.log <<< "$out"; then echo "::error::fd: --exclude didn't exclude"; exit 1; fi

# .ignore files are honoured, and hidden files skipped, unless told not to.
out="$(fdw . /data/ign)"
expect "$out" "/data/ign/keep.me"
if grep -q skip.me <<< "$out"; then echo "::error::fd: .ignore wasn't honoured"; exit 1; fi
expect "$(fdw -I -H . /data/ign | tr '\n' ' ')" "/data/ign/.ignore /data/ign/keep.me /data/ign/skip.me "

# Exit statuses: --quiet says whether anything matched.
expect_status 0 wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -q fruit /data
expect_status 1 wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" -q nothing-matches /data

# WASI can't run commands, so --exec, --exec-batch and --list-details fail.
for action in -x -X; do
    expect_status 1 wasmtime run --dir "$WORK/data::/data" "$OUT/component.wasm" fruit /data "$action" cat
    expect "$(fdw fruit /data "$action" cat || true)" "operation not supported on this platform"
done
expect "$(fdw -l fruit /data || true)" "'fd --list-details' is not supported on this platform."
