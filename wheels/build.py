#!/usr/bin/env python3
"""Build, verify and publish WASI wheels from the recipes in native/

    native/build.py build RECIPE --sysroot DIR --out DIR
    native/build.py verify WHEEL RECIPE --precompile PATH
    native/build.py publish WHEEL... --bucket NAME --endpoint URL

`build` downloads the recipe's sdist (checked against its hash) into a new
directory and builds a wheel for eryx's WASI with the wasi-sdk at
$WASI_SDK_PATH and a sysroot from native/sysroot.sh: `pip wheel`, unless the
recipe has a build.sh. `verify` imports the recipe's modules in eryx and runs
its test.py there, with eryx-precompile, alongside its `test_requires`
(pure-Python packages from PyPI). `publish` uploads wheels to the
native wheels bucket and lists them in its index.json (with the aws CLI and
its usual AWS_* credentials), which the builder reads (src/builder/native.rs).

Needs Python 3.12+ (tarfile's `filter`) and uv; the wheel is built with a Python 3.14
that uv provides.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def abi():
    """The ABI the builder installs builds for (`native::ABI`)"""
    source = (REPO / "src/builder/native.rs").read_text()
    return re.search(r'pub const ABI: &str = "([^"]+)";', source).group(1)


def normalize(name):
    """A package name as PyPI compares them (PEP 503)"""
    return re.sub(r"[-_.]+", "-", name).lower()


def load(recipe):
    recipe = Path(recipe).resolve()
    with open(recipe / "recipe.toml", "rb") as f:
        return recipe, tomllib.load(f)


def run(*args, **kwargs):
    print("+", " ".join(str(arg) for arg in args), flush=True)
    subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def build(args):
    recipe, spec = load(args.recipe)
    sysroot = Path(args.sysroot).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sdk = Path(os.environ["WASI_SDK_PATH"])
    work = Path(tempfile.mkdtemp(prefix=f"native-{spec['name']}-"))

    # The sdist, checked against the recipe's hash
    archive = work / spec["sdist"].rsplit("/", 1)[-1]
    with urllib.request.urlopen(spec["sdist"]) as response:
        archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != spec["sha256"]:
        sys.exit(f"{archive.name} has sha256 {digest}, not the recipe's {spec['sha256']}")
    src = work / "src"
    src.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(src, filter="data")
    (top,) = src.iterdir()
    for patch in sorted(recipe.glob("*.patch")):
        run("patch", "-p1", "-i", patch, cwd=top)

    # A Python 3.14 venv with the build requirements; the build runs there,
    # cross-compiling through the sysroot's sysconfig
    venv = work / "venv"
    run("uv", "venv", "--quiet", "--python", "3.14", venv)
    python = venv / "bin/python"
    run("uv", "pip", "install", "--quiet", "--python", python, "pip", *spec.get("requires", []))

    target = "--target=wasm32-wasip2"
    flags = f"-fPIC -I{sysroot}/include/python3.14"
    env = {
        **os.environ,
        "CC": f"{sdk}/bin/clang {target}",
        "CXX": f"{sdk}/bin/clang++ {target}",
        "LDSHARED": f"{sdk}/bin/clang {target} -shared",
        "LDCXXSHARED": f"{sdk}/bin/clang++ {target} -shared",
        "CFLAGS": flags,
        "CXXFLAGS": flags,
        # Linking to eryx's libpython resolves the C API's symbols
        "LDFLAGS": f"-shared -L{sysroot}/lib -lpython3.14",
        "AR": f"{sdk}/bin/ar",
        "RANLIB": "true",
        "_PYTHON_SYSCONFIGDATA_NAME": "_sysconfigdata__wasi_wasm32-wasi",
        "_PYTHON_HOST_PLATFORM": "wasi-wasm32",
        "PYTHONPATH": str(sysroot / "sysconfig"),
        "PATH": f"{venv}/bin:{os.environ['PATH']}",
        "SYSROOT": str(sysroot),
        "WHEEL_DIR": str(work / "dist"),
        **spec.get("env", {}),
    }
    if (recipe / "build.sh").exists():
        run("bash", recipe / "build.sh", cwd=top, env=env)
    else:
        run(python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "-w", work / "dist", ".", cwd=top, env=env)

    (wheel,) = (work / "dist").glob("*.whl")
    with zipfile.ZipFile(wheel) as zf:
        natives = [name for name in zf.namelist() if name.endswith(".so")]
    if not wheel.name.endswith("-wasi_wasm32.whl") or not natives:
        sys.exit(f"{wheel.name} isn't a WASI build with native code: {natives}")
    built = out / wheel.name
    built.write_bytes(wheel.read_bytes())
    print(f"Built {built} ({', '.join(natives)})")


def verify(args):
    recipe, spec = load(args.recipe)
    wheel = Path(args.wheel).resolve()
    with tempfile.TemporaryDirectory() as work:
        command = [
            args.precompile, "compile", "/dev/null", "--preinit",
            "--cpu-features", "native", "--package", wheel,
            "--output", Path(work) / "runtime.cwasm",
        ]
        # What the wheel needs to import, as environments get it from PyPI
        if spec.get("test_requires"):
            site_packages = Path(work) / "site-packages"
            run("uv", "pip", "install", "--quiet", "--no-deps", "--only-binary", ":all:",
                "--python-version", "3.14", "--python-platform", "x86_64-unknown-linux-gnu",
                "--target", site_packages, *spec["test_requires"])
            command += ["--site-packages", site_packages]
        for module in spec.get("imports", []):
            command += ["--import", module]
        test = recipe / "test.py"
        if test.exists():
            command += ["--verify-code", test.read_text()]
        run(*command)


def publish(args):
    """Upload each wheel and list it in index.json, replacing a build of the
    same package version for the same ABI"""

    def aws(*command):
        run("aws", "--endpoint-url", args.endpoint, "s3", *command)

    with tempfile.TemporaryDirectory() as work:
        index_path = Path(work) / "index.json"
        # Only a missing index starts a new one: any other failure stops,
        # rather than replace the index with this run's wheels alone
        found = subprocess.run(
            ["aws", "--endpoint-url", args.endpoint, "s3api", "head-object",
             "--bucket", args.bucket, "--key", "index.json"],
            capture_output=True, text=True,
        )
        if found.returncode == 0:
            aws("cp", f"s3://{args.bucket}/index.json", index_path)
            index = json.loads(index_path.read_text())
        elif "Not Found" in found.stderr or "404" in found.stderr:
            index = {"wheels": []}
        else:
            sys.exit(f"Couldn't check for the index: {found.stderr}")

        for wheel in map(Path, args.wheels):
            name, version = wheel.name.split("-")[:2]
            build = {
                "name": normalize(name),
                "version": version,
                "abi": abi(),
                "file": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
            aws("cp", wheel, f"s3://{args.bucket}/wheels/{build['sha256']}/{wheel.name}")
            # A rebuild of the same version replaces it; other versions stay,
            # for environments locked to them. The list stays in publishing
            # order: the builder pins new resolves to the last of each package.
            same = (build["name"], build["version"], build["abi"])
            index["wheels"] = [
                other for other in index["wheels"]
                if (other["name"], other["version"], other["abi"]) != same
            ] + [build]
            print(f"Published {json.dumps(build)}")

        index_path.write_text(json.dumps(index, indent=2) + "\n")
        aws("cp", index_path, f"s3://{args.bucket}/index.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)

    command = commands.add_parser("build", help="Build a recipe's wheel")
    command.add_argument("recipe")
    command.add_argument("--sysroot", required=True)
    command.add_argument("--out", required=True)
    command.set_defaults(run=build)

    command = commands.add_parser("verify", help="Import and test a wheel in eryx")
    command.add_argument("wheel")
    command.add_argument("recipe")
    command.add_argument("--precompile", default="target/release/eryx-precompile")
    command.set_defaults(run=verify)

    command = commands.add_parser("publish", help="Upload wheels and list them in the index")
    command.add_argument("wheels", nargs="+")
    command.add_argument("--bucket", default="eryx-native-wheels")
    command.add_argument("--endpoint", default="https://fly.storage.tigris.dev")
    command.set_defaults(run=publish)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
