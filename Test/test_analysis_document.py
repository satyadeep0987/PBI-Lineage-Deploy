"""Tests for local Markdown export of Claude lineage output."""

from __future__ import annotations

import unittest

from pbi_modules.analysis_document import (
    build_claude_analysis_markdown,
    claude_analysis_markdown_filename,
)


class AnalysisDocumentTests(unittest.TestCase):
    def test_markdown_contains_response_activity_and_local_note(self):
        document = build_claude_analysis_markdown(
            question="Trace Northstar Sales",
            answer="## Answer\nThe report uses Sales Story.",
            context={
                "workspace_names": ["Sales&Marketing"],
                "model": "claude-haiku-4-5",
                "runtime": "Managed",
                "strategy": "Multi",
            },
            trace=[
                {
                    "agent": "Power BI semantic specialist",
                    "tool": "inspect_report_lineage",
                    "status": "completed",
                    "duration_ms": 20,
                    "summary": "1 result",
                }
            ],
            usage={"input_tokens": 100, "output_tokens": 50},
            orchestration={"mode": "multi", "selected_agents": ["powerbi_semantic"]},
            evidence_packets=[
                {
                    "tool": "inspect_report_lineage",
                    "input": {"report_id": "report-1"},
                    "content": '{"report": "Northstar"}',
                }
            ],
            created_at=0,
        )

        self.assertIn("# Power BI Lineage Analysis", document)
        self.assertIn("```mermaid", document)
        self.assertIn("inspect_report_lineage", document)
        self.assertIn("Sales&Marketing", document)
        self.assertIn("No additional Claude request", document)

    def test_filename_is_safe_and_uses_markdown_extension(self):
        filename = claude_analysis_markdown_filename("Sales / Northstar", created_at=0)

        self.assertTrue(filename.startswith("pbi_lineage_Sales_Northstar_19700101_"))
        self.assertTrue(filename.endswith(".md"))


if __name__ == "__main__":
    unittest.main()
