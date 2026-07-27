"""Tests for extracting rendered-diagram inputs from PowerAI tool evidence."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_powerai_json_content",
    "_powerai_snowflake_diagram_specs",
}


def _load_diagram_helpers():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    nodes = [
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in HELPER_NAMES
    ]
    found = {node.name for node in nodes}
    if found != HELPER_NAMES:
        raise AssertionError(f"Missing helpers: {HELPER_NAMES - found}")
    namespace = {"json": json}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class PowerAiLineageDiagramTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_diagram_helpers()

    def test_extracts_column_diagram_from_direct_tool_evidence(self):
        packets = [{
            "tool": "trace_snowflake_lineage",
            "content": json.dumps({
                "object_name": "DEMO.CORE.SALES_VIEW",
                "object_type": "COLUMN",
                "column_name": "NET_SALES",
                "direction": "UPSTREAM",
                "rows": [{
                    "Parent_Object_Name": "DEMO.CORE.SALES_VIEW.NET_SALES",
                    "Source_Fully_Qualified_Name": "RAW.SALES",
                    "Source_Column_Name": "AMOUNT",
                    "Source_Object_Type": "TABLE",
                    "Lineage_Level": 1,
                }],
            }),
        }]

        specs = self.helpers["_powerai_snowflake_diagram_specs"](packets)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["lineage_grain"], "COLUMN")
        self.assertEqual(specs[0]["payload"]["source_column"], "NET_SALES")

    def test_extracts_table_diagram_from_nested_multi_agent_evidence(self):
        trace_packet = {
            "tool": "trace_snowflake_lineage",
            "content": json.dumps({
                "object_name": "DEMO.CORE.SALES_VIEW",
                "object_type": "VIEW",
                "direction": "UPSTREAM",
                "rows": [{
                    "Parent_Object_Name": "DEMO.CORE.SALES_VIEW",
                    "Source_Fully_Qualified_Name": "RAW.SALES",
                    "Source_Object_Type": "TABLE",
                    "Lineage_Level": 1,
                }],
            }),
        }
        packets = [{"content": json.dumps({"evidence": [trace_packet]})}]

        specs = self.helpers["_powerai_snowflake_diagram_specs"](packets)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["lineage_grain"], "OBJECT")
        self.assertEqual(specs[0]["source_type"], "VIEW")


if __name__ == "__main__":
    unittest.main()
