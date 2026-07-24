# Claude Agentic AI Implementation

## Purpose

This document records the changes required to make PBI Lineage Explorer a
Claude agentic application. It supports either direct Anthropic tool calling or
Anthropic Managed Agents created in Claude Console. It also records the
replacement of the former OpenAI measure-definition provider.

The implementation keeps the existing Power BI, Fabric, XMLA, report-definition,
and Snowflake lineage functions as the deterministic source of truth. Claude
selects read-only tools and explains their results; Claude does not calculate or
invent lineage.

## Target Architecture

```text
Authenticated Streamlit user
          |
          v
Claude Lineage Agent page
          |
          v
Deterministic router (single agent by default)
          |
          +---- General lineage agent
          |
          +---- Selected specialists -----> Evidence reviewer -----> Coordinator
          |      Power BI | Visual | Snowflake | Impact
          v
Bounded read-only tool executor -----> Direct Messages API or Managed Agent session
          |
          +---- Power BI report inventory
          +---- XMLA semantic metadata and dependencies
          +---- Fabric/Power BI report-definition visual metadata
          +---- Table impact analysis
          +---- Measure impact analysis
          +---- Snowflake object and column lineage
```

Tool execution remains inside this application. Power BI and Snowflake
credentials are never sent to Claude.

## Existing Application Changes

| File | Change |
|---|---|
| `pbi_modules/claude_agent.py` | Direct Anthropic client and Managed Agent session bridge, deterministic router, tool-limited specialists, evidence reviewer/coordinator, budget guard, token usage, result limits, and error redaction |
| `pbi_modules/app_shell.py` | Adds `Claude Agent` as the first sidebar destination |
| `streamlit_app.py` | Adds the agent page, cached estate-wide shared evidence, permission-scoped tool executor, strategy control, routing, and Claude measure definitions |
| `utils.py` | Replaces OpenAI defaults with the shared `claude` configuration |
| `tls_trust.py` | Applies an approved custom CA bundle to the HTTPX-based Anthropic SDK |
| `config/app_settings.template.json` | Documents safe direct and Managed Agent settings with no populated secret |
| `.streamlit/secrets.toml.example` | Documents direct and Managed Agent Streamlit Secrets configuration |
| `config/app_settings.json` | Local ignored configuration is migrated without preserving the OpenAI key |
| `requirements.txt` | Adds the official `anthropic` Python SDK |
| `README.md` | Replaces OpenAI setup and cloud capability text with Claude |
| `Test/test_claude_agent.py` | Tests configuration, text generation, tool execution, scope limits, and tool-round limits |
| `Test/test_claude_multi_agent.py` | Tests single-agent routing, specialist selection, shared evidence, reviewer/coordinator execution, and request-run caps |
| `docs/CLAUDE_MULTI_AGENT_GUIDE.md` | Detailed setup, code map with function references, budget behavior, outputs, and troubleshooting |

## Claude Configuration

The application uses the direct Anthropic endpoint rather than Microsoft
Foundry:

```text
https://api.anthropic.com
```

Recommended Streamlit Secrets:

```toml
[claude]
enabled = true
api_key = "<anthropic-api-key>"
base_url = "https://api.anthropic.com"
model = "claude-sonnet-4-6"
timeout_seconds = 90
max_tokens = 1800
measure_definition_max_tokens = 900
temperature = 0
max_tool_rounds = 6
max_tool_result_chars = 30000
history_messages = 12
orchestration_mode = "auto"
max_specialist_agents = 3
max_agent_calls = 4
max_total_input_tokens = 30000
max_total_output_tokens = 3600
max_estimated_cost_usd = 0.08
specialist_max_tokens = 700
evidence_reviewer_max_tokens = 500
coordinator_max_tokens = 900
shared_evidence_max_chars = 6000
```

### Claude Managed Agents

To use agents already created in Claude Console, retain the same `api_key` and
add the following to the same secret store. `environment_id` is the shared
`env_...` ID; every `agent_...` value is the ID copied from the corresponding
Claude Console agent. Do not add Power BI, Snowflake, or Entra credentials to
the Claude agent configuration.

```toml
[claude]
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
```

