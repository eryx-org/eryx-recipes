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
  make `sysconfig` describe WASI, so extensions get WASI suffixes and the
  wheel is tagged `cp314-cp314-wasi_wasm32`.
- `$SYSROOT` is the sysroot (`include/`, `lib/`, `sysconfig/`).
- `$NATIVE_DEPS` has the recipe's `native_requires`, unpacked.

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
`wheels/{sha256}/{file}` in the `eryx-native-wheels` bucket and lists it in
`index.json` with the ABI it's for (`native::ABI`). A rebuild of the same
version replaces it; older versions stay listed, so environments locked to
them still rebuild, and new resolves are pinned to the newest. Every deployment reads the same bucket, so a merged recipe
is installable everywhere once it's published, without a deploy.
Environments created before then that needed it have to be created again.
