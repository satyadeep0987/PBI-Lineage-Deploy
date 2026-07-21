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
```

`auth_flow = "device_code"` is the default for this deploy package. The user clicks sign in, opens Microsoft device login, enters the shown code, and then returns to the app.

The deploy app also accepts root-level names like `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_AUTHORITY`, and `PBI_SCOPES`.

Your Entra App Registration should allow public client/device-code sign-in and have the delegated Power BI API permissions needed by your workspace reports. The default MasterUser login does not require a client secret.

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

## Important Cloud Limitations

Streamlit Community Cloud runs on Linux. The existing `xmla_ado_com.py` helper uses Windows COM, `pywin32`, and the Microsoft MSOLAP provider. XMLA-dependent semantic lineage features may not work on Community Cloud until that connector is replaced with a Linux-compatible XMLA approach or moved behind a Windows backend.

REST-based Power BI inventory, report/app listing, CSV downloads, manual layout uploads, OpenAI measure explanations, and Snowflake connector features are packaged for cloud use, subject to your tenant permissions and network access.

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
