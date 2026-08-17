"""Direct raw Power BI, Fabric, and XMLA API probes for local testing.

Every REST API function below returns the same raw envelope:

{
    "api": {"method": "...", "url": "...", "params": {...}, "body": ...},
    "status_code": 200,
    "reason": "OK",
    "headers": {...},
    "text": "... raw response text when the service returns text/json ...",
    "json": {... parsed JSON when possible ...},
    "content_base64": "... only when include_binary_body=True ...",
    "error": ""
}

The functions do not reshape Power BI/Fabric responses into app tables. They are
for comparing exactly what the service returned with what the app later renders.
"""

from __future__ import annotations

import base64
import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import msal
import requests

from tls_trust import configure_tls_trust, format_request_exception
from utils import Utils


TLS_TRUST_CONFIG = configure_tls_trust()

POWER_BI_API_BASE = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
POWER_BI_DEFAULT_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_DEFAULT_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_REPORT_SCOPE = "https://api.fabric.microsoft.com/Report.ReadWrite.All"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _clean_token(token_or_header: str) -> str:
    return re.sub(r"^Bearer\s+", "", str(token_or_header or "").strip(), flags=re.IGNORECASE)


def _auth_header(access_token: str) -> Dict[str, str]:
    token = _clean_token(access_token)
    if not token:
        raise ValueError("A Power BI or Fabric bearer token is required.")
    return {"Authorization": f"Bearer {token}"}


def _is_text_response(content_type: str) -> bool:
    value = str(content_type or "").casefold()
    return "json" in value or value.startswith("text/") or "xml" in value or "html" in value


def _redact_error(error: BaseException, protected_values: Iterable[str]) -> str:
    message = format_request_exception(error, TLS_TRUST_CONFIG)
    for value in protected_values:
        secret = str(value or "")
        if secret:
            message = message.replace(secret, "<redacted>")
    return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer <redacted>", message)


def _response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _raw_response(
    response: requests.Response,
    api: Mapping[str, Any],
    *,
    include_binary_body: bool = False,
    max_text_chars: Optional[int] = None,
) -> Dict[str, Any]:
    content_type = response.headers.get("Content-Type", "")
    body = response.content or b""
    text = response.text if _is_text_response(content_type) else None
    text_truncated = False
    if text is not None and max_text_chars is not None and len(text) > max_text_chars:
        text = text[:max_text_chars]
        text_truncated = True

    return _json_safe(
        {
            "api": dict(api),
            "status_code": response.status_code,
            "reason": response.reason,
            "url": response.url,
            "ok": response.ok,
            "elapsed_seconds": response.elapsed.total_seconds() if response.elapsed else None,
            "headers": dict(response.headers),
            "content_type": content_type,
            "content_length_bytes": len(body),
            "text": text,
            "text_truncated": text_truncated,
            "json": _response_json(response),
            "content_base64": (
                base64.b64encode(body).decode("ascii")
                if include_binary_body
                else None
            ),
            "error": "",
        }
    )


def _request(
    method: str,
    url: str,
    access_token: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    body: Optional[Any] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: int | Tuple[int, int] = 60,
    include_binary_body: bool = False,
    max_text_chars: Optional[int] = None,
    allow_redirects: bool = True,
) -> Dict[str, Any]:
    request_headers = _auth_header(access_token)
    if headers:
        for key, value in headers.items():
            if str(key).casefold() != "authorization":
                request_headers[str(key)] = str(value)
    if body is not None:
        request_headers.setdefault("Accept", "application/json")
        request_headers.setdefault("Content-Type", "application/json")

    api = {
        "method": method.upper(),
        "url": url,
        "params": dict(params or {}),
        "body": body,
        "request_headers": {
            key: ("<redacted>" if key.casefold() == "authorization" else value)
            for key, value in request_headers.items()
        },
    }

    try:
        response = requests.request(
            method.upper(),
            url,
            headers=request_headers,
            params=params,
            json=body,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )
    except requests.RequestException as error:
        return _json_safe(
            {
                "api": api,
                "status_code": None,
                "reason": "",
                "url": url,
                "ok": False,
                "elapsed_seconds": None,
                "headers": {},
                "content_type": "",
                "content_length_bytes": 0,
                "text": None,
                "text_truncated": False,
                "json": None,
                "content_base64": None,
                "error": _redact_error(error, [access_token]),
            }
        )

    return _raw_response(
        response,
        api,
        include_binary_body=include_binary_body,
        max_text_chars=max_text_chars,
    )


