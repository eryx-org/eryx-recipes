# Wheels

PyPI has no WebAssembly builds of packages with native code (C, C++, Rust),
so eryx can't install them from there. This directory has a recipe for each
one we build for eryx's WASI, against eryx at `mise.toml`'s `eryx_rev` (the
same pin as the commands). CI (`.github/workflows/wheels.yml`) builds the
recipes a PR changes and tests them in eryx, and on `main` publishes them to
`https://wheels.eryx.run`. eryx-run's builders install them in place of
PyPI's wheels: a requirement on `markupsafe`, or on a package that needs it,
gets our build, with resolution pinned to the version we built (eryx-run's
`src/builder/native.rs`). These moved here from eryx-run's `native/`, with
their history; `eryx-org/eryx-run#N` in old commits are its PRs.

## A recipe

`wheels/<package>/` holds:

- `recipe.toml`, with:
  - `name` and `version`: the PyPI package and the version built. Use a
    version that has CPython 3.14 Linux wheels on PyPI, as environments
    resolve requirements for Linux, then swap in our build.
  - `sdist` and `sha256`: its source distribution on PyPI, from
    `https://pypi.org/pypi/<package>/<version>/json` (`urls`, where
    `packagetype` is `sdist`).
  - `requires`: what the build needs installed (e.g. `setuptools`,
    `meson-python`, `maturin`).
  - `imports`: modules to import in eryx once it's built. Include the
    extension modules, so one that doesn't load fails the build.
  - `test_requires`: pure-Python packages it needs to import or test,
    pinned (e.g. `["typing-extensions==4.16.0"]`), installed beside it for
    `verify` only (environments get them from PyPI, as its dependencies).
  - `native_requires`: other recipes it compiles against or needs to import
    (e.g. pandas' `["numpy"]`). `build` builds them first, into
    `OUT/deps/`, and unpacks them into `$NATIVE_DEPS` for its `build.sh`;
    `verify` installs them beside it. Environments get our builds of them
    anyway, as they're recipes too.
  - `[env]`: extra environment variables for the build.
- `test.py`: Python run in eryx with the wheel installed. Use `assert`s
  that only pass with the native code (not a pure-Python fallback).
- `build.sh` (optional): how to build, when `pip wheel` isn't it. It runs in
  the unpacked sdist, with the cross-compiling environment below and the
  build venv on `PATH`, and must leave one wheel in `$WHEEL_DIR`.
- `*.patch` (optional): applied to the sdist with `patch -p1`, in order.

`markupsafe` is the simplest: C, built by setuptools.

## How it builds

`wheels/build.py build` downloads the sdist, checks its hash, and builds it
in a Python 3.14 venv, cross-compiling for `wasm32-wasip2`:

- `CC`, `CXX`, `LDSHARED`, `AR` are the wasi-sdk's (`$WASI_SDK_PATH`), the
  version eryx uses (its `mise.toml`'s `wasi_sdk_version`).
- `CFLAGS`/`CXXFLAGS` add `-fPIC` and the CPython headers; `LDFLAGS` links
  against eryx's own `libpython3.14.so`, which resolves the C API.
- `_PYTHON_SYSCONFIGDATA_NAME`, `_PYTHON_HOST_PLATFORM` and `PYTHONPATH`
  make `sysconfig` describe WASI, so extensions get WASI suffixes.
- Whatever platform tag the build backend gives the wheel, `build` retags
  it `wasi_0_0_0_wasm32` (`wheel tags`): what CPython's
  `sysconfig.get_platform()` reports on WASI, and what maturin and
  wasi-wheels use. There's no PEP for WASI tags yet.
- `$SYSROOT` is the sysroot (`include/`, `lib/`, `sysconfig/`).
- `$NATIVE_DEPS` has the recipe's `native_requires`, unpacked.

Builds are reproducible: the same recipe, sdist and toolchain give the same
wheel, byte for byte, so a rebuild publishes the file that's already listed
and hashes locked against the index stay valid. Each recipe builds in
`/tmp/eryx-native/<package>`, not a random directory, as backends record
paths under it (numpy's `__config__.py` keeps the compiler's and the venv's),
and reaches the sysroot and wasi-sdk through `/tmp/eryx-native/sysroot` and
`/tmp/eryx-native/wasi-sdk` (numpy records the sysroot in its compiler
flags), so a build on another machine gives the same wheel too. And
`SOURCE_DATE_EPOCH` is the sdist's newest file's time, so the files a
build generates aren't stamped with the time it ran. A recipe whose output
still varies needs that fixed in its `build.sh` or a patch.

The sysroot comes from `wheels/sysroot.sh`: CPython's headers and
sysconfig, configured as componentize-py builds eryx's libpython. The
wheel must contain a `.so`; WASI has no threads, processes, signals or
`mmap` beyond eryx's emulation, so code that needs them has to be patched or
configured out.

## Locally

```sh
mise run fetch-eryx                      # eryx's mise.toml and libpython, into scratch/eryx
mise run install-eryx-precompile         # for verify; a while, cold
export WASI_SDK_PATH=$(mise where github:WebAssembly/wasi-sdk@wasi-sdk-33)
wheels/sysroot.sh /tmp/sysroot           # once; about 5 minutes
wheels/build.py build wheels/markupsafe --sysroot /tmp/sysroot --out /tmp/wheels
wheels/build.py verify /tmp/wheels/markupsafe-*.whl wheels/markupsafe
```

`verify` uses `scratch/tools/bin/eryx-precompile` (`--precompile` picks
another). `wheels/build.py unpack wheels/markupsafe --out /tmp/src` unpacks a
recipe's sdist to read, checked against its hash and as data only, without
running any of it.
Add `--sandbox` to `build` or `verify` to run it in a throwaway Docker
container (`ubuntu:26.04` with `build-essential`) that has only the recipes,
the toolchain and the output mounted: for building packages you haven't
reviewed, as the agent does. A `build.sh` that needs more (e.g. `rustup`)
installs it there.

## Requests

When an environment needs a package with native code we haven't built, it
waits (`status: waiting`), and the builder counts a request for it in
`native_package_requests` and opens a GitHub issue labelled
`native-package` here (eryx-run's `ERYX_GITHUB_ISSUES_REPO`, with
`ERYX_GITHUB_ISSUES_TOKEN`, a token that can write issues here; one issue
per package version, shared by every deployment). Labelling the issue
`build-native` runs `.github/workflows/wheels-agent.yml`: Claude writes the
recipe, builds and tests it, and a draft PR is opened with it, or a comment
says on the issue why it can't be built. Once a build is published, builders
queue every environment waiting for it.

The agent reads the package's source, which anyone can publish to PyPI and
then ask for, so it's treated as hostile:

- The agent's job has a read-only token, which it never sees (it runs the
  `claude` CLI, not claude-code-action), and no `id-token`. Its tools are
  reading, editing only `wheels/<package>/` (the name parsed from the
  issue's title), fetching from PyPI and GitHub, and `build.py unpack`,
  `build` and `verify`.
- `WHEELS_REQUIRE_SANDBOX` makes `build.py` refuse to build or verify
  without `--sandbox`, and `unpack` and the sandbox's output refuse a
  directory overlapping the checkout, so the package's code can't reach
  `build.py`.
- A second job, with no agent and nothing of the package's running, takes
  only `wheels/<package>/` (regular files, at most 1 MB), pushes it to a
  branch and opens the PR with `GITHUB_TOKEN`, then runs `wheels.yml` on
  the branch, as pushes with that token trigger nothing.

## Publishing

On `main`, `wheels/build.py publish` uploads each wheel to
`wheels/{sha256}/{file}` in the `eryx-wheels` R2 bucket, public at
`https://wheels.eryx.run` (Cloudflare caches it: a zone cache rule, and a
URL rewrite serving a path ending in `/` from its `index.html`, which R2
doesn't do itself). It lists each wheel in `index.json` with the ABI it's
for (`abi()`), which eryx-run's builders read, and writes a PEP 503 simple
index per ABI from it for pip (`<abi>/simple/<package>/`). Wheels are
cached for good (their keys have their hash) and the index pages for 5
minutes; `index.json` isn't cached, so builders see a publish at once.

Each wheel's entry also has a hash of what it was built from (`inputs` in
`build.py`: the recipe and its `native_requires`, `build.py`, the sysroot's
inputs and the ABI), and CI builds only recipes whose inputs aren't listed
(`build.py unpublished`), so an `eryx_rev` or workflow bump that changes none
of them builds nothing. A rebuild of the same version replaces it (and as builds
are reproducible, with the same file unless its inputs changed); older versions stay listed, so
environments locked to them still rebuild, and new resolves are pinned to
the newest. Every deployment reads the same bucket, so a merged recipe is
installable everywhere once it's published, without a deploy. Environments
created before then that needed it have to be created again.

The publish job uses an R2 token that can only write that bucket
(`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in the `wheels`
environment, which only `main` can use).

### The ABI

A wheel's tag can't say what it really needs, so the ABI is in the index's
path instead. It's `cp314-wasip2-sdk<N>` (`abi()`):

- `cp314`: CPython 3.14.
- `wasip2`: eryx's WASI. Its libpython is componentize-py's shared
  `libpython3.14.so`, and the wheels' `.so` files are dynamic libraries
  linked against it, so they only load in runtimes that link modules the
  same way (eryx, componentize-py), not in CPython's own statically linked
  WASI build, which has no `dlopen`.
- `sdk<N>`: the wasi-sdk eryx's guest libraries and these wheels are built
  with (`wasi_sdk_version` in eryx's mise.toml, as fetched).
  wasi-libc has no compatibility guarantees between versions, so moving it
  is a new ABI, and CI rebuilds and republishes every recipe for it.

eryx-run's builders install builds for their own `native::ABI`, which reads
the same version from its eryx submodule; its CI fails if the index has no
builds for it, so an eryx-run whose eryx moved to a new wasi-sdk before
`eryx_rev` here did finds out. Change `ABI_PREFIX` in `build.py`, and the
prefix in eryx-run's `native.rs`, if eryx's guest libraries change
incompatibly some other way.

## Using the wheels elsewhere

Anything that runs eryx's runtime can install them with pip, for the WASI
platform, into a directory to use as site-packages. The index is the ABI's,
`cp314-wasip2-sdk33` as of eryx's wasi-sdk 33:

```sh
pip install --target site-packages --only-binary :all: \
    --python-version 3.14 --implementation cp --abi cp314 \
    --platform wasi_0_0_0_wasm32 --platform any \
    --extra-index-url https://wheels.eryx.run/cp314-wasip2-sdk33/simple/ \
    numpy
```

Pure-Python packages still come from PyPI. Then give eryx the directory
(`SandboxBuilder::with_site_packages`, or `eryx-precompile --site-packages`).
