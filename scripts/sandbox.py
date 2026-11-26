#!/usr/bin/env python3
"""Build and test code we haven't reviewed in a throwaway Docker container

    scripts/sandbox.py build-cli NAME [--scratch DIR]
    scripts/sandbox.py smoke NAME --scratch DIR

`build-cli` runs eryx-build for command NAME (`mise run build-cli -- NAME
--sandbox` calls it), and `smoke` its smoke test, clis/smoke.sh (`clis/smoke.sh
NAME --sandbox` calls it). wheels/build.py's `--sandbox` uses `run` here too.

The container has this checkout read-only at its own path, and for a
command's build a new directory, DIR (by default a new one in /tmp),
read-write at <checkout>/scratch, where eryx-build fetches sources and writes
its output: so the build writes only DIR on the host. Once it's done, DIR's
symlinks are deleted (`unlink_links`) and `build-cli` prints the component
dir, DIR/NAME-component, as the last line of its stdout (everything else goes
to stderr). `smoke` mounts the same DIR read-only at <checkout>/scratch.

The toolchains are mounted read-only from where this checkout's pins put them
(`toolchain`), never from paths in the arguments: eryx-build
(scratch/tools), the Rust toolchain mise.toml pins (its rustup toolchain
dir, with the wasm32-wasip2 target), wasm-tools, wasmtime and wit-bindgen
(mise), the WASI SDK (mise, at vars.wasi_sdk_version) and the wasip3 Go fork
(scratch/go-wasip3, as scripts/bootstrap-go-wasip3.sh leaves it), each that's
installed. Cargo's home (crates, git checkouts) is DIR/cargo-home, at the
path the host's CARGO_HOME has, so the paths a build records are the same in
and out of the sandbox, and HOME (Go's caches) is DIR/home. Nothing else of
the host's is mounted, no Docker socket, and none of its environment is
passed on, but what the build needs, set here. The network is on (crates,
upstream sources, apt). The build and the smoke test run as the caller's
uid and gid, after root installs the image's packages.

Everything in the checkout is readable to the build, so keep nothing secret
in it (a .env) when building recipes you haven't reviewed.

With RECIPES_REQUIRE_SANDBOX set (the agents, whose arguments may come from
hostile source), the mise build-cli task, clis/smoke.sh and wheels/build.py's
build and verify only run sandboxed, a read-write DIR (or wheels' --out) must
be a new directory directly in /tmp, and `smoke`'s DIR an existing one there
(not a link). See wheels/build.py for what its sandbox mounts.

Only the standard library is used.
"""

import argparse
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path("/tmp").resolve()
# Set for the agents: see the module's docstring
STRICT = "RECIPES_REQUIRE_SANDBOX"

IMAGE = "ubuntu:26.04"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# What eryx-build's lanes run besides the toolchains, as CI's runners have
# them (plus the ninja-build and bison CI installs for the C lane): git, curl,
# unzip and tar for sources, a host C compiler and linker for Rust build
# scripts, CMake, Ninja, make and autotools for the C lane, and perl and
# python3 for patch scripts
BUILD_PACKAGES = ("ca-certificates curl git unzip xz-utils bzip2 build-essential cmake "
                  "ninja-build bison autoconf automake libtool pkg-config perl python3")
# The smoke tests' tools beyond the image's: `strings` (git's)
# ca-certificates: gh's smoke test copies their bundle for its HTTPS call
SMOKE_PACKAGES = "binutils ca-certificates"
# A command's name, as its directory's (clis/<name>/)
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def strict():
    """Whether this is an agent, whose arguments may come from hostile
    source: see the module's docstring"""
    return bool(os.environ.get(STRICT))


def outside_repo(path):
    """`path`, resolved, if it and this checkout don't overlap: what build
    code can write to mustn't be able to reach the scripts or anything else
    here"""
    path = Path(path).resolve()
    if path == REPO or REPO in path.parents or path in REPO.parents:
        sys.exit(f"{path} overlaps {REPO}: choose a directory outside it, such as /tmp/<dir>")
    return path


def scratch(path):
    """`path` if it's a new directory directly in /tmp: for the agents, the
    only host directories the sandbox may mount read-write, so the build's
    code can't reach the checkout, the agent's own files, the Docker socket
    or anything else on the host"""
    path = Path(path).resolve()
    if path.parent != TMP or path.exists():
        sys.exit(f"{path} must be a new directory directly in /tmp, such as /tmp/build-1")
    return path


def new_scratch(path=None):
    """A new directory for a command's build to write, created here: `path`
    (`scratch`'s rules for an agent, else anywhere outside the checkout), or
    a new one in /tmp"""
    if path is None:
        return Path(tempfile.mkdtemp(prefix="eryx-sandbox-", dir=TMP))
    path = scratch(path) if strict() else outside_repo(path)
    if path.exists():
        sys.exit(f"{path} exists: a sandboxed build starts from a new directory")
    path.mkdir(parents=True)
    return path