def _powerbi_url(path: str) -> str:
    return f"{POWER_BI_API_BASE}/{path.lstrip('/')}"


def _fabric_url(path: str) -> str:
    return f"{FABRIC_API_BASE}/{path.lstrip('/')}"


def _tenant_authority(authority: Any, tenant_id: Any) -> str:
    tenant = str(tenant_id or "").strip()
    raw_authority = str(authority or "https://login.microsoftonline.com/organizations").strip().rstrip("/")
    parsed = urlsplit(raw_authority)
    if parsed.scheme and parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if tenant and (not parts or parts[-1].casefold() in {"common", "organizations", "consumers"}):
            parts = [tenant]
        return urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(parts), "", ""))
    return f"https://login.microsoftonline.com/{tenant or 'organizations'}"


def _scopes_for(auth_mode: str, audience: str, config: Mapping[str, Any], scopes: Optional[Sequence[str] | str]) -> List[str]:
    if isinstance(scopes, str):
        explicit = [item.strip() for item in scopes.replace(",", " ").split() if item.strip()]
    else:
        explicit = [str(item).strip() for item in scopes or [] if str(item).strip()]
    if explicit:
        return explicit

    is_master_user_mode = "masteruser" in str(auth_mode or "").replace("-", "").replace("_", "").casefold()

    if str(audience).casefold() == "fabric":
        configured = config.get("fabric_scope") or []
        if isinstance(configured, str):
            configured = [item.strip() for item in configured.replace(",", " ").split() if item.strip()]
        configured = [str(item).strip() for item in configured if str(item).strip()]
        if configured:
            return configured
        return [FABRIC_REPORT_SCOPE if is_master_user_mode else FABRIC_DEFAULT_SCOPE]

    if is_master_user_mode:
        configured = config.get("scope") or []
        if isinstance(configured, str):
            configured = [item.strip() for item in configured.replace(",", " ").split() if item.strip()]
        configured = [str(item).strip() for item in configured if str(item).strip()]
        if configured:
            return configured
    return [POWER_BI_DEFAULT_SCOPE]


def load_auth_config(auth_mode: str = "MasterUser") -> Dict[str, Any]:
    result = Utils.validate_config(auth_mode)
    if isinstance(result, str):
        raise RuntimeError(result)
    return dict(result)


# Token: use a one-time bearer token that was already supplied.
def get_token_existing_bearer(existing_bearer_token: str) -> Dict[str, Any]:
    token = _clean_token(existing_bearer_token)
    return {
        "ok": bool(token),
        "user_type": "existing_bearer",
        "access_token": token,
        "authorization_header": f"Bearer {token}" if token else "",
        "raw_result": None,
    }


# Token: delegated Microsoft Entra device-code flow for an interactive user.
def get_token_delegated_device_code(
    auth_mode: str = "MasterUser",
    audience: str = "powerbi",
    scopes: Optional[Sequence[str] | str] = None,
) -> Dict[str, Any]:
    config = load_auth_config(auth_mode)
    authority = _tenant_authority(config.get("authority"), config.get("tenant_id"))
    requested_scopes = _scopes_for(auth_mode, audience, config, scopes)
    app = msal.PublicClientApplication(
        client_id=str(config.get("client_id") or "").strip(),
        authority=authority,
    )
    flow = app.initiate_device_flow(scopes=requested_scopes)
    if "user_code" not in flow:
        return {
            "ok": False,
            "user_type": "delegated_device_code",
            "authority": authority,
            "scopes": requested_scopes,
            "access_token": "",
            "authorization_header": "",
            "raw_result": flow,
        }
    print(flow.get("message") or "Complete Microsoft device-code sign-in.", flush=True)
    result = app.acquire_token_by_device_flow(flow)
    token = _clean_token(str((result or {}).get("access_token") or ""))
    return {
        "ok": bool(token),
        "user_type": "delegated_device_code",
        "authority": authority,
        "scopes": requested_scopes,
        "access_token": token,
        "authorization_header": f"Bearer {token}" if token else "",
        "raw_result": _json_safe(result),
    }


