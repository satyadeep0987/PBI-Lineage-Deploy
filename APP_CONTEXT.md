# PBI Lineage Explorer App Context

## Purpose

PBI Lineage Explorer is a Streamlit application for exploring Power BI estate
metadata and lineage. It signs a user into Microsoft Entra ID, inventories
authorized Power BI workspaces, reports, dashboards, apps, datasets, semantic
model objects, visual layout metadata, source database references, DAX measure
dependencies, and optional Snowflake lineage. It also includes a PowerAI
assistant surface that explains and searches metadata through bounded, read-only
tools. PowerAI is the in-app brand; the current provider implementation is still
configured through the existing Claude/Anthropic settings for compatibility.

The current lineage context separates the workspace that owns a report from the
workspace that owns each semantic model. When authorized metadata is available,
the Admin Workspace Scanner expands composite/multi-semantic-model reports into
primary and upstream model contexts before source, measure, table-impact, and
visual lineage are joined.

The production source repository uses `streamlit_app.py` as the root entrypoint.
The full-feature production target is a private Windows host because XMLA
depends on COM, `pywin32`, and the Microsoft Analysis Services OLE DB Provider.
A Linux or Streamlit Community Cloud deployment is suitable only for the
cloud-safe REST, Fabric, visual, Snowflake, and PowerAI paths unless XMLA is
provided by a separate private Windows worker.

## Main Runtime

- Entrypoint: `streamlit_app.py`
- UI framework: Streamlit
- Primary data stack: Power BI REST API, Fabric report-definition API, XMLA over
  MSOLAP/ADO COM, pandas
- Main-app authentication: delegated Master User MSAL local interactive browser
  flow with PKCE. The connection sidebar assigns that delegated token
  to the legacy `mu`, `sp`, and `spa` variables. Standalone diagnostic scripts
  remain separate from the application runtime.
- Token audiences: Power BI REST/XMLA and Fabric report-definition calls require
  separate audience-specific access tokens; one token must not be assumed valid
  for both resources.
- Optional assistant provider: Anthropic Claude direct API or Claude Managed
  Agents, surfaced in the UI as PowerAI
- Optional source lineage: Snowflake connector and configured lineage stored
  procedure

## High-Level Flow

1. App startup configures TLS trust and imports Streamlit, MSAL, requests,
   pandas, XMLA, and local modules.
2. Streamlit table rendering is patched so displayed dataframe column names use
   normalized no-space headers.
3. The normal Home page renders immediately. Independent Power BI, optional
   Power BI scope, Snowflake database, and optional PowerAI controls live in the
   sidebar and never read local runtime configuration files or Streamlit Secrets.
4. Power BI requires only Tenant ID and Client ID; workspace/report limits are
   separate and optional. Power BI and Snowflake open their own browser SSO
   flows and can be disconnected independently. Tokens, the live Snowflake
   connection, and the optional Claude key remain in session memory.
5. Authenticated navigation is rendered as Streamlit buttons so a navigation
   action preserves the WebSocket-owned runtime session.
6. The user can choose home, guided exploration, report lineage, table impact,
   or measure impact from the left sidebar. Query parameters restore the target
   workflow; report-lineage URLs carry only navigation IDs and resolve all
   semantic-model context from the session's validated report record,
   and the browser tab title is updated to match the active navigation page
   while Home remains titled `PBI Lineage Explorer`.
7. Lineage views call Power BI/Fabric/XMLA/Snowflake helpers, cache expensive
   results in `st.session_state`, render dataframes, and expose CSV downloads.
   Cross-WebSocket/browser registries are disabled under strict session
   isolation; a new tab must complete its own setup.
8. PowerAI opens from a bold floating button at the lower-right of authenticated
   navigation views as a large Streamlit modal dialog. The main page always
   renders as one full-width canvas, and the assistant floats above it only
   while the dialog is open.
9. The PowerAI modal omits the Conversation selector and recent-chat controls.
   Suggested questions sit above the chat stream, older turns collapse into an
   "Earlier conversation" expander, the prompt box stays near the bottom, and
   PowerAI settings sit directly below the prompt box.
10. The assistant provider receives only bounded metadata returned by app-owned
    read-only tools; credentials and tokens stay in the application.

## Key Files

- `streamlit_app.py`: Main app. Contains authentication, Power BI/Fabric API
  calls, app/workspace/report inventory, visual layout parsing, XMLA semantic
  object and measure lineage extraction, Snowflake lineage handoff and graphing,
  impact analysis, PowerAI dialog wiring, and page rendering.
- `utils.py`: Retains built-in non-secret behavior defaults and legacy
  diagnostic compatibility. The application runtime does not call its
  file-backed configuration loaders.
