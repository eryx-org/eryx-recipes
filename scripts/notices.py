#!/usr/bin/env python3
"""Third-party notices for the published command components (eryx-org/eryx#837).

    scripts/notices.py generate OUTDIR NAME...   # write OUTDIR/<name>.notices.md and .spdx.json
    scripts/notices.py check                     # what CI's check job runs
    scripts/notices.py published NAME            # print yes or no

A command's notices are the entries of third_party/manual.toml that list it
(the tool, the libraries it links, and its toolchain's libc and runtime), and
for a Rust command, first, cargo-about's rendering of the crates it's built
from (about.toml, third_party/about.hbs) and of its toolchain's standard
library. For a Go command they're the Go modules it links: `go list -deps`
with the build's GOOS/GOARCH gives the set, google/licenseclassifier's
identify_license (pinned in mise.toml) classifies each module's licence
files in the vendor tree, and each licence must be one about.toml accepts
(see go_section). A Rust command's crates come from its checkout, with the features
its manifest builds with, so `generate` needs it built first (`mise run
build-cli -- NAME`). cargo-about's crate list is checked against `cargo tree`
for the same target and features (see cargo_about). The standard library's
come from the toolchain's rust-src (`mise run ensure-rust-src`).

`generate` also writes each command's SBOM, `<name>.spdx.json` (SPDX 2.3):
the command, and every package its notices list, as packages it contains.

`check` reads the manifests and manual.toml only, and fails, listing every
failure, when:
- a published command, or one of its [[deps]], has no entry, or a command of
  a lane lacks that lane's toolchain entries (LANE_ENTRIES);
- an entry is stale: a pin (commit or sha256) that no longer matches its
  manifest, a toolchain or WASI SDK other than mise.toml's, or a string its
  toolchain's sysroot no longer contains;
- a licence file an entry names doesn't exist.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "third_party" / "manual.toml"
TEMPLATE = ROOT / "third_party" / "about.hbs"
STD_ABOUT = ROOT / "third_party" / "about-std.toml"
MISE = ROOT / "mise.toml"

# Every manifest: clis/*.toml and clis/gpl/<name>/<name>.toml, by name.
COMMANDS = {
    p.stem: str(p.relative_to(ROOT))
    for p in sorted([*(ROOT / "clis").glob("*.toml"), *(ROOT / "clis" / "gpl").glob("*/*.toml")])
}

# Built and checked, but not published until their notices are complete.
UNPUBLISHED = set()
PUBLISHED = {name for name in COMMANDS if name not in UNPUBLISHED}

# The cargo flags of a manifest that choose its crates, which cargo-about
# takes too.
FEATURE_FLAGS = {"--no-default-features", "--all-features", "--features"}

# What every command of a lane links besides its own code and [[deps]]: the
# names of the manual.toml entries that must list it.
LANE_ENTRIES = {
    "c": ["wasi-libc (WASI SDK)", "LLVM compiler-rt builtins (WASI SDK)"],
    "rust": ["wasi-libc (Rust's wasm targets)", "The Rust standard library"],
    "go": ["The Go standard library (wasip3 fork)"],
}

# The Go lane's target (eryx-build's lanes/go.rs) and toolchain: the wasip3
# fork that scripts/bootstrap-go-wasip3.sh pins and builds.
GO_ENV = {"GOOS": "wasip3", "GOARCH": "wasm32"}
GO_BOOTSTRAP = ROOT / "scripts" / "bootstrap-go-wasip3.sh"

# The target every Rust command is built for (eryx-build's Rust lane).
RUST_TARGET = "wasm32-wasip2"

# Source files named like licences (go-openapi/spec's license.go) are code, not licence text.
LICENCE_FILE = re.compile(r"^(LICEN[CS]E|COPYING|UNLICENSE|NOTICE|COPYRIGHT)(?!.*\.(go|rs|c|h|py|js|s)$)", re.I)


def stable_toolchain():
    tool = tomllib.loads(MISE.read_text())["tools"]["rust"]
    return tool["version"] if isinstance(tool, dict) else tool


STABLE = stable_toolchain()


def published_version(name):
    """Command `name`'s version in commands.toml (`<upstream>-r.<n>`)."""
    namespace = "eryx-gpl" if COMMANDS[name].startswith("clis/gpl/") else "eryx"
    return tomllib.loads((ROOT / "commands.toml").read_text())[namespace][name]["version"]


