# Claude Configuration Reference

## Purpose

This reference explains every key in the `claude` configuration. It records
which runtime uses the key, what changing it does, and a practical setting for
efficient Power BI and Snowflake lineage work.

Keep populated settings only in ignored `config/app_settings.json`, ignored
`.streamlit/secrets.toml`, or the deployment secret store. Never commit API
keys, `env_...` identifiers, or `agent_...` identifiers.

The application resolves and bounds settings in
[pbi_modules/claude_agent.py:179](../pbi_modules/claude_agent.py:179). Defaults
are in [utils.py:34](../utils.py:34).

## Connection And Answer Settings

| Key | Runtime | Meaning | Efficient recommendation |
|---|---|---|---|
| `enabled` | Direct and Managed | Enables Claude features. | Keep `false` outside approved deployments; set `true` only where a valid key exists. |
| `api_key` | Direct and Managed | Anthropic API credential. | Put it only in a secret store. Never include it in Git or Claude Console instructions. |
| `base_url` | Direct and Managed | Anthropic API endpoint. | Keep `https://api.anthropic.com`; change only for an approved HTTPS proxy. |
| `model` | Direct; cost planning for Managed | Direct response model. In Managed mode, the Console agent chooses the real model. | Use an available Haiku model for routine analysis. Use Sonnet for high-quality final synthesis or deep report analysis. Mirror the Console model family for meaningful cost planning. |
| `timeout_seconds` | Direct and Managed | Maximum wait for an API/session call. Valid range: `10-300`. | `90` normally; `120-180` for slow networks or deep analysis. It improves patience, not answer quality. |
| `max_tokens` | Direct | Maximum output tokens for a normal direct agent answer. Valid range: `128-8192`. | `1200-1800` routine; `2400-3500` for a full report narrative. |
| `measure_definition_max_tokens` | Direct measure definition | Output limit for the Measure Detail Definition feature. | `600-900`; this is intentionally smaller than the lineage-chat answer. |
| `temperature` | Direct | Response randomness. | Keep `0` for repeatable, evidence-based lineage answers. Higher values do not improve factual metadata analysis. |

## Evidence And Conversation Settings

| Key | Runtime | Meaning | Efficient recommendation |
|---|---|---|---|
| `max_tool_rounds` | Direct | Maximum Claude tool-call cycles in one agent run. Valid range: `1-10`. | `4` for lookup, `6` for standard report analysis, `8` only for a focused deep investigation. |
| `max_tool_result_chars` | Direct | Maximum characters returned from one application tool to Claude. Valid range: `2000-100000`. | `12000-30000`. Raising it may add evidence but also raises input cost and can obscure the important facts. |
| `history_messages` | Direct | Number of earlier chat messages carried into the next call. Valid range: `2-40`. | `6-10`. It adds conversational context; it does not add report metadata. |
| `dax_expression_max_chars` | Measure definition | Maximum DAX expression text placed in one measure-definition prompt. | Keep `3000`; use `5000-8000` only when long DAX is essential. |
| `source_query_max_chars` | Measure definition | Maximum source-query text placed in one measure-definition prompt. | `1200-2500`. Long SQL often adds more token cost than explanation value. |

## Routing, Limits, And Cost Guards

| Key | Meaning | Efficient recommendation |
|---|---|---|
| `orchestration_mode` | `auto` routes simple questions to one agent and invokes specialists only for multi-domain questions. `single` always uses the general agent. `multi` requests specialists whenever the question warrants them. | Use `auto` as the normal default. |
| `max_specialist_agents` | Maximum number of selected specialists. Valid range: `1-4`. | `2` low-cost, `3` balanced, `4` only for an explicit deep report analysis. |
| `max_agent_calls` | Maximum total multi-agent runs, including reviewer and coordinator. Valid range: `1-6`. | `4` is balanced: two specialists, reviewer, and coordinator. Use `6` for the deepest route. |
| `max_total_input_tokens` | Planning guard for aggregate multi-agent input tokens. Valid range: `2000-200000`. | `20000-30000` routine; `50000-60000` for one deep report. It prevents more runs after usage is known, not a guaranteed billing cap. |
| `max_total_output_tokens` | Planning guard for aggregate multi-agent output. Valid range: `128-20000`. | `2400-3600` routine; `5000-7000` deep report. |
| `max_estimated_cost_usd` | App-side estimated-cost guard per multi-agent request. | Set to your approved request limit, such as `0.03-0.08` routinely and `0.30` only for an approved deep investigation. It is not an Anthropic invoice. |
| `specialist_max_tokens` | Output target for each direct specialist. Valid range: `128-2048`. | `500-700`; raise to `900-1200` only for deep source or visual work. |
| `evidence_reviewer_max_tokens` | Output target for direct evidence review. Valid range: `128-1536`. | `350-500`; reviewers should identify confirmation, conflict, and gaps concisely. |
| `coordinator_max_tokens` | Output target for the direct final multi-agent answer. Valid range: `128-2048`. | `700-1100` routine; `1500-1800` full report summary. This most directly changes final-answer length. |
| `shared_evidence_max_chars` | Maximum cached evidence supplied to specialists, reviewer, and coordinator. Valid range: `1000-30000`. | `4000-6000` routine; `8000-12000` for one focused report. More shared text is duplicated across agents and increases input cost. |

## Managed Agent Settings

These settings apply only when `agent_runtime` is `managed`. The session bridge
is implemented in [pbi_modules/claude_agent.py:715](../pbi_modules/claude_agent.py:715).

