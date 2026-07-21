# PBI Lineage Explorer - Streamlit Cloud Deploy

This folder is a clean repository root for deploying the Streamlit app from:

```text
C:\Users\Administrator\Desktop\PBI-Lineage\streamlit_app\streamlit_app.py
```

In this deploy repo, the app entrypoint is:

```text
streamlit_app.py
```

## Included Files

```text
deploy/
|-- streamlit_app.py
|-- utils.py
|-- xmla_ado_com.py
|-- pbi_modules/
|-- config/
|   |-- app_settings.template.json
|   `-- powerbi_auth_config.template.json
|-- .streamlit/
|   |-- config.toml
|   `-- secrets.toml.example
|-- requirements.txt
|-- .gitignore
`-- README.md
```

No filled credential file is included. Keep real secrets out of Git.

## Deploy On Streamlit Community Cloud

1. Create a new GitHub repository using the contents of this `deploy/` folder.
2. Go to <https://share.streamlit.io/deploy>.
3. Select the repository and branch.
4. Set the app file path to `streamlit_app.py`.
5. Open **Advanced settings**.
6. Select Python `3.11` to match the current local development environment.
7. Paste filled secrets from `.streamlit/secrets.toml.example`.
8. Deploy the app.

Streamlit Community Cloud runs apps from the repository root and installs Python dependencies from `requirements.txt`. Its docs also note that the entrypoint path should use forward slashes when the app is in a subfolder. This package avoids that by placing `streamlit_app.py` at the repo root.

References:
- <https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/file-organization>
- <https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/app-dependencies>
- <https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management>
- <https://learn.microsoft.com/en-us/rest/api/fabric/report/items/get-report-definition>
- <https://learn.microsoft.com/en-us/rest/api/fabric/articles/scopes>

## Configuration Model

The deploy app can read configuration from three places:

1. Streamlit Cloud secrets, preferably the `[powerbi]` TOML section shown below.
2. Environment variables or root-level Streamlit secrets such as `PBI_TENANT_ID`.
3. Local JSON files, for local-only development.

For Streamlit Cloud, use secrets instead of committing filled JSON files.

### Power BI Auth

Paste this into **Streamlit Community Cloud > App settings > Secrets**:

```toml
[powerbi]
auth_flow = "device_code"
tenant_id = "<tenant-id>"
client_id = "<entra-app-registration-client-id>"
authority = "https://login.microsoftonline.com/<tenant-id>"
scopes = [
  "https://analysis.windows.net/powerbi/api/App.Read.All",
  "https://analysis.windows.net/powerbi/api/Report.Read.All",
  "https://analysis.windows.net/powerbi/api/Dashboard.Read.All",
  "https://analysis.windows.net/powerbi/api/Dataset.Read.All",
  "https://analysis.windows.net/powerbi/api/Workspace.Read.All",
  "https://analysis.windows.net/powerbi/api/Tenant.Read.All",
]
fabric_scopes = [
  "https://api.fabric.microsoft.com/Report.ReadWrite.All",
]
```

`auth_flow = "device_code"` is the default for this deploy package. The user clicks sign in, opens Microsoft device login, enters the shown code, and then returns to the app.

The deploy app also accepts root-level names like `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_AUTHORITY`, and `PBI_SCOPES`.

Your Entra App Registration should allow public client/device-code sign-in and have the delegated Power BI API permissions needed by your workspace reports. Grant delegated `Report.ReadWrite.All` consent for the Fabric API so the app can call Get Report Definition. Keep Fabric scopes separate from the Power BI scopes because the APIs use different token audiences. The default MasterUser login does not require a client secret.

### App Settings

Safe defaults live in:

```text
config/app_settings.template.json
```

On Streamlit Cloud, override values through secrets sections:

```toml
[openai_measure_definitions]
enabled = true
api_key = "<openai-api-key>"
model = "gpt-5-nano"

[snowflake_lineage]
enabled = true
account = "<snowflake-account>"
user = "<snowflake-user>"
password = "<snowflake-password>"
role = "<role>"
warehouse = "<warehouse>"
database = "<database>"
schema = "<schema>"
```

For local-only testing, you may create these ignored files:

```text
config/powerbi_auth_config.json
config/app_settings.json
.streamlit/secrets.toml
```

Do not commit those filled files.

## Local Test

From this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Before running locally, fill either `.streamlit/secrets.toml` or local JSON config files.

On Windows, `requirements.txt` installs `pywin32` through a platform-specific marker. XMLA lineage also requires the Microsoft Analysis Services OLE DB Provider (MSOLAP) on the machine.

### TLS Certificate Errors on Windows

The app uses the operating system certificate store through `truststore`. After moving the app to another machine, install the complete requirements before starting Streamlit:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the machine is behind an HTTPS-inspecting corporate proxy, install the organization's root CA in the Windows **Trusted Root Certification Authorities** store. When that is not possible, obtain the approved PEM CA chain from your network/security team and set it before starting the app:

```powershell
$env:PBI_CA_BUNDLE = "C:\certificates\organization-ca-chain.pem"
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py
```

`REQUESTS_CA_BUNDLE` is also supported. Do not work around certificate errors with `verify=False`; that disables server identity verification.

## Important Cloud Limitations

Streamlit Community Cloud runs on Linux. The existing `xmla_ado_com.py` helper uses Windows COM, `pywin32`, and the Microsoft MSOLAP provider. `pywin32` cannot be installed or used on Streamlit Community Cloud, so XMLA-dependent semantic lineage features require either a Windows deployment host or a separate Windows backend service. Do not add unqualified `pywin32` to `requirements.txt` for Streamlit Cloud because Linux dependency installation will fail.

REST-based Power BI inventory, report/app listing, Fabric report-definition retrieval, visual-layout parsing, CSV downloads, manual layout uploads, OpenAI measure explanations, and Snowflake connector features are packaged for cloud use, subject to your tenant permissions and network access. Report definitions use `api.fabric.microsoft.com` and do not require Windows, `pywin32`, MSOLAP, or full PBIX download permission.

## Git Push

```powershell
cd C:\Users\Administrator\Desktop\PBI-Lineage-Deploy
git init
git add .
git commit -m "Prepare Streamlit Cloud deployment"
git branch -M main
git remote add origin <your-new-github-repo-url>
git push -u origin main
```