# Token: device-code flow for a confidential app registration that requires client_secret at token exchange.
def get_token_confidential_device_code(
    auth_mode: str = "MasterUser-Admin",
    audience: str = "powerbi",
    scopes: Optional[Sequence[str] | str] = None,
    poll_timeout_seconds: int = 900,
) -> Dict[str, Any]:
    config = load_auth_config(auth_mode)
    authority = _tenant_authority(config.get("authority"), config.get("tenant_id"))
    requested_scopes = _scopes_for(auth_mode, audience, config, scopes)
    client_id = str(config.get("client_id") or "").strip()
    client_secret = str(config.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("confidential device-code flow requires client_id and client_secret.")

    device_url = f"{authority}/oauth2/v2.0/devicecode"
    token_url = f"{authority}/oauth2/v2.0/token"
    device_response = requests.post(
        device_url,
        data={
            "client_id": client_id,
            "scope": " ".join(requested_scopes),
        },
        timeout=60,
    )
    try:
        device_payload = device_response.json()
    except Exception:
        device_payload = {"text": device_response.text}
    if device_response.status_code != 200:
        return {
            "ok": False,
            "user_type": "confidential_device_code",
            "authority": authority,
            "scopes": requested_scopes,
            "access_token": "",
            "authorization_header": "",
            "raw_result": _json_safe(device_payload),
        }

    print(device_payload.get("message") or "Complete Microsoft device-code sign-in.", flush=True)
    device_code = str(device_payload.get("device_code") or "")
    interval = max(1, int(device_payload.get("interval") or 5))
    expires_in = max(1, int(device_payload.get("expires_in") or poll_timeout_seconds))
    deadline = time.monotonic() + min(int(poll_timeout_seconds), expires_in)
    last_payload: Any = device_payload

    while time.monotonic() < deadline:
        time.sleep(interval)
        token_response = requests.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": device_code,
            },
            timeout=60,
        )
        try:
            token_payload = token_response.json()
        except Exception:
            token_payload = {"text": token_response.text}
        last_payload = token_payload

        access_token = _clean_token(str(token_payload.get("access_token") or ""))
        if access_token:
            return {
                "ok": True,
                "user_type": "confidential_device_code",
                "authority": authority,
                "scopes": requested_scopes,
                "access_token": access_token,
                "authorization_header": f"Bearer {access_token}",
                "raw_result": _json_safe(token_payload),
            }

        error = str(token_payload.get("error") or "")
        if error == "slow_down":
            interval += 5
            continue
        if error == "authorization_pending":
            continue
        if error in {"authorization_declined", "bad_verification_code", "expired_token"}:
            break
        if token_response.status_code not in {400, 401}:
            break

    return {
        "ok": False,
        "user_type": "confidential_device_code",
        "authority": authority,
        "scopes": requested_scopes,
        "access_token": "",
        "authorization_header": "",
        "raw_result": _json_safe(last_payload),
    }


# Token: username/password flow for a non-MFA local test account.
def get_token_username_password(
    username: Optional[str] = None,
    password: Optional[str] = None,
    auth_mode: str = "MasterUser",
    audience: str = "powerbi",
    scopes: Optional[Sequence[str] | str] = None,
) -> Dict[str, Any]:
    config = load_auth_config(auth_mode)
    authority = _tenant_authority(config.get("authority"), config.get("tenant_id"))
    requested_scopes = _scopes_for(auth_mode, audience, config, scopes)
    user_name = username or str(config.get("username") or "").strip()
    user_password = password or str(config.get("password") or "").strip()
    if not user_name or not user_password:
        raise RuntimeError("username and password are required for username_password token flow.")

    app = msal.PublicClientApplication(
        client_id=str(config.get("client_id") or "").strip(),
        authority=authority,
    )
    result = app.acquire_token_by_username_password(
        username=user_name,
        password=user_password,
        scopes=requested_scopes,
    )
    token = _clean_token(str((result or {}).get("access_token") or ""))
    return {
        "ok": bool(token),
        "user_type": "username_password",
        "authority": authority,
        "scopes": requested_scopes,
        "access_token": token,
        "authorization_header": f"Bearer {token}" if token else "",
        "raw_result": _json_safe(result),
    }


# Token: service principal client-credentials flow.
def get_token_service_principal(
    auth_mode: str = "ServicePrincipal",
    audience: str = "powerbi",
    scopes: Optional[Sequence[str] | str] = None,
) -> Dict[str, Any]:
    config = load_auth_config(auth_mode)
    authority = _tenant_authority(config.get("authority"), config.get("tenant_id"))
    requested_scopes = (
        [FABRIC_DEFAULT_SCOPE]
        if str(audience).casefold() == "fabric"
        else [POWER_BI_DEFAULT_SCOPE]
    )
    if scopes:
        requested_scopes = _scopes_for(auth_mode, audience, config, scopes)
    client_secret = str(config.get("client_secret") or "").strip()
    if not client_secret:
        raise RuntimeError(f"{auth_mode} requires client_secret.")

    app = msal.ConfidentialClientApplication(
        client_id=str(config.get("client_id") or "").strip(),
        client_credential=client_secret,
        authority=authority,
    )
    result = app.acquire_token_for_client(scopes=requested_scopes)
    token = _clean_token(str((result or {}).get("access_token") or ""))
    return {
        "ok": bool(token),
        "user_type": "service_principal",
        "authority": authority,
        "scopes": requested_scopes,
        "access_token": token,
        "authorization_header": f"Bearer {token}" if token else "",
        "raw_result": _json_safe(result),
    }


