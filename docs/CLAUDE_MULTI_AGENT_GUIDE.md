# Claude Multi-Agent Guide

## Purpose

This guide documents the Claude implementation in PBI Lineage Explorer and the
multi-agent extension. It explains why each part exists, what it does, which
function calls it, and what it returns. The line references were verified against
the current implementation and will naturally move if code is edited later.

The application does not send Power BI, Fabric, XMLA, Snowflake, or browser
credentials to Claude. Claude receives only bounded metadata returned by the
application's existing read-only tools.

## Visual Evidence Gate

Visual-level metadata is deliberately excluded by default. A prompt about a
report, measure, table, model, source, or impact receives only non-visual
lineage evidence. The application enables visual evidence only when the user
explicitly asks for visuals, visual usage, charts, cards, slicers, or visual
details. This gate is enforced at the tool executor, so a model cannot request
report-page or visual-field data during an ordinary lineage prompt. Non-visual
turns also send only the current user question to Claude, preventing visual
details from an earlier chat answer being included in a later request.

Claude prompts request only `Answer`, `Evidence`, and `Impact` sections. The
application does not request or render a separate `Gaps` section.

## Where The Agents Are Created

The application supports two runtimes:

| Runtime | Where agent behavior lives | When to use it |
|---|---|---|
| `direct` | This application's Python prompts, tool allowlists, and bounded Anthropic Messages API calls | Default and simplest deployment. No Claude Console agent IDs are required. |
| `managed` | Reusable/versioned agents created in Anthropic Console and referenced by `agent_...` IDs | Use this when your organization has approved Managed Agents and wants their lifecycle governed in Claude. |

For Managed Agents, the application still owns Power BI, Fabric, XMLA, and
Snowflake access. It passes only bounded, read-only results back to Claude via
custom-tool responses. Do not place Power BI, Snowflake, Entra, browser, or
database credentials in Claude Console agent configuration.

## Configure Your Existing Claude Managed Agents

Use this section after creating the agents in Anthropic Console.

1. In Claude Console, copy the shared environment ID beginning with `env_` and
   each relevant agent ID beginning with `agent_`.
2. Configure every agent that the application may run. In normal `Auto` mode,
   configure the general agent and the specialist agents you intend to allow.
   Multi-agent requests also need the coordinator; the reviewer is needed only
   when the configured run budget permits it.

| Application role | Secrets key | Claude Console agent purpose |
|---|---|---|
| General lineage | `general_lineage_agent_id` | Standard report, model, measure, source, and impact questions |
| Power BI semantic | `powerbi_semantic_agent_id` | Semantic objects, DAX, and report/model mappings |
| Visual evidence | `visual_evidence_agent_id` | Report-page and visual-field evidence |
| Snowflake lineage | `snowflake_lineage_agent_id` | Source objects, columns, and transformations |
| Impact analysis | `impact_analysis_agent_id` | Measure/table downstream impact |
| Evidence reviewer | `evidence_reviewer_agent_id` | Conflicts and evidence quality across specialist findings |
| Coordinator | `coordinator_agent_id` | One final grounded answer from the selected findings |

3. Paste the values in the deployment secret store, never into a tracked file.
   For Streamlit Community Cloud, use **App settings > Secrets**. For local
   development, use the ignored `.streamlit/secrets.toml` or ignored
   `config/app_settings.json`.

```toml
[claude]
enabled = true
api_key = "<anthropic-api-key>"
base_url = "https://api.anthropic.com"
model = "claude-haiku-4-5"
agent_runtime = "managed"

[claude.managed_agents]
enabled = true
environment_id = "env_<your-managed-environment-id>"
general_lineage_agent_id = "agent_<general-lineage-agent-id>"
powerbi_semantic_agent_id = "agent_<powerbi-semantic-agent-id>"
visual_evidence_agent_id = "agent_<visual-evidence-agent-id>"
snowflake_lineage_agent_id = "agent_<snowflake-lineage-agent-id>"
impact_analysis_agent_id = "agent_<impact-analysis-agent-id>"
evidence_reviewer_agent_id = "agent_<evidence-reviewer-agent-id>"
coordinator_agent_id = "agent_<coordinator-agent-id>"
session_title_prefix = "PBI Lineage Explorer"
session_timeout_seconds = 180
max_custom_tool_events = 20
```

