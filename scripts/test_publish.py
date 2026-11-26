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


class Fetched(unittest.TestCase):
    def test_multicall_names_are_the_features(self):
        manifest = {"name": "coreutils", "build": {"cargo_flags": ["--no-default-features", "--features", "cat,ls"]}}
        self.assertEqual(publish.multicall_names(manifest), ["cat", "ls"])
        for flags in [[], ["--features"]]:
            with self.assertRaises(publish.Error):
                publish.multicall_names({"name": "x", "build": {"cargo_flags": flags}})

    def test_bumped(self):
        self.assertEqual(publish.bumped("0.9.0-r.3", "0.9.0"), "0.9.0-r.4")
        self.assertEqual(publish.bumped("0.9.0-r.3", "0.10.0"), "0.10.0-r.1")


    def test_set_version_touches_only_the_version(self):
        text = 'multicall = true\nversion = "2.0.0-r.1"\n# version = "x"\n'
        self.assertEqual(publish.set_version(text, "2.0.0-r.2"),
                         'multicall = true\nversion = "2.0.0-r.2"\n# version = "x"\n')
        with self.assertRaises(publish.Error):
            publish.set_version("upstream = \"1.0.0\"\n", "3.0.0-r.1")

    def test_recipe_url(self):
        mise = {"vars": {"coreutils_rev": "abc"}}
        self.assertEqual(
            publish.recipe_url(mise, {"fetched": "coreutils_rev"}, "clis/coreutils.toml"),
            "https://github.com/eryx-org/eryx/blob/abc/clis/coreutils.toml",
        )
        self.assertIsNone(publish.recipe_url(mise, {}, "clis/rg/rg.toml"))


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
            (component / "sd.spdx.json").write_text("{}")
            version = publish.commands(publish.ROOT)[("eryx", "sd")]["version"]
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


SD = """
    name = "sd"
    license = "MIT"
    lang = "rust"
    [source]
    ref = "v1.1.0"
    dir = "scratch/sd"
    [build]
    vendor_patch = "clis/sd/patches/patch-sd.sh"
"""
CURL = """
    name = "curl"
    license = "curl"
    lang = "c"
    [source]
    ref = "curl-8_20_0"
"""


