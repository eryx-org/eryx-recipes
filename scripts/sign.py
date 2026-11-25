#!/usr/bin/env python3
"""Sign published commands and attach what goes with them (#6).

    sign.py RESULTS.json COMPONENT_DIR... [--insecure] [--key KEY --pub PUB]
            [--tlog | --no-tlog]

For each command in RESULTS.json (as `publish.py publish` writes it), by its
manifest digest:

- sign it with cosign;
- attest its SLSA provenance (`cosign attest --type slsaprovenance1`);
- attach its notices (`<name>.notices.md`, from its COMPONENT_DIR) as an OCI
  referrer of type `application/vnd.eryx.notices.v1+markdown`, and its SPDX
  SBOM (`<name>.spdx.json`) as one of type `application/spdx+json`;
- for a GPL command (namespace `eryx-gpl`), attach its complete
  corresponding source as a referrer of type
  `application/vnd.eryx.source-offer.v1+json` (see source_offer), so it sits
  next to the binary, in the same place (GPLv2 section 3's "equivalent
  access").

A command is only signed when its layer is this run's component.wasm: a
version that was already in the registry with a different layer (a re-run
that rebuilt it) is skipped and reported, since signing it would vouch for
a build this run didn't make.

Each step is skipped when the registry already has it (a signature or
attestation that verifies, a referrer of the type), so a re-run after a
failure part way finishes what's missing and never adds duplicates.

Signing is keyless by default: the certificate is issued to this repo's
publish workflow ([IDENTITY], [ISSUER]), which is what consumers verify. It
then only works inside that GitHub Actions run. --key/--pub sign with a key
pair instead, with no transparency log, which CI's dry run uses against its
throwaway registry. --no-tlog signs keyless without uploading to the
transparency log (see the README for what that means for verification).

Registry credentials are cosign's and oras's own: run `oras login` first.
Only the standard library is used, plus the cosign and oras binaries.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

import publish

ROOT = Path(__file__).resolve().parent.parent

SOURCE_URL = "https://github.com/eryx-org/eryx-recipes"
# The publish workflow's certificate identity and issuer, as consumers
# verify them (eryx-org/eryx#998). The publish job only runs from main.
IDENTITY = f"{SOURCE_URL}/.github/workflows/ci.yml@refs/heads/main"
# What counts as already signed: that identity, or the same workflow under
# the repo's old name (eryx-commands), which signed everything published
# before the rename
IDENTITY_PATTERN = r"^https://github\.com/eryx-org/eryx-(commands|recipes)/\.github/workflows/ci\.yml@refs/heads/main$"
ISSUER = "https://token.actions.githubusercontent.com"

NOTICES_TYPE = "application/vnd.eryx.notices.v1+markdown"
SBOM_TYPE = "application/spdx+json"
SOURCE_OFFER_TYPE = "application/vnd.eryx.source-offer.v1+json"
RECIPE_MEDIA_TYPE = "application/vnd.eryx.source-offer.recipe.v1.tar+gzip"
UPSTREAM_MEDIA_TYPE = "application/vnd.eryx.source-offer.upstream.v1.tar+gzip"
# GitHub Actions' SLSA build type: what the workflow's parameters mean.
BUILD_TYPE = "https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1"
GPL_NAMESPACE = "eryx-gpl"


class Error(Exception):
    pass


def by_digest(result: dict) -> str:
    """`registry/repository@sha256:...` for a result's manifest."""
    repository = result["reference"].rsplit(":", 1)[0]
    return f"{repository}@{result['manifest_digest']}"


