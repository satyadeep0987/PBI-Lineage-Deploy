"""Permission-boundary tests for the Streamlit Claude tool executor helpers."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPER_NAMES = {
    "_agent_text_argument",
    "_agent_scoped_records",
    "_agent_find_report",
}


def _load_scope_helpers():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(
        source_path.read_text(encoding="utf-8-sig"),
        filename=str(source_path),
    )
    helper_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPER_NAMES
    ]
    found = {node.name for node in helper_nodes}
    if found != HELPER_NAMES:
        raise AssertionError(f"Missing Claude scope helpers: {HELPER_NAMES - found}")

    namespace = {}
    exec(
        compile(ast.Module(body=helper_nodes, type_ignores=[]), str(source_path), "exec"),
        namespace,
    )
    return namespace


class ClaudeAgentScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_scope_helpers()
        cls.records = [
            {
                "Workspace Name": "Sales&Marketing",
                "Report Name": "Northstar",
                "Report ID": "report-1",
            },
            {
                "Workspace Name": "Restricted",
                "Report Name": "Finance",
                "Report ID": "report-2",
            },
        ]

    def test_workspace_matching_is_case_insensitive(self):
        scoped = self.helpers["_agent_scoped_records"](
            self.records,
            ["Sales&Marketing"],
            ["sales&marketing"],
        )

        self.assertEqual([row["Report ID"] for row in scoped], ["report-1"])

    def test_workspace_outside_page_scope_is_rejected(self):
        with self.assertRaises(PermissionError):
            self.helpers["_agent_scoped_records"](
                self.records,
                ["Sales&Marketing"],
                ["Restricted"],
            )

    def test_report_outside_scoped_inventory_is_rejected(self):
        scoped = [self.records[0]]

        with self.assertRaises(PermissionError):
            self.helpers["_agent_find_report"](scoped, "report-2")

    def test_tool_text_rejects_statement_separator(self):
        with self.assertRaises(ValueError):
            self.helpers["_agent_text_argument"](
                "TABLE_NAME; DROP TABLE OTHER",
                "table_name",
            )


if __name__ == "__main__":
    unittest.main()
