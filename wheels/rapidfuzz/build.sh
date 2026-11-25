# rapidfuzz: C++ (rapidfuzz-cpp, header-only) wrapped by Cython, built with
# scikit-build-core and CMake. The sdist ships the Cython output (.cxx), so
# Cython isn't needed. Runs in the unpacked sdist, with build.py's
# environment (see wheels/README.md).
set -eu

recipe=$(dirname "$0")
build=$PWD/build-wasi
mkdir "$build"

# Stand-ins for libc++abi's exception functions (see no-exceptions.cpp),
# linked into every module. As an object file it's always linked, wherever
# it lands on the command line.
"$WASI_SDK_PATH/bin/clang++" --target=wasm32-wasip2 -fPIC -O2 \
    -c "$recipe/no-exceptions.cpp" -o "$build/no-exceptions.o"
export LDFLAGS="$LDFLAGS $build/no-exceptions.o"

# scikit-build-core runs CMake. The wasi-sdk's toolchain file sets the
# compilers and CMAKE_SYSTEM_NAME=WASI (CC and CXX are ignored), and CMake
# reads CFLAGS, CXXFLAGS and LDFLAGS from the environment, so the modules
# are built -fPIC with CPython's WASI headers and linked -shared against
# eryx's libpython.
#
# FindPython would otherwise look for the headers of the Python running the
# build, so they're given explicitly; it doesn't need a libpython to link
# modules. The module suffix (.cpython-314-wasm32-wasi.so) and the wheel tag
# come from the WASI sysconfig that build.py selects.
#
# wasi.patch turns off LTO (wasi-sdk's would link a static libc) and
# Taskflow (threads) for WASI. RAPIDFUZZ_BUILD_EXTENSION (in recipe.toml) makes a failed C++ build fail
# instead of producing a pure-Python wheel.
python -m pip wheel --no-deps --no-build-isolation -w "$WHEEL_DIR" . \
    -Cbuild-dir="$build/cmake" \
    -Ccmake.define.CMAKE_TOOLCHAIN_FILE="$WASI_SDK_PATH/share/cmake/wasi-sdk-p2.cmake" \
    -Ccmake.define.Python_INCLUDE_DIR="$SYSROOT/include/python3.14"
