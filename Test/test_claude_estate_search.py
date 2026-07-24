"""Tests for the Claude estate-wide lineage search helpers."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from pbi_modules.table_impact import normalize_identifier


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_prefer_non_na",
    "_is_meaningful_value",
    "_trim_for_prompt",
    "_agent_text_argument",
    "_agent_distinct_meaningful_values",
    "_agent_estate_overview",
    "_agent_search_row",
    "_agent_compact_search_match",
    "_search_claude_agent_estate",
}


def _load_estate_search_helpers():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    selected_nodes = []
    found_helpers = set()
    found_fields = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES:
            selected_nodes.append(node)
            found_helpers.add(node.name)
        elif (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_AGENT_SEARCH_RESULT_FIELDS"
                for target in node.targets
            )
        ):
            selected_nodes.append(node)
            found_fields = True

    if found_helpers != HELPER_NAMES:
        raise AssertionError(
            f"Missing Claude estate helpers: {HELPER_NAMES - found_helpers}"
        )
    if not found_fields:
        raise AssertionError("Missing _AGENT_SEARCH_RESULT_FIELDS.")

    namespace = {
        "normalize_identifier": normalize_identifier,
        "re": re,
    }
    exec(
        compile(
            ast.Module(body=selected_nodes, type_ignores=[]),
            str(source_path),
            "exec",
        ),
        namespace,
    )
    return namespace


class ClaudeEstateSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_estate_search_helpers()

    def test_search_returns_every_matching_lineage_surface(self):
        index = {
            "workspaces": ["Sales&Marketing"],
            "model_count": 1,
            "reports": [
                {
                    "Workspace Name": "Sales&Marketing",
                    "Report Name": "NET_SALES Executive Report",
                    "Report ID": "report-1",
                    "Dataset ID": "dataset-1",
                }
            ],
            "semantic_objects": [
                {
                    "Agent Workspace Name": "Sales&Marketing",
                    "Agent Dataset ID": "dataset-1",
                    "Agent Connected Reports": "NET_SALES Executive Report",
                    "Semantic Table/View": "FACT_SALES",
                    "Object Type": "COLUMN",
                    "Semantic Object Name": "NET_SALES",
                }
            ],
            "source_lineage": [
                {
                    "Agent Workspace Name": "Sales&Marketing",
                    "Agent Dataset ID": "dataset-1",
                    "Agent Connected Reports": "NET_SALES Executive Report",
                    "Fully Qualified Name": "PBI_LINEAGE_DEMO.MART.FACT_SALES",
                    "Native Query Columns": "NET_SALES",
                }
            ],
            "measure_lineage": [
                {
                    "Agent Workspace Name": "Sales&Marketing",
                    "Agent Dataset ID": "dataset-1",
                    "Agent Connected Reports": "NET_SALES Executive Report",
                    "Measure Name": "Total NET_SALES",
                    "Fully Qualified Source Object": (
                        "PBI_LINEAGE_DEMO.MART.FACT_SALES.NET_SALES"
                    ),
                }
            ],
            "visual_usage": [
                {
                    "Agent Workspace Name": "Sales&Marketing",
                    "Agent Report Name": "NET_SALES Executive Report",
                    "Agent Report ID": "report-1",
                    "Page Name": "Overview",
                    "Visual Name": "Net sales card",
                    "Column / Measure Name": "Total NET_SALES",
                }
            ],
            "visual_coverage": [
                {
                    "report_id": "report-1",
                    "available": True,
                    "row_count": 1,
                }
            ],
            "fabric_authorized": True,
            "errors": [],
        }

        result = self.helpers["_search_claude_agent_estate"](
            index,
            "NET_SALES",
        )

        self.assertEqual(
            result["match_counts"],
            {
                "reports": 1,
                "semantic_objects": 1,
                "source_lineage": 1,
                "measure_lineage": 1,
                "visual_usage": 1,
            },
        )
        self.assertEqual(result["overview"]["report_count"], 1)
        self.assertEqual(result["overview"]["visual_metadata_reports"], 1)

    def test_search_limits_each_category_and_marks_truncation(self):
        index = {
            "workspaces": ["Sales"],
            "model_count": 1,
            "reports": [
                {
                    "Workspace Name": "Sales",
                    "Report Name": f"Sales report {index}",
                    "Report ID": f"report-{index}",
                }
                for index in range(3)
            ],
            "semantic_objects": [],
            "source_lineage": [],
            "measure_lineage": [],
            "visual_usage": [],
            "visual_coverage": [],
            "errors": [],
        }

        result = self.helpers["_search_claude_agent_estate"](
            index,
            "Sales",
            limit_per_category=2,
        )

        self.assertEqual(result["match_counts"]["reports"], 3)
        self.assertEqual(len(result["reports"]), 2)
        self.assertTrue(result["reports_truncated"])


if __name__ == "__main__":
    unittest.main()
