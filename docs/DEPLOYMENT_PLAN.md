# PBI Lineage Explorer deployment plan

## Decision in brief

Keep **Streamlit** for the first production release. It is the shortest, lowest-risk route because the application is already a Streamlit application and its pages, session state, downloads, and PowerAI interaction are implemented directly in Streamlit.

Host the first production release on a **private Windows Server VM in Azure**, protected by Microsoft Entra ID. This retains the current XMLA/COM functionality without a rewrite. Do not publish it as an anonymous public website: it can expose Power BI metadata, Snowflake lineage, and AI-assisted investigation of enterprise data.

The scalable target architecture is a Linux-hosted UI/API plus a small, private Windows XMLA service. Move to that architecture only after the current release is stable and usage justifies the refactor.

## Why Streamlit is the right UI now

| Option | Recommendation | Reason |
| --- | --- | --- |
| Streamlit | **Use now** | No UI rewrite; well suited to an internal analytical workflow, tables, downloads, lineage diagrams, and an assistant. |
| React/Next.js + FastAPI | **Target only if product scale demands it** | Best long-term option for a highly branded, externally used product with fine-grained frontend tests, an API contract, independent UI/backend deployment, and many concurrent users. It requires a material rewrite because the current business flow is implemented in Streamlit page functions. |
| Dash | Do not migrate | It would still require a Python UI rewrite but does not provide enough benefit over the current Streamlit implementation. |

Use the following decision trigger for a future React/FastAPI migration: choose it when the product needs a dedicated public customer experience, separate frontend/backend teams, long-lived REST APIs, or much richer client-side interaction than Streamlit provides. None of those are required merely to deploy the application safely today.

## Current technical constraint: Windows XMLA capability

The app has two feature classes:

1. **Cloud-safe features:** Power BI/Fabric REST calls, Snowflake calls, report exports, and the PowerAI assistant.
2. **Windows-dependent features:** XMLA semantic-model inspection implemented through Python COM, `pywin32`, and the Microsoft Analysis Services OLE DB provider (`MSOLAP`).

