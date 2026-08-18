# Organization deployment approval package

Date prepared: 2026-08-18

## Executive status

The application is ready to be submitted for repository onboarding, SAST, and
security review. It is not yet cleared for production cloud deployment because
the organization-mandated security validation, budget approval, cloud governance
approval, SAST, and VAPT stages still need to complete.

Recommended first deployment target for the initial 20-person rollout: Azure,
Central India region, private Windows Server 2022 host. The Windows host is
required for the current XMLA semantic-model inspection path because it uses
ADODB COM, `pywin32`, and the Microsoft Analysis Services OLE DB Provider.

Important deployment boundary: the current local browser sign-in flow opens the
browser on the machine running Streamlit. A remote HTTPS cloud deployment needs
a tenant-approved Entra web or SPA authorization-code architecture and a
Snowflake OAuth integration before production go-live.

## Process mapping

| Organization step | Current project position | Required action |
| --- | --- | --- |
| 1. Security validation | Not completed | Submit source and docs for prerequisite security assessment. Remediate critical/high findings before deployment. |
| 2. Budget estimation and approval | Draft estimate prepared below | Confirm usage assumptions, region, HA/WAF requirement, and submit to Entity Lead and Finance. |
| 3. Cloud governance review | Pending budget approval | Start cloud onboarding, compliance validation, and architecture review after budget sign-off. |
| 4. Source repository creation | Pending org action | Secure repository to be created by the assigned SPOC with development-team access. |
| 5. Source upload | Ready after repository is available | Upload tracked source, docs, tests, and deployment artifacts only. Do not upload local caches, virtual environments, diagnostics, secrets, or generated outputs. |
| 6. SAST | Pending repository integration | Integrate SonarCloud, remediate security and quality findings, and retain scan evidence. |
| 7. Cloud provisioning | Blocked until approvals | Provision resources only after security, SAST, budget, and governance approvals. |
| 8. VAPT | Pending provisioned environment | Run pre-deployment VAPT and post-deployment validation. Add WAF, DAST, and network validation if internet-facing. |
| 9. Final clearance | Pending all prior stages | Cloud Security must confirm critical/high remediation, compliance, and cloud-security-standard alignment. |

## Budget assumptions

- Cloud provider: Microsoft Azure.
- Region: Central India. Adjust if the final subscription mandates another
  region.
- User base: approximately 20 named internal users.
- Expected concurrency: 3 to 5 active users at the same time during normal
  usage, with occasional short bursts.
- Runtime: one Windows Server 2022 VM for the 20-person first release.
- VM size: `Standard_D4as_v5` Windows, 4 vCPU and 16 GiB RAM, running 730 hours
  per month.
- Storage: managed OS/data disks, approximately 256 GiB total.
- Traffic: internal 20-user rollout, under 50 GB/month egress.
- Monitoring: Log Analytics/Application Insights at approximately 10 GB/month
  ingestion.
- Security perimeter:
  - Private/internal publishing: no Application Gateway WAF line item.
  - Public or internet-facing publishing: Application Gateway WAF v2 required.
- Exclusions: Power BI/Fabric licenses or capacity, Snowflake credits, Anthropic
  API usage, corporate network/VPN charges, SonarCloud licensing, reserved
  instance discounts, support plans, taxes, enterprise discounts, and currency
  conversion.

Prices were checked from the Azure Retail Prices API on 2026-08-18. Azure
retail prices are USD list prices and must be validated by Finance against the
organization's agreement, taxes, discounts, and billing currency.

## Monthly Azure estimate

| Cost item | Assumption | Estimated monthly cost |
| --- | --- | ---: |
| Windows VM | `Standard_D4as_v5`, Central India, 730 hours at USD 0.295/hour | USD 215 |
| Managed disks | Premium/standard managed disk allowance for OS and app data | USD 60 |
| Azure Backup | Azure VM protected instance plus LRS backup storage allowance | USD 20 |
| Monitoring | Log Analytics/Application Insights, about 10 GB/month | USD 35 |
| Key Vault | Standard operations for secrets/certificates | USD 5 |
| Public IP and light egress | Static IP and low outbound transfer allowance | USD 10 |
| Operational buffer | Small allowance for minor meter variance | USD 40 |
| Estimated private/internal 20-user total | No WAF, single VM | **USD 385/month** |

