#!/usr/bin/env python3
"""Version checks and registry publishing for the commands (#5).

    publish.py check-versions [--base REF]
        Check commands.toml against the recipes in clis/. With --base, also
        check that every command whose build inputs changed since REF has a
        new version, that a new upstream version starts again at r.1, and
        that no version is lower than one commands.lock already has.

    publish.py publish --registry HOST [--insecure] [--fail-existing]
                       [--out RESULTS.json] COMPONENT_DIR...
        Push each built component dir (as eryx-build leaves it, with
        component.wasm and provenance.json) to
        HOST/eryx-org/commands/<namespace>/<name>:<version> with `wkg oci
        push`, annotated from its provenance. A version that's already in the
        registry is never pushed again: it's listed as already there, or with
        --fail-existing it fails the run. The pushed manifest is read back
        and its layer checked against component.wasm. RESULTS.json lists
        every command, pushed or already there, with its digests, for
        signing (#6) and for `lock`; it's rewritten after each command.

    publish.py lock --results RESULTS.json [--lock commands.lock]
        Add the results' versions to a lock file in wkg.lock's format.

    publish.py cache-key NAME
        Print a hash of everything a command's built component dir depends
        on, for CI's component cache: its build inputs (build_inputs) and its
        notices' (NOTICE_FILES, its third_party/manual.toml entries and their
        files, its commands.toml entry). Any change to them is a new key.

    publish.py bump NAMESPACE:NAME [--branch BRANCH]
        For a recipe fetched from eryx (`fetched` in commands.toml): move its
        pin to the newest commit on BRANCH that changed the recipe or its
        vendor patch, and bump its version (back to r.1 for a new upstream).

Nothing in an OCI registry stops a tag being pushed again: immutability rests
on the check before each push, and on publishes never running concurrently
(CI's publish concurrency group).

Registry credentials come from WKG_OCI_USERNAME and WKG_OCI_PASSWORD, as
for wkg itself. Only the standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = "commands.toml"
LOCK = "commands.lock"
REPOSITORY_PREFIX = "eryx-org/commands"
SOURCE_URL = "https://github.com/eryx-org/eryx-recipes"
ERYX_URL = "https://github.com/eryx-org/eryx"
# `<MAJOR.MINOR.PATCH>-r.<n>`: semver, with the revision as a prerelease
# whose numeric part compares numerically (`-r.2 < -r.10`), as wkg needs.
VERSION = re.compile(
    r"^(?P<upstream>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"-r\.(?P<revision>[1-9][0-9]*)$"
)
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"

# The pinned tools whose versions change what each lane builds, as
# (mise.toml table, key) pairs; see build_inputs.
LANE_PINS = {
    "rust": [("vars", "eryx_rev"), ("tools", "rust")],
    # C builds link a rustc-built start-up object (eryx-org/eryx#978).
    "c": [("vars", "eryx_rev"), ("tools", "rust"), ("vars", "wasi_sdk_version"),
          ("tools", "github:bytecodealliance/wit-bindgen")],
    "go": [("vars", "eryx_rev"), ("tools", "cargo:wasm-tools")],
}
# The wasmtime that compiles a manifest's `[cwasm]` (eryx-build's
# compile_cwasm runs mise's): a pin of every command with one.
CWASM_PINS = [("tools", "aqua:bytecodealliance/wasmtime")]
# Files outside the recipe a lane's output depends on; see build_inputs.
LANE_FILES = {"go": ["scripts/bootstrap-go-wasip3.sh"]}
# What the notices and SBOM (scripts/notices.py) are made from, besides the
# command's manual.toml entries, its commands.toml entry and its build inputs:
# the generator for every lane, and each lane's pinned notice tools.
NOTICE_FILES = ["scripts/notices.py", "about.toml", "third_party/about.hbs",
                "third_party/about-std.toml"]
NOTICE_PINS = {
    "rust": [("tools", "cargo:cargo-about")],
    "c": [],
    "go": [("vars", "license_classifier_version")],
}
# A file a patch script reads from its own directory, named in the script.
SIBLING = re.compile(r"[A-Za-z0-9_.-]+\.(?:patch|diff|sh|c|h|py)\b")
# A path a patch script spells relative to its own directory, `$dir/<path>`
# or `$(dirname "${BASH_SOURCE[0]}")/<path>` (`$dir/../shims/x.h`): it must
# exist. One with a variable in it (`$dir/$1`) is left to SIBLING.
SCRIPT_RELATIVE = re.compile(
    r'(?:\$\{?dir\}?|\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\))/([A-Za-z0-9_./-]+)'
)


class Error(Exception):
    pass


# ---------------------------------------------------------------------------
# commands.toml and the recipes
# ---------------------------------------------------------------------------


def load_commands(text: str) -> dict[tuple[str, str], dict]:
    """commands.toml's entries, by (namespace, name)."""
    commands = {}
    for namespace, entries in tomllib.loads(text).items():
        for name, entry in entries.items():
            commands[(namespace, name)] = entry
    return commands