def provenance_predicate(result: dict, provenance: dict, revision: str, env: dict) -> dict:
    """A SLSA v1 provenance predicate for a published command: built from
    this repo at `revision` by the GitHub Actions run in `env`, from its
    upstream source and patches as its provenance.json records them."""
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repository = env.get("GITHUB_REPOSITORY", "eryx-org/eryx-recipes")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", f"{repository}/.github/workflows/ci.yml@local")
    path, _, ref = workflow_ref.removeprefix(f"{repository}/").partition("@")
    source = provenance["source"]
    upstream = {"uri": f"git+{source['url']}"}
    if source.get("commit"):
        upstream["digest"] = {"gitCommit": source["commit"]}
    elif source.get("sha256"):
        upstream["digest"] = {"sha256": source["sha256"]}
    run_id = env.get("GITHUB_RUN_ID")
    attempt = f"/attempts/{env.get('GITHUB_RUN_ATTEMPT', '1')}"
    # Signing an earlier run's publish (ci.yml's sign_run_id): that run built it.
    if env.get("ERYX_BUILD_RUN_ID"):
        run_id, attempt = env["ERYX_BUILD_RUN_ID"], ""
    return {
        "buildDefinition": {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "workflow": {"ref": ref, "repository": f"{server}/{repository}", "path": path},
            },
            "internalParameters": {
                "github": {
                    "event_name": env.get("GITHUB_EVENT_NAME"),
                    "repository_id": env.get("GITHUB_REPOSITORY_ID"),
                    "repository_owner_id": env.get("GITHUB_REPOSITORY_OWNER_ID"),
                    "runner_environment": env.get("RUNNER_ENVIRONMENT"),
                },
            },
            "resolvedDependencies": [
                {
                    "uri": f"git+{server}/{repository}@{ref}",
                    "digest": {"gitCommit": revision},
                },
                upstream,
                *[
                    {"uri": f"git+{server}/{repository}/{p['path']}@{revision}",
                     "digest": {"sha256": p["sha256"]}}
                    for p in provenance.get("patches", [])
                ],
            ],
        },
        "runDetails": {
            "builder": {"id": f"{server}/{workflow_ref}"},
            "metadata": {
                "invocationId": (
                    f"{server}/{repository}/actions/runs/{run_id}{attempt}"
                    if run_id else "local"
                ),
            },
            "byproducts": [{"name": result["name"], "digest": {
                "sha256": result["layer_digest"].removeprefix("sha256:"),
            }}],
        },
    }


def git(*args: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE).stdout


