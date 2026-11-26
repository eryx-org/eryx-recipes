# git's smoke test, sourced by clis/smoke.sh (see there).
# git imports eryx:process/run and pipe, for the subcommands and remote
# helpers it runs itself, which only eryx provides, so it can't run under
# the wasmtime CLI. Check its shape here: those imports, and libcurl and
# its remote helper built in. eryx-org/eryx's opt-in git tests
# (git_stash_runs_git_itself, git_clones_fetches_and_pushes_over_http_with_a_secret,
# git_clones_from_github_over_https) run it.
wit="$(wasm-tools component wit "$OUT/component.wasm")"
expect "$wit" "import eryx:process/run@0.1.0;"
expect "$wit" "import eryx:process/pipe@0.1.0;"
expect "$wit" "export wasi:cli/run@0.2"
for built_in in remote-https /etc/ssl/certs/ca-certificates.crt; do
    strings "$OUT/component.wasm" | grep -qF -- "$built_in" \
        || { echo "::error::$NAME: no '$built_in' in the component"; exit 1; }
done