def default_manifest(namespace: str, name: str) -> str:
    if namespace == "eryx-gpl":
        return f"clis/gpl/{name}/{name}.toml"
    return f"clis/{name}.toml"


def manifest_path(key: tuple[str, str], entry: dict) -> str:
    return entry.get("manifest", default_manifest(*key))


def recipes(root: Path) -> dict[tuple[str, str], str]:
    """Every recipe in clis/, as (namespace, name) -> repo-relative path."""
    found = {}
    paths = sorted(root.glob("clis/*.toml")) + sorted(root.glob("clis/gpl/*/*.toml"))
    for path in paths:
        rel = path.relative_to(root).as_posix()
        namespace = "eryx-gpl" if rel.startswith("clis/gpl/") else "eryx"
        name = tomllib.loads(path.read_text())["name"]
        found[(namespace, name)] = rel
    return found


def upstream_from_ref(name: str, ref: str) -> str:
    """`v1.1.0` -> `1.1.0`, `curl-8_20_0` -> `8.20.0`."""
    ref = ref.removeprefix(f"{name}-").removeprefix("v")
    return ref.replace("_", ".")


def parse_version(version: str) -> tuple[str, int]:
    match = VERSION.match(version)
    if not match:
        raise Error(f"version {version!r} isn't `<MAJOR.MINOR.PATCH>-r.<n>`")
    return match["upstream"], int(match["revision"])


