#!/usr/bin/env python3
"""Build, verify and publish WASI wheels from the recipes in wheels/

    wheels/build.py build RECIPE --sysroot DIR --out DIR [--sandbox]
    wheels/build.py verify WHEEL RECIPE --precompile PATH [--sandbox]
    wheels/build.py publish WHEEL... --bucket NAME --endpoint URL

`build` downloads the recipe's sdist (checked against its hash) into a new
directory and builds a wheel for eryx's WASI with the wasi-sdk at
$WASI_SDK_PATH and a sysroot from wheels/sysroot.sh: `pip wheel`, unless the
recipe has a build.sh. `verify` imports the recipe's modules in eryx and runs
its test.py there, with eryx-precompile, alongside its `test_requires`
(pure-Python packages from PyPI) and its `native_requires` (other recipes,
built first). `publish` uploads wheels to the native wheels bucket (R2,
public at https://wheels.eryx.run) and lists them in its index.json, which
eryx-run's builders read (its src/builder/native.rs), and in a PEP 503 simple index per
ABI for pip (with the aws CLI and its usual AWS_* credentials).

`--sandbox` runs a build or verify in a throwaway Docker container that has
only the recipes, the toolchain and the output mounted, and none of this
machine's environment, as an sdist's build code may not be trustworthy (the
agent in wheels-agent.yml always uses it).

Needs Python 3.12+ (tarfile's `filter`) and uv; the wheel is built with a Python 3.14
that uv provides.
"""

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECIPES = REPO / "wheels"
# eryx at mise.toml's eryx_rev, as `mise run fetch-eryx` leaves it: its
# mise.toml (the wasi-sdk version) and the libpython wheels link to
ERYX = REPO / "scratch/eryx"

# The ABI's prefix (`abi()` adds the wasi-sdk version): CPython 3.14 on
# eryx's WASI, its libpython and libc. eryx-run's `native::ABI` must match
# (its CI checks the index has builds for it), so change both together when
# eryx's guest libraries change incompatibly other than by the wasi-sdk.
ABI_PREFIX = "cp314-wasip2-sdk"

# The platform tag wheels get, whatever their build backend chose: what
# CPython's sysconfig.get_platform() reports on WASI, as maturin and
# wasi-wheels use (there's no PEP for WASI tags yet). It doesn't say which
# wasi-sdk or libpython a wheel needs, so the index is per ABI (`abi()`).
PLATFORM = "wasi_0_0_0_wasm32"
# Where recipes build, each in a directory of its own (see `build`)
WORK = Path("/tmp/eryx-native")

# Where `publish` puts wheels, served at https://wheels.eryx.run through
# Cloudflare's cache (R2's public custom domain)
BUCKET = "eryx-wheels"
INDEX_URL = "https://wheels.eryx.run/index.json"
ENDPOINT = "https://944ab6808cfb80c606e4873f8e6c2344.r2.cloudflarestorage.com"

# What a sandboxed build runs in; recipes that need more (e.g. rustup)
# install it in their build.sh
SANDBOX_IMAGE = "ubuntu:26.04"
SANDBOX_PACKAGES = "python3 patch ca-certificates curl build-essential"


def abi():
    """The ABI builds are for: `ABI_PREFIX` and eryx's wasi-sdk version, as
    eryx-run's build.rs reads it"""
    try:
        mise = (ERYX / "mise.toml").read_text()
    except FileNotFoundError:
        sys.exit(f"{ERYX / 'mise.toml'} is missing: run mise run fetch-eryx")
    # Whatever's between `wasi-sdk-` and the quote
    sdk = re.search(r'^wasi_sdk_version = "wasi-sdk-([^"]+)"$', mise, re.M)
    if not sdk:
        sys.exit('eryx\'s mise.toml has no wasi_sdk_version = "wasi-sdk-<N>"')
    return ABI_PREFIX + sdk.group(1)


