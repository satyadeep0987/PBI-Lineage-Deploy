"""Session-only setup controller for Power BI, Snowflake, and PowerAI.

This module deliberately has no filesystem-backed configuration path. Runtime
connection parameters, OAuth flow material, bearer tokens, and the PowerAI API
key are supplied by the connection sidebar and kept in caller-owned session state.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import re
import secrets
import select
import socket
import time
import types
import uuid
import webbrowser
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests


SETUP_STATE_KEY = "_runtime_setup_v1"
SETUP_VERSION = 2
AUTH_FLOW_TTL_SECONDS = 15 * 60
HTTP_TIMEOUT_SECONDS = 20

POWER_BI_SCOPES = (
    "https://analysis.windows.net/powerbi/api/App.Read.All",
    "https://analysis.windows.net/powerbi/api/Report.Read.All",
    "https://analysis.windows.net/powerbi/api/Dashboard.Read.All",
    "https://analysis.windows.net/powerbi/api/Dataset.Read.All",
    "https://analysis.windows.net/powerbi/api/Workspace.Read.All",
    "https://analysis.windows.net/powerbi/api/Tenant.Read.All",
)

# Fabric Get Report Definition currently requires Report.ReadWrite.All even
# though the operation only retrieves metadata. Keep this as a separate token
# audience; it must never be substituted for the Power BI REST/XMLA token.
FABRIC_SCOPES = ("https://api.fabric.microsoft.com/Report.ReadWrite.All",)

MANAGED_AGENT_FIELDS = (
    "general_lineage_agent_id",
    "powerbi_semantic_agent_id",
    "visual_evidence_agent_id",
    "snowflake_lineage_agent_id",
    "impact_analysis_agent_id",
    "evidence_reviewer_agent_id",
    "coordinator_agent_id",
)

_SNOWFLAKE_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SNOWFLAKE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
_CLAUDE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,254}$")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ANTHROPIC_KEY_PATTERN = re.compile(r"\bsk-ant-[A-Za-z0-9_-]+\b")
_FORBIDDEN_SNOWFLAKE_ROLES = frozenset(
    {"ACCOUNTADMIN", "ORGADMIN", "SECURITYADMIN", "SYSADMIN", "USERADMIN"}
)


class SetupValidationError(ValueError):
    """Raised when a setup parameter is missing or unsafe."""


class SetupAuthenticationError(RuntimeError):
    """Raised when an interactive authentication step cannot complete."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _required_text(value: Any, label: str) -> str:
    normalized = _text(value)
    if not normalized:
        raise SetupValidationError(f"{label} is required.")
    if any(ord(character) < 32 for character in normalized):
        raise SetupValidationError(f"{label} contains unsupported control characters.")
    return normalized


def _uuid(value: Any, label: str) -> str:
    normalized = _required_text(value, label)
    try:
        return str(uuid.UUID(normalized))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SetupValidationError(f"{label} must be a valid UUID.") from exc


def parse_report_ids(value: Any) -> list[str]:
    """Parse one or more comma/newline-separated Power BI report UUIDs."""
    if isinstance(value, (list, tuple, set)):
        raw_values: Iterable[Any] = value
    else:
        raw_values = re.split(r"[,;\n\r]+", _text(value))

    report_ids: list[str] = []
    seen = set()
    for raw_value in raw_values:
        if not _text(raw_value):
            continue
        report_id = _uuid(raw_value, "Report ID")
        if report_id not in seen:
            seen.add(report_id)
            report_ids.append(report_id)
    if not report_ids:
        raise SetupValidationError("At least one Power BI Report ID is required.")
    return report_ids


def _snowflake_account(value: Any) -> str:
    account = _required_text(value, "Snowflake Account Identifier")
    if account.casefold().endswith(".snowflakecomputing.com"):
        raise SetupValidationError(
            "Snowflake Account Identifier must not include the snowflakecomputing.com suffix."
        )
    if not _SNOWFLAKE_ACCOUNT_PATTERN.fullmatch(account):
        raise SetupValidationError("Snowflake Account Identifier has an unsupported format.")
    return account


def _snowflake_identifier(value: Any, label: str) -> str:
    identifier = _required_text(value, label)
    if not _SNOWFLAKE_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise SetupValidationError(
            f"{label} must be an unquoted Snowflake identifier using letters, digits, _, or $."
        )
    return identifier


def _claude_identifier(value: Any, label: str) -> str:
    identifier = _required_text(value, label)
    if not _CLAUDE_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise SetupValidationError(f"{label} has an unsupported format.")
    return identifier


