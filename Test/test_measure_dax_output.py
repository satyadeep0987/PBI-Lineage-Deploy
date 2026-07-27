"""Regression tests for displaying the exact DAX with LLM measure detail."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {"_prefer_non_na", "_selected_measure_dax_expression"}


def _load_dax_helper():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES
    ]
    found = {node.name for node in nodes}
    if found != HELPER_NAMES:
        raise AssertionError(f"Missing helpers: {HELPER_NAMES - found}")
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["_selected_measure_dax_expression"]


class MeasureDaxOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.get_dax = staticmethod(_load_dax_helper())

    def test_prefers_semantic_dax_expression_over_other_available_fields(self):
        dax = self.get_dax({
            "Semantic_DAX_Expression": "CALCULATE([Revenue], Sales[HighValue] = 1)",
            "Target Expression": "[Revenue]",
        })

        self.assertEqual(dax, "CALCULATE([Revenue], Sales[HighValue] = 1)")

    def test_uses_target_expression_when_semantic_dax_is_unavailable(self):
        dax = self.get_dax({
            "Semantic_DAX_Expression": "N/A",
            "Target Expression": "SUM(Sales[Amount])",
        })

        self.assertEqual(dax, "SUM(Sales[Amount])")


if __name__ == "__main__":
    unittest.main()