If the application is published as an internet-facing endpoint, add Application
Gateway WAF v2:

| Additional cost item | Assumption | Estimated monthly cost |
| --- | --- | ---: |
| Application Gateway WAF v2 | Fixed cost, Central India, 730 hours at USD 0.504/hour | USD 368 |
| WAF capacity units | One low-traffic capacity unit, 730 hours at USD 0.0144/hour | USD 11 |
| Estimated public/WAF 20-user total | Private 20-user total plus WAF | **USD 765/month** |

For a higher-concurrency production profile, budget a larger VM or high
availability pair. A practical first HA estimate is USD 950 to USD 1,200/month
before Power BI, Snowflake, Anthropic, support, taxes, or enterprise discounts.

Approximate infrastructure cost per named user for the 20-person rollout:

| Deployment option | Monthly estimate | Approximate cost per named user |
| --- | ---: | ---: |
| Private/internal | USD 385/month | USD 19/user/month |
| Public/WAF | USD 765/month | USD 38/user/month |
| HA production profile | USD 950 to USD 1,200/month | USD 48 to USD 60/user/month |

## Security and deployment readiness notes

- The application no longer relies on runtime JSON/YAML/`.env` files for
  connection settings or credentials.
- Power BI, Snowflake, and PowerAI settings are entered through the runtime
  sidebar and retained in Streamlit session memory.
- Power BI and Snowflake login are independent. Power BI requires only Tenant ID
  and Client ID; workspace/report scoping is optional and separate.
- Snowflake password input is not accepted; Snowflake uses browser SSO in the
  current local/co-located mode.
- The production source upload must exclude `.venv`, caches, logs, diagnostics,
  local config files, local secrets, and generated outputs.
- Any credentials that previously existed in local files or shared locations
  must be rotated before production review.
- The active browser sign-in implementation is suitable for a local or
  co-located interactive Streamlit host. For cloud production with remote users,
  plan a security-approved auth update:
  - Power BI/Entra: web app authorization-code flow with secure server session
    handling, or browser-side SPA flow approved by the tenant.
  - Snowflake: Snowflake OAuth authorization-code integration instead of the
    local `externalbrowser` loopback flow.

## Approval response draft

Subject: PBI Lineage Explorer - budget estimate and deployment readiness

Hi Team,

We have reviewed the deployment process and agree with the staged governance
approach. The application is ready to be submitted for secure repository
onboarding, SAST, and the prerequisite security validation. Production
deployment will remain blocked until critical/high findings are remediated and
security/governance approvals are completed.

For initial budget approval, we recommend an Azure Central India private Windows
Server deployment sized for approximately 20 named internal users, with expected
normal concurrency of 3 to 5 active users. This is appropriate for the first
release because the current XMLA capability depends on Windows COM/MSOLAP. The
estimated monthly Azure infrastructure cost is approximately USD 385/month for a
private/internal rollout, or about USD 19 per named user per month. If the
application must be internet-facing with Application Gateway WAF v2, the
estimate increases to approximately USD 765/month, or about USD 38 per named
user per month. A higher-availability production profile should be budgeted at
approximately USD 950 to USD 1,200/month before external service usage, taxes,
support plans, or enterprise discounts.

This estimate excludes Power BI/Fabric licensing or capacity, Snowflake credits,
Anthropic API usage, SonarCloud licensing, and any organization-specific network
or security tooling charges. Final values should be validated by Finance using
the approved Azure subscription, billing currency, region, and enterprise
discounts.

One architecture item should be reviewed before cloud go-live: the current
interactive browser authentication is suitable for a local/co-located Streamlit
host. A remote HTTPS deployment will require a tenant-approved Entra web/SPA
authorization-code design and a Snowflake OAuth integration.

Once Entity Lead and Finance approve the budget, Cloud Governance can initiate
cloud onboarding, compliance validation, architecture review, and resource
provisioning as per the defined process.

Regards,
