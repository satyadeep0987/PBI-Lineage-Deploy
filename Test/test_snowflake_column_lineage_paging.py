"""Regression tests for Snowflake column-lineage continuation."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_fetch_snowflake_column_lineage_batch",
    "_fetch_snowflake_column_lineage",
}
CONSTANT_NAMES = {"_SNOWFLAKE_LINEAGE_BATCH_DEPTH"}


def _load_lineage_helpers():
    """Compile the lineage fetch helpers without executing the Streamlit app."""
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    helper_nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in HELPER_NAMES
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in CONSTANT_NAMES
                for target in node.targets
            )
        )
    ]
    namespace = {}
    helper_module = ast.Module(body=helper_nodes, type_ignores=[])
    exec(compile(helper_module, str(source_path), "exec"), namespace)
    return namespace


def _row(level, parent, source_object, source_column):
    return {
        "Starting_Source_Fully_Qualified_Name": "",
        "Starting_Source_Type": "COLUMN",
        "Selected_Source_Column": "",
        "Parent_Object_Name": parent,
        "Parent_Object_Type": "COLUMN",
        "Source_Fully_Qualified_Name": source_object,
        "Source_Column_Name": source_column,
        "Source_Object_Type": "COLUMN",
        "Lineage_Level": level,
        "Direction": "UPSTREAM",
        "Column_Transformation": "",
        "Modification_SQL": "",
    }


class SnowflakeColumnLineagePagingTests(unittest.TestCase):
    ROOT = "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY"
    ROOT_COLUMN = "NET_SALES"
    FRONTIER = "PBI_LINEAGE_DEMO.CORE.T_ORDER_FINANCIALS"

    def setUp(self):
        self.helpers = _load_lineage_helpers()
        self.fetch = self.helpers["_fetch_snowflake_column_lineage"]
        self.calls = []

        first_batch = [
            _row(0, f"{self.ROOT}.{self.ROOT_COLUMN}", self.ROOT, self.ROOT_COLUMN),
            _row(1, f"{self.ROOT}.{self.ROOT_COLUMN}", "PBI_LINEAGE_DEMO.MART.T_SALES_STORY", self.ROOT_COLUMN),
            _row(2, "PBI_LINEAGE_DEMO.MART.T_SALES_STORY.NET_SALES", "PBI_LINEAGE_DEMO.ANALYTICS.V_SALES_TARGET_STATUS", self.ROOT_COLUMN),
            _row(3, "PBI_LINEAGE_DEMO.ANALYTICS.V_SALES_TARGET_STATUS.NET_SALES", "PBI_LINEAGE_DEMO.ANALYTICS.T_ORDER_BEHAVIOR", self.ROOT_COLUMN),
            _row(4, "PBI_LINEAGE_DEMO.ANALYTICS.T_ORDER_BEHAVIOR.NET_SALES", "PBI_LINEAGE_DEMO.CORE.V_ORDER_BEHAVIOR", self.ROOT_COLUMN),
            _row(5, "PBI_LINEAGE_DEMO.CORE.V_ORDER_BEHAVIOR.NET_SALES", self.FRONTIER, self.ROOT_COLUMN),
        ]
        second_batch = [
            _row(0, f"{self.FRONTIER}.{self.ROOT_COLUMN}", self.FRONTIER, self.ROOT_COLUMN),
            _row(1, f"{self.FRONTIER}.{self.ROOT_COLUMN}", "PBI_LINEAGE_DEMO.CORE.V_ORDER_ENRICHED", "QUANTITY"),
            _row(2, "PBI_LINEAGE_DEMO.CORE.V_ORDER_ENRICHED.QUANTITY", "PBI_LINEAGE_DEMO.STAGE.T_ORDER_VALIDATED", "QUANTITY"),
            _row(3, "PBI_LINEAGE_DEMO.STAGE.T_ORDER_VALIDATED.QUANTITY", "PBI_LINEAGE_DEMO.STAGE.V_ORDER_VALIDATED", "QUANTITY"),
            _row(4, "PBI_LINEAGE_DEMO.STAGE.V_ORDER_VALIDATED.QUANTITY", "PBI_LINEAGE_DEMO.RAW.RAW_ORDER_LINES", "QUANTITY"),
        ]

        def fake_batch(conn, object_name, column_name, direction, depth, procedure_name):
            self.calls.append((object_name, column_name, depth))
            if object_name == self.ROOT:
                return first_batch
            if object_name == self.FRONTIER:
                return second_batch
            return []

        self.helpers["_fetch_snowflake_column_lineage_batch"] = fake_batch

    def test_continues_from_the_fifth_hop_and_adjusts_levels(self):
        rows = self.fetch(
            object(),
            self.ROOT,
            self.ROOT_COLUMN,
            "UPSTREAM",
            20,
            "COMMON_DB.COMMON_SCHEMA.TRACE_COLUMN_LINEAGE",
        )

        self.assertEqual(
            self.calls,
            [
                (self.ROOT, self.ROOT_COLUMN, 5),
                (self.FRONTIER, self.ROOT_COLUMN, 5),
            ],
        )
        self.assertEqual(max(row["Lineage_Level"] for row in rows), 9)
        raw_row = next(
            row
            for row in rows
            if row["Source_Fully_Qualified_Name"].endswith("RAW_ORDER_LINES")
        )
        self.assertEqual(raw_row["Lineage_Level"], 9)
        self.assertEqual(
            raw_row["Starting_Source_Fully_Qualified_Name"],
            self.ROOT,
        )
        self.assertEqual(raw_row["Selected_Source_Column"], self.ROOT_COLUMN)

    def test_does_not_continue_past_the_configured_depth(self):
        rows = self.fetch(
            object(),
            self.ROOT,
            self.ROOT_COLUMN,
            "UPSTREAM",
            5,
            "COMMON_DB.COMMON_SCHEMA.TRACE_COLUMN_LINEAGE",
        )

        self.assertEqual(self.calls, [(self.ROOT, self.ROOT_COLUMN, 5)])
        self.assertEqual(max(row["Lineage_Level"] for row in rows), 5)

    def test_omits_duplicate_level_zero_from_continuation_batch(self):
        rows = self.fetch(
            object(),
            self.ROOT,
            self.ROOT_COLUMN,
            "UPSTREAM",
            20,
            "COMMON_DB.COMMON_SCHEMA.TRACE_COLUMN_LINEAGE",
        )

        level_zero_rows = [
            row for row in rows if row["Lineage_Level"] == 0
        ]
        self.assertEqual(len(level_zero_rows), 1)


if __name__ == "__main__":
    unittest.main()
