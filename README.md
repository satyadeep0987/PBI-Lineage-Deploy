# PBI Lineage Explorer

PBI Lineage Explorer is an enterprise Streamlit application for exploring Power
BI and Fabric lineage across reports, semantic models, source systems,
Snowflake objects, visual metadata, and AI-assisted impact analysis.

The application should run as a Streamlit process, but the full project is not
a fit for Streamlit Community Cloud. Full semantic-model lineage depends on
Windows COM, `pywin32`, and the Microsoft Analysis Services OLE DB Provider
(`MSOLAP`). Streamlit Community Cloud runs on Linux, so the full XMLA path must
be hosted on Windows or moved behind a separate Windows service.

## Deployment Decision

Use an Azure Windows Server VM for the first full-feature deployment.

| Target | Status | Reason |
| --- | --- | --- |
| Azure Windows Server VM | Recommended | Supports Streamlit, `pywin32`, COM, MSOLAP, XMLA, Fabric REST, Snowflake, and PowerAI/Claude features without a UI rewrite. |
| Streamlit Community Cloud | Not supported for full app | Linux runtime cannot install or execute Windows-only `pywin32`/COM/MSOLAP dependencies. |
| Azure App Service built-in Python | Not supported for full app | Built-in Python on App Service is Linux-only; Python on Windows is not supported by the managed runtime. |
| Linux UI plus private Windows XMLA API | Future scale path | Good long-term architecture after the current app is stable and XMLA calls are isolated behind a bounded internal service. |

## Project Context

The repository has been organized as a deployable application root:

```text
PBI-Lineage-Deploy/
|-- streamlit_app.py
|-- utils.py
|-- xmla_ado_com.py
|-- tls_trust.py
|-- requirements.txt
|-- pbi_modules/
|-- config/
|-- .streamlit/
|-- docs/
|-- demo_packages/
|-- Images/
|-- presentations/
|-- Test/
`-- README.md
```

Folder and file responsibilities:

| Path | Purpose |
| --- | --- |
| `streamlit_app.py` | Main Streamlit entry point, authentication flow, routing, Power BI/Fabric API calls, XMLA lineage, report-definition parsing, Snowflake lineage, exports, and PowerAI workspace integration. |
| `pbi_modules/` | Shared UI shell, controls, Claude/PowerAI agent orchestration, Markdown analysis export helpers, and table-impact helpers. |
| `utils.py` | Configuration loader for JSON files, environment variables, and Streamlit secrets. |
| `xmla_ado_com.py` | Windows-only XMLA connector using ADO COM, `pywin32`, and MSOLAP. |
| `tls_trust.py` | TLS trust-store and custom CA bundle support for corporate Windows environments. |
| `config/` | Safe template files for Power BI auth and app settings. Filled JSON files are intentionally ignored by Git. |
| `.streamlit/` | Streamlit runtime config and secrets example. Real `.streamlit/secrets.toml` is ignored. |
| `docs/` | Architecture, deployment plan, Claude/PowerAI setup, user-experience coverage, and admin/provisioning references. |
| `demo_packages/` | Isolated Snowflake-to-Power BI ten-level lineage demo with CSVs, SQL, DAX, Power BI build guide, and presenter runbooks. |
| `Images/` | Architecture and flow images used for explanation or presentation. |
| `presentations/` | PowerPoint decks and preview images for stakeholder walkthroughs. |
| `Test/` | Unit and demo tests for Claude, lineage display, impact suggestions, PowerAI diagrams, Snowflake procedures, and workspace filtering. |
| `.venv/`, `__pycache__/`, `.git/` | Local environment/generated folders; do not deploy or document as product source. |

## What The App Does

- Authenticates users with Microsoft Entra ID and Power BI/Fabric delegated
  scopes.
- Lists accessible Power BI workspaces, reports, apps, dashboards, semantic
  models, and access metadata.
- Resolves report-to-semantic-model context for workspace and app artifacts.
- Reads semantic model tables, columns, measures, DAX, relationships, and
  dependencies through XMLA.
- Retrieves Fabric/Power BI report definitions to parse pages, visuals, visual
  fields, roles, and visual-to-source lookup evidence.
- Runs table and measure impact analysis across authorized reports and models.
- Connects optional Snowflake lineage for physical object and column tracing.
- Provides a read-only PowerAI/Claude lineage agent that uses bounded local
  tools and never receives raw Power BI, Fabric, Snowflake, or Entra secrets.
- Exports CSVs and report-detail ZIP/Markdown packages for audit and handoff.

## Runtime Requirements

For the full feature set:

- Azure Windows Server 2022 or later, sized for expected concurrent users.
- Python 3.11 64-bit.
- Microsoft Analysis Services OLE DB Provider (`MSOLAP`) installed on the
  Windows host.
- Power BI/Fabric tenant and workspace permissions for the selected reports and
  semantic models.
- XMLA endpoint enabled for the target Power BI Premium, Premium Per User, or
  Fabric capacity/workspace scenario.
- Outbound HTTPS access to Microsoft Entra ID, Power BI, Fabric, Snowflake, and
  the approved AI provider when those features are enabled.
- No production secrets committed to Git.

Python dependencies are installed from `requirements.txt`. `pywin32` is guarded
with a Windows platform marker, but XMLA features still need a Windows runtime.

## Local Windows Run

Run from the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Before starting the app, configure one of these ignored local secret sources:

```text
config/powerbi_auth_config.json
config/app_settings.json
.streamlit/secrets.toml
```

Use the provided templates:

```text
config/powerbi_auth_config.template.json
config/app_settings.template.json
.streamlit/secrets.toml.example
```

## Configuration Model

The app reads configuration in this order:

1. Built-in safe defaults.
2. Template/default files under `config/`.
3. Optional local JSON files under `config/`.
4. Environment variables and Streamlit secrets.

Important environment variable names include:

```text
PBI_TENANT_ID
PBI_CLIENT_ID
PBI_AUTHORITY
PBI_SCOPES
PBI_FABRIC_SCOPES
ANTHROPIC_API_KEY
CLAUDE_ENABLED
CLAUDE_MODEL
PBI_CA_BUNDLE
REQUESTS_CA_BUNDLE
```

Keep filled files and secrets out of source control. The `.gitignore` already
excludes local config, secrets, caches, exports, logs, and virtual environments.

## Azure Windows Server Deployment

This is the recommended production deployment for the current codebase.

### 1. Provision Azure Resources

Create:

- Resource group for the application environment.
- Private virtual network and subnet.
- Windows Server VM with managed disk and system-assigned managed identity.
- Network security group with no public inbound access to Streamlit port `8501`.
- Azure Key Vault for secrets and certificates.
- Log Analytics workspace or Application Insights for operational telemetry.
- Azure Application Gateway WAF, Microsoft Entra Application Proxy, or an
  approved enterprise reverse proxy for HTTPS publishing.

Use a private DNS name or custom domain for the application. Do not expose the
raw Streamlit process directly to the internet.

### 2. Prepare The Windows VM

Install on the VM:

- Python 3.11 64-bit.
- Git or your approved artifact deployment agent.
- Microsoft Analysis Services OLE DB Provider (`MSOLAP`) amd64.
- Any corporate root CA certificate required for TLS inspection.
- Monitoring, endpoint protection, and patch-management agents required by the
  Azure baseline.

Clone or copy the repo to a stable path such as:

```text
C:\apps\pbi-lineage-explorer
```

Install the app:

```powershell
cd C:\apps\pbi-lineage-explorer
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. Configure Secrets

Recommended production pattern:

- Store tenant IDs, client IDs, client secrets, Snowflake credentials, Claude
  keys, and CA bundle paths in Azure Key Vault or the enterprise secret manager.
- Grant the VM managed identity access only to the required secrets.
- Materialize secrets as locked-down environment variables at service startup,
  or generate a protected `.streamlit/secrets.toml` readable only by the service
  identity.
- Rotate any secret that was ever copied into a local shared file.

The app can use device-code delegated auth for user-scoped access or configured
service-principal settings where tenant policy allows it. Validate the selected
mode with a normal analyst account and a restricted analyst account before
go-live.

### 4. Run Streamlit As A Service

Run the Streamlit app under a dedicated non-admin Windows service account.

For a same-VM reverse proxy, bind Streamlit to localhost:

```powershell
C:\apps\pbi-lineage-explorer\.venv\Scripts\python.exe -m streamlit run C:\apps\pbi-lineage-explorer\streamlit_app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

For Azure Application Gateway connecting directly to the VM backend, bind
Streamlit to the VM private interface and allow port `8501` only from the
Application Gateway subnet.

Configure the Windows service or approved service wrapper with:

- Automatic start.
- Restart on failure.
- Working directory set to the repository root.
- Environment variables loaded before process start.
- Log redirection to an approved local path or collector.

### 5. Publish The HTTPS Endpoint

Use one of these patterns:

- Azure Application Gateway WAF with TLS certificate, custom domain, WebSocket
  support, and backend routing to the VM private address.
- Microsoft Entra Application Proxy with Entra pre-authentication and
  Conditional Access for a private internal-style application.
- Enterprise reverse proxy/IIS in front of the localhost Streamlit process,
  provided WebSocket upgrade traffic is supported.

Required controls:

- Entra assignment required for the enterprise app.
- Access granted by security groups such as `PBI-Lineage-Explorer-Users` and
  `PBI-Lineage-Explorer-Admins`.
- MFA and Conditional Access enabled.
- No anonymous public access.
- Inbound network access limited to the proxy path.
- Outbound egress limited to required Microsoft, Snowflake, AI-provider, package,
  and monitoring endpoints.

### 6. Validate Go-Live

Test with representative accounts:

- Platform/admin account.
- Normal authorized analyst.
- Authorized analyst with limited Power BI access.
- Unauthorized user.

Smoke-test:

- Microsoft sign-in and logout.
- Workspace and report inventory.
- Report lineage.
- XMLA semantic model objects, relationships, measures, and dependencies.
- Fabric report-definition retrieval and visual-level lineage.
- Table impact and measure impact.
- Snowflake lineage, if enabled.
- PowerAI/Claude answer generation, if enabled.
- CSV, ZIP, and Markdown downloads.
- Service restart and proxy/WebSocket behavior.

## Why Streamlit Community Cloud Is Not The Target

The app imports a Windows XMLA connector and full semantic lineage calls require
`pywin32`, COM, and MSOLAP. Streamlit documents that Community Cloud runs apps
in a Linux environment, where `pywin32` fails, and recommends excluding
`pywin32` or deploying on a cloud service that offers Windows machines.

Some REST-only features can be demonstrated on Linux if XMLA paths are not used,
but that is not the complete PBI Lineage Explorer product. Production should use
Azure Windows Server now, then optionally split the architecture later:

```text
Users
  -> Entra-protected HTTPS endpoint
  -> Streamlit UI/API tier
  -> private Windows XMLA service
  -> Power BI/Fabric XMLA endpoint
```

## TLS On Windows

The app uses the operating system certificate store through `truststore`.
Install the complete requirements before starting Streamlit:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the VM is behind an HTTPS-inspecting proxy, install the organization root CA
in the Windows Trusted Root Certification Authorities store. If security policy
requires a PEM bundle, set it before service startup:

```powershell
$env:PBI_CA_BUNDLE = "C:\certificates\organization-ca-chain.pem"
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

`REQUESTS_CA_BUNDLE` is also supported. Do not bypass TLS verification with
`verify=False`.

## Supporting Documentation

- [Deployment plan](docs/DEPLOYMENT_PLAN.md)
- [Claude agentic AI implementation](docs/CLAUDE_AGENTIC_AI_IMPLEMENTATION.md)
- [Claude multi-agent guide](docs/CLAUDE_MULTI_AGENT_GUIDE.md)
- [Claude configuration reference](docs/CLAUDE_CONFIGURATION_REFERENCE.md)
- [User experience, KPI demo, and functional coverage](docs/USER_EXPERIENCE_KPI_DEMO_AND_FUNCTIONAL_COVERAGE.md)
- [Snowflake to Power BI ten-level lineage demo](demo_packages/snowflake_powerbi_10_level_lineage/README.md)

## Official References

- Streamlit dependency guidance:
  <https://docs.streamlit.io/deploy/concepts/dependencies>
- Streamlit `pywin32` / Linux Community Cloud limitation:
  <https://docs.streamlit.io/knowledge-base/dependencies/no-matching-distribution>
- Streamlit secrets management:
  <https://docs.streamlit.io/deploy/concepts/secrets>
- Azure App Service Python runtime guidance:
  <https://learn.microsoft.com/en-us/azure/app-service/configure-language-python>
- Azure Application Gateway WebSocket support:
  <https://learn.microsoft.com/en-us/azure/application-gateway/application-gateway-websocket>
- Microsoft Entra Application Proxy security:
  <https://learn.microsoft.com/en-us/entra/identity/app-proxy/application-proxy-security>
- Windows VM managed identity with Key Vault:
  <https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/tutorial-windows-managed-identities-vm-access>
- Analysis Services client libraries and MSOLAP:
  <https://learn.microsoft.com/en-us/analysis-services/client-libraries>
- Power BI/Fabric XMLA endpoint:
  <https://learn.microsoft.com/en-us/fabric/enterprise/powerbi/service-premium-connect-tools>