def manifest(name):
    return tomllib.loads((ROOT / COMMANDS[name]).read_text())


def components():
    return tomllib.loads(MANUAL.read_text())["component"]


def pinned_cargo_about():
    """The cargo-about binary mise.toml pins, checked to be that version.

    `cargo about` would run whichever cargo-about $CARGO_HOME/bin has first,
    whatever mise pins, and an older one renders different notices (0.7.1
    drops wasm32-wasip3's crates and ignores some clarifications)."""
    pinned = tomllib.loads(MISE.read_text())["tools"]["cargo:cargo-about"]["version"]
    # `mise where`, not `mise which`: the latter returns a cargo-about found
    # in $CARGO_HOME/bin first.
    found = subprocess.run(["mise", "where", "cargo:cargo-about"], cwd=ROOT, capture_output=True, text=True)
    binary = Path(found.stdout.strip()) / "bin" / "cargo-about"
    if found.returncode != 0 or not binary.is_file():
        sys.exit(f"can't find mise's cargo-about {pinned}: {found.stderr.strip()} (run `mise install`)")
    version = run([str(binary), "--version"]).split()[-1]
    if version != pinned:
        sys.exit(f"{binary} is cargo-about {version}, but mise.toml pins {pinned}")
    return binary


def run(command, **kwargs):
    """Run a command whose output we read, showing its stderr if it fails."""
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        sys.exit(f"{' '.join(command)} failed ({result.returncode}):\n{result.stderr}")
    return result.stdout


