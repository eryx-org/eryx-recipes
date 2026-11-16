# eryx-commands

Command components for [eryx](https://github.com/eryx-org/eryx) shell sandboxes: CLI tools (ripgrep, jq, curl, gh, git, ...) built from upstream source as WASI components, and published to an OCI registry.

- **Recipes** (`clis/*.toml`): one per command. Each pins the upstream source and its patches, and is built by [eryx-build](https://github.com/eryx-org/eryx/tree/multi-guest/crates/eryx-build). GPL recipes live apart, in `clis/gpl/`.
- **Registry:** `ghcr.io/eryx-org/commands/<namespace>/<name>:<upstream>-r.<n>`. That's `eryx:<name>` for permissively licensed commands and `eryx-gpl:<name>` for GPL ones. The layout and metadata follow [wkg](https://github.com/bytecodealliance/wasm-pkg-tools)'s conventions, so `wkg` can fetch them.
- **Versions:** each published version is immutable. `<upstream>-r.<n>` is the upstream release plus our build revision, which bumps whenever the binary changes. There are no floating tags.
- **Notices:** each command ships its third-party notices (the tool, what it links, its crates or modules) and is signed with its provenance.

The design and decisions are in [#1](https://github.com/eryx-org/eryx-commands/issues/1).

## Licence

The recipes, patches and tooling here are dual-licensed under MIT and Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`), like eryx. The commands themselves keep their upstream licences, which their published notices carry. GPL components are built from `clis/gpl/`, whose recipes carry their own notices.
