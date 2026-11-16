# eryx-commands

Command components for [eryx](https://github.com/eryx-org/eryx) shell sandboxes: CLI tools (ripgrep, jq, curl, gh, git, ...) built from upstream source as WASI components, and published to an OCI registry.

- **Recipes** (`clis/*.toml`): one per command. Each pins the upstream source and its patches, and is built by [eryx-build](https://github.com/eryx-org/eryx/tree/multi-guest/crates/eryx-build). GPL recipes live apart, in `clis/gpl/`.
- **Registry:** `ghcr.io/eryx-org/commands/<namespace>/<name>:<upstream>-r.<n>`. That's `eryx:<name>` for permissively licensed commands and `eryx-gpl:<name>` for GPL ones. The layout and metadata follow [wkg](https://github.com/bytecodealliance/wasm-pkg-tools)'s conventions, so `wkg` can fetch them.
- **Versions:** each published version is immutable. `<upstream>-r.<n>` is the upstream release plus our build revision, which bumps whenever the binary changes. There are no floating tags.
- **Notices:** each command ships its third-party notices (the tool, what it links, its crates or modules) and is signed with its provenance.

The design and decisions are in [#1](https://github.com/eryx-org/eryx-commands/issues/1).

## Building a command

```sh
mise install                      # the pinned toolchains
mise run build-cli -- rg          # or a path: clis/gpl/git/git.toml
clis/smoke.sh rg                  # run it under the wasmtime CLI
mise run check-clis               # validate every manifest
```

After a build, `mise run notices -- OUTDIR rg` writes the command's third-party notices (see `scripts/notices.py`), and `mise run notices-check` checks every published command's notice entries in `third_party/manual.toml`.

`build-cli` installs eryx-build from eryx-org/eryx at `mise.toml`'s `eryx_rev` into `scratch/tools` the first time. C recipes need the WASI SDK (`mise run ensure-wasi-sdk`), and gh needs the wasip3 Go fork (`scripts/bootstrap-go-wasip3.sh`). Sources and outputs go in the git-ignored `scratch/`.

## Publishing

Each command's version is in `commands.toml`: `<upstream>-r.<n>`, keyed by its name (`[eryx.rg]` is `eryx:rg`).

- **Bumping:** a PR that changes what a command is built from must bump its `r.<n>`, and a new upstream version starts at `r.1`. What it's built from is:
  - its manifest and everything in its own directory;
  - the patches, shims and WIT it names, and the files its patch scripts apply;
  - its lane's pins: eryx-build, Rust, the WASI SDK, wit-bindgen or the Go fork.

  `python3 scripts/publish.py check-versions --base origin/main` checks this, and CI runs it on every PR. A version may never be lower than one `commands.lock` already has. Versions are `MAJOR.MINOR.PATCH-r.<n>`, as wkg needs semver.
- **Dry run:** every CI run publishes what it built to a throwaway registry on the runner. It reads each push back, checks the layer against `component.wasm`, and uploads the would-be `commands.lock`.
- **Real publish:** Ben runs the CI workflow by hand on `main` with `publish` set; the job only runs for that actor. After the dry run, it pushes every built command whose version isn't in the registry yet to `ghcr.io/eryx-org/commands/<namespace>/<name>:<version>`. Then it opens a PR that adds them to `commands.lock`.
  - On a private repo, a required reviewer on an environment needs a paid plan, so the actor check stands in for one until the repo is public.
- **Versions are never replaced, by our own rules.** ghcr doesn't stop a tag being pushed again. publish.py checks each tag first and never pushes one that exists, and real publishes run one at a time (a concurrency group that's never cancelled), so nothing races that check.
  - A version that's already there is listed in the results with the registry's digests, so `lock` and signing pick it up. A re-run after a failure part way records and signs everything.
  - `--fail-existing`, which the dry run uses, makes an existing version an error.
  - A tag check counts as "not there" only on a 404, or a 401/403 whose OCI error says the name is unknown. Anything else fails the publish.

`scripts/publish.py publish` pushes with `wkg oci push`, which writes the Wasm OCI layout wkg reads:
- an `application/vnd.wasm.config.v0+json` config with the component's imports and exports;
- one `application/wasm` layer.

It annotates the manifest from the build's `provenance.json`:

| Annotation | |
|---|---|
| `org.opencontainers.image.title` | `<namespace>:<name>` |
| `org.opencontainers.image.version` | the version |
| `org.opencontainers.image.description` | from the manifest |
| `org.opencontainers.image.source`, `.revision` | this repo and commit |
| `org.opencontainers.image.created` | the publish time |
| `org.opencontainers.image.licenses` | the SPDX licences of the tool and the libraries it statically links (its `[[deps]]`), joined with `AND`. Its crates' and Go modules' licences are only in its notices for now. |
| `run.eryx.command.upstream` | `<repo>@<commit>`, or `<url>#sha256:<digest>` for an archive |
| `run.eryx.command.eryx-build` | the eryx-build version and the eryx commit it was installed from (`0.15.1+<eryx_rev>`) |
| `run.eryx.command.wit` | the WIT packages it imports, with versions (`wasi:cli@0.2.12,...`) |
| `run.eryx.command.names` | for a multicall command, every name it answers to (`names` in `commands.toml`) |

`commands.lock`, in wkg.lock's format, lists every published version with its layer digest. eryx's own `commands.lock` copies its pins from it.

To fetch a command with wkg, map the namespaces in wkg's `config.toml`:

```toml
[namespace_registries]
eryx = { registry = "eryx", metadata = { preferredProtocol = "oci", oci = { registry = "ghcr.io", namespacePrefix = "eryx-org/commands/" } } }
eryx-gpl = { registry = "eryx", metadata = { preferredProtocol = "oci", oci = { registry = "ghcr.io", namespacePrefix = "eryx-org/commands/" } } }
```

Then run `wkg get eryx:sd@1.1.0-r.1`. The packages are private for now, so wkg needs credentials, such as `docker login ghcr.io`.

## Licence

The recipes, patches and tooling here are dual-licensed under MIT and Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`), like eryx. The commands themselves keep their upstream licences, which their published notices carry. GPL components are built from `clis/gpl/`, whose recipes carry their own notices.
