# Sidebar Connections And Browser SSO

## Runtime Flow

1. The existing Home page renders immediately. It is not replaced by a setup
   wizard.
2. `pbi_modules/connection_sidebar.py` renders independent **Power BI login**,
   **Power BI scope (optional)**, **Snowflake database**, and optional PowerAI
   panels in the persistent sidebar.
3. Power BI requires only Tenant ID and Client ID. MSAL opens the organization
   browser, validates tenant plus immutable principal claims, and probes the
   Power BI API with an explicit timeout. When no optional scope is supplied,
   accessible workspace reports are discovered dynamically.
4. Snowflake independently opens browser SSO and validates `CURRENT_ACCOUNT()`,
   `CURRENT_USER()`, `CURRENT_ROLE()`, `CURRENT_WAREHOUSE()`, and
   `CURRENT_DATABASE()` before retaining the live connection.
5. A successful provider locks its input fields. Its own Disconnect action
   clears only that provider; it does not reset the other connection.

Power BI enables inventory, search, Explore, Report Lineage, Table Impact, and
Measure Impact. Snowflake enables database/source-lineage operations. PowerAI is
available only when its own session key and Power BI are ready.

## Form Contract

### Power BI login

- Tenant ID — required UUID
- Client ID — required UUID

### Power BI scope (separate and optional)

- Workspace ID — optional UUID
- Report IDs — optional UUID list; requires Workspace ID when supplied

Leaving both scope values blank means workspace reports accessible to the
delegated user. Supplying only Workspace ID limits dynamic inventory to that
workspace. Supplying Report IDs creates an exact report allowlist.

### Snowflake database

- Account Identifier
- Database
- Warehouse
- Dedicated read-only Role
- SSO Login Name (a non-secret Snowflake `LOGIN_NAME`)

No Power BI, Snowflake, or Entra password is collected.

### PowerAI

- Disabled: no values
- Single agent: one Claude API key
- Managed multi-agent: one Claude API key, environment ID, and seven
  role-specific agent IDs

Agent IDs are routing identifiers, not additional API keys.

## Memory And Cleanup Boundaries

- Public parameters, bearer tokens, in-memory MSAL cache data, the Snowflake
  connection, the Claude key, and validation state live in `st.session_state`.
- The active local browser flows do not use an application query callback,
  cookie, local config, or cross-session redirect bridge.
- Provider disconnect and expiry are isolated: Power BI expiry does not close
  Snowflake or remove the Claude key; Snowflake failure does not invalidate the
  Power BI token.
- Full **Clear session** closes Snowflake, removes MSAL accounts/cache
  references, clears application metadata, and deletes Streamlit session state.
- Python strings cannot be cryptographically zeroized; cleanup removes all
  application references and relies on process/session isolation.

## Deployment Boundary

MSAL's no-client-secret interactive browser method and Snowflake
`externalbrowser` both open a browser on the machine running Python and use a
loopback callback. They are therefore supported for a local/co-located
interactive Streamlit host. A remote HTTPS deployment needs tenant-approved web
identity architecture for Entra and a configured Snowflake OAuth integration;
those integrations require deployment metadata or external secret management
that is intentionally not accepted by these forms.

Fabric Get Report Definition remains optional because it requires
`Report.ReadWrite.All` (or `Item.ReadWrite.All`) and read/write permission on the
report. Demo-specific stored-procedure column lineage remains disabled in the
strict session-only path.

References:

- [MSAL Python token acquisition](https://learn.microsoft.com/en-us/entra/msal/python/getting-started/acquiring-tokens)
- [Microsoft redirect URI guidance](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url)
- [Fabric Get Report Definition](https://learn.microsoft.com/en-us/rest/api/fabric/report/items/get-report-definition)
- [Snowflake Python Connector parameters](https://docs.snowflake.com/en/developer-guide/python-connector/python-connector-api)