- `xmla_ado_com.py`: Windows-only XMLA helper using ADODB COM through `pywin32`.
  Raises clear runtime errors on non-Windows/cloud hosts.
- `tls_trust.py`: TLS trust setup for OS certificates and optional custom CA
  bundles used by requests/HTTPX-based clients.
- `pbi_modules/setup_controller.py`: Pure validation, provider-specific state,
  Power BI/Fabric access checks, Snowflake SSO validation, redaction, and
  session-lifecycle helpers.
- `pbi_modules/connection_sidebar.py`: Independent Power BI, optional scope,
  Snowflake database, and PowerAI connection controls.
- `pbi_modules/setup_page.py`: Small compatibility wrapper for older callers.
- `pbi_modules/app_shell.py`: Sidebar, workflow selection, direct
  report lookup, and impact-page shell components.
- `pbi_modules/claude_agent.py`: Anthropic client, tool schemas, direct tool
  loop, Managed Agent session bridge, deterministic routing, specialist
  profiles, budget guards, and error redaction used behind the PowerAI surface.
- `pbi_modules/analysis_document.py`: Builds downloadable Markdown from only
  the completed PowerAI answer without making another model call.
- `pbi_modules/controls.py`: Shared searchable selects, checkbox selectors, and
  CSV downloads.
- `pbi_modules/table_impact.py`: Table/object matching helpers used by impact
  analysis.
- `.streamlit/config.toml`: Streamlit server/theme defaults.
- `requirements.txt`: Tested direct dependency versions. `pywin32` remains
  guarded by a Windows platform marker.
- `docs/`: Deployment, architecture, functional-coverage, PowerAI
  implementation, multi-agent, and configuration references.
- `docs/PBI_LINEAGE_APPLICATION_ARCHITECTURE_AND_FLOW.drawio`: Editable,
  four-page diagrams.net model of the deployed architecture, authentication and
  navigation flow, tab outputs, multi-semantic lineage processing, fallbacks,
  and PowerAI read-only orchestration. It reflects the current floating PowerAI
  modal and marks the source-retained App view as not currently routed.
- `local_test_tools/powerbi_raw_api_probe.py`: Local-only raw Power BI/Fabric/XMLA
  diagnostic helper. It can acquire tokens for configured user/service-principal
  modes, wrap one-time bearer tokens, run the Admin Workspace Scanner, and call
  Power BI/Fabric REST and XMLA DMV paths used by the app. The file is kept as
  direct endpoint functions with an API comment immediately above each function,
  and returns raw service response envelopes.
- `local_test_tools/run_powerbi_raw_api_probe_all.py`: Local-only runner that
  calls the direct raw probe functions, discovers workspace/report/dataset/app
  IDs from earlier responses where possible, and writes one JSON output file per
  function plus an `index.json` summary under an ignored, ephemeral
  `local_test_tools/raw_api_outputs/` directory.
- `Test/`: `unittest`-compatible offline regression coverage for PowerAI agent
  behavior and scoping, estate search, managed agents, Snowflake column lineage,
  source enrichment, multi-semantic context expansion, impact suggestions,
  visual-name resolution, workspace filtering, and analysis documents.

## Major Capabilities

- Power BI workspace inventory, with `Admin Monitoring` excluded by app policy.
- Power BI app, report, dashboard, artifact, user, and access inventory.
- Audience/app metadata flattening from API responses or uploaded mapping files.
- REST/Fabric report-definition retrieval and PBIR/report layout parsing.
- Visual usage extraction by page, visual, field, role, source table, and source
  field where available.
- Visual Details resolves PBIR names from `visualContainerObjects.title` and
  derives a stable field-based name when Power BI relies on an automatic title;
  the layout cache is versioned so older GUID-based display names are refreshed.
- Manual report layout upload for cases where automatic retrieval is blocked.
- XMLA semantic model object extraction, including tables, columns, measures,
  expressions, dependencies, and source query/source object details.
- XMLA semantic-model relationship extraction.
- Source database lineage extraction from Power Query/M and native SQL metadata.
- Measure source lineage, measure detail explanation, and provider selection
  between PowerAI and Snowflake Cortex.
- Table impact and measure impact analysis across reports, models, measures,
  sources, and optional visual evidence.
- Searchable Table Impact and Measure Impact dropdowns from cached, authorized
  semantic metadata, including scanner-discovered upstream models; both controls
  still allow custom manual names for exact or partial searches.
- New-tab-safe page navigation for the main authenticated workflows, backed by
  query-parameter routing and browser-scoped server cache hydration.
- Composite-model context expansion through the Admin Workspace Scanner when
  the signed-in identity has the required tenant metadata access.
- Downloadable metadata-only report-detail packages containing inventory,
  semantic objects, relationships, source lineage, measure lineage, and visual
  metadata when each evidence source is available.