def built_scratch(path):
    """`path`, a directory a sandboxed build wrote, to mount read-only: for
    an agent, directly in /tmp, and never a link (Docker would mount what it
    points at)"""
    given = Path(path).absolute()
    path = given.resolve()
    if given != path or not path.is_dir():
        sys.exit(f"{given} must be a directory a sandboxed build left, not a link")
    if strict() and path.parent != TMP:
        sys.exit(f"{path} must be directly in /tmp, where a sandboxed build left it")
    return outside_repo(path)


def unlink_links(directory):
    """Delete the symlinks in `directory`: the build's code wrote it, and a
    link (to /proc/self/environ, say) would have whatever reads it next, on
    the host, read where it points instead"""
    for root, dirs, files in os.walk(directory):
        for name in dirs + files:
            path = Path(root) / name
            if path.is_symlink():
                print(f"Removing the link {path} the build left", file=sys.stderr, flush=True)
                path.unlink()


def check_mounts(mounts):
    """Refuse mounts, as (host path, container path, writable), that would
    give the container more than they say: a path that isn't absolute or
    that has a character `--mount` would read as another field, or a
    writable one that overlaps the checkout (or, for an agent, isn't under
    /tmp)"""
    for source, target, writable in mounts:
        for path in (source, target):
            if not Path(path).is_absolute() or re.search(r'[,"\n]', str(path)):
                sys.exit(f"Can't mount {path}: it must be absolute, without commas or quotes")
        if writable:
            outside_repo(source)
            if strict() and TMP not in Path(source).resolve().parents:
                sys.exit(f"Can't mount {source} read-write: it must be in /tmp")


def docker_command(mounts, env, script, packages, workdir="/tmp", user=None):
    """The `docker run` for `script`, with only `mounts` and `env`. Root
    installs `packages` first; then the script runs as `user` (uid, gid), if
    given, or root"""
    check_mounts(mounts)
    # No setuid binary in the image can raise anyone's privileges
    command = ["docker", "run", "--rm", "--init", "--security-opt", "no-new-privileges",
               "--workdir", str(workdir)]
    for name, value in env.items():
        command += ["--env", f"{name}={value}"]
    # --mount, not --volume: it refuses a missing source rather than create
    # it, as root, on the host
    for source, target, writable in mounts:
        mount = f"type=bind,source={source},target={target}"
        command += ["--mount", mount if writable else f"{mount},readonly"]
    if user:
        uid, gid = user
        # With no capabilities left at all, not just none effective
        script = (f"exec setpriv --reuid={uid} --regid={gid} --clear-groups --inh-caps=-all "
                  f"--bounding-set=-all --no-new-privs -- bash -c {shlex.quote(script)}")
    setup = (f"apt-get -qq update && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y "
             f"--no-install-recommends {packages} > /dev/null")
    return command + [IMAGE, "bash", "-c", f"{setup} && {script}"]


def run(mounts, env, script, packages, workdir="/tmp", user=None, stdout=None):
    """Run `script` in a throwaway container (`docker_command`)"""
    command = docker_command(mounts, env, script, packages, workdir, user)
    print("+", shlex.join(command), file=sys.stderr, flush=True)
    subprocess.run(command, check=True, stdout=stdout)


