"""Regression tests for source-detail enrichment without importing the Streamlit app."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_clean_source_value",
    "_prefer_non_na",
    "_is_meaningful_value",
    "_build_fully_qualified_name",
    "_normalise_name_for_join",
    "_resolve_native_actual_column",
    "_enrich_with_source_details",
}


def _load_source_helpers():
    """Compile only the pure helper functions so app-level Streamlit code does not run."""
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    helper_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in HELPER_NAMES
    ]
    found_names = {node.name for node in helper_nodes}
    missing_names = HELPER_NAMES - found_names
    if missing_names:
        raise AssertionError(f"Could not load source helpers: {sorted(missing_names)}")

    namespace = {"json": json, "re": re}
    helper_module = ast.Module(body=helper_nodes, type_ignores=[])
    exec(compile(helper_module, str(source_path), "exec"), namespace)
    return namespace


class EnrichWithSourceDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_source_helpers()
        cls.enrich = staticmethod(cls.helpers["_enrich_with_source_details"])

    def test_missing_semantic_row_uses_visual_names_without_crashing(self):
        result = self.enrich(None, "Sales Story", "NET_SALES", {})

        self.assertEqual(result["Exact Source Table/View"], "Sales Story")
        self.assertEqual(result["Exact Source Column Name"], "NET_SALES")
        self.assertEqual(result["Fully Qualified Source Object"], "Sales Story")
        self.assertEqual(result["Semantic Model Name"], "N/A")

    def test_missing_semantic_row_still_uses_matching_source_lineage(self):
        source_lookup = {
            "salesstory": {
                "Source Database": "PBI_LINEAGE_DEMO",
                "Source Schema": "MART",
                "Source Name": "FACT_PBI_SALES_STORY",
                "Source Type": "TABLE",
                "Fully Qualified Name": (
                    "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY"
                ),
            }
        }

        result = self.enrich(
            None,
            "Sales Story",
            "NET_SALES",
            source_lookup,
        )

        self.assertEqual(result["Exact Source Database"], "PBI_LINEAGE_DEMO")
        self.assertEqual(result["Exact Source Schema"], "MART")
        self.assertEqual(result["Exact Source Table/View"], "FACT_PBI_SALES_STORY")
        self.assertEqual(result["Exact Source Object Type"], "TABLE")
        self.assertEqual(
            result["Fully Qualified Source Object"],
            "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY",
        )

    def test_non_dictionary_source_lookup_is_treated_as_empty(self):
        base_row = {
            "Exact Source Database": "PBI_LINEAGE_DEMO",
            "Exact Source Schema": "MART",
            "Exact Source Table/View": "FACT_PBI_SALES_STORY",
            "Exact Source Object Type": "TABLE",
            "Source Column Name From Model": "NET_SALES",
        }

        result = self.enrich(
            base_row,
            "Sales Story",
            "NET_SALES",
            None,
        )

        self.assertEqual(result["Exact Source Column Name"], "NET_SALES")
        self.assertEqual(
            result["Fully Qualified Source Object"],
            "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY",
        )


if __name__ == "__main__":
    unittest.main()