- Snowflake object/column lineage tracing, recursive lineage rendering, and
  graph output from configured Snowflake metadata/procedures.
- PowerAI modal assistant with single-program or multi-program routing,
  read-only tool allowlists, permission-scoped execution, visual-evidence
  gating, token/cost guardrails, trace display, session-local conversation
  history, and answer-only Markdown downloads.

## Current Lineage Data Contract

The implemented lineage path now separates report ownership from semantic
model ownership and can expand one report into multiple model contexts:

1. Report inventory creates a primary context containing `Report Workspace ID`,
   `Semantic Model Workspace ID`, `Primary Dataset ID`, and `Dataset ID`.
   `Target Workspace ID` remains only as a backward-compatible semantic-model
   workspace alias.
2. Report-definition retrieval always uses `Report Workspace ID`. Fabric
   `getDefinition`, Power BI PBIX export, or manual upload extracts page,
   visual, table, field, role, aggregation, and query-reference metadata.
3. Semantic/XMLA retrieval always uses `Semantic Model Workspace ID`.
   Same-workspace ownership is validated from the workspace dataset inventory;
   cross-workspace ownership can be resolved from report metadata, an admin
   dataset lookup, or the Admin Workspace Scanner.
4. When Workspace Scanner lineage metadata is authorized, report
   `datasetWorkspaceId` and dataset `upstreamDatasets` expand the report context
   into primary and upstream models. Each context carries `Model Role`,
   `Lineage Depth`, `Parent Dataset ID`, owner workspace, and resolution
   provenance.
5. XMLA reads tables, columns, measures, relationships, partitions,
   expressions, `LineageTag`, `SourceLineageTag`, and calculation dependencies.
   Partition `ExpressionSourceID` is joined to `TMSCHEMA_EXPRESSIONS`, allowing
   `AnalysisServices.Database` sources to be labeled as upstream semantic
   models instead of unknown physical sources.
6. Visual rows retain the report's `Primary Dataset ID`. Semantic object joins
   prefer that model. If a composite object exposes `SourceLineageTag`, visual
   source lookup can follow it to the matching upstream object's `LineageTag`
   and then use that upstream model's source partition.
7. A resolved physical source fully qualified name can be handed to optional
   Snowflake object/column lineage.

Report-definition retrieval and semantic-model retrieval therefore use
different explicit identities. They may match for a same-workspace report but
are never treated as interchangeable.

## Remaining Multi-Semantic-Model And Cross-Workspace Limitations

- Admin Workspace Scanner expansion is opportunistic. It requires a Fabric
  administrator or an eligible service principal and the applicable tenant
  read scope/settings. If it is not authorized, the app keeps the primary-model
  path and uses safe owner-resolution fallbacks without claiming upstream
  closure.
- Report-definition parsing does not preserve `definition.pbir`
  `datasetReference` bindings. Manual PBIX parsing reads legacy
  `Report/Layout` or PBIR page/visual JSON but does not inspect `Connections` or
  the embedded `DataModel`.
- Visual rows retain the report's primary model binding. Per-visual dataset
  bindings are not invented; upstream object resolution is added only when
  scanner provenance or `SourceLineageTag`/`LineageTag` evidence exists.
- Upstream recursion is limited to datasets returned in the authorized
  Workspace Scanner result. A referenced workspace omitted from that scan can
  still be identified by `groupId`/`targetDatasetId`, but deeper closure
  requires that workspace's metadata to be scanned.
- Semantic object queries exclude hidden objects, even though a hidden object
  can be an essential dependency intermediate.
- Source lookups keep the first normalized match. Multiple partitions, multiple
  physical sources, or duplicate semantic names can be collapsed or joined
  ambiguously.
- Power Query/native SQL extraction is heuristic. Complex SQL, unsupported
  connectors, and inferred visual-name fallbacks must not be labeled as verified
  physical lineage.

The retained findings from the July 2026 PBIX case study are summarized in this
section. The raw assessment and tenant-specific identifiers are intentionally
excluded from the production repository.

## Manual PBIX Upload Boundary

Manual PBIX/PBIR upload is a visual-metadata fallback. It can provide pages,
visuals, field wells, query references, and visual field names. In the current
application it does not extract the embedded model, Power Query/M, native SQL,
relationships, source model ownership, or upstream semantic-model closure.
Those semantic details still depend on a correctly resolved XMLA context.

## Runtime Configuration

The production application accepts connection parameters only through the
connection sidebar. Built-in non-secret behavior defaults remain in source, while
tenant IDs, client IDs, workspace/report IDs, Snowflake connection identifiers,
OAuth material, bearer tokens, and optional Claude configuration remain in the
current Streamlit session. Runtime JSON/YAML/TOML/`.env` credential files and
Streamlit Secrets are not part of this path.

