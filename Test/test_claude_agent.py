"""Unit tests for the direct Claude client and bounded tool loop."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pbi_modules.claude_agent import (
    ClaudeAgentError,
    ClaudeConfigurationError,
    generate_claude_text,
    lineage_agent_tool_definitions,
    resolve_claude_settings,
    run_claude_agent,
)


def _settings(**overrides):
    settings = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://api.anthropic.com",
        "model": "claude-test",
        "max_tokens": 500,
        "max_tool_rounds": 3,
        "max_tool_result_chars": 5000,
        "history_messages": 10,
        "instructions": "Use only tool evidence.",
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


class ClaudeConfigurationTests(unittest.TestCase):
    def test_disabled_configuration_is_rejected(self):
        with self.assertRaises(ClaudeConfigurationError):
            resolve_claude_settings({"enabled": False, "api_key": "test"})

    def test_missing_api_key_is_rejected(self):
        with self.assertRaises(ClaudeConfigurationError):
            resolve_claude_settings({"enabled": True, "api_key": ""})

    def test_limits_are_bounded(self):
        settings = resolve_claude_settings(
            _settings(max_tool_rounds=999, max_tool_result_chars=1)
        )

        self.assertEqual(settings["max_tool_rounds"], 10)
        self.assertEqual(settings["max_tool_result_chars"], 2000)

    def test_environment_can_enable_and_select_model(self):
        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "environment-key",
                "CLAUDE_ENABLED": "true",
                "CLAUDE_MODEL": "claude-environment-model",
            },
            clear=False,
        ):
            settings = resolve_claude_settings(
                {"enabled": False, "api_key": "", "model": "configured-model"}
            )

        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["api_key"], "environment-key")
        self.assertEqual(settings["model"], "claude-environment-model")


class ClaudeTextTests(unittest.TestCase):
    def test_generate_text_uses_configured_model_and_system(self):
        client = _FakeClient([_response([{"type": "text", "text": "Definition"}])])

        result = generate_claude_text(
            "measure metadata",
            _settings(),
            system="Measure analyst",
            client=client,
        )

        self.assertEqual(result, "Definition")
        request = client.messages.calls[0]
        self.assertEqual(request["model"], "claude-test")
        self.assertEqual(request["system"], "Measure analyst")


class ClaudeAgentLoopTests(unittest.TestCase):
    def test_executes_tool_and_returns_grounded_answer(self):
        client = _FakeClient(
            [
                _response(
                    [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "search_reports",
                            "input": {"query": "sales"},
                        }
                    ],
                    input_tokens=12,
                    output_tokens=4,
                ),
                _response(
                    [{"type": "text", "text": "One authorized sales report was found."}],
                    input_tokens=18,
                    output_tokens=7,
                ),
            ]
        )
        executions = []

        def execute(name, arguments):
            executions.append((name, arguments))
            return {"count": 1, "reports": [{"report_name": "Sales"}]}

        result = run_claude_agent(
            [{"role": "user", "content": "Find sales reports"}],
            _settings(),
            execute,
            client=client,
        )

        self.assertEqual(
            executions,
            [("search_reports", {"query": "sales"})],
        )
        self.assertIn("authorized sales report", result["text"])
        self.assertEqual(result["trace"][0]["status"], "completed")
        self.assertEqual(result["usage"]["input_tokens"], 30)
        self.assertEqual(result["usage"]["output_tokens"], 11)
        second_request_messages = client.messages.calls[1]["messages"]
        self.assertEqual(second_request_messages[-1]["role"], "user")
        self.assertEqual(
            second_request_messages[-1]["content"][0]["type"],
            "tool_result",
        )

    def test_tool_error_is_returned_to_claude_for_final_explanation(self):
        client = _FakeClient(
            [
                _response(
                    [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "inspect_report_lineage",
                            "input": {
                                "report_id": "outside-scope",
                                "lineage_type": "summary",
                            },
                        }
                    ]
                ),
                _response(
                    [{"type": "text", "text": "The report is outside your scope."}]
                ),
            ]
        )

        result = run_claude_agent(
            [{"role": "user", "content": "Inspect the report"}],
            _settings(),
            lambda _name, _arguments: (_ for _ in ()).throw(
                PermissionError("Report outside authorized scope")
            ),
            client=client,
        )

        self.assertEqual(result["trace"][0]["status"], "error")
        tool_result = client.messages.calls[1]["messages"][-1]["content"][0]
        self.assertTrue(tool_result["is_error"])
        self.assertIn("authorized scope", tool_result["content"])

    def test_can_require_tool_evidence_on_the_first_round(self):
        client = _FakeClient(
            [
                _response(
                    [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "get_lineage_estate_overview",
                            "input": {},
                        }
                    ]
                ),
                _response([{"type": "text", "text": "The estate has four reports."}]),
            ]
        )

        run_claude_agent(
            [{"role": "user", "content": "Summarize my estate"}],
            _settings(),
            lambda _name, _arguments: {"report_count": 4},
            require_initial_tool=True,
            client=client,
        )

        self.assertEqual(
            client.messages.calls[0]["tool_choice"],
            {"type": "any"},
        )
        self.assertNotIn("tool_choice", client.messages.calls[1])

    def test_stops_after_configured_tool_round_limit(self):
        tool_call = [
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "search_reports",
                "input": {"query": "sales"},
            }
        ]
        client = _FakeClient([_response(tool_call), _response(tool_call)])

        with self.assertRaises(ClaudeAgentError):
            run_claude_agent(
                [{"role": "user", "content": "Keep searching"}],
                _settings(max_tool_rounds=1),
                lambda _name, _arguments: {"count": 0},
                client=client,
            )

    def test_only_read_only_tools_are_exposed(self):
        names = {tool["name"] for tool in lineage_agent_tool_definitions()}

        self.assertEqual(
            names,
            {
                "get_lineage_estate_overview",
                "search_entire_lineage",
                "search_reports",
                "inspect_report_lineage",
                "analyze_measure_impact",
                "analyze_table_impact",
                "trace_snowflake_lineage",
            },
        )
        self.assertFalse(any("delete" in name or "write" in name for name in names))

    def test_column_lineage_tool_can_request_the_recursive_procedure_depth(self):
        trace_tool = next(
            tool
            for tool in lineage_agent_tool_definitions(max_snowflake_depth=50)
            if tool["name"] == "trace_snowflake_lineage"
        )

        self.assertEqual(
            trace_tool["input_schema"]["properties"]["max_depth"]["maximum"],
            50,
        )


if __name__ == "__main__":
    unittest.main()