def spdx_package(name, version, licence, location, purl=None):
    """An SPDX 2.3 package for the SBOM. Its SPDXID is set when the document is written."""
    package = {
        "name": name,
        "versionInfo": version,
        "downloadLocation": location or "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": licence or "NOASSERTION",
        "copyrightText": "NOASSERTION",
    }
    if purl:
        package["externalRefs"] = [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": purl}]
    return package


def crate_package(package, licence):
    """The SPDX package of a crate, from its `cargo metadata` entry."""
    name, version = package["name"], package["version"]
    # A crate from crates.io, or a path source: one of the checkout's own,
    # or the standard library's (std, core, ...), which crates.io doesn't
    # have, so no purl.
    if (package.get("source") or "").startswith("registry+"):
        location = f"https://crates.io/api/v1/crates/{name}/{version}/download"
        return spdx_package(name, version, licence, location, f"pkg:cargo/{name}@{version}")
    return spdx_package(name, version, licence, package.get("repository"))


def sbom_document(name, packages, created=None):
    """Command `name`'s SBOM: the command (packages[0]) contains the rest.
    The namespace is a hash of the packages, the command's version and
    component.wasm digest included, so the same build gives the same
    document apart from `created`, and another build a different one."""
    # A crate both the command and its standard library use is listed once.
    unique = []
    for p in packages:
        if p not in unique:
            unique.append(p)
    packages = [{**p, "SPDXID": f"SPDXRef-Package-{i}"} for i, p in enumerate(unique)]
    digest = hashlib.sha256(json.dumps(packages, sort_keys=True).encode()).hexdigest()
    created = created or datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": name,
        "documentNamespace": f"https://github.com/eryx-org/eryx-commands/spdx/{name}-{digest}",
        "creationInfo": {"created": created, "creators": ["Tool: eryx-commands scripts/notices.py"]},
        "packages": packages,
        "relationships": [
            {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Package-0"},
            *({"spdxElementId": "SPDXRef-Package-0", "relationshipType": "CONTAINS", "relatedSpdxElement": p["SPDXID"]}
              for p in packages[1:]),
        ],
    }


def cargo_about(args, output, label, cwd=ROOT, env=None, config=ROOT / "about.toml", sbom=None):
    """Render the Rust crates' notices; false if any licence can't be accepted,
    or cargo-about leaves out a crate cargo builds that can't be accounted for
    otherwise (see missed_section). `label` names what they're the crates of.
    Each crate is added to `sbom` (a list of SPDX packages) too."""
    binary = pinned_cargo_about()

    def run_about(extra, out):
        return subprocess.run(
            [str(binary), "about", "generate", "--offline", "--fail", "-c", str(config), *args, *extra, "-o", str(out)],
            cwd=cwd,
            env=env,
        ).returncode == 0

    if not run_about([str(TEMPLATE)], output):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        listed = Path(tmp) / "crates.json"
        if not run_about(["--format", "json"], listed):
            return False
        crates = json.loads(listed.read_text())["crates"]
        about = {f"{c['package']['name']} {c['package']['version']}" for c in crates}
        if sbom is not None:
            sbom += [crate_package(c["package"], c.get("license") or c["package"].get("license")) for c in crates]
    cargo = tree_crates(args, cwd, env)
    # cargo-about resolves targets and features itself, and can disagree with
    # cargo (0.7.1 dropped every crate only wasm32-wasip3 uses, and missed
    # some reached through weak `dep?/feature`s). A crate it lists that cargo
    # doesn't build only over-states the notices.
    for name in sorted(about - cargo):
        print(f"warning: cargo-about listed {name}, which cargo doesn't build for {args}", file=sys.stderr)
    missed = sorted(cargo - about)
    if missed:
        section = missed_section(missed, args, label, cwd, env, sbom)
        if section is None:
            return False
        with output.open("a") as f:
            f.write(section)
    return True


def accepted_licence(expression):
    """Whether every licence in an SPDX expression is one about.toml accepts."""
    accepted = set(tomllib.loads((ROOT / "about.toml").read_text())["accepted"])
    names = re.sub(r"[()]", " ", expression).split()
    ids = [n for i, n in enumerate(names) if n not in ("AND", "OR", "WITH") and (i == 0 or names[i - 1] != "WITH")]
    exceptions = [names[i + 1] for i, n in enumerate(names) if n == "WITH"]
    return all(i in accepted for i in ids) and all(
        any(a.endswith(f" WITH {e}") for a in accepted) for e in exceptions
    )


def missed_section(missed, args, label, cwd, env, sbom=None):
    """Notices for crates cargo builds but cargo-about left out, from each
    crate's declared licence and its licence files; None (after reporting
    why) if one can't be accounted for."""
    manifest = args[args.index("-m") + 1] if "-m" in args else str(ROOT / "Cargo.toml")
    metadata = json.loads(run(
        ["cargo", "metadata", "--format-version", "1", "--offline", "--manifest-path", manifest], cwd=cwd, env=env,
    ))
    packages = {f"{p['name']} {p['version']}": p for p in metadata["packages"]}
    out = ["", f"## Crates of {label} that cargo-about left out", "",
           "cargo builds these, but cargo-about didn't list them.", ""]
    ok = True
    for name in missed:
        package = packages[name]
        licence = package.get("license")
        files = sorted(f for f in Path(package["manifest_path"]).parent.iterdir() if LICENCE_FILE.match(f.name) and f.is_file())
        if not licence or not accepted_licence(licence) or not files:
            print(f"error: cargo-about left out {name} (licence {licence!r}, {len(files)} licence files), which cargo builds for {args}", file=sys.stderr)
            ok = False
            continue
        out += [f"### {name}", "", f"Licence: {licence}  ", f"Source: {package.get('repository') or 'crates.io'}", ""]
        if sbom is not None:
            sbom.append(crate_package(package, licence))
        for f in files:
            out += ["```", f.read_text(errors="replace").rstrip(), "```", ""]
    return "\n".join(out) if ok else None


def tree_crates(args, cwd, env):
    """The crates cargo itself builds for cargo-about's arguments: `cargo
    tree -e normal` of the root package (or with --workspace every member)
    on each target, with the same features. (`cargo metadata` would do too
    much: its resolve unifies features across the workspace and dev
    dependencies.)"""
    manifest = args[args.index("-m") + 1] if "-m" in args else str(ROOT / "Cargo.toml")
    targets = [args[i + 1] for i, a in enumerate(args) if a == "--target"]
    flags = ["--workspace"] if "--workspace" in args else []
    for i, a in enumerate(args):
        if a in ("--no-default-features", "--all-features"):
            flags.append(a)
        elif a == "--features":
            flags += [a, args[i + 1]]
    if not targets:
        sys.exit(f"no --target in {args}: the cross-check needs the artifact's targets")
    crates = set()
    for target in targets:
        lines = run(
            ["cargo", "tree", "--offline", "-e", "normal", "--target", target, "--prefix", "none",
             "--format", "{p}", *flags, "--manifest-path", manifest],
            cwd=cwd, env=env,
        ).splitlines()
        # "name vX.Y.Z (path)" or "name vX.Y.Z", "(*)" when already shown.
        crates |= {f"{line.split()[0]} {line.split()[1].removeprefix('v')}" for line in lines if line.strip()}
    if not crates:
        sys.exit(f"cargo tree found no crates for {args}")
    return crates


def std_library(toolchain):
    """The rust-src `library/` directory of `toolchain`."""
    sysroot = subprocess.run(
        ["rustc", f"+{toolchain}", "--print", "sysroot"], capture_output=True, text=True, check=True
    ).stdout.strip()
    library = Path(sysroot) / "lib" / "rustlib" / "src" / "rust" / "library"
    if not (library / "Cargo.lock").is_file():
        sys.exit(f"{toolchain} has no rust-src; run `rustup component add rust-src --toolchain {toolchain}`")
    return library


def std_overlay(library, tmp):
    """A copy of `library` (symlinks to its files) whose own crates all
    declare a licence.

    The Rust project's crates in library/ are under its MIT OR Apache-2.0
    (its COPYRIGHT and REUSE.toml), but a few internal ones (sysroot, test,
    windows-link) don't say so, and cargo-about can't clarify a crate without
    a licence file. Some files in them are under other licences, which the
    "Rust standard library" entries in manual.toml carry: core's Unicode
    tables (Unicode-3.0, REUSE.toml). Cargo.toml files and the lock file are
    real copies, so nothing writes into the toolchain.
    """
    overlay = Path(tmp) / "library"
    shutil.copytree(library, overlay, copy_function=os.symlink, ignore=shutil.ignore_patterns("vendor", "Cargo.lock"))
    shutil.copy(library / "Cargo.lock", overlay / "Cargo.lock")
    # The sources rust-src vendors, which its .cargo/config.toml points at.
    os.symlink(library / "vendor", overlay / "vendor")
    for toml in overlay.rglob("Cargo.toml"):
        text = toml.read_text()
        package = tomllib.loads(text).get("package")
        toml.unlink()
        if package and not {"license", "license-file"} & package.keys():
            text = text.replace("[package]\n", '[package]\nlicense = "MIT OR Apache-2.0"\n', 1)
        toml.write_text(text)
    return overlay


def std_section(path, toolchain, targets, sbom=None):
    """Append the notices of `toolchain`'s standard library, as built for
    `targets`, to `path`; false if cargo-about fails."""
    with tempfile.TemporaryDirectory() as tmp:
        library = std_overlay(std_library(toolchain), tmp)
        std = library / "std"
        # std's workspace uses unstable cargo features, which RUSTC_BOOTSTRAP
        # lets its toolchain's cargo read. Everything runs from std's
        # directory, so rust-src's .cargo/config.toml replaces crates.io with
        # the sources it vendors (cargo doesn't take source replacement from
        # the environment), and offline works with an empty cargo cache.
        env = {**os.environ, "RUSTUP_TOOLCHAIN": toolchain, "RUSTC_BOOTSTRAP": "1"}
        # about.toml plus std's own clarifications, which would only warn
        # (as config for a crate that isn't there) on every other artifact.
        config = Path(tmp) / "about.toml"
        config.write_text((ROOT / "about.toml").read_text() + "\n" + STD_ABOUT.read_text())
        rendered = Path(tmp) / "std.md"
        # The features the toolchain's own std builds use (and more than a
        # -Z build-std one may need), so nothing it can link is left out.
        args = [
            "-m", str(std / "Cargo.toml"), "--features", "backtrace panic-unwind",
            *(a for t in targets for a in ("--target", t)),
        ]
        if not cargo_about(args, rendered, "the Rust standard library", cwd=std, env=env, config=config, sbom=sbom):
            return False
        version = subprocess.run(
            ["rustc", f"+{toolchain}", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        with path.open("a") as f:
            f.write(f"\n\n# The Rust standard library ({version})\n\n" + rendered.read_text().split("\n", 1)[1])
    return True


def bin_package(checkout, bin_name):
    """The Cargo.toml of the package in `checkout` that builds `bin_name`:
    the root package, or a workspace member's.

    A package builds it if it declares a [[bin]] of that name or, with no
    [[bin]]s, is named for it and has a src/main.rs (cargo's default binary).
    Explicit [[bin]]s win, so a library sharing the binary's name (sd's `sd`,
    whose binary is in `sd-cli`) isn't taken for it.
    """
    root = checkout / "Cargo.toml"
    data = tomllib.loads(root.read_text())
    candidates = [root] if "package" in data else []
    for member in data.get("workspace", {}).get("members", []):
        dirs = checkout.glob(member) if any(c in member for c in "*?[") else [checkout / member]
        candidates += sorted(d / "Cargo.toml" for d in dirs if (d / "Cargo.toml").is_file())
    packages = [(path, tomllib.loads(path.read_text())) for path in candidates]
    for path, package in packages:
        if bin_name in [b["name"] for b in package.get("bin", [])]:
            return path
    for path, package in packages:
        if (
            not package.get("bin")
            and package.get("package", {}).get("name") == bin_name
            and (path.parent / "src" / "main.rs").is_file()
        ):
            return path
    sys.exit(f"no package in {checkout} builds the binary {bin_name}")


def cli_about_args(artifact, required):
    """cargo-about arguments for a Rust CLI's crates, from its checkout.

    None if the artifact isn't a Rust CLI, or (unless `required`) its
    checkout isn't there because it hasn't been built.
    """
    if artifact not in COMMANDS:
        return None
    manifest = tomllib.loads((ROOT / COMMANDS[artifact]).read_text())
    if manifest["lang"] != "rust":
        return None
    checkout = ROOT / manifest["source"]["dir"]
    if not (checkout / "Cargo.toml").is_file():
        if required:
            sys.exit(f"{artifact}: {checkout} isn't checked out; run `mise run build-cli -- {artifact}` first")
        return None
    cargo_toml = bin_package(checkout, manifest["build"]["bin"])
    flags = manifest["build"].get("cargo_flags", [])
    for flag in flags:
        if flag.startswith("--") and flag not in FEATURE_FLAGS:
            sys.exit(f"{artifact}: cargo flag {flag} in {COMMANDS[artifact]} isn't one cargo-about takes")
    return ["-m", str(cargo_toml), "--target", "wasm32-wasip2", *flags]


def manual_section(artifact, sbom=None):
    entries = [c for c in components() if artifact in c["artifacts"]]
    if sbom is not None:
        # Not the tool's own entry: generate starts the SBOM with the tool.
        sbom += [spdx_package(c["name"], c["version"], c["license"], c["url"])
                 for c in entries if not (c.get("cli") == COMMANDS[artifact] and "cli_dep" not in c)]
    if not entries:
        return ""
    out = ["", "# Other third-party code", ""]
    for c in entries:
        out += [f"## {c['name']} {c['version']}", "", f"Licence: {c['license']}  ", f"Source: {c['url']}", ""]
        for f in c["files"]:
            out += ["```", (ROOT / f).read_text().rstrip(), "```", ""]
    return "\n".join(out)


def go_bootstrap_var(name):
    match = re.search(rf'^{name}="([^"]+)"', GO_BOOTSTRAP.read_text(), re.M)
    return match[1] if match else None


def go_fork_commit():
    return go_bootstrap_var("GO_WASIP3_COMMIT")


def escape_module(module):
    """A module path as the module proxy and cache spell it: each capital as
    `!` and its lower case."""
    return re.sub(r"[A-Z]", lambda m: "!" + m[0].lower(), module)


def mpl_changes(checkout, module, version, env):
    """The files of vendor/<module> that differ from, or aren't in, the
    module as published (the module cache's copy, which `go mod vendor`
    fetched and verified against go.sum)."""
    cache = Path(run(["go", "env", "GOMODCACHE"], cwd=checkout, env=env).strip()) / f"{escape_module(module)}@{version}"
    if not cache.is_dir():
        sys.exit(f"{module}@{version} isn't in the module cache ({cache}); rebuild the command")
    vendored = checkout / "vendor" / module
    # Modules vendored inside this one's directory (a/b and a/b/v2) are
    # their own, so their files aren't compared with this one's.
    nested = [
        line.split()[1][len(module) + 1:] + "/"
        for line in (checkout / "vendor" / "modules.txt").read_text().splitlines()
        if line.startswith(f"# {module}/")
    ]
    changed = []
    for f in sorted(p for p in vendored.rglob("*") if p.is_file()):
        rel = f.relative_to(vendored).as_posix()
        if any(rel.startswith(n) for n in nested):
            continue
        original = cache / rel
        if not original.is_file() or original.read_bytes() != f.read_bytes():
            changed.append(rel)
    return changed


def go_section(name, path, sbom=None):
    """Append the notices of the Go modules command `name` links to `path`;
    false (after reporting why) if one's licence isn't accepted or isn't
    found.

    Each module's licence files are the ones `go mod vendor` copies into
    vendor/<module>/ (LICENSE, COPYING, NOTICE, ...). go-licenses would find
    them by walking up from each package, but in a vendored build `go list`
    gives modules no directory, so it walks up to the main module's own
    LICENSE and reports that (in-toto/attestation, Apache-2.0, came out as
    gh's MIT). They're classified with identify_license instead, with
    licence headers matched too: several modules ship only Apache-2.0's
    header as their LICENSE.
    """
    m = manifest(name)
    checkout = ROOT / m["source"]["dir"]
    if not (checkout / "vendor" / "modules.txt").is_file():
        sys.exit(f"{name}: {checkout} isn't built; run `mise run build-cli -- {name}` first")
    goroot = Path(os.environ.get("ERYX_GO_WASIP3_ROOT", ROOT / "scratch" / "go-wasip3"))
    classifier = ROOT / "scratch" / "tools" / "bin" / "identify_license"
    if not classifier.is_file():
        sys.exit("identify_license isn't installed; run `mise run install-license-classifier`")
    env = {
        **os.environ, **GO_ENV,
        "GOROOT": str(goroot),
        "PATH": f"{goroot / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "GOFLAGS": "-mod=vendor",
    }
    package = m["build"]["package"]
    # The modules whose packages are linked: not everything in go.sum.
    listed = run(
        ["go", "list", "-deps", "-f", "{{with .Module}}{{if not .Main}}{{.Path}} {{.Version}}{{end}}{{end}}", package],
        cwd=checkout, env=env,
    )
    modules = sorted({tuple(line.split()) for line in listed.splitlines() if line.strip()})
    # A licence for a module's documentation (opencontainers/go-digest's
    # LICENSE.docs, CC-BY-SA-4.0) doesn't cover the code that's linked.
    files = {
        module: sorted(
            f for f in (checkout / "vendor" / module).iterdir()
            if f.is_file() and LICENCE_FILE.match(f.name) and "doc" not in f.name.lower()
        )
        for module, _ in modules
    }
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "licences.json"
        run([str(classifier), "-headers", f"-json={report}", *(str(f) for fs in files.values() for f in fs)])
        found = {
            Path(r["Filepath"]): {c["Name"] for c in r.get("Classifications") or [] if c["Name"] != "Copyright"}
            for r in json.loads(report.read_text())
        }
    accepted = set(tomllib.loads((ROOT / "about.toml").read_text())["accepted"])
    ok = True
    out = ["", f"# Go modules {name} is built from", "",
           f"This command includes these Go modules (`go list -deps {package}` for {GO_ENV['GOOS']}/{GO_ENV['GOARCH']}), under the licences below.", ""]
    for module, version in modules:
        names = sorted(set().union(*(found.get(f.resolve(), found.get(f, set())) for f in files[module])) if files[module] else set())
        if not names:
            print(f"error: {name}: no licence found for {module} {version} in vendor/{module}", file=sys.stderr)
            ok = False
            continue
        if bad := [n for n in names if n not in accepted]:
            print(f"error: {name}: {module} {version} is {', '.join(bad)}, which about.toml doesn't accept", file=sys.stderr)
            ok = False
            continue
        source = [f"Source: https://pkg.go.dev/{module}@{version}"]
        if "MPL-2.0" in names:
            # Accepted for unmodified dependencies only (about.toml): check
            # the vendored copy is the published module, and point at its
            # source, as MPL-2.0 asks.
            if changed := mpl_changes(checkout, module, version, env):
                print(f"error: {name}: {module} {version} is MPL-2.0, which is only accepted unmodified, "
                      f"but the recipe changed {', '.join(changed)}", file=sys.stderr)
                ok = False
                continue
            source = [
                "Source (MPL-2.0, unmodified): "
                f"https://proxy.golang.org/{escape_module(module)}/@v/{version}.zip",
                *([f"Repository: https://{module}/tree/{version}"] if module.startswith("github.com/") else []),
            ]
        out += [f"## {module} {version}", "", f"Licence: {' AND '.join(names)}  ", *(f"{line}  " for line in source), ""]
        if sbom is not None:
            sbom.append(spdx_package(module, version, " AND ".join(names), f"https://proxy.golang.org/{escape_module(module)}/@v/{version}.zip",
                                     f"pkg:golang/{module}@{version}"))
        for f in files[module]:
            out += ["```", f.read_text(errors="replace").rstrip(), "```", ""]
    if ok:
        with path.open("a") as f:
            f.write("\n".join(out))
    return ok


def generate(outdir, names):
    """Write each named command's notices; return the ones that failed."""
    outdir.mkdir(parents=True, exist_ok=True)
    failed = []
    for name in names:
        if name not in COMMANDS:
            sys.exit(f"unknown command {name}; known: {', '.join(COMMANDS)}")
        if name not in PUBLISHED:
            sys.exit(f"{name} isn't published yet (UNPUBLISHED), so it has no complete notices")
        path = outdir / f"{name}.notices.md"
        m = manifest(name)
        lang = m["lang"]
        source = m["source"]
        # The command itself, as published (its commands.toml version and
        # the component built); what it statically links ([[deps]]) is in
        # manual.toml.
        component = ROOT / m["output"]["dir"] / "component.wasm"
        if not component.is_file():
            sys.exit(f"{name}: {component} isn't built; run `mise run build-cli -- {name}` first")
        tool = spdx_package(
            name, published_version(name), m["license"],
            f"git+{source['repo']}@{source['commit']}" if source.get("commit") else source["repo"],
        )
        tool["sourceInfo"] = f"built from {source['repo']} at {source['ref']}"
        with component.open("rb") as f:
            tool["checksums"] = [{"algorithm": "SHA256", "checksumValue": hashlib.file_digest(f, "sha256").hexdigest()}]
        sbom = [tool]
        if lang not in LANE_ENTRIES:
            sys.exit(f"{name}: no notices for the {lang} lane (LANE_ENTRIES)")
        args = cli_about_args(name, required=True)
        if manifest(name)["lang"] == "go":
            path.write_text("# Third-party notices\n")
            if not go_section(name, path, sbom):
                failed.append(name)
                continue
        elif args is None:
            path.write_text("# Third-party notices\n")
        else:
            run(["cargo", "fetch", "--locked", "--manifest-path", args[args.index("-m") + 1]])
            if not cargo_about(args, path, name, sbom=sbom) or not std_section(path, STABLE, [RUST_TARGET], sbom):
                failed.append(name)
                continue
        with path.open("a") as f:
            f.write(manual_section(name, sbom))
        (outdir / f"{name}.spdx.json").write_text(json.dumps(sbom_document(name, sbom), indent=2) + "\n")
    return failed


def check_manual():
    errors = []
    comps = components()
    mise = tomllib.loads(MISE.read_text())
    for c in comps:
        for a in c["artifacts"]:
            if a not in COMMANDS:
                errors.append(f"{c['name']}: unknown command {a!r}")
        for f in c["files"]:
            if not (ROOT / f).is_file():
                errors.append(f"{c['name']}: missing licence file {f}")
        if "cli" in c:
            m = tomllib.loads((ROOT / c["cli"]).read_text())
            # The source, or one of its [[deps]], pinned by commit or sha256.
            source = m["source"]
            if "cli_dep" in c:
                source = next((d for d in m.get("deps", []) if d["name"] == c["cli_dep"]), {})
            for key in ("commit", "sha256"):
                if f"cli_{key}" in c and source.get(key) != c[f"cli_{key}"]:
                    errors.append(f"{c['name']}: {c['cli']} builds {key} {source.get(key)}, not {c[f'cli_{key}']}")
        if "rust_toolchain" in c and c["rust_toolchain"] != STABLE:
            errors.append(f"{c['name']}: mise.toml pins rust {STABLE}, not {c['rust_toolchain']}; update the entry")
        if "sysroot_strings" in c:
            sysroot = Path(run(["rustc", f"+{c['rust_toolchain']}", "--print", "sysroot"]).strip())
            for rel in c["sysroot_strings"]["paths"]:
                if not (sysroot / rel).is_file():
                    errors.append(f"{c['name']}: {c['rust_toolchain']} has no {rel}; install its target (`mise run ensure-rust-src`)")
                    continue
                data = (sysroot / rel).read_bytes()
                for string in c["sysroot_strings"]["strings"]:
                    if string.encode() not in data:
                        errors.append(f"{c['name']}: {c['rust_toolchain']}'s {rel} doesn't contain {string!r}; update the entry")
        if "go_fork_commit" in c and c["go_fork_commit"] != go_fork_commit():
            errors.append(f"{c['name']}: scripts/bootstrap-go-wasip3.sh builds {go_fork_commit()}, not {c['go_fork_commit']}; update the entry")
        if "go_fork_commit" in c and not c["version"].startswith(f"{go_bootstrap_var('GO_WASIP3_REF')} "):
            errors.append(f"{c['name']}: scripts/bootstrap-go-wasip3.sh builds {go_bootstrap_var('GO_WASIP3_REF')}, not {c['version']}; update the entry")
        if "wasi_sdk" in c and c["wasi_sdk"] != mise["vars"]["wasi_sdk_version"]:
            errors.append(f"{c['name']}: mise.toml pins {mise['vars']['wasi_sdk_version']}, not {c['wasi_sdk']}; update the entry")

    for name in sorted(PUBLISHED):
        m = manifest(name)
        path = COMMANDS[name]
        listing = [c for c in comps if name in c["artifacts"]]
        if not any(c.get("cli") == path and "cli_dep" not in c for c in listing):
            errors.append(f"{name}: no third_party/manual.toml entry for the tool ({path})")
        for dep in m.get("deps", []):
            if not any(c.get("cli") == path and c.get("cli_dep") == dep["name"] for c in listing):
                errors.append(f"{name}: no third_party/manual.toml entry for its dependency {dep['name']} ({path})")
        if m["lang"] not in LANE_ENTRIES:
            errors.append(f"{name}: no notices for the {m['lang']} lane (LANE_ENTRIES)")
            continue
        for entry in LANE_ENTRIES[m["lang"]]:
            if not any(c["name"] == entry for c in listing):
                errors.append(f"{name}: the {m['lang']} lane's {entry!r} entry doesn't list it")
    return errors


def main():
    match sys.argv[1:]:
        case ["generate", outdir, *names] if names:
            if failed := generate(Path(outdir), names):
                sys.exit(f"the notices failed for {', '.join(failed)}; see the errors above")
        case ["published", name]:
            if name not in COMMANDS:
                sys.exit(f"unknown command {name}")
            print("yes" if name in PUBLISHED else "no")
        case ["check"]:
            errors = check_manual()
            for e in errors:
                print(f"error: {e}", file=sys.stderr)
            if errors:
                sys.exit(1)
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main()
