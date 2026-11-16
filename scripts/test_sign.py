"""Tests for sign.py: `python3 -m unittest discover -s scripts`."""

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

    def test_source_offer(self):
        offer = sign.source_offer(RESULT, PROVENANCE, "1234", "clis/gpl/git")
        self.assertEqual(offer["source"], {"repository": sign.SOURCE_URL, "commit": "1234",
                                           "recipe": "clis/gpl/git"})
        self.assertEqual(offer["upstream"]["commit"], "c0ffee")

    def test_recipe_tarball_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "patches").mkdir()
            (root / "git.toml").write_text("x")
            (root / "patches" / "0001.patch").write_text("y")
            first = sign.recipe_tarball(root, "git-recipe")
            (root / "git.toml").touch()
            self.assertEqual(first, sign.recipe_tarball(root, "git-recipe"))


if __name__ == "__main__":
    unittest.main()
