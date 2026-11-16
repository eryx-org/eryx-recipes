"""Tests for notices.py: `python3 -m unittest discover -s scripts`."""

import unittest

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

    def test_path_crate_has_no_crates_io_location(self):
        crate = notices.crate_package({"name": "core", "version": "0.0.0", "source": None}, "MIT OR Apache-2.0")
        self.assertEqual(crate["downloadLocation"], "NOASSERTION")


if __name__ == "__main__":
    unittest.main()
