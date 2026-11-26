"""Tests for sandbox.py's path rules. Run with
`python3 -m unittest discover -s scripts`. Nothing here runs Docker."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sandbox

REPO = sandbox.REPO
STRICT = {sandbox.STRICT: "1"}


def mounts_of(command):
    """The --mount values of a docker command"""
    return [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=sandbox.TMP))
        self.addCleanup(subprocess.run, ["rm", "-rf", "--", str(self.tmp)], check=True)

    def test_a_new_directory_directly_in_tmp(self):
        new = sandbox.TMP / f"{self.tmp.name}-new"
        self.assertEqual(sandbox.scratch(new), new)

    def test_refuses_an_existing_or_nested_directory_or_one_elsewhere(self):
        for path in (self.tmp, self.tmp / "nested", REPO / "scratch/x", Path.home() / "x-new"):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                sandbox.scratch(path)

    def test_a_link_counts_as_where_it_points(self):
        link = sandbox.TMP / f"{self.tmp.name}-link"
        link.symlink_to(REPO)
        self.addCleanup(link.unlink)
        with self.assertRaises(SystemExit):
            sandbox.scratch(link)

    def test_outside_repo(self):
        self.assertEqual(sandbox.outside_repo(self.tmp), self.tmp)
        for path in (REPO, REPO / "scratch", REPO.parent):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                sandbox.outside_repo(path)

    def test_new_scratch_makes_a_new_directory(self):
        made = sandbox.new_scratch()
        self.addCleanup(made.rmdir)
        self.assertEqual(made.parent, sandbox.TMP)
        given = self.tmp / "build"
        self.assertEqual(sandbox.new_scratch(given), given)
        self.assertTrue(given.is_dir())
        with self.assertRaises(SystemExit):
            sandbox.new_scratch(given)

    def test_strict_new_scratch_is_directly_in_tmp(self):
        with mock.patch.dict(os.environ, STRICT):
            with self.assertRaises(SystemExit):
                sandbox.new_scratch(self.tmp / "nested")
            with self.assertRaises(SystemExit):
                sandbox.new_scratch(self.tmp)
            new = sandbox.TMP / f"{self.tmp.name}-strict"
            self.addCleanup(new.rmdir)
            self.assertEqual(sandbox.new_scratch(new), new)

    def test_built_scratch_must_be_a_directory_not_a_link(self):
        self.assertEqual(sandbox.built_scratch(self.tmp), self.tmp)
        link = sandbox.TMP / f"{self.tmp.name}-link"
        link.symlink_to(self.tmp)
        self.addCleanup(link.unlink)
        for path in (link, self.tmp / "missing", REPO):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                sandbox.built_scratch(path)

    def test_strict_built_scratch_is_directly_in_tmp(self):
        (self.tmp / "nested").mkdir()
        with mock.patch.dict(os.environ, STRICT):
            self.assertEqual(sandbox.built_scratch(self.tmp), self.tmp)
            with self.assertRaises(SystemExit):
                sandbox.built_scratch(self.tmp / "nested")

    def test_unlink_links(self):
        (self.tmp / "dir").mkdir()
        (self.tmp / "dir/file").write_text("kept")
        (self.tmp / "environ").symlink_to("/proc/self/environ")
        (self.tmp / "dir/up").symlink_to(REPO, target_is_directory=True)
        (self.tmp / "dangling").symlink_to(self.tmp / "nothing")
        with contextlib.redirect_stderr(io.StringIO()) as printed:
            sandbox.unlink_links(self.tmp)
        self.assertIn("Removing the link", printed.getvalue())
        left = sorted(str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*"))
        self.assertEqual(left, ["dir", "dir/file"])


class Mounts(unittest.TestCase):
    def test_read_only_unless_writable(self):
        command = sandbox.docker_command(
            [(REPO, REPO, False), (Path("/tmp/x"), REPO / "scratch", True)],
            {"A": "b"}, "true", "curl")
        self.assertEqual(mounts_of(command), [
            f"type=bind,source={REPO},target={REPO},readonly",
            f"type=bind,source=/tmp/x,target={REPO}/scratch",
        ])
        # Only the environment given, and never the Docker socket
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "--env"], ["A=b"])
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertIn("no-new-privileges", command[command.index("--security-opt") + 1])

    def test_runs_as_the_user_after_setup(self):
        script = sandbox.docker_command([], {}, "echo 'hi'", "curl", user=(1000, 100))[-1]
        self.assertIn("apt-get", script.split("setpriv")[0])
        self.assertIn("setpriv --reuid=1000 --regid=100 --clear-groups", script)
        # The build keeps no capabilities, and nothing can gain any
        self.assertIn("--bounding-set=-all", script)
        self.assertIn("--no-new-privs", script)

    def test_refuses_mounts_that_add_fields(self):
        for path in (Path("/tmp/a,source=/"), Path('/tmp/a"b'), Path("relative")):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                sandbox.check_mounts([(path, Path("/x"), False)])

    def test_refuses_writing_the_checkout(self):
        for path in (REPO, REPO / "scratch", REPO.parent):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                sandbox.check_mounts([(path, Path("/x"), True)])
        sandbox.check_mounts([(REPO, REPO, False)])

    def test_strict_writes_only_in_tmp(self):
        home = Path.home() / "out"
        sandbox.check_mounts([(home, home, True)])
        with mock.patch.dict(os.environ, STRICT), self.assertRaises(SystemExit):
            sandbox.check_mounts([(home, home, True)])

    def test_a_build_mounts_only_its_scratch_and_the_toolchain(self):
        tools = {"eryx-build": REPO / "scratch/tools", "go": REPO / "scratch/go-wasip3",
                 "rust": Path("/opt/rust"), "wasi-sdk": Path("/opt/wasi-sdk")}
        scratch = Path("/tmp/build-1")
        with mock.patch.dict(os.environ, {"CARGO_HOME": "/home/u/.cargo"}):
            mounts = sandbox.build_mounts(scratch, tools)
        self.assertEqual([m for m in mounts if m[2]], [
            (scratch, REPO / "scratch", True),
            (scratch / "cargo-home", Path("/home/u/.cargo"), True),
        ])
        self.assertEqual(sorted(str(m[0]) for m in mounts if not m[2]),
                         sorted(map(str, [REPO, *tools.values()])))
        # eryx-build and the Go fork, from the checkout's scratch/, go on
        # top of the build's
        self.assertIn((REPO / "scratch/go-wasip3", REPO / "scratch/go-wasip3", False), mounts)
        with mock.patch.dict(os.environ, STRICT):
            sandbox.check_mounts(mounts)

    def test_the_wasi_sdk_is_not_on_path(self):
        tools = {"wasi-sdk": Path("/opt/wasi-sdk"), "rust": Path("/opt/rust"),
                 "wasmtime": Path("/opt/wasmtime")}
        self.assertEqual(sandbox.bin_dirs(tools), [Path("/opt/wasmtime"), Path("/opt/rust/bin")])

    def test_names(self):
        for name in ("../x", "x/y", "-x", "X", ""):
            with self.subTest(name=name), self.assertRaises(SystemExit):
                sandbox.manifest(name)
        self.assertEqual(sandbox.manifest("sd"), "clis/sd/sd.toml")


class Strict(unittest.TestCase):
    """Without --sandbox, what runs build code refuses to under strict mode"""

    def run_strict(self, *command):
        env = {**os.environ, **STRICT, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(command, cwd=REPO, env=env, capture_output=True, text=True)

    def test_smoke_needs_the_sandbox(self):
        out = self.run_strict("bash", "clis/smoke.sh", "sd")
        self.assertEqual(out.returncode, 1)
        self.assertIn("RECIPES_REQUIRE_SANDBOX is set: smoke tests only run with --sandbox", out.stderr)

    def test_wheels_need_the_sandbox(self):
        for command in (["build", "wheels/markupsafe", "--sysroot", "/x", "--out", "/tmp/x"],
                        ["verify", "/tmp/x/a.whl", "wheels/markupsafe"]):
            with self.subTest(command=command[0]):
                out = self.run_strict(sys.executable, "wheels/build.py", *command)
                self.assertEqual(out.returncode, 1)
                self.assertIn("RECIPES_REQUIRE_SANDBOX is set: build and verify only run with --sandbox",
                              out.stderr)


if __name__ == "__main__":
    unittest.main()
