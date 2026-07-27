"""Tests for the budgeted Claude multi-agent orchestration path."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pbi_modules.claude_agent import (
    lineage_agent_tool_definitions,
    plan_claude_agent_route,
    question_requests_lineage_diagram,
    question_requests_visual_details,
    resolve_claude_settings,
    run_claude_orchestrated_agent,
)


def _settings(**overrides):
    settings = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-4-5",
        "max_tokens": 500,
        "max_tool_rounds": 3,
        "max_tool_result_chars": 5000,
        "history_messages": 10,
        "instructions": "Use only tool evidence.",
        "orchestration_mode": "auto",
        "max_specialist_agents": 3,
        "max_agent_calls": 4,
        "max_total_input_tokens": 30000,
        "max_total_output_tokens": 3600,
        "max_estimated_cost_usd": 0.08,
    }
    settings.update(overrides)
    return settings


class _FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake Claude response remains.")
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


def _response(content, input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def _tool_response(tool_id, tool_name="search_entire_lineage"):
    return _response(
        [
            {
                "type": "tool_use",
                "id": tool_id,
                "name": tool_name,
                "input": {"query": "NET_SALES"},
            }
        ]
    )


class ClaudeMultiAgentRouteTests(unittest.TestCase):
    def test_simple_database_table_question_uses_single_agent(self):
        route = plan_claude_agent_route(
            "Give me the name of db tables used in Sales and Marketing workspace",
            _settings(),
        )

        self.assertEqual(route["mode"], "single")

    def test_cross_system_question_selects_specialists(self):
        route = plan_claude_agent_route(
            "Trace NET_SALES from Snowflake source through the semantic model to report "
            "visuals and downstream impact.",
            _settings(max_specialist_agents=4),
        )

        self.assertEqual(route["mode"], "multi")
        self.assertEqual(
            route["specialists"],
            [
                "powerbi_semantic",
                "visual_evidence",
                "snowflake_lineage",
                "impact_analysis",
            ],
        )

    def test_visual_specialist_requires_an_explicit_visual_request(self):
        self.assertFalse(
            question_requests_visual_details(
                "Explain the lineage for the Northstar report and its source tables."
            )
        )
        self.assertTrue(
            question_requests_visual_details(
                "Which report visuals use the Total Sales measure?"
            )
        )
        route = plan_claude_agent_route(
            "Trace NET_SALES from Snowflake through the semantic model to downstream impact.",
            _settings(max_specialist_agents=4),
        )
        self.assertNotIn("visual_evidence", route["specialists"])

    def test_lineage_diagram_request_selects_snowflake_specialist(self):
        question = "Draw a lineage diagram for the Net Sales measure and its source column."
        self.assertTrue(question_requests_lineage_diagram(question))

        route = plan_claude_agent_route(
            question,
            _settings(max_specialist_agents=4),
        )

        self.assertIn("snowflake_lineage", route["specialists"])

    def test_haiku_cost_defaults_are_selected_from_model_name(self):
        settings = resolve_claude_settings(_settings())

        self.assertEqual(settings["estimated_input_cost_per_million"], 1.0)
        self.assertEqual(settings["estimated_output_cost_per_million"], 5.0)


class ClaudeMultiAgentExecutionTests(unittest.TestCase):
    def test_two_specialists_review_and_coordinate_shared_evidence(self):
        client = _FakeClient(
            [
                _tool_response("powerbi-tool"),
                _response([{"type": "text", "text": "Power BI mapping found."}]),
                _tool_response("snowflake-tool"),
                _response([{"type": "text", "text": "Snowflake source found."}]),
                _response([{"type": "text", "text": "Evidence is consistent."}]),
                _response([{"type": "text", "text": "NET_SALES is mapped end to end."}]),
            ]
        )
        tool_calls = []
        progress_events = []

        def execute(name, arguments):
            tool_calls.append((name, arguments))
            return {"count": 1, "source": "PBI_LINEAGE_DEMO.MART.FACT_SALES"}

        result = run_claude_orchestrated_agent(
            [{"role": "user", "content": "Trace NET_SALES end to end."}],
            _settings(max_specialist_agents=2),
            execute,
            tools=lineage_agent_tool_definitions(),
            shared_evidence={"estate_overview": {"report_count": 1}},
            route={
                "mode": "multi",
                "reason": "Cross-system test",
                "specialists": ["powerbi_semantic", "snowflake_lineage"],
            },
            client=client,
            progress_callback=lambda stage, status: progress_events.append((stage, status)),
        )

        self.assertEqual(result["text"], "NET_SALES is mapped end to end.")
        self.assertEqual(
            [name for name, _arguments in tool_calls],
            ["search_entire_lineage", "search_entire_lineage"],
        )
        self.assertTrue(result["orchestration"]["shared_evidence_used"])
        self.assertTrue(result["orchestration"]["evidence_reviewer_used"])
        self.assertEqual(result["orchestration"]["budget"]["agent_runs"], 4)
        self.assertEqual(result["usage"], {"input_tokens": 60, "output_tokens": 30})
        self.assertEqual(
            progress_events,
            [
                ("powerbi_semantic", "running"),
                ("powerbi_semantic", "completed"),
                ("snowflake_lineage", "running"),
                ("snowflake_lineage", "completed"),
                ("evidence_reviewer", "running"),
                ("evidence_reviewer", "completed"),
                ("coordinator", "running"),
                ("coordinator", "completed"),
            ],
        )

    def test_agent_run_cap_reserves_a_final_coordinator(self):
        client = _FakeClient(
            [
                _tool_response("powerbi-tool"),
                _response([{"type": "text", "text": "Power BI mapping found."}]),
                _response([{"type": "text", "text": "Final answer."}]),
            ]
        )

        result = run_claude_orchestrated_agent(
            [{"role": "user", "content": "Trace NET_SALES end to end."}],
            _settings(max_agent_calls=2, max_specialist_agents=2),
            lambda _name, _arguments: {"count": 1},
            tools=lineage_agent_tool_definitions(),
            shared_evidence={"estate_overview": {"report_count": 1}},
            route={
                "mode": "multi",
                "reason": "Budget test",
                "specialists": ["powerbi_semantic", "snowflake_lineage"],
            },
            client=client,
        )

        self.assertEqual(result["text"], "Final answer.")
        self.assertEqual(result["orchestration"]["completed_agents"], ["powerbi_semantic"])
        self.assertIn("snowflake_lineage (budget guard)", result["orchestration"]["skipped"])
        self.assertEqual(result["orchestration"]["budget"]["agent_runs"], 2)


if __name__ == "__main__":
    unittest.main()
