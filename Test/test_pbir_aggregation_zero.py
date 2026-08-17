"""Regression test for PBIR aggregation enum value zero (Sum)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "_aggregation_function_name",
    "_extract_pbir_field",
    "_extract_source_entity_from_expression",
    "_clean_layout_text",
    "_infer_field_from_query_ref",
}


def _load_helpers():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class PbirAggregationZeroTests(unittest.TestCase):
    def test_zero_is_preserved_as_sum(self):
        helpers = _load_helpers()
        result = helpers["_extract_pbir_field"]({
            "Aggregation": {
                "Function": 0,
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Source": "s"}},
                        "Property": "SALES_AMOUNT",
                    }
                },
            }
        })

        self.assertEqual(result["Aggregation"], "Sum")
        self.assertEqual(result["Field Type"], "Sum Aggregation")


if __name__ == "__main__":
    unittest.main()
