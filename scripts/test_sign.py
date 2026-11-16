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

    def test_a_layer_this_run_didnt_build_is_not_signed(self):
        class NoTools:
            def __getattr__(self, name):
                raise AssertionError(f"called {name}")

        with tempfile.TemporaryDirectory() as tmp:
            component = Path(tmp) / "git-component"
            component.mkdir()
            (component / "component.wasm").write_bytes(b"this run's build")
            (component / "provenance.json").write_text(json.dumps(PROVENANCE))
            already = {**RESULT, "pushed": False}
            skipped = sign.sign_all([already], [component], NoTools(), ENV, Path(tmp))
        self.assertEqual(len(skipped), 1)
        self.assertIn("wasn't signed", skipped[0])


if __name__ == "__main__":
    unittest.main()
