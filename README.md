# eryx-recipes

Recipes for building third-party software for [eryx](https://github.com/eryx-org/eryx), from upstream source:

- **Commands** (`clis/`, the rest of this README): CLI tools for eryx shell sandboxes (ripgrep, jq, curl, gh, git, ...) as WASI components, published to an OCI registry.
- **Wheels** (`wheels/`, see [its README](wheels/README.md)): Python packages with native code (numpy, pandas, pydantic-core, ...) as WASI wheels, published to a pip index at `https://wheels.eryx.run`.

Both build against eryx at `mise.toml`'s `eryx_rev`.

## Using them

**Commands** are Wasm components at `ghcr.io/eryx-org/commands/<namespace>/<name>:<version>` (namespace `eryx`, or `eryx-gpl` for GPL ones), and `eryx.run` is a [wkg](https://github.com/bytecodealliance/wasm-pkg-tools) registry for them. [`commands.lock`](commands.lock) lists every published version and its digest.

```sh
wkg get --registry eryx.run eryx:rg@15.1.0-r.4 -o ./rg.wasm   # or without @<version>, the newest
wkg oci pull ghcr.io/eryx-org/commands/eryx/rg:15.1.0-r.4 -o rg.wasm

# Signed by this repo's publish workflow (eryx-commands, its old name, before November 2026)
cosign verify ghcr.io/eryx-org/commands/eryx/rg:15.1.0-r.4 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/eryx-org/eryx-(commands|recipes)/\.github/workflows/ci\.yml@refs/heads/main$'
```

eryx's book covers [running them in a sandbox](https://docs.eryx.run/latest/book/guide/precompile.html#commands-from-a-registry). Each command's notices, SBOM and (for GPL ones) source are attached to it as OCI referrers (`oras discover`); see [Signatures and referrers](#signatures-and-referrers).

**Wheels** are on a pip index whose path is the ABI they're built for:

```sh
pip download --dest wheels --only-binary :all: \
    --python-version 3.14 --implementation cp --abi cp314 \
    --platform wasi_0_0_0_wasm32 --platform any \
    --extra-index-url https://wheels.eryx.run/cp314-wasip2-sdk33/simple/ \
    numpy
```

[`wheels/README.md`](wheels/README.md#using-the-wheels-elsewhere) says what they need, and eryx's book covers [using them, with pip or uv](https://docs.eryx.run/latest/book/guide/packages.html).

To ask for a command or package we don't build, [open an issue](https://github.com/eryx-org/eryx-recipes/issues) (for a command, titled `Command: <name>`: see [Requesting a command](#requesting-a-command)).

## Commands

- **Recipes** (`clis/<name>/`): one directory per command (see [below](#a-commands-directory)). Its manifest pins the upstream source and its patches, and is built by [eryx-build](https://github.com/eryx-org/eryx/tree/multi-guest/crates/eryx-build). GPL commands live apart, in `clis/gpl/<name>/`, which is what puts them in the `eryx-gpl` namespace.
- **Registry:** `ghcr.io/eryx-org/commands/<namespace>/<name>:<upstream>-r.<n>`. That's `eryx:<name>` for permissively licensed commands and `eryx-gpl:<name>` for GPL ones. The layout and metadata follow [wkg](https://github.com/bytecodealliance/wasm-pkg-tools)'s conventions, so `wkg` can fetch them.
- **Versions:** each published version is immutable. `<upstream>-r.<n>` is the upstream release plus our build revision, which bumps whenever the binary changes. There are no floating tags.
- **Notices:** each command ships its third-party notices (the tool, what it links, its crates or modules) and is signed with its provenance (see [Signatures and referrers](#signatures-and-referrers)).

The design and decisions are in [#1](https://github.com/eryx-org/eryx-recipes/issues/1).

### A command's directory

Everything that's one command's is in `clis/<name>/` (`clis/gpl/<name>/` for a GPL one). To add a command, create that directory with the files below: `<name>.toml`, `publish.toml` (a `version` ending `-r.1`), `notices.toml` and the licence texts it names, `smoke.sh`, and its patches and shims if it has any. Nothing outside it changes: CI finds it (`scripts/publish.py matrix`) and builds it in its manifest's lane. Before pushing, `mise run check-clis`, `mise run notices-check`, and `python3 scripts/publish.py check-versions --base origin/main` check it.

| File | |
|---|---|
| `<name>.toml` | the eryx-build manifest. Paths in it are relative to the repo root (`clis/<name>/patches/...`). |
| `publish.toml` | its published version, and how it's published (see [Publishing](#publishing)) |
| `patches/` | its patches, and the scripts that apply them (a manifest's `vendor_patch`, or a dep's `config_patch`) |
| `shims/` | C sources and headers its build adds (`shims`, `-include`) |
| `licenses/` | the licence and notice texts its notice entries name, copied from the source at its pin |
| `notices.toml` | its third-party notice entries: the tool, and each library it statically links (its `[[deps]]`), in [`third_party/manual.toml`](third_party/manual.toml)'s form, without `artifacts` (they're this command's) |
| `smoke.sh` | its smoke test, which `clis/smoke.sh <name>` sources after setting `NAME`, `OUT` (its component dir), `WORK` (a scratch dir with `data/fruit.txt`) and the helpers `expect` and `expect_status` |

Everything there but `publish.toml`, `notices.toml`, `smoke.sh` and `licenses/` is a build input. What isn't any one command's stays outside:

- `clis/shared/` has the patches and shims several commands build with: curl's patch and Mbed TLS's, which curl and git's libcurl both use.
- The Go commands go-compile, go-asm and go-link build from the same patched fork as go, with go's patches (`clis/go/patches/`).
- `third_party/manual.toml` has the notice entries for what a lane's toolchain links (`lanes`), and `third_party/licenses/` the licence texts several commands' entries name.
- `wit/` has the eryx WIT that commands calling `eryx:process` are built against.
- coreutils' manifest isn't kept here: eryx builds the same coreutils for its embedded commands, so `mise run fetch-coreutils` fetches it and its uucore patch from eryx at `vars.coreutils_rev` to where eryx keeps them (`clis/coreutils.toml`, `clis/patches/patch-uucore-wasip2.sh`, both git-ignored), and `clis/coreutils/publish.toml` names it. `mise run bump-coreutils [-- BRANCH]` moves that pin (and the version) to the branch's newest change to them. A new upstream coreutils version also needs `clis/coreutils/notices.toml`'s uutils entry (`version`, `url`, `cli_commit`) and its licence file updated by hand; `mise run notices-check` fails until they are. check-clis and check-versions fail if the fetched copy isn't the pinned one.

## Building a command

```sh
mise install                      # the pinned toolchains
mise run build-cli -- rg          # or a manifest's path: clis/gpl/git/git.toml
clis/smoke.sh rg                  # run it under the wasmtime CLI
mise run check-clis               # validate every manifest
mise run list-clis                # every command and its manifest
```

eryx-build itself takes a name to mean `clis/<name>.toml`, so `build-cli` passes it the manifest's path. After a build, `mise run notices -- OUTDIR rg` writes the command's third-party notices and its SPDX 2.3 SBOM, `<name>.spdx.json` (see `scripts/notices.py`), and `mise run notices-check` checks every published command's notice entries (its `notices.toml`, and `third_party/manual.toml`'s for its lane).

`build-cli` installs eryx-build from eryx-org/eryx at `mise.toml`'s `eryx_rev` into `scratch/tools` the first time. C recipes need the WASI SDK (`mise run ensure-wasi-sdk`), and gh needs the wasip3 Go fork (`scripts/bootstrap-go-wasip3.sh`). Sources and outputs go in the git-ignored `scratch/`.

Go itself is four commands, built from the fork's own tree (eryx-build's `std_tool`) with `clis/go/patches/go-wasip3.patch`:

- `go` runs `go-compile`, `go-asm` and `go-link` through `eryx:process/run`, so it only builds inside eryx, registered with `allow_spawn`. Its child commands share eryx's child memory pool, which defaults to 256 MiB; linking gh needed more than 512 MiB (1 GiB worked).
- After `mise run build-cli -- go`, `scripts/go-root.sh` writes `goroot.tar.gz` into its component dir. That's the GOROOT go needs (`go/`: the fork's standard library source and `go.env`) and a prebuilt standard library cache (`gocache/`). Mount them, and set `GOROOT` and `GOCACHE` to them.
- Modules must be vendored: go.env sets `GOPROXY=off`.
- Don't set `GOFLAGS` in the sandbox. It replaces go.env's `-mod=vendor -trimpath -p=1` rather than adding to it, and without `-trimpath` the prebuilt cache never matches, so every build recompiles the standard library.
- The archive isn't published or uploaded yet, because eryx can't mount it yet. CI builds it to smoke-test go, and caches it with go's component.

### Building a command in the sandbox

To build or test a recipe you haven't reviewed (an agent's, say), run eryx-build and the smoke test in a throwaway Docker container instead, with `scripts/sandbox.py` (wheels' `build.py --sandbox` uses it too):

```sh
dir=$(mise run build-cli -- sd --sandbox)   # prints /tmp/eryx-sandbox-.../sd-component
clis/smoke.sh sd --sandbox --scratch "$(dirname "$dir")"
mise run build-cli -- sd --sandbox --scratch /tmp/build-1   # a new directory of your choosing
```

- The build writes only its scratch directory, a new one (`--scratch`, by default a new one in `/tmp`), mounted read-write at the checkout's `scratch/`. That's where its manifest's `source.dir` and `output.dir` are, so the component is left in `<scratch>/<name>-component`, outside the checkout. Every build starts from nothing (no fetched sources or crates), so it takes as long as CI's.
- The checkout is mounted read-only at its own path, so everything in it is readable to the build: keep nothing secret in it (a `.env`) when building recipes you haven't reviewed. The toolchains are mounted read-only from where this checkout's pins put them, never from paths in the arguments: eryx-build (`scratch/tools`), the pinned Rust toolchain (its rustup toolchain dir, with `wasm32-wasip2`), wasm-tools, wasmtime and wit-bindgen (mise), the WASI SDK, and the wasip3 Go fork in `scratch/go-wasip3`, each that's installed. Cargo's home is in the scratch directory, mounted where the host's `CARGO_HOME` is, so the paths the build records (crates' panic locations) match a build outside it: sd, jq and go-asm come out byte for byte the same.
- Nothing else of the host's is mounted, not the Docker socket, and none of its environment is passed in, only what the build needs. The network is on. Root installs the image's packages (`ubuntu:26.04`, with what CI's runners give the C lane), and then the build and the smoke test run as your uid. Afterwards, every symlink the build left in the scratch directory is deleted, as something on the host could otherwise read where it points.
- `smoke` mounts the scratch directory read-only, with wasmtime and wasm-tools.
- It takes a command's name, not a manifest's path. go's `goroot.tar.gz` (`scripts/go-root.sh`) isn't written in the sandbox, so go's smoke test can't run there yet.

With `RECIPES_REQUIRE_SANDBOX` set, as the agents run, `build-cli` and `clis/smoke.sh` refuse to run without `--sandbox`, a `--scratch` to build in must be a new directory directly in `/tmp`, and one to smoke-test must be a directory directly in `/tmp`, not a link. wheels/build.py's build and verify follow the same rules ([wheels/README.md](wheels/README.md#requests)). The Sandbox workflow (`.github/workflows/sandbox.yml`) builds sd, jq and go-asm and a wheel that way when the sandbox changes, and weekly, and checks the commands against a build outside it.

## Requesting a command

1. Open an issue titled exactly `Command: <name>`, the name it'll be published under (lowercase letters, digits and `-`), saying what the tool is and where its source is.
2. A maintainer reads it and labels it `build-command`, which runs `.github/workflows/commands-agent.yml`. It stops unless the title is exactly that and neither `clis/<name>/` nor `clis/gpl/<name>/` exists.
3. Claude writes `clis/<name>/` (or `clis/gpl/<name>/`, for a GPL-family licence), builds and smoke-tests it in the sandbox, and a draft PR titled `feat(commands): add <name>` is opened with it. If it can't be done, a comment on the issue says why. If the run fails, a comment links to it.
4. Review the PR like any other: the recipe, its patches, its notices, and the licence and namespace it chose, which its description states for you to confirm. A GPL command also needs its licence and notice copied into its component the way ci.yml's step does for git, which only covers git so far.
5. The Actions bot opened it, so its CI waits for a maintainer's **Approve and run**. Approve only once you've read the recipe, since CI builds it; that gives it its required checks. Then merge, and publish as usual.
6. In eryx-run, once it's published: pin it in `shell-commands.lock` with `scripts/shell-commands-bump.py ghcr.io/eryx-org/commands/<namespace>/<name>`, decide in `src/execution/commands.rs` whether it gets the network or may spawn, and add its line to `the_shells_pinned_commands_all_run`.

Anyone can open an issue, and the agent reads the issue's body and the upstream source, which anyone can publish, so both are treated as hostile. The label, which only maintainers can add, is the vouch. The run reads the issue as it was when labelled.

- The agent's job has a read-only token, which it never sees (it runs the `claude` CLI, pinned with integrity hashes by `.github/claude/package-lock.json`, not claude-code-action), and no `id-token`. The issue's body reaches it as a file in `/tmp`, never in its prompt or a shell command, and the prompt says it's untrusted. Its tools are reading the checkout and `/tmp` (not `/proc`, so not its own environment), editing only `clis/<name>/` and `clis/gpl/<name>/` (the name parsed from the title), fetching from GitHub, crates.io, docs.rs and Go's module sites, `scripts/sandbox.py build-cli <name> --scratch ...` and `smoke <name> --scratch ...`, and `scripts/publish.py check-manifests` and `check-versions`, which only parse TOML (eryx-build's `--check` reads the manifests and checks the files they name exist).
- `RECIPES_REQUIRE_SANDBOX` makes the build and smoke test run only in the sandbox ([above](#building-a-command-in-the-sandbox)), in a new directory directly in `/tmp`, with none of the job's environment, nothing of the host's mounted but the checkout and toolchains (read-only) and that directory, and no Docker socket or capabilities. So the source's code can't reach the scripts, the agent's files or its key.
- A second job, with no agent and nothing of the recipe's running, takes only `clis/<name>/` or `clis/gpl/<name>/` (exactly one; regular files, at most 2 MB, nothing like an Anthropic key, and a `publish.toml` at `-r.1`), drops its `PR.md`, pushes it to a branch and opens the PR with `GITHUB_TOKEN`, or comments the agent's `CANNOT.md` on the issue.
- Nothing it writes is published until the PR is reviewed and merged, and its CI doesn't run until a maintainer approves it.

## Publishing

Each command's version is in its `publish.toml`: `<upstream>-r.<n>`. Its name is its directory's, and its namespace `eryx`, or `eryx-gpl` under `clis/gpl/`, so `clis/rg/publish.toml` is `eryx:rg`'s, published as `ghcr.io/eryx-org/commands/eryx/rg:<version>`. It has:

- `version`: the upstream version, then `-r.<n>`, our build revision. Bump `n` for any change to the built component at the same upstream version (below). Go back to `r.1` when the upstream version changes. Published versions are never overwritten.
- `upstream` (optional): the upstream version, when it can't be read from the manifest's `source.ref` once a leading `v` or `<name>-` is dropped and `_` becomes `.`.
- `manifest` (optional): the recipe, if it isn't `<name>.toml` beside it (coreutils' fetched one).
- `names` (optional): every name a multicall command answers to, recorded in its `run.eryx.command.names` annotation.
- `multicall` (optional): `true` takes `names` from the manifest's cargo `--features` list instead, one tool per feature.
- `fetched` (optional): the recipe isn't kept here but fetched from eryx-org/eryx at the commit in this mise.toml var (`mise run fetch-<name>`; `mise run bump-<name>` moves it). Moving the pin counts as a change to the command's build inputs.
- `pins` (optional): mise.toml pins its build uses that its lane doesn't, as `<table>.<key>` (`["vars.wasi_sdk_version"]` for a Rust command that compiles C, which also has CI's Rust lane install the WASI SDK for it). They count as build inputs, like the lane's.
- `files` (optional): repo files its build uses that its recipe doesn't name, such as a script writing something beside the component. They count as build inputs too.

The rules:

- **Bumping:** a PR that changes what a command is built from must bump its `r.<n>`, and a new upstream version starts at `r.1`. What it's built from is:
  - its manifest and everything in its directory but `publish.toml`, `notices.toml`, `smoke.sh` and `licenses/`;
  - the patches, shims and WIT it names (in `clis/shared/` or another command's directory too), and the files its patch scripts apply;
  - its lane's pins: eryx-build, Rust, the WASI SDK, wit-bindgen or the Go fork; wasmtime, if it ships a cwasm; and any `pins` and `files` its `publish.toml` lists.

  `python3 scripts/publish.py check-versions --base origin/main` checks this, and CI runs it on every PR. Moving a file isn't a change, nor is changing a manifest or script only to name moved files where they now are (or a manifest's comments): it compares each with its base version with the moved paths rewritten. A version may never be lower than one `commands.lock` already has. Versions are `MAJOR.MINOR.PATCH-r.<n>`, as wkg needs semver.
- **CI builds:** on pushes and PRs, CI builds the commands whose directory or build inputs changed, and every command when what they all share does (mise.toml, the workflow and its actions, `clis/smoke.sh`, `scripts/publish.py`, or what the notices are made from); `python3 scripts/publish.py matrix --base REF` prints what it would build. The weekly run and manual runs build every command.
- **CI cache:** on pushes and PRs, CI restores a command's built component dir instead of rebuilding it when nothing it's made from changed: the inputs above, plus its `publish.toml` and its notices' inputs (`python3 scripts/publish.py cache-key NAME` prints the hash). It still smoke-tests it. The weekly run and manual runs, so every real publish, build everything from source.
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
| `run.eryx.command.names` | for a multicall command, every name it answers to (`names` in its `publish.toml`) |

`commands.lock`, in wkg.lock's format, lists every published version with its layer digest. eryx's own `commands.lock` copies its pins from it.

To fetch a command with wkg, map the namespaces in wkg's `config.toml`:

```toml
[namespace_registries]
eryx = { registry = "eryx", metadata = { preferredProtocol = "oci", oci = { registry = "ghcr.io", namespacePrefix = "eryx-org/commands/" } } }
eryx-gpl = { registry = "eryx", metadata = { preferredProtocol = "oci", oci = { registry = "ghcr.io", namespacePrefix = "eryx-org/commands/" } } }
```

Then run `wkg get eryx:sd@1.1.0-r.1`. The packages are public, so wkg needs no credentials.

## Signatures and referrers

After a real publish, the sign job (`scripts/sign.py`) attaches these to each command's manifest, by digest, as OCI referrers:

| Referrer | |
|---|---|
| a cosign signature | keyless: a Fulcio certificate for this repo's publish workflow on `main` |
| a SLSA v1 provenance attestation (`https://slsa.dev/provenance/v1`) | the workflow run, this repo's commit, and the upstream source and patches from `provenance.json` |
| `application/vnd.eryx.notices.v1+markdown` | the command's third-party notices |
| `application/spdx+json` | an SPDX 2.3 SBOM: the command (its published version, with `component.wasm`'s SHA-256), containing every package its notices list (crates with `pkg:cargo` purls, Go modules with `pkg:golang` purls, and what it links, from its notice entries) |
| `application/vnd.eryx.source-offer.v1+json` | GPL commands only: the complete corresponding source, next to the binary. Its layers are `<name>-recipe.tar.gz` (every build input from this repo at the published commit: the recipe, patches, shims and WIT, plus `BUILD-INPUTS.json` with the tool pins such as `eryx_rev` and the WASI SDK) and the upstream sources at their pins (the tool's own tree at its commit, and each statically linked dependency's archive). Its config lists all of them with their digests. |

cosign 3 stores signatures and attestations as Sigstore bundles in referrers too; on a registry without the referrers API they're listed under the `sha256-<digest>` tag. The job skips whatever's already attached, so a re-run finishes a failed one without duplicates. It only signs a manifest (by digest) whose layer is this run's `component.wasm` and, if publish found it already there, whose revision is a commit of `main`. A version that was already in the registry with another layer (a full re-run rebuilds, and builds aren't guaranteed to be reproducible) is left unsigned and listed in the job summary; re-run only the failed jobs, which reuse the original build, or run the workflow on `main` with `sign_run_id` set to the publishing run's ID: that run builds and publishes nothing, and signs the earlier run's versions from its artifacts (they're kept 14 days, `published` 90). The dry run signs and attaches the same way, to its throwaway registry, with a key made for the run.

To verify a command:

```sh
ref=ghcr.io/eryx-org/commands/eryx/sd@sha256:...
# This repo was eryx-commands until 2026-11-25, and what was published
# before then is signed under that name
id='^https://github\.com/eryx-org/eryx-(commands|recipes)/\.github/workflows/ci\.yml@refs/heads/main$'
issuer=https://token.actions.githubusercontent.com
cosign verify --certificate-identity-regexp "$id" --certificate-oidc-issuer "$issuer" "$ref"
cosign verify-attestation --type slsaprovenance1 \
  --certificate-identity-regexp "$id" --certificate-oidc-issuer "$issuer" "$ref"
oras discover "$ref"   # the notices, SBOM and source offer
```

The workflow's `tlog_upload` input (on by default) says whether signing records the signatures in Sigstore's public transparency log, Rekor:

- **On:** the log entry proves when the short-lived certificate signed, and anyone can see every signing made with this repo's identity. The entries are public: the certificate (naming this repo, the workflow and the commit) and each command's digest, and possibly the provenance attestation itself. Verify as above.
- **Off:** nothing goes to the log. A Sigstore timestamp authority countersigns instead, which still proves the signing time, but nothing makes a signing public, so a stolen workflow identity could sign without anyone seeing it. Verify with `--insecure-ignore-tlog` added to both cosign commands.

## Licence

The recipes, patches and tooling here are dual-licensed under MIT and Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`), like eryx. The commands themselves keep their upstream licences, which their published notices carry. GPL components are built from `clis/gpl/`, whose recipes carry their own notices.