def strings(value) -> list[str]:
    """Every string anywhere in a parsed TOML value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def build_inputs(root: Path, manifest_rel: str, manifest: dict) -> tuple[list[str], list]:
    """What a recipe's build depends on: the repo paths it reads, and the
    mise.toml pins of its lane, as (table, key) pairs. A change to any of them
    is a new build, so needs a new version (check_versions).

    The paths are:
    - the manifest, and every file under its own directory when it has one
      (clis/gpl/<name>/: its patches, shims and notices);
    - every repo path the manifest names, bare or as `{repo}/<path>` inside a
      flag (patches, shims, a WIT world), outside scratch/;
    - the files a named script reads from beside it (patch-git.sh applies
      git-wasi.patch), found by name in the script, and every path it spells
      relative to its own directory (SCRIPT_RELATIVE), which must exist;
    - its lane's files outside clis/ (LANE_FILES).

    The pins are its lane's (LANE_PINS), and the wasmtime that compiles its
    cwasm if it has one (CWASM_PINS). A commands.toml entry adds its own
    (entry_pins).
    """
    paths = {manifest_rel}
    recipe_dir = Path(manifest_rel).parent
    if recipe_dir != Path("clis"):
        paths.update(
            p.relative_to(root).as_posix() for p in (root / recipe_dir).rglob("*") if p.is_file()
        )
    for value in strings(manifest):
        for candidate in [value, *re.findall(r"\{repo\}/(\S+)", value)]:
            candidate = candidate.rstrip("/")
            if candidate.startswith(("scratch/", "/")) or ".." in candidate:
                continue
            path = root / candidate
            if not path.exists():
                continue
            paths.add(candidate)
            if path.is_file() and path.suffix == ".sh":
                text = path.read_text()
                for name in SIBLING.findall(text):
                    if (path.parent / name).is_file():
                        paths.add((Path(candidate).parent / name).as_posix())
                for rel in SCRIPT_RELATIVE.findall(text):
                    found = os.path.normpath(Path(candidate).parent / rel.rstrip("/."))
                    if found.startswith(("..", "/")) or not (root / found).exists():
                        raise Error(f"{candidate} reads $dir/{rel}, which isn't a path in the repo")
                    paths.add(Path(found).as_posix())
    lang = manifest["lang"]
    paths.update(LANE_FILES.get(lang, []))
    pins = LANE_PINS[lang]
    if manifest.get("cwasm", {}).get("enabled"):
        pins = pins + CWASM_PINS
    return sorted(paths), pins


def entry_pins(entry: dict) -> list:
    """The pins a commands.toml entry adds to its recipe's build inputs: a
    fetched recipe's (it isn't in git, so its pin stands for it), and
    `pins`, as `table.key` (a toolchain its lane doesn't otherwise use)."""
    pins = [("vars", entry["fetched"])] if "fetched" in entry else []
    for pin in entry.get("pins", []):
        table, _, key = pin.partition(".")
        if not key:
            raise Error(f"pin {pin!r} isn't `<mise.toml table>.<key>`")
        pins.append((table, key.strip('"')))
    return pins


def entry_files(root: Path, entry: dict) -> list[str]:
    """The repo files a commands.toml entry adds to its recipe's build
    inputs (`files`): ones its build uses that its recipe doesn't name, such
    as a script that writes something beside the component. Each must
    exist."""
    files = strings(entry.get("files", []))
    for f in files:
        if not (root / f).exists():
            raise Error(f"`files` names {f}, which isn't in the repo")
    return files


def pin_values(mise: dict, pins: list) -> list:
    return [mise.get(table, {}).get(key) for table, key in pins]


def cache_key(root: Path, name: str) -> str:
    """A hash of everything `name`'s built component dir (component, cwasm,
    manifest, provenance, notices, SBOM) depends on; see `cache-key`."""
    commands = load_commands((root / COMMANDS).read_text())
    keys = [key for key in recipes(root) if key[1] == name]
    if len(keys) != 1:
        raise Error(f"{name}: {len(keys)} recipes in clis/ have that name")
    key = keys[0]
    entry = commands.get(key, {})
    rel = manifest_path(key, entry)
    manifest = tomllib.loads((root / rel).read_text())
    mise = tomllib.loads((root / "mise.toml").read_text())
    paths, pins = build_inputs(root, rel, manifest)
    paths = paths + entry_files(root, entry)
    # A fetched recipe is hashed from its fetched copy, but say its pin too.
    # Every command's smoke test runs under wasmtime, and a cached cwasm
    # only loads under the one that compiled it.
    pins = pins + entry_pins(entry) + NOTICE_PINS[manifest["lang"]] + CWASM_PINS
    manual = tomllib.loads((root / "third_party" / "manual.toml").read_text())
    listed = [c for c in manual.get("component", []) if name in c.get("artifacts", [])]
    paths = set(paths) | set(NOTICE_FILES) | {f for c in listed for f in c.get("files", [])}
    files = {}
    for path in sorted(paths):
        full = root / path
        found = sorted(full.rglob("*")) if full.is_dir() else [full]
        for f in found:
            if f.is_file():
                files[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
            elif not f.exists():
                raise Error(f"{name}: build input {path} doesn't exist")
    inputs = {
        "command": [key[0], key[1], entry],
        "files": files,
        "pins": [[table, k, v] for (table, k), v in zip(pins, pin_values(mise, pins))],
        "manual": listed,
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


def multicall_names(manifest: dict) -> list[str]:
    """A multicall command's names: the cargo features its manifest builds
    (`--features a,b,c`), one per tool."""
    flags = manifest.get("build", {}).get("cargo_flags", [])
    if "--features" not in flags or flags.index("--features") + 1 == len(flags):
        raise Error(f"{manifest['name']}: multicall, but its cargo_flags have no `--features` list")
    return flags[flags.index("--features") + 1].split(",")


def recipe_url(mise: dict, entry: dict, rel: str) -> str | None:
    """Where a recipe fetched from eryx comes from, pinned."""
    if "fetched" not in entry:
        return None
    return f"{ERYX_URL}/blob/{mise['vars'][entry['fetched']]}/{rel}"


def check_versions(root: Path, base: str | None) -> list[str]:
    """Problems with commands.toml, as messages (none when it's fine)."""
    problems = []
    commands = load_commands((root / COMMANDS).read_text())
    found = recipes(root)
    for key in sorted(found.keys() - commands.keys()):
        problems.append(f"{key[0]}:{key[1]} ({found[key]}) has no entry in {COMMANDS}")

    base_commands, base_mise, changed, moved = {}, {}, set(), {}
    if base:
        base_commands = load_commands(git(root, "show", f"{base}:{COMMANDS}", check=False) or "")
        base_mise = tomllib.loads(git(root, "show", f"{base}:mise.toml", check=False) or "")
        changed, moved = changes(root, base)
        merge_base = git(root, "merge-base", base, "HEAD").strip()
    moved_from = {new: old for old, new in moved.items()}

    def only_moved(path: str) -> bool:
        """Whether `path` is its base version with only the paths in it
        moved (same_recipe), so not a change to what's built."""
        old_path = moved_from.get(path, path)
        old = git(root, "show", f"{merge_base}:{old_path}", check=False)
        new = git(root, "show", f"HEAD:{path}", check=False)
        return old is not None and new is not None and same_recipe(old, old_path, new, path, moved)
    mise = tomllib.loads((root / "mise.toml").read_text())
    lock_path = root / LOCK
    locked = read_lock(lock_path.read_text() if lock_path.exists() else "")

    for key, entry in sorted(commands.items()):
        label = f"{key[0]}:{key[1]}"
        rel = manifest_path(key, entry)
        if "fetched" in entry and entry["fetched"] not in mise.get("vars", {}):
            problems.append(f"{label}: fetched at vars.{entry['fetched']}, which mise.toml doesn't set")
            continue
        if not (root / rel).is_file():
            hint = f" (run `mise run fetch-{key[1]}`)" if "fetched" in entry else ""
            problems.append(f"{label}: its manifest {rel} doesn't exist{hint}")
            continue
        if "fetched" in entry:
            stamp = root / "scratch" / "fetched" / entry["fetched"]
            fetched = stamp.read_text().strip() if stamp.exists() else None
            if fetched != mise["vars"][entry["fetched"]]:
                problems.append(
                    f"{label}: {rel} was fetched at {fetched or 'an unknown commit'}, not "
                    f"vars.{entry['fetched']}; run `mise run fetch-{key[1]}`"
                )
                continue
        manifest = tomllib.loads((root / rel).read_text())
        if entry.get("multicall"):
            try:
                multicall_names(manifest)
            except Error as e:
                problems.append(f"{label}: {e}")
        try:
            upstream, revision = parse_version(entry.get("version", ""))
        except Error as e:
            problems.append(f"{label}: {e}")
            continue
        expected = entry.get("upstream", upstream_from_ref(key[1], manifest["source"]["ref"]))
        if upstream != expected:
            problems.append(
                f"{label}: version {entry['version']} doesn't match the upstream "
                f"{expected} in {rel} (set `upstream` if the ref can't say)"
            )
        published = sorted(locked.get(f"{key[0]}:{key[1]}", {}), key=version_key)
        if published and entry["version"] not in published and (
            version_key(entry["version"]) < version_key(published[-1])
        ):
            problems.append(
                f"{label}: version {entry['version']} is lower than the published {published[-1]}"
            )
        old = base_commands.get(key)
        if not old:
            continue
        # A new version must be new to the registry, or publish would skip it
        # as already there: going back to a published version isn't one.
        if old["version"] != entry["version"] and published and (
            version_key(entry["version"]) <= version_key(published[-1])
        ):
            problems.append(
                f"{label}: version {entry['version']} must be higher than the published "
                f"{published[-1]}"
            )
        old_upstream, _ = parse_version(old["version"])
        if old_upstream != upstream and revision != 1:
            problems.append(f"{label}: a new upstream version starts at r.1, not r.{revision}")
        try:
            paths, pins = build_inputs(root, rel, manifest)
            paths = paths + entry_files(root, entry)
            pins = pins + entry_pins(entry)
        except Error as e:
            problems.append(f"{label}: {e}")
            continue
        touched = sorted(
            path for path in changed
            if any(path == i or path.startswith(i + "/") for i in paths) and not only_moved(path)
        )
        if pin_values(base_mise, pins) != pin_values(mise, pins):
            touched.append("mise.toml (a pinned toolchain or recipe)")
        if touched and old["version"] == entry["version"]:
            problems.append(
                f"{label}: {', '.join(touched)} changed, so its version "
                f"{entry['version']} needs a new revision"
            )
    return problems


def changes(root: Path, base: str) -> tuple[set[str], dict[str, str]]:
    """What changed from `base` to HEAD (`base...HEAD`): the paths whose
    content is new, changed or gone, and every rename, old -> new. A pure
    rename (git's R100) isn't a change, so neither of its paths is in the
    first; one with edits has both. git can't pair a small file whose edit
    leaves it under half the same, so an added file is also taken for a
    deleted one with the same name, when there's only one."""
    out = git(root, "diff", "--name-status", "-z", "-M", f"{base}...HEAD") or ""
    fields = out.split("\0")
    changed, moved, added, deleted = set(), {}, [], []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status[0] in "RC":
            old, new = fields[i + 1], fields[i + 2]
            i += 3
            if status[0] == "R":
                moved[old] = new
            if status != "R100":
                changed.update({old, new} if status[0] == "R" else {new})
        else:
            changed.add(fields[i + 1])
            {"A": added, "D": deleted}.get(status[0], []).append(fields[i + 1])
            i += 2
    for new in added:
        same_name = [old for old in deleted if Path(old).name == Path(new).name]
        if len(same_name) == 1:
            moved[same_name[0]] = new
    return changed, moved


def moved_dirs(moved: dict[str, str]) -> dict[str, str]:
    """The directories a set of renames moved whole: `a/x/f` -> `b/x/f`
    moved `a/x` to `b/x`, unless another file under `a/x` went elsewhere."""
    candidates: dict[str, set[str]] = {}
    for old, new in moved.items():
        o, n = old.split("/"), new.split("/")
        i = 1
        while i < min(len(o), len(n)) and o[-i] == n[-i]:
            candidates.setdefault("/".join(o[:-i]), set()).add("/".join(n[:-i]))
            i += 1
    dirs = {}
    for old_dir, new_dirs in candidates.items():
        [new_dir] = new_dirs if len(new_dirs) == 1 else [None]
        under = [(o, n) for o, n in moved.items() if o.startswith(old_dir + "/")]
        # Not when something moved into it: then it's still there.
        if new_dir and not any(n.startswith(old_dir + "/") for n in moved.values()) and all(
            n == new_dir + o[len(old_dir):] for o, n in under
        ):
            dirs[old_dir] = new_dir
    return dirs


def rewrite_paths(text: str, moved: dict[str, str]) -> str:
    """`text` with every repo path a rename moved (and every directory it
    moved whole) spelled at its new place."""
    mapping = {**moved_dirs(moved), **moved}
    if not mapping:
        return text
    alternatives = "|".join(re.escape(p) for p in sorted(mapping, key=len, reverse=True))
    pattern = rf"(?<![\w.-])(?:{alternatives})(?![\w-]|\.\w)"
    return re.sub(pattern, lambda m: mapping[m[0]], text)


def resolve_relative(text: str, path: str) -> str:
    """`text`, a script at `path`, with each path it spells relative to its
    own directory (SCRIPT_RELATIVE) written as the repo path it names, so a
    script that moved and changed `$dir/../x` to `$dir/../../x` to keep
    naming the same file reads the same."""
    def repo_path(m):
        found = os.path.normpath(Path(path).parent / m[1])
        return m[0][:m.start(1) - m.start(0)] + "{repo}/" + Path(found).as_posix()
    return SCRIPT_RELATIVE.sub(repo_path, text)


def same_recipe(old_text: str, old_path: str, new_text: str, new_path: str,
                moved: dict[str, str]) -> bool:
    """Whether a build input at `new_path` is its base version at
    `old_path` with nothing changed but where the files it names are: old
    paths rewritten to where they moved (per `moved`), and paths relative to
    a moved script adjusted to name the same files. A manifest is compared
    as TOML data, so its comments don't count; everything else as text."""
    old = rewrite_paths(resolve_relative(old_text, old_path), moved)
    new = resolve_relative(new_text, new_path)
    if new_path.endswith(".toml"):
        try:
            return tomllib.loads(old) == tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            return False
    return old == new


def git(root: Path, *args: str, check: bool = True) -> str | None:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        if check:
            raise Error(f"git {' '.join(args)}: {result.stderr.strip()}")
        return None
    return result.stdout


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def spdx_and(expressions: list[str]) -> str:
    """Join SPDX expressions with AND, parenthesising compound ones."""
    unique = list(dict.fromkeys(e for e in expressions if e))
    if len(unique) == 1:
        return unique[0]
    return " AND ".join(f"({e})" if " " in e else e for e in unique)


def upstream_pin(source: dict) -> str:
    if source.get("commit"):
        return f"{source['url']}@{source['commit']}"
    return f"{source['url']}#sha256:{source['sha256']}"


def annotations(provenance: dict, entry: dict, key: tuple[str, str], revision: str,
                created: str, eryx_rev: str) -> dict[str, str]:
    """The manifest annotations for a component, from its provenance.

    `eryx_rev` is mise.toml's eryx commit, recorded with the eryx-build
    version until eryx-build comes from crates.io (eryx-org/eryx#997).
    """
    for field in ("eryx_build", "imports"):
        if field not in provenance:
            raise Error(
                f"{key[0]}:{key[1]}'s provenance.json has no `{field}`: build it with an "
                "eryx-build from eryx-org/eryx#1004 or later"
            )
    # The tool's licence and those of the libraries it statically links (its
    # [[deps]]). Its crates' and Go modules' licences are in its notices.
    # TODO(#4): fold those in, from the same data the notices are made from.
    licenses = [provenance["license"]] + [d["license"] for d in provenance["deps"]]
    result = {
        "org.opencontainers.image.title": f"{key[0]}:{key[1]}",
        "org.opencontainers.image.description": provenance["description"],
        "org.opencontainers.image.version": entry["version"],
        "org.opencontainers.image.source": SOURCE_URL,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.licenses": spdx_and(licenses),
        "run.eryx.command.upstream": upstream_pin(provenance["source"]),
        "run.eryx.command.eryx-build": f"{provenance['eryx_build']}+{eryx_rev}",
        "run.eryx.command.wit": ",".join(provenance["imports"]),
    }
    if entry.get("names"):
        result["run.eryx.command.names"] = ",".join(entry["names"])
    return result


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class Registry:
    """Just enough of the OCI distribution API to read manifests."""

    def __init__(self, host: str, insecure: bool):
        self.host = host
        self.base = f"{'http' if insecure else 'https'}://{host}"
        user = os.environ.get("WKG_OCI_USERNAME")
        password = os.environ.get("WKG_OCI_PASSWORD")
        self.basic = (
            base64.b64encode(f"{user}:{password}".encode()).decode() if user and password else None
        )

    def manifest(self, repository: str, tag: str) -> dict | None:
        """The manifest tagged `tag`, or None if there's none.

        "None" needs an unambiguous answer: a 404, or a 401/403 whose OCI
        error body says the repository or tag is unknown (NAME_UNKNOWN,
        MANIFEST_UNKNOWN), as some registries answer for a package that
        doesn't exist yet. Anything else, such as a bare DENIED, fails, so
        a credentials problem never reads as "not published".
        """
        url = f"{self.base}/v2/{repository}/manifests/{tag}"
        headers = {"Accept": MANIFEST_MEDIA_TYPE}
        try:
            return json.load(self._open(url, headers))
        except urllib.error.HTTPError as e:
            if e.code == 404 or (e.code in (401, 403) and unknown_name(e.read())):
                return None
            raise Error(f"GET {url}: HTTP {e.code}") from e

    def manifest_digest(self, repository: str, tag: str) -> str:
        """The digest the registry gives the manifest tagged `tag`, which
        signing (#6) and its referrers attach to."""
        url = f"{self.base}/v2/{repository}/manifests/{tag}"
        response = self._open(url, {"Accept": MANIFEST_MEDIA_TYPE}, method="HEAD")
        digest = response.headers.get("Docker-Content-Digest")
        if not digest:
            raise Error(f"HEAD {url}: no Docker-Content-Digest")
        return digest

    def _open(self, url: str, headers: dict, method: str = "GET"):
        """Open `url`, answering a bearer-token challenge if there is one."""
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method))
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if e.code != 401 or not challenge.startswith("Bearer "):
                raise
            token = self._token(challenge)
            headers = {**headers, "Authorization": f"Bearer {token}"}
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method))

    def _token(self, challenge: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        query = "&".join(f"{k}={params[k]}" for k in ("service", "scope") if k in params)
        request = urllib.request.Request(f"{params['realm']}?{query}")
        if self.basic:
            request.add_header("Authorization", f"Basic {self.basic}")
        body = json.load(urllib.request.urlopen(request))
        return body.get("token") or body["access_token"]


def unknown_name(body: bytes) -> bool:
    """Whether an OCI error body only says the name or manifest is unknown."""
    try:
        codes = {error.get("code") for error in json.loads(body).get("errors", [])}
    except (ValueError, AttributeError):
        return False
    return bool(codes) and codes <= {"NAME_UNKNOWN", "MANIFEST_UNKNOWN"}


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def publish(dirs: list[Path], registry: Registry, fail_existing: bool,
            out: Path | None = None) -> list[dict]:
    """Push each component dir, and return what's in the registry for each:
    pushed now (`pushed: true`), or already there (`pushed: false`, with the
    registry's digests). Listing existing versions too makes `lock` and
    signing idempotent: a re-run after a failure part way records and signs
    whatever the failed run pushed. `out` is rewritten after each command, so
    a failure still leaves what was done."""
    commands = load_commands((ROOT / COMMANDS).read_text())
    by_name = {}
    for key in commands:
        by_name.setdefault(key[1], []).append(key)
    revision = (git(ROOT, "rev-parse", "HEAD") or "").strip()
    mise = tomllib.loads((ROOT / "mise.toml").read_text())
    eryx_rev = mise["vars"]["eryx_rev"]
    created = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    results = []
    for directory in dirs:
        provenance = json.loads((directory / "provenance.json").read_text())
        name = provenance["name"]
        keys = by_name.get(name, [])
        if len(keys) != 1:
            raise Error(f"{directory}: {name!r} names {len(keys)} entries in {COMMANDS}")
        key = keys[0]
        entry = commands[key]
        if entry.get("multicall"):
            manifest = tomllib.loads((directory / "manifest.toml").read_text())
            entry = {**entry, "names": multicall_names(manifest)}
        repository = f"{REPOSITORY_PREFIX}/{key[0]}/{key[1]}"
        reference = f"{registry.host}/{repository}:{entry['version']}"
        component = directory / "component.wasm"
        digest = sha256_file(component)
        # A command only goes out with its third-party notices (#4) and SBOM, which
        # CI writes next to it; #6 attaches them to the manifest.
        for file in (f"{name}.notices.md", f"{name}.spdx.json"):
            if not (directory / file).is_file():
                raise Error(f"{directory}: no {file}, so {reference} can't be published")

        existing = registry.manifest(repository, entry["version"])
        if existing is not None:
            if fail_existing:
                raise Error(f"{reference} already exists: published versions are never replaced")
            layers = [layer["digest"] for layer in existing.get("layers", [])]
            if len(layers) != 1:
                raise Error(f"{reference} is already there, with layers {layers}, not one")
            print(f"skipped {reference}: already published", file=sys.stderr)
            pushed, digest = False, layers[0]
        else:
            command = ["wkg", "oci", "push"]
            if registry.base.startswith("http:"):
                command += ["--insecure", registry.host]
            notes = annotations(provenance, entry, key, revision, created, eryx_rev)
            if recipe := recipe_url(mise, entry, manifest_path(key, entry)):
                notes["run.eryx.command.recipe"] = recipe
            for k, v in notes.items():
                command += ["--annotation", f"{k}={v}"]
            command += [reference, str(component)]
            subprocess.run(command, check=True)
            layers = (registry.manifest(repository, entry["version"]) or {}).get("layers", [])
            if [layer["digest"] for layer in layers] != [digest]:
                raise Error(f"{reference}: the pushed layers {layers} aren't component.wasm ({digest})")
            print(f"pushed {reference} ({digest})", file=sys.stderr)
            pushed = True
        results.append({
            "name": f"{key[0]}:{key[1]}",
            "version": entry["version"],
            "reference": reference,
            "pushed": pushed,
            "layer_digest": digest,
            "manifest_digest": registry.manifest_digest(repository, entry["version"]),
        })
        if out:
            out.write_text(json.dumps(results, indent=2) + "\n")
    return results


# ---------------------------------------------------------------------------
# The lock file
# ---------------------------------------------------------------------------


def toml_string(value: str) -> str:
    return json.dumps(value)


def read_lock(text: str) -> dict[str, dict[str, str]]:
    """A wkg.lock's pins, as name -> version -> digest."""
    if not text.strip():
        return {}
    data = tomllib.loads(text)
    pinned = {}
    for package in data.get("packages", []):
        versions = pinned.setdefault(package["name"], {})
        for version in package.get("versions", []):
            versions[version["version"]] = version["digest"]
    return pinned


def write_lock(pinned: dict[str, dict[str, str]]) -> str:
    """Pins in wkg.lock's format (version 1), sorted by name and version."""
    lines = [
        "# Every published command version and its layer digest, in wkg.lock's",
        "# format. Written by `scripts/publish.py lock`; don't edit by hand.",
        "version = 1",
    ]
    for name in sorted(pinned):
        lines += ["", "[[packages]]", f"name = {toml_string(name)}"]
        for version in sorted(pinned[name], key=version_key):
            lines += [
                "",
                "[[packages.versions]]",
                f"requirement = {toml_string('=' + version)}",
                f"version = {toml_string(version)}",
                f"digest = {toml_string(pinned[name][version])}",
            ]
    return "\n".join(lines) + "\n"


def version_key(version: str):
    upstream, revision = parse_version(version)
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"[.+]", upstream)]
    return parts, revision


