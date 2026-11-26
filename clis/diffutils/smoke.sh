# diffutils's smoke test, sourced by clis/smoke.sh (see there).
# One binary: argv[0] picks diff or cmp, as the host registers both.
dw() { wasmtime run --dir "$WORK/data::/data" --argv0 "$1" "$OUT/component.wasm" "${@:2}"; }
mkdir -p "$WORK/data/a" "$WORK/data/b"
printf 'banana\napple\ncherry\n' > "$WORK/data/a/f"
printf 'banana\nAPPLE\ncherry\n' > "$WORK/data/b/f"
expect "$(dw diff -u /data/a/f /data/b/f || true)" "+APPLE"
expect_status 0 dw diff /data/fruit.txt /data/a/f
expect_status 1 dw diff /data/a/f /data/b/f
# Trouble is 2: the Rust lane passes on the ExitCode main returns
# (eryx-org/eryx#1136).
expect_status 2 dw diff /data/a/f /data/missing
# stdin as one side
expect "$(printf 'banana\n' | dw diff - /data/a/f || true)" "> apple"
# -r, and bundled options (our patch)
expect "$(dw diff -r -u /data/a /data/b || true)" "+APPLE"
expect "$(dw diff -ru /data/a /data/b || true)" "+APPLE"
expect "$(dw diff -rq /data/a /data/b || true)" "differ"
expect "$(dw diff -Naur /data/a /data/b || true)" "+APPLE"
expect "$(dw diff -U1 /data/a/f /data/b/f || true)" "@@ -1,3 +1,3 @@"
expect "$(dw cmp /data/a/f /data/b/f || true)" "differ"
expect_status 0 dw cmp /data/fruit.txt /data/a/f
