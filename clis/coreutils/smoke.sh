# coreutils's smoke test, sourced by clis/smoke.sh (see there).
# The precompiled cwasm needs the engine flags it was compiled with.
cu() {
    wasmtime run --allow-precompiled -W component-model-async \
        --dir "$WORK/data::/data" --argv0 "$1" "$OUT/coreutils.cwasm" "${@:2}"
}
expect "$(cu sort /data/fruit.txt | head -1)" apple
expect "$(cu wc -l /data/fruit.txt)" 3
expect "$(cu ls /data)" fruit.txt
# Character devices read like files (eryx-org/eryx#1102). Preopen the
# host's /dev for this one read.
status=0
wasmtime run --allow-precompiled -W component-model-async --dir /dev::/dev \
    --argv0 cat "$OUT/coreutils.cwasm" /dev/null || status=$?
[ "$status" = 0 ] || { echo "::error::coreutils: cat /dev/null exited $status"; exit 1; }