# Token: one wrapper when you want to choose user type by string.
def get_token(
    user_type: str = "delegated_device_code",
    auth_mode: str = "MasterUser",
    audience: str = "powerbi",
    existing_bearer_token: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    scopes: Optional[Sequence[str] | str] = None,
) -> Dict[str, Any]:
    value = str(user_type or "").strip().casefold()
    if value in {"existing", "existing_bearer", "bearer"}:
        return get_token_existing_bearer(existing_bearer_token or "")
    if value in {"device", "device_code", "delegated", "delegated_device_code"}:
        return get_token_delegated_device_code(auth_mode=auth_mode, audience=audience, scopes=scopes)
    if value in {"confidential_device", "confidential_device_code"}:
        return get_token_confidential_device_code(auth_mode=auth_mode, audience=audience, scopes=scopes)
    if value in {"username_password", "password", "ropc"}:
        return get_token_username_password(
            username=username,
            password=password,
            auth_mode=auth_mode,
            audience=audience,
            scopes=scopes,
        )
    if value in {"sp", "service_principal", "client_credentials"}:
        return get_token_service_principal(auth_mode=auth_mode, audience=audience, scopes=scopes)
    if value == "auto":
        if auth_mode == "MasterUser":
            return get_token_delegated_device_code(auth_mode=auth_mode, audience=audience, scopes=scopes)
        return get_token_service_principal(auth_mode=auth_mode, audience=audience, scopes=scopes)
    raise ValueError("user_type must be existing_bearer, delegated_device_code, username_password, service_principal, or auto.")


# API: GET /groups
def get_workspaces(access_token: str, top: Optional[int] = None, skip: Optional[int] = None) -> Dict[str, Any]:
    params = {key: value for key, value in {"$top": top, "$skip": skip}.items() if value is not None}
    return _request("GET", _powerbi_url("/groups"), access_token, params=params)


# API: GET /groups/{workspace_id}
def get_workspace(access_token: str, workspace_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}"), access_token)


