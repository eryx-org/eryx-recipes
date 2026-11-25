#!/bin/bash
# pandas builds with meson, which takes the cross-compiler from a cross file
# rather than build.py's CC/CFLAGS, so write one from them (as native/numpy
# does), against our numpy's headers.
set -eu

# pandas compiles against numpy's headers and cimports its .pxd files, finding
# both through the build Python (`numpy.get_include()`, sys.path). That Python
# can't import our wasm32 numpy, and PyPI's numpy's headers describe its own
# platform (a 64-bit long, for one), so give it a stand-in: our numpy's files
# (NATIVE_DEPS, from native_requires), with an __init__ that only says where
# its headers are.
shim=$PWD/numpy-shim
mkdir -p "$shim"
cp -r "$NATIVE_DEPS/numpy" "$shim/numpy"
numpy_version=$(basename "$NATIVE_DEPS"/numpy-*.dist-info .dist-info | cut -d- -f2)
cat > "$shim/numpy/__init__.py" <<PY
__version__ = "$numpy_version"
def get_include():
    return "$shim/numpy/_core/include"
PY
export PYTHONPATH="$shim:$PYTHONPATH"

emulated="-D_WASI_EMULATED_MMAN -D_WASI_EMULATED_PROCESS_CLOCKS -D_WASI_EMULATED_GETPID -D_WASI_EMULATED_SIGNAL"
libs="-lwasi-emulated-mman -lwasi-emulated-process-clocks -lwasi-emulated-getpid -lwasi-emulated-signal"

# C++ throws abort (noexcept.c); an archive, so only extensions that throw
# link it.
$CC $CFLAGS -c "$(dirname "$0")/noexcept.c" -o noexcept.o
$AR rcs libnoexcept.a noexcept.o

# Compilers that link shared libraries (the extensions) to libpython, which
# only exists as a .so, so meson's checks can still link test programs. As in
# native/numpy/build.sh: wasm-ld itself, no library groups, and the sysroot's
# Python headers in place of the build Python's.
host_include=$(python -c 'import sysconfig; print(sysconfig.get_path("include"))')
mkdir wasi-bin
for tool in cc:"$CC" c++:"$CXX"; do
  cat > "wasi-bin/${tool%%:*}" <<WRAPPER
#!/bin/bash
args=()
for arg; do
  case "\$arg" in
    -Wl,--start-group | -Wl,--end-group) ;;
    -I$host_include) args+=("-I$SYSROOT/include/python3.14") ;;
    *) args+=("\$arg") ;;
  esac
done
set -- "\${args[@]}"
case " \$* " in
  *" -shared "*) exec ${tool#*:} -fuse-ld=lld "\$@" -L$SYSROOT/lib -lpython3.14 $PWD/libnoexcept.a ;;
  *" -c "* | *" -E "* | *" -S "*) exec ${tool#*:} "\$@" ;;
  *) exec ${tool#*:} -fuse-ld=lld "\$@" ;;
esac
WRAPPER
  chmod +x "wasi-bin/${tool%%:*}"
done

array() { printf "'%s', " $1; }

cat > wasi.cross <<CROSS
[binaries]
c = '$PWD/wasi-bin/cc'
cpp = '$PWD/wasi-bin/c++'
ar = '$AR'

[host_machine]
system = 'wasi'
cpu_family = 'wasm32'
cpu = 'wasm32'
endian = 'little'

[properties]
# Can't run test programs to detect it; clang's wasm32 long double is binary128
longdouble_format = 'IEEE_QUAD_LE'

[built-in options]
c_args = [$(array "$CFLAGS $emulated")]
cpp_args = [$(array "$CXXFLAGS $emulated")]
c_link_args = [$(array "$libs")]
cpp_link_args = [$(array "$libs")]
CROSS

# meson reads these for the build machine; the cross file has the host's
unset CC CXX CFLAGS CXXFLAGS LDFLAGS LDSHARED LDCXXSHARED

python -m pip wheel --no-deps --no-build-isolation -w "$WHEEL_DIR" . \
  "-Csetup-args=--cross-file=$PWD/wasi.cross"
