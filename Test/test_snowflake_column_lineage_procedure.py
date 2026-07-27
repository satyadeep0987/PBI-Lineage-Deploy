"""Regression tests for the recursive Snowflake column-lineage procedure call."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {"_fetch_snowflake_column_lineage"}
CONSTANT_NAMES = {"_COLUMN_LINEAGE_RESULT_COLUMNS"}


def _load_lineage_helper():
    """Compile the column-lineage helper without running the Streamlit app."""
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    nodes = [
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
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"),
        namespace,
    )
    return namespace["_fetch_snowflake_column_lineage"]


class _FakeCursor:
    def __init__(self, columns, rows):
        self.description = [(column,) for column in columns]
        self._rows = rows
        self.executions = []
        self.closed = False

    def execute(self, query, parameters):
        self.executions.append((query, parameters))

    def fetchall(self):
        return list(self._rows)

    def close(self):
        self.closed = True


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class SnowflakeColumnLineageProcedureTests(unittest.TestCase):
    PROCEDURE = "SALES_ANALYTICS.REPORTING.TRACE_COLUMN_LINEAGE"
    ROOT = "PBI_LINEAGE_DEMO.MART.FACT_PBI_SALES_STORY"
    COLUMN = "NET_SALES"
    RESULT_COLUMNS = [
        "STARTING_SOURCE_FULLY_QUALIFIED_NAME",
        "STARTING_SOURCE_TYPE",
        "SELECTED_SOURCE_COLUMN",
        "PARENT_OBJECT_NAME",
        "PARENT_OBJECT_TYPE",
        "SOURCE_FULLY_QUALIFIED_NAME",
        "SOURCE_COLUMN_NAME",
        "SOURCE_OBJECT_TYPE",
        "LINEAGE_LEVEL",
        "DIRECTION",
        "COLUMN_TRANSFORMATION",
        "MODIFICATION_SQL",
    ]

    def setUp(self):
        self.fetch = _load_lineage_helper()

    def test_calls_the_recursive_procedure_once_with_the_requested_depth(self):
        procedure_row = {
            "STARTING_SOURCE_FULLY_QUALIFIED_NAME": self.ROOT,
            "STARTING_SOURCE_TYPE": "COLUMN",
            "SELECTED_SOURCE_COLUMN": self.COLUMN,
            "PARENT_OBJECT_NAME": "PBI_LINEAGE_DEMO.CORE.V_ORDER_ENRICHED.NET_SALES",
            "PARENT_OBJECT_TYPE": "COLUMN",
            "SOURCE_FULLY_QUALIFIED_NAME": "PBI_LINEAGE_DEMO.RAW.RAW_ORDER_LINES",
            "SOURCE_COLUMN_NAME": "UNIT_PRICE",
            "SOURCE_OBJECT_TYPE": "COLUMN",
            "LINEAGE_LEVEL": 9,
            "DIRECTION": "UPSTREAM",
            "COLUMN_TRANSFORMATION": "TRY_TO_DECIMAL(UNIT_PRICE, 14, 2)",
            "MODIFICATION_SQL": "CREATE OR REPLACE VIEW ...",
        }
        cursor = _FakeCursor(
            self.RESULT_COLUMNS,
            [tuple(procedure_row[column] for column in self.RESULT_COLUMNS)],
        )

        rows = self.fetch(
            _FakeConnection(cursor),
            self.ROOT,
            self.COLUMN,
            "UPSTREAM",
            50,
            self.PROCEDURE,
        )

        self.assertEqual(
            cursor.executions,
            [
                (
                    f"CALL {self.PROCEDURE}(%s, %s, %s, %s)",
                    (self.ROOT, self.COLUMN, "UPSTREAM", 50),
                )
            ],
        )
        self.assertTrue(cursor.closed)
        self.assertEqual(
            rows,
            [
                {
                    "Starting_Source_Fully_Qualified_Name": self.ROOT,
                    "Starting_Source_Type": "COLUMN",
                    "Selected_Source_Column": self.COLUMN,
                    "Parent_Object_Name": "PBI_LINEAGE_DEMO.CORE.V_ORDER_ENRICHED.NET_SALES",
                    "Parent_Object_Type": "COLUMN",
                    "Source_Fully_Qualified_Name": "PBI_LINEAGE_DEMO.RAW.RAW_ORDER_LINES",
                    "Source_Column_Name": "UNIT_PRICE",
                    "Source_Object_Type": "COLUMN",
                    "Lineage_Level": 9,
                    "Direction": "UPSTREAM",
                    "Column_Transformation": "TRY_TO_DECIMAL(UNIT_PRICE, 14, 2)",
                    "Modification_SQL": "CREATE OR REPLACE VIEW ...",
                }
            ],
        )

    def test_rejects_a_procedure_result_without_the_required_columns(self):
        cursor = _FakeCursor(["PARENT_OBJECT_NAME"], [])

        with self.assertRaisesRegex(RuntimeError, "Missing columns"):
            self.fetch(
                _FakeConnection(cursor),
                self.ROOT,
                self.COLUMN,
                "UPSTREAM",
                50,
                self.PROCEDURE,
            )

        self.assertTrue(cursor.closed)


if __name__ == "__main__":
    unittest.main()