def sha256_file(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def source_inputs(key: tuple[str, str], revision: str, workdir: Path) -> dict:
    """What command `key` was built from at `revision` of this repo: its
    recipe's build inputs (publish.build_inputs: the manifest, its patches,
    shims and WIT, its lane's files) and its lane's mise.toml pins, read from
    that commit, not the working tree."""
    tree = workdir / f"{key[1]}-tree-{revision}"
    tree.mkdir()
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", revision))) as tar:
        tar.extractall(tree, filter="data")
    entry = publish.load_commands((tree / publish.COMMANDS).read_text())[key]
    rel = publish.manifest_path(key, entry)
    manifest = tomllib.loads((tree / rel).read_text())
    mise = tomllib.loads((tree / "mise.toml").read_text())
    paths, pins = publish.build_inputs(tree, rel, manifest)
    return {
        "recipe": rel,
        "files": paths,
        "pins": {f"{table}.{name}": value
                 for (table, name), value in zip(pins, publish.pin_values(mise, pins))},
        "manifest": manifest,
    }


def fetch_upstream(source: dict, prefix: str, out: Path) -> dict:
    """An upstream source as a .tar.gz at `out`: a git repo's tree at its
    pinned commit (`git archive`), or an archive checked against its pinned
    sha256. Returns where it came from."""
    if source.get("commit"):
        # A command's own [source] names its repo `repo`; a git [[deps]]
        # entry names it `url`.
        remote = source.get("repo") or source.get("url")
        repo = out.parent / f"{prefix}.git"
        git("init", "-q", "--bare", str(repo))
        git("fetch", "-q", "--depth", "1", remote, source["commit"], cwd=repo)
        git("archive", "--format=tar.gz", f"--prefix={prefix}/", "-o", str(out), source["commit"],
            cwd=repo)
        return {"repo": remote, "ref": source.get("ref"), "commit": source["commit"]}
    url, sha256 = source.get("url") or source.get("repo"), source.get("sha256")
    if not (url and sha256):
        raise Error(f"{prefix}: pinned by neither commit nor url and sha256: {source}")
    with urllib.request.urlopen(url) as response, out.open("wb") as f:
        while chunk := response.read(1 << 20):
            f.write(chunk)
    if sha256_file(out) != sha256:
        raise Error(f"{url}: sha256 {sha256_file(out)}, not the pinned {sha256}")
    return {"url": url, "ref": source.get("ref"), "sha256": sha256}


def source_offer(result: dict, provenance: dict, revision: str, workdir: Path
                 ) -> tuple[Path, list[tuple[Path, str]]]:
    """A GPL command's complete corresponding source, as the source offer's
    config and layers:

    - `<name>-recipe.tar.gz`: every build input from this repo at `revision`
      (`git archive`), with BUILD-INPUTS.json: the upstream source and
      [[deps]] pins, the lane's tool pins (eryx_rev, the WASI SDK, ...),
      eryx-build's version, and the patches with their digests;
    - one `.tar.gz` per upstream source: the tool's own at its pinned
      commit, and each statically linked [[deps]], fetched at its pin.

    The config (source-offer.json) lists all of it, so the referrer is
    enough on its own: it doesn't rely on this repo, or upstream's hosting,
    staying reachable."""
    namespace, name = result["name"].split(":", 1)
    inputs = source_inputs((namespace, name), revision, workdir)
    manifest = inputs.pop("manifest")
    offer_dir = workdir / f"{name}-source-offer"
    offer_dir.mkdir()

    upstreams = [(f"{name}-{manifest['source']['ref']}", manifest["source"])]
    upstreams += [(f"{d['name']}-{d['ref']}", d) for d in manifest.get("deps", [])]
    layers, upstream = [], []
    for prefix, source in upstreams:
        archive = offer_dir / f"{prefix}.tar.gz"
        upstream.append({"file": archive.name, **fetch_upstream(source, prefix, archive),
                         "license": source.get("license", manifest["license"]),
                         "sha256_of_file": sha256_file(archive)})
        layers.append((archive, UPSTREAM_MEDIA_TYPE))

    build_inputs = {
        "command": result["name"],
        "version": result["version"],
        "license": provenance["license"],
        "repository": SOURCE_URL,
        "commit": revision,
        **inputs,
        "eryx_build": provenance.get("eryx_build"),
        "upstream": upstream,
        "patches": provenance.get("patches", []),
    }
    recipe = offer_dir / f"{name}-recipe.tar.gz"
    git("archive", "--format=tar.gz", f"--prefix={name}-recipe/",
        # A virtual file's path doesn't take the prefix.
        f"--add-virtual-file={name}-recipe/BUILD-INPUTS.json:{json.dumps(build_inputs, indent=2)}\n",
        "-o", str(recipe), revision, "--", *inputs["files"])
    layers.insert(0, (recipe, RECIPE_MEDIA_TYPE))

    offer = offer_dir / "source-offer.json"
    offer.write_text(json.dumps({
        **build_inputs,
        "recipe_archive": {"file": recipe.name, "sha256_of_file": sha256_file(recipe)},
        "notice": f"{inputs['recipe'].rsplit('/', 1)[0]}/NOTICE.md",
    }, indent=2) + "\n")
    return offer, layers


class Tools:
    """cosign and oras, with the flags this run signs and verifies with."""

    def __init__(self, insecure: bool, key: Path | None, pub: Path | None, tlog: bool,
                 workdir: Path):
        self.insecure = insecure
        self.key, self.pub, self.tlog = key, pub, tlog
        self.registry_flags = (
            ["--allow-insecure-registry", "--allow-http-registry"] if insecure else []
        )
        self.oras_flags = ["--plain-http"] if insecure else []
        self.signing = []
        if key:
            # A key, and no services at all: nothing leaves the machine.
            config = workdir / "signing-config.json"
            self.run(["cosign", "signing-config", "create", "--out", str(config)])
            self.signing = ["--key", str(key), "--signing-config", str(config)]
        elif not tlog:
            # Sigstore's services (Fulcio, the timestamp authority) without
            # the transparency log.
            config = workdir / "signing-config.json"
            self.run(["cosign", "signing-config", "create", "--with-default-services",
                      "--out", str(config)])
            data = json.loads(config.read_text())
            data.pop("rekorTlogUrls", None)
            config.write_text(json.dumps(data))
            self.signing = ["--signing-config", str(config)]
        if key:
            self.verifying = ["--key", str(pub), "--insecure-ignore-tlog"]
        else:
            self.verifying = ["--certificate-identity-regexp", IDENTITY_PATTERN,
                              "--certificate-oidc-issuer", ISSUER]
            if not tlog:
                self.verifying.append("--insecure-ignore-tlog")

    @staticmethod
    def run(command: list[str], check: bool = True, cwd: Path | None = None,
            quiet: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(command, check=check, cwd=cwd, text=True,
                              capture_output=quiet)

    def signed(self, ref: str) -> bool:
        return self.run(["cosign", "verify", *self.verifying, *self.registry_flags, ref],
                        check=False, quiet=True).returncode == 0

    def sign(self, ref: str) -> None:
        self.run(["cosign", "sign", "--yes", *self.signing, *self.registry_flags, ref])

    def attested(self, ref: str) -> bool:
        return self.run(["cosign", "verify-attestation", "--type", "slsaprovenance1",
                         *self.verifying, *self.registry_flags, ref],
                        check=False, quiet=True).returncode == 0

    def attest(self, ref: str, predicate: Path) -> None:
        self.run(["cosign", "attest", "--yes", "--type", "slsaprovenance1",
                  "--predicate", str(predicate), *self.signing, *self.registry_flags, ref])

    def referrers(self, ref: str, artifact_type: str) -> list[dict]:
        out = self.run(["oras", "discover", *self.oras_flags, "--format", "json",
                        "--artifact-type", artifact_type, ref], quiet=True).stdout
        return json.loads(out).get("referrers") or []

    def attach(self, ref: str, artifact_type: str, files: list[tuple[Path, str]],
               config: tuple[Path, str] | None = None) -> None:
        """Attach `files` (path, media type) as one referrer of `ref`, from
        their directory so oras records bare file names."""
        directory = files[0][0].parent
        command = ["oras", "attach", *self.oras_flags, "--artifact-type", artifact_type]
        if config:
            command += ["--config", f"{config[0].name}:{config[1]}"]
        command += [ref, *[f"{path.name}:{media}" for path, media in files]]
        self.run(command, cwd=directory)

    def manifest(self, ref: str) -> dict:
        out = self.run(["oras", "manifest", "fetch", *self.oras_flags, ref], quiet=True).stdout
        return json.loads(out)


def sign_all(results: list[dict], dirs: list[Path], tools: Tools, env: dict,
             workdir: Path) -> list[str]:
    """Sign and attach each result; returns the ones skipped, as messages."""
    skipped = []
    components = {}
    for directory in dirs:
        provenance = json.loads((directory / "provenance.json").read_text())
        components[provenance["name"]] = (directory, provenance)
    for result in results:
        namespace, name = result["name"].split(":", 1)
        if name not in components:
            raise Error(f"{result['name']}: no component dir among {[str(d) for d in dirs]}")
        directory, provenance = components[name]
        ref = by_digest(result)
        built = "sha256:" + sha256_file(directory / "component.wasm")
        label = f"{result['name']} {result['version']} ({ref})"
        # The manifest that's signed, by digest: its layer, not the results'
        # (read by tag, so a tag moved in between would differ), must be
        # this run's build. One already published from another build isn't
        # described by this run's notices, SBOM or provenance either.
        manifest = tools.manifest(ref)
        layers = [layer["digest"] for layer in manifest.get("layers", [])]
        if layers != [built]:
            skipped.append(f"{label}: its layers are {layers}, but this run built {built}, so "
                           "it wasn't signed. Re-run the failed jobs of the run that published "
                           "it (they reuse its build), or publish a new version.")
            continue
        # The commit the command was published from, which may be an
        # earlier run's when this one signs what that run pushed.
        revision = manifest.get("annotations", {}).get("org.opencontainers.image.revision")
        if not revision:
            raise Error(f"{ref}: no org.opencontainers.image.revision annotation")
        # One this run didn't push must name a commit of this branch, so a
        # manifest pushed by something else, with a made-up revision, isn't
        # signed.
        if not result["pushed"] and subprocess.run(
                ["git", "merge-base", "--is-ancestor", revision, "HEAD"], cwd=ROOT,
                stderr=subprocess.DEVNULL).returncode != 0:
            skipped.append(f"{label}: it was already in the registry, and its revision "
                           f"{revision} isn't a commit of this branch, so it wasn't signed.")
            continue

        if tools.signed(ref):
            print(f"{ref}: already signed", file=sys.stderr)
        else:
            tools.sign(ref)

        if tools.attested(ref):
            print(f"{ref}: provenance already attested", file=sys.stderr)
        else:
            predicate = workdir / f"{name}.provenance.json"
            predicate.write_text(json.dumps(
                provenance_predicate(result, provenance, revision, env), indent=2))
            tools.attest(ref, predicate)

        for what, path, artifact_type, media_type in [
            ("notices", directory / f"{name}.notices.md", NOTICES_TYPE, "text/markdown"),
            ("SBOM", directory / f"{name}.spdx.json", SBOM_TYPE, SBOM_TYPE),
        ]:
            if not path.is_file():
                raise Error(f"{directory}: no {path.name}")
            if tools.referrers(ref, artifact_type):
                print(f"{ref}: {what} already attached", file=sys.stderr)
            else:
                tools.attach(ref, artifact_type, [(path, media_type)])

        if namespace == GPL_NAMESPACE:
            if tools.referrers(ref, SOURCE_OFFER_TYPE):
                print(f"{ref}: source offer already attached", file=sys.stderr)
                continue
            offer, layers = source_offer(result, provenance, revision, workdir)
            tools.attach(ref, SOURCE_OFFER_TYPE, layers, config=(offer, SOURCE_OFFER_TYPE))
    return skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("results", type=Path)
    parser.add_argument("dirs", type=Path, nargs="+")
    parser.add_argument("--insecure", action="store_true", help="plain-HTTP registry")
    parser.add_argument("--key", type=Path, help="sign with this cosign key, not keyless")
    parser.add_argument("--pub", type=Path, help="the --key's public key, to verify with")
    parser.add_argument("--tlog", action=argparse.BooleanOptionalAction, default=True,
                        help="upload keyless signatures to the transparency log")
    args = parser.parse_args()
    if bool(args.key) != bool(args.pub):
        parser.error("--key and --pub go together")
    results = json.loads(args.results.read_text())
    if not args.key and os.environ.get("GITHUB_WORKFLOW_REF"):
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        if f"{server}/{os.environ['GITHUB_WORKFLOW_REF']}" != IDENTITY:
            print(f"error: keyless signing here would certify "
                  f"{server}/{os.environ['GITHUB_WORKFLOW_REF']}, not {IDENTITY}",
                  file=sys.stderr)
            return 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            tools = Tools(args.insecure, args.key, args.pub, args.tlog, workdir)
            skipped = sign_all(results, args.dirs, tools, dict(os.environ), workdir)
    except (Error, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for message in skipped:
        print(f"::warning title=Not signed::{message}")
    if skipped and (summary := os.environ.get("GITHUB_STEP_SUMMARY")):
        with open(summary, "a") as f:
            f.write("## Not signed\n\n" + "".join(f"- {m}\n" for m in skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
