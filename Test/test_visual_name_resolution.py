"""Regression tests for readable PBIR visual names in Visual Details."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "_clean_layout_text",
    "_get_nested_value",
    "_json_literal_to_text",
    "_pbir_projection_label",
    "_infer_pbir_visual_title",
    "_extract_pbir_visual_title",
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


class VisualNameResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_helpers()

    def test_reads_title_from_pbir_visual_container_objects(self):
        visual_json = {
            "name": "72abe80e7ae35dc00862",
            "visual": {
                "visualType": "kpi",
                "visualContainerObjects": {
                    "title": [{
                        "properties": {
                            "text": {
                                "expr": {
                                    "Literal": {"Value": "'Total Profit by MONTH_NAME'"}
                                }
                            }
                        }
                    }]
                },
            },
        }

        title = self.helpers["_extract_pbir_visual_title"](
            visual_json,
            "72abe80e7ae35dc00862",
        )

        self.assertEqual(title, "Total Profit by MONTH_NAME")

    def test_explicit_title_takes_precedence_over_automatic_title(self):
        visual_json = {
            "name": "28de925eb40c801ca695",
            "visual": {
                "visualType": "cardVisual",
                "query": {
                    "queryState": {
                        "Data": {
                            "projections": [{
                                "nativeQueryRef": "Total Payment",
                            }]
                        }
                    }
                },
                "visualContainerObjects": {
                    "title": [{
                        "properties": {
                            "text": {
                                "expr": {
                                    "Literal": {"Value": "'Total Pay'"}
                                }
                            }
                        }
                    }]
                },
            },
        }

        title = self.helpers["_extract_pbir_visual_title"](
            visual_json,
            "28de925eb40c801ca695",
        )

        self.assertEqual(title, "Total Pay")

    def test_builds_readable_name_for_automatic_pbir_title(self):
        visual_json = {
            "name": "7038bbe0619b54c2990b",
            "visual": {
                "visualType": "kpi",
                "query": {
                    "queryState": {
                        "Indicator": {
                            "projections": [{
                                "queryRef": "RPT_EXECUTIVE_DASHBOARD.REVENUE",
                                "nativeQueryRef": "Sum of REVENUE",
                            }]
                        },
                        "TrendLine": {
                            "projections": [{
                                "queryRef": "RPT_EXECUTIVE_DASHBOARD.MONTH_NAME",
                                "nativeQueryRef": "MONTH_NAME",
                            }]
                        },
                    }
                },
            },
        }

        title = self.helpers["_extract_pbir_visual_title"](
            visual_json,
            "7038bbe0619b54c2990b",
        )

        self.assertEqual(title, "Sum of REVENUE by MONTH_NAME")


if __name__ == "__main__":
    unittest.main()