# API: GET /groups/{workspace_id}/reports
def get_workspace_reports(access_token: str, workspace_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/reports"), access_token)


# API: GET /groups/{workspace_id}/reports/{report_id}
def get_workspace_report(access_token: str, workspace_id: str, report_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/reports/{report_id}"), access_token)


# API: GET /groups/{workspace_id}/dashboards
def get_workspace_dashboards(access_token: str, workspace_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/dashboards"), access_token)


# API: GET /groups/{workspace_id}/datasets
def get_workspace_datasets(access_token: str, workspace_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/datasets"), access_token)


# API: GET /groups/{workspace_id}/datasets/{dataset_id}
def get_workspace_dataset(access_token: str, workspace_id: str, dataset_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/datasets/{dataset_id}"), access_token)


# API: GET /groups/{workspace_id}/users
def get_workspace_users(access_token: str, workspace_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/users"), access_token)


# API: GET /groups/{workspace_id}/reports/{report_id}/users
def get_workspace_report_users(access_token: str, workspace_id: str, report_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/reports/{report_id}/users"), access_token)


# API: GET /groups/{workspace_id}/dashboards/{dashboard_id}/users
def get_workspace_dashboard_users(access_token: str, workspace_id: str, dashboard_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/dashboards/{dashboard_id}/users"), access_token)


# API: GET /groups/{workspace_id}/dashboards/{dashboard_id}/tiles
def get_dashboard_tiles(access_token: str, workspace_id: str, dashboard_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/dashboards/{dashboard_id}/tiles"), access_token)


# API: GET /apps/{app_id}/reports
def get_app_reports(access_token: str, app_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/apps/{app_id}/reports"), access_token)


# API: GET /apps/{app_id}/dashboards
def get_app_dashboards(access_token: str, app_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/apps/{app_id}/dashboards"), access_token)


# API: GET /apps/{app_id}/dashboards/{dashboard_id}/tiles
def get_app_dashboard_tiles(access_token: str, app_id: str, dashboard_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/apps/{app_id}/dashboards/{dashboard_id}/tiles"), access_token)


# API: GET /admin/apps
def get_admin_apps(access_token: str, top: int = 50, continuation_url: Optional[str] = None) -> Dict[str, Any]:
    if continuation_url:
        return _request("GET", continuation_url, access_token)
    return _request("GET", _powerbi_url("/admin/apps"), access_token, params={"$top": top})


# API: GET /admin/apps/{app_id}/users
def get_admin_app_users(access_token: str, app_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/admin/apps/{app_id}/users"), access_token)


# API: GET /admin/reports/{report_id}/users
def get_admin_report_users(access_token: str, report_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/admin/reports/{report_id}/users"), access_token)


# API: GET /admin/dashboards/{dashboard_id}/users
def get_admin_dashboard_users(access_token: str, dashboard_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/admin/dashboards/{dashboard_id}/users"), access_token)


# API: GET /admin/groups
def get_admin_groups(access_token: str, filter_expr: Optional[str] = None, top: Optional[int] = None) -> Dict[str, Any]:
    params = {key: value for key, value in {"$filter": filter_expr, "$top": top}.items() if value is not None}
    return _request("GET", _powerbi_url("/admin/groups"), access_token, params=params)


# API: GET /admin/reports
def get_admin_reports(access_token: str, filter_expr: Optional[str] = None, top: Optional[int] = None) -> Dict[str, Any]:
    params = {key: value for key, value in {"$filter": filter_expr, "$top": top}.items() if value is not None}
    return _request("GET", _powerbi_url("/admin/reports"), access_token, params=params)


# API: GET /admin/datasets
def get_admin_datasets(access_token: str, filter_expr: Optional[str] = None, top: Optional[int] = None) -> Dict[str, Any]:
    params = {key: value for key, value in {"$filter": filter_expr, "$top": top}.items() if value is not None}
    return _request("GET", _powerbi_url("/admin/datasets"), access_token, params=params)


# API: GET /admin/dashboards
def get_admin_dashboards(access_token: str, filter_expr: Optional[str] = None, top: Optional[int] = None) -> Dict[str, Any]:
    params = {key: value for key, value in {"$filter": filter_expr, "$top": top}.items() if value is not None}
    return _request("GET", _powerbi_url("/admin/dashboards"), access_token, params=params)


# API: POST /datasets/{dataset_id}/executeQueries
def post_dataset_execute_queries(
    access_token: str,
    dataset_id: str,
    query: str,
    impersonated_user_name: Optional[str] = None,
    serializer_settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"queries": [{"query": query}]}
    if impersonated_user_name:
        body["impersonatedUserName"] = impersonated_user_name
    if serializer_settings:
        body["serializerSettings"] = dict(serializer_settings)
    return _request("POST", _powerbi_url(f"/datasets/{dataset_id}/executeQueries"), access_token, body=body)


# API: POST /datasets/{dataset_id}/executeQueries | DMV: DBSCHEMA_TABLES
def post_dataset_table_names_query(access_token: str, dataset_id: str) -> Dict[str, Any]:
    return post_dataset_execute_queries(
        access_token,
        dataset_id,
        "SELECT [TABLE_NAME] FROM $SYSTEM.DBSCHEMA_TABLES WHERE [TABLE_TYPE] = 'TABLE'",
    )


# API: POST /datasets/{dataset_id}/executeQueries | DMV: MDSCHEMA_MEASURES names
def post_dataset_measure_names_query(access_token: str, dataset_id: str) -> Dict[str, Any]:
    return post_dataset_execute_queries(
        access_token,
        dataset_id,
        "SELECT [MEASURE_NAME] FROM $SYSTEM.MDSCHEMA_MEASURES",
    )


# API: POST /datasets/{dataset_id}/executeQueries | DMV: MDSCHEMA_MEASURES details
def post_dataset_measure_details_query(access_token: str, dataset_id: str) -> Dict[str, Any]:
    return post_dataset_execute_queries(
        access_token,
        dataset_id,
        "SELECT [MEASUREGROUP_NAME], [MEASURE_NAME], [EXPRESSION] FROM $SYSTEM.MDSCHEMA_MEASURES",
    )


# API: GET /groups/{workspace_id}/reports/{report_id}/pages
def get_report_pages(access_token: str, workspace_id: str, report_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/groups/{workspace_id}/reports/{report_id}/pages"), access_token)


# API: GET /groups/{workspace_id}/reports/{report_id}/Export
def export_report(
    access_token: str,
    workspace_id: str,
    report_id: str,
    download_type: Optional[str] = None,
    include_binary_body: bool = False,
) -> Dict[str, Any]:
    params: Dict[str, str] = {"preferClientRouting": "true"}
    if download_type and str(download_type).casefold() != "default":
        params["downloadType"] = str(download_type)
    return _request(
        "GET",
        _powerbi_url(f"/groups/{workspace_id}/reports/{report_id}/Export"),
        access_token,
        params=params,
        timeout=(30, 900),
        include_binary_body=include_binary_body,
    )


# API: POST /admin/workspaces/getInfo
def post_admin_workspace_get_info(
    access_token: str,
    workspace_ids: Sequence[str],
    lineage: bool = True,
    datasource_details: bool = False,
    dataset_schema: bool = False,
    dataset_expressions: bool = False,
    get_artifact_users: bool = False,
) -> Dict[str, Any]:
    params = {
        "lineage": str(bool(lineage)).lower(),
        "datasourceDetails": str(bool(datasource_details)).lower(),
        "datasetSchema": str(bool(dataset_schema)).lower(),
        "datasetExpressions": str(bool(dataset_expressions)).lower(),
        "getArtifactUsers": str(bool(get_artifact_users)).lower(),
    }
    body = {"workspaces": [str(item).strip() for item in workspace_ids if str(item or "").strip()]}
    return _request("POST", _powerbi_url("/admin/workspaces/getInfo"), access_token, params=params, body=body, timeout=30)


# API: GET /admin/workspaces/scanStatus/{scan_id}
def get_admin_workspace_scan_status(access_token: str, scan_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/admin/workspaces/scanStatus/{scan_id}"), access_token, timeout=30)


# API: GET /admin/workspaces/scanResult/{scan_id}
def get_admin_workspace_scan_result(access_token: str, scan_id: str) -> Dict[str, Any]:
    return _request("GET", _powerbi_url(f"/admin/workspaces/scanResult/{scan_id}"), access_token, timeout=60)


# API chain: POST getInfo, GET scanStatus, GET scanResult. Every raw API response is preserved.
def run_admin_workspace_scanner(
    access_token: str,
    workspace_ids: Sequence[str],
    lineage: bool = True,
    datasource_details: bool = False,
    dataset_schema: bool = False,
    dataset_expressions: bool = False,
    get_artifact_users: bool = False,
    poll_interval_seconds: float = 0.5,
    max_poll_attempts: int = 8,
) -> Dict[str, Any]:
    post_response = post_admin_workspace_get_info(
        access_token,
        workspace_ids,
        lineage=lineage,
        datasource_details=datasource_details,
        dataset_schema=dataset_schema,
        dataset_expressions=dataset_expressions,
        get_artifact_users=get_artifact_users,
    )
    scan_id = ""
    post_json = post_response.get("json")
    if isinstance(post_json, dict):
        scan_id = str(post_json.get("id") or "").strip()

    status_responses: List[Dict[str, Any]] = []
    result_response: Optional[Dict[str, Any]] = None
    if scan_id:
        for attempt in range(max(1, int(max_poll_attempts))):
            status_response = get_admin_workspace_scan_status(access_token, scan_id)
            status_responses.append(status_response)
            status_json = status_response.get("json")
            status = str(status_json.get("status") or "").casefold() if isinstance(status_json, dict) else ""
            if status == "succeeded":
                result_response = get_admin_workspace_scan_result(access_token, scan_id)
                break
            if status in {"failed", "cancelled"}:
                break
            if attempt < max_poll_attempts - 1:
                time.sleep(max(0.0, float(poll_interval_seconds)))

    return {
        "scan_id": scan_id,
        "post_get_info": post_response,
        "scan_status_responses": status_responses,
        "scan_result": result_response,
    }


# API: POST /workspaces/{workspace_id}/reports/{report_id}/getDefinition
def post_fabric_report_get_definition(
    access_token: str,
    workspace_id: str,
    report_id: str,
    report_format: Optional[str] = None,
) -> Dict[str, Any]:
    params: Dict[str, str] = {}
    if str(report_format or "").casefold() == "pbir":
        params["format"] = "PBIR"
    elif str(report_format or "").casefold() == "pbirlegacy":
        params["format"] = "PBIR-Legacy"
    return _request(
        "POST",
        _fabric_url(f"/workspaces/{workspace_id}/reports/{report_id}/getDefinition"),
        access_token,
        params=params,
        timeout=60,
    )


# API: GET /operations/{operation_id}
def get_fabric_operation(access_token: str, operation_id_or_url: str) -> Dict[str, Any]:
    url = operation_id_or_url if operation_id_or_url.startswith("http") else _fabric_url(f"/operations/{operation_id_or_url}")
    return _request("GET", url, access_token, timeout=60)


# API: GET /operations/{operation_id}/result
def get_fabric_operation_result(access_token: str, operation_id_or_url: str) -> Dict[str, Any]:
    url = operation_id_or_url if operation_id_or_url.startswith("http") else _fabric_url(f"/operations/{operation_id_or_url}/result")
    return _request("GET", url, access_token, timeout=60)


# API chain: Fabric getDefinition POST, operation polling, and operation result. Every raw response is preserved.
def run_fabric_report_get_definition(
    access_token: str,
    workspace_id: str,
    report_id: str,
    report_format: Optional[str] = None,
    poll_interval_seconds: float = 5.0,
    max_poll_attempts: int = 60,
) -> Dict[str, Any]:
    post_response = post_fabric_report_get_definition(
        access_token,
        workspace_id,
        report_id,
        report_format=report_format,
    )
    if post_response.get("status_code") == 200:
        return {"post_get_definition": post_response, "operation_responses": [], "operation_result": None}

    headers = post_response.get("headers") if isinstance(post_response.get("headers"), dict) else {}
    operation_id = str(headers.get("x-ms-operation-id") or "").strip()
    operation_url = str(headers.get("Location") or "").strip()
    if not operation_url and operation_id:
        operation_url = _fabric_url(f"/operations/{operation_id}")

    operation_responses: List[Dict[str, Any]] = []
    operation_result: Optional[Dict[str, Any]] = None
    if operation_url:
        for attempt in range(max(1, int(max_poll_attempts))):
            operation_response = get_fabric_operation(access_token, operation_url)
            operation_responses.append(operation_response)
            operation_json = operation_response.get("json")
            status = str(operation_json.get("status") or "").casefold() if isinstance(operation_json, dict) else ""
            if isinstance(operation_json, dict) and isinstance(operation_json.get("definition"), dict):
                operation_result = operation_response
                break
            if status == "succeeded":
                poll_headers = operation_response.get("headers") if isinstance(operation_response.get("headers"), dict) else {}
                result_url = str(poll_headers.get("Location") or "").strip()
                if not result_url or result_url.rstrip("/") == operation_url.rstrip("/"):
                    result_url = _fabric_url(f"/operations/{operation_id}/result") if operation_id else ""
                if result_url:
                    operation_result = _request("GET", result_url, access_token, timeout=60)
                break
            if status in {"failed", "cancelled"}:
                break
            if attempt < max_poll_attempts - 1:
                time.sleep(max(0.0, float(poll_interval_seconds)))

    return {
        "operation_id": operation_id,
        "operation_url": operation_url,
        "post_get_definition": post_response,
        "operation_responses": operation_responses,
        "operation_result": operation_result,
    }


# Internal Power BI web call used only to inspect app.powerbi.com pages for metadata URLs.
def get_powerbi_web_page(access_token: str, url: str, max_text_chars: Optional[int] = 750000) -> Dict[str, Any]:
    return _request(
        "GET",
        url,
        access_token,
        headers={
            "Accept": "text/html,application/json,*/*",
            "User-Agent": "Mozilla/5.0 PowerBI-Lineage-RawProbe",
        },
        timeout=60,
        max_text_chars=max_text_chars,
        allow_redirects=True,
    )


# Internal API: GET {metadata_base_url}/metadata/appmodel/apps/{app_identifier}
def get_internal_app_audience_metadata(access_token: str, metadata_base_url: str, app_identifier: str) -> Dict[str, Any]:
    base_url = str(metadata_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("metadata_base_url is required.")
    url = f"{base_url}/metadata/appmodel/apps/{app_identifier}?requestDataType=7&access-control-allow-credentials=true"
    return _request("GET", url, access_token, headers={"Accept": "application/json"}, timeout=60)


def _xmla_workspace_urls(workspace_name: str) -> List[str]:
    raw_name = str(workspace_name or "").strip()
    return list(
        dict.fromkeys(
            [
                f"powerbi://api.powerbi.com/v1.0/myorg/{raw_name}",
                f"powerbi://api.powerbi.com/v1.0/myorg/{quote(raw_name, safe='')}",
            ]
        )
    )


def _ole_value(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _xmla_connection_string(workspace_url: str, dataset_name: str, access_token: str, user_id: Optional[str] = None) -> str:
    parts = [
        "Provider=MSOLAP",
        f"Data Source={_ole_value(workspace_url)}",
        f"Initial Catalog={_ole_value(dataset_name)}",
    ]
    if user_id:
        parts.append(f"User ID={_ole_value(user_id)}")
    parts.append(f"Password={_ole_value(_clean_token(access_token))}")
    return ";".join(parts) + ";"


# XMLA DMV: execute one read-only DMV query and return raw columns/rows from the cursor.
def execute_xmla_dmv(
    access_token: str,
    workspace_name: str,
    dataset_name: str,
    query: str,
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    if platform.system() != "Windows":
        return {
            "ok": False,
            "workspace_name": workspace_name,
            "dataset_name": dataset_name,
            "query": query,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "attempts": [],
            "error": "XMLA DMV calls require Windows, pywin32, and the Microsoft MSOLAP provider.",
        }

    from xmla_ado_com import connect_xmla

    attempts: List[Dict[str, Any]] = []
    for workspace_url in _xmla_workspace_urls(workspace_name):
        conn = None
        cursor = None
        started = time.monotonic()
        try:
            conn = connect_xmla(_xmla_connection_string(workspace_url, dataset_name, access_token, user_id=user_id))
            cursor = conn.cursor()
            cursor.execute(query)
            columns = [str(item[0]) for item in (cursor.description or []) if item and item[0] is not None]
            rows = cursor.fetchall()
            return _json_safe(
                {
                    "ok": True,
                    "workspace_url": workspace_url,
                    "workspace_name": workspace_name,
                    "dataset_name": dataset_name,
                    "query": query,
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "elapsed_seconds": time.monotonic() - started,
                    "attempts": attempts,
                    "error": "",
                }
            )
        except Exception as error:
            attempts.append(
                {
                    "workspace_url": workspace_url,
                    "elapsed_seconds": time.monotonic() - started,
                    "error": _redact_error(error, [access_token]),
                }
            )
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return _json_safe(
        {
            "ok": False,
            "workspace_name": workspace_name,
            "dataset_name": dataset_name,
            "query": query,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "attempts": attempts,
            "error": "All XMLA connection attempts failed.",
        }
    )


# XMLA DMV: SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS
def xmla_catalogs(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, "SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS")


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_TABLES
def xmla_tables(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = "SELECT [ID], [Name], [IsHidden], [LineageTag] FROM $SYSTEM.TMSCHEMA_TABLES"
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_COLUMNS
def xmla_columns(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = (
        "SELECT [ID], [TableID], [ExplicitName], [InferredName], "
        "[ExplicitDataType], [InferredDataType], [IsHidden], [SourceColumn], "
        "[Type], [Expression], [LineageTag], [SourceLineageTag] "
        "FROM $SYSTEM.TMSCHEMA_COLUMNS"
    )
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_MEASURES
def xmla_measures(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = "SELECT [Name], [TableID], [Expression], [IsHidden], [LineageTag] FROM $SYSTEM.TMSCHEMA_MEASURES"
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_PARTITIONS
def xmla_partitions(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = (
        "SELECT [TableID], [Name], [QueryDefinition], [SourceType], [ExpressionSourceID] "
        "FROM $SYSTEM.TMSCHEMA_PARTITIONS"
    )
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_EXPRESSIONS
def xmla_expressions(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = "SELECT [ID], [Name], [Expression] FROM $SYSTEM.TMSCHEMA_EXPRESSIONS"
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.TMSCHEMA_RELATIONSHIPS
def xmla_relationships(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = (
        "SELECT [ID], [FromTableID], [FromColumnID], [ToTableID], [ToColumnID], "
        "[IsActive], [CrossFilteringBehavior], [FromCardinality], [ToCardinality] "
        "FROM $SYSTEM.TMSCHEMA_RELATIONSHIPS"
    )
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)


# XMLA DMV: SELECT from $SYSTEM.DISCOVER_CALC_DEPENDENCY
def xmla_calc_dependency(access_token: str, workspace_name: str, dataset_name: str) -> Dict[str, Any]:
    query = (
        "SELECT [OBJECT_TYPE], [TABLE], [OBJECT], [EXPRESSION], "
        "[REFERENCED_OBJECT_TYPE], [REFERENCED_TABLE], [REFERENCED_OBJECT], "
        "[REFERENCED_EXPRESSION], [QUERY] FROM $SYSTEM.DISCOVER_CALC_DEPENDENCY"
    )
    return execute_xmla_dmv(access_token, workspace_name, dataset_name, query)
