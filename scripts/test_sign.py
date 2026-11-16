"""Tests for sign.py: `python3 -m unittest discover -s scripts`."""

import json
import tempfile
import unittest
from pathlib import Path

import publish
import sign

RESULT = {
    "name": "eryx-gpl:git",
    "version": "2.53.0-r.1",
    "reference": "ghcr.io/eryx-org/commands/eryx-gpl/git:2.53.0-r.1",
    "pushed": True,
    "layer_digest": "sha256:" + "a" * 64,
    "manifest_digest": "sha256:" + "b" * 64,
}
PROVENANCE = {
    "name": "git",
    "license": "GPL-2.0-only",
    "source": {"url": "https://github.com/git/git.git", "commit": "c0ffee", "sha256": None},
    "patches": [{"path": "clis/gpl/git/patches/0001.patch", "sha256": "d" * 64}],
}
ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "eryx-org/eryx-commands",
    "GITHUB_WORKFLOW_REF": "eryx-org/eryx-commands/.github/workflows/ci.yml@refs/heads/main",
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "2",
}


class Sign(unittest.TestCase):
    def test_identity_is_this_repo(self):
        self.assertEqual(sign.SOURCE_URL, publish.SOURCE_URL)
        self.assertEqual(sign.IDENTITY, f"https://github.com/{ENV['GITHUB_WORKFLOW_REF']}")

    def test_by_digest(self):
        self.assertEqual(sign.by_digest(RESULT),
                         "ghcr.io/eryx-org/commands/eryx-gpl/git@sha256:" + "b" * 64)

    def test_provenance(self):
        p = sign.provenance_predicate(RESULT, PROVENANCE, "1234", ENV)
        self.assertEqual(p["runDetails"]["builder"]["id"], sign.IDENTITY)
        self.assertEqual(p["runDetails"]["metadata"]["invocationId"],
                         "https://github.com/eryx-org/eryx-commands/actions/runs/42/attempts/2")
        self.assertEqual(p["buildDefinition"]["externalParameters"]["workflow"],
                         {"ref": "refs/heads/main", "path": ".github/workflows/ci.yml",
                          "repository": "https://github.com/eryx-org/eryx-commands"})
        deps = p["buildDefinition"]["resolvedDependencies"]
        self.assertEqual([d["digest"] for d in deps],
                         [{"gitCommit": "1234"}, {"gitCommit": "c0ffee"}, {"sha256": "d" * 64}])
        self.assertEqual(p["runDetails"]["byproducts"][0]["digest"], {"sha256": "a" * 64})

    def test_source_inputs_are_read_from_the_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = sign.source_inputs(("eryx-gpl", "git"), "HEAD", Path(tmp))
        self.assertEqual(inputs["recipe"], "clis/gpl/git/git.toml")
        for path in ["clis/gpl/git/git.toml", "clis/gpl/git/patches/git-wasi.patch",
                     "clis/gpl/git/shims/wasi-git-stubs.c", "wit/process.wit"]:
            self.assertIn(path, inputs["files"])
        self.assertIn("vars.eryx_rev", inputs["pins"])
        self.assertIn("vars.wasi_sdk_version", inputs["pins"])
        self.assertEqual(inputs["manifest"]["deps"][0]["name"], "zlib")

    def skipped(self, result, layers, revision):
        """sign_all over one result whose manifest (by digest) has `layers`
        and `revision`, with a tool set that fails on anything but reading
        it: the messages for what it skipped."""
        class ReadOnly:
            def manifest(self, ref):
                return {"layers": [{"digest": d} for d in layers],
                        "annotations": {"org.opencontainers.image.revision": revision}}

            def __getattr__(self, name):
                raise AssertionError(f"called {name}")

        with tempfile.TemporaryDirectory() as tmp:
            component = Path(tmp) / "git-component"
            component.mkdir()
            (component / "component.wasm").write_bytes(b"this run's build")
            (component / "provenance.json").write_text(json.dumps(PROVENANCE))
            built = "sha256:" + sign.sha256_file(component / "component.wasm")
            layers = [built if d == "built" else d for d in layers]
            # The results say this run's build either way: only the
            # manifest that would be signed counts.
            return sign.sign_all([{**result, "layer_digest": built}], [component], ReadOnly(),
                                 ENV, Path(tmp))

    def test_a_manifest_without_this_runs_layer_is_not_signed(self):
        [message] = self.skipped(RESULT, ["sha256:" + "c" * 64], "HEAD")
        self.assertIn("wasn't signed", message)

    def test_an_existing_manifest_needs_a_revision_on_this_branch(self):
        [message] = self.skipped({**RESULT, "pushed": False}, ["built"], "0" * 40)
        self.assertIn("isn't a commit of this branch", message)

    def test_this_runs_build_at_a_commit_of_this_branch_is_signed(self):
        with self.assertRaisesRegex(AssertionError, "called signed"):
            self.skipped({**RESULT, "pushed": False}, ["built"], "HEAD")


if __name__ == "__main__":
    unittest.main()