def load(name):
    """scripts/<name>.py, by path: not from sys.path, so nothing else can
    stand in for it"""
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mise(*args):
    """mise's output for `args`, in the checkout, or None if it fails"""
    found = subprocess.run(["mise", *args], cwd=REPO, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else None


def toolchain():
    """The toolchains a command's build and smoke test use, where this
    checkout's pins put them, as {name: directory}: each installed one, and
    eryx-build, which must be. They're mounted at these paths, as the build
    outside the sandbox sees them (Docker follows links in a mount's source)"""
    pins = tomllib.loads((REPO / "mise.toml").read_text())
    tools = REPO / "scratch/tools"
    rev = tools / "eryx-rev"
    if not (tools / "bin/eryx-build").is_file() or not rev.is_file() \
            or rev.read_text().strip() != pins["vars"]["eryx_rev"]:
        sys.exit("No eryx-build at vars.eryx_rev in scratch/tools: run mise run install-eryx-build")
    found = {"eryx-build": tools}
    for name, spec in [("wasm-tools", "cargo:wasm-tools"),
                       ("wasmtime", "aqua:bytecodealliance/wasmtime"),
                       ("wit-bindgen", "github:bytecodealliance/wit-bindgen")]:
        path = mise("bin-paths", spec)
        if path and (Path(path) / name).is_file():
            found[name] = Path(path)
    sdk = mise("where", f"github:WebAssembly/wasi-sdk@{pins['vars']['wasi_sdk_version']}")
    if sdk and (Path(sdk) / "bin/clang").is_file():
        found["wasi-sdk"] = Path(sdk)
    # The toolchain dir itself (bin/rustc, lib/rustlib/<target>), not
    # rustup's proxies, so nothing else of rustup's is needed. The Rust and C
    # lanes need its wasm32-wasip2 target, which eryx-build says if missing;
    # the Go lane doesn't
    sysroot = mise("exec", "--", "rustc", "--print", "sysroot")
    if sysroot:
        sysroot = Path(sysroot).resolve()
        version = subprocess.run([sysroot / "bin/rustc", "--version"],
                                 capture_output=True, text=True).stdout
        if not version.startswith(f"rustc {pins['tools']['rust']['version']} "):
            sys.exit(f"{sysroot}'s rustc is {version.strip()!r}, not mise.toml's")
        found["rust"] = sysroot
    go = REPO / "scratch/go-wasip3"
    if (go / "bin/go").is_file():
        found["go"] = go
    return found


def cargo_home():
    """The host's CARGO_HOME, where the sandbox's goes too: cargo records the
    paths of the crates it builds"""
    return Path(os.environ.get("CARGO_HOME") or Path.home() / ".cargo").absolute()


def bin_dirs(tools):
    """The directories to put on PATH: every tool's but the WASI SDK's, as a
    wasm clang on PATH breaks host builds (eryx-build's C lane)"""
    dirs = [tools[name] for name in ("wasm-tools", "wasmtime", "wit-bindgen") if name in tools]
    if "rust" in tools:
        dirs.append(tools["rust"] / "bin")
    return dirs


def build_mounts(scratch_dir, tools):
    """What a command's build sees: the checkout, `scratch_dir` (writable)
    at its scratch/, and the toolchains, at the paths the build expects"""
    mounts = [(REPO, REPO, False), (scratch_dir, REPO / "scratch", True),
              (scratch_dir / "cargo-home", cargo_home(), True)]
    for name, path in tools.items():
        # eryx-build and the Go fork are in the checkout's own scratch/,
        # under the build's
        target = {"eryx-build": REPO / "scratch/tools", "go": REPO / "scratch/go-wasip3"}.get(name, path)
        mounts.append((path, target, False))
    return mounts


def manifest(name):
    """Command `name`'s manifest, relative to the checkout"""
    if not NAME.match(name):
        sys.exit(f"{name!r} isn't a command's name")
    publish = load("publish")
    try:
        key, entry = publish.find_command(REPO, name)
    except publish.Error as e:
        sys.exit(str(e))
    path = publish.manifest_path(key, entry)
    if (REPO / "clis") not in (REPO / path).resolve().parents:
        sys.exit(f"{name}'s manifest {path} isn't in clis/")
    return path


def build_cli(args):
    path = manifest(args.name)
    tools = toolchain()
    scratch_dir = new_scratch(args.scratch)
    # Directories the build's mounts go on, made here so they're the
    # caller's, not root's
    for sub in ("cargo-home", "home", "tools", "go-wasip3"):
        (scratch_dir / sub).mkdir()
    env = {
        "PATH": ":".join(map(str, [*bin_dirs(tools), SYSTEM_PATH])),
        "HOME": REPO / "scratch/home",
        "CARGO_HOME": cargo_home(),
    }
    if "wasi-sdk" in tools:
        env["WASI_SDK_PATH"] = tools["wasi-sdk"]
    if "go" in tools:
        env["ERYX_GO_WASIP3_ROOT"] = REPO / "scratch/go-wasip3"
    script = f"exec scratch/tools/bin/eryx-build {shlex.quote(path)}"
    try:
        # The build's output on stderr, so stdout is just the path
        run(build_mounts(scratch_dir, tools), env, script, BUILD_PACKAGES,
            workdir=REPO, user=(os.getuid(), os.getgid()), stdout=sys.stderr)
    except subprocess.CalledProcessError:
        sys.exit(f"The build failed; its scratch directory is {scratch_dir}")
    finally:
        unlink_links(scratch_dir)
    out = scratch_dir / f"{args.name}-component"
    if not (out / "component.wasm").is_file():
        sys.exit(f"The build left no {out / 'component.wasm'}")
    print(out)


def smoke(args):
    if not NAME.match(args.name):
        sys.exit(f"{args.name!r} isn't a command's name")
    scratch_dir = built_scratch(args.scratch)
    tools = toolchain()
    mounts = [(REPO, REPO, False), (scratch_dir, REPO / "scratch", False)]
    mounts += [(tools[name], tools[name], False) for name in ("wasm-tools", "wasmtime") if name in tools]
    env = {"PATH": ":".join(map(str, [*bin_dirs(tools), SYSTEM_PATH])), "HOME": "/tmp"}
    run(mounts, env, f"exec bash clis/smoke.sh {shlex.quote(args.name)}", SMOKE_PACKAGES,
        workdir=REPO, user=(os.getuid(), os.getgid()))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(required=True)
    command = commands.add_parser("build-cli", help="Build a command with eryx-build")
    command.add_argument("name")
    command.add_argument("--scratch", help="A new directory for the build (default: one in /tmp)")
    command.set_defaults(run=build_cli)
    command = commands.add_parser("smoke", help="Smoke-test a command a sandboxed build built")
    command.add_argument("name")
    command.add_argument("--scratch", required=True, help="The directory its build wrote")
    command.set_defaults(run=smoke)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
