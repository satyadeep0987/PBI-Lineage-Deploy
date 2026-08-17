"""Regression tests for report/model workspace separation and upstream expansion."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from pbi_modules.app_shell import direct_report_context


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    "_prefer_non_na",
    "_is_meaningful_value",
    "_report_workspace_id",
    "_semantic_workspace_id",
    "_normalize_model_context",
    "_workspace_scan_contexts",
    "_expand_contexts_from_workspace_scan",
}


def _load_context_helpers():
    source_path = PROJECT_ROOT / "streamlit_app.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"), filename=str(source_path))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS
    ]
    found = {node.name for node in nodes}
    if found != HELPERS:
        raise AssertionError(f"Missing context helpers: {sorted(HELPERS - found)}")
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace


class MultiSemanticContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_context_helpers()

    def test_direct_context_keeps_report_and_model_workspaces_separate(self):
        context = direct_report_context({
            "Workspace Name": "Report Workspace",
            "Workspace ID": "report-ws",
            "Report Name": "Sales",
            "Report ID": "report-1",
            "Dataset ID": "model-1",
            "Dataset Workspace ID": "model-ws",
        })

        self.assertEqual(context["Report Workspace ID"], "report-ws")
        self.assertEqual(context["Semantic Model Workspace ID"], "model-ws")
        self.assertEqual(context["Target Workspace ID"], "model-ws")

    def test_workspace_scan_adds_upstream_model_without_rebinding_report(self):
        context = direct_report_context({
            "Workspace Name": "Sales Reports",
            "Workspace ID": "report-ws",
            "Report Name": "Sales",
            "Report ID": "report-1",
            "Dataset ID": "sales-model",
        })
        scan_result = {
            "workspaces": [
                {
                    "id": "report-ws",
                    "name": "Sales Reports",
                    "reports": [{
                        "id": "report-1",
                        "datasetId": "sales-model",
                        "datasetWorkspaceId": "sales-model-ws",
                    }],
                    "datasets": [],
                },
                {
                    "id": "sales-model-ws",
                    "name": "Sales Models",
                    "datasets": [{
                        "id": "sales-model",
                        "name": "Sales Composite",
                        "upstreamDatasets": [{
                            "groupId": "native-ws",
                            "targetDatasetId": "native-model",
                        }],
                    }],
                    "reports": [],
                },
                {
                    "id": "native-ws",
                    "name": "Native",
                    "datasets": [{
                        "id": "native-model",
                        "name": "NativeQueryReport",
                        "upstreamDatasets": [],
                    }],
                    "reports": [],
                },
            ]
        }

        expanded = self.helpers["_expand_contexts_from_workspace_scan"](
            [context],
            scan_result,
        )
        by_dataset = {item["Dataset ID"]: item for item in expanded}

        self.assertEqual(set(by_dataset), {"sales-model", "native-model"})
        self.assertEqual(by_dataset["sales-model"]["Semantic Model Workspace ID"], "sales-model-ws")
        self.assertEqual(by_dataset["native-model"]["Semantic Model Workspace ID"], "native-ws")
        self.assertEqual(by_dataset["native-model"]["Model Role"], "Upstream")
        self.assertEqual(by_dataset["native-model"]["Lineage Depth"], 1)
        self.assertEqual(by_dataset["native-model"]["Report Workspace ID"], "report-ws")
        self.assertEqual(by_dataset["native-model"]["Primary Dataset ID"], "sales-model")


if __name__ == "__main__":
    unittest.main()
