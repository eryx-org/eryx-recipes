#!/usr/bin/env bash
# Builds pydantic-core's Rust extension as a wasm shared library (a dylink
# module) that eryx links into its CPython at startup.
set -euo pipefail

# rustup, where there isn't one (e.g. `build.py --sandbox`'s container)
if ! command -v rustup > /dev/null; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs |
    sh -s -- -y --quiet --profile minimal --default-toolchain none
  export PATH="$HOME/.cargo/bin:$PATH"
fi

# Pinned: the linker below must be able to read the bitcode this rustc emits
toolchain=1.100.0
rustup toolchain install --profile minimal --target wasm32-wasip2 "$toolchain"
host="$(rustc +"$toolchain" -vV | sed -n 's/^host: //p')"
lld="$(rustc +"$toolchain" --print sysroot)/lib/rustlib/$host/bin/rust-lld"

# A Python extension here is a wasm shared library (a dylink module), so all
# its code must be position-independent:
# - Rust ships its wasm32 std built non-PIC, so the build uses link-time LTO
#   (`linker-plugin-lto`): the linker recompiles everything from bitcode, std
#   included, as PIC, with no nightly `-Z build-std`.
# - That needs a linker on the same LLVM as rustc, so it's the toolchain's
#   rust-lld, not the wasi-sdk's wasm-ld (LLVM 22, which can't read rustc
#   1.100's LLVM 23 bitcode).
# - wasm32-wasip2's default linker, wasm-component-ld, wraps the module in a
#   component; eryx needs the core module, which it links into its CPython.
# libc and libpython are linked as shared libraries, resolved when eryx links.
flags=(
  -C relocation-model=pic
  -C linker-plugin-lto=yes
  -C "linker=$lld"
  -C link-self-contained=no
  -C link-arg=--experimental-pic
  -C link-arg=--shared
  -C "link-arg=-L$WASI_SDK_PATH/share/wasi-sysroot/lib/wasm32-wasip2"
  -C "link-arg=-L$SYSROOT/lib"
  -C link-arg=-lpython3.14
)
export RUSTFLAGS="${flags[*]}"
export RUSTUP_TOOLCHAIN="$toolchain"
export PYO3_CROSS_LIB_DIR="$SYSROOT/sysconfig"
export PYO3_CROSS_PYTHON_VERSION=3.14

maturin build --release --target wasm32-wasip2 --interpreter python3.14 --out "$WHEEL_DIR"