def inputs(recipe):
    """A hash of everything a recipe's wheel is built from: its recipe
    directory and its native_requires' (recursively), this script, what the
    sysroot is made of, and the ABI. Builds are reproducible, so a recipe
    whose inputs are already published needs no build. Build requirements a
    recipe doesn't pin aren't in it: change the recipe to pick up new ones"""
    digest = hashlib.sha256(abi().encode())
    seen = set()

    def add(path):
        digest.update(f"\0{path.relative_to(REPO)}\0".encode())
        digest.update(path.read_bytes())

    def add_recipe(recipe):
        recipe, spec = load(recipe)
        if recipe in seen:
            return
        seen.add(recipe)
        for path in sorted(recipe.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                add(path)
        for dep in spec.get("native_requires", []):
            add_recipe(RECIPES / dep)

    add_recipe(recipe)
    for path in (Path(__file__).resolve(), RECIPES / "sysroot.sh",
                 ERYX / "crates/eryx-runtime/libs/libpython3.14.so.zst"):
        add(path)
    return digest.hexdigest()


def unpublished(args):
    """Print the recipes whose inputs (`inputs()`) aren't published yet"""
    # Cloudflare refuses urllib's default User-Agent
    request = urllib.request.Request(INDEX_URL, headers={"User-Agent": "eryx-run-native-build"})
    with urllib.request.urlopen(request) as response:
        index = json.load(response)
    published = {(build["name"], build["version"], build["abi"], build.get("inputs"))
                 for build in index["wheels"]}
    for recipe in args.recipes:
        path, spec = load(RECIPES / recipe)
        if (normalize(spec["name"]), spec["version"], abi(), inputs(path)) not in published:
            print(recipe)


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


def fixed_path(name, target):
    """WORK/name, a symlink to `target`"""
    # Resolved first: a native_requires build is given this link itself
    target = target.resolve(strict=True)
    link = WORK / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(target, target_is_directory=True)
    return link


def build(args):
    recipe, spec = load(args.recipe)
    # The toolchain is reached through fixed paths too, so a build on another
    # machine gives the same wheel: numpy's __config__.py records the
    # sysroot's path in its compiler flags
    sysroot = fixed_path("sysroot", Path(args.sysroot))
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    sdk = fixed_path("wasi-sdk", Path(os.environ["WASI_SDK_PATH"]))
    # Builds are reproducible: the same recipe and toolchain give the same
    # wheel, so publishing a rebuild doesn't change a locked hash. The work
    # directory is fixed, not random, as backends record paths under it (numpy's
    # __config__.py keeps the compiler's and the venv's)
    work = WORK / spec["name"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

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
        # Backends stamp the files they generate with SOURCE_DATE_EPOCH, if
        # set, rather than the time of the build: the sdist's newest file's
        epoch = str(int(max(member.mtime for member in tar.getmembers())))
        tar.extractall(src, filter="data")
    (top,) = src.iterdir()
    for patch in sorted(recipe.glob("*.patch")):
        run("patch", "-p1", "-i", patch, cwd=top)

    # A Python 3.14 venv with the build requirements; the build runs there,
    # cross-compiling through the sysroot's sysconfig
    venv = work / "venv"
    run("uv", "venv", "--quiet", "--python", "3.14", venv)
    python = venv / "bin/python"
    run("uv", "pip", "install", "--quiet", "--python", python, "pip", "wheel", *spec.get("requires", []))

    # Other recipes this one compiles against (e.g. numpy's headers), built
    # first into OUT/deps (for verify, which installs them beside it) and
    # unpacked into NATIVE_DEPS for its build.sh to use
    native_deps = work / "native-deps"
    native_deps.mkdir()
    for dep in spec.get("native_requires", []):
        dep_wheel = build(argparse.Namespace(recipe=RECIPES / dep, sysroot=sysroot, out=out / "deps"))
        with zipfile.ZipFile(dep_wheel) as zf:
            zf.extractall(native_deps)

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
        "NATIVE_DEPS": str(native_deps),
        "SOURCE_DATE_EPOCH": epoch,
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
    if "wasi" not in wheel.name.rsplit("-", 1)[-1] or not natives:
        sys.exit(f"{wheel.name} isn't a WASI build with native code: {natives}")
    # Backends tag WASI differently (`wasi_wasm32`, `wasi_0_0_0_wasm32`), so
    # every wheel gets PLATFORM; `wheel tags` rewrites its WHEEL and RECORD
    retag = subprocess.run(
        [python, "-m", "wheel", "tags", "--remove", "--platform-tag", PLATFORM, wheel],
        check=True, capture_output=True, text=True, env={**os.environ, "SOURCE_DATE_EPOCH": epoch},
    )
    wheel = wheel.parent / retag.stdout.strip().splitlines()[-1]
    built = out / wheel.name
    built.write_bytes(wheel.read_bytes())
    print(f"Built {built} ({', '.join(natives)})")
    return built


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
        # Wheels of its native_requires, which build left beside it
        for dep in spec.get("native_requires", []):
            (dep_wheel,) = (wheel.parent / "deps").glob(f"{normalize(dep).replace('-', '_')}-*.whl")
            command += ["--package", dep_wheel]
        for module in spec.get("imports", []):
            command += ["--import", module]
        test = recipe / "test.py"
        if test.exists():
            command += ["--verify-code", test.read_text()]
        run(*command)


def publish(args):
    """Upload each wheel and list it in index.json, replacing a build of the
    same package version for the same ABI, then write the simple index
    (`simple()`) from it"""

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
                # What it was built from, for `unpublished`; recipes are
                # named as their packages
                "inputs": inputs(RECIPES / normalize(name)),
            }
            # Keyed by hash, so never changed once there: cached for good
            aws("cp", wheel, f"s3://{args.bucket}/wheels/{build['sha256']}/{wheel.name}",
                "--cache-control", IMMUTABLE)
            # A rebuild of the same version replaces it; other versions stay,
            # for environments locked to them. The list stays in publishing
            # order: the builder pins new resolves to the last of each package.
            same = (build["name"], build["version"], build["abi"])
            index["wheels"] = [
                other for other in index["wheels"]
                if (other["name"], other["version"], other["abi"]) != same
            ] + [build]
            print(f"Published {json.dumps(build)}")

        site = Path(work) / "site"
        for path, content in simple(index).items():
            (site / path).parent.mkdir(parents=True, exist_ok=True)
            (site / path).write_text(content)
        # The index pages are written before index.json, so a builder that
        # sees a wheel listed can be sure pip users can find it too
        aws("cp", site, f"s3://{args.bucket}/", "--recursive",
            "--content-type", "text/html", "--cache-control", CHANGING)
        index_path.write_text(json.dumps(index, indent=2) + "\n")
        # Only builders read it, rarely, and they should see a publish at once
        aws("cp", index_path, f"s3://{args.bucket}/index.json",
            "--content-type", "application/json", "--cache-control", "no-cache")


# Cache lifetimes: wheels never change, the index pages do on each publish
IMMUTABLE = "public, max-age=31536000, immutable"
CHANGING = "public, max-age=300"


def simple(index):
    """A PEP 503 simple index of `index`'s wheels for each ABI, as
    {path: html}: `<abi>/simple/index.html` and
    `<abi>/simple/<name>/index.html` (Cloudflare serves a path ending in `/`
    from its index.html, as R2 doesn't)"""
    pages = {}
    for abi_name in sorted({build["abi"] for build in index["wheels"]}):
        builds = [build for build in index["wheels"] if build["abi"] == abi_name]
        names = sorted({build["name"] for build in builds})
        pages[f"{abi_name}/simple/index.html"] = page(
            "Simple index", [(f"{name}/", name) for name in names])
        for name in names:
            links = [
                (f"../../../wheels/{build['sha256']}/{build['file']}#sha256={build['sha256']}",
                 build["file"])
                for build in builds if build["name"] == name
            ]
            pages[f"{abi_name}/simple/{name}/index.html"] = page(f"Links for {name}", links)
    return pages


def page(title, links):
    anchors = "\n".join(
        f'<a href="{html.escape(href)}">{html.escape(text)}</a><br>' for href, text in links)
    return (f'<!DOCTYPE html>\n<html><head><meta name="pypi:repository-version" content="1.0">'
            f"<title>{html.escape(title)}</title></head>\n<body>\n{anchors}\n</body></html>\n")


def sandboxed(args):
    """Run this build or verify again in a throwaway container, with only
    what it needs mounted (at the same paths, read-only but for the output)
    and none of this machine's environment, credentials or git checkout"""
    uv = Path(shutil.which("uv") or "uv").resolve()
    if uv.name != "uv":
        sys.exit(f"--sandbox needs the uv binary itself on PATH, not {uv}")
    sdk = Path(os.environ["WASI_SDK_PATH"]).resolve()
    recipe = Path(args.recipe).resolve()
    if RECIPES not in recipe.parents:
        sys.exit(f"{recipe} isn't a recipe in {RECIPES}")
    mounts = [(RECIPES, "ro"), (sdk, "ro"), (uv.parent, "ro")]
    if args.run is build:
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        sysroot = Path(args.sysroot).resolve()
        mounts += [(sysroot, "ro"), (out, "rw")]
        inner = ["build", recipe, "--sysroot", sysroot, "--out", out]
    else:
        wheel, precompile = Path(args.wheel).resolve(), Path(args.precompile).resolve()
        mounts += [(wheel, "ro"), (precompile, "ro")]
        if (wheel.parent / "deps").is_dir():
            mounts.append((wheel.parent / "deps", "ro"))
        inner = ["verify", wheel, recipe, "--precompile", precompile]

    command = ["docker", "run", "--rm", "--init", "--workdir", "/tmp",
               "--env", f"WASI_SDK_PATH={sdk}",
               "--env", f"PATH={uv.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
    for path, mode in mounts:
        command += ["--volume", f"{path}:{path}:{mode}"]
    setup = (f"apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y "
             f"--no-install-recommends {SANDBOX_PACKAGES} > /dev/null")
    script = f"{setup} && exec python3 {RECIPES}/build.py {shlex.join(map(str, inner))}"
    run(*command, SANDBOX_IMAGE, "bash", "-c", script)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)

    command = commands.add_parser("build", help="Build a recipe's wheel")
    command.add_argument("recipe")
    command.add_argument("--sysroot", required=True)
    command.add_argument("--out", required=True)
    command.add_argument("--sandbox", action="store_true", help="Build in a throwaway container")
    command.set_defaults(run=build)

    command = commands.add_parser("verify", help="Import and test a wheel in eryx")
    command.add_argument("wheel")
    command.add_argument("recipe")
    command.add_argument("--precompile", default="scratch/tools/bin/eryx-precompile")
    command.add_argument("--sandbox", action="store_true", help="Verify in a throwaway container")
    command.set_defaults(run=verify)

    command = commands.add_parser("unpublished", help="List the recipes whose inputs aren't published")
    command.add_argument("recipes", nargs="+")
    command.set_defaults(run=unpublished)

    command = commands.add_parser("publish", help="Upload wheels and list them in the index")
    command.add_argument("wheels", nargs="+")
    command.add_argument("--bucket", default=BUCKET)
    command.add_argument("--endpoint", default=ENDPOINT)
    command.set_defaults(run=publish)

    args = parser.parse_args()
    if getattr(args, "sandbox", False):
        sandboxed(args)
    else:
        args.run(args)


if __name__ == "__main__":
    main()
