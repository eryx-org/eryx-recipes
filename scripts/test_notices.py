"""Tests for notices.py: `python3 -m unittest discover -s scripts`."""

import tempfile
import textwrap
import unittest
from pathlib import Path

import notices


class Sbom(unittest.TestCase):
    def test_document(self):
        tool = notices.spdx_package("sd", "v1.1.0", "MIT", "git+https://github.com/chmln/sd.git@abc")
        crate = notices.crate_package(
            {"name": "regex", "version": "1.0.0", "source": "registry+https://github.com/rust-lang/crates.io-index"},
            "MIT OR Apache-2.0",
        )
        doc = notices.sbom_document("sd", [tool, crate, crate], created="2026-01-01T00:00:00Z")
        # The crate both the command and std use is listed once.
        self.assertEqual([p["name"] for p in doc["packages"]], ["sd", "regex"])
        self.assertEqual(doc["packages"][1]["externalRefs"][0]["referenceLocator"], "pkg:cargo/regex@1.0.0")
        self.assertEqual(doc["packages"][1]["downloadLocation"],
                         "https://crates.io/api/v1/crates/regex/1.0.0/download")
        self.assertEqual(
            [(r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"]) for r in doc["relationships"]],
            [("SPDXRef-DOCUMENT", "DESCRIBES", "SPDXRef-Package-0"),
             ("SPDXRef-Package-0", "CONTAINS", "SPDXRef-Package-1")],
        )
        # The same packages give the same namespace.
        again = notices.sbom_document("sd", [tool, crate], created="2027-01-01T00:00:00Z")
        self.assertEqual(doc["documentNamespace"], again["documentNamespace"])

    def test_path_crate_has_no_crates_io_location_or_purl(self):
        crate = notices.crate_package({"name": "core", "version": "0.0.0", "source": None}, "MIT OR Apache-2.0")
        self.assertEqual(crate["downloadLocation"], "NOASSERTION")
        self.assertNotIn("externalRefs", crate)

    def test_another_build_gets_another_namespace(self):
        tool = notices.spdx_package("sd", "1.1.0-r.1", "MIT", None)
        rebuilt = {**tool, "versionInfo": "1.1.0-r.2"}
        self.assertNotEqual(notices.sbom_document("sd", [tool])["documentNamespace"],
                            notices.sbom_document("sd", [rebuilt])["documentNamespace"])

    def test_published_version(self):
        self.assertRegex(notices.published_version("sd"), r"-r\.[1-9][0-9]*$")
        self.assertRegex(notices.published_version("git"), r"-r\.[1-9][0-9]*$")


class GoModules(unittest.TestCase):
    def test_vendored_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            vendor = Path(tmp)
            (vendor / "modules.txt").write_text(textwrap.dedent("""\
                # golang.org/x/mod v0.36.1
                ## explicit; go 1.25.0
                golang.org/x/mod/modfile
                golang.org/x/mod/semver
                # golang.org/x/sync v0.20.0
                ## explicit; go 1.25.0
                golang.org/x/sync/errgroup
            """))
            self.assertEqual(notices.vendored_modules(vendor), {
                "golang.org/x/mod/modfile": ("golang.org/x/mod", "v0.36.1"),
                "golang.org/x/mod/semver": ("golang.org/x/mod", "v0.36.1"),
                "golang.org/x/sync/errgroup": ("golang.org/x/sync", "v0.20.0"),
            })


if __name__ == "__main__":
    unittest.main()
