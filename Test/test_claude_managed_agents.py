"""Tests for Claude Managed Agents configuration and custom-tool bridging."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from pbi_modules.claude_agent import (
    managed_agent_configuration_status,
    run_claude_managed_agent,
)


def _settings(**overrides):
    settings = {
        "enabled": True,
        "api_key": "test-key",
        "base_url": "https://api.anthropic.com",
        "model": "claude-haiku-4-5",
        "timeout_seconds": 90,
        "max_tool_result_chars": 5000,
        "agent_runtime": "managed",
        "managed_agents": {
            "enabled": True,
            "environment_id": "env-test",
            "general_lineage_agent_id": "agent-general",
            "powerbi_semantic_agent_id": "agent-powerbi",
            "visual_evidence_agent_id": "agent-visual",
            "snowflake_lineage_agent_id": "agent-snowflake",
            "impact_analysis_agent_id": "agent-impact",
            "evidence_reviewer_agent_id": "agent-reviewer",
            "coordinator_agent_id": "agent-coordinator",
        },
    }
    settings.update(overrides)
    return settings


class _FakeStream:
    def __init__(self, events):
        self.events = list(events)

    def __enter__(self):
        return iter(self.events)

    def __exit__(self, _type, _value, _traceback):
        return False


class _FakeEvents:
    def __init__(self, stream_events):
        self.stream_events = stream_events
        self.sent = []

    def stream(self, _session_id, **_kwargs):
        return _FakeStream(self.stream_events)

    def send(self, _session_id, *, events, **_kwargs):
        self.sent.extend(events)


class _FakeSessions:
    def __init__(self, stream_events):
        self.events = _FakeEvents(stream_events)
        self.created = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id="session-test", usage=None)

    def retrieve(self, _session_id, **_kwargs):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=123, output_tokens=45)
        )


class _FakeManagedClient:
    def __init__(self, stream_events):
        self.beta = SimpleNamespace(sessions=_FakeSessions(stream_events))


class ClaudeManagedAgentTests(unittest.TestCase):
    def test_configuration_status_reports_missing_agent_role(self):
        settings = _settings(
            managed_agents={
                "enabled": True,
                "environment_id": "env-test",
                "general_lineage_agent_id": "agent-general",
            }
        )

        status = managed_agent_configuration_status(
            settings,
            ["general_lineage", "coordinator"],
        )

        self.assertFalse(status["ready"])
        self.assertEqual(status["missing_roles"], ["coordinator"])

    def test_managed_agent_bridges_custom_tool_to_local_executor(self):
        custom_tool_event = SimpleNamespace(
            type="agent.custom_tool_use",
            id="custom-tool-1",
            name="search_entire_lineage",
            input={"query": "NET_SALES"},
        )
        agent_message = SimpleNamespace(
            type="agent.message",
            content=[{"type": "text", "text": "Managed result."}],
        )
        client = _FakeManagedClient(
            [custom_tool_event, agent_message, SimpleNamespace(type="end_turn")]
        )
        tool_calls = []

        result = run_claude_managed_agent(
            "general_lineage",
            "Find NET_SALES.",
            _settings(),
            lambda name, arguments: tool_calls.append((name, arguments))
            or {"count": 1},
            client=client,
        )

        self.assertEqual(result["text"], "Managed result.")
        self.assertEqual(result["managed_session_id"], "session-test")
        self.assertEqual(result["usage"], {"input_tokens": 123, "output_tokens": 45})
        self.assertEqual(tool_calls, [("search_entire_lineage", {"query": "NET_SALES"})])
        self.assertEqual(
            client.beta.sessions.created[0]["agent"],
            "agent-general",
        )
        self.assertEqual(
            client.beta.sessions.created[0]["environment_id"],
            "env-test",
        )
        self.assertEqual(
            client.beta.sessions.events.sent[0]["type"],
            "user.custom_tool_result",
        )


if __name__ == "__main__":
    unittest.main()