### Local API diagnostics

`local_test_tools/powerbi_raw_api_probe.py` and
`local_test_tools/run_powerbi_raw_api_probe_all.py` are retained as operator
diagnostics but are not part of the runtime deployment. The probe exposes direct
Power BI, Fabric, Scanner, and XMLA calls and returns raw response envelopes for
lineage troubleshooting.

Diagnostic output is sensitive even when token fields are redacted because it
can contain tenant, workspace, report, user, semantic-model, source, and query
metadata. Output under `local_test_tools/raw_api_outputs/` is therefore ignored,
ephemeral, excluded from deployment packages, and removed after each approved
diagnostic session. Device-login transcripts are handled the same way.

Validate the token audience before testing: Power BI REST/XMLA and Fabric
`getDefinition` require different audience-specific tokens. Request only the
read permissions needed for the diagnostic and never place a token in source,
logs, screenshots, output archives, or support tickets.

Do not create or use filled credential/configuration files for the application
runtime. Existing legacy local files should be securely removed and any
credentials they contained rotated before production use.

## Deployment Notes

- Use Python 3.11 and the tested direct dependency versions in
  `requirements.txt`.
- The full-feature first production target is a private Windows host running
  Streamlit behind the approved reverse proxy. XMLA requires Windows,
  `pywin32`, MSOLAP, and enabled Power BI XMLA endpoints.
- A later scaled design can run the web/API tier on Linux while delegating
  allow-listed XMLA reads to a private Windows worker.
- Streamlit Community Cloud is suitable for demonstrations of cloud-safe REST,
  Fabric, visual, Snowflake, and PowerAI capabilities, not the complete
  XMLA-backed product.
- Runtime credentials exist only in the active session and must not be copied
  into a release artifact, log, cache, browser URL, or cookie.
- Corporate TLS inspection must use OS trust or an approved CA bundle through
  `PBI_CA_BUNDLE` or `REQUESTS_CA_BUNDLE`; never disable certificate
  verification.

## Local Development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Before running locally, register the Entra loopback redirect URI, then enter
connection identifiers in the sidebar and complete browser SSO.

## Testing And Validation

The repository does not declare `pytest` in `requirements.txt`. The current
test modules are runnable with the standard-library `unittest` runner. The
verified full offline check is:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Test -p "test_*.py"
```

The 2026-08-13 sidebar-connection baseline completed all 85 tests across 20
`test_*.py` modules successfully. Coverage includes cross-workspace context
separation, scanner-driven upstream expansion, impact type-ahead ranking, PBIR
aggregation enum `0` (`Sum`), PBIR visual-name resolution, PowerAI scope and
orchestration, source enrichment, Snowflake procedure handling, and document
output.

Live Power BI, Fabric, XMLA, Snowflake, or Anthropic checks remain operator
diagnostics and must not be treated as offline unit tests.

## Repository Hygiene

The production source repository retains runtime source, built-in non-secret
behavior defaults, Markdown documentation, the editable diagrams.net
architecture, offline regression tests, and reusable raw API probe scripts.

The following are excluded from source-control and deployment packages:

- virtual environments, Python bytecode, tool caches, logs, temporary files,
  downloads, exports, and notebooks;
- filled JSON/TOML credentials and local secret files;
- raw API responses and device-login transcripts;
- generated PNG, PowerPoint, Word, presentation, and demo-package collateral;
- tenant-specific analysis files and identifiers.

Build releases from tracked files with an explicit allowlist or `git archive`.
Never create a release by zipping a developer working directory. `APP_CONTEXT.md`
and every offline regression test are intentionally trackable; broad ignore
patterns such as `test*` must not be reintroduced.

## Important Guardrails

- Treat Power BI, Fabric, Entra, Anthropic, and Snowflake credentials as secrets.
- Keep PowerAI tools read-only and scoped to the signed-in user's authorized
  workspace inventory.
- Do not claim visual-level impact unless report-definition or uploaded layout
  evidence confirms it.
- Do not claim upstream semantic-model or physical-source lineage without model
  ownership, upstream model IDs, and provenance for the matching object.
- Keep report workspace identity separate from semantic-model workspace
  identity in diagnostics and exported evidence.
- Label physical-source values as verified, inferred, ambiguous, unsupported,
  or permission-blocked instead of presenting all name matches as exact.
- Do not send credentials, access tokens, raw business records, arbitrary SQL,
  or administrative operations to the assistant provider.
- Preserve cloud compatibility by keeping Windows-only XMLA dependencies behind
  platform guards.
- Respect the app's caching behavior in `st.session_state`; expensive metadata
  scans are intentionally reused across views and agent requests.
