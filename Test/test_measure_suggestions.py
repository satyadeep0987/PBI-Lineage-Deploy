"""Regression tests for deterministic related-measure suggestions."""

from __future__ import annotations

import ast
import difflib
import unittest
from pathlib import Path

from pbi_modules.table_impact import normalize_identifier


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_related_measure_helper():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_related_measure_suggestions"
    )
    namespace = {
        "difflib": difflib,
        "normalize_identifier": normalize_identifier,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_related_measure_suggestions"]


class RelatedMeasureSuggestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suggest = staticmethod(_load_related_measure_helper())

    def test_typo_returns_real_closest_measure(self):
        suggestions = self.suggest(
            "netsalesamt",
            ["Net Sales Amount", "Gross Margin", "Order Count"],
        )

        self.assertEqual(suggestions[0], "Net Sales Amount")

    def test_exact_measure_is_not_suggested_as_an_alternative(self):
        suggestions = self.suggest(
            "Net Sales",
            ["Net Sales", "Net Sales Amount", "Gross Sales"],
        )

        self.assertNotIn("Net Sales", suggestions)
        self.assertIn("Net Sales Amount", suggestions)


if __name__ == "__main__":
    unittest.main()
