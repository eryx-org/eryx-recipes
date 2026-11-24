#!/usr/bin/env bash
# Apply go-wasip3.patch to the Go fork's checkout (the cwd), for Go's own
# commands (clis/go.toml and the go-*.toml beside it). eryx-build resets the
# checkout before each build, so it always applies to a clean tree.
set -euo pipefail
dir="$(dirname "${BASH_SOURCE[0]}")"
git apply "$dir/go-wasip3.patch"
echo "go-wasip3.patch: applied"
