# jq's smoke test, sourced by clis/smoke.sh (see there).
jqw() { wasmtime run "$OUT/component.wasm" "$@"; }
expect "$(printf '{"a":1}' | jqw .a)" 1
expect "$(jqw --version)" "jq-1.8.2"
# Regex filters need oniguruma linked in.
expect "$(jqw -n '"baaa" | test("a+")')" true
# ...and it must be the pinned [[deps]] build: when its probe fails, jq's
# configure quietly builds the copy it vendors instead.
grep -q '^ac_cv_lib_onig_onig_version=yes' "$OUT/build/config.log" \
    || { echo "::error::jq: configure didn't find the pinned oniguruma"; exit 1; }
if [ -e "$OUT/build/vendor/oniguruma" ]; then
    echo "::error::jq: built its vendored oniguruma instead of the pinned one"
    exit 1
fi
# Upstream jq truncates a fractional index (jaq rejects it).
expect "$(jqw -n '[1,2] | .[1.5]')" 2
# jq's exit statuses come through, not just 1 (as native jq 1.8.2): -e on
# false is 1, a usage error 2, a compile error 3, unparseable input 5,
# and halt_error its own.
expect_status 1 jqw -n -e false
expect_status 2 jqw --no-such-flag
expect_status 3 jqw -n '.['
expect_status 5 jqw . <<< '{'
expect_status 6 jqw -n '"x" | halt_error(6)'