class ScratchRepo(unittest.TestCase):
    """A scratch git repo with two commands: a base commit, then changes."""

    MISE = 'vars = { eryx_rev = "%s", wasi_sdk_version = "wasi-sdk-33" }\n[tools]\nrust = "1.100.0"\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write("mise.toml", self.MISE % "aaa")
        self.base_layout()
        self.git("init", "-q", "-b", "main")
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def base_layout(self):
        self.write("clis/sd/sd.toml", SD)
        self.write("clis/curl/curl.toml", CURL)
        self.write("clis/sd/patches/patch-sd.sh", "echo 1\n")
        self.write("clis/sd/publish.toml", 'version = "1.1.0-r.1"\n')
        self.write("clis/curl/publish.toml", 'version = "8.20.0-r.1"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def append(self, rel, text):
        self.write(rel, (self.root / rel).read_text() + text)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")

    def rebase_here(self):
        self.commit()
        self.base = self.git("rev-parse", "HEAD").strip()

    def problems(self):
        return publish.check_versions(self.root, self.base)

    def set_version(self, name, version):
        path = self.root / publish.command_dir(("eryx-gpl" if name == "git" else "eryx", name)) / "publish.toml"
        path.write_text(publish.set_version(path.read_text(), version))


class CheckVersions(ScratchRepo):
    """check_versions on the scratch repo."""

    def test_unchanged_is_fine(self):
        self.assertEqual(self.problems(), [])

    def test_a_patch_change_needs_a_new_revision(self):
        self.write("clis/sd/patches/patch-sd.sh", "echo 2\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("eryx:sd", problem)
        self.assertIn("clis/sd/patches/patch-sd.sh", problem)
        self.set_version("sd", "1.1.0-r.2")
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_branch_behind_its_base_is_judged_on_its_own_changes(self):
        # curl 8.20.0-r.1 is published; this branch forks there, and only
        # changes sd's smoke test, while its base moves on to curl r.2
        self.write("commands.lock", publish.write_lock({"eryx:curl": {"8.20.0-r.1": "sha256:" + "a" * 64}}))
        self.rebase_here()
        fork = self.base
        self.set_version("curl", "8.20.0-r.2")
        self.commit()
        main = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "-q", "-b", "pr", fork)
        self.write("clis/sd/smoke.sh", "expect 1\n")
        self.commit()
        self.base = main
        self.assertEqual(self.problems(), [])

    def test_its_version_notices_and_smoke_test_arent_build_inputs(self):
        self.write("clis/sd/smoke.sh", "expect 1\n")
        self.write("clis/sd/notices.toml", "")
        self.write("clis/sd/licenses/sd-LICENSE", "MIT\n")
        self.append("clis/sd/publish.toml", 'names = ["sd"]\n')
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_file_in_its_directory_is_a_build_input(self):
        self.write("clis/sd/shims/x.h", "1\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("clis/sd/shims/x.h", problem)

    def test_wasmtime_needs_a_new_revision_only_for_a_cwasm(self):
        self.append("clis/sd/sd.toml", "[cwasm]\nenabled = true\n")
        self.set_version("sd", "1.1.0-r.2")
        self.rebase_here()
        self.write("mise.toml", self.MISE % "aaa" + '"aqua:bytecodealliance/wasmtime" = "51.0.0"\n')
        self.commit()
        [problem] = self.problems()
        self.assertIn("eryx:sd", problem)

    def test_its_pins_need_a_new_revision(self):
        self.append("clis/sd/publish.toml", 'pins = ["vars.wasi_sdk_version"]\n')
        self.rebase_here()
        self.write("mise.toml", (self.MISE % "aaa").replace("wasi-sdk-33", "wasi-sdk-34"))
        self.commit()
        # curl's lane uses the WASI SDK too.
        self.assertEqual(sorted(p.split(":")[1] for p in self.problems()), ["curl", "sd"])

    def test_its_files_need_a_new_revision(self):
        self.write("scripts/extra.sh", "echo 1\n")
        self.append("clis/sd/publish.toml", 'files = ["scripts/extra.sh"]\n')
        self.rebase_here()
        self.write("scripts/extra.sh", "echo 2\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("eryx:sd", problem)
        self.assertIn("scripts/extra.sh", problem)

    def test_a_missing_file_is_a_problem(self):
        self.append("clis/sd/publish.toml", 'files = ["scripts/gone.sh"]\n')
        self.commit()
        [problem] = self.problems()
        self.assertIn("scripts/gone.sh", problem)

    def test_a_patch_scripts_missing_relative_path_is_a_problem(self):
        self.write("clis/sd/patches/patch-sd.sh", 'cp "$dir/../shims/gone.h" x\n')
        self.commit()
        [problem] = self.problems()
        self.assertIn("shims/gone.h", problem)

    def test_a_toolchain_pin_needs_a_new_revision_for_every_lane_using_it(self):
        self.write("mise.toml", self.MISE % "bbb")
        self.commit()
        self.assertEqual(len(self.problems()), 2)

    def test_a_new_upstream_restarts_at_r1(self):
        text = (self.root / "clis/sd/sd.toml").read_text().replace('"v1.1.0"', '"v1.2.0"')
        self.write("clis/sd/sd.toml", text)
        self.set_version("sd", "1.2.0-r.2")
        self.commit()
        [problem] = self.problems()
        self.assertIn("starts at r.1", problem)
        self.set_version("sd", "1.2.0-r.1")
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_shared_file_and_a_scripts_siblings_are_inputs(self):
        self.write("clis/gpl/git/git.toml", """
            name = "git"
            license = "GPL-2.0-only"
            lang = "c"
            [source]
            ref = "v2.56.1"
            [build]
            vendor_patch = "clis/gpl/git/patches/patch-git.sh"
            cflags = ["-include", "{repo}/clis/gpl/git/shims/wasi-git.h"]
            [[deps]]
            name = "curl"
            config_patch = "clis/shared/patches/patch-curl.sh"
        """)
        self.write("clis/gpl/git/patches/patch-git.sh", 'git apply "$(dirname "$0")/git-wasi.patch"\n')
        self.write("clis/gpl/git/patches/git-wasi.patch", "a\n")
        self.write("clis/gpl/git/shims/wasi-git.h", "a\n")
        self.write("clis/shared/patches/patch-curl.sh", "sed -i x lib/cf-socket.c\n")
        self.write("clis/gpl/git/publish.toml", 'version = "2.56.1-r.1"\n')
        self.rebase_here()
        for rel in ["clis/gpl/git/patches/git-wasi.patch", "clis/gpl/git/shims/wasi-git.h",
                    "clis/shared/patches/patch-curl.sh"]:
            self.write(rel, "b\n")
            self.commit()
            [problem] = self.problems()
            self.assertIn("eryx-gpl:git", problem)
            self.assertIn(rel, problem)
            self.git("reset", "-q", "--hard", self.base)

    def test_moving_inputs_and_rewriting_their_paths_isnt_a_change(self):
        self.write("clis/sd/patches/patch-sd.sh", 'cp "$dir/../shims/sd.h" x  # clis/sd/shims\n')
        self.write("clis/sd/shims/sd.h", "1\n")
        self.rebase_here()
        (self.root / "clis/shared").mkdir()
        self.git("mv", "clis/sd/patches", "clis/shared/patches")
        self.git("mv", "clis/sd/shims", "clis/shared/shims")
        self.write("clis/sd/sd.toml", "# moved\n" + textwrap.dedent(SD).replace(
            "clis/sd/patches/", "clis/shared/patches/"))
        self.write("clis/shared/patches/patch-sd.sh", 'cp "$dir/../shims/sd.h" x  # clis/shared/shims\n')
        self.commit()
        self.assertEqual(self.problems(), [])
        # A real change to a moved file still counts.
        self.write("clis/shared/shims/sd.h", "2\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("clis/shared/shims/sd.h", problem)

    def test_a_moved_file_with_other_edits_is_a_change(self):
        self.git("mv", "clis/sd/patches/patch-sd.sh", "clis/sd/patch-sd.sh")
        self.write("clis/sd/sd.toml", textwrap.dedent(SD).replace(
            "clis/sd/patches/patch-sd.sh", "clis/sd/patch-sd.sh"))
        self.write("clis/sd/patch-sd.sh", "echo 1\necho 2\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("clis/sd/patch-sd.sh", problem)
        self.assertNotIn("clis/sd/sd.toml", problem)

    def test_a_manifest_naming_another_file_is_a_change(self):
        self.write("clis/sd/patches/other.sh", "echo 1\n")
        self.rebase_here()
        self.write("clis/sd/sd.toml", textwrap.dedent(SD).replace("patch-sd.sh", "other.sh"))
        self.commit()
        [problem] = self.problems()
        self.assertIn("clis/sd/sd.toml", problem)

    def test_rewrite_paths(self):
        moved = {"clis/shims/awk/sys/wait.h": "clis/awk/shims/sys/wait.h",
                 "clis/shims/awk-wasi.c": "clis/awk/shims/awk-wasi.c",
                 "clis/shims/mbedtls-wasi.c": "clis/shared/shims/mbedtls-wasi.c",
                 "clis/go.toml": "clis/go/go.toml"}
        self.assertEqual(
            publish.rewrite_paths("see clis/go.toml. (clis/shims/awk/sys) clis/shims "
                                  "{repo}/clis/shims/awk-wasi.c clis/go.tomlx", moved),
            # clis/shims went to two places, so it's not rewritten.
            "see clis/go/go.toml. (clis/awk/shims/sys) clis/shims "
            "{repo}/clis/awk/shims/awk-wasi.c clis/go.tomlx",
        )

    def test_a_version_never_goes_below_a_locked_one(self):
        self.write("commands.lock", publish.write_lock({"eryx:sd": {"1.1.0-r.3": "sha256:a"}}))
        problems = publish.check_versions(self.root, None)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("lower than the published 1.1.0-r.3", problems[0])
        self.set_version("sd", "1.1.0-r.3")
        self.assertEqual(publish.check_versions(self.root, None), [])

    def test_a_changed_version_must_be_new_to_the_registry(self):
        self.write("commands.lock", publish.write_lock(
            {"eryx:sd": {"1.1.0-r.1": "sha256:a", "1.1.0-r.2": "sha256:b"}}
        ))
        self.set_version("sd", "1.1.0-r.2")
        self.rebase_here()
        # Back to r.1, which is published: publish would skip it.
        self.write("clis/sd/patches/patch-sd.sh", "echo 2\n")
        self.set_version("sd", "1.1.0-r.1")
        self.commit()
        [problem] = self.problems()
        self.assertIn("must be higher than the published 1.1.0-r.2", problem)
        self.set_version("sd", "1.1.0-r.3")
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_moving_a_fetched_recipes_pin_needs_a_new_revision(self):
        # The fetched recipe is git-ignored, so only the pin shows the change.
        self.write(".gitignore", "/clis/coreutils.toml\nscratch/\n")
        self.write("clis/coreutils.toml", 'name = "coreutils"\nlicense = "MIT"\nlang = "rust"\n'
                   '[source]\nref = "0.9.0"\n[build]\ncargo_flags = ["--features", "cat"]\n')
        mise = (self.MISE % "aaa").replace("wasi_sdk_version", 'coreutils_rev = "c1", wasi_sdk_version')
        self.write("mise.toml", mise)
        self.write("scratch/fetched/coreutils_rev", "c1\n")
        self.write("clis/coreutils/publish.toml", 'version = "0.9.0-r.1"\nmulticall = true\n'
                   'fetched = "coreutils_rev"\nmanifest = "clis/coreutils.toml"\n')
        self.rebase_here()
        self.assertEqual(self.problems(), [])

        self.write("mise.toml", mise.replace('"c1"', '"c2"'))
        self.commit()
        # The local copy is still c1's.
        [problem] = self.problems()
        self.assertIn("mise run fetch-coreutils", problem)
        self.write("scratch/fetched/coreutils_rev", "c2\n")
        [problem] = self.problems()
        self.assertIn("eryx:coreutils", problem)
        self.assertIn("needs a new revision", problem)
        self.set_version("coreutils", "0.9.0-r.2")
        self.commit()
        self.assertEqual(self.problems(), [])

        # Unfetched, it says how to fetch it.
        (self.root / "clis/coreutils.toml").unlink()
        [problem] = publish.check_versions(self.root, None)
        self.assertIn("mise run fetch-coreutils", problem)

    def test_versions_must_match_the_manifest_and_cover_every_recipe(self):
        self.set_version("curl", "8.21.0-r.1")
        self.write("clis/gpl/git/git.toml", 'name = "git"\nlicense = "GPL-2.0-only"\nlang = "c"\n[source]\nref = "v2.56.1"\n')
        self.write("clis/rg/rg.toml", 'name = "ripgrep"\nlicense = "MIT"\nlang = "rust"\n[source]\nref = "15.1.0"\n')
        self.write("clis/rg/publish.toml", 'version = "15.1.0-r.1"\n')
        problems = publish.check_versions(self.root, None)
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("eryx-gpl:git" in p and "has no clis/gpl/git/publish.toml" in p for p in problems))
        self.assertTrue(any("8.20.0" in p for p in problems))
        self.assertTrue(any("eryx:rg" in p and "'ripgrep'" in p for p in problems))


class FromTheOldLayout(ScratchRepo):
    """check_versions against a base from before clis/<name>/, when every
    manifest was clis/<name>.toml, its patches in clis/patches/, and the
    versions in commands.toml."""

    def base_layout(self):
        self.write("clis/sd.toml", SD.replace("clis/sd/patches/", "clis/patches/"))
        self.write("clis/curl.toml", CURL)
        self.write("clis/patches/patch-sd.sh", "# clis/patches/patch-sd.sh\ncp $dir/../../wit/x.wit .\n")
        self.write("wit/x.wit", "x\n")
        self.write("commands.toml", '[eryx.sd]\nversion = "1.1.0-r.1"\n[eryx.curl]\nversion = "8.20.0-r.1"\n')

    def migrate(self):
        for name in ["sd", "curl"]:
            (self.root / "clis" / name).mkdir()
            self.git("mv", f"clis/{name}.toml", f"clis/{name}/{name}.toml")
        self.git("mv", "clis/patches", "clis/sd/patches")
        self.git("rm", "-q", "commands.toml")
        self.write("clis/sd/publish.toml", 'version = "1.1.0-r.1"\n')
        self.write("clis/curl/publish.toml", 'version = "8.20.0-r.1"\n')
        self.write("clis/sd/sd.toml", SD)
        self.write("clis/sd/patches/patch-sd.sh", "# clis/sd/patches/patch-sd.sh\ncp $dir/../../../wit/x.wit .\n")

    def test_an_old_tree_is_read_too(self):
        # sign.py makes a source offer from the commit a command was
        # published from, which may be from before clis/<name>/.
        self.assertEqual(publish.commands(self.root)[("eryx", "sd")],
                         {"manifest": "clis/sd.toml", "version": "1.1.0-r.1"})

    def test_the_move_isnt_a_change(self):
        self.migrate()
        self.commit()
        self.assertEqual(self.problems(), [])

    def test_a_change_with_the_move_still_is(self):
        self.migrate()
        self.append("clis/sd/patches/patch-sd.sh", "echo more\n")
        self.commit()
        [problem] = self.problems()
        self.assertIn("eryx:sd", problem)
        self.assertIn("clis/sd/patches/patch-sd.sh", problem)

    def test_its_versions_are_the_base_versions(self):
        self.migrate()
        self.write("clis/sd/publish.toml", 'version = "1.2.0-r.2"\n')
        self.write("clis/sd/sd.toml", SD.replace('"v1.1.0"', '"v1.2.0"'))
        self.commit()
        [problem] = self.problems()
        self.assertIn("starts at r.1", problem)


class Matrix(ScratchRepo):
    """matrix on the scratch repo."""

    def setUp(self):
        super().setUp()
        self.write("clis/sd/smoke.sh", "expect 1\n")
        self.write("clis/find/find.toml", SD.replace('"sd"', '"find"').replace("clis/sd/patches/patch-sd.sh", ""))
        self.write("clis/find/publish.toml", 'version = "1.1.0-r.1"\npins = ["vars.wasi_sdk_version"]\n')
        self.write("scripts/notices.py", "x\n")
        self.rebase_here()

    def lanes(self, base=True):
        return {lang: [i["cli"] for i in items]
                for lang, items in publish.matrix(self.root, self.base if base else None).items()}

    def test_nothing_changed(self):
        self.assertEqual(self.lanes(), {"rust": [], "c": [], "go": []})

    def test_everything_without_a_base(self):
        self.assertEqual(self.lanes(base=False), {"rust": ["find", "sd"], "c": ["curl"], "go": []})
        self.assertIn({"cli": "find", "wasi_sdk": True}, publish.matrix(self.root, None)["rust"])

    def test_a_command_whose_directory_or_inputs_changed(self):
        self.write("clis/sd/smoke.sh", "expect 2\n")
        self.commit()
        self.assertEqual(self.lanes(), {"rust": ["sd"], "c": [], "go": []})

    def test_a_shared_input(self):
        self.write("clis/shared/patches/patch-curl.sh", "x\n")
        self.append("clis/curl/curl.toml", '[build]\nvendor_patch = "clis/shared/patches/patch-curl.sh"\n')
        self.rebase_here()
        self.write("clis/shared/patches/patch-curl.sh", "y\n")
        self.commit()
        self.assertEqual(self.lanes(), {"rust": [], "c": ["curl"], "go": []})

    def test_what_they_all_share(self):
        for rel in ["mise.toml", "scripts/notices.py"]:
            self.append(rel, "\n")
            self.commit()
            self.assertEqual(self.lanes(), self.lanes(base=False), rel)
            self.git("reset", "-q", "--hard", self.base)


class CacheKey(ScratchRepo):
    """cache_key on the same scratch repo, with notices inputs."""

    def setUp(self):
        super().setUp()
        for rel in publish.NOTICE_FILES:
            self.write(rel, "x\n")
        self.write("clis/sd/licenses/sd", "MIT\n")
        self.write("clis/sd/notices.toml", """
            [[component]]
            name = "sd"
            files = ["clis/sd/licenses/sd"]
        """)
        self.write("clis/curl/notices.toml", """
            [[component]]
            name = "curl"
            files = []
        """)
        self.write("third_party/manual.toml", """
            [[component]]
            name = "wasi-libc"
            files = []
            lanes = ["c"]
        """)
        self.sd, self.curl = self.keys()

    def keys(self):
        return publish.cache_key(self.root, "sd"), publish.cache_key(self.root, "curl")

    def test_same_inputs_same_key(self):
        self.assertEqual(self.keys(), (self.sd, self.curl))
        self.assertNotEqual(self.sd, self.curl)

    def test_a_patch_change_is_a_new_key_for_its_command_only(self):
        self.write("clis/sd/patches/patch-sd.sh", "echo 2\n")
        sd, curl = self.keys()
        self.assertNotEqual(sd, self.sd)
        self.assertEqual(curl, self.curl)

    def test_another_commands_notices_dont_change_the_key(self):
        text = (self.root / "clis/curl/notices.toml").read_text()
        self.write("clis/curl/notices.toml", text.replace('name = "curl"', 'name = "libcurl"'))
        sd, curl = self.keys()
        self.assertEqual(sd, self.sd)
        self.assertNotEqual(curl, self.curl)

    def test_its_lanes_notices_change_the_key(self):
        text = (self.root / "third_party/manual.toml").read_text()
        self.write("third_party/manual.toml", text.replace("wasi-libc", "wasi-libc 2"))
        self.assertNotEqual(self.keys()[1], self.curl)

    def test_its_notices_cant_name_artifacts(self):
        self.append("clis/sd/notices.toml", 'artifacts = ["curl"]\n')
        with self.assertRaisesRegex(publish.Error, "only go in sd's notices"):
            publish.cache_key(self.root, "sd")

    def test_its_licence_file_changes_the_key(self):
        self.write("clis/sd/licenses/sd", "Apache-2.0\n")
        self.assertNotEqual(self.keys()[0], self.sd)

    def test_its_version_changes_the_key(self):
        # It's in the SBOM.
        self.set_version("sd", "1.1.0-r.2")
        self.assertEqual(self.keys(), (self.keys()[0], self.curl))
        self.assertNotEqual(self.keys()[0], self.sd)

    def test_a_toolchain_pin_changes_every_key(self):
        self.write("mise.toml", self.MISE % "bbb")
        sd, curl = self.keys()
        self.assertNotEqual(sd, self.sd)
        self.assertNotEqual(curl, self.curl)

    def test_wasmtime_changes_every_key(self):
        # Every smoke test runs under it, and a cwasm only loads under its own.
        self.write("mise.toml", self.MISE % "aaa" + '"aqua:bytecodealliance/wasmtime" = "51.0.0"\n')
        sd, curl = self.keys()
        self.assertNotEqual(sd, self.sd)
        self.assertNotEqual(curl, self.curl)

    def test_a_script_relative_path_is_an_input(self):
        # patch-awk.sh copies $dir/../shims/sys/wait.h.
        self.write("clis/shared/wait.h", "1\n")
        self.write("clis/sd/patches/patch-sd.sh", 'cp "$dir/../../shared/wait.h" x\n')
        before = self.keys()[0]
        self.write("clis/shared/wait.h", "2\n")
        self.assertNotEqual(self.keys()[0], before)

    def test_a_script_relative_path_must_exist(self):
        self.write("clis/sd/patches/patch-sd.sh", 'cp "$dir/../shims/gone.h" x\n')
        with self.assertRaises(publish.Error):
            publish.cache_key(self.root, "sd")
        # A variable path is left to the by-name match.
        self.write("clis/sd/patches/patch-sd.sh", 'git apply "$dir/$1"\n')
        publish.cache_key(self.root, "sd")

    def test_its_pins_are_inputs(self):
        mise = self.MISE % "aaa"
        self.append("clis/sd/publish.toml", 'pins = ["vars.wasi_sdk_version"]\n')
        sd = self.keys()[0]
        self.write("mise.toml", mise.replace("wasi-sdk-33", "wasi-sdk-34"))
        self.assertNotEqual(self.keys()[0], sd)

    def test_its_files_are_inputs(self):
        self.write("scripts/extra.sh", "echo 1\n")
        self.append("clis/sd/publish.toml", 'files = ["scripts/extra.sh"]\n')
        sd, curl = self.keys()
        self.write("scripts/extra.sh", "echo 2\n")
        self.assertEqual(self.keys(), (self.keys()[0], curl))
        self.assertNotEqual(self.keys()[0], sd)

    def test_a_missing_input_fails(self):
        (self.root / "clis/sd/patches/patch-sd.sh").unlink()
        # build_inputs skips a named path that doesn't exist, so the
        # manifest's text still covers it; a notices file must exist.
        (self.root / "about.toml").unlink()
        with self.assertRaises(publish.Error):
            publish.cache_key(self.root, "sd")


if __name__ == "__main__":
    unittest.main()