def normalize_redirect_uri(value: Any) -> str:
    """Return a query-free HTTP(S) redirect URI for the current application."""
    raw_uri = _required_text(value, "Power BI redirect URI")
    parsed = urlsplit(raw_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SetupValidationError("Power BI redirect URI must be an HTTP(S) application URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise SetupValidationError("Power BI redirect URI contains unsupported URL components.")
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SetupValidationError("Power BI redirect URI must use HTTPS outside local development.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


def is_loopback_redirect_uri(value: Any) -> bool:
    """Return whether the redirect is supported by this public-client flow."""
    parsed = urlsplit(normalize_redirect_uri(value))
    return (parsed.hostname or "").casefold() in {"localhost", "127.0.0.1"}


def create_empty_setup_state(now: Optional[float] = None) -> Dict[str, Any]:
    """Create a provider-neutral runtime state for independent sidebar login."""
    now = time.time() if now is None else float(now)
    return {
        "version": SETUP_VERSION,
        "attempt_id": secrets.token_urlsafe(18),
        "phase": "connections_available",
        "created_at": now,
        "last_activity": now,
        "public": {
            "powerbi": {
                "tenant_id": "",
                "client_id": "",
                "workspace_id": "",
                "report_ids": [],
                "scope_mode": "all_accessible",
                "validated_scope": {},
                "validated_workspace": {},
                "validated_reports": [],
            },
            "snowflake": {
                "account": "",
                "database": "",
                "warehouse": "",
                "role": "",
                "user_override": "",
            },
            "claude": {
                "mode": "disabled",
                "environment_id": "",
                "managed_agents": {},
            },
        },
        "credentials": {
            "claude_api_key": "",
            "powerbi": {
                "access_token": "",
                "expires_at": 0.0,
                "fabric_token": "",
                "fabric_expires_at": 0.0,
                "token_cache": "",
                "auth_flow": None,
                "identity": {},
            },
            "snowflake": {"connection": None, "identity": {}},
        },
        "status": {
            "powerbi": {
                "state": "not_configured",
                "message": "Enter Tenant ID and Client ID to connect.",
            },
            "fabric": {
                "state": "not_configured",
                "message": "Optional Fabric access follows Power BI sign-in.",
            },
            "snowflake": {
                "state": "not_configured",
                "message": "Enter database connection parameters to connect.",
            },
            "claude": {
                "state": "disabled",
                "message": "PowerAI is optional.",
            },
        },
        # Retained for compatibility with older sessions. Application routing
        # now uses provider-specific readiness and has no combined Continue gate.
        "complete": False,
    }


def ensure_setup_state(
    state: Optional[MutableMapping[str, Any]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Return a valid independent-provider state without reading external config."""
    if isinstance(state, MutableMapping) and int(state.get("version") or 0) == SETUP_VERSION:
        return state  # type: ignore[return-value]
    if isinstance(state, MutableMapping):
        close_setup_resources(state)
    return create_empty_setup_state(now=now)


def create_setup_state(raw: Mapping[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """Compatibility constructor that now supports provider-partial input."""
    now = time.time() if now is None else float(now)
    state = create_empty_setup_state(now=now)
    if raw.get("powerbi") is not None:
        configure_powerbi(state, dict(raw.get("powerbi") or {}), now=now)
    if raw.get("snowflake") is not None:
        configure_snowflake(state, dict(raw.get("snowflake") or {}), now=now)
    if raw.get("claude") is not None:
        configure_claude(state, dict(raw.get("claude") or {}), now=now)
    return state


def _optional_report_ids(value: Any) -> list[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    if isinstance(value, (list, tuple, set)) and not any(_text(item) for item in value):
        return []
    return parse_report_ids(value)


def _scope_mode(workspace_id: str, report_ids: Sequence[str]) -> str:
    if report_ids:
        return "reports"
    if workspace_id:
        return "workspace"
    return "all_accessible"


def configure_powerbi(
    state: MutableMapping[str, Any],
    raw: Mapping[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Configure Power BI independently; only tenant and client are required."""
    now = time.time() if now is None else float(now)
    tenant_id = _uuid(raw.get("tenant_id"), "Power BI Tenant ID")
    client_id = _uuid(raw.get("client_id"), "Power BI Client ID")
    current = dict(((state.get("public") or {}).get("powerbi") or {}))
    workspace_value = raw.get("workspace_id", current.get("workspace_id", ""))
    workspace_id = (
        _uuid(workspace_value, "Power BI Workspace ID") if _text(workspace_value) else ""
    )
    reports_value = raw.get("report_ids", current.get("report_ids") or [])
    report_ids = _optional_report_ids(reports_value)
    if report_ids and not workspace_id:
        raise SetupValidationError(
            "Power BI Workspace ID is required only when Report IDs are supplied."
        )

    identity_changed = (
        _text(current.get("tenant_id")).casefold() != tenant_id.casefold()
        or _text(current.get("client_id")).casefold() != client_id.casefold()
    )
    if identity_changed:
        disconnect_powerbi(state, keep_configuration=True)
        # Optional scope identifiers are tenant-specific and must never carry
        # into a different Tenant ID or Client ID configuration.
        workspace_value = raw.get("workspace_id", "")
        reports_value = raw.get("report_ids", [])
        workspace_id = (
            _uuid(workspace_value, "Power BI Workspace ID")
            if _text(workspace_value)
            else ""
        )
        report_ids = _optional_report_ids(reports_value)

    configured = {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "workspace_id": workspace_id,
        "report_ids": report_ids,
        "scope_mode": _scope_mode(workspace_id, report_ids),
        "validated_scope": {},
        "validated_workspace": {},
        "validated_reports": [],
    }
    state.setdefault("public", {})["powerbi"] = configured
    state.setdefault("status", {})["powerbi"] = {
        "state": "configured",
        "message": "Ready for Microsoft browser sign-in.",
    }
    state["status"]["fabric"] = {
        "state": "configured",
        "message": "Optional Fabric authorization follows Power BI sign-in.",
    }
    state["last_activity"] = now
    state["phase"] = "powerbi_configured"
    return configured


def configure_powerbi_scope(
    state: MutableMapping[str, Any],
    workspace_id: Any = "",
    report_ids: Any = None,
) -> Dict[str, Any]:
    """Apply an optional workspace/report limit without changing PBI identity."""
    public = state.setdefault("public", {}).setdefault("powerbi", {})
    workspace = _uuid(workspace_id, "Power BI Workspace ID") if _text(workspace_id) else ""
    reports = _optional_report_ids(report_ids)
    if reports and not workspace:
        raise SetupValidationError(
            "Power BI Workspace ID is required only when Report IDs are supplied."
        )
    public.update(
        {
            "workspace_id": workspace,
            "report_ids": reports,
            "scope_mode": _scope_mode(workspace, reports),
            "validated_scope": {},
            "validated_workspace": {},
            "validated_reports": [],
        }
    )
    credentials = ((state.get("credentials") or {}).get("powerbi") or {})
    state.setdefault("status", {})["powerbi"] = {
        "state": "authenticated" if _text(credentials.get("access_token")) else "configured",
        "message": (
            "Optional scope changed; validating access."
            if _text(credentials.get("access_token"))
            else "Scope saved. Connect Power BI to validate it."
        ),
    }
    return dict(public)


def configure_snowflake(
    state: MutableMapping[str, Any],
    raw: Mapping[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Configure the Snowflake database connection independently of Power BI."""
    now = time.time() if now is None else float(now)
    account = _snowflake_account(raw.get("account"))
    database = _snowflake_identifier(raw.get("database"), "Snowflake Database")
    warehouse = _snowflake_identifier(raw.get("warehouse"), "Snowflake Warehouse")
    role = _snowflake_identifier(raw.get("role"), "Snowflake Role")
    if role.upper() in _FORBIDDEN_SNOWFLAKE_ROLES:
        raise SetupValidationError(
            "Snowflake Role must be a dedicated read-only role, not a system administrator role."
        )
    if not bool(raw.get("read_only_confirmed")):
        raise SetupValidationError("Confirm that the Snowflake role is dedicated and read-only.")
    user = _required_text(raw.get("user"), "Snowflake SSO Login Name")
    if len(user) > 255 or any(ord(character) < 32 for character in user):
        raise SetupValidationError("Snowflake SSO Login Name has an unsupported format.")

    current = dict(((state.get("public") or {}).get("snowflake") or {}))
    configured = {
        "account": account,
        "database": database,
        "warehouse": warehouse,
        "role": role,
        "user_override": user,
    }
    if any(_text(current.get(key)) != _text(value) for key, value in configured.items()):
        disconnect_snowflake(state, keep_configuration=True)
    state.setdefault("public", {})["snowflake"] = configured
    state.setdefault("status", {})["snowflake"] = {
        "state": "configured",
        "message": "Ready for Snowflake browser SSO.",
    }
    state["last_activity"] = now
    state["phase"] = "snowflake_configured"
    return configured


def configure_claude(
    state: MutableMapping[str, Any],
    raw: Mapping[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Configure optional PowerAI independently of either data provider."""
    now = time.time() if now is None else float(now)
    mode = _text(raw.get("mode") or "disabled").casefold()
    if mode not in {"disabled", "single", "managed_multi"}:
        raise SetupValidationError(
            "PowerAI mode must be Disabled, Single agent, or Managed multi-agent."
        )
    environment_id = ""
    managed_agents: Dict[str, str] = {}
    if mode == "managed_multi":
        environment_id = _claude_identifier(
            raw.get("environment_id"), "Claude Managed Agent Environment ID"
        )
        for field in MANAGED_AGENT_FIELDS:
            managed_agents[field] = _claude_identifier(
                raw.get(field), field.replace("_", " ").title()
            )
    configured = {
        "mode": mode,
        "environment_id": environment_id,
        "managed_agents": managed_agents,
    }
    state.setdefault("public", {})["claude"] = configured
    if mode == "disabled":
        state.setdefault("credentials", {})["claude_api_key"] = ""
        state.setdefault("status", {})["claude"] = {
            "state": "disabled",
            "message": "PowerAI is optional.",
        }
    else:
        state.setdefault("status", {})["claude"] = {
            "state": "configured",
            "message": "Enter the API key for this session.",
        }
    state["last_activity"] = now
    return configured


def set_claude_api_key(state: MutableMapping[str, Any], api_key: Any) -> None:
    """Place the Claude key in the credential compartment after redirect flows."""
    mode = _text(((state.get("public") or {}).get("claude") or {}).get("mode")).casefold()
    if mode == "disabled":
        state.setdefault("credentials", {})["claude_api_key"] = ""
        state.setdefault("status", {})["claude"] = {
            "state": "disabled",
            "message": "PowerAI is optional.",
        }
        return
    key = _required_text(api_key, "Claude API Key")
    if len(key) > 512:
        raise SetupValidationError("Claude API Key has an unsupported format.")
    state.setdefault("credentials", {})["claude_api_key"] = key
    state.setdefault("status", {})["claude"] = {
        "state": "ready",
        "message": "The session-only PowerAI key is ready.",
    }


def _powerbi_public(state: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(((state.get("public") or {}).get("powerbi") or {}))


def _powerbi_credentials(state: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    credentials = state.setdefault("credentials", {})
    return credentials.setdefault("powerbi", {})


def powerbi_runtime_config(state: Mapping[str, Any]) -> Dict[str, Any]:
    powerbi = _powerbi_public(state)
    tenant_id = _uuid(powerbi.get("tenant_id"), "Power BI Tenant ID")
    client_id = _uuid(powerbi.get("client_id"), "Power BI Client ID")
    return {
        "authenticate_mode": "MasterUser",
        "tenant_id": tenant_id,
        "client_id": client_id,
        "authority": f"https://login.microsoftonline.com/{tenant_id}",
        "scope": list(POWER_BI_SCOPES),
        "fabric_scope": list(FABRIC_SCOPES),
    }


def powerbi_scope_fingerprint(state: Mapping[str, Any]) -> str:
    powerbi = _powerbi_public(state)
    scope_text = "|".join(
        [
            _text(powerbi.get("scope_mode") or "all_accessible").casefold(),
            _text(powerbi.get("workspace_id")).casefold(),
            *sorted(_text(value).casefold() for value in powerbi.get("report_ids") or []),
        ]
    )
    return hashlib.sha256(scope_text.encode("utf-8")).hexdigest()


def powerbi_scope_allows(
    state: Mapping[str, Any], workspace_id: Any, report_id: Any = None
) -> bool:
    powerbi = _powerbi_public(state)
    mode = _text(powerbi.get("scope_mode") or "all_accessible").casefold()
    requested_workspace = _text(workspace_id).casefold()
    if not requested_workspace:
        return False
    if mode == "all_accessible":
        return True
    if requested_workspace != _text(powerbi.get("workspace_id")).casefold():
        return False
    if mode == "workspace" or report_id in {None, ""}:
        return True
    allowed_reports = {
        _text(value).casefold() for value in powerbi.get("report_ids") or []
    }
    return _text(report_id).casefold() in allowed_reports


def validated_powerbi_report(
    state: Mapping[str, Any], workspace_id: Any, report_id: Any
) -> Optional[Dict[str, Any]]:
    """Return only the server-validated record for an allowed report pair."""
    if not powerbi_scope_allows(state, workspace_id, report_id):
        return None
    for report in _powerbi_public(state).get("validated_reports") or []:
        if not isinstance(report, Mapping):
            continue
        if (
            _text(report.get("Workspace ID")).casefold() == _text(workspace_id).casefold()
            and _text(report.get("Report ID")).casefold() == _text(report_id).casefold()
        ):
            return dict(report)
    return None


class _BoundedMsalHttpClient:
    """MSAL HTTP adapter with explicit connect/read deadlines and no disk cache."""

    def __init__(self, timeout: int = HTTP_TIMEOUT_SECONDS):
        self.timeout = int(timeout)
        self.session = requests.Session()

    def get(self, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.timeout)
        return self.session.post(url, **kwargs)


def _msal_client(state: MutableMapping[str, Any], msal_module: Any):
    config = powerbi_runtime_config(state)
    cache = msal_module.SerializableTokenCache()
    serialized_cache = _text(_powerbi_credentials(state).get("token_cache"))
    if serialized_cache:
        cache.deserialize(serialized_cache)
    client = msal_module.PublicClientApplication(
        client_id=config["client_id"],
        authority=config["authority"],
        token_cache=cache,
        http_client=_BoundedMsalHttpClient(),
    )
    return client, cache


def build_msal_client(state: MutableMapping[str, Any], msal_module: Any):
    """Recreate the session's in-memory MSAL client from its serialized cache."""
    client, _cache = _msal_client(state, msal_module)
    return client


def begin_entra_authorization(
    state: MutableMapping[str, Any],
    redirect_uri: str,
    audience: str,
    msal_module: Any,
    now: Optional[float] = None,
) -> str:
    """Create a PKCE authorization-code request and retain it only in state."""
    now = time.time() if now is None else float(now)
    audience = _text(audience).casefold()
    if audience not in {"powerbi", "fabric"}:
        raise SetupValidationError("Unsupported Microsoft token audience.")
    if audience == "fabric" and state.get("status", {}).get("powerbi", {}).get("state") != "ready":
        raise SetupValidationError("Complete Power BI sign-in before authorizing Fabric.")

    redirect_uri = normalize_redirect_uri(redirect_uri)
    if not is_loopback_redirect_uri(redirect_uri):
        raise SetupValidationError(
            "This session-only Microsoft public-client flow requires a loopback Streamlit "
            "URL (localhost or 127.0.0.1). A remote HTTPS deployment needs a "
            "tenant-approved web or SPA callback architecture."
        )
    client, cache = _msal_client(state, msal_module)
    scopes = list(POWER_BI_SCOPES if audience == "powerbi" else FABRIC_SCOPES)
    flow = client.initiate_auth_code_flow(
        scopes=scopes,
        redirect_uri=redirect_uri,
        state=secrets.token_urlsafe(32),
        prompt="select_account" if audience == "powerbi" else None,
        login_hint=(
            _text((_powerbi_credentials(state).get("identity") or {}).get("username"))
            if audience == "fabric"
            else None
        ),
    )
    if not isinstance(flow, dict) or not flow.get("auth_uri"):
        message = (flow or {}).get("error_description") or (flow or {}).get("error")
        raise SetupAuthenticationError(redact_text(message or "Microsoft did not return a sign-in URL."))

    flow["_audience"] = audience
    flow["_created_at"] = now
    flow["_expires_at"] = now + AUTH_FLOW_TTL_SECONDS
    flow["_redirect_uri"] = redirect_uri
    powerbi_credentials = _powerbi_credentials(state)
    powerbi_credentials["auth_flow"] = flow
    powerbi_credentials["token_cache"] = cache.serialize()
    state.setdefault("status", {}).setdefault(audience, {})
    state["status"][audience] = {
        "state": "pending",
        "message": "Waiting for browser authorization.",
    }
    state["phase"] = f"{audience}_pending"
    state["last_activity"] = now
    return str(flow["auth_uri"])


def _callback_error_message(callback: Mapping[str, Any]) -> Optional[str]:
    error = _text(callback.get("error")).casefold()
    if not error:
        return None
    if error in {"access_denied", "authorization_declined"}:
        return "Microsoft sign-in was denied. You can retry this step."
    if error in {"temporarily_unavailable", "server_error"}:
        return "Microsoft sign-in is temporarily unavailable. Try again."
    return "Microsoft sign-in did not complete. You can retry this step."


def complete_entra_authorization(
    state: MutableMapping[str, Any],
    callback: Mapping[str, Any],
    msal_module: Any,
    now: Optional[float] = None,
) -> str:
    """Validate an OAuth callback, redeem its code, and retain tokens in memory."""
    now = time.time() if now is None else float(now)
    powerbi_credentials = _powerbi_credentials(state)
    flow = powerbi_credentials.get("auth_flow")
    if not isinstance(flow, dict):
        raise SetupAuthenticationError("The Microsoft sign-in session was not found. Start again.")

    audience = _text(flow.get("_audience")).casefold()
    if float(flow.get("_expires_at") or 0) <= now:
        powerbi_credentials["auth_flow"] = None
        state["status"][audience] = {
            "state": "timed_out",
            "message": "The browser sign-in session timed out. Start it again.",
        }
        raise SetupAuthenticationError("The Microsoft sign-in session timed out. Start it again.")

    if _text(callback.get("state")) != _text(flow.get("state")):
        # Do not destroy the valid pending attempt when an unrelated or forged
        # callback reaches the application.
        raise SetupAuthenticationError("The sign-in response failed state validation.")

    callback_error = _callback_error_message(callback)
    if callback_error:
        powerbi_credentials["auth_flow"] = None
        state["status"][audience] = {"state": "denied", "message": callback_error}
        raise SetupAuthenticationError(callback_error)

    client, cache = _msal_client(state, msal_module)
    try:
        response = client.acquire_token_by_auth_code_flow(flow, dict(callback))
    except ValueError as exc:
        powerbi_credentials["auth_flow"] = None
        state["status"][audience] = {
            "state": "error",
            "message": "The sign-in response failed security validation.",
        }
        raise SetupAuthenticationError("The sign-in response failed security validation.") from exc
    except Exception as exc:
        powerbi_credentials["auth_flow"] = None
        state["status"][audience] = {
            "state": "error",
            "message": "Microsoft token exchange could not be completed. Retry sign-in.",
        }
        raise SetupAuthenticationError(
            "Microsoft token exchange could not be completed. Retry sign-in."
        ) from exc

    if not response or not response.get("access_token"):
        powerbi_credentials["auth_flow"] = None
        raw_error = (response or {}).get("error")
        mapped_error = _callback_error_message({"error": raw_error})
        message = mapped_error or "Microsoft did not return an access token. Retry sign-in."
        state["status"][audience] = {"state": "error", "message": message}
        raise SetupAuthenticationError(message)

    claims = dict(response.get("id_token_claims") or {})
    expected_tenant = powerbi_runtime_config(state)["tenant_id"].casefold()
    token_tenant = _text(claims.get("tid")).casefold()
    principal_id = _text(claims.get("oid"))
    if token_tenant != expected_tenant or not principal_id:
        powerbi_credentials["auth_flow"] = None
        state["status"][audience] = {
            "state": "error",
            "message": "Microsoft identity validation failed for the configured tenant.",
        }
        raise SetupAuthenticationError(
            "Microsoft identity validation failed for the configured tenant."
        )

    username = _text(
        claims.get("preferred_username") or claims.get("upn") or claims.get("email")
    )
    account_candidates = (
        client.get_accounts(username=username) if username else client.get_accounts()
    )
    matching_account = next(
        (
            account
            for account in (account_candidates or [])
            if _text(account.get("tenant_id")).casefold() in {"", expected_tenant}
            and (
                _text(account.get("local_account_id")) == principal_id
                or _text((account.get("id_token_claims") or {}).get("oid")) == principal_id
            )
        ),
        None,
    )
    home_account_id = _text((matching_account or {}).get("home_account_id"))

    expires_at = now + max(0, int(response.get("expires_in") or 3599))
    if audience == "powerbi":
        powerbi_credentials["access_token"] = response["access_token"]
        powerbi_credentials["expires_at"] = expires_at
        powerbi_credentials["identity"] = {
            "tenant_id": token_tenant,
            "principal_id": principal_id,
            "home_account_id": home_account_id,
            "username": username,
            "display_name": _text(claims.get("name")),
        }
    else:
        powerbi_identity = dict(powerbi_credentials.get("identity") or {})
        if (
            principal_id != _text(powerbi_identity.get("principal_id"))
            or token_tenant != _text(powerbi_identity.get("tenant_id")).casefold()
        ):
            powerbi_credentials["auth_flow"] = None
            state["status"][audience] = {
                "state": "error",
                "message": "Fabric authorization must use the same Microsoft identity as Power BI.",
            }
            raise SetupAuthenticationError(
                "Fabric authorization must use the same Microsoft identity as Power BI."
            )
        powerbi_credentials["fabric_token"] = response["access_token"]
        powerbi_credentials["fabric_expires_at"] = expires_at

    powerbi_credentials["token_cache"] = cache.serialize()
    powerbi_credentials["auth_flow"] = None
    state["status"][audience] = {
        "state": "authenticated",
        "message": "Browser authorization completed; validating access.",
    }
    state["last_activity"] = now
    return audience


def authenticate_powerbi_interactive(
    state: MutableMapping[str, Any],
    msal_module: Any,
    now: Optional[float] = None,
) -> None:
    """Run local external-browser Power BI login without replacing the session."""
    now = time.time() if now is None else float(now)
    config = powerbi_runtime_config(state)
    # Every interactive attempt starts clean; a denied retry must not leave a
    # prior bearer token or refresh material reachable in the session.
    disconnect_powerbi(state, keep_configuration=True)
    client, cache = _msal_client(state, msal_module)
    state.setdefault("status", {})["powerbi"] = {
        "state": "pending",
        "message": "Waiting for Microsoft browser sign-in.",
    }
    try:
        response = client.acquire_token_interactive(
            scopes=list(POWER_BI_SCOPES),
            prompt="select_account",
            timeout=120,
        )
    except Exception as exc:
        state["status"]["powerbi"] = {
            "state": "timed_out" if "timeout" in type(exc).__name__.casefold() else "error",
            "message": "Microsoft browser sign-in did not complete. Retry the connection.",
        }
        raise SetupAuthenticationError(
            "Microsoft browser sign-in did not complete. Retry the connection."
        ) from exc

    if not isinstance(response, Mapping) or not _text(response.get("access_token")):
        raw_error = _text((response or {}).get("error")).casefold() if isinstance(response, Mapping) else ""
        denied = raw_error in {"access_denied", "authorization_declined", "user_cancelled"}
        message = (
            "Microsoft sign-in was denied or cancelled. You can retry this connection."
            if denied
            else "Microsoft did not return an access token. Retry sign-in."
        )
        state["status"]["powerbi"] = {
            "state": "denied" if denied else "error",
            "message": message,
        }
        raise SetupAuthenticationError(message)

    claims = dict(response.get("id_token_claims") or {})
    expected_tenant = config["tenant_id"].casefold()
    token_tenant = _text(claims.get("tid")).casefold()
    principal_id = _text(claims.get("oid"))
    if token_tenant != expected_tenant or not principal_id:
        state["status"]["powerbi"] = {
            "state": "error",
            "message": "Microsoft identity validation failed for the configured tenant.",
        }
        raise SetupAuthenticationError(
            "Microsoft identity validation failed for the configured tenant."
        )

    username = _text(
        claims.get("preferred_username") or claims.get("upn") or claims.get("email")
    )
    matching_account = next(
        (
            account
            for account in (client.get_accounts(username=username) if username else client.get_accounts())
            if _text(account.get("tenant_id")).casefold() in {"", expected_tenant}
            and (
                _text(account.get("local_account_id")) == principal_id
                or _text((account.get("id_token_claims") or {}).get("oid")) == principal_id
            )
        ),
        None,
    )
    credentials = _powerbi_credentials(state)
    credentials.update(
        {
            "access_token": _text(response.get("access_token")),
            "expires_at": now + max(0, int(response.get("expires_in") or 3599)),
            "token_cache": cache.serialize(),
            "auth_flow": None,
            "identity": {
                "tenant_id": token_tenant,
                "principal_id": principal_id,
                "home_account_id": _text((matching_account or {}).get("home_account_id")),
                "username": username,
                "display_name": _text(claims.get("name")),
            },
        }
    )
    state["status"]["powerbi"] = {
        "state": "authenticated",
        "message": "Microsoft sign-in completed; validating Power BI access.",
    }
    state["last_activity"] = now


def _safe_json(response: Any) -> Dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _raise_for_validation_status(status_code: int, service: str, resource: str) -> None:
    if status_code == 401:
        raise SetupAuthenticationError(f"{service} authorization expired. Sign in again.")
    if status_code == 403:
        raise SetupAuthenticationError(f"{service} permission was denied for the configured {resource}.")
    if status_code == 404:
        raise SetupAuthenticationError(
            f"The configured {resource} is unavailable or is not authorized for this identity."
        )
    if status_code == 429:
        raise SetupAuthenticationError(f"{service} is rate limiting validation. Retry shortly.")
    raise SetupAuthenticationError(f"{service} validation failed with HTTP {int(status_code)}.")


def _get(http_client: Any, url: str, headers: Mapping[str, str], timeout: int):
    try:
        return http_client.get(url, headers=dict(headers), timeout=timeout)
    except Exception as exc:
        name = type(exc).__name__.casefold()
        if "timeout" in name:
            raise SetupAuthenticationError("The connection validation request timed out.") from exc
        raise SetupAuthenticationError("The connection validation request could not reach the service.") from exc


def _post(http_client: Any, url: str, headers: Mapping[str, str], timeout: int):
    try:
        return http_client.post(url, headers=dict(headers), timeout=timeout)
    except Exception as exc:
        name = type(exc).__name__.casefold()
        if "timeout" in name:
            raise SetupAuthenticationError("The connection validation request timed out.") from exc
        raise SetupAuthenticationError("The connection validation request could not reach the service.") from exc


def validate_powerbi_targets(
    state: MutableMapping[str, Any],
    http_client: Any,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> list[Dict[str, Any]]:
    """Validate Power BI access and any optional workspace/report restriction."""
    public = _powerbi_public(state)
    credentials = _powerbi_credentials(state)
    token = _text(credentials.get("access_token"))
    if not token:
        raise SetupAuthenticationError("Complete Power BI sign-in before validation.")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    workspace_id = _text(public.get("workspace_id"))
    scope_mode = _text(public.get("scope_mode") or "all_accessible").casefold()

    if scope_mode == "all_accessible":
        probe_response = _get(
            http_client,
            "https://api.powerbi.com/v1.0/myorg/groups?$top=1",
            headers,
            timeout,
        )
        if int(probe_response.status_code) != 200:
            _raise_for_validation_status(
                int(probe_response.status_code), "Power BI", "workspace inventory"
            )
        probe_payload = _safe_json(probe_response)
        if not isinstance(probe_payload.get("value"), list):
            raise SetupAuthenticationError(
                "Power BI returned an invalid workspace inventory response."
            )
        state["public"]["powerbi"]["validated_workspace"] = {}
        state["public"]["powerbi"]["validated_reports"] = []
        state["public"]["powerbi"]["validated_scope"] = {
            "mode": "all_accessible",
            "fingerprint": powerbi_scope_fingerprint(state),
        }
        state["status"]["powerbi"] = {
            "state": "ready",
            "message": "Connected. Accessible workspace reports will load dynamically.",
        }
        state["phase"] = "powerbi_ready"
        return []

    workspace_response = _get(
        http_client,
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}",
        headers,
        timeout,
    )
    if int(workspace_response.status_code) != 200:
        _raise_for_validation_status(int(workspace_response.status_code), "Power BI", "workspace")
    workspace_payload = _safe_json(workspace_response)
    if _text(workspace_payload.get("id")).casefold() != workspace_id.casefold():
        raise SetupAuthenticationError("Power BI returned an invalid workspace validation response.")

    validated_reports: list[Dict[str, Any]] = []
    for report_id in public.get("report_ids") or []:
        report_response = _get(
            http_client,
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}",
            headers,
            timeout,
        )
        if int(report_response.status_code) != 200:
            _raise_for_validation_status(int(report_response.status_code), "Power BI", "report")
        report_payload = _safe_json(report_response)
        returned_report_id = _text(report_payload.get("id"))
        if returned_report_id.casefold() != _text(report_id).casefold():
            raise SetupAuthenticationError("Power BI returned an invalid report validation response.")
        validated_reports.append(
            {
                "Workspace Name": _text(workspace_payload.get("name")),
                "Workspace ID": workspace_id,
                "Report Name": _text(report_payload.get("name")),
                "Report ID": report_id,
                "Dataset ID": _text(report_payload.get("datasetId")),
                "Dataset Workspace ID": _text(report_payload.get("datasetWorkspaceId")),
                "Report Type": _text(report_payload.get("reportType")),
                "Report Format": _text(report_payload.get("format")),
                "Embed URL": _text(report_payload.get("embedUrl")),
            }
        )

    state["public"]["powerbi"]["validated_workspace"] = {
        "id": workspace_id,
        "name": _text(workspace_payload.get("name")),
    }
    state["public"]["powerbi"]["validated_reports"] = validated_reports
    state["public"]["powerbi"]["validated_scope"] = {
        "mode": scope_mode,
        "fingerprint": powerbi_scope_fingerprint(state),
    }
    state["status"]["powerbi"] = {
        "state": "ready",
        "message": (
            f"Connected to workspace {workspace_payload.get('name') or workspace_id}."
            if scope_mode == "workspace"
            else f"Validated {len(validated_reports)} configured report(s)."
        ),
    }
    state["phase"] = "powerbi_ready"
    return validated_reports


def validate_fabric_targets(
    state: MutableMapping[str, Any],
    http_client: Any,
    timeout: int = HTTP_TIMEOUT_SECONDS,
) -> None:
    """Verify the optional Fabric grant against the actual definition operation."""
    public = _powerbi_public(state)
    credentials = _powerbi_credentials(state)
    token = _text(credentials.get("fabric_token"))
    if not token:
        raise SetupAuthenticationError("Complete Fabric authorization before validation.")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    workspace_id = public["workspace_id"]
    report_ids = list(public.get("report_ids") or [])
    if not workspace_id or not report_ids:
        state["status"]["fabric"] = {
            "state": "authenticated",
            "message": (
                "Fabric is authorized. Report-definition access will be validated "
                "when a dynamically discovered report is selected."
            ),
        }
        return
    for report_id in report_ids:
        response = _post(
            http_client,
            (
                "https://api.fabric.microsoft.com/v1/workspaces/"
                f"{workspace_id}/reports/{report_id}/getDefinition"
            ),
            headers,
            timeout,
        )
        if int(response.status_code) not in {200, 202}:
            _raise_for_validation_status(int(response.status_code), "Fabric", "report")
    state["status"]["fabric"] = {
        "state": "ready",
        "message": "Validated Fabric report-definition access.",
    }
    state["phase"] = "fabric_ready"


def effective_snowflake_user(state: Mapping[str, Any]) -> str:
    snowflake = dict(((state.get("public") or {}).get("snowflake") or {}))
    override = _text(snowflake.get("user_override"))
    if override:
        return override
    identity = dict(((state.get("credentials") or {}).get("powerbi") or {}).get("identity") or {})
    return _text(identity.get("username"))


def snowflake_connection_settings(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the only allowed Snowflake settings; no password can enter this map."""
    snowflake = dict(((state.get("public") or {}).get("snowflake") or {}))
    user = effective_snowflake_user(state)
    if not user:
        raise SetupValidationError(
            "Snowflake SSO Login Name is required because it could not be derived from Microsoft sign-in."
        )
    return {
        "enabled": True,
        "account": _snowflake_account(snowflake.get("account")),
        "user": user,
        "database": _snowflake_identifier(snowflake.get("database"), "Snowflake Database"),
        "warehouse": _snowflake_identifier(snowflake.get("warehouse"), "Snowflake Warehouse"),
        "role": _snowflake_identifier(snowflake.get("role"), "Snowflake Role"),
        "authenticator": "externalbrowser",
        "direction": "UPSTREAM",
        "default_object_domain": "VIEW",
        # Stored-procedure execution is deliberately disabled unless a future
        # administrator-approved, read-only allowlist is added to runtime setup.
        "column_lineage_procedure": "",
        "column_lineage_max_depth": 50,
        "max_depth": 20,
        "statement_timeout_seconds": 120,
    }


def build_snowflake_connect_kwargs(
    state: Mapping[str, Any], auth_class: Any = None
) -> Dict[str, Any]:
    settings = snowflake_connection_settings(state)
    kwargs: Dict[str, Any] = {
        "account": settings["account"],
        "user": settings["user"],
        "database": settings["database"],
        "warehouse": settings["warehouse"],
        "role": settings["role"],
        "authenticator": "externalbrowser",
        "application": "PBI_Lineage_Explorer",
        "login_timeout": 120,
        "network_timeout": 30,
        "socket_timeout": 30,
        "external_browser_timeout": 120,
        "validate_default_parameters": True,
        "client_store_temporary_credential": False,
        "client_request_mfa_token": False,
        "client_session_keep_alive": False,
        "session_parameters": {
            "CLIENT_STORE_TEMPORARY_CREDENTIAL": False,
            "CLIENT_REQUEST_MFA_TOKEN": False,
            "QUERY_TAG": "PBI_LINEAGE_EXPLORER",
        },
    }
    if auth_class is not None:
        kwargs["auth_class"] = auth_class
    return kwargs


def validate_snowflake_connection(conn: Any, state: Mapping[str, Any]) -> Dict[str, str]:
    """Validate identity and effective read context without touching business rows."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_DATABASE()"
        )
        values = cursor.fetchone() or ()
    finally:
        cursor.close()
    if len(values) < 5:
        raise SetupAuthenticationError("Snowflake did not return the active session context.")

    identity = {
        "account": _text(values[0]),
        "user": _text(values[1]),
        "role": _text(values[2]),
        "warehouse": _text(values[3]),
        "database": _text(values[4]),
    }
    expected = snowflake_connection_settings(state)
    for key in ("role", "warehouse", "database"):
        if identity[key].casefold() != _text(expected[key]).casefold():
            raise SetupAuthenticationError(
                f"Snowflake connected, but the active {key} does not match the requested setup value."
            )
    return identity


class _FailClosedBrowserLauncher:
    """Prevent the connector from falling back to blocking terminal input."""

    @staticmethod
    def open_new(url: str) -> bool:
        if not webbrowser.open_new(url):
            raise SetupAuthenticationError(
                "Snowflake could not open a browser on the Streamlit host."
            )
        return True


def _bounded_receive_saml_token(self: Any, conn: Any, server_socket: Any) -> None:
    """Connector-compatible browser callback receiver with a hard deadline."""
    deadline = time.monotonic() + 120
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Snowflake browser authentication timed out.")
        readable, _writable, _errors = select.select(
            [server_socket], [], [], remaining
        )
        if not readable:
            raise TimeoutError("Snowflake browser authentication timed out.")

        client_socket = None
        try:
            client_socket, _address = server_socket.accept()
            client_socket.settimeout(max(0.1, deadline - time.monotonic()))
            raw_data = bytearray()
            attempts = 0
            while not raw_data and attempts < 15:
                attempts += 1
                try:
                    raw_data = client_socket.recv(16384)
                except socket.timeout as exc:
                    raise TimeoutError("Snowflake browser authentication timed out.") from exc
                if not raw_data and time.monotonic() >= deadline:
                    raise TimeoutError("Snowflake browser authentication timed out.")
            if not raw_data:
                raise SetupAuthenticationError(
                    "Snowflake browser authentication returned an empty callback."
                )
            data = raw_data.decode("utf-8").split("\r\n")
            if self._process_options(data, client_socket):
                continue
            self._process_receive_saml_token(conn, data, client_socket)
            return
        finally:
            if client_socket is not None:
                try:
                    client_socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    client_socket.close()
                except Exception:
                    pass


def connect_snowflake_external_browser(
    state: MutableMapping[str, Any], connector_module: Any = None
) -> Any:
    """Open Snowflake browser SSO with disk token caching explicitly disabled."""
    if connector_module is None:
        import snowflake.connector as connector_module  # type: ignore
        from snowflake.connector.auth.webbrowser import AuthByWebBrowser  # type: ignore
    else:
        AuthByWebBrowser = connector_module.AuthByWebBrowser

    auth_class = AuthByWebBrowser(
        application="PBI_Lineage_Explorer",
        webbrowser_pkg=_FailClosedBrowserLauncher,
        timeout=120,
    )
    # Connector 4.x auto-enables SSO credential caching on Windows for the
    # normal externalbrowser branch. Supplying the first-party auth instance
    # takes the non-caching branch, and consent_cache_id_token=False prevents
    # an ID token from being written to Credential Manager or a cache file.
    auth_class.consent_cache_id_token = False
    auth_class._receive_saml_token = types.MethodType(  # noqa: SLF001 - connector hook
        _bounded_receive_saml_token,
        auth_class,
    )
    kwargs = build_snowflake_connect_kwargs(state, auth_class=auth_class)

    state["status"]["snowflake"] = {
        "state": "pending",
        "message": "Waiting for Snowflake browser SSO.",
    }
    connection = None
    try:
        # The connector prints its one-time SSO URL. The browser opens directly,
        # so suppress that URL rather than leaking it into service logs.
        with contextlib.redirect_stdout(io.StringIO()):
            connection = connector_module.connect(**kwargs)
        identity = validate_snowflake_connection(connection, state)
    except Exception as exc:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        message = redact_text(str(exc), secret_values(state))
        if "timeout" in message.casefold() or "timed out" in message.casefold():
            safe_message = "Snowflake browser authentication timed out. Retry the connection."
            status = "timed_out"
        elif "denied" in message.casefold() or "cancel" in message.casefold():
            safe_message = "Snowflake browser authentication was denied or cancelled."
            status = "denied"
        elif isinstance(exc, SetupAuthenticationError):
            safe_message = message
            status = "error"
        else:
            safe_message = "Snowflake SSO or connection validation failed. Verify access and retry."
            status = "error"
        state["status"]["snowflake"] = {"state": status, "message": safe_message}
        raise SetupAuthenticationError(safe_message) from exc

    state["credentials"]["snowflake"] = {
        "connection": connection,
        "identity": identity,
    }
    state["status"]["snowflake"] = {
        "state": "ready",
        "message": "Validated the Snowflake SSO session and active context.",
    }
    state["phase"] = "snowflake_ready"
    return connection


def claude_runtime_settings(
    state: Mapping[str, Any], defaults: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Return PowerAI settings sourced only from this runtime session."""
    settings = copy.deepcopy(dict(defaults or {}))
    public = dict(((state.get("public") or {}).get("claude") or {}))
    mode = _text(public.get("mode") or "disabled").casefold()
    api_key = _text((state.get("credentials") or {}).get("claude_api_key"))
    settings.update(
        {
            "enabled": mode != "disabled",
            "api_key": api_key,
            "orchestration_mode": "multi" if mode == "managed_multi" else "single",
            "agent_runtime": "managed" if mode == "managed_multi" else "direct",
            "_session_only_config": True,
        }
    )
    managed = dict(settings.get("managed_agents") or {})
    managed.update(
        {
            "enabled": mode == "managed_multi",
            "environment_id": _text(public.get("environment_id")),
        }
    )
    managed.update(dict(public.get("managed_agents") or {}))
    settings["managed_agents"] = managed
    return settings


def powerbi_is_ready(
    state: Optional[Mapping[str, Any]], now: Optional[float] = None
) -> bool:
    if not isinstance(state, Mapping) or int(state.get("version") or 0) != SETUP_VERSION:
        return False
    now = time.time() if now is None else float(now)
    status = dict(state.get("status") or {})
    credentials = dict(state.get("credentials") or {})
    powerbi_credentials = dict(credentials.get("powerbi") or {})
    powerbi_public = dict(((state.get("public") or {}).get("powerbi") or {}))
    validated_scope = dict(powerbi_public.get("validated_scope") or {})
    if _text(validated_scope.get("fingerprint")) != powerbi_scope_fingerprint(state):
        return False
    if _text(validated_scope.get("mode")) != _text(
        powerbi_public.get("scope_mode") or "all_accessible"
    ):
        return False
    validated_report_ids = {
        _text(report.get("Report ID")).casefold()
        for report in powerbi_public.get("validated_reports") or []
        if isinstance(report, Mapping)
    }
    configured_report_ids = {
        _text(report_id).casefold()
        for report_id in powerbi_public.get("report_ids") or []
    }
    if _text(powerbi_public.get("scope_mode")) == "reports":
        if not configured_report_ids or validated_report_ids != configured_report_ids:
            return False
    return (
        _text((status.get("powerbi") or {}).get("state")) == "ready"
        and bool(_text(powerbi_credentials.get("access_token")))
        and float(powerbi_credentials.get("expires_at") or 0) > now + 30
    )


def snowflake_is_ready(state: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(state, Mapping) or int(state.get("version") or 0) != SETUP_VERSION:
        return False
    return (
        _text(((state.get("status") or {}).get("snowflake") or {}).get("state"))
        == "ready"
        and (((state.get("credentials") or {}).get("snowflake") or {}).get("connection"))
        is not None
    )


def claude_is_ready(state: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(state, Mapping):
        return False
    mode = _text(((state.get("public") or {}).get("claude") or {}).get("mode"))
    if mode == "disabled":
        return False
    return (
        _text(((state.get("status") or {}).get("claude") or {}).get("state")) == "ready"
        and bool(_text((state.get("credentials") or {}).get("claude_api_key")))
    )


def setup_is_ready(
    state: Optional[Mapping[str, Any]], now: Optional[float] = None
) -> bool:
    """Compatibility composite; routing now uses provider-specific readiness."""
    claude_mode = _text(((state or {}).get("public") or {}).get("claude", {}).get("mode"))
    return (
        powerbi_is_ready(state, now=now)
        and snowflake_is_ready(state)
        and (claude_mode == "disabled" or claude_is_ready(state))
    )


def disconnect_powerbi(
    state: Optional[MutableMapping[str, Any]], keep_configuration: bool = True
) -> None:
    """Clear only Microsoft credentials and validation; preserve Snowflake/Claude."""
    if not isinstance(state, MutableMapping):
        return
    credentials = state.setdefault("credentials", {}).setdefault("powerbi", {})
    for key, empty_value in (
        ("access_token", ""),
        ("expires_at", 0.0),
        ("fabric_token", ""),
        ("fabric_expires_at", 0.0),
        ("token_cache", ""),
        ("auth_flow", None),
        ("identity", {}),
    ):
        credentials[key] = empty_value
    public = state.setdefault("public", {}).setdefault("powerbi", {})
    public["validated_scope"] = {}
    public["validated_workspace"] = {}
    public["validated_reports"] = []
    if not keep_configuration:
        public.update(
            {
                "tenant_id": "",
                "client_id": "",
                "workspace_id": "",
                "report_ids": [],
                "scope_mode": "all_accessible",
            }
        )
    configured = bool(_text(public.get("tenant_id")) and _text(public.get("client_id")))
    state.setdefault("status", {})["powerbi"] = {
        "state": "configured" if configured else "not_configured",
        "message": (
            "Power BI is disconnected. Connect again when ready."
            if configured
            else "Enter Tenant ID and Client ID to connect."
        ),
    }
    state["status"]["fabric"] = {
        "state": "configured" if configured else "not_configured",
        "message": "Optional Fabric access follows Power BI sign-in.",
    }
    state["complete"] = False


def disconnect_snowflake(
    state: Optional[MutableMapping[str, Any]], keep_configuration: bool = True
) -> None:
    """Close only the Snowflake connection and preserve Power BI/Claude."""
    if not isinstance(state, MutableMapping):
        return
    credentials = state.setdefault("credentials", {}).setdefault("snowflake", {})
    connection = credentials.get("connection")
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
    credentials["connection"] = None
    credentials["identity"] = {}
    public = state.setdefault("public", {}).setdefault("snowflake", {})
    if not keep_configuration:
        public.update(
            {"account": "", "database": "", "warehouse": "", "role": "", "user_override": ""}
        )
    configured = all(
        _text(public.get(key))
        for key in ("account", "database", "warehouse", "role", "user_override")
    )
    state.setdefault("status", {})["snowflake"] = {
        "state": "configured" if configured else "not_configured",
        "message": (
            "Snowflake is disconnected. Connect again when ready."
            if configured
            else "Enter database connection parameters to connect."
        ),
    }
    state["complete"] = False


def reconcile_setup_state(
    state: Optional[MutableMapping[str, Any]], now: Optional[float] = None
) -> None:
    """Fail closed when credential objects no longer match their ready status."""
    if not isinstance(state, MutableMapping):
        return
    now = time.time() if now is None else float(now)
    credentials = state.setdefault("credentials", {})
    powerbi = credentials.setdefault("powerbi", {})
    status = state.setdefault("status", {})
    powerbi_ready = _text((status.get("powerbi") or {}).get("state")) == "ready"
    token_missing_or_expired = (
        not _text(powerbi.get("access_token"))
        or float(powerbi.get("expires_at") or 0) <= now + 30
    )
    has_powerbi_material = bool(
        _text(powerbi.get("access_token")) or _text(powerbi.get("token_cache"))
    )
    if token_missing_or_expired and (powerbi_ready or has_powerbi_material):
        disconnect_powerbi(state, keep_configuration=True)
        status["powerbi"] = {
            "state": "configured",
            "message": "The Microsoft session expired. Sign in again.",
        }
        status["fabric"] = {
            "state": "configured",
            "message": "Optional Fabric authorization follows the Microsoft session.",
        }
        state["phase"] = "powerbi_reauthentication_required"

    snowflake = credentials.setdefault("snowflake", {})
    if (
        _text((status.get("snowflake") or {}).get("state")) == "ready"
        and snowflake.get("connection") is None
    ):
        status["snowflake"] = {
            "state": "configured",
            "message": "The Snowflake session is unavailable. Reconnect to continue.",
        }
        state["complete"] = False

    if (
        _text((status.get("claude") or {}).get("state")) == "ready"
        and not _text(credentials.get("claude_api_key"))
    ):
        status["claude"] = {
            "state": "configured",
            "message": "Enter the session-only Claude API key.",
        }
        state["complete"] = False


def legacy_auth_bundle(
    state: MutableMapping[str, Any], msal_module: Any, now: Optional[float] = None
) -> Dict[str, Any]:
    """Adapt validated session tokens to the existing read-only app contract."""
    now = time.time() if now is None else float(now)
    if not powerbi_is_ready(state, now=now):
        raise SetupValidationError("Power BI must be connected before using Power BI features.")
    credentials = _powerbi_credentials(state)
    if float(credentials.get("expires_at") or 0) <= now:
        raise SetupAuthenticationError("The Power BI session expired during setup. Sign in again.")
    client = build_msal_client(state, msal_module)
    token = _text(credentials.get("access_token"))
    return {
        "auth_mode": "MasterUserOnly",
        "mu": token,
        "sp": token,
        "spa": token,
        "fabric": _text(credentials.get("fabric_token")),
        "fabric_error": None,
        "fabric_expires_at": float(credentials.get("fabric_expires_at") or 0),
        "expires_at": float(credentials.get("expires_at") or 0),
        "login_time": now,
        "clientapp_mu": client,
        "clientapp_sp": None,
        "clientapp_spa": None,
    }


def close_setup_resources(state: Optional[MutableMapping[str, Any]]) -> None:
    """Best-effort close of live connections before session data is discarded."""
    if not isinstance(state, MutableMapping):
        return
    snowflake_credentials = (state.get("credentials") or {}).get("snowflake") or {}
    connection = snowflake_credentials.get("connection")
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
        snowflake_credentials["connection"] = None


def secret_values(state: Optional[Mapping[str, Any]]) -> Sequence[str]:
    if not isinstance(state, Mapping):
        return ()
    credentials = dict(state.get("credentials") or {})
    powerbi = dict(credentials.get("powerbi") or {})
    values = [
        _text(credentials.get("claude_api_key")),
        _text(powerbi.get("access_token")),
        _text(powerbi.get("fabric_token")),
        _text(powerbi.get("token_cache")),
    ]
    flow = powerbi.get("auth_flow")
    if isinstance(flow, Mapping):
        for key, value in flow.items():
            normalized_key = _text(key).casefold()
            if any(marker in normalized_key for marker in ("state", "verifier", "nonce", "secret")):
                values.append(_text(value))
    return tuple(value for value in values if value)


def redact_text(value: Any, protected_values: Sequence[str] = ()) -> str:
    """Redact known credentials and common bearer/API-key shapes from errors."""
    text = _text(value)
    for protected in sorted((_text(item) for item in protected_values), key=len, reverse=True):
        if protected:
            text = text.replace(protected, "[REDACTED]")
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    text = _ANTHROPIC_KEY_PATTERN.sub("[REDACTED_API_KEY]", text)
    return text[:1000]


def pending_state_snapshot(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy only data needed to finish the current OAuth round trip.

    Claude secrets, issued bearer tokens, and live Snowflake objects never enter
    the short-lived process-memory bridge.
    """
    source = dict(state)
    credentials = dict(source.get("credentials") or {})
    credentials["claude_api_key"] = ""
    powerbi_credentials = dict(credentials.get("powerbi") or {})
    powerbi_credentials["access_token"] = ""
    powerbi_credentials["expires_at"] = 0.0
    powerbi_credentials["fabric_token"] = ""
    powerbi_credentials["fabric_expires_at"] = 0.0
    powerbi_credentials["token_cache"] = ""
    powerbi_credentials["identity"] = {}
    credentials["powerbi"] = powerbi_credentials
    snowflake_credentials = dict(credentials.get("snowflake") or {})
    snowflake_credentials["connection"] = None
    snowflake_credentials["identity"] = {}
    credentials["snowflake"] = snowflake_credentials
    source["credentials"] = credentials
    status = copy.deepcopy(dict(source.get("status") or {}))
    status["snowflake"] = {
        "state": "configured",
        "message": "Ready for browser SSO after Microsoft authorization.",
    }
    claude_mode = _text(((source.get("public") or {}).get("claude") or {}).get("mode"))
    if claude_mode != "disabled":
        status["claude"] = {
            "state": "configured",
            "message": "Enter the session API key after browser authorization.",
        }
    source["status"] = status
    source["complete"] = False
    return copy.deepcopy(source)
