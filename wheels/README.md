# Native wheels

PyPI has no WebAssembly builds of packages with native code (C, C++, Rust),
so environments can't install them from there. This directory has a recipe
for each one we build for eryx's WASI. CI (`.github/workflows/native.yml`)
builds every recipe and tests it in eryx on each PR, and on `main` publishes
the wheels to the native wheels bucket. Builders install them in place of
PyPI's wheels: a requirement on `markupsafe`, or on a package that needs it,
gets our build, with resolution pinned to the version we built
(`src/builder/native.rs`).

## A recipe

`native/<package>/` holds:

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

`native/build.py build` downloads the sdist, checks its hash, and builds it
in a Python 3.14 venv, cross-compiling for `wasm32-wasip2`:

- `CC`, `CXX`, `LDSHARED`, `AR` are the wasi-sdk's (`$WASI_SDK_PATH`), the
  version eryx uses (`eryx/mise.toml`'s `wasi_sdk_version`).
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
and `SOURCE_DATE_EPOCH` is the sdist's newest file's time, so the files a
build generates aren't stamped with the time it ran. A recipe whose output
still varies needs that fixed in its `build.sh` or a patch.

The sysroot comes from `native/sysroot.sh`: CPython's headers and
sysconfig, configured as componentize-py builds eryx's libpython. The
wheel must contain a `.so`; WASI has no threads, processes, signals or
`mmap` beyond eryx's emulation, so code that needs them has to be patched or
configured out.

## Locally

```sh
export WASI_SDK_PATH=$(mise where github:WebAssembly/wasi-sdk@wasi-sdk-33)
native/sysroot.sh /tmp/sysroot           # once; about 5 minutes
native/build.py build native/markupsafe --sysroot /tmp/sysroot --out /tmp/wheels
native/build.py verify /tmp/wheels/markupsafe-*.whl native/markupsafe
```

`verify` needs `target/release/eryx-precompile` (`mise run eryx-precompile`).
Add `--sandbox` to `build` or `verify` to run it in a throwaway Docker
container (`ubuntu:24.04` with `build-essential`) that has only the recipes,
the toolchain and the output mounted: for building packages you haven't
reviewed, as the agent does. A `build.sh` that needs more (e.g. `rustup`)
installs it there.

## Requests

When an environment needs a package with native code we haven't built, it
waits (`status: waiting`), and the builder counts a request for it in
`native_package_requests` and opens a GitHub issue labelled
`native-package` (with `ERYX_GITHUB_ISSUES_TOKEN`, a token that can write
issues here; one issue per package version, shared by every deployment).
Labelling the issue `build-native` runs `.github/workflows/native-agent.yml`:
Claude writes the recipe, builds and tests it, and opens a draft PR, or says
on the issue why it can't. Once a build is published, builders queue every
environment waiting for it.

## Publishing

On `main`, `native/build.py publish` uploads each wheel to
`wheels/{sha256}/{file}` in the `eryx-wheels` R2 bucket, public at
`https://wheels.eryx.run` (Cloudflare caches it: a zone cache rule, and a
URL rewrite serving a path ending in `/` from its `index.html`, which R2
doesn't do itself). It lists each wheel in `index.json` with the ABI it's
for (`native::ABI`), which builders read, and writes a PEP 503 simple
index per ABI from it for pip (`<abi>/simple/<package>/`). Wheels are
cached for good (their keys have their hash) and the index pages for 5
minutes; `index.json` isn't cached, so builders see a publish at once.

A rebuild of the same version replaces it (and as builds are reproducible,
with the same file unless its inputs changed); older versions stay listed, so
environments locked to them still rebuild, and new resolves are pinned to
the newest. Every deployment reads the same bucket, so a merged recipe is
installable everywhere once it's published, without a deploy. Environments
created before then that needed it have to be created again.

The publish job uses an R2 token that can only write that bucket
(`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in the `native-wheels`
environment).

### The ABI

A wheel's tag can't say what it really needs, so the ABI is in the index's
path instead. `native::ABI` is `cp314-wasip2-sdk<N>`:

- `cp314`: CPython 3.14.
- `wasip2`: eryx's WASI. Its libpython is componentize-py's shared
  `libpython3.14.so`, and the wheels' `.so` files are dynamic libraries
  linked against it, so they only load in runtimes that link modules the
  same way (eryx, componentize-py), not in CPython's own statically linked
  WASI build, which has no `dlopen`.
- `sdk<N>`: the wasi-sdk eryx's guest libraries and these wheels are built
  with (`wasi_sdk_version` in eryx's mise.toml, read by `build.rs`).
  wasi-libc has no compatibility guarantees between versions, so moving it
  is a new ABI, and CI rebuilds and republishes every recipe for it.

Change the prefix in `native.rs` if eryx's guest libraries change
incompatibly some other way.

## Using the wheels elsewhere

Anything that runs eryx's runtime can install them with pip, for the WASI
platform, into a directory to use as site-packages. The index is the ABI's,
`native::ABI` (`cp314-wasip2-sdk33` as of eryx's wasi-sdk 33):

```sh
pip install --target site-packages --only-binary :all: \
    --python-version 3.14 --implementation cp --abi cp314 \
    --platform wasi_0_0_0_wasm32 --platform any \
    --extra-index-url https://wheels.eryx.run/cp314-wasip2-sdk33/simple/ \
    numpy
```

Pure-Python packages still come from PyPI. Then give eryx the directory
(`SandboxBuilder::with_site_packages`, or `eryx-precompile --site-packages`).
