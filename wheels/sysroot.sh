#!/usr/bin/env bash
# Build what recipes compile against into OUT (a new directory):
#
#   OUT/include/python3.14/    CPython's headers, configured for eryx's WASI
#   OUT/sysconfig/             its _sysconfigdata, for setuptools and friends
#   OUT/lib/libpython3.14.so   eryx's own libpython, to link extensions to
#
# Usage: WASI_SDK_PATH=... native/sysroot.sh OUT
#
# The headers come from CPython configured the way componentize-py builds
# eryx's libpython (wasip2, -fPIC), at the same version, which this checks;
# within 3.14 the ABI is stable, so they match the libpython eryx runs.
# WASI_SDK_PATH must be the wasi-sdk eryx is built with (eryx/mise.toml's
# wasi_sdk_version), so extensions link against the same libc.
set -euo pipefail

out=${1:?usage: native/sysroot.sh OUT}
repo=$(cd "$(dirname "$0")/.." && pwd)
python_version=3.14.0
: "${WASI_SDK_PATH:?set WASI_SDK_PATH to the wasi-sdk eryx uses}"

libpython="$repo/eryx/crates/eryx-runtime/libs/libpython3.14.so.zst"
if ! zstd -dc "$libpython" | strings | grep -x "$python_version" >/dev/null; then
    echo "eryx's libpython isn't CPython $python_version: update python_version" >&2
    exit 1
fi

mkdir "$out" # a new directory, so nothing is overwritten
out=$(cd "$out" && pwd)
work="$out/work"
mkdir "$work" "$out/sysconfig" "$out/lib"

curl -sSfL "https://www.python.org/ftp/python/$python_version/Python-$python_version.tgz" |
    tar xz -C "$work"
src="$work/Python-$python_version"

# A build Python of the same version, which the cross build needs
mkdir "$src/builddir" "$src/builddir/build" "$src/builddir/wasi"
(cd "$src/builddir/build" && "$src/configure" -q --prefix="$src/builddir/build/install" && make -s -j"$(nproc)")

(
    cd "$src/builddir/wasi"
    CONFIG_SITE="$src/Tools/wasm/wasi/config.site-wasm32-wasi" \
        CFLAGS="--target=wasm32-wasip2 -fPIC" LDFLAGS="--target=wasm32-wasip2" \
        "$src/Tools/wasm/wasi-env" "$src/configure" -q -C \
        --host=wasm32-unknown-wasip2 --build="$("$src/config.guess")" \
        --with-build-python="$src/builddir/build/python" \
        --prefix="$src/builddir/wasi/install" --enable-ipv6 --disable-test-modules
    make -s -j"$(nproc)" build_all install
)

install="$src/builddir/wasi/install"
cp -r "$install/include" "$out/include"
cp "$install/lib/python3.14/_sysconfigdata__wasi_wasm32-wasi.py" "$out/sysconfig/"
zstd -q -dc "$libpython" -o "$out/lib/libpython3.14.so"
rm -r "$work"
echo "Sysroot for CPython $python_version on wasm32-wasip2 in $out"