Power BI XMLA clients use Analysis Services client libraries; Microsoft identifies `MSOLAP` as the native OLE DB client library. XMLA read-only access can expose semantic-model data, metadata, events, and schema when capacity, workspace, and user permissions allow it. [Microsoft guidance](https://learn.microsoft.com/en-us/fabric/enterprise/powerbi/service-premium-connect-tools)

### Phase 1: preserve all functionality

Deploy the complete Streamlit process on a hardened **Windows Server 2022 Azure VM** in a private subnet.

- Install a supported Python version, the application dependencies, `pywin32`, and the current Analysis Services client libraries/MSOLAP provider.
- Run Streamlit under a Windows service account with no interactive administrator privileges.
- Put IIS (with a WebSocket-capable reverse-proxy configuration) or an approved enterprise reverse proxy in front of Streamlit; terminate TLS at the proxy.
- Allow outbound HTTPS only to required services: Microsoft Entra ID, Power BI/Fabric, Snowflake, the approved LLM provider, package/monitoring services as applicable.
- Do not expose the VM's Streamlit port directly to the internet.

This is the recommended first production deployment because it keeps the existing COM integration intact.

### Phase 2: isolate Windows rather than make the whole estate Windows-only

Refactor the XMLA calls behind a small internal service running on Windows. The primary web application then runs on Linux, while the Windows service exposes only bounded, read-only operations such as schema, measures, DAX, dependencies, and lineage metadata.

```text
Users
  -> Entra-protected HTTPS endpoint
  -> Linux Streamlit UI (initially) / React UI + API (later)
  -> private Windows XMLA service (MSOLAP + COM)
  -> Power BI/Fabric XMLA endpoint
```

The UI must never receive XMLA credentials. The XMLA service must require application authentication, accept only validated request shapes, enforce timeouts/result-size limits, and write audit logs. Keep it private to the virtual network.

### Alternatives and their limits

- A custom Windows container can be evaluated in a proof of concept, but it adds operational complexity and still requires validating COM/MSOLAP behavior. It is not the fastest route to reliable production.
- Replacing XMLA with REST APIs is possible only after a feature-by-feature validation. Do not assume REST APIs deliver every semantic-model metadata/dependency capability currently obtained through XMLA.
- Azure App Service's managed Python runtime is Linux-only; Microsoft notes that Python on Windows is no longer supported there, although a custom Windows container is possible. [Azure App Service guidance](https://learn.microsoft.com/en-us/azure/app-service/configure-language-python)

## Hosting platform recommendation

### Recommended platform: Azure

Azure is the best fit because this product already depends on Microsoft Entra ID, Power BI/Fabric, and Windows XMLA capabilities.

| Stage | Hosting choice | Purpose |
| --- | --- | --- |
| Pilot / first production release | Windows Server 2022 Azure VM + managed disk + private VNet | Full functional fidelity with minimal code change. |
| Scaled production | Azure Container Apps for the Linux web tier + private Windows VM/VM Scale Set for XMLA | Independent web scaling while retaining Windows-only connectivity. |
| Edge protection | Azure Application Gateway WAF or the organization's approved reverse proxy | TLS, web protection, routing, and a stable custom domain. |
| Secrets | Azure Key Vault + managed identity | No production secrets in source, local JSON, or container images. |
| Monitoring | Application Insights / Log Analytics | Health checks, errors, latency, dependency failures, and audit correlation. |

Azure Container Apps is appropriate for the Linux web tier because it supports HTTP-based scaling and revisions. It can reference Key Vault secrets through a managed identity. [Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app) and [Key Vault secret references](https://learn.microsoft.com/en-us/azure/container-apps/manage-secrets)

### Why not Streamlit Community Cloud for this production app

It is useful for a non-sensitive cloud-safe demo, but not the recommended production home for this application. It cannot run the present Windows COM/XMLA implementation, and this enterprise app needs Entra access controls, private-network connectivity, auditability, controlled egress, and enterprise secret management. Community Cloud can deploy apps quickly and has its own secret store, but it is not a substitute for this enterprise architecture. [Community Cloud deployment](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy) and [secrets guidance](https://docs.streamlit.io/deploy/concepts/secrets)

## Making the application available to other users

“Public” should mean **available through a public HTTPS URL to approved users**, not anonymous access.

1. Register a production Microsoft Entra application and use a production redirect URL/custom domain.
2. Make the Entra enterprise application assignment-required; grant access through security groups such as `PBI-Lineage-Explorer-Users` and `PBI-Lineage-Explorer-Admins`.
3. Keep the app's current user-delegated Power BI model where possible. Each user sees only reports and semantic models for which they already have Power BI/Fabric permission. This reduces the risk of the app becoming a data-access bypass.
4. Configure the required API permissions and tenant admin consent. Validate Build permission and XMLA/capacity requirements for every workspace that will be inspected.
5. Enforce MFA and Conditional Access. For partners, use Entra B2B guests and grant both application access and the required Power BI permissions; do not create shared credentials.
6. Publish through Entra Application Proxy if the application remains private/on-premises-style, or through Application Gateway WAF for an Azure-hosted endpoint. Entra Application Proxy provides authenticated, Conditional-Access-aware access to private applications without opening inbound ports to the application host. [Application Proxy security](https://learn.microsoft.com/en-us/entra/identity/app-proxy/application-proxy-security)
7. Choose one clear authentication boundary. The simplest first release is Entra pre-authentication at the publishing layer plus the app's existing Entra sign-in/session validation. Test the complete redirect/logout sequence before go-live.

Anonymous internet access is not approved for this product unless its data sources, exports, assistant prompts, and access model are redesigned for public data.

## Deployment work plan

### 1. Production readiness (before infrastructure)

- Move every credential and API key out of local configuration files into Azure Key Vault. Rotate any key that may have existed in a local or shared configuration file.
- Create separate development, test, and production Entra app registrations and Snowflake/LLM credentials.
- Pin tested Python and package versions; generate a dependency vulnerability report.
- Add configuration validation that fails startup if a required secret or allowed-origin value is missing.
- Decide the data-retention policy for downloads, logs, prompts, DAX, and report metadata. Do not log secrets or full sensitive query results.

### 2. Build the first production environment

- Provision resource group, private VNet/subnet, Windows Server VM, Key Vault, monitoring workspace, and private DNS/custom domain records.
- Install the app and Windows XMLA prerequisites using an automated script/image rather than manual-only setup.
- Configure a Windows service with automatic restart, a health endpoint/check, and non-admin identity.
- Configure the TLS reverse proxy and verify Streamlit/WebSocket traffic through the production hostname.
- Restrict inbound traffic to the proxy and approved administration paths; restrict outbound traffic to approved dependency endpoints.

### 3. Identity, authorization, and data protection

- Configure Entra group assignment, MFA/Conditional Access, consent, redirect URIs, and logout URLs.
- Test with four accounts: platform admin, normal authorized analyst, authorized user with limited Power BI access, and unauthorized user.
- Test service-principal versus delegated-user behavior separately; never silently fall back to a broader identity.
- Confirm CSV/ZIP downloads contain only information the signed-in user may inspect, and add audit events for exports.

### 4. Acceptance and go-live

- Execute a smoke test for report discovery, XMLA semantic objects, measure DAX, table/column lineage diagrams, Snowflake lineage, downloads, and PowerAI.
- Test network loss, expired tokens, unavailable XMLA endpoint, Snowflake failure, LLM failure, and service restart behavior.
- Set alerts for app unavailable, repeated authentication failures, XMLA/Snowflake/LLM dependency errors, high error rate, and disk/memory pressure.
- Publish an internal URL and support runbook. Start with an approved pilot group; expand group membership after acceptance.

### 5. Scale/refactor checkpoint

After 4–8 weeks of real usage, review concurrency, response times, Windows VM utilization, feature demand, and operating cost. Only then prioritize the private Windows XMLA service and Linux web tier split. A later React/FastAPI migration should be treated as a product evolution, not a deployment prerequisite.

## Go-live acceptance criteria

- All existing Windows/XMLA features work from the production hostname.
- Only assigned Entra users can open the application.
- A user cannot retrieve reports, measures, exports, or lineage beyond their Power BI/Fabric permissions.
- No secret exists in source control, deployment artifacts, browser storage, or application logs.
- TLS, MFA/Conditional Access, monitoring, backups/image recovery, and a support runbook are in place.
- The owner has tested a rollback to the prior VM/image or Container Apps revision.

