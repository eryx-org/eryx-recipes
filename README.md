# eryx-commands

Command components for [eryx](https://github.com/eryx-org/eryx) shell sandboxes: CLI tools (ripgrep, jq, curl, gh, git, ...) built from upstream source as WASI components, and published to an OCI registry.

- **Recipes** (`clis/*.toml`): one per command. Each pins the upstream source and its patches, and is built by [eryx-build](https://github.com/eryx-org/eryx/tree/multi-guest/crates/eryx-build). GPL recipes live apart, in `clis/gpl/`. coreutils' recipe isn't kept here: eryx builds the same coreutils for its embedded commands, so `mise run fetch-coreutils` fetches `clis/coreutils.toml` and its uucore patch from eryx at `vars.coreutils_rev`, and `mise run bump-coreutils [-- BRANCH]` moves that pin (and the version) to the branch's newest change to them. A new upstream coreutils version also needs `third_party/manual.toml`'s uutils entry (`version`, `url`, `cli_commit`) and `third_party/licenses/coreutils-LICENSE` updated by hand; `mise run notices-check` fails until they are. check-clis and check-versions fail if the fetched copy isn't the pinned one.
- **Registry:** `ghcr.io/eryx-org/commands/<namespace>/<name>:<upstream>-r.<n>`. That's `eryx:<name>` for permissively licensed commands and `eryx-gpl:<name>` for GPL ones. The layout and metadata follow [wkg](https://github.com/bytecodealliance/wasm-pkg-tools)'s conventions, so `wkg` can fetch them.
- **Versions:** each published version is immutable. `<upstream>-r.<n>` is the upstream release plus our build revision, which bumps whenever the binary changes. There are no floating tags.
- **Notices:** each command ships its third-party notices (the tool, what it links, its crates or modules) and is signed with its provenance (see [Signatures and referrers](#signatures-and-referrers)).

The design and decisions are in [#1](https://github.com/eryx-org/eryx-commands/issues/1).

## Building a command

```sh
mise install                      # the pinned toolchains
mise run build-cli -- rg          # or a path: clis/gpl/git/git.toml
clis/smoke.sh rg                  # run it under the wasmtime CLI
mise run check-clis               # validate every manifest
```

After a build, `mise run notices -- OUTDIR rg` writes the command's third-party notices and its SPDX 2.3 SBOM, `<name>.spdx.json` (see `scripts/notices.py`), and `mise run notices-check` checks every published command's notice entries in `third_party/manual.toml`.

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
  - On a private repo, a required reviewer on an environment needs a paid plan, so the actor check stands in for one until the repo is public. It also checks who triggered a re-run.
  - **The gate only holds while sd2k is the only user with write access,** because anyone who can push can change the workflow.
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

## Signatures and referrers

After a real publish, the sign job (`scripts/sign.py`) attaches these to each command's manifest, by digest, as OCI referrers:

| Referrer | |
|---|---|
| a cosign signature | keyless: a Fulcio certificate for this repo's publish workflow on `main` |
| a SLSA v1 provenance attestation (`https://slsa.dev/provenance/v1`) | the workflow run, this repo's commit, and the upstream source and patches from `provenance.json` |
| `application/vnd.eryx.notices.v1+markdown` | the command's third-party notices |
| `application/spdx+json` | an SPDX 2.3 SBOM: the command, containing every package its notices list (crates with `pkg:cargo` purls, Go modules with `pkg:golang` purls, and what it links from `third_party/manual.toml`) |
| `application/vnd.eryx.source-offer.v1+json` | GPL commands only: the complete corresponding source, next to the binary. Its layers are `<name>-recipe.tar.gz` (every build input from this repo at the published commit: the recipe, patches, shims and WIT, plus `BUILD-INPUTS.json` with the tool pins such as `eryx_rev` and the WASI SDK) and the upstream sources at their pins (the tool's own tree at its commit, and each statically linked dependency's archive). Its config lists all of them with their digests. |

cosign 3 stores signatures and attestations as Sigstore bundles in referrers too; on a registry without the referrers API they're listed under the `sha256-<digest>` tag. The job skips whatever's already attached, so a re-run finishes a failed one without duplicates. It only signs a manifest (by digest) whose layer is this run's `component.wasm` and, if publish found it already there, whose revision is a commit of `main`. A version that was already in the registry with another layer (a full re-run rebuilds, and builds aren't guaranteed to be reproducible) is left unsigned and listed in the job summary; re-run only the failed jobs, which reuse the original build, or publish a new version. The dry run signs and attaches the same way, to its throwaway registry, with a key made for the run.

To verify a command:

```sh
ref=ghcr.io/eryx-org/commands/eryx/sd@sha256:...
id=https://github.com/eryx-org/eryx-commands/.github/workflows/ci.yml@refs/heads/main
issuer=https://token.actions.githubusercontent.com
cosign verify --certificate-identity "$id" --certificate-oidc-issuer "$issuer" "$ref"
cosign verify-attestation --type slsaprovenance1 \
  --certificate-identity "$id" --certificate-oidc-issuer "$issuer" "$ref"
oras discover "$ref"   # the notices, SBOM and source offer
```

The workflow's `tlog_upload` input (on by default) says whether signing records the signatures in Sigstore's public transparency log, Rekor:

- **On:** the log entry proves when the short-lived certificate signed, and anyone can see every signing made with this repo's identity. The entries are public: the certificate (naming this repo, the workflow and the commit) and each command's digest, and possibly the provenance attestation itself. Verify as above.
- **Off:** nothing goes to the log. A Sigstore timestamp authority countersigns instead, which still proves the signing time, but nothing makes a signing public, so a stolen workflow identity could sign without anyone seeing it. Verify with `--insecure-ignore-tlog` added to both cosign commands.

## Licence

The recipes, patches and tooling here are dual-licensed under MIT and Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`), like eryx. The commands themselves keep their upstream licences, which their published notices carry. GPL components are built from `clis/gpl/`, whose recipes carry their own notices.