def lock(results: list[dict], lock_path: Path) -> None:
    pinned = read_lock(lock_path.read_text() if lock_path.exists() else "")
    for result in results:
        versions = pinned.setdefault(result["name"], {})
        old = versions.get(result["version"])
        if old and old != result["layer_digest"]:
            raise Error(f"{result['name']}@{result['version']} is already locked to {old}")
        versions[result["version"]] = result["layer_digest"]
    lock_path.write_text(write_lock(pinned))


# ---------------------------------------------------------------------------
# Moving a fetched recipe's pin
# ---------------------------------------------------------------------------


def raw_eryx(rev: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/eryx-org/eryx/{rev}/{path}"
    try:
        return urllib.request.urlopen(url).read().decode()
    except urllib.error.HTTPError as e:
        raise Error(f"{url}: HTTP {e.code}") from e


def newest_commit(branch: str, paths: list[str]) -> str:
    """The newest commit on eryx's `branch` that changed any of `paths`."""
    newest = None
    for path in paths:
        url = f"https://api.github.com/repos/eryx-org/eryx/commits?sha={branch}&path={path}&per_page=1"
        commits = json.load(urllib.request.urlopen(url))
        if not commits:
            raise Error(f"no commit on eryx's {branch} changed {path}")
        date = commits[0]["commit"]["committer"]["date"]
        if newest is None or date > newest[0]:
            newest = (date, commits[0]["sha"])
    return newest[1]


def bumped(version: str, upstream: str) -> str:
    old_upstream, revision = parse_version(version)
    return f"{upstream}-r.1" if upstream != old_upstream else f"{upstream}-r.{revision + 1}"


def replace_once(text: str, pattern: str, replacement: str, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        raise Error(f"couldn't find {what}")
    return new


def set_version(text: str, key: tuple[str, str], version: str) -> str:
    """commands.toml's text with `key`'s version replaced."""
    table = rf'^(\[{re.escape(key[0])}\.{re.escape(key[1])}\]\n(?:(?!\[).*\n)*?)version = "[^"]*"'
    return replace_once(text, table, rf'\g<1>version = "{version}"', f"{key[0]}:{key[1]}'s version in {COMMANDS}")


def bump(root: Path, name: str, branch: str) -> None:
    namespace, _, short = name.partition(":")
    key = (namespace, short)
    commands = load_commands((root / COMMANDS).read_text())
    entry = commands.get(key)
    if not entry or "fetched" not in entry:
        raise Error(f"{name} isn't a recipe fetched from eryx in {COMMANDS}")
    var = entry["fetched"]
    mise_text = (root / "mise.toml").read_text()
    current = tomllib.loads(mise_text)["vars"][var]

    rel = manifest_path(key, entry)
    manifest = tomllib.loads(raw_eryx(branch, rel))
    paths = [rel] + strings(manifest.get("build", {}).get("vendor_patch", []))
    rev = newest_commit(branch, paths)
    if rev == current:
        print(f"{name}: already at {rev}, the newest change on {branch}", file=sys.stderr)
        return
    url = f"https://api.github.com/repos/eryx-org/eryx/compare/{current}...{rev}"
    if json.load(urllib.request.urlopen(url))["status"] != "ahead":
        raise Error(f"{rev[:12]}, {branch}'s newest change to {rel}, doesn't follow the pin {current[:12]}")
    manifest = tomllib.loads(raw_eryx(rev, rel))
    upstream = entry.get("upstream", upstream_from_ref(short, manifest["source"]["ref"]))
    version = bumped(entry["version"], upstream)

    (root / "mise.toml").write_text(replace_once(
        mise_text, rf'^{re.escape(var)} = "[0-9a-f]+"', f'{var} = "{rev}"', f"vars.{var} in mise.toml"))
    (root / COMMANDS).write_text(set_version((root / COMMANDS).read_text(), key, version))
    print(f"{name}: {current[:12]} -> {rev[:12]}, {entry['version']} -> {version}", file=sys.stderr)
    if version.endswith("-r.1"):
        print(f"{name}: a new upstream version: update its third_party/manual.toml entry "
              "(version, url, cli_commit) and licence file by hand", file=sys.stderr)


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-versions")
    check.add_argument("--base")
    push = sub.add_parser("publish")
    push.add_argument("--registry", required=True)
    push.add_argument("--insecure", action="store_true")
    push.add_argument("--fail-existing", action="store_true")
    push.add_argument("--out", type=Path)
    push.add_argument("dirs", nargs="+", type=Path)
    locking = sub.add_parser("lock")
    locking.add_argument("--results", type=Path, required=True)
    locking.add_argument("--lock", type=Path, default=ROOT / LOCK)
    keying = sub.add_parser("cache-key")
    keying.add_argument("name")
    bumping = sub.add_parser("bump")
    bumping.add_argument("name")
    bumping.add_argument("--branch", default="multi-guest")
    args = parser.parse_args()

    try:
        if args.command == "check-versions":
            problems = check_versions(ROOT, args.base)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 1 if problems else 0
        if args.command == "publish":
            if args.out:
                args.out.write_text("[]\n")
            results = publish(
                args.dirs, Registry(args.registry, args.insecure), args.fail_existing, args.out
            )
            if not args.out:
                sys.stdout.write(json.dumps(results, indent=2) + "\n")
            return 0
        if args.command == "cache-key":
            print(cache_key(ROOT, args.name))
            return 0
        if args.command == "bump":
            bump(ROOT, args.name, args.branch)
            return 0
        lock(json.loads(args.results.read_text()), args.lock)
        return 0
    except (Error, subprocess.CalledProcessError, urllib.error.URLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
