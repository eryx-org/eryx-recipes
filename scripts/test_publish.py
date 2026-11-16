"""Tests for publish.py: `python3 -m unittest discover -s scripts`."""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import publish

PROVENANCE = {
    "format": 1,
    "eryx_build": "0.15.1",
    "name": "curl",
    "description": "curl",
    "license": "curl",
    "lang": "c",
    "source": {"url": "https://github.com/curl/curl.git", "commit": "abc", "sha256": None},
    "deps": [
        {"name": "mbedtls", "license": "Apache-2.0 OR GPL-2.0-or-later"},
        {"name": "zlib", "license": "Zlib"},
        {"name": "again", "license": "curl"},
    ],
    "imports": ["wasi:cli@0.2.6", "wasi:sockets@0.2.6"],
}


class Versions(unittest.TestCase):
    def test_upstream_from_ref(self):
        self.assertEqual(publish.upstream_from_ref("sd", "v1.1.0"), "1.1.0")
        self.assertEqual(publish.upstream_from_ref("curl", "curl-8_20_0"), "8.20.0")
        self.assertEqual(publish.upstream_from_ref("rg", "15.1.0"), "15.1.0")

    def test_parse_version(self):
        self.assertEqual(publish.parse_version("15.1.0-r.2"), ("15.1.0", 2))
        for bad in ["15.1.0", "15.1.0-r2", "15.1.0-r.0", "15.1.0-r.x", "-r.1", "1.2-r.1",
                    "01.2.3-r.1", "1.2.3a-r.1"]:
            with self.assertRaises(publish.Error, msg=bad):
                publish.parse_version(bad)

    def test_revisions_sort_numerically(self):
        versions = ["1.10.0-r.1", "1.2.0-r.10", "1.2.0-r.2", "1.2.0-r.1"]
        self.assertEqual(
            sorted(versions, key=publish.version_key),
            ["1.2.0-r.1", "1.2.0-r.2", "1.2.0-r.10", "1.10.0-r.1"],
        )


class Annotations(unittest.TestCase):
    def test_from_provenance(self):
        entry = {"version": "8.20.0-r.1", "names": ["a", "b"]}
        got = publish.annotations(
            PROVENANCE, entry, ("eryx", "curl"), "deadbeef", "2026-11-16T00:00:00Z", "abc123"
        )
        self.assertEqual(got["org.opencontainers.image.title"], "eryx:curl")
        self.assertEqual(got["org.opencontainers.image.version"], "8.20.0-r.1")
        self.assertEqual(got["org.opencontainers.image.revision"], "deadbeef")
        self.assertEqual(
            got["org.opencontainers.image.licenses"],
            "curl AND (Apache-2.0 OR GPL-2.0-or-later) AND Zlib",
        )
        self.assertEqual(got["run.eryx.command.upstream"], "https://github.com/curl/curl.git@abc")
        self.assertEqual(got["run.eryx.command.eryx-build"], "0.15.1+abc123")
        self.assertEqual(got["run.eryx.command.wit"], "wasi:cli@0.2.6,wasi:sockets@0.2.6")
        self.assertEqual(got["run.eryx.command.names"], "a,b")

    def test_archive_upstream_and_no_names(self):
        provenance = {
            **PROVENANCE,
            "source": {"url": "https://x/y.zip", "commit": None, "sha256": "ff"},
            "deps": [],
        }
        got = publish.annotations(provenance, {"version": "1.0.0-r.1"}, ("eryx", "y"), "r", "t", "e")
        self.assertEqual(got["run.eryx.command.upstream"], "https://x/y.zip#sha256:ff")
        self.assertEqual(got["org.opencontainers.image.licenses"], "curl")
        self.assertNotIn("run.eryx.command.names", got)

    def test_provenance_must_have_the_eryx_build_fields(self):
        for field in ["eryx_build", "imports"]:
            provenance = {k: v for k, v in PROVENANCE.items() if k != field}
            with self.assertRaises(publish.Error, msg=field):
                publish.annotations(provenance, {"version": "1.0.0-r.1"}, ("eryx", "y"), "r", "t", "e")


class FakeRegistry:
    """A registry that already has some versions."""

    host = "registry.test"
    base = "https://registry.test"

    def __init__(self, existing):
        self.existing = existing

    def manifest(self, repository, tag):
        layer = self.existing.get((repository, tag))
        return {"layers": [{"digest": layer}]} if layer else None

    def manifest_digest(self, repository, tag):
        return f"sha256:manifest-of-{tag}"


