#!/usr/bin/env python3
"""WS6 documentation and version metadata guard tests."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GRIMOIRE_BUILD = ROOT / "grimoire" / "build"
CHANGELOG = ROOT / "CHANGELOG.md"


class Ws6DocsTest(unittest.TestCase):
    def test_ws6_docs_exist(self) -> None:
        for relative_path in (
            "implicit_sdf_modeling.md",
            "visual_hull_reconstruction.md",
            "subdivision_surfaces.md",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertTrue((GRIMOIRE_BUILD / relative_path).exists())

    def test_fitting_doc_mentions_divine_eye_loop(self) -> None:
        text = (GRIMOIRE_BUILD / "analysis_by_synthesis_fitting.md").read_text(encoding="utf-8")
        for marker in (
            "fit_against_divine_eye()",
            "correction_loop.decide()",
            "bestRawFidelity",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


    def test_changelog_defines_each_link_reference_once(self) -> None:
        """A merge kept both sides' link blocks, so six versions had two definitions each.

        Markdown silently resolves the first and ignores the rest, so half the block was dead text
        pointing at a different organisation than the live one -- invisible in the rendered page.
        Deliberately not asserting heading order: `update_changelog` inserts above the first `## [`,
        so a test pinning `Unreleased` to the top would fail on the first automated release.
        """
        labels = re.findall(r"^\[([^\]]+)\]:", CHANGELOG.read_text(encoding="utf-8"), re.MULTILINE)
        duplicates = sorted({label for label in labels if labels.count(label) > 1})

        self.assertEqual(duplicates, [], f"CHANGELOG.md defines these link references twice: {duplicates}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