For tool-using managed agents, define the seven existing read-only custom tools
in Claude Console. The app executes them locally with the signed-in user's
workspace scope and responds with `user.custom_tool_result`. The complete
mapping, setup instructions, and tool names are in
[Claude Multi-Agent Guide](CLAUDE_MULTI_AGENT_GUIDE.md#configure-your-existing-claude-managed-agents).
Set `claude.model` to the model family configured in Claude Console so the
application's request-cost estimate is meaningful; provider billing and actual
usage come from the Managed Agent session.

Supported environment variables:

```text
ANTHROPIC_API_KEY
CLAUDE_ENABLED
CLAUDE_MODEL
CLAUDE_BASE_URL
```

The API key must be stored only in Streamlit Secrets, an environment variable,
Snowflake Secrets, or another approved secret manager. Do not commit it to Git.

## Read-Only Agent Tools

| Tool | Purpose | Authorization boundary |
|---|---|---|
| `get_lineage_estate_overview` | Count authorized workspaces, reports, models, semantic objects, source rows, measure dependencies, and visual coverage | Signed-in inventory and selected workspace scope |
| `search_entire_lineage` | Search reports, semantic objects, physical sources, measure lineage, and available visual fields in one call | Signed-in inventory and selected workspace scope |
| `search_reports` | Resolve report, workspace, dataset, and report IDs | Signed-in inventory and selected workspace scope |
| `inspect_report_lineage` | Retrieve semantic objects, source lineage, measure dependencies, or cached visual usage | Report must exist in authorized inventory |
| `analyze_measure_impact` | Trace one measure to reports, models, sources, and visual evidence | Selected workspace scope |
| `analyze_table_impact` | Trace one table/view to measures, reports, models, and visual evidence | Selected workspace scope |
| `trace_snowflake_lineage` | Trace Snowflake object or column lineage | Existing Snowflake connection and configured depth |

There is no arbitrary SQL, shell, file-write, refresh, export, deletion, or
workspace administration tool.

## Request Lifecycle

1. The signed-in user opens the first sidebar destination, selects the
   workspace scope. All authorized workspaces are selected by default.
2. The user chooses Auto, Single agent, or Multi-agent strategy and asks a
   lineage question. Auto remains the recommended setting.
3. The deterministic router selects the general agent for simple or
   single-domain work. It selects only the relevant specialists for a clearly
   cross-system question; the router itself has no Claude token cost.
4. For a multi-agent route, the application builds or reuses the authorized
   estate index once and shares bounded evidence with the selected specialists.
5. Claude receives the question, tool descriptions, and authorized workspace
   names. It does not receive Power BI or Snowflake credentials.
   In Managed Agent mode, Claude receives the same information through a
   session attached to the configured `agent_...` and `env_...` resources.
6. Each tool-using agent must request at least one read-only tool on its first model turn.
   Broad questions use the estate overview or comprehensive search tool.
7. The comprehensive search tool builds or reuses a model-aware index. Each
   semantic model is scanned once, connected reports are retained, and available
   Fabric report-definition metadata adds visual evidence.
8. The server validates the tool name, argument types, names, depth, report ID,
   and workspace authorization.
9. Existing application functions retrieve deterministic lineage evidence.
10. Results are limited in row count and character count before returning to
   Claude.
11. When two or more specialists complete, an evidence reviewer identifies
    conflicts and gaps before a coordinator produces one answer.
12. Claude returns an answer that distinguishes model impact, visual
   confirmation, and missing evidence.
13. The UI displays the answer, strategy, selected agents, budget estimate,
    tool activity, execution duration, and token usage.

The estate index is cached in the active Streamlit session. `Refresh index`
clears only Claude's index and rebuilds it on the next estate-wide request.

## Measure Definition Migration

The `OpenAI LLM` option is removed from Measure Detail Definition. Available
options are now:

```text
Auto (enabled provider order)
Claude
Snowflake Cortex
```

The default provider order is:

```json
["claude", "snowflake_cortex"]
```

Claude receives only the bounded measure metadata payload already assembled by
the application: measure name, DAX, semantic dependencies, source object names,
source columns, and bounded source queries.

## Security Controls

- Every Power BI tool is constrained to the signed-in user's inventory.
- The page-level workspace selection further restricts the tool scope.
- The first Claude model turn is forced to choose a read-only evidence tool.
- Report IDs outside that scope are rejected server-side.
- DAX, SQL, comments, and metadata are treated as untrusted data.
- Tool names are allowlisted and inputs use JSON schemas.
- Tool rounds, token output, lineage depth, row counts, and result characters
  are bounded.
- API keys and authorization headers are redacted from displayed errors.
- Managed Agent IDs are read from the secret store and validated before a
  session starts. Custom tool calls always execute inside this app; raw Power
  BI, Fabric, Snowflake, and Entra credentials are not sent to Claude.
- Visual usage is described as confirmed only when report-definition evidence is
  available.
- Conversation state is held in Streamlit session state and cleared by the user
  or application session lifecycle.
- The initial release is read-only. Future write actions require a separate
  approval workflow.

## Hosting Requirements

### Windows or standalone Streamlit

Allow outbound HTTPS on port 443 to `api.anthropic.com`. Store the API key in
environment variables or `.streamlit/secrets.toml`.

### Streamlit Community Cloud

Add the `[claude]` section in App Settings > Secrets. XMLA-backed tools still
require a separate Windows backend because Streamlit Community Cloud is Linux.

### Streamlit in Snowflake

Create an External Access Integration and network rule for
`api.anthropic.com:443`, then store the Anthropic key as a Snowflake secret.
XMLA-backed tools still require the Windows execution path.

## Validation Checklist

- `anthropic` installs successfully from `requirements.txt`.
- The Claude API smoke test returns a text response.
- `Claude Agent` is the first sidebar navigation item.
- A simple lookup routes to one general agent.
- A cross-system question routes only to the selected specialists.
- The configured agent-run budget reserves a final coordinator.
- An unauthorized workspace request is rejected.
- A single estate search returns matches across every available lineage surface.
- Refreshing the Claude index does not clear authentication or credentials.
- Report search returns only reports visible to the signed-in user.
- Measure and table impact tools return deterministic evidence.
- Visual usage is not claimed when report-definition metadata is absent.
- Snowflake depth cannot exceed the configured maximum.
- Measure Detail Definition lists Claude and no OpenAI option.
- No OpenAI setting, endpoint, model, or API key remains in tracked files.
- No Anthropic key is committed.
- Existing tests and the new Claude tests pass.

For the complete implementation map, including each file/function, caller, and
returned output, read [Claude Multi-Agent Guide](CLAUDE_MULTI_AGENT_GUIDE.md).

## Known Limitation

Claude changes the analysis and explanation experience, not the underlying XMLA
runtime requirement. Semantic-model tools that depend on `pywin32` and MSOLAP
must run on Windows or through a Windows backend API.

## Official References

- Anthropic API quickstart:
  <https://platform.claude.com/docs/en/get-started>
- Anthropic tool use:
  <https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works>
- Anthropic authentication:
  <https://platform.claude.com/docs/en/manage-claude/authentication>
- Snowflake Streamlit external access:
  <https://docs.snowflake.com/en/developer-guide/streamlit/features/external-access>
