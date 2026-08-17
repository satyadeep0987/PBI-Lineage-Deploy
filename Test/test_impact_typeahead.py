"""Regression tests for live Table Impact and Measure Impact suggestions."""

from __future__ import annotations

import ast
import difflib
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_suggestion_helper():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_impact_name_suggestions"
    )
    namespace = {"difflib": difflib, "re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_impact_name_suggestions"]


class ImpactTypeaheadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suggest = staticmethod(_load_suggestion_helper())

    def test_prefix_suggestions_appear_after_first_character(self):
        suggestions = self.suggest(
            "f",
            ["DIM_DATE", "FACT_PAYMENTS", "FACT_SALES"],
        )

        self.assertEqual(suggestions, ["FACT_PAYMENTS", "FACT_SALES"])

    def test_exact_and_prefix_matches_rank_before_contains(self):
        suggestions = self.suggest(
            "sales",
            ["Net Sales", "Sales", "Sales Amount", "Gross Margin"],
        )

        self.assertEqual(suggestions[:3], ["Sales", "Sales Amount", "Net Sales"])

    def test_typo_returns_authorized_metadata_candidate(self):
        suggestions = self.suggest(
            "revnue",
            ["Revenue", "Margin", "Order Count"],
        )

        self.assertEqual(suggestions[0], "Revenue")


if __name__ == "__main__":
    unittest.main()