class Publish(unittest.TestCase):
    def test_existing_versions_are_listed_not_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            component = Path(tmp) / "sd-component"
            component.mkdir()
            (component / "component.wasm").write_bytes(b"local build")
            (component / "provenance.json").write_text('{"name": "sd"}')
            (component / "sd.notices.md").write_text("notices")
            version = publish.load_commands((publish.ROOT / "commands.toml").read_text())[("eryx", "sd")]["version"]
            registry = FakeRegistry({("eryx-org/commands/eryx/sd", version): "sha256:published"})
            out = Path(tmp) / "results.json"
            [result] = publish.publish([component], registry, fail_existing=False, out=out)
            self.assertEqual(result["pushed"], False)
            # The registry's digests, not this build's.
            self.assertEqual(result["layer_digest"], "sha256:published")
            self.assertEqual(result["manifest_digest"], f"sha256:manifest-of-{version}")
            self.assertIn('"pushed": false', out.read_text())
            with self.assertRaises(publish.Error):
                publish.publish([component], registry, fail_existing=True)

    def test_a_command_needs_its_notices(self):
        with tempfile.TemporaryDirectory() as tmp:
            component = Path(tmp) / "sd-component"
            component.mkdir()
            (component / "component.wasm").write_bytes(b"x")
            (component / "provenance.json").write_text('{"name": "sd"}')
            with self.assertRaisesRegex(publish.Error, "notices"):
                publish.publish([component], FakeRegistry({}), fail_existing=False)

    def test_only_unknown_names_count_as_absent(self):
        self.assertTrue(publish.unknown_name(b'{"errors":[{"code":"NAME_UNKNOWN"}]}'))
        self.assertTrue(publish.unknown_name(b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'))
        self.assertFalse(publish.unknown_name(b'{"errors":[{"code":"DENIED"}]}'))
        self.assertFalse(publish.unknown_name(b'{"errors":[{"code":"NAME_UNKNOWN"},{"code":"DENIED"}]}'))
        self.assertFalse(publish.unknown_name(b'{"errors":[]}'))
        self.assertFalse(publish.unknown_name(b"<html>"))


class Lock(unittest.TestCase):
    def test_round_trip_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "commands.lock"
            publish.lock([{"name": "eryx:sd", "version": "1.1.0-r.2", "layer_digest": "sha256:b"}], path)
            publish.lock([
                {"name": "eryx:sd", "version": "1.1.0-r.10", "layer_digest": "sha256:c"},
                {"name": "eryx:rg", "version": "15.1.0-r.1", "layer_digest": "sha256:a"},
                {"name": "eryx:sd", "version": "1.1.0-r.2", "layer_digest": "sha256:b"},
            ], path)
            text = path.read_text()
            self.assertEqual(
                publish.read_lock(text),
                {"eryx:rg": {"15.1.0-r.1": "sha256:a"},
                 "eryx:sd": {"1.1.0-r.2": "sha256:b", "1.1.0-r.10": "sha256:c"}},
            )
            self.assertLess(text.index("eryx:rg"), text.index("eryx:sd"))
            self.assertLess(text.index('"1.1.0-r.2"'), text.index('"1.1.0-r.10"'))
            self.assertIn('requirement = "=1.1.0-r.2"', text)
            # A locked version never changes digest.
            with self.assertRaises(publish.Error):
                publish.lock([{"name": "eryx:sd", "version": "1.1.0-r.2", "layer_digest": "sha256:x"}], path)


class CheckVersions(unittest.TestCase):
    """check_versions on a scratch git repo: a base commit, then changes."""

    MISE = 'vars = { eryx_rev = "%s", wasi_sdk_version = "wasi-sdk-33" }\n[tools]\nrust = "1.100.0"\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write("mise.toml", self.MISE % "aaa")
        self.write("clis/sd.toml", """
            name = "sd"
            license = "MIT"
            lang = "rust"
            [source]
            ref = "v1.1.0"
            dir = "scratch/sd"
            [build]
            vendor_patch = "clis/patches/patch-sd.sh"
        """)
        self.write("clis/curl.toml", """
            name = "curl"
            license = "curl"
            lang = "c"
            [source]
            ref = "curl-8_20_0"
        """)
        self.write("clis/patches/patch-sd.sh", "echo 1\n")
        self.write("commands.toml", """
            [eryx.sd]
            version = "1.1.0-r.1"
            [eryx.curl]
            version = "8.20.0-r.1"
        """)
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")

    def problems(self):
        return publish.check_versions(self.root, self.base)

    def set_version(self, name, version):
        text = (self.root / "commands.toml").read_text()
        old = publish.load_commands(text)[("eryx", name)]["version"]
        self.write("commands.toml", text.replace(f'"{old}"', f'"{version}"'))

    def test_unchanged_is_fine(self):
        self.assertEqual(self.problems(), [])

    def test_a_patch_change_needs_a_new_revision(self):
        self.write("clis/patches/patch-sd.sh", "echo 2\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("eryx:sd", problem)
        self.assertIn("clis/patches/patch-sd.sh", problem)
        self.set_version("sd", "1.1.0-r.2")
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_toolchain_pin_needs_a_new_revision_for_every_lane_using_it(self):
        self.write("mise.toml", self.MISE % "bbb")
        self.commit()
        self.assertEqual(len(self.problems()), 2)

    def test_a_new_upstream_restarts_at_r1(self):
        text = (self.root / "clis/sd.toml").read_text().replace('"v1.1.0"', '"v1.2.0"')
        self.write("clis/sd.toml", text)
        self.set_version("sd", "1.2.0-r.2")
        self.commit()
        [problem] = self.problems()
        self.assertIn("starts at r.1", problem)
        self.set_version("sd", "1.2.0-r.1")
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_recipe_dirs_files_and_a_scripts_siblings_are_inputs(self):
        self.write("clis/gpl/git/git.toml", """
            name = "git"
            license = "GPL-2.0-only"
            lang = "c"
            [source]
            ref = "v2.56.1"
            [build]
            vendor_patch = "clis/gpl/git/patches/patch-git.sh"
            cflags = ["-include", "{repo}/clis/gpl/git/shims/wasi-git.h"]
        """)
        self.write("clis/gpl/git/patches/patch-git.sh", 'git apply "$(dirname "$0")/git-wasi.patch"\n')
        self.write("clis/gpl/git/patches/git-wasi.patch", "a\n")
        self.write("clis/gpl/git/shims/wasi-git.h", "a\n")
        self.write("clis/patches/patch-curl.sh", "sed -i x lib/cf-socket.c\n")
        text = (self.root / "commands.toml").read_text() + '[eryx-gpl.git]\nversion = "2.56.1-r.1"\n'
        self.write("commands.toml", text)
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()
        for rel in ["clis/gpl/git/patches/git-wasi.patch", "clis/gpl/git/shims/wasi-git.h"]:
            self.write(rel, "b\n")
            self.commit()
            [problem] = self.problems()
            self.assertIn("eryx-gpl:git", problem)
            self.assertIn(rel, problem)
            self.git("reset", "-q", "--hard", self.base)
        paths, _ = publish.build_inputs(
            self.root, "clis/gpl/git/git.toml",
            publish.tomllib.loads((self.root / "clis/gpl/git/git.toml").read_text()),
        )
        self.assertIn("clis/gpl/git/patches/git-wasi.patch", paths)
        self.assertIn("clis/gpl/git/shims/wasi-git.h", paths)

    def test_a_version_never_goes_below_a_locked_one(self):
        self.write("commands.lock", publish.write_lock({"eryx:sd": {"1.1.0-r.3": "sha256:a"}}))
        problems = publish.check_versions(self.root, None)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("lower than the published 1.1.0-r.3", problems[0])
        self.set_version("sd", "1.1.0-r.3")
        self.assertEqual(publish.check_versions(self.root, None), [])

    def test_versions_must_match_the_manifest_and_cover_every_recipe(self):
        self.set_version("curl", "8.21.0-r.1")
        self.write("clis/gpl/git/git.toml", 'name = "git"\nlicense = "GPL-2.0-only"\nlang = "c"\n[source]\nref = "v2.56.1"\n')
        problems = publish.check_versions(self.root, None)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("eryx-gpl:git" in p and "no entry" in p for p in problems))
        self.assertTrue(any("8.20.0" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