| Key | Meaning | Efficient recommendation |
|---|---|---|
| `agent_runtime` | Selects `direct` application-defined agents or `managed` Claude Console agents. | Keep `direct` until the Console agents and custom tools are ready; then set `managed`. Unsupported values become `direct`. |
| `managed_agents.enabled` | Enables the Managed Agent path. | Set `true` only with `agent_runtime = "managed"`; global `claude.enabled` must also be `true`. |
| `managed_agents.environment_id` | Shared Claude Console environment ID, beginning `env_`. | Use the one environment that contains all configured lineage agents. Required for every Managed Agent session. |
| `general_lineage_agent_id` | Agent ID for simple/Single questions. | Required for normal Home prompts. |
| `powerbi_semantic_agent_id` | Agent ID for report, dataset, table, column, measure, and DAX questions. | Configure for multi-agent operation. |
| `visual_evidence_agent_id` | Agent ID for page, visual, and field-usage evidence. | Configure when visual analysis is needed. |
| `snowflake_lineage_agent_id` | Agent ID for Snowflake table, view, column, and transformation lineage. | Configure when Snowflake lineage is enabled. |
| `impact_analysis_agent_id` | Agent ID for downstream measure/table/report impact. | Configure when impact analysis is needed. |
| `evidence_reviewer_agent_id` | Text-only agent that checks specialist agreement and gaps. | Configure for quality review. It runs only when at least two specialists finish and the run budget permits it. |
| `coordinator_agent_id` | Text-only agent that produces the final consolidated response. | Required for multi-agent operation. |
| `session_title_prefix` | Prefix shown in Claude Console session history. | Keep `PBI Lineage Explorer`; it is an operational label only. Maximum stored length: 120 characters. |
| `session_timeout_seconds` | Maximum wait for Managed Agent create, stream, send, and retrieve actions. Valid range: `30-600`. | `180`; use `240-300` only for deep work. It does not create a richer answer. |
| `max_custom_tool_events` | Maximum custom-tool calls one Managed Agent can request in a session. Valid range: `1-100`. | `12-20`. Keep it bounded to prevent loops and repeated metadata reads. |

Managed agent IDs must belong to `environment_id`. Tool-using agents receive
only the approved lineage custom tools. Built-in `bash`, `read`, `write`,
`edit`, `glob`, `grep`, `web_fetch`, and `web_search` remain denied. The
reviewer and coordinator need no tools.

## Instruction Settings

| Key | Meaning | Efficient recommendation |
|---|---|---|
| `instructions` | System instructions for the direct general lineage agent. | Retain read-only and evidence-only rules. Add concise output sections such as Answer, Evidence, Impact, and Gaps rather than lengthy business context. |
| `measure_definition_instructions` | System instructions for direct measure-definition explanations. | Keep the source-grounding rule and four current sections: Definition, Business meaning, DAX logic, Source lineage. |

Managed Agents use role-specific system instructions configured in Claude
Console. The application still enforces selected-workspace scope and local,
read-only tool execution.

## Recommended Profiles

Use only the keys that differ from the defaults.

### Cost-Efficient Daily Lookup

```json
{
  "model": "claude-haiku-4-5",
  "orchestration_mode": "auto",
  "max_tokens": 1200,
  "max_tool_rounds": 4,
  "max_tool_result_chars": 12000,
  "history_messages": 8,
  "max_specialist_agents": 2,
  "max_agent_calls": 3,
  "max_total_input_tokens": 20000,
  "max_total_output_tokens": 2400,
  "max_estimated_cost_usd": 0.03,
  "specialist_max_tokens": 500,
  "coordinator_max_tokens": 700,
  "shared_evidence_max_chars": 4000
}
```

### Balanced Production Default

```json
{
  "model": "claude-haiku-4-5",
  "orchestration_mode": "auto",
  "max_tokens": 1800,
  "max_tool_rounds": 6,
  "max_tool_result_chars": 30000,
  "history_messages": 10,
  "max_specialist_agents": 3,
  "max_agent_calls": 4,
  "max_total_input_tokens": 30000,
  "max_total_output_tokens": 3600,
  "max_estimated_cost_usd": 0.08,
  "specialist_max_tokens": 700,
  "evidence_reviewer_max_tokens": 500,
  "coordinator_max_tokens": 900,
  "shared_evidence_max_chars": 6000
}
```

### Deep One-Report Analysis

Use this only for a deliberate **Analyze entire report** action, not as the
global default.

```json
{
  "model": "claude-sonnet-4-6",
  "orchestration_mode": "multi",
  "max_tokens": 3000,
  "max_tool_rounds": 8,
  "max_tool_result_chars": 50000,
  "history_messages": 6,
  "max_specialist_agents": 4,
  "max_agent_calls": 6,
  "max_total_input_tokens": 60000,
  "max_total_output_tokens": 7000,
  "max_estimated_cost_usd": 0.30,
  "specialist_max_tokens": 1100,
  "evidence_reviewer_max_tokens": 800,
  "coordinator_max_tokens": 1800,
  "shared_evidence_max_chars": 12000
}
```

## Getting Complete Output For One Report

Increasing limits alone does not reliably create a complete report analysis.
The safer design is a dedicated workflow that retrieves, in order:

1. Report summary and semantic-model identity.
2. Semantic objects.
3. Source lineage.
4. Measure lineage and dependencies.
5. Visual usage when Fabric report-definition metadata is available.
6. Focused measure/table impact only for dependencies found above.
7. A final coordinator summary plus CSV/ZIP downloads for raw rows too large
   for chat.

Do not put all raw metadata into one unbounded prompt. That can exceed context,
increase cost, and make important facts easier to miss. For better one-report
answers, increase `coordinator_max_tokens`, `max_tool_rounds`, and
report-specific evidence coverage before increasing `history_messages` or
timeouts.
