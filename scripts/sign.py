#!/usr/bin/env python3
"""Sign published commands and attach what goes with them (#6).

    sign.py RESULTS.json COMPONENT_DIR... [--insecure] [--key KEY --pub PUB]
            [--tlog | --no-tlog]

For each command in RESULTS.json (as `publish.py publish` writes it), by its
manifest digest:

- sign it with cosign;
- attest its SLSA provenance (`cosign attest --type slsaprovenance1`);
- attach its notices (`<name>.notices.md`, from its COMPONENT_DIR) as an OCI
  referrer of type `application/vnd.eryx.notices.v1+markdown`;
- for a GPL command (namespace `eryx-gpl`), attach a source offer of type
  `application/vnd.eryx.source-offer.v1+json`: where its corresponding
  source is (this repo at the published commit, and the upstream commit),
  with this repo's recipe directory for it (manifest, patches, shims,
  NOTICE) as a tarball layer.

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
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_URL = "https://github.com/eryx-org/eryx-commands"
# The publish workflow's certificate identity and issuer, as consumers
# verify them (eryx-org/eryx#998). The publish job only runs from main.
IDENTITY = f"{SOURCE_URL}/.github/workflows/ci.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"

NOTICES_TYPE = "application/vnd.eryx.notices.v1+markdown"
SOURCE_OFFER_TYPE = "application/vnd.eryx.source-offer.v1+json"
PATCHES_MEDIA_TYPE = "application/vnd.eryx.source-offer.recipe.v1.tar+gzip"
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
    repository = env.get("GITHUB_REPOSITORY", "eryx-org/eryx-commands")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", f"{repository}/.github/workflows/ci.yml@local")
    path, _, ref = workflow_ref.removeprefix(f"{repository}/").partition("@")
    source = provenance["source"]
    upstream = {"uri": f"git+{source['url']}"}
    if source.get("commit"):
        upstream["digest"] = {"gitCommit": source["commit"]}
    elif source.get("sha256"):
        upstream["digest"] = {"sha256": source["sha256"]}
    run_id = env.get("GITHUB_RUN_ID")
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
                    f"{server}/{repository}/actions/runs/{run_id}/attempts/"
                    f"{env.get('GITHUB_RUN_ATTEMPT', '1')}"
                    if run_id else "local"
                ),
            },
            "byproducts": [{"name": result["name"], "digest": {
                "sha256": result["layer_digest"].removeprefix("sha256:"),
            }}],
        },
    }


def source_offer(result: dict, provenance: dict, revision: str, recipe: str) -> dict:
    """Where a GPL command's corresponding source is: this repo at the
    commit it was published from (its recipe directory, which the offer's
    layer also carries), and the upstream source and patches it was built
    from."""
    return {
        "command": result["name"],
        "version": result["version"],
        "license": provenance["license"],
        "source": {"repository": SOURCE_URL, "commit": revision, "recipe": recipe},
        "upstream": provenance["source"],
        "patches": provenance.get("patches", []),
        "notice": f"{recipe}/NOTICE.md",
    }


def recipe_tarball(directory: Path, arcname: str) -> bytes:
    """`directory` as a reproducible .tar.gz: sorted, no owners, no times."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(p for p in directory.rglob("*") if p.is_file()):
                info = tarfile.TarInfo(f"{arcname}/{path.relative_to(directory).as_posix()}")
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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
            self.verifying = ["--certificate-identity", IDENTITY,
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
             workdir: Path) -> None:
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
        # The commit the command was published from, which may be an
        # earlier run's when this one signs what that run pushed.
        annotations = tools.manifest(ref).get("annotations", {})
        revision = annotations.get("org.opencontainers.image.revision")
        if not revision:
            raise Error(f"{ref}: no org.opencontainers.image.revision annotation")

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

        notices = directory / f"{name}.notices.md"
        if not notices.is_file():
            raise Error(f"{directory}: no {notices.name}")
        if tools.referrers(ref, NOTICES_TYPE):
            print(f"{ref}: notices already attached", file=sys.stderr)
        else:
            tools.attach(ref, NOTICES_TYPE, [(notices, "text/markdown")])

        if namespace == GPL_NAMESPACE:
            if tools.referrers(ref, SOURCE_OFFER_TYPE):
                print(f"{ref}: source offer already attached", file=sys.stderr)
                continue
            recipe = f"clis/gpl/{name}"
            offer_dir = workdir / f"{name}-source-offer"
            offer_dir.mkdir(exist_ok=True)
            offer = offer_dir / "source-offer.json"
            offer.write_text(json.dumps(
                source_offer(result, provenance, revision, recipe), indent=2) + "\n")
            tarball = offer_dir / f"{name}-recipe.tar.gz"
            tarball.write_bytes(recipe_tarball(ROOT / recipe, f"{name}-recipe"))
            tools.attach(ref, SOURCE_OFFER_TYPE, [(tarball, PATCHES_MEDIA_TYPE)],
                         config=(offer, SOURCE_OFFER_TYPE))


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
            sign_all(results, args.dirs, tools, dict(os.environ), workdir)
        return 0
    except (Error, subprocess.CalledProcessError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