4. In each tool-using Claude Console agent, add custom tools with these exact
   names and matching JSON schemas from the application's tool contract:
   `get_lineage_estate_overview`, `search_entire_lineage`, `search_reports`,
   `inspect_report_lineage`, `analyze_measure_impact`,
   `analyze_table_impact`, and `trace_snowflake_lineage`. The application
   receives those calls, enforces signed-in workspace scope, runs its existing
   read-only functions, and returns the result with `user.custom_tool_result`.
   The reviewer and coordinator can be text-only agents.
5. Restart or rerun the app after saving Secrets. The Claude page validates the
   required `env_...` and `agent_...` values before it starts a managed request.

Anthropic documents that a Managed Agent session needs both an agent ID and an
environment ID, and that custom tools require the application to return the
tool result. See [Agent setup](https://platform.claude.com/docs/en/managed-agents/agent-setup),
[sessions](https://platform.claude.com/docs/en/managed-agents/sessions), and
[custom tools](https://platform.claude.com/docs/en/managed-agents/tools).

## What Was Added

The application now uses a deterministic router with a single-agent default.
Simple lookups continue through one general Claude agent. Questions that clearly
span several domains can invoke only the needed specialists:

```text
User question
    |
    v
Deterministic router (no Claude token cost)
    | single                         | multi-domain
    v                                v
General lineage agent       Shared authorized estate index (once)
                                     |
                      selected specialists only
                 Power BI | Visual | Snowflake | Impact
                                     |
                     Evidence reviewer when useful
                                     |
                           Final coordinator
```

The worker agents run sequentially deliberately. They share the active
Streamlit session cache and the same permission-scoped executor. This avoids
unsafe concurrent access to session state and prevents each agent from building
the Power BI estate index independently.

## How To Use The Agents

1. Create or fund an Anthropic API account and generate an API key. A Claude
   web subscription is separate from API billing. The configured account must
   have available API credits.
2. Choose `agent_runtime = "direct"` to use application-defined agents, or
   follow **Configure Your Existing Claude Managed Agents** and set
   `agent_runtime = "managed"` to use your Console-created agents.
3. Store the key outside Git. For local deployment use ignored
   `config/app_settings.json`; for Streamlit deployment use the `[claude]`
   section in Streamlit Secrets. Use
   [.streamlit/secrets.toml.example](../.streamlit/secrets.toml.example) as the
   safe template.
3. Keep `orchestration_mode = "auto"` as the normal setting. It routes simple
   questions to the general agent and expands only complex cross-system
   questions.
4. Start the app, sign in to Power BI, open **Claude Agent**, select the
   authorized workspace scope, and leave **Agent strategy** at **Auto**.
5. Use **Single agent** for a direct lookup such as “List database tables used
   in Sales & Marketing.” Use **Multi-agent** only when you explicitly need a
   coordinated deep investigation.
6. Ask a cross-system question such as: “Trace NET_SALES from Snowflake through
   the semantic model to report visuals and downstream impact.” The router
   selects the relevant specialist agents, shows their activity, and returns one
   coordinator answer.
7. Review the displayed strategy, agent count, token use, estimated cost, tool
   activity, and any skipped work. A skipped specialist means a budget or
   availability guard acted; it is not silently ignored.

## Recommended Configuration

Add these values under `[claude]` in Streamlit Secrets, or as members of the
`claude` object in `config/app_settings.json`. Do not paste a real API key into
source-controlled files.

For a field-by-field explanation, runtime applicability, validated ranges, and
cost/quality profiles, see [Claude Configuration Reference](CLAUDE_CONFIGURATION_REFERENCE.md).

```toml
orchestration_mode = "auto"     # auto | single | multi
max_specialist_agents = 3       # Auto uses only the first relevant agents
max_agent_calls = 4             # Specialist/reviewer/coordinator runs combined
max_total_input_tokens = 30000
max_total_output_tokens = 3600
max_estimated_cost_usd = 0.08   # Planning guard, not an Anthropic invoice
specialist_max_tokens = 700
evidence_reviewer_max_tokens = 500
coordinator_max_tokens = 900
shared_evidence_max_chars = 6000
```

For Console-created agents, append the `[claude.managed_agents]` block from
**Configure Your Existing Claude Managed Agents** and change
`agent_runtime = "managed"`. Keep `agent_runtime = "direct"` when those IDs
are not configured. In Managed Agent mode, set `claude.model` to the same model
family selected for the Console agents so the application's planning estimate
uses the closest configured price assumption; Anthropic billing remains the
source of actual cost.

For a deeper, intentionally more expensive investigation, set
`max_specialist_agents = 4` and `max_agent_calls = 6`. This permits up to four
specialists, one evidence reviewer, and one coordinator. Do not make this the
default.

The cost estimate uses configurable token-price assumptions. The code selects
Haiku-like defaults for models whose name contains `haiku`, Sonnet-like defaults
for other models, and lets configuration override them. It is a request guard,
not a billing guarantee: Anthropic reports actual input usage only after a model
request completes. Power BI capacity and Snowflake warehouse consumption are
separate from the Claude estimate.

## Agent Responsibilities

| Agent | Why it exists | Allowed work | Output |
|---|---|---|---|
| Router | Avoids paying for unnecessary agents | Detects simple versus cross-domain questions with deterministic keywords | Mode, selected specialists, reason |
| General lineage agent | Cheapest default for normal questions | Existing complete read-only tool set | One grounded answer plus tool trace |
| Power BI semantic specialist | Keeps report/model reasoning focused | Reports, semantic objects, measure/source mappings | Findings and exact names/IDs |
| Visual evidence specialist | Prevents false visual-usage claims | Report pages, visual fields, visual metadata | Confirmed visual evidence |
| Snowflake lineage specialist | Keeps physical-source and transformation analysis focused | Fully qualified sources and Snowflake recursive lineage | Source path and transformations |
| Impact specialist | Separates downstream impact from source tracing | Measure/table impact and dependent reports/models | Counts, affected objects, visual-confirmation status |
| Evidence reviewer | Checks agreement before a broad conclusion | Bounded specialist evidence only; no tools | Confirmed evidence and conflicts |
| Coordinator | Produces one usable final response | Bounded specialist packets and reviewer output; no tools | Answer, Evidence, Impact |

## Claude Code Map

| File and line | Function or section | Why it is used | Called by | Output / effect |
|---|---|---|---|---|
| [pbi_modules/claude_agent.py:15](../pbi_modules/claude_agent.py:15) | `SPECIALIST_AGENT_PROFILES` | Defines the four real tool-limited specialists | `run_claude_orchestrated_agent` | Labels, instructions, and allowed tool names per specialist |
| [pbi_modules/claude_agent.py:74](../pbi_modules/claude_agent.py:74) | `MANAGED_AGENT_ROLE_CONFIG_KEYS` | Maps application roles to configuration fields | Managed configuration and runner | Required `agent_...` identifier key per role |
| [pbi_modules/claude_agent.py:179](../pbi_modules/claude_agent.py:179) | `resolve_claude_settings` | Validates credentials and normalizes direct/managed runtime, limits, and budgets | Page and all Claude runners | Safe resolved settings dictionary; raises configuration error when unavailable |
| [pbi_modules/claude_agent.py:388](../pbi_modules/claude_agent.py:388) | `generate_claude_response` | Makes one direct no-tool Claude call while preserving token usage | Direct-runtime reviewer and coordinator | `text`, `model`, input/output token counts |
| [pbi_modules/claude_agent.py:427](../pbi_modules/claude_agent.py:427) | `lineage_agent_tool_definitions` | Defines the read-only functions and schemas | Direct runner and Claude Console custom-tool setup | Tool schemas for estate, report, impact, and Snowflake lineage |
| [pbi_modules/claude_agent.py:661](../pbi_modules/claude_agent.py:661) | `managed_agent_configuration_status` | Validates Managed Agent environment and role IDs without exposing values | Claude page before a managed request | Readiness and missing-ID status |
| [pbi_modules/claude_agent.py:715](../pbi_modules/claude_agent.py:715) | `run_claude_managed_agent` | Starts a Managed Agent session and bridges custom-tool calls locally | Managed-runtime orchestrator | Grounded text, session ID, trace, evidence, and usage |
| [pbi_modules/claude_agent.py:872](../pbi_modules/claude_agent.py:872) | `run_claude_agent` | Runs the direct bounded tool loop for one general or specialist agent | Direct-runtime orchestrator | Grounded text, tool trace, bounded evidence packets, usage |
| [pbi_modules/claude_agent.py:1021](../pbi_modules/claude_agent.py:1021) | `lineage_specialist_profiles` | Exposes copies of specialist definitions for inspection/tests | Future integrations and tests | Profile dictionary without mutable shared tool sets |
| [pbi_modules/claude_agent.py:1053](../pbi_modules/claude_agent.py:1053) | `plan_claude_agent_route` | Routes without an additional Claude call | Claude Agent page and orchestrator | `single` or `multi`, selected specialists, reason |
| [pbi_modules/claude_agent.py:1269](../pbi_modules/claude_agent.py:1269) | `run_claude_orchestrated_agent` | Applies route, shared evidence, direct/managed runners, and budget guards | `render_claude_lineage_agent_page` | Final answer, merged trace, usage, estimated budget, skipped work |
| [streamlit_app.py:5523](../streamlit_app.py:5523) | `_get_claude_settings` | Reads only the Claude settings section | Claude page and measure-definition feature | Config copy with no UI exposure of the key |
| [streamlit_app.py:9531](../streamlit_app.py:9531) | `_build_claude_agent_estate_index` | Builds/caches the authorized report/model/source/measure/visual index once per scope | Estate search tools and multi-agent shared evidence | Session-cached lineage surfaces and coverage/errors |
| [streamlit_app.py:9873](../streamlit_app.py:9873) | `_search_claude_agent_estate` | Searches all indexed lineage surfaces together | `search_entire_lineage` tool | Bounded matches by report, semantic, source, measure, and visual categories |
| [streamlit_app.py:9913](../streamlit_app.py:9913) | `_build_claude_lineage_tool_executor` | Enforces selected workspace scope and calls deterministic existing lineage functions | All Claude tool requests | Read-only, permission-scoped tool results; rejects out-of-scope data |
| [streamlit_app.py:10177](../streamlit_app.py:10177) | `_prepare_claude_agent_shared_evidence` | Creates/reuses one estate index before multi-agent work | Claude Agent page for a multi route | Shared overview plus a visible cache-build trace row |
| [streamlit_app.py:10215](../streamlit_app.py:10215) | `_render_claude_agent_trace` | Makes agent activity and budget visible to a user/auditor | Claude Agent page | Strategy/cost caption and evidence activity table |
| [streamlit_app.py:10254](../streamlit_app.py:10254) | `render_claude_lineage_agent_page` | Provides workspace scope, managed-ID validation, strategy, index refresh, chat, and output rendering | Main Streamlit workflow router | Claude user interface and persisted chat state |
| [pbi_modules/app_shell.py:119](../pbi_modules/app_shell.py:119) | Claude sidebar button | Makes Claude the first app navigation destination | Authenticated sidebar renderer | Switches workflow to `claude_agent` |
| [utils.py:34](../utils.py:34) | `DEFAULT_APP_SETTINGS["claude"]` | Supplies safe local defaults, including Auto mode and the $0.08 planning guard | `Utils.load_app_settings` | Merged configuration when values are absent from secrets/local settings |
| [config/app_settings.template.json:9](../config/app_settings.template.json:9) | Claude template | Gives a safe JSON deployment example without a key | Local configuration setup | Copyable settings structure |
| [.streamlit/secrets.toml.example:25](../.streamlit/secrets.toml.example:25) | Claude Secrets template | Gives a Streamlit Cloud TOML deployment example without a key | Streamlit Secrets setup | Copyable secrets structure |
| [tls_trust.py:16](../tls_trust.py:16) | `configure_tls_trust` | Lets the Anthropic HTTP client trust approved corporate CA roots | Imported at Streamlit startup | HTTPS trust configuration; SSL verification remains enabled |
| [requirements.txt:4](../requirements.txt:4) | `anthropic` dependency | Provides the official direct Anthropic SDK | Python environment | Claude Messages API client |
| [Test/test_claude_agent.py:1](../Test/test_claude_agent.py:1) | Single-agent tests | Protects settings, grounding, tool and error behavior | Test runner | Pass/fail regression coverage |
| [Test/test_claude_agent_scope.py:1](../Test/test_claude_agent_scope.py:1) | Scope tests | Ensures agent requests cannot escape selected workspaces | Test runner | Permission-boundary coverage |
| [Test/test_claude_estate_search.py:1](../Test/test_claude_estate_search.py:1) | Estate-search tests | Ensures a term can return all lineage categories | Test runner | Comprehensive search coverage |
| [Test/test_claude_multi_agent.py:1](../Test/test_claude_multi_agent.py:1) | Multi-agent tests | Protects routing, cost-profile selection, shared evidence, reviewer, coordinator, and agent-run cap | Test runner | Multi-agent regression coverage |
| [Test/test_claude_managed_agents.py:1](../Test/test_claude_managed_agents.py:1) | Managed-agent tests | Protects ID validation and the local custom-tool bridge | Test runner | Managed session and authorization-boundary regression coverage |

## Expected Outputs

Every request returns an answer plus an evidence activity table. Multi-agent
requests additionally display:

- `Strategy`: Single or Multi.
- `Agents`: selected roles, not every defined role.
- `Budget`: agent runs and estimated Claude model cost against the configured
  guard.
- `Evidence activity`: shared-cache creation, each specialist's requested tool,
  reviewer/coordinator activity, timings, and errors.
- `Download analysis (.md)`: locally prepared Markdown containing only the
  completed PowerAI answer. Preparing it makes no additional Claude request.

## Security And Guardrails

- All tools remain read-only.
- The page-level workspace scope is server-enforced for every tool call.
- Report IDs outside the authorized inventory are rejected.
- Each specialist gets only an allowlisted subset of tools.
- The shared estate cache stays in the active Streamlit session.
- DAX, SQL, comments, and metadata are treated as data, not instructions.
- Tool-result characters, output tokens, agent runs, aggregate tokens, and
  estimated model spend are bounded.
- The dollar figure is an estimate, not a provider invoice or an absolute cap;
  returned usage arrives after each API call. The run guard prevents starting
  additional agents once a limit is reached.
- API keys and authorization headers are never rendered in the UI or passed to
  Claude.
- Managed Agent IDs and environment IDs are kept in the secret store to avoid
  exposing deployment topology in source control.
- A Managed Agent receives only the user question and bounded custom-tool
  responses. Its configuration must never contain raw Power BI, Fabric,
  Snowflake, Entra, or browser credentials.

## Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| `credit balance is too low` | API key is valid but Anthropic API billing has no usable credits | Add API credits, then retry the request |
| `Enable the claude section` | Claude is disabled in merged settings | Set `enabled = true` in the applicable local config or Secrets section |
| `Missing Claude API key` | No key resolved from secrets/environment/local ignored config | Add the key to an approved secret store only |
| `Missing claude.managed_agents...` | The selected Managed Agent role or shared environment is not configured | Paste the relevant `env_...` or `agent_...` ID in the `[claude.managed_agents]` Secrets block |
| Managed agent returns no answer | The Console agent ended without an `agent.message`, often because its required custom tool is absent | Add the exact custom tool in Claude Console, or inspect the Managed Agent session trace |
| Specialist skipped by budget guard | Requested route exceeded configured run/token/cost limits | Use Single agent, reduce the question scope, or deliberately raise the limits |
| Visual evidence unavailable | Fabric report definition was not cached or retrievable | Treat report impact as model-level; do not claim visual confirmation |
| XMLA or `pywin32` error | Semantic-model operations require Windows/MSOLAP execution | Run on Windows or route XMLA operations through a Windows backend |
