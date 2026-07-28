"""Regression tests for the Measure Source Lineage display contract."""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_normalize_ui_column_name",
    "_normalize_dataframe_column_names",
    "_apply_lineage_display_contract",
}


def _load_display_contract():
    """Compile the display helpers without executing the Streamlit application."""
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
    namespace = {"pd": pd, "re": re}
    exec(
        compile(ast.Module(body=helper_nodes, type_ignores=[]), str(source_path), "exec"),
        namespace,
    )
    return namespace["_apply_lineage_display_contract"]


class MeasureSourceLineageDisplayTests(unittest.TestCase):
    def test_blank_measure_name_uses_the_semantic_object_name(self):
        apply_contract = _load_display_contract()
        lineage_df = pd.DataFrame(
            [
                {
                    "Target_Scope_Type": "Workspace",
                    "Target_Workspace_Name": "Sales&Marketing",
                    "Target_Report_Name": "Northstar_Sales_Lineage_Demo",
                    "Target_Semantic_Object_Type": "CALC_COLUMN",
                    "Target_Semantic_Table_View": "Sales Story",
                    "Target_Semantic_Object_Name": "Gross Margin %",
                    "Target_DAX_Expression": "DIVIDE([Gross Profit], [Total Revenue])",
                    "Target_Measure_Name": "",
                    "Source_Object_Type": "Table",
                    "Source_Column_Name": "NET_SALES",
                    "Source_Fully_Qualified_Object": "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY",
                },
                {
                    "Target_Scope_Type": "Workspace",
                    "Target_Workspace_Name": "Sales&Marketing",
                    "Target_Report_Name": "Northstar_Sales_Lineage_Demo",
                    "Target_Semantic_Object_Type": "MEASURE",
                    "Target_Semantic_Table_View": "Sales Story",
                    "Target_Semantic_Object_Name": "Total Revenue",
                    "Target_DAX_Expression": "SUM('Sales Story'[NET_SALES])",
                    "Target_Measure_Name": "Total Revenue",
                    "Source_Object_Type": "Table",
                    "Source_Column_Name": "NET_SALES",
                    "Source_Fully_Qualified_Object": "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY",
                },
            ]
        )

        display_df = apply_contract(lineage_df, "measure_source_lineage")

        self.assertEqual(
            display_df["Semantic_Measure_Name"].tolist(),
            ["Gross Margin %", "Total Revenue"],
        )
        self.assertEqual(
            display_df["Semantic_Object_Name"].tolist(),
            ["Gross Margin %", "Total Revenue"],
        )


if __name__ == "__main__":
    unittest.main()
