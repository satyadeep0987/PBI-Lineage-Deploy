# PBI Lineage Explorer

PBI Lineage Explorer is a read-only Streamlit application for inspecting Power
BI reports, semantic models, visual usage, source objects, DAX dependencies,
table impact, measure impact, optional Snowflake lineage, and evidence-backed
PowerAI answers.

The application keeps report-workspace identity separate from semantic-model
workspace identity. When authorized, the Power BI Admin Workspace Scanner
expands composite reports through their primary and upstream semantic models.

## Production Deployment

The full application requires a private Windows host because XMLA metadata is
read through ADODB COM, `pywin32`, and the Microsoft Analysis Services OLE DB
Provider (MSOLAP). A Linux deployment can run REST, Fabric definition, visual,
Snowflake, and PowerAI features, but it cannot provide the complete XMLA-backed
semantic lineage path without a separate Windows XMLA worker.

See [Deployment Plan](docs/DEPLOYMENT_PLAN.md) for the staged production model.

## Repository Layout

```text
.
|-- streamlit_app.py
|-- utils.py
|-- tls_trust.py
|-- xmla_ado_com.py
|-- pbi_modules/
|   |-- setup_controller.py
|   |-- connection_sidebar.py
|   `-- setup_page.py
|-- .streamlit/
|   `-- config.toml
|-- local_test_tools/
|   |-- powerbi_raw_api_probe.py
|   `-- run_powerbi_raw_api_probe_all.py
|-- Test/
|-- docs/
|-- APP_CONTEXT.md
|-- requirements.txt
`-- README.md
```

`local_test_tools` contains reusable operator diagnostics only. Generated probe
outputs are ignored and must never be included in a deployment package.

## Requirements

- Python 3.11
- Power BI account with the required delegated permissions
- Entra application registration configured for delegated sign-in
- Windows, `pywin32`, and MSOLAP for XMLA-backed features
- Snowflake account with a dedicated read-only SSO role
- Optional Anthropic configuration for PowerAI

Install the tested dependency versions:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Runtime Setup And Authentication

The application does not load runtime connection settings or credentials from
JSON, YAML, `.env`, Streamlit Secrets, or other local configuration files. The
normal Home page is shown immediately, with independent connection panels in
the sidebar:

- **Power BI login** requires only Microsoft Entra Tenant ID and Client ID.
  Workspace ID and Report IDs are a separate optional scope control; leaving
  them blank dynamically discovers accessible workspace reports for the signed-in user.
- **Snowflake database** separately collects account identifier, database,
  warehouse, dedicated read-only role, and the non-secret SSO login name.
- **PowerAI** remains an optional independent panel. Managed mode collects the
  environment and all role-specific agent IDs plus one masked Claude API key.

Each successful provider panel locks its connection fields and shows a separate
Disconnect action. Power BI enables inventory and navigation; Snowflake enables
database lineage features without becoming a prerequisite for the Home page.

Power BI uses MSAL's local interactive browser flow with PKCE. The current
no-client-secret implementation is a local/co-located public-client flow: add a
loopback redirect URI for the Entra app's **Mobile and desktop applications**
platform, enable public-client authorization as required by your tenant, and
grant the delegated read scopes used by the application. A remote HTTPS
Streamlit deployment needs a tenant-approved web or browser-side SPA identity
architecture. Optional Fabric report-definition access uses a separate same-user
audience token and degrades gracefully when it is unavailable.

Snowflake uses `authenticator='externalbrowser'`; no password parameter is
accepted. The connector session is validated with `CURRENT_*` context functions
and retained only for the Streamlit session. External-browser SSO opens on the
machine running Streamlit, so a remote/headless deployment must use a
tenant-approved Snowflake OAuth authorization-code callback instead.

Tokens, the Snowflake connection, and the Claude key are held only in
`st.session_state`. The active browser flows do not use an application query
callback or a cross-session redirect bridge. Secrets are excluded from query
parameters, cookies, shared data caches, downloads, and logs. Clearing the
session closes Snowflake, removes MSAL cache entries, and drops runtime values.

## Run

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

The authenticated navigation contains Home, Explore, Report Lineage, Table
Impact, and Measure Impact. PowerAI opens as a floating modal over those pages.

## Test

The offline regression suite uses the Python standard-library `unittest`
runner:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s Test -p "test_*.py"
```

Live Power BI, Fabric, XMLA, Snowflake, or Anthropic checks are intentionally
separate from the offline regression suite.

## TLS

The app uses the operating-system certificate store through `truststore`.
Install the approved corporate root CA on the host or provide an approved PEM
bundle through `PBI_CA_BUNDLE` or `REQUESTS_CA_BUNDLE`. Never disable TLS
verification.

## Production Guardrails

- Package from tracked source using an explicit allowlist; do not zip a working
  developer directory.
- Never package `.venv`, caches, logs, downloads, local JSON configuration,
  `.streamlit/secrets.toml`, or diagnostic outputs.
- Do not create `config/app_settings.json`, `config/powerbi_auth_config.json`,
  `.env`, or another runtime credential file; use the connection sidebar.
- Keep PowerAI tools read-only and scoped to authorized workspaces.
- Never send credentials, access tokens, raw business rows, arbitrary SQL, or
  administrative operations to the assistant provider.
- Do not claim upstream or visual-level lineage without corresponding scanner,
  XMLA, report-definition, or uploaded-layout evidence.
- Keep report workspace and semantic-model workspace identities distinct.
- Use permission-blocked, unavailable, inferred, or ambiguous statuses instead
  of presenting missing evidence as an empty verified result.

## Documentation

- [Application context](APP_CONTEXT.md)
- [Session-only setup and SSO](docs/SESSION_SETUP_AND_SSO.md)
- [Deployment plan](docs/DEPLOYMENT_PLAN.md)
- [Organization deployment approval package](docs/ORG_DEPLOYMENT_APPROVAL_PACKAGE.md)
- [Architecture and flow diagram](docs/PBI_LINEAGE_APPLICATION_ARCHITECTURE_AND_FLOW.drawio)
- [PowerAI implementation](docs/CLAUDE_AGENTIC_AI_IMPLEMENTATION.md)
- [PowerAI managed-agent guide](docs/CLAUDE_MULTI_AGENT_GUIDE.md)
- [PowerAI configuration reference](docs/CLAUDE_CONFIGURATION_REFERENCE.md)
- [Functional coverage](docs/USER_EXPERIENCE_KPI_DEMO_AND_FUNCTIONAL_COVERAGE.md)
