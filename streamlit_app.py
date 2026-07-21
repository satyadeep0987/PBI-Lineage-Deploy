import streamlit as st
import time
import msal
import requests
import pandas as pd
from utils import Utils
import base64
import re
import zipfile
import io
import json
import html
import hashlib
import os
import streamlit.components.v1 as components
from xmla_ado_com import connect_xmla
from pathlib import PurePosixPath
from urllib.parse import quote
from pbi_modules.app_shell import (
    check_authenticated_session,
    render_app_top_bar,
    render_direct_measure_lookup_page,
    render_login_page,
    render_workflow_choice_page,
)
from pbi_modules.controls import (
    render_checkbox_selector,
    render_csv_download,
    render_searchable_multiselect,
    render_searchable_single_select,
)


# --- GLOBAL DISPLAY COLUMN NAME STANDARDIZATION ---
def _normalize_ui_column_name(column_name):
    """Return a Streamlit/CSV friendly column name without spaces.

    Display rule:
    - No spaces in any tab/table column header.
    - Replace spaces and special separators such as '/', '-', '()' with '_'.
    - Collapse repeated underscores to one underscore.
    """
    name = str(column_name).strip()
    if not name:
        return name
    name = re.sub(r"[^0-9A-Za-z_]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def _normalize_dataframe_column_names(df):
    """Normalize dataframe column names for all user-facing tables/downloads."""
    if df is None or not isinstance(df, pd.DataFrame):
        return df
    normalized_df = df.copy()
    normalized_columns = []
    seen = {}
    for col in normalized_df.columns:
        base = _normalize_ui_column_name(col)
        if base in seen:
            seen[base] += 1
            normalized_columns.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            normalized_columns.append(base)
    normalized_df.columns = normalized_columns
    return normalized_df


# Patch Streamlit dataframe rendering so every displayed table follows the same
# no-space column naming rule, including inventory/access tables that are created
# directly from REST API payloads.
_ORIGINAL_ST_DATAFRAME = st.dataframe

def _dataframe_without_space_columns(data=None, *args, **kwargs):
    try:
        if isinstance(data, pd.DataFrame):
            data = _normalize_dataframe_column_names(data)
    except Exception:
        pass
    return _ORIGINAL_ST_DATAFRAME(data, *args, **kwargs)

st.dataframe = _dataframe_without_space_columns

# --- AUTH & MSAL GLOBALS ---
clientapp_mu = None
clientapp_sp = None
clientapp_spa = None
_DEVICE_FLOW_STATE_KEY = "msal_device_flow"
_FABRIC_DEVICE_FLOW_STATE_KEY = "msal_fabric_device_flow"
_GENERIC_AAD_AUTHORITY_TENANTS = {"common", "organizations", "consumers"}
_FABRIC_API_BASE_URL = "https://api.fabric.microsoft.com/v1"
_DEFAULT_FABRIC_REPORT_SCOPES = ["https://api.fabric.microsoft.com/Report.ReadWrite.All"]


def _streamlit_secret_value(*keys):
    try:
        value = st.secrets
        for key in keys:
            if not hasattr(value, "get") or key not in value:
                return None
            value = value.get(key)
        return value
    except Exception:
        return None


def _use_device_code_auth():
    """Use device-code auth by default in the deploy package."""
    secret_auth_flow = (
        _streamlit_secret_value("PBI_AUTH_FLOW")
        or _streamlit_secret_value("powerbi", "PBI_AUTH_FLOW")
        or _streamlit_secret_value("powerbi", "auth_flow")
    )
    auth_flow = str(os.getenv("PBI_AUTH_FLOW") or secret_auth_flow or "device_code").strip().lower()
    return auth_flow in {"device", "device_code", "device-code", "cloud"}


def _tenant_specific_authority(authority, tenant_id):
    """Return an Entra authority URL that includes a concrete tenant when available."""
    authority = str(authority or "").strip().rstrip("/")
    tenant_id = str(tenant_id or "").strip()
    if not authority or not tenant_id:
        return authority

    authority_parts = authority.split("/")
    if authority_parts[-1].lower() in _GENERIC_AAD_AUTHORITY_TENANTS:
        authority_parts[-1] = tenant_id
        return "/".join(authority_parts)

    if authority.lower() in {"https://login.microsoftonline.com", "http://login.microsoftonline.com"}:
        return f"{authority}/{tenant_id}"

    return authority


def _clear_device_flow_state():
    st.session_state.pop(_DEVICE_FLOW_STATE_KEY, None)


def _fabric_scopes(config_result=None):
    configured = (config_result or {}).get("fabric_scope") if isinstance(config_result, dict) else None
    scopes = Utils._split_scopes(configured)
    return scopes or list(_DEFAULT_FABRIC_REPORT_SCOPES)


def _try_acquire_fabric_token_silent(clientapp, config_result=None):
    """Use the signed-in user's MSAL cache to obtain the separate Fabric audience token."""
    if not clientapp:
        return None, "The MasterUser MSAL client is unavailable."

    last_error = "No signed-in account was found in the MSAL cache."
    for account in clientapp.get_accounts() or []:
        response = clientapp.acquire_token_silent(scopes=_fabric_scopes(config_result), account=account)
        if response and response.get("access_token"):
            return response, None
        if response:
            last_error = response.get("error_description") or response.get("error") or last_error
    return None, last_error


def _render_device_flow_instructions(flow):
    verification_uri = (
        flow.get("verification_uri")
        or flow.get("verification_url")
        or "https://microsoft.com/devicelogin"
    )
    user_code = str(flow.get("user_code") or "").strip()
    if user_code:
        st.info(f"Open {verification_uri} and enter this code to sign in:")
        st.code(user_code)
    message = str(flow.get("message") or "").strip()
    if message:
        st.caption(message)


def _start_master_user_device_flow(clientapp, scope):
    flow = clientapp.initiate_device_flow(scopes=scope)
    if "user_code" not in flow:
        error_message = (
            flow.get("error_description")
            or flow.get("error")
            or "Microsoft Entra did not return a device code."
        )
        raise Exception(f"Could not start device-code sign-in: {error_message}")

    flow.setdefault("created_at", time.time())
    if not flow.get("expires_at"):
        flow["expires_at"] = time.time() + int(flow.get("expires_in", 900))
    st.session_state[_DEVICE_FLOW_STATE_KEY] = flow
    _render_device_flow_instructions(flow)
    st.info("After approving the sign-in, return here and click 'I completed sign-in'.")
    return None


def _acquire_master_user_device_token(clientapp, scope):
    flow = st.session_state.get(_DEVICE_FLOW_STATE_KEY)
    if not isinstance(flow, dict) or float(flow.get("expires_at", 0)) <= time.time():
        return _start_master_user_device_flow(clientapp, scope)

    response = clientapp.acquire_token_by_device_flow(
        flow,
        exit_condition=lambda current_flow: True,
    )
    if response and "access_token" in response:
        _clear_device_flow_state()
        return response

    error = (response or {}).get("error")
    if error in {"authorization_pending", "slow_down"}:
        st.session_state[_DEVICE_FLOW_STATE_KEY] = flow
        _render_device_flow_instructions(flow)
        st.info("Microsoft is still waiting for the sign-in approval.")
        return None

    if error == "expired_token":
        _clear_device_flow_state()
        raise Exception("The device code expired. Start sign-in again.")

    error_description = (
        (response or {}).get("error_description")
        or error
        or "Token response did not contain access_token"
    )
    raise Exception(error_description)

def get_access_token(auth_mode, prompt_behavior="select_account"):
    """Create a fresh MSAL token for the requested authentication mode.

    In Master User only mode we must not silently reuse an old browser/account
    session when the user clicks Logout and logs in again. Passing
    ``prompt=select_account`` forces Microsoft Entra ID to show the account
    picker. This gives the user a clean re-login path even when the browser
    still has Microsoft cookies from an earlier session.
    """
    config_result = Utils.validate_config(auth_mode)
    if isinstance(config_result, str):
        raise Exception(config_result)

    authenticate_mode = config_result["authenticate_mode"]
    tenant_id = config_result["tenant_id"]
    client_id = config_result["client_id"]
    client_secret = config_result["client_secret"]
    scope = config_result["scope"]
    authority = config_result["authority"]
    response = None

    try:
        if authenticate_mode.lower() == 'masteruser':
            authority = _tenant_specific_authority(authority, tenant_id)
            clientapp = msal.PublicClientApplication(client_id=client_id, authority=authority)
            if _use_device_code_auth():
                response = _acquire_master_user_device_token(clientapp, scope)
                if response is None:
                    return None
            else:
                try:
                    # Force a clean account picker on every login attempt.
                    response = clientapp.acquire_token_interactive(scopes=scope, prompt=prompt_behavior)
                except TypeError:
                    # Compatibility fallback for older MSAL versions that may not
                    # support the prompt argument. The app will still clear all
                    # Streamlit/session state before this call.
                    response = clientapp.acquire_token_interactive(scopes=scope)
        else:
            authority = _tenant_specific_authority(authority, tenant_id)
            clientapp = msal.ConfidentialClientApplication(client_id, client_credential=client_secret, authority=authority)
            response = clientapp.acquire_token_for_client(scopes=scope)

        if not response or 'access_token' not in response:
            error_description = (response or {}).get('error_description') or (response or {}).get('error') or 'Token response did not contain access_token'
            raise Exception(error_description)

        return response, clientapp

    except Exception as ex:
        raise Exception('Error retrieving Access token\n' + str(ex))

def get_all_tokens(prompt_behavior="select_account"):
    """Authenticate only with the delegated Master User account.

    Earlier versions authenticated three identities:
    1. MasterUser
    2. ServicePrincipal
    3. ServicePrincipal-Admin

    This version intentionally uses only MasterUser everywhere. To avoid touching the
    complete UI/data-flow contract, the same MasterUser token is assigned to the
    existing mu/sp/spa keys. All REST API, Admin API, App API, ExecuteQueries, and
    XMLA calls therefore run under the same delegated user identity.
    """
    try:
        token_result = get_access_token("MasterUser", prompt_behavior=prompt_behavior)
        if token_result is None:
            return None
        mu_resp, clientapp_mu = token_result
        master_token = mu_resp['access_token']
        config_result = Utils.validate_config("MasterUser")
        fabric_resp, fabric_error = _try_acquire_fabric_token_silent(
            clientapp_mu,
            config_result if isinstance(config_result, dict) else None,
        )

        data = {
            "auth_mode": "MasterUserOnly",
            "mu": master_token,
            "sp": master_token,
            "spa": master_token,
            "fabric": (fabric_resp or {}).get("access_token"),
            "fabric_error": fabric_error,
            "fabric_expires_at": time.time() + (fabric_resp or {}).get('expires_in', 0),
            "expires_at": time.time() + mu_resp.get('expires_in', 3599),
            "login_time": time.time(),
            "clientapp_mu": clientapp_mu,
            "clientapp_sp": None,
            "clientapp_spa": None,
        }
        return data
    except Exception as e:
        st.error(f"Login failed: {e}")
        return None


def _store_fabric_token_response(response, error=None):
    bundle = dict(st.session_state.get("auth_bundle") or {})
    if response and response.get("access_token"):
        bundle["fabric"] = response["access_token"]
        bundle["fabric_expires_at"] = time.time() + response.get("expires_in", 3599)
        bundle["fabric_error"] = None
    elif error:
        bundle["fabric_error"] = str(error)
    st.session_state.auth_bundle = bundle


def _fabric_headers_from_session():
    """Return a valid Fabric API header, refreshing silently from the MasterUser cache when possible."""
    bundle = st.session_state.get("auth_bundle") or {}
    token = str(bundle.get("fabric") or "").strip()
    expires_at = float(bundle.get("fabric_expires_at") or 0)
    if token and expires_at > time.time() + 30:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    config_result = Utils.validate_config("MasterUser")
    response, error = _try_acquire_fabric_token_silent(
        bundle.get("clientapp_mu"),
        config_result if isinstance(config_result, dict) else None,
    )
    _store_fabric_token_response(response, error)
    if response and response.get("access_token"):
        return {
            "Authorization": f"Bearer {response['access_token']}",
            "Content-Type": "application/json",
        }
    return None


def _new_fabric_device_flow():
    config_result = Utils.validate_config("MasterUser")
    if isinstance(config_result, str):
        raise RuntimeError(config_result)
    authority = _tenant_specific_authority(config_result["authority"], config_result["tenant_id"])
    clientapp = msal.PublicClientApplication(client_id=config_result["client_id"], authority=authority)
    flow = clientapp.initiate_device_flow(scopes=_fabric_scopes(config_result))
    if "user_code" not in flow:
        message = flow.get("error_description") or flow.get("error") or "Microsoft Entra did not return a device code."
        raise RuntimeError(f"Could not start Fabric authorization: {message}")
    flow.setdefault("created_at", time.time())
    if not flow.get("expires_at"):
        flow["expires_at"] = time.time() + int(flow.get("expires_in", 900))
    st.session_state[_FABRIC_DEVICE_FLOW_STATE_KEY] = flow
    return flow


def _poll_fabric_device_flow():
    flow = st.session_state.get(_FABRIC_DEVICE_FLOW_STATE_KEY)
    if not isinstance(flow, dict) or float(flow.get("expires_at", 0)) <= time.time():
        st.session_state.pop(_FABRIC_DEVICE_FLOW_STATE_KEY, None)
        return None, "The Fabric authorization code expired. Start authorization again."

    config_result = Utils.validate_config("MasterUser")
    if isinstance(config_result, str):
        return None, config_result
    authority = _tenant_specific_authority(config_result["authority"], config_result["tenant_id"])
    clientapp = msal.PublicClientApplication(client_id=config_result["client_id"], authority=authority)
    response = clientapp.acquire_token_by_device_flow(flow, exit_condition=lambda current_flow: True)
    if response and response.get("access_token"):
        st.session_state.pop(_FABRIC_DEVICE_FLOW_STATE_KEY, None)
        _store_fabric_token_response(response)
        return response, None

    error = (response or {}).get("error")
    if error in {"authorization_pending", "slow_down"}:
        return None, None
    st.session_state.pop(_FABRIC_DEVICE_FLOW_STATE_KEY, None)
    message = (response or {}).get("error_description") or error or "Fabric token was not returned."
    return None, message


def render_fabric_definition_authorization(scope_key):
    """Render an on-demand authorization step when silent Fabric token acquisition was unavailable."""
    headers = _fabric_headers_from_session()
    if headers:
        return headers

    st.warning("Automatic report layout retrieval needs a one-time Fabric API authorization for this session.")
    flow = st.session_state.get(_FABRIC_DEVICE_FLOW_STATE_KEY)
    if isinstance(flow, dict):
        _render_device_flow_instructions(flow)
        complete_col, restart_col = st.columns(2)
        with complete_col:
            if st.button("I completed Fabric authorization", type="primary", key=f"{scope_key}_fabric_auth_complete"):
                response, error = _poll_fabric_device_flow()
                if response:
                    st.rerun()
                elif error:
                    st.error(error)
                else:
                    st.info("Microsoft is still waiting for authorization approval.")
        with restart_col:
            if st.button("Restart Fabric authorization", key=f"{scope_key}_fabric_auth_restart"):
                st.session_state.pop(_FABRIC_DEVICE_FLOW_STATE_KEY, None)
                st.rerun()
        return None

    if st.button("Authorize automatic report layouts", key=f"{scope_key}_fabric_auth_start"):
        try:
            _new_fabric_device_flow()
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
    return None


def get_confidential_client_auth_header(auth_mode):
    """Acquire a real service-principal token for a targeted tester flow."""
    config_result = Utils.validate_config(auth_mode)
    if isinstance(config_result, str):
        raise RuntimeError(config_result)

    authenticate_mode = str(config_result.get("authenticate_mode") or auth_mode).lower()
    if authenticate_mode == "masteruser":
        raise RuntimeError("MasterUser uses the existing delegated interactive login token.")

    tenant_id = config_result["tenant_id"]
    authority = _tenant_specific_authority(config_result["authority"], tenant_id)
    client_id = config_result["client_id"]
    client_secret = config_result.get("client_secret")
    scope = config_result["scope"]

    if not client_secret:
        raise RuntimeError(f"Missing client_secret for {auth_mode} in config/powerbi_auth_config.json.")

    clientapp = msal.ConfidentialClientApplication(
        client_id,
        client_credential=client_secret,
        authority=authority,
    )
    response = clientapp.acquire_token_for_client(scopes=scope)
    if not response or "access_token" not in response:
        message = (response or {}).get("error_description") or (response or {}).get("error") or "Token response did not contain access_token"
        raise RuntimeError(f"{auth_mode} token acquisition failed: {message}")

    return {
        "Authorization": f"Bearer {response['access_token']}",
        "Content-Type": "application/json",
    }
    
def remove_tokens_for_client(clientapp_mu, clientapp_sp=None, clientapp_spa=None):
    """Best-effort removal of MSAL token-cache entries from existing client apps."""
    try:
        if clientapp_mu:
            accounts = clientapp_mu.get_accounts()
            for account in accounts:
                clientapp_mu.remove_account(account)
    except Exception as ex:
        print(f"Master user token cleanup warning: {ex}")

    try:
        if clientapp_sp:
            clientapp_sp.remove_tokens_for_client()
    except Exception as ex:
        print(f"Service principal token cleanup warning: {ex}")

    try:
        if clientapp_spa:
            clientapp_spa.remove_tokens_for_client()
    except Exception as ex:
        print(f"Service principal admin token cleanup warning: {ex}")

def clear_streamlit_session_state(keep_auth=False):
    """Clear Streamlit auth and data cache keys for clean logout/re-login.

    Streamlit stores API results, selected workspace/app, uploaded report layout,
    and generated Power BI metadata in ``st.session_state``. If those keys are
    not removed on logout, a new Master User login can still display stale
    workspace/report/dataset results from the previous token.
    """
    preserved = {"auth_bundle"} if keep_auth else set()
    for key in list(st.session_state.keys()):
        if key not in preserved:
            del st.session_state[key]

    if not keep_auth:
        st.session_state.auth_bundle = None

def logout_and_clear_session():
    """Remove MSAL accounts and clear all Streamlit runtime state."""
    bundle = st.session_state.get('auth_bundle')
    if bundle:
        remove_tokens_for_client(
            bundle.get('clientapp_mu'),
            bundle.get('clientapp_sp'),
            bundle.get('clientapp_spa'),
        )
    clear_streamlit_session_state(keep_auth=False)

# --- POWER BI REST API FUNCTIONS ---

def get_workspace_inventory(headers):
    ws_url = "https://api.powerbi.com/v1.0/myorg/groups"
    response = requests.get(ws_url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []

def get_all_app_details(headers):
    apps_data = []
    scan_url = "https://api.powerbi.com/v1.0/myorg/admin/apps?$top=50" 
    while scan_url:
        response = requests.get(scan_url, headers=headers)
        if response.status_code != 200:
            break
        data = response.json()
        if 'value' in data:
            apps_data.extend(data['value'])
        scan_url = data.get('@odata.nextLink', None)
    return apps_data

def get_artifacts(headers, workspace_id, artifact_type):
    endpoint = "reports" if artifact_type == 'report' else "dashboards"
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/{endpoint}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []

def get_workspace_users(headers, workspace_id):
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/users"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []

def get_artifact_users(headers, workspace_id, artifact_id, artifact_type, admin_headers=None):
    """
    Return users/groups/service principals that have access to a selected report/dashboard.

    The function first tries the workspace-level artifact users endpoint when a workspace_id is available.
    If that endpoint is blocked or unsupported, it falls back to the Admin artifact users endpoint.
    This keeps the Access tab focused only on permissions for the selected report/dashboard.
    """
    if not artifact_id:
        return []

    endpoint = "reports" if artifact_type == "Report" else "dashboards"

    if workspace_id:
        url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/{endpoint}/{artifact_id}/users"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json().get('value', [])

    fallback_headers = admin_headers or headers
    admin_url = f"https://api.powerbi.com/v1.0/myorg/admin/{endpoint}/{artifact_id}/users"
    admin_response = requests.get(admin_url, headers=fallback_headers)
    if admin_response.status_code == 200:
        return admin_response.json().get('value', [])

    return []


def _first_available_value(source, keys, default="N/A"):
    """Pick the first non-empty value from a dict using multiple possible Power BI API field names."""
    if not isinstance(source, dict):
        return default

    for key in keys:
        value = source.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return default


def _normalize_access_level(user):
    """Normalize report/dashboard/workspace/app access-right fields into one output column."""
    return _first_available_value(
        user,
        [
            "reportUserAccessRight",
            "dashboardUserAccessRight",
            "groupUserAccessRight",
            "appUserAccessRight",
            "accessRight",
        ],
    )


def build_access_records(container_type, container_name, artifact_type, artifact_name, artifact_id, access_source, users):
    """
    Convert Power BI user/access API responses into a consistent table structure.

    Output is intentionally access-only so it can be used as an API-style response for selected
    reports/dashboards without mixing lineage, table, or measure fields.
    """
    records = []

    for user in users or []:
        if not isinstance(user, dict):
            continue

        principal_name = _first_available_value(user, ["displayName", "name", "identifier", "emailAddress"])
        email_or_identifier = _first_available_value(user, ["emailAddress", "identifier", "graphId", "id"])

        records.append({
            "Container Type": container_type,
            "Container Name": container_name,
            "Artifact Type": artifact_type,
            "Artifact Name": artifact_name,
            "Artifact ID": artifact_id,
            "Access Source": access_source,
            "Principal Name": principal_name,
            "Email / Identifier": email_or_identifier,
            "Principal Type": _first_available_value(user, ["principalType"]),
            "User Type": _first_available_value(user, ["userType"]),
            "Access Level": _normalize_access_level(user),
        })

    return records


def dedupe_access_records(records):
    """Remove exact duplicate access rows while preserving row order."""
    seen = set()
    deduped = []

    for record in records or []:
        key = (
            record.get("Container Type"),
            record.get("Container Name"),
            record.get("Artifact Type"),
            record.get("Artifact ID"),
            record.get("Access Source"),
            record.get("Email / Identifier"),
            record.get("Principal Type"),
            record.get("Access Level"),
        )
        if key not in seen:
            seen.add(key)
            deduped.append(record)

    return deduped


def render_access_records(records, empty_message, download_key):
    """Render the access-only API output with a stable column order and CSV download."""
    records = dedupe_access_records(records)

    if not records:
        st.info(empty_message)
        return

    column_order = [
        "Container Type",
        "Container Name",
        "Artifact Type",
        "Artifact Name",
        "Access Source",
        "Principal Name",
        "Email / Identifier",
        "Principal Type",
        "User Type",
        "Access Level",
        "Artifact ID",
    ]

    df_access = pd.DataFrame(records)
    for column in column_order:
        if column not in df_access.columns:
            df_access[column] = "N/A"

    df_access = df_access[column_order]
    display_df_access = _clean_dataframe_for_display(df_access)
    st.dataframe(display_df_access, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download access API output as CSV",
        data=display_df_access.to_csv(index=False).encode("utf-8"),
        file_name="selected_artifact_access.csv",
        mime="text/csv",
        key=download_key,
    )

def get_dashboard_tiles(headers, workspace_id, dashboard_id):
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/dashboards/{dashboard_id}/tiles"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []

def get_app_artifacts(headers, app_id, artifact_type):
    endpoint = "reports" if artifact_type == 'report' else "dashboards"
    url = f"https://api.powerbi.com/v1.0/myorg/apps/{app_id}/{endpoint}"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []

def get_app_users(headers, app_id):
    url = f"https://api.powerbi.com/v1.0/myorg/admin/apps/{app_id}/users"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []


_ANALYSIS_BASE_URL_RE = re.compile(r"https://[A-Za-z0-9.-]+\.analysis\.windows\.net", re.IGNORECASE)
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _unique_preserve_order(values):
    seen = set()
    ordered = []
    for value in values or []:
        text = str(value or "").strip().rstrip("/),;")
        marker = text.lower()
        if text and marker not in seen:
            seen.add(marker)
            ordered.append(text)
    return ordered


def _dedupe_auth_header_candidates(auth_header_candidates):
    """Keep the first label for each unique Authorization header."""
    deduped = []
    seen_tokens = set()
    for identity_name, headers in _normalize_auth_header_candidates(auth_header_candidates):
        token = str(headers.get("Authorization") or "").strip()
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        deduped.append((identity_name, headers))
    return deduped


def _extract_analysis_base_urls(value):
    """Find Power BI regional analysis.windows.net base URLs inside nested data."""
    found = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(_extract_analysis_base_urls(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            found.extend(_extract_analysis_base_urls(child))
    elif value is not None:
        found.extend(match.group(0).rstrip("/") for match in _ANALYSIS_BASE_URL_RE.finditer(str(value)))
    return _unique_preserve_order(found)


def _extract_http_urls(value):
    urls = []
    if isinstance(value, dict):
        for child in value.values():
            urls.extend(_extract_http_urls(child))
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            urls.extend(_extract_http_urls(child))
    elif value is not None:
        urls.extend(match.group(0).rstrip("),;") for match in _HTTP_URL_RE.finditer(str(value)))
    return _unique_preserve_order(urls)


def _records_for_app(app_id, app_name, records):
    app_id_marker = str(app_id or "").strip().lower()
    app_name_marker = str(app_name or "").strip().lower()
    matched = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        record_app_id = str(record.get("App ID") or record.get("appId") or record.get("id") or "").strip().lower()
        record_app_name = str(record.get("App Name") or record.get("appName") or record.get("name") or "").strip().lower()
        same_app_id = app_id_marker and record_app_id == app_id_marker
        same_app_name = app_name_marker and record_app_name == app_name_marker
        if same_app_id or same_app_name:
            matched.append(record)
    return matched


def _candidate_powerbi_urls_for_app(app_id, app_reports=None, app_dashboards=None):
    candidates = []
    if app_id:
        candidates.append(f"https://app.powerbi.com/groups/me/apps/{app_id}")

    for record in list(app_reports or []) + list(app_dashboards or []):
        candidates.extend(_extract_http_urls(record))
        report_id = record.get("Original ID") or record.get("ID")
        if app_id and report_id:
            candidates.extend([
                f"https://app.powerbi.com/groups/me/apps/{app_id}/reports/{report_id}",
                f"https://app.powerbi.com/reportEmbed?reportId={report_id}&appId={app_id}",
            ])

    return _unique_preserve_order(candidates)


def _candidate_internal_appmodel_identifiers(app_id, app_name="", app_records=None, app_report_records=None, app_dashboard_records=None):
    """Return possible IDs accepted by /metadata/appmodel/apps/<id>."""
    records = (
        _records_for_app(app_id, app_name, app_records)
        + _records_for_app(app_id, app_name, app_report_records)
        + _records_for_app(app_id, app_name, app_dashboard_records)
    )
    candidates = []
    seen = set()

    def add(label, value):
        text = str(value or "").strip()
        marker = text.lower()
        if not text or marker in {"n/a", "none", "null", "nan"} or marker in seen:
            return
        seen.add(marker)
        candidates.append({"label": label, "id": text})

    # The browser metadata endpoint often uses provider/workspace identifiers,
    # while public REST app APIs expose app IDs. Try provider/workspace IDs first.
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ["providerId", "Provider_ID", "Workspace ID", "workspaceId", "groupId", "sourceWorkspaceId"]:
            add(key, record.get(key))

    add("Selected App ID", app_id)

    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ["providerKey", "App ID", "appId", "id", "ID", "originalAppId"]:
            add(key, record.get(key))

    return candidates


def discover_internal_metadata_base_url(auth_header_candidates, app_id, app_name="", app_reports=None, app_dashboards=None, timeout=45):
    """Best-effort discovery of the regional analysis.windows.net base for appmodel metadata."""
    app_records = _records_for_app(app_id, app_name, app_reports) + _records_for_app(app_id, app_name, app_dashboards)
    direct_bases = _extract_analysis_base_urls(app_records)
    if direct_bases:
        return {
            "ok": True,
            "base_url": direct_bases[0],
            "identity": "metadata",
            "source_url": "app/report metadata",
            "status_code": "",
            "error": "",
        }

    candidate_urls = _candidate_powerbi_urls_for_app(app_id, app_records, [])
    if not candidate_urls:
        return {
            "ok": False,
            "base_url": "",
            "identity": "",
            "source_url": "",
            "status_code": "",
            "error": "No app/report URLs were available for discovery.",
        }

    attempts = []
    for candidate_url in candidate_urls:
        direct_bases = _extract_analysis_base_urls(candidate_url)
        if direct_bases:
            return {
                "ok": True,
                "base_url": direct_bases[0],
                "identity": "url",
                "source_url": candidate_url,
                "status_code": "",
                "error": "",
            }

        for identity_name, headers in _dedupe_auth_header_candidates(auth_header_candidates):
            request_headers = {
                "Accept": "text/html,application/json,*/*",
                "Authorization": headers.get("Authorization", ""),
                "User-Agent": "Mozilla/5.0 PowerBI-Lineage-AudienceMetadataResolver",
            }
            try:
                response = requests.get(
                    candidate_url,
                    headers=request_headers,
                    timeout=timeout,
                    allow_redirects=True,
                )
            except Exception as exc:
                attempts.append(f"{identity_name} {candidate_url}: {exc}")
                continue

            haystack = {
                "final_url": response.url,
                "location": response.headers.get("Location", ""),
                "body": (response.text or "")[:750000],
            }
            bases = _extract_analysis_base_urls(haystack)
            if bases:
                return {
                    "ok": True,
                    "base_url": bases[0],
                    "identity": identity_name,
                    "source_url": candidate_url,
                    "status_code": response.status_code,
                    "error": "",
                }

            attempts.append(f"{identity_name} {candidate_url}: HTTP {response.status_code}, no analysis.windows.net URL found")

    return {
        "ok": False,
        "base_url": "",
        "identity": "",
        "source_url": "",
        "status_code": "",
        "error": "\n".join(attempts[-8:]) or "Could not discover metadata base URL.",
    }


def get_internal_app_audience_metadata(auth_header_candidates, metadata_base_url, app_identifier, timeout=60):
    """Call the internal Power BI appmodel metadata endpoint for one app.

    This endpoint is not part of the public Power BI REST API, so the base URL is
    provided at runtime and the function returns status details for testing.
    """
    base_url = str(metadata_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("Internal metadata base URL is required.")
    if not app_identifier:
        raise ValueError("App metadata identifier is required.")

    url = f"{base_url}/metadata/appmodel/apps/{app_identifier}?requestDataType=7&access-control-allow-credentials=true"
    errors = []
    for identity_name, headers in _dedupe_auth_header_candidates(auth_header_candidates):
        request_headers = {
            "Accept": "application/json",
            "Authorization": headers.get("Authorization", ""),
        }
        try:
            response = requests.get(url, headers=request_headers, timeout=timeout)
        except Exception as exc:
            errors.append(f"{identity_name}: {exc}")
            continue

        if response.status_code == 200:
            return {
                "ok": True,
                "identity": identity_name,
                "status_code": response.status_code,
                "url": url,
                "app_identifier": app_identifier,
                "json": response.json(),
                "error": "",
            }

        errors.append(f"{identity_name}: HTTP {response.status_code}: {response.text[:1000]}")

    return {
        "ok": False,
        "identity": "",
        "status_code": None,
        "url": url,
        "app_identifier": app_identifier,
        "json": None,
        "error": "\n".join(errors) or "No auth header candidates were available.",
    }


def get_internal_app_audience_metadata_for_identifiers(auth_header_candidates, metadata_base_url, identifier_candidates, timeout=60):
    """Try all possible appmodel path identifiers and return the first successful result."""
    errors = []
    for candidate in identifier_candidates or []:
        app_identifier = candidate.get("id") if isinstance(candidate, dict) else str(candidate or "").strip()
        identifier_label = candidate.get("label", "ID") if isinstance(candidate, dict) else "ID"
        if not app_identifier:
            continue

        result = get_internal_app_audience_metadata(auth_header_candidates, metadata_base_url, app_identifier, timeout=timeout)
        result["app_identifier_label"] = identifier_label
        if result.get("ok"):
            return result
        errors.append(
            f"{identifier_label}={app_identifier}\nURL: {result.get('url')}\n{result.get('error')}"
        )

    return {
        "ok": False,
        "identity": "",
        "status_code": None,
        "url": "",
        "app_identifier": "",
        "app_identifier_label": "",
        "json": None,
        "error": "\n\n".join(errors) or "No appmodel identifier candidates were available.",
    }


def flatten_internal_app_audience_metadata(data, group_id=""):
    """Flatten internal appmodel audience metadata into one row per audience/report/user."""
    if not data:
        return []

    app_items = data if isinstance(data, list) else [data]
    rows = []
    for item in app_items:
        if not isinstance(item, dict):
            continue

        app_views = item.get("appViewDetails") or item.get("views") or []
        if isinstance(app_views, dict):
            app_views = [app_views]

        for view in app_views if isinstance(app_views, list) else []:
            if not isinstance(view, dict):
                continue

            report_ids = view.get("reportIds") or view.get("reports") or []
            if isinstance(report_ids, (str, int)):
                report_ids = [report_ids]
            if not report_ids:
                report_ids = [""]

            permissions = view.get("contentProviderPermissions") or {}
            users = permissions.get("adUserMetadataList") or permissions.get("users") or []
            if isinstance(users, dict):
                users = [users]
            if not users:
                users = [{}]

            for report_id in report_ids:
                for user in users:
                    user = user if isinstance(user, dict) else {}
                    rows.append({
                        "Group": group_id,
                        "Provider_ID": item.get("providerId", ""),
                        "App_ID": item.get("providerKey", ""),
                        "App_Display_Name": item.get("displayText", ""),
                        "Audience_Name": view.get("viewName") or view.get("name") or "",
                        "Report_ID": report_id,
                        "User_Principal_Name": user.get("userPrincipalName", ""),
                        "AD_Display_Name": user.get("displayName", ""),
                        "User_Object_ID": user.get("objectId") or user.get("id") or "",
                    })

    return rows


def get_app_dashboard_tiles(headers, app_id, dashboard_id):
    url = f"https://api.powerbi.com/v1.0/myorg/apps/{app_id}/dashboards/{dashboard_id}/tiles"
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get('value', [])
    return []


# --- APP AUDIENCE HELPERS ---

def _canonicalize_audience_mapping_columns(df):
    """Normalize uploaded audience mapping columns into stable names used by the app view."""
    if df is None or df.empty:
        return pd.DataFrame()

    normalized_lookup = {
        re.sub(r"[^a-z0-9]", "", str(col).strip().lower()): col
        for col in df.columns
    }

    aliases = {
        "App Name": ["appname", "applicationname", "app", "powerbiapp", "powerbiappname"],
        "App ID": ["appid", "applicationid", "appguid", "powerbiappid"],
        "Audience Name": ["audiencename", "audience", "audiencegroup", "audiencetab", "sectionname", "navigationsection"],
        "Audience ID": ["audienceid", "audienceguid", "sectionid", "navigationsectionid"],
        "Artifact Type": ["artifacttype", "objecttype", "itemtype", "contenttype", "type"],
        "Artifact Name": ["artifactname", "objectname", "itemname", "contentname", "name", "reportname", "dashboardname"],
        "Artifact ID": ["artifactid", "objectid", "itemid", "contentid", "id", "reportid", "dashboardid"],
        "Original ID": ["originalid", "originalreportobjectid", "originaldashboardobjectid", "sourceobjectid"],
        "Workspace ID": ["workspaceid", "groupid", "sourceworkspaceid"],
        "Dataset ID": ["datasetid", "semanticmodelid", "modelid"],
        "Visible": ["visible", "isvisible", "included", "enabled", "show", "isactive"],
        "Order": ["order", "sortorder", "position", "index"],
    }

    out = pd.DataFrame()
    for target, keys in aliases.items():
        source_col = None
        for key in keys:
            if key in normalized_lookup:
                source_col = normalized_lookup[key]
                break
        out[target] = df[source_col] if source_col is not None else None

    # Derive Artifact Type when upload has report/dashboard-specific columns but no generic type.
    if "Artifact Type" in out.columns:
        out["Artifact Type"] = out["Artifact Type"].fillna("").astype(str)
    return out


def _json_to_flat_records(data):
    """Best-effort flattening for uploaded audience JSON exports."""
    records = []

    if isinstance(data, list):
        for item in data:
            records.extend(_json_to_flat_records(item))
        return records

    if not isinstance(data, dict):
        return records

    # Direct list-of-dicts shape.
    common_keys = {
        "appName", "appId", "audienceName", "audience", "artifactName", "artifactType",
        "reportName", "dashboardName", "reportId", "dashboardId", "objectId", "id"
    }
    if any(key in data for key in common_keys):
        records.append(data.copy())

    # Common wrappers from manually prepared exports or browser metadata captures.
    for key in ["audiences", "appAudiences", "appViewDetails", "views", "sections", "navigation", "items", "reports", "dashboards", "content"]:
        value = data.get(key)
        if isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    merged = data.copy()
                    merged.pop(key, None)
                    child_records = _json_to_flat_records(child)
                    if child_records:
                        for child_record in child_records:
                            combined = merged.copy()
                            combined.update(child_record)
                            records.append(combined)
                    else:
                        combined = merged.copy()
                        combined.update(child)
                        records.append(combined)
        elif isinstance(value, dict):
            records.extend(_json_to_flat_records(value))

    return records


def parse_app_audience_mapping_upload(uploaded_file):
    """Parse optional CSV/XLSX/JSON audience mapping supplied by the user.

    Power BI public REST APIs return installed app reports/dashboards and flattened app users,
    but they do not expose audience -> object mapping. This upload bridge lets the app keep
    the audience-first UX when the audience mapping is exported/maintained separately.
    """
    if uploaded_file is None:
        return []

    try:
        file_name = uploaded_file.name.lower()
        if file_name.endswith(".csv"):
            raw_df = pd.read_csv(uploaded_file)
        elif file_name.endswith((".xlsx", ".xls")):
            raw_df = pd.read_excel(uploaded_file)
        elif file_name.endswith(".json"):
            data = json.load(uploaded_file)
            raw_df = pd.DataFrame(_json_to_flat_records(data))
        else:
            st.warning("Unsupported audience mapping file type. Upload CSV, Excel, or JSON.")
            return []
    except Exception as exc:
        st.warning(f"Could not read audience mapping file: {exc}")
        return []

    df = _canonicalize_audience_mapping_columns(raw_df)
    if df.empty:
        return []

    records = []
    for _, row in df.iterrows():
        visible_raw = row.get("Visible")
        if visible_raw is not None and str(visible_raw).strip().lower() in {"false", "0", "no", "n", "hidden"}:
            continue

        artifact_type = str(row.get("Artifact Type") or "").strip()
        artifact_name = str(row.get("Artifact Name") or "").strip()

        # Tolerant inference from naming when upload does not include Artifact Type.
        if not artifact_type:
            lower_name = artifact_name.lower()
            if "dashboard" in lower_name:
                artifact_type = "Dashboard"
            else:
                artifact_type = "Report"

        if artifact_type.lower().startswith("dash"):
            artifact_type = "Dashboard"
        elif artifact_type.lower().startswith("rep"):
            artifact_type = "Report"
        else:
            artifact_type = artifact_type.title() or "Report"

        audience_name = str(row.get("Audience Name") or "").strip() or "Unspecified Audience"

        records.append({
            "App Name": str(row.get("App Name") or "").strip(),
            "App ID": str(row.get("App ID") or "").strip(),
            "Audience Name": audience_name,
            "Audience ID": str(row.get("Audience ID") or "").strip() or audience_name,
            "Audience Source": "Uploaded audience mapping",
            "Artifact Type": artifact_type,
            "Artifact Name": artifact_name,
            "Artifact ID": str(row.get("Artifact ID") or "").strip(),
            "Original ID": str(row.get("Original ID") or "").strip(),
            "Workspace ID": str(row.get("Workspace ID") or "").strip(),
            "Dataset ID": str(row.get("Dataset ID") or "").strip(),
            "Order": row.get("Order"),
        })

    return records


def _match_app_artifact(mapping_record, candidates):
    """Find the app artifact record that matches an uploaded audience mapping row."""
    if not candidates:
        return None

    candidate_ids = [
        str(mapping_record.get("Artifact ID") or "").strip().lower(),
        str(mapping_record.get("Original ID") or "").strip().lower(),
    ]
    candidate_ids = [value for value in candidate_ids if value]
    artifact_name = str(mapping_record.get("Artifact Name") or "").strip().lower()
    app_name = str(mapping_record.get("App Name") or "").strip().lower()
    app_id = str(mapping_record.get("App ID") or "").strip().lower()

    for artifact in candidates:
        ids = {
            str(artifact.get("ID") or "").strip().lower(),
            str(artifact.get("Original ID") or "").strip().lower(),
        }
        if candidate_ids and any(value in ids for value in candidate_ids):
            return artifact

    for artifact in candidates:
        same_app = True
        if app_name:
            same_app = str(artifact.get("App Name") or "").strip().lower() == app_name
        if app_id:
            same_app = same_app and str(artifact.get("App ID") or "").strip().lower() == app_id
        same_name = str(artifact.get("Name") or "").strip().lower() == artifact_name
        if same_app and same_name:
            return artifact

    return None


def build_audience_scoped_app_objects(selected_app_names, app_mapping, all_app_reports, all_app_dashboards, uploaded_mapping_records):
    """Build audience-scoped reports/dashboards used by the App Deep Dive flow."""
    selected_app_ids = {str(app_mapping.get(name) or "").strip() for name in selected_app_names}
    selected_app_name_set = {str(name).strip() for name in selected_app_names}

    scoped_reports = []
    scoped_dashboards = []
    unmatched_mapping_rows = []

    if uploaded_mapping_records:
        for record in uploaded_mapping_records:
            rec_app_name = str(record.get("App Name") or "").strip()
            rec_app_id = str(record.get("App ID") or "").strip()
            if rec_app_name and rec_app_name not in selected_app_name_set:
                continue
            if rec_app_id and rec_app_id not in selected_app_ids:
                continue

            artifact_type = record.get("Artifact Type")
            candidates = all_app_reports if artifact_type == "Report" else all_app_dashboards
            matched = _match_app_artifact(record, candidates)
            if not matched:
                unmatched_mapping_rows.append(record)
                continue

            enriched = matched.copy()
            enriched["Audience Name"] = record.get("Audience Name") or "Unspecified Audience"
            enriched["Audience ID"] = record.get("Audience ID") or enriched["Audience Name"]
            enriched["Audience Source"] = record.get("Audience Source") or "Uploaded audience mapping"
            enriched["Audience Order"] = record.get("Order")
            # Use uploaded metadata only when the API artifact did not already return it.
            for field in ["Workspace ID", "Dataset ID", "Original ID"]:
                if not enriched.get(field) and record.get(field):
                    enriched[field] = record.get(field)

            if artifact_type == "Report":
                scoped_reports.append(enriched)
            else:
                scoped_dashboards.append(enriched)
    else:
        for artifact in all_app_reports:
            enriched = artifact.copy()
            enriched["Audience Name"] = "All App Content"
            enriched["Audience ID"] = "ALL_APP_CONTENT"
            enriched["Audience Source"] = "Official app artifact API - audience mapping unavailable"
            scoped_reports.append(enriched)

        for artifact in all_app_dashboards:
            enriched = artifact.copy()
            enriched["Audience Name"] = "All App Content"
            enriched["Audience ID"] = "ALL_APP_CONTENT"
            enriched["Audience Source"] = "Official app artifact API - audience mapping unavailable"
            scoped_dashboards.append(enriched)

    return scoped_reports, scoped_dashboards, unmatched_mapping_rows


def build_app_audience_summary(scoped_reports, scoped_dashboards, all_app_users):
    """Create one row per app audience for the UI."""
    summary = {}

    for artifact_type, records in [("Report", scoped_reports), ("Dashboard", scoped_dashboards)]:
        for item in records or []:
            key = (
                item.get("App Name", "N/A"),
                item.get("App ID", "N/A"),
                item.get("Audience Name", "All App Content"),
                item.get("Audience ID", "ALL_APP_CONTENT"),
                item.get("Audience Source", "N/A"),
            )
            if key not in summary:
                summary[key] = {
                    "App Name": key[0],
                    "App ID": key[1],
                    "Audience Name": key[2],
                    "Audience ID": key[3],
                    "Audience Source": key[4],
                    "Reports": 0,
                    "Dashboards": 0,
                    "Flattened App Principals": 0,
                    "Access Note": "Audience-specific membership is not returned by the public Power BI REST API.",
                }
            if artifact_type == "Report":
                summary[key]["Reports"] += 1
            else:
                summary[key]["Dashboards"] += 1

    users_by_app = {}
    for user in all_app_users or []:
        app_key = (user.get("App Name", "N/A"), user.get("App ID", "N/A"))
        users_by_app[app_key] = users_by_app.get(app_key, 0) + 1

    for row in summary.values():
        row["Flattened App Principals"] = users_by_app.get((row["App Name"], row["App ID"]), 0)

    return list(summary.values())


def render_internal_app_audience_test(
    app_mapping,
    selected_app_names,
    headersMU,
    headersSPA,
    headersSP,
    app_report_records=None,
    app_dashboard_records=None,
    app_records=None,
):
    """Render a tester for the internal appmodel audience metadata endpoint."""
    with st.expander("Internal audience metadata test", expanded=False):
        st.caption("Use this only for testing the internal metadata/appmodel endpoint. The regional metadata base URL is discovered automatically from app/report metadata when possible.")
        if "internal_appmodel_metadata_base_url" not in st.session_state:
            st.session_state["internal_appmodel_metadata_base_url"] = ""
        metadata_base_url = st.text_input(
            "Metadata base URL override (optional)",
            placeholder="Leave blank to auto-discover, or enter https://<region>.analysis.windows.net",
            key="internal_appmodel_metadata_base_url",
        )
        st.caption("Auto-discovery checks app/report metadata, app page URLs, and report embed URLs for the regional analysis.windows.net host.")

        auth_mode_labels = {
            "ServicePrincipal-Admin": "Service Principal Admin",
            "ServicePrincipal": "Service Principal",
            "MasterUser": "Signed-in delegated login",
        }
        selected_auth_modes = st.multiselect(
            "Authentication type",
            options=list(auth_mode_labels.keys()),
            default=["ServicePrincipal-Admin", "ServicePrincipal", "MasterUser"],
            format_func=lambda mode: auth_mode_labels.get(mode, mode),
            key="internal_audience_auth_modes",
            help="The internal metadata endpoint may accept a different token type than the public Power BI REST API.",
        )

        available_apps = list(selected_app_names or [])
        selected_test_apps = render_searchable_multiselect(
            "Select app(s) for audience metadata test",
            available_apps,
            key="internal_audience_test_apps",
            default=available_apps,
        )

        if selected_test_apps:
            candidate_preview_rows = []
            for app_name in selected_test_apps:
                app_id = app_mapping.get(app_name)
                for candidate in _candidate_internal_appmodel_identifiers(
                    app_id,
                    app_name=app_name,
                    app_records=app_records,
                    app_report_records=app_report_records,
                    app_dashboard_records=app_dashboard_records,
                ):
                    candidate_preview_rows.append({
                        "App_Name": app_name,
                        "Candidate_Type": candidate.get("label"),
                        "Candidate_ID": candidate.get("id"),
                    })
            if candidate_preview_rows:
                with st.expander("Appmodel ID candidates", expanded=False):
                    st.dataframe(pd.DataFrame(candidate_preview_rows), use_container_width=True, hide_index=True)

        if st.button("Test audience metadata endpoint", key="internal_audience_test_submit"):
            if not selected_test_apps:
                st.warning("Select at least one app to test.")
                return []
            if not selected_auth_modes:
                st.warning("Select at least one authentication type to test.")
                return []

            auth_candidates = []
            auth_errors = []
            for auth_mode in selected_auth_modes:
                try:
                    if auth_mode == "MasterUser":
                        auth_candidates.append(("MasterUser", headersMU))
                    else:
                        auth_candidates.append((auth_mode, get_confidential_client_auth_header(auth_mode)))
                except Exception as exc:
                    auth_errors.append(f"{auth_mode}: {exc}")

            if auth_errors:
                st.warning("Some authentication types could not be prepared.")
                st.code("\n".join(auth_errors))
            if not auth_candidates:
                st.error("No usable authentication token was available for the metadata test.")
                return []

            flattened_rows = []
            errors = []

            with st.spinner("Fetching internal audience metadata..."):
                for app_name in selected_test_apps:
                    app_id = app_mapping.get(app_name)
                    identifier_candidates = _candidate_internal_appmodel_identifiers(
                        app_id,
                        app_name=app_name,
                        app_records=app_records,
                        app_report_records=app_report_records,
                        app_dashboard_records=app_dashboard_records,
                    )
                    resolved_base_url = str(metadata_base_url or "").strip()
                    discovery = {
                        "identity": "manual override" if resolved_base_url else "",
                        "source_url": "manual override" if resolved_base_url else "",
                        "status_code": "",
                    }
                    if not resolved_base_url:
                        discovery = discover_internal_metadata_base_url(
                            auth_candidates,
                            app_id,
                            app_name=app_name,
                            app_reports=list(app_records or []) + list(app_report_records or []),
                            app_dashboards=app_dashboard_records,
                        )
                        if not discovery.get("ok"):
                            errors.append(f"{app_name}: Could not auto-discover metadata base URL.\n{discovery.get('error')}")
                            continue
                        resolved_base_url = discovery.get("base_url")

                    result = get_internal_app_audience_metadata_for_identifiers(
                        auth_candidates,
                        resolved_base_url,
                        identifier_candidates,
                    )
                    if not result.get("ok"):
                        candidate_text = ", ".join(
                            f"{candidate.get('label')}={candidate.get('id')}"
                            for candidate in identifier_candidates
                        )
                        errors.append(
                            f"{app_name}: Internal audience metadata failed.\n"
                            f"Base URL: {resolved_base_url}\n"
                            f"Tried identifiers: {candidate_text}\n\n"
                            f"{result.get('error')}"
                        )
                        continue

                    rows = flatten_internal_app_audience_metadata(result.get("json"), group_id=result.get("app_identifier") or app_id)
                    for row in rows:
                        row["App_Name"] = app_name
                        row["Token_Identity"] = result.get("identity")
                        row["Metadata_Base_URL"] = resolved_base_url
                        row["Metadata_Path_ID"] = result.get("app_identifier", "")
                        row["Metadata_Path_ID_Type"] = result.get("app_identifier_label", "")
                        row["Metadata_Request_URL"] = result.get("url", "")
                        row["Base_URL_Discovered_By"] = discovery.get("identity", "")
                        row["Base_URL_Source"] = discovery.get("source_url", "")
                    flattened_rows.extend(rows)

            if errors:
                st.error("Some audience metadata calls failed.")
                st.code("\n\n".join(errors))

            if not flattened_rows:
                st.info("No audience metadata rows were returned.")
                return []

            display_df = _clean_dataframe_for_display(pd.DataFrame(flattened_rows))
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download internal audience metadata as CSV",
                data=display_df.to_csv(index=False).encode("utf-8"),
                file_name="internal_app_audience_metadata.csv",
                mime="text/csv",
                key="internal_app_audience_metadata_download",
            )
            return display_df.to_dict("records")

    return []


def filter_app_objects_by_audience(scoped_reports, scoped_dashboards, selected_audience_labels):
    """Return app reports/dashboards for the selected audience labels."""
    selected = set(selected_audience_labels or [])

    def label(item):
        return f"{item.get('App Name', 'N/A')} ➔ {item.get('Audience Name', 'All App Content')}"

    if not selected:
        return [], []

    return (
        [item for item in scoped_reports if label(item) in selected],
        [item for item in scoped_dashboards if label(item) in selected],
    )

def get_report_details(headers, workspace_id, report_id):
    """Return report metadata, including embedUrl, for a workspace report."""
    if not workspace_id or not report_id:
        return {}

    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}"
    try:
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return {}


def resolve_report_embed_url(auth_headers, workspace_id, report_id, existing_embed_url=None):
    """
    Resolve the report embed URL. Existing report inventory data is preferred;
    otherwise the helper tries the provided auth candidates in order.
    """
    if existing_embed_url:
        return existing_embed_url, "Inventory response"

    for identity_name, headers in _normalize_auth_header_candidates(auth_headers):
        details = get_report_details(headers, workspace_id, report_id)
        embed_url = details.get("embedUrl") if isinstance(details, dict) else None
        if embed_url:
            return embed_url, identity_name

    return None, None

def resolve_dataset_for_app_report(headers_sp, original_report_id):
    if not original_report_id:
        return None, None
    url = f"https://api.powerbi.com/v1.0/myorg/admin/reports?$filter=id eq '{original_report_id}'"
    response = requests.get(url, headers=headers_sp)
    
    if response.status_code == 200:
        data = response.json().get('value', [])
        if data:
            return data[0].get('datasetId'), data[0].get('workspaceId')
    return None, None

def get_table_details(headers, dataset_id):
    if not dataset_id:
        return []
        
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"
    query = {"queries": [{"query": "SELECT [TABLE_NAME] FROM $SYSTEM.DBSCHEMA_TABLES WHERE [TABLE_TYPE] = 'TABLE'"}]}
    
    response = requests.post(url, headers=headers, json=query)
    
    if response.status_code == 200:
        rows = response.json()['results'][0]['tables'][0]['rows']
        cleaned_tables = []
        for r in rows:
            t_name = str(r['TABLE_NAME'])
            if t_name.startswith('$'):
                t_name = t_name[1:]
            t_name = t_name.replace("'", "")
            
            if not t_name.startswith(('LocalDate', 'DateTableTemplate', 'Calculation')):
                cleaned_tables.append(t_name)
        return cleaned_tables
    else:
        st.warning(f"Dataset {dataset_id} blocked the table query. (It may be a Live Connection, DirectQuery, or lack permissions).")
        return []

def check_measure_usage_in_report(headers, workspace_id, report_id, measure_names):
    if not measure_names:
        return {}
        
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/Export"
    
    try:
        response = requests.get(url, headers=headers, timeout=60) 
        
        if response.status_code == 200:
            zip_data = io.BytesIO(response.content)
            usage_dict = {}
            
            with zipfile.ZipFile(zip_data) as z:
                if 'Report/Layout' in z.namelist():
                    layout_bytes = z.read('Report/Layout')
                    layout_str = layout_bytes.decode('utf-16-le', errors='ignore')
                    
                    for m in measure_names:
                        if m in layout_str:
                            usage_dict[m] = "Yes"
                        else:
                            usage_dict[m] = "No"
                else:
                    st.warning(f"Exported PBIX for Report {report_id} did not contain a Layout file.")
                    usage_dict = {m: "No Layout Found" for m in measure_names}
                    
            zip_data.close() 
            return usage_dict
            
        elif response.status_code == 403:
            return {m: "API Blocked (403)" for m in measure_names}
        elif response.status_code == 404:
            return {m: "Not Found (404)" for m in measure_names}
        else:
            return {m: f"API Error ({response.status_code})" for m in measure_names}
            
    except requests.exceptions.Timeout:
        return {m: "Timeout Error" for m in measure_names}
    except Exception as e:
        return {m: "System Error" for m in measure_names}


# --- REPORT VISUAL USAGE FUNCTIONS ---

AGGREGATION_FUNCTION_MAP = {
    0: "Sum",
    1: "Average",
    2: "Min",
    3: "Max",
    4: "Count",
    5: "CountNonNull",
    6: "Median",
    7: "StandardDeviation",
    8: "Variance",
}


def _safe_json_loads(value):
    """Safely parse a Power BI layout JSON fragment that may already be a dict/list."""
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return {}

    value = value.strip()
    if not value:
        return {}

    try:
        return json.loads(value)
    except Exception:
        return {}


def _clean_layout_text(value, default="N/A"):
    """Clean Power BI literal text values like '\'Sales Amount\'' into Sales Amount."""
    if value is None:
        return default

    text_value = str(value).strip()
    if not text_value:
        return default

    while len(text_value) >= 2 and text_value[0] == text_value[-1] and text_value[0] in {"'", '"'}:
        text_value = text_value[1:-1].strip()

    text_value = text_value.replace("\\'", "'").replace('\\"', '"')
    return text_value if text_value else default


def _get_nested_value(source, path, default=None):
    """Read a nested dictionary path without raising KeyError."""
    current = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _find_first_literal_value(source):
    """Recursively find the first Power BI Literal.Value from an object."""
    if isinstance(source, dict):
        literal_value = _get_nested_value(source, ["Literal", "Value"])
        if literal_value is not None:
            return literal_value
        for value in source.values():
            found = _find_first_literal_value(value)
            if found is not None:
                return found
    elif isinstance(source, list):
        for item in source:
            found = _find_first_literal_value(item)
            if found is not None:
                return found
    return None


def _extract_visual_title(config, visual_id):
    """Extract the visible visual title from config when available."""
    single_visual = config.get("singleVisual", {}) if isinstance(config, dict) else {}
    title_candidates = [
        _get_nested_value(single_visual, ["vcObjects", "title"]),
        _get_nested_value(single_visual, ["objects", "title"]),
        _get_nested_value(config, ["objects", "title"]) if isinstance(config, dict) else None,
    ]

    for title_config in title_candidates:
        if isinstance(title_config, list):
            for title_item in title_config:
                text_expr = _get_nested_value(title_item, ["properties", "text", "expr"])
                literal = _find_first_literal_value(text_expr)
                if literal is not None:
                    return _clean_layout_text(literal, default=f"Untitled Visual ({visual_id})")
        elif isinstance(title_config, dict):
            text_expr = _get_nested_value(title_config, ["properties", "text", "expr"])
            literal = _find_first_literal_value(text_expr)
            if literal is not None:
                return _clean_layout_text(literal, default=f"Untitled Visual ({visual_id})")

    return f"Untitled Visual ({visual_id})" if visual_id != "N/A" else "Untitled Visual"


def _get_semantic_command(query_config):
    """Return the SemanticQueryDataShapeCommand object from a visual query config."""
    if not isinstance(query_config, dict):
        return {}

    commands = query_config.get("Commands", [])
    if not isinstance(commands, list):
        return {}

    for command in commands:
        semantic_command = command.get("SemanticQueryDataShapeCommand") if isinstance(command, dict) else None
        if isinstance(semantic_command, dict):
            return semantic_command
    return {}


def _build_source_alias_map(semantic_query):
    """Map short source aliases used in layout query JSON to actual model table names."""
    alias_map = {}
    from_items = semantic_query.get("From", []) if isinstance(semantic_query, dict) else []

    for item in from_items if isinstance(from_items, list) else []:
        if not isinstance(item, dict):
            continue
        alias = item.get("Name")
        entity = item.get("Entity") or item.get("Property") or item.get("Name")
        if alias:
            alias_map[alias] = entity

    return alias_map


def _extract_source_table(expression, alias_map):
    """Resolve table name from a field expression using SourceRef.Source alias."""
    if not isinstance(expression, dict):
        return "N/A"

    source_alias = _get_nested_value(expression, ["SourceRef", "Source"])
    if source_alias:
        return alias_map.get(source_alias, source_alias)

    for value in expression.values():
        if isinstance(value, dict):
            table_name = _extract_source_table(value, alias_map)
            if table_name != "N/A":
                return table_name

    return "N/A"


def _extract_field_from_select(select_item, alias_map):
    """Convert one Power BI Select expression into a normalized field usage record."""
    query_ref = select_item.get("Name", "N/A") if isinstance(select_item, dict) else "N/A"

    if not isinstance(select_item, dict):
        return {
            "Field Type": "Unknown",
            "Table Name": "N/A",
            "Column / Measure Name": "N/A",
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "Measure" in select_item:
        node = select_item.get("Measure", {})
        return {
            "Field Type": "Measure",
            "Table Name": _extract_source_table(node.get("Expression", {}), alias_map),
            "Column / Measure Name": node.get("Property", "N/A"),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "Column" in select_item:
        node = select_item.get("Column", {})
        return {
            "Field Type": "Column",
            "Table Name": _extract_source_table(node.get("Expression", {}), alias_map),
            "Column / Measure Name": node.get("Property", "N/A"),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "HierarchyLevel" in select_item:
        node = select_item.get("HierarchyLevel", {})
        return {
            "Field Type": "Hierarchy Level",
            "Table Name": _extract_source_table(node.get("Expression", {}), alias_map),
            "Column / Measure Name": node.get("Level", node.get("Property", "N/A")),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "Aggregation" in select_item:
        aggregation = select_item.get("Aggregation", {})
        function_id = aggregation.get("Function")
        aggregation_name = AGGREGATION_FUNCTION_MAP.get(function_id, str(function_id) if function_id is not None else "Aggregation")
        expression = aggregation.get("Expression", {})

        if isinstance(expression, dict) and "Column" in expression:
            field_record = _extract_field_from_select({"Column": expression.get("Column", {}), "Name": query_ref}, alias_map)
        elif isinstance(expression, dict) and "Measure" in expression:
            field_record = _extract_field_from_select({"Measure": expression.get("Measure", {}), "Name": query_ref}, alias_map)
        else:
            field_record = {
                "Field Type": "Aggregation",
                "Table Name": _extract_source_table(expression, alias_map),
                "Column / Measure Name": query_ref,
                "Aggregation": aggregation_name,
                "Query Reference": query_ref,
            }

        field_record["Field Type"] = f"{aggregation_name} Aggregation"
        field_record["Aggregation"] = aggregation_name
        return field_record

    native_visual_calc = select_item.get("NativeVisualCalculation")
    if isinstance(native_visual_calc, dict):
        return {
            "Field Type": "Visual Calculation",
            "Table Name": "N/A",
            "Column / Measure Name": native_visual_calc.get("Name", query_ref),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    return {
        "Field Type": "Unknown",
        "Table Name": "N/A",
        "Column / Measure Name": query_ref,
        "Aggregation": "N/A",
        "Query Reference": query_ref,
    }


def _build_role_map_from_config(config):
    """Map visual projection queryRef values to Power BI visual field wells/roles."""
    single_visual = config.get("singleVisual", {}) if isinstance(config, dict) else {}
    projections = single_visual.get("projections", {}) if isinstance(single_visual, dict) else {}
    role_map = {}

    if not isinstance(projections, dict):
        return role_map

    for role_name, projection_items in projections.items():
        if not isinstance(projection_items, list):
            continue
        for projection in projection_items:
            if not isinstance(projection, dict):
                continue
            query_ref = projection.get("queryRef") or projection.get("field", {}).get("queryRef")
            if query_ref:
                role_map.setdefault(query_ref, set()).add(role_name)

    return role_map


def _infer_field_from_query_ref(query_ref):
    """Fallback parser when a field exists in projections but not in Select."""
    cleaned = _clean_layout_text(query_ref, default="N/A")
    table_name = "N/A"
    field_name = cleaned
    field_type = "Unknown"
    aggregation = "N/A"

    agg_match = re.match(r"([A-Za-z]+)\((.*)\)", cleaned)
    if agg_match:
        aggregation = agg_match.group(1)
        cleaned = agg_match.group(2)
        field_type = f"{aggregation} Aggregation"

    if "." in cleaned:
        table_name, field_name = cleaned.split(".", 1)

    return {
        "Field Type": field_type,
        "Table Name": table_name,
        "Column / Measure Name": field_name,
        "Aggregation": aggregation,
        "Query Reference": query_ref,
    }


def _parse_report_layout_visuals(layout, report_id):
    """Parse Report/Layout JSON and return one row per visual-field usage."""
    records = []
    sections = layout.get("sections", []) if isinstance(layout, dict) else []

    for page_index, section in enumerate(sections if isinstance(sections, list) else []):
        if not isinstance(section, dict):
            continue

        page_name = section.get("displayName") or section.get("name") or f"Page {page_index + 1}"
        page_id = section.get("name", "N/A")
        visual_containers = section.get("visualContainers", [])

        for visual_index, visual_container in enumerate(visual_containers if isinstance(visual_containers, list) else []):
            container = _safe_json_loads(visual_container)
            if not isinstance(container, dict):
                continue

            config = _safe_json_loads(container.get("config"))
            query_config = _safe_json_loads(container.get("query"))
            single_visual = config.get("singleVisual", {}) if isinstance(config, dict) else {}

            visual_id = config.get("name") or container.get("name") or f"visual_{page_index + 1}_{visual_index + 1}"
            visual_type = single_visual.get("visualType") or config.get("visualType") or "Unknown"
            visual_title = _extract_visual_title(config, visual_id)
            role_map = _build_role_map_from_config(config)

            semantic_command = _get_semantic_command(query_config)
            semantic_query = semantic_command.get("Query", {}) if isinstance(semantic_command, dict) else {}
            alias_map = _build_source_alias_map(semantic_query)
            select_items = semantic_query.get("Select", []) if isinstance(semantic_query, dict) else []

            field_records_by_ref = {}
            for select_item in select_items if isinstance(select_items, list) else []:
                field_record = _extract_field_from_select(select_item, alias_map)
                query_ref = field_record.get("Query Reference", "N/A")
                field_records_by_ref[query_ref] = field_record

            # Include projection-only fields too, because some visuals keep role metadata there.
            for query_ref in role_map.keys():
                if query_ref not in field_records_by_ref:
                    field_records_by_ref[query_ref] = _infer_field_from_query_ref(query_ref)

            base_record = {
                "Report ID": report_id,
                "Page Name": page_name,
                "Page ID": page_id,
                "Visual ID": visual_id,
                "Visual Name": visual_title,
                "Visualization Type": visual_type,
                "Visual X": container.get("x", "N/A"),
                "Visual Y": container.get("y", "N/A"),
                "Visual Width": container.get("width", "N/A"),
                "Visual Height": container.get("height", "N/A"),
            }

            if field_records_by_ref:
                for query_ref, field_record in field_records_by_ref.items():
                    roles = sorted(role_map.get(query_ref, []))
                    records.append({
                        **base_record,
                        "Field Role": ", ".join(roles) if roles else "N/A",
                        **field_record,
                    })
            else:
                records.append({
                    **base_record,
                    "Field Role": "N/A",
                    "Field Type": "No model field detected",
                    "Table Name": "N/A",
                    "Column / Measure Name": "N/A",
                    "Aggregation": "N/A",
                    "Query Reference": "N/A",
                })

    return records


def _get_nested_value(obj, path, default=None):
    """Safely read nested values from dict/list structures."""
    current = obj
    for key in path:
        if isinstance(current, dict):
            current = current.get(key, default)
        elif isinstance(current, list) and isinstance(key, int) and 0 <= key < len(current):
            current = current[key]
        else:
            return default
        if current is default:
            return default
    return current


def _json_literal_to_text(value):
    """Extract readable text from PBIR literal/expression wrappers."""
    if value is None:
        return ""
    if isinstance(value, str):
        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == "'" and cleaned[-1] == "'":
            cleaned = cleaned[1:-1]
        return cleaned.replace("''", "'")
    if isinstance(value, dict):
        for path in (
            ["Literal", "Value"],
            ["literal", "value"],
            ["Value"],
            ["value"],
            ["expr", "Literal", "Value"],
        ):
            found = _get_nested_value(value, path)
            if found:
                return _json_literal_to_text(found)
    return str(value)


def _extract_pbir_visual_title(visual_json, visual_id):
    """Best-effort title extraction from PBIR visual JSON."""
    direct_title = visual_json.get("displayName") or visual_json.get("title")
    if direct_title:
        return _clean_layout_text(direct_title, default=visual_id)

    visual = visual_json.get("visual", {}) if isinstance(visual_json.get("visual"), dict) else visual_json
    objects = visual.get("objects", {}) if isinstance(visual, dict) else {}
    title_obj = objects.get("title") if isinstance(objects, dict) else None

    candidates = []
    if isinstance(title_obj, list):
        candidates.extend(title_obj)
    elif isinstance(title_obj, dict):
        candidates.append(title_obj)

    # Newer PBIR files can store properties in nested dictionaries or lists.
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for path in (
            ["properties", "text", "expr"],
            ["properties", "text"],
            ["text", "expr"],
            ["text"],
        ):
            value = _get_nested_value(candidate, path)
            text_value = _json_literal_to_text(value)
            if text_value:
                return text_value

    return _clean_layout_text(visual_id, default="Untitled / hidden title")


def _extract_source_entity_from_expression(expression):
    """Find the source table/entity from a PBIR field expression."""
    if not isinstance(expression, dict):
        return "N/A"

    source_ref = expression.get("SourceRef") or expression.get("sourceRef")
    if isinstance(source_ref, dict):
        return source_ref.get("Entity") or source_ref.get("entity") or source_ref.get("Source") or "N/A"

    # Some expressions wrap the source inside nested Column/Measure/Aggregation nodes.
    for value in expression.values():
        if isinstance(value, dict):
            found = _extract_source_entity_from_expression(value.get("Expression") or value.get("expression") or value)
            if found != "N/A":
                return found

    return "N/A"


def _aggregation_function_name(function_value):
    """Convert common PBIR aggregation enum values to readable names."""
    enum_map = {
        0: "Sum",
        1: "Average",
        2: "Min",
        3: "Max",
        4: "Count",
        5: "CountNonNull",
        6: "Median",
        7: "StandardDeviation",
        8: "Variance",
    }
    if isinstance(function_value, int):
        return enum_map.get(function_value, str(function_value))
    if isinstance(function_value, str):
        return function_value
    return "N/A"


def _extract_pbir_field(field_obj, query_ref="N/A"):
    """Parse a PBIR field object into the common visual usage column shape."""
    if not isinstance(field_obj, dict):
        return _infer_field_from_query_ref(query_ref)

    # PBIR projections usually contain one of: Column, Measure, Aggregation, HierarchyLevel.
    if "Column" in field_obj or "column" in field_obj:
        column = field_obj.get("Column") or field_obj.get("column") or {}
        expression = column.get("Expression") or column.get("expression") or {}
        table_name = _extract_source_entity_from_expression(expression)
        field_name = column.get("Property") or column.get("property") or query_ref
        return {
            "Field Type": "Column",
            "Table Name": _clean_layout_text(table_name, default="N/A"),
            "Column / Measure Name": _clean_layout_text(field_name, default="N/A"),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "Measure" in field_obj or "measure" in field_obj:
        measure = field_obj.get("Measure") or field_obj.get("measure") or {}
        expression = measure.get("Expression") or measure.get("expression") or {}
        table_name = _extract_source_entity_from_expression(expression)
        field_name = measure.get("Property") or measure.get("property") or query_ref
        return {
            "Field Type": "Measure",
            "Table Name": _clean_layout_text(table_name, default="N/A"),
            "Column / Measure Name": _clean_layout_text(field_name, default="N/A"),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    if "Aggregation" in field_obj or "aggregation" in field_obj:
        aggregation = field_obj.get("Aggregation") or field_obj.get("aggregation") or {}
        expression = aggregation.get("Expression") or aggregation.get("expression") or {}
        function_name = _aggregation_function_name(aggregation.get("Function") or aggregation.get("function"))
        inner = _extract_pbir_field(expression, query_ref)
        inner["Field Type"] = f"{function_name} Aggregation" if function_name != "N/A" else "Aggregation"
        inner["Aggregation"] = function_name
        return inner

    if "HierarchyLevel" in field_obj or "hierarchyLevel" in field_obj:
        hierarchy = field_obj.get("HierarchyLevel") or field_obj.get("hierarchyLevel") or {}
        expression = hierarchy.get("Expression") or hierarchy.get("expression") or {}
        table_name = _extract_source_entity_from_expression(expression)
        field_name = hierarchy.get("Level") or hierarchy.get("level") or hierarchy.get("Property") or query_ref
        return {
            "Field Type": "Hierarchy Level",
            "Table Name": _clean_layout_text(table_name, default="N/A"),
            "Column / Measure Name": _clean_layout_text(field_name, default="N/A"),
            "Aggregation": "N/A",
            "Query Reference": query_ref,
        }

    # Some versions store the real field inside Expression directly.
    expression = field_obj.get("Expression") or field_obj.get("expression")
    if isinstance(expression, dict):
        return _extract_pbir_field(expression, query_ref)

    return _infer_field_from_query_ref(query_ref)


def _position_value(position, key):
    if not isinstance(position, dict):
        return "N/A"
    return position.get(key) or position.get(key.capitalize()) or "N/A"


def _parse_pbir_visual_json(visual_json, page_name, page_id, visual_id, report_id, source_path):
    """Parse one PBIR visual.json file into common visual usage rows."""
    if not isinstance(visual_json, dict):
        return []

    visual = visual_json.get("visual", {}) if isinstance(visual_json.get("visual"), dict) else visual_json
    visual_id = visual_json.get("name") or visual.get("name") or visual_id or "N/A"
    visual_type = visual.get("visualType") or visual.get("type") or visual_json.get("visualType") or "Unknown"
    visual_title = _extract_pbir_visual_title(visual_json, visual_id)
    position = visual_json.get("position") or visual.get("position") or {}

    base_record = {
        "Report ID": report_id,
        "Page Name": page_name,
        "Page ID": page_id,
        "Visual ID": visual_id,
        "Visual Name": visual_title,
        "Visualization Type": visual_type,
        "Visual X": _position_value(position, "x"),
        "Visual Y": _position_value(position, "y"),
        "Visual Width": _position_value(position, "width"),
        "Visual Height": _position_value(position, "height"),
        "Definition Source": source_path,
    }

    query = visual.get("query", {}) if isinstance(visual, dict) else {}
    query_state = query.get("queryState") or query.get("QueryState") or visual.get("queryState") or {}
    records = []

    if isinstance(query_state, dict):
        for role_name, role_value in query_state.items():
            role_obj = role_value if isinstance(role_value, dict) else {}
            projections = role_obj.get("projections") or role_obj.get("Projections") or []
            if isinstance(projections, dict):
                projections = list(projections.values())

            for projection in projections if isinstance(projections, list) else []:
                if not isinstance(projection, dict):
                    continue
                field_obj = projection.get("field") or projection.get("Field") or projection.get("queryRef")
                query_ref = projection.get("queryRef") or projection.get("QueryRef") or projection.get("nativeQueryRef") or role_name
                if isinstance(field_obj, str):
                    field_record = _infer_field_from_query_ref(field_obj)
                else:
                    field_record = _extract_pbir_field(field_obj, query_ref)
                records.append({
                    **base_record,
                    "Field Role": role_name,
                    **field_record,
                })

    if not records:
        # Heuristic fallback: search recursively for projection-like objects with queryRef/field.
        projection_records = []

        def walk(obj, role_hint="N/A"):
            if isinstance(obj, dict):
                current_role = obj.get("role") or obj.get("Role") or role_hint
                if ("queryRef" in obj or "QueryRef" in obj) and ("field" in obj or "Field" in obj):
                    query_ref = obj.get("queryRef") or obj.get("QueryRef") or "N/A"
                    field_record = _extract_pbir_field(obj.get("field") or obj.get("Field"), query_ref)
                    projection_records.append({
                        **base_record,
                        "Field Role": current_role,
                        **field_record,
                    })
                for key, value in obj.items():
                    walk(value, key if key.lower() in {"category", "series", "y", "x", "values", "legend", "tooltips", "tooltip", "group"} else current_role)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, role_hint)

        walk(visual_json)
        records = projection_records

    if not records:
        records.append({
            **base_record,
            "Field Role": "N/A",
            "Field Type": "No model field detected",
            "Table Name": "N/A",
            "Column / Measure Name": "N/A",
            "Aggregation": "N/A",
            "Query Reference": "N/A",
        })

    return records


def _parse_pbir_definition_from_zip(report_zip, report_id, file_name):
    """
    Parse PBIR/PBIP style report definition folders.

    Supports ZIP/PBIX/PBIP exports that contain files like:
      - *.Report/definition/pages/<pageId>/page.json
      - *.Report/definition/pages/<pageId>/visuals/<visualId>/visual.json
    """
    members = report_zip.namelist()
    json_members = [name for name in members if name.lower().endswith(".json")]

    page_json_members = [
        name for name in json_members
        if "/definition/pages/" in ("/" + name.replace("\\", "/").lstrip("/"))
        and name.lower().endswith("/page.json")
    ]
    visual_json_members = [
        name for name in json_members
        if "/definition/pages/" in ("/" + name.replace("\\", "/").lstrip("/"))
        and "/visuals/" in ("/" + name.replace("\\", "/").lstrip("/"))
        and name.lower().endswith("/visual.json")
    ]

    if not visual_json_members:
        return []

    page_map = {}
    for page_path in page_json_members:
        normalized = page_path.replace("\\", "/")
        page_folder = normalized.rsplit("/page.json", 1)[0]
        try:
            page_json = json.loads(report_zip.read(page_path).decode("utf-8-sig"))
        except Exception:
            page_json = {}
        page_id = page_json.get("name") or page_folder.split("/")[-1]
        page_name = page_json.get("displayName") or page_json.get("displayNameExpression") or page_id
        page_order = page_json.get("ordinal") or page_json.get("order") or "N/A"
        page_map[page_folder] = {
            "Page ID": page_id,
            "Page Name": page_name,
            "Page Order": page_order,
        }

    records = []
    for visual_path in visual_json_members:
        normalized = visual_path.replace("\\", "/")
        page_folder = normalized.split("/visuals/", 1)[0]
        visual_id = normalized.split("/visuals/", 1)[1].split("/", 1)[0]
        page_info = page_map.get(page_folder, {})
        page_id = page_info.get("Page ID") or page_folder.split("/")[-1]
        page_name = page_info.get("Page Name") or page_id

        try:
            visual_json = json.loads(report_zip.read(visual_path).decode("utf-8-sig"))
        except Exception:
            continue

        visual_records = _parse_pbir_visual_json(
            visual_json=visual_json,
            page_name=page_name,
            page_id=page_id,
            visual_id=visual_id,
            report_id=report_id,
            source_path=f"{file_name}::{visual_path}",
        )
        for row in visual_records:
            if page_info.get("Page Order") not in (None, "N/A"):
                row["Page Order"] = page_info.get("Page Order")
        records.extend(visual_records)

    return records


def _extract_powerbi_error(response):
    """Return a normalized Power BI REST API error code/message/body."""
    raw_body = response.text or ""
    error_code = f"HTTP_{response.status_code}"
    error_message = raw_body[:1000]

    try:
        payload = response.json()
        error_obj = payload.get("error", {}) if isinstance(payload, dict) else {}
        nested_error = error_obj.get("pbi.error", {}) if isinstance(error_obj, dict) else {}
        error_code = (
            error_obj.get("code")
            or nested_error.get("code")
            or error_code
        )
        error_message = (
            error_obj.get("message")
            or nested_error.get("message")
            or json.dumps(payload, ensure_ascii=False)[:1000]
        )
        raw_body = json.dumps(payload, ensure_ascii=False)[:2000]
    except Exception:
        pass

    if not error_message:
        error_message = f"HTTP {response.status_code}: {getattr(response, 'reason', '')}".strip()
    return error_code, error_message, raw_body[:2000]


def _is_premium_files_export_error(error_code, error_body):
    """Detect the Premium Files model export limitation returned by Power BI."""
    combined = f"{error_code} {error_body}".lower()
    return (
        "premiumfiles" in combined
        or "operationisnotsupportedforpremiumfilesmodel" in combined
    )


def _extract_fabric_error(response):
    raw_body = str(response.text or "")[:2000]
    error_code = f"HTTP_{response.status_code}"
    error_message = raw_body or getattr(response, "reason", "")
    request_id = (
        response.headers.get("requestId")
        or response.headers.get("x-ms-request-id")
        or response.headers.get("ActivityId")
        or ""
    )
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error_code = payload.get("errorCode") or payload.get("code") or error_code
            error_message = payload.get("message") or error_message
            request_id = payload.get("requestId") or request_id
    except Exception:
        pass
    return error_code, error_message, request_id, raw_body


def _fabric_retry_after_seconds(response, default=5):
    try:
        return max(1, min(int(response.headers.get("Retry-After") or default), 30))
    except (TypeError, ValueError):
        return default


def _raise_fabric_response_error(response, operation):
    error_code, error_message, request_id, _ = _extract_fabric_error(response)
    detail = f"{operation}: HTTP {response.status_code} {error_code}: {error_message}".strip()
    if request_id:
        detail += f" (Request ID: {request_id})"
    raise RuntimeError(detail)


def _get_fabric_report_definition(fabric_headers, workspace_id, report_id, report_format=None):
    """Return a report public definition, including Fabric long-running-operation polling."""
    endpoint = f"{_FABRIC_API_BASE_URL}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    params = {}
    normalized_format = str(report_format or "").lower()
    if normalized_format == "pbirlegacy":
        params["format"] = "PBIR-Legacy"
    elif normalized_format == "pbir":
        params["format"] = "PBIR"

    response = requests.post(endpoint, headers=fabric_headers, params=params, timeout=60)
    if response.status_code == 200:
        return response.json()
    if response.status_code != 202:
        _raise_fabric_response_error(response, "Fabric Get Report Definition failed")

    operation_id = str(response.headers.get("x-ms-operation-id") or "").strip()
    operation_url = str(response.headers.get("Location") or "").strip()
    if not operation_url and operation_id:
        operation_url = f"{_FABRIC_API_BASE_URL}/operations/{operation_id}"
    if not operation_url:
        raise RuntimeError("Fabric accepted the report definition request but returned no operation URL.")

    retry_seconds = _fabric_retry_after_seconds(response)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(retry_seconds)
        poll = requests.get(operation_url, headers=fabric_headers, timeout=60)
        if poll.status_code not in (200, 202):
            _raise_fabric_response_error(poll, "Fabric report definition polling failed")

        try:
            operation = poll.json()
        except Exception:
            operation = {}
        if isinstance(operation, dict) and isinstance(operation.get("definition"), dict):
            return operation

        status = str(operation.get("status") or "").lower() if isinstance(operation, dict) else ""
        if status == "succeeded":
            result_url = str(poll.headers.get("Location") or "").strip()
            if not result_url or result_url.rstrip("/") == operation_url.rstrip("/"):
                if not operation_id:
                    raise RuntimeError("Fabric completed report definition retrieval without a result URL.")
                result_url = f"{_FABRIC_API_BASE_URL}/operations/{operation_id}/result"
            result = requests.get(result_url, headers=fabric_headers, timeout=60)
            if result.status_code != 200:
                _raise_fabric_response_error(result, "Fabric report definition result failed")
            return result.json()
        if status in {"failed", "cancelled"}:
            raise RuntimeError(f"Fabric report definition operation {status}: {json.dumps(operation.get('error') or {})}")
        retry_seconds = _fabric_retry_after_seconds(poll, retry_seconds)

    raise RuntimeError("Fabric report definition retrieval did not finish within five minutes.")


def _fabric_definition_zip_bytes(payload):
    definition = payload.get("definition") if isinstance(payload, dict) else None
    if not isinstance(definition, dict):
        raise RuntimeError("Fabric response did not contain a report definition.")
    parts = definition.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Fabric report definition contained no parts.")

    decoded_parts = []
    seen_paths = set()
    for part in parts:
        if not isinstance(part, dict) or part.get("payloadType") != "InlineBase64":
            raise RuntimeError("Fabric returned an unsupported report definition part.")
        raw_path = str(part.get("path") or "").replace("\\", "/").strip("/")
        part_path = PurePosixPath(raw_path)
        if not raw_path or part_path.is_absolute() or ".." in part_path.parts:
            raise RuntimeError("Fabric returned an unsafe report definition part path.")
        encoded = str(part.get("payload") or "").strip()
        encoded += "=" * (-len(encoded) % 4)
        decoded_parts.append((str(part_path), base64.b64decode(encoded, validate=True)))
        if str(part_path) in seen_paths:
            raise RuntimeError(f"Fabric returned a duplicate report definition part: {part_path}")
        seen_paths.add(str(part_path))

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part_path, part_bytes in decoded_parts:
            archive.writestr(part_path, part_bytes)
    return archive_buffer.getvalue(), str(definition.get("format") or "not returned"), len(decoded_parts)


def _parse_fabric_report_definition(payload, report_id):
    archive_bytes, definition_format, part_count = _fabric_definition_zip_bytes(payload)
    definition_file = io.BytesIO(archive_bytes)
    definition_file.name = f"{report_id}-fabric-report-definition.zip"
    records = parse_uploaded_report_layout(definition_file, report_id=report_id)
    usable_records = [
        record for record in records or []
        if isinstance(record, dict)
        and str(record.get("Visual ID") or "").strip().lower() not in {"", "n/a", "none"}
    ]
    if not usable_records:
        detail = "; ".join(
            str(record.get("Error Detail") or record.get("Status") or "")
            for record in records or []
            if isinstance(record, dict)
        )
        raise RuntimeError(f"Fabric definition was downloaded but no visual metadata could be parsed. {detail}".strip())
    parsed_records = []
    for record in usable_records:
        row = dict(record)
        row["Metadata Source"] = f"Fabric Get Report Definition ({definition_format})"
        row["Status"] = "Visual metadata parsed automatically from the Fabric report definition"
        row["Definition Parts"] = part_count
        parsed_records.append(row)
    return parsed_records


def _normalize_auth_header_candidates(auth_headers):
    """
    Accept either one headers dict or a list of (identity_name, headers) tuples.
    This lets the visual extractor try MasterUser first, then SP/SPA, instead of failing
    just because the first token type cannot export PBIX layout.
    """
    candidates = []

    if isinstance(auth_headers, dict):
        candidates.append(("ProvidedAuth", auth_headers))
    elif isinstance(auth_headers, (list, tuple)):
        for idx, item in enumerate(auth_headers):
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], dict):
                candidates.append((str(item[0]), item[1]))
            elif isinstance(item, dict):
                candidates.append((f"Auth{idx + 1}", item))

    clean_candidates = []
    for name, headers in candidates:
        if isinstance(headers, dict) and headers.get("Authorization"):
            clean_candidates.append((name, headers))

    return clean_candidates


def _format_powerbi_attempt_errors(errors):
    """Create a compact readable message for multiple auth/download attempts."""
    if not errors:
        return "No detailed Power BI error was returned."

    parts = []
    for error in errors:
        identity = error.get("identity", "UnknownAuth")
        source_name = error.get("source_name", "Unknown API")
        status_code = error.get("status_code", "N/A")
        error_code = error.get("error_code") or "HTTP_ERROR"
        error_message = error.get("error_message") or error.get("error_body") or "No message"
        parts.append(f"{identity} / {source_name} => HTTP {status_code} / {error_code}: {error_message}")

    return " | ".join(parts[:6])


def _get_with_auth_fallback(url, auth_headers, timeout=60):
    """Try the same GET URL with all configured identities and return the first successful response."""
    errors = []
    candidates = _normalize_auth_header_candidates(auth_headers)

    if not candidates:
        return None, None, [{
            "identity": "N/A",
            "source_name": "Auth validation",
            "status_code": "N/A",
            "error_code": "MissingAuthHeaders",
            "error_message": "No valid Authorization header was provided.",
            "error_body": "",
            "is_premium_files_error": False,
        }]

    for identity_name, headers in candidates:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response, identity_name, errors

            error_code, error_message, error_body = _extract_powerbi_error(response)
            errors.append({
                "identity": identity_name,
                "source_name": url.split("/")[-1].split("?")[0] or "GET",
                "status_code": response.status_code,
                "error_code": error_code,
                "error_message": error_message,
                "error_body": error_body,
                "is_premium_files_error": _is_premium_files_export_error(error_code, error_body),
            })
        except requests.exceptions.Timeout:
            errors.append({
                "identity": identity_name,
                "source_name": url.split("/")[-1].split("?")[0] or "GET",
                "status_code": "TIMEOUT",
                "error_code": "Timeout",
                "error_message": "The request timed out.",
                "error_body": "",
                "is_premium_files_error": False,
            })
        except Exception as e:
            errors.append({
                "identity": identity_name,
                "source_name": url.split("/")[-1].split("?")[0] or "GET",
                "status_code": "ERROR",
                "error_code": type(e).__name__,
                "error_message": str(e),
                "error_body": "",
                "is_premium_files_error": False,
            })

    return None, None, errors


def get_report_page_metadata_only(headers, workspace_id, report_id, status, error_detail):
    """
    Fallback when PBIX/Report Layout export is blocked.

    The supported REST surface can still return report pages, but not visual field mappings.
    This function returns API-safe rows so the Streamlit table does not crash and users can
    clearly see why visual-level columns are unavailable.
    """
    if not workspace_id or not report_id:
        return [{
            "Report ID": report_id or "N/A",
            "Metadata Source": "REST fallback",
            "Status": status,
            "Error Detail": error_detail,
        }]

    pages_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/pages"

    try:
        response, identity_name, errors = _get_with_auth_fallback(pages_url, headers, timeout=60)
        if response is None:
            return [{
                "Report ID": report_id,
                "Metadata Source": "REST fallback",
                "Status": status,
                "Error Detail": f"{error_detail} | Page fallback also failed: {_format_powerbi_attempt_errors(errors)}",
            }]

        pages = response.json().get("value", [])
        if not pages:
            return [{
                "Report ID": report_id,
                "Metadata Source": f"REST fallback ({identity_name})",
                "Status": status,
                "Error Detail": f"{error_detail} | Page fallback succeeded but no pages were returned.",
            }]

        fallback_rows = []
        for page in pages:
            fallback_rows.append({
                "Report ID": report_id,
                "Metadata Source": f"REST pages API fallback ({identity_name})",
                "Page Name": page.get("displayName") or page.get("name") or "N/A",
                "Page ID": page.get("name", "N/A"),
                "Page Order": page.get("order", "N/A"),
                "Visual ID": "N/A",
                "Visual Name": "Unavailable from supported REST API",
                "Visualization Type": "Unavailable from supported REST API",
                "Field Role": "N/A",
                "Field Type": "Unavailable from supported REST API",
                "Table Name": "N/A",
                "Column / Measure Name": "N/A",
                "Aggregation": "N/A",
                "Query Reference": "N/A",
                "Status": status,
                "Error Detail": error_detail,
            })
        return fallback_rows

    except Exception as e:
        return [{
            "Report ID": report_id,
            "Metadata Source": "REST fallback",
            "Status": status,
            "Error Detail": f"{error_detail} | Page fallback failed: {str(e)}",
        }]


def _download_report_layout_package(auth_headers, workspace_id, report_id):
    """
    Try to download the report package in a layout-friendly way.

    Important: the Power BI service UI download often works with the delegated user, while
    service-principal tokens can still get 403. Therefore this function supports multiple
    identities and should receive MasterUser first.
    """
    base_url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/reports/{report_id}/Export"
    attempts = [
        ("Export Report API - LiveConnect", f"{base_url}?downloadType=LiveConnect&preferClientRouting=true"),
        ("Export Report API - IncludeModel", f"{base_url}?downloadType=IncludeModel&preferClientRouting=true"),
        ("Export Report API - default", f"{base_url}?preferClientRouting=true"),
    ]

    all_errors = []
    candidates = _normalize_auth_header_candidates(auth_headers)

    if not candidates:
        return None, None, {
            "source_name": "Auth validation",
            "status_code": "N/A",
            "error_code": "MissingAuthHeaders",
            "error_message": "No valid Authorization header was provided.",
            "error_body": "",
            "is_premium_files_error": False,
            "all_errors": [],
        }

    for identity_name, headers in candidates:
        for source_name, url in attempts:
            try:
                response = requests.get(url, headers=headers, timeout=180)
                if response.status_code == 200:
                    return response.content, f"{identity_name} - {source_name}", None

                error_code, error_message, error_body = _extract_powerbi_error(response)
                all_errors.append({
                    "identity": identity_name,
                    "source_name": source_name,
                    "status_code": response.status_code,
                    "error_code": error_code,
                    "error_message": error_message,
                    "error_body": error_body,
                    "is_premium_files_error": _is_premium_files_export_error(error_code, error_body),
                })
            except requests.exceptions.Timeout:
                all_errors.append({
                    "identity": identity_name,
                    "source_name": source_name,
                    "status_code": "TIMEOUT",
                    "error_code": "Timeout",
                    "error_message": "The export request timed out.",
                    "error_body": "",
                    "is_premium_files_error": False,
                })
            except Exception as e:
                all_errors.append({
                    "identity": identity_name,
                    "source_name": source_name,
                    "status_code": "ERROR",
                    "error_code": type(e).__name__,
                    "error_message": str(e),
                    "error_body": "",
                    "is_premium_files_error": False,
                })

    last_error = all_errors[-1] if all_errors else {}
    return None, None, {
        "source_name": last_error.get("source_name"),
        "status_code": last_error.get("status_code"),
        "error_code": last_error.get("error_code"),
        "error_message": last_error.get("error_message"),
        "error_body": last_error.get("error_body"),
        "is_premium_files_error": any(e.get("is_premium_files_error") for e in all_errors),
        "all_errors": all_errors,
    }


def get_report_visual_usage(headers, workspace_id, report_id, fabric_headers=None, report_format=None):
    """
    Return report visual and field usage rows.

    Preferred path:
    - Retrieve the public report definition through the Linux-compatible Fabric API.
    - Parse PBIR-Legacy report.json or PBIR definition files.

    Fallback path:
    - Try PBIX Export / Report Layout, then page-level metadata with diagnostics.
    """
    if not workspace_id or not report_id:
        return [{
            "Report ID": report_id or "N/A",
            "Metadata Source": "N/A",
            "Status": "Missing Workspace ID or Report ID",
            "Error Detail": "Workspace ID and Report ID are required to inspect report visuals.",
        }]

    fabric_error = None
    if fabric_headers:
        try:
            definition_payload = _get_fabric_report_definition(
                fabric_headers,
                workspace_id,
                report_id,
                report_format=report_format,
            )
            return _parse_fabric_report_definition(definition_payload, report_id)
        except Exception as exc:
            fabric_error = str(exc)

    try:
        package_bytes, metadata_source, export_error = _download_report_layout_package(headers, workspace_id, report_id)

        if package_bytes is None:
            attempt_detail = ""
            if export_error and export_error.get("all_errors"):
                attempt_detail = f" Auth attempts: {_format_powerbi_attempt_errors(export_error.get('all_errors'))}"
            if fabric_error:
                attempt_detail += f" Fabric definition attempt: {fabric_error}"

            if export_error and export_error.get("is_premium_files_error"):
                return get_report_page_metadata_only(
                    headers,
                    workspace_id,
                    report_id,
                    "Visual layout export blocked - Premium Files model",
                    "Power BI returned ServerError_PremiumFilesErrors_OperationIsNotSupportedForPremiumFilesModel. "
                    "The app cannot extract visual type or visual column/measure usage from the supported REST API "
                    f"when Report/Layout export is blocked.{attempt_detail}",
                )

            if export_error:
                return get_report_page_metadata_only(
                    headers,
                    workspace_id,
                    report_id,
                    f"Export API Error ({export_error.get('status_code')})",
                    f"{export_error.get('error_code')}: {export_error.get('error_message')}.{attempt_detail}",
                )

            return get_report_page_metadata_only(
                headers,
                workspace_id,
                report_id,
                "Export API Error",
                "Report/Layout export failed for an unknown reason.",
            )

        with zipfile.ZipFile(io.BytesIO(package_bytes)) as report_zip:
            layout_member = next((name for name in report_zip.namelist() if name.endswith("Report/Layout")), None)
            if not layout_member:
                return get_report_page_metadata_only(
                    headers,
                    workspace_id,
                    report_id,
                    "No Report/Layout Found",
                    "The exported report package did not contain Report/Layout, so visual-level fields could not be parsed.",
                )

            layout_bytes = report_zip.read(layout_member)
            layout_str = layout_bytes.decode("utf-16-le", errors="ignore")
            layout_json = json.loads(layout_str)

        visual_records = _parse_report_layout_visuals(layout_json, report_id)
        if not visual_records:
            return get_report_page_metadata_only(
                headers,
                workspace_id,
                report_id,
                "No Visuals Found in Report/Layout",
                "Report/Layout was parsed, but no visual containers were found.",
            )

        return [
            {
                "Metadata Source": metadata_source,
                "Status": "Visual metadata parsed successfully",
                **record,
            }
            for record in visual_records
        ]

    except zipfile.BadZipFile:
        return get_report_page_metadata_only(
            headers,
            workspace_id,
            report_id,
            "Invalid Export File",
            "The Export API response was not a valid PBIX zip package.",
        )
    except Exception as e:
        return get_report_page_metadata_only(
            headers,
            workspace_id,
            report_id,
            "System Error",
            str(e),
        )


def _visual_usage_block_reason(records):
    """Return a human-friendly reason when API-based visual metadata extraction is blocked."""
    combined = " ".join(
        str(row.get("Status", "")) + " " + str(row.get("Error Detail", ""))
        for row in records
        if isinstance(row, dict)
    ).lower()

    if "http_403" in combined or "export api error (403)" in combined or "forbidden" in combined:
        return (
            "Power BI returned HTTP 403 for the report export endpoint. The app can only show page metadata from "
            "the supported REST pages API. Visual type and column/measure mapping require Report/Layout access, "
            "which Power BI is blocking for this report or for the current identity."
        )

    if "premiumfiles" in combined or "operationisnotsupportedforpremiumfilesmodel" in combined:
        return (
            "Power BI blocked Report/Layout export for a Premium/Fabric-backed model. The supported REST pages API "
            "does not expose visual field mappings, so only page metadata can be shown."
        )

    if "unavailable from supported rest api" in combined:
        return (
            "Only report page metadata is available through the supported REST fallback. Visual-level fields are not "
            "returned by the pages API."
        )

    return ""


def _add_visual_usage_guidance(records):
    """Add diagnostic columns so fallback rows explain exactly what needs to be fixed."""
    reason = _visual_usage_block_reason(records)
    if not reason:
        return records

    guidance = (
        "To get Visual Name, Visualization Type and used columns/measures, enable/allow report PBIX export for the "
        "current identity, run with an account that is at least Contributor on the report workspace and source semantic "
        "model workspace, or use the manual PBIX/Report Layout upload fallback below."
    )

    updated = []
    for row in records:
        if isinstance(row, dict):
            row_copy = row.copy()
            row_copy.setdefault("Visual Metadata Availability", "Blocked")
            row_copy.setdefault("Why Visual Fields Are Missing", reason)
            row_copy.setdefault("Recommended Action", guidance)
            updated.append(row_copy)
        else:
            updated.append(row)
    return updated


def _decode_report_layout_bytes(raw_bytes):
    """Decode a Report/Layout or report JSON payload using common Power BI encodings."""
    for encoding in ("utf-16-le", "utf-8-sig", "utf-8"):
        try:
            decoded = raw_bytes.decode(encoding)
            return json.loads(decoded)
        except Exception:
            continue
    return None


def parse_uploaded_report_layout(uploaded_file, report_id="Manual Upload"):
    """
    Parse manually uploaded PBIX/ZIP/PBIP/PBIR/Report Layout JSON.

    Priority:
    1. Legacy PBIX Report/Layout parser.
    2. PBIR/PBIP definition/pages/.../visuals/.../visual.json parser.
    3. Raw Report/Layout JSON parser.
    """
    if uploaded_file is None:
        return []

    file_name = getattr(uploaded_file, "name", "uploaded_report_layout")
    raw = uploaded_file.getvalue()
    layout_json = None
    layout_source = file_name
    pbir_records = []
    discovered_members = []

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as report_zip:
            members = report_zip.namelist()
            discovered_members = members[:50]

            # First try legacy PBIX layout/report.json style.
            legacy_candidates = [
                name for name in members
                if name.endswith("Report/Layout")
                or name.endswith("report.json")
                or name.endswith("definition/report.json")
            ]

            for candidate in legacy_candidates:
                candidate_json = _decode_report_layout_bytes(report_zip.read(candidate))
                if isinstance(candidate_json, dict):
                    layout_json = candidate_json
                    layout_source = f"{file_name}::{candidate}"
                    break

            if isinstance(layout_json, dict):
                visual_records = _parse_report_layout_visuals(layout_json, report_id)
                if visual_records:
                    return [
                        {
                            "Metadata Source": "Manual upload fallback - Legacy Report/Layout",
                            "Manual Layout Source": layout_source,
                            "Status": "Visual metadata parsed successfully from uploaded legacy layout",
                            **record,
                        }
                        for record in visual_records
                    ]

            # Then try PBIR/PBIP enhanced report format.
            pbir_records = _parse_pbir_definition_from_zip(report_zip, report_id, file_name)
            if pbir_records:
                return [
                    {
                        "Metadata Source": "Manual upload fallback - PBIR/PBIP definition",
                        "Manual Layout Source": record.pop("Definition Source", file_name),
                        "Status": "Visual metadata parsed successfully from PBIR/PBIP definition files",
                        **record,
                    }
                    for record in pbir_records
                ]

    except zipfile.BadZipFile:
        layout_json = _decode_report_layout_bytes(raw)
    except Exception as e:
        return [{
            "Report ID": report_id,
            "Metadata Source": "Manual upload fallback",
            "Status": "Manual upload parse failed",
            "Error Detail": str(e),
        }]

    # Raw JSON fallback.
    if isinstance(layout_json, dict):
        visual_records = _parse_report_layout_visuals(layout_json, report_id)
        if visual_records:
            return [
                {
                    "Metadata Source": "Manual upload fallback - Raw Report/Layout JSON",
                    "Manual Layout Source": layout_source,
                    "Status": "Visual metadata parsed successfully from uploaded JSON",
                    **record,
                }
                for record in visual_records
            ]

    hint = "Upload one of: manually downloaded .pbix, report-only PBIX, PBIP/PBIR ZIP folder, extracted Report/Layout JSON, or PBIR definition ZIP."
    if discovered_members:
        sample = ", ".join(discovered_members[:12])
        hint += f" First ZIP members seen: {sample}"

    return [{
        "Report ID": report_id,
        "Metadata Source": "Manual upload fallback",
        "Status": "No supported report definition found",
        "Error Detail": hint,
    }]

def render_powerbi_js_visual_scanner(
    report_label,
    workspace_id,
    report_id,
    access_token,
    embed_url,
    component_key,
    dataset_id=None,
    height=980,
):
    """
    Render a browser-side Power BI JavaScript Embed API scanner.

    This scanner does not depend on PBIX Export / Report Layout download. It loads
    the report in the browser using the delegated MasterUser token and calls
    report.getPages(), page.getVisuals(), and visual.exportData() where allowed.
    """
    if not report_id or not workspace_id:
        st.warning("Report ID and Workspace ID are required for the JavaScript Embed scanner.")
        return

    if not access_token:
        st.warning("A signed-in Power BI access token is required for the JavaScript Embed scanner.")
        return

    if not embed_url:
        st.warning(
            "Could not resolve the report embed URL. Confirm the signed-in account can open the report "
            "and that the report metadata API returns embedUrl."
        )
        return

    row_limit = st.number_input(
        "Rows to sample from each visual export",
        min_value=1,
        max_value=30000,
        value=200,
        step=100,
        key=f"{component_key}_row_limit",
        help="Used only for visual.exportData(). The scanner reads the CSV headers to infer visible fields/columns.",
    )
    include_export_data = st.checkbox(
        "Try visual.exportData() to infer visible columns/measures",
        value=True,
        key=f"{component_key}_include_export_data",
        help="If tenant/report settings block export data, the scanner still returns page, visual name, visual title, type and layout.",
    )

    report_label_safe = html.escape(str(report_label or report_id))
    component_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(component_key))

    html_payload = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8" />
  <script src="https://cdn.jsdelivr.net/npm/powerbi-client@2.23.1/dist/powerbi.min.js"></script>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 0;
      color: #1f2937;
      background: #ffffff;
    }}
    .wrap {{ padding: 12px; }}
    .notice {{
      border: 1px solid #d1d5db;
      background: #f9fafb;
      border-radius: 10px;
      padding: 10px 12px;
      margin-bottom: 12px;
      font-size: 13px;
      line-height: 1.4;
    }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }}
    button {{
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      background: #ffffff;
      padding: 8px 12px;
      cursor: pointer;
      font-weight: 600;
    }}
    button.primary {{ background: #111827; color: #ffffff; border-color: #111827; }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    #status_{component_id} {{ font-size: 13px; margin: 8px 0; white-space: pre-wrap; }}
    #reportContainer_{component_id} {{
      height: 430px;
      width: 100%;
      border: 1px solid #d1d5db;
      border-radius: 12px;
      overflow: hidden;
      background: #f3f4f6;
    }}
    .tableWrap {{ max-height: 410px; overflow: auto; border: 1px solid #e5e7eb; border-radius: 12px; margin-top: 12px; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 7px 8px; text-align: left; vertical-align: top; }}
    th {{ position: sticky; top: 0; background: #f9fafb; z-index: 1; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="notice">
      <b>Power BI JS Embed Scanner:</b> {report_label_safe}<br />
      This runs in your browser using the signed-in access token. It can read page + visual descriptors even when PBIX export is blocked.
      Columns/measures are inferred from <code>visual.exportData()</code> CSV headers when Power BI allows visual data export.
    </div>

    <div class="toolbar">
      <button class="primary" id="loadBtn_{component_id}">Load report</button>
      <button id="scanBtn_{component_id}" disabled>Scan visuals</button>
      <button id="downloadBtn_{component_id}" disabled>Download JS scan CSV</button>
      <span class="muted">Report ID: {html.escape(str(report_id))}</span>
    </div>

    <div id="status_{component_id}">Waiting to load report...</div>
    <div id="reportContainer_{component_id}"></div>

    <div class="tableWrap">
      <table id="resultsTable_{component_id}">
        <thead>
          <tr>
            <th>Page Name</th>
            <th>Page ID</th>
            <th>Page Order</th>
            <th>Visual Title</th>
            <th>Visual Name / ID</th>
            <th>Visual Type</th>
            <th>Visible Fields From exportData()</th>
            <th>Export Status</th>
            <th>Layout</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

<script>
(function() {{
  const reportId = {json.dumps(str(report_id))};
  const datasetId = {json.dumps(str(dataset_id or ""))};
  const embedUrl = {json.dumps(str(embed_url))};
  const accessToken = {json.dumps(str(access_token))};
  const rowLimit = {int(row_limit)};
  const includeExportData = {str(bool(include_export_data)).lower()};
  const statusEl = document.getElementById("status_{component_id}");
  const reportContainer = document.getElementById("reportContainer_{component_id}");
  const loadBtn = document.getElementById("loadBtn_{component_id}");
  const scanBtn = document.getElementById("scanBtn_{component_id}");
  const downloadBtn = document.getElementById("downloadBtn_{component_id}");
  const tbody = document.querySelector("#resultsTable_{component_id} tbody");
  let report = null;
  let pbiModels = null;
  let scannedRows = [];

  function setStatus(message) {{ statusEl.textContent = message; }}
  function escapeHtml(value) {{
    return String(value ?? "").replace(/[&<>'"]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}}[ch]));
  }}
  function parseCsvLine(line) {{
    const values = [];
    let current = "";
    let inQuotes = false;
    for (let i = 0; i < line.length; i++) {{
      const ch = line[i];
      const next = line[i + 1];
      if (ch === '"' && inQuotes && next === '"') {{ current += '"'; i++; continue; }}
      if (ch === '"') {{ inQuotes = !inQuotes; continue; }}
      if (ch === ',' && !inQuotes) {{ values.push(current.trim()); current = ""; continue; }}
      current += ch;
    }}
    values.push(current.trim());
    return values.filter(v => v !== "");
  }}
  function extractCsvHeader(csvText) {{
    if (!csvText) return [];
    const firstDataLine = String(csvText).split(/\r?\n/).find(line => line.trim().length > 0) || "";
    return parseCsvLine(firstDataLine);
  }}
  function errorText(error) {{
    if (!error) return "Unknown error";
    if (typeof error === "string") return error;
    if (error.message) return error.message;
    try {{ return JSON.stringify(error); }} catch (_) {{ return String(error); }}
  }}
  function csvEscape(value) {{
    const text = String(value ?? "");
    return '"' + text.replace(/"/g, '""') + '"';
  }}
  function downloadCsv() {{
    const headers = ["Page Name","Page ID","Page Order","Visual Title","Visual Name / ID","Visual Type","Visible Fields From exportData()","Export Status","Layout","Report ID","Dataset ID"];
    const lines = [headers.map(csvEscape).join(",")];
    scannedRows.forEach(row => {{
      lines.push([
        row.pageName, row.pageId, row.pageOrder, row.visualTitle, row.visualName, row.visualType,
        row.visibleFields, row.exportStatus, row.layout, reportId, datasetId
      ].map(csvEscape).join(","));
    }});
    const blob = new Blob([lines.join("\n")], {{ type: "text/csv;charset=utf-8;" }});
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `powerbi_js_visual_scan_${{reportId}}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  }}
  function renderRows() {{
    tbody.innerHTML = "";
    scannedRows.forEach(row => {{
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${{escapeHtml(row.pageName)}}</td>
        <td>${{escapeHtml(row.pageId)}}</td>
        <td>${{escapeHtml(row.pageOrder)}}</td>
        <td>${{escapeHtml(row.visualTitle)}}</td>
        <td>${{escapeHtml(row.visualName)}}</td>
        <td>${{escapeHtml(row.visualType)}}</td>
        <td>${{escapeHtml(row.visibleFields)}}</td>
        <td>${{escapeHtml(row.exportStatus)}}</td>
        <td>${{escapeHtml(row.layout)}}</td>`;
      tbody.appendChild(tr);
    }});
    downloadBtn.disabled = scannedRows.length === 0;
  }}
  async function loadReport() {{
    try {{
      const pbi = window.powerbi;
      const models = (window["powerbi-client"] && window["powerbi-client"].models) || (pbi && pbi.models);
      if (!pbi || !models) {{ throw new Error("powerbi-client JavaScript library did not load. Check internet/CDN access."); }}
      pbiModels = models;

      pbi.reset(reportContainer);
      const config = {{
        type: "report",
        id: reportId,
        embedUrl: embedUrl,
        accessToken: accessToken,
        tokenType: models.TokenType.Aad,
        permissions: models.Permissions.Read,
        settings: {{
          panes: {{
            filters: {{ visible: false }},
            pageNavigation: {{ visible: true }}
          }},
          background: models.BackgroundType.Transparent
        }}
      }};

      report = pbi.embed(reportContainer, config);
      setStatus("Loading embedded report...");
      report.on("loaded", function() {{
        setStatus("Report loaded. Click 'Scan visuals'.");
        scanBtn.disabled = false;
      }});
      report.on("error", function(event) {{
        setStatus("Power BI embed error: " + errorText(event && event.detail));
      }});
    }} catch (error) {{
      setStatus("Load failed: " + errorText(error));
    }}
  }}
  async function scanVisuals() {{
    if (!report) {{ setStatus("Load the report first."); return; }}
    scanBtn.disabled = true;
    scannedRows = [];
    renderRows();
    try {{
      setStatus("Reading report pages...");
      const pages = await report.getPages();
      let totalVisuals = 0;
      for (const page of pages) {{
        setStatus(`Scanning page: ${{page.displayName || page.name}}`);
        try {{ await page.setActive(); }} catch (_) {{ /* Some contexts can block page activation; continue. */ }}
        const visuals = await page.getVisuals();
        totalVisuals += visuals.length;
        for (const visual of visuals) {{
          const layout = visual.layout || {{}};
          let visibleFields = "Not requested";
          let exportStatus = "Skipped";
          if (includeExportData) {{
            try {{
              const exported = await visual.exportData(pbiModels.ExportDataType.Summarized, rowLimit);
              const headers = extractCsvHeader(exported && exported.data);
              visibleFields = headers.length ? headers.join(" | ") : "No CSV header returned";
              exportStatus = "exportData summarized success";
            }} catch (exportError) {{
              visibleFields = "Unavailable from exportData()";
              exportStatus = "exportData failed: " + errorText(exportError);
            }}
          }}
          scannedRows.push({{
            pageName: page.displayName || page.name || "N/A",
            pageId: page.name || "N/A",
            pageOrder: page.order ?? "N/A",
            visualTitle: visual.title || "Untitled / hidden title",
            visualName: visual.name || "N/A",
            visualType: visual.type || "N/A",
            visibleFields: visibleFields,
            exportStatus: exportStatus,
            layout: `x=${{layout.x ?? "N/A"}}, y=${{layout.y ?? "N/A"}}, w=${{layout.width ?? "N/A"}}, h=${{layout.height ?? "N/A"}}, visible=${{layout.visibility ?? "N/A"}}`
          }});
          renderRows();
        }}
      }}
      setStatus(`Scan completed. Pages: ${{pages.length}}, visuals: ${{totalVisuals}}, rows: ${{scannedRows.length}}.`);
    }} catch (error) {{
      setStatus("Scan failed: " + errorText(error));
    }} finally {{
      scanBtn.disabled = false;
    }}
  }}

  loadBtn.addEventListener("click", loadReport);
  scanBtn.addEventListener("click", scanVisuals);
  downloadBtn.addEventListener("click", downloadCsv);
}})();
</script>
</body>
</html>
"""
    components.html(html_payload, height=height, scrolling=True)


def render_js_visual_scanner_section(
    report_options,
    headers_candidates,
    master_user_token,
    section_key,
    default_expanded=False,
):
    """Render a one-report-at-a-time Power BI JS Embed scanner section."""
    if not report_options:
        return

    with st.expander("🌐 Power BI JS Embed API scanner", expanded=default_expanded):
        st.info(
            "Use this when PBIX/Report Layout export is blocked but the same signed-in account can open/download the report. "
            "It embeds the report in the browser and scans pages/visuals using Power BI JavaScript APIs."
        )
        selected_label = render_searchable_single_select(
            "Choose one report to scan with JavaScript Embed API",
            options=list(report_options.keys()),
            key=f"{section_key}_selected_js_report",
        )
        selected = report_options[selected_label]
        workspace_id = selected.get("Workspace ID")
        report_id = selected.get("Report ID") or selected.get("ID") or selected.get("Original ID")
        dataset_id = selected.get("Dataset ID")
        existing_embed_url = selected.get("Embed URL")

        cache_key_embed_url = f"js_embed_url_{workspace_id}_{report_id}"
        if cache_key_embed_url not in st.session_state:
            st.session_state[cache_key_embed_url] = resolve_report_embed_url(
                headers_candidates,
                workspace_id,
                report_id,
                existing_embed_url,
            )

        embed_url, resolved_by = st.session_state.get(cache_key_embed_url, (None, None))
        if resolved_by:
            st.caption(f"Embed URL resolved by: {resolved_by}")

        render_powerbi_js_visual_scanner(
            selected_label,
            workspace_id,
            report_id,
            master_user_token,
            embed_url,
            f"{section_key}_{report_id}",
            dataset_id=dataset_id,
        )


def render_manual_visual_layout_upload(report_label, report_id_hint, upload_key):
    """Render an optional manual fallback for blocked REST export visual metadata."""
    with st.expander("Manual fallback when REST export is blocked", expanded=False):
        st.info(
            "Your REST/JS calls can be blocked by Power BI or browser embedding rules. "
            "Because you can manually download the report, upload that PBIX here. This parser now supports both legacy Report/Layout and PBIR/PBIP definition files."
        )
        uploaded_file = st.file_uploader(
            f"Upload PBIX / PBIP ZIP / PBIR definition ZIP / Report Layout JSON for {report_label}",
            type=["pbix", "zip", "json", "txt", "pbip"],
            key=upload_key,
        )

        if uploaded_file is not None:
            manual_records = parse_uploaded_report_layout(uploaded_file, report_id_hint or "Manual Upload")
            render_visual_usage_records(
                manual_records,
                "No visual metadata found in uploaded file.",
                f"{upload_key}_download",
            )

def render_visual_usage_records(records, empty_message, download_key):
    """Render report visual usage rows and expose a CSV download."""
    if not records:
        st.info(empty_message)
        return

    records = _add_visual_usage_guidance(records)
    block_reason = _visual_usage_block_reason(records)
    if block_reason:
        st.warning(block_reason)

    preferred_columns = [
        "Workspace",
        "App Name",
        "Report",
        "Dataset ID",
        "Report ID",
        "Metadata Source",
        "Definition Parts",
        "Page Name",
        "Page ID",
        "Visual Name",
        "Visualization Type",
        "Field Role",
        "Field Type",
        "Table Name",
        "Column / Measure Name",
        "Aggregation",
        "Query Reference",
        "Visual ID",
        "Visual X",
        "Visual Y",
        "Visual Width",
        "Visual Height",
    ]

    df_visuals = pd.DataFrame(records)
    if "Report" not in df_visuals.columns and "Source Report" in df_visuals.columns:
        df_visuals["Report"] = df_visuals["Source Report"]
    ordered_columns = [column for column in preferred_columns if column in df_visuals.columns]
    df_visuals = df_visuals[ordered_columns]

    display_df_visuals = _clean_dataframe_for_display(df_visuals)
    st.dataframe(display_df_visuals, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download report visual usage as CSV",
        data=display_df_visuals.to_csv(index=False).encode("utf-8"),
        file_name="selected_report_visual_usage.csv",
        mime="text/csv",
        key=download_key,
    )

def get_measure_details(headers, dataset_id):
    if not dataset_id:
        return []
        
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"
    
    query_measures = {"queries": [{"query": "SELECT [MEASUREGROUP_NAME], [MEASURE_NAME], [EXPRESSION] FROM $SYSTEM.MDSCHEMA_MEASURES"}]}
    resp = requests.post(url, headers=headers, json=query_measures)
    
    if resp.status_code == 200:
        measure_rows = resp.json()['results'][0]['tables'][0]['rows']
        
        clean_measures = []
        for r in measure_rows:
            m_name = r.get('MEASURE_NAME', '')
            if not m_name.startswith('__') and m_name != 'FormatString':
                clean_measures.append({
                    "Home Table": r.get('MEASUREGROUP_NAME', 'Unknown'),
                    "Measure Name": m_name,
                    "DAX Expression": r.get('EXPRESSION', '')
                })
        return clean_measures
    else:
        st.error(f"Error fetching measures for Dataset {dataset_id}: {resp.text}")
        return None

# --- XMLA LINEAGE FUNCTIONS ---

def _powerbi_get_json_from_candidates(auth_headers, url, timeout=30):
    """Try the same Power BI REST URL with one or more auth headers and return JSON from the first successful call."""
    for identity_name, headers in _normalize_auth_header_candidates(auth_headers):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response.json(), identity_name, response.status_code
        except Exception:
            continue
    return None, None, None


def _build_auth_header_candidates(primary_headers=None, extra_headers=None):
    """Build a de-duplicated list of auth candidates for metadata resolution."""
    raw_candidates = []
    if isinstance(primary_headers, dict):
        raw_candidates.append(("Primary", primary_headers))
    if isinstance(extra_headers, (list, tuple)):
        raw_candidates.extend(extra_headers)
    elif isinstance(extra_headers, dict):
        raw_candidates.append(("Extra", extra_headers))

    clean = []
    seen = set()
    for name, headers in _normalize_auth_header_candidates(raw_candidates):
        token = headers.get("Authorization")
        if not token or token in seen:
            continue
        seen.add(token)
        clean.append((name, headers))
    return clean


def resolve_workspace_name_for_xmla(headers_spa, workspace_id, workspace_name_hint=None, auth_headers=None):
    """Resolve a workspace name for XMLA, with selected UI value as a reliable fallback."""
    hinted_name = _prefer_non_na(workspace_name_hint)
    if hinted_name != "N/A":
        return hinted_name

    if not workspace_id:
        return None

    candidates = _build_auth_header_candidates(headers_spa, auth_headers)

    # Normal workspace API. Works for identities that have workspace membership.
    data, _, _ = _powerbi_get_json_from_candidates(
        candidates,
        f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}",
    )
    if isinstance(data, dict) and data.get("name"):
        return data.get("name")

    # Admin workspace API fallback. Works for Fabric/Power BI admin or allowed service principal.
    data, _, _ = _powerbi_get_json_from_candidates(
        candidates,
        f"https://api.powerbi.com/v1.0/myorg/admin/groups?$filter=id eq '{workspace_id}'",
    )
    if isinstance(data, dict):
        values = data.get("value", [])
        if values and values[0].get("name"):
            return values[0].get("name")

    return None


def resolve_dataset_name_for_xmla(headers_spa, workspace_id, dataset_id, dataset_name_hint=None, auth_headers=None):
    """Resolve a semantic model/dataset name for XMLA using workspace and admin API fallbacks."""
    hinted_name = _prefer_non_na(dataset_name_hint)
    if hinted_name != "N/A":
        return hinted_name

    if not dataset_id:
        return None

    candidates = _build_auth_header_candidates(headers_spa, auth_headers)

    # Workspace-scoped dataset API: this is often more reliable than admin datasets when multiple reports are selected.
    if workspace_id:
        data, _, _ = _powerbi_get_json_from_candidates(
            candidates,
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets/{dataset_id}",
        )
        if isinstance(data, dict) and data.get("name"):
            return data.get("name")

        data, _, _ = _powerbi_get_json_from_candidates(
            candidates,
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets",
        )
        if isinstance(data, dict):
            for dataset in data.get("value", []) or []:
                if str(dataset.get("id", "")).lower() == str(dataset_id).lower() and dataset.get("name"):
                    return dataset.get("name")

    # Admin dataset API fallback. Useful for app reports where only the original report id is known.
    data, _, _ = _powerbi_get_json_from_candidates(
        candidates,
        f"https://api.powerbi.com/v1.0/myorg/admin/datasets?$filter=id eq '{dataset_id}'",
    )
    if isinstance(data, dict):
        values = data.get("value", [])
        if values and values[0].get("name"):
            return values[0].get("name")

    # Last fallback: try the dataset GUID as catalog. Some environments accept it; if not, XMLA connection will fail clearly.
    return dataset_id


def resolve_names_for_xmla(headers_spa, workspace_id, dataset_id, workspace_name_hint=None, dataset_name_hint=None, auth_headers=None):
    """
    Resolve workspace and semantic model names used in the XMLA connection string.

    v15 fix: when multiple reports/datasets are selected, do not depend only on the
    admin datasets endpoint. Resolve names using workspace-scoped APIs first, then
    admin APIs, and finally selected UI hints. This prevents one unresolved dataset
    from breaking mixed native/non-native lineage output.
    """
    ws_name = resolve_workspace_name_for_xmla(
        headers_spa,
        workspace_id,
        workspace_name_hint=workspace_name_hint,
        auth_headers=auth_headers,
    )
    ds_name = resolve_dataset_name_for_xmla(
        headers_spa,
        workspace_id,
        dataset_id,
        dataset_name_hint=dataset_name_hint,
        auth_headers=auth_headers,
    )
    return ws_name, ds_name


def _xmla_workspace_url_candidates(workspace_name):
    raw_name = str(workspace_name or "").strip()
    encoded_name = quote(raw_name, safe="")
    urls = [f"powerbi://api.powerbi.com/v1.0/myorg/{raw_name}"]
    encoded_url = f"powerbi://api.powerbi.com/v1.0/myorg/{encoded_name}"
    if encoded_url not in urls:
        urls.append(encoded_url)
    return urls


def _remember_xmla_connection_error(workspace_name, dataset_name, error_message):
    try:
        st.session_state["_last_xmla_connection_error"] = {
            "workspace": workspace_name,
            "dataset": dataset_name,
            "error": str(error_message or "").strip(),
        }
    except Exception:
        pass


def _last_xmla_connection_error():
    try:
        return st.session_state.get("_last_xmla_connection_error") or {}
    except Exception:
        return {}


def get_xmla_cursor(workspace_name, dataset_name, access_token):
    """
    Establishes a connection to the Power BI XMLA endpoint.
    Returns a tuple: (connection, cursor). 
    Make sure to close both when finished!
    """
    # Forcefully strip "Bearer " if it exists
    raw_token = access_token.replace("Bearer ", "").strip()

    last_error = None
    for workspace_url in _xmla_workspace_url_candidates(workspace_name):
        conn_str = (
            f"Provider=MSOLAP;"
            f"Data Source={workspace_url};"
            f"Initial Catalog={dataset_name};"
            f"Password={raw_token};"
        )

        try:
            conn = connect_xmla(conn_str)
            cursor = conn.cursor()
            return conn, cursor
        except Exception as e:
            last_error = e
            print(f"XMLA Connection Failed for {workspace_url}: {e}")

    _remember_xmla_connection_error(workspace_name, dataset_name, last_error)
    return None, None

def execute_xmla_query(cursor, query):
    """
    Takes an active XMLA cursor and a query string.
    Executes the query, safely parses the headers, and returns a list of rows.
    """
    try:
        cursor.execute(query)
        raw_rows = cursor.fetchall()
        return raw_rows
    except Exception as e:
        print(f"Query Execution Failed: {str(e)}")
        return None

def _first_regex_group(pattern, text, default="N/A"):
    """Return the first regex group found in text, otherwise a default value."""
    match = re.search(pattern, text or "", re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else default


def _clean_source_value(value):
    """Normalize source values used in lineage output."""
    if value is None:
        return "N/A"
    value = str(value).strip().strip('"').strip("'")
    return value if value else "N/A"


def _prefer_non_na(*values):
    """Return the first meaningful value that is not blank/N/A."""
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.upper() not in {"N/A", "NA", "NONE", "NULL", "NAN"}:
            return value
    return "N/A"


def _is_meaningful_value(value):
    """Return True when a value should be displayed instead of being treated as blank/N/A."""
    return _prefer_non_na(value) != "N/A"


def _unique_meaningful_values(values):
    """Return unique non-empty/non-N/A values while preserving the first-seen order."""
    result = []
    seen = set()
    for value in values or []:
        cleaned = _clean_source_value(value)
        if not _is_meaningful_value(cleaned):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _join_unique_meaningful(values):
    """Join unique meaningful values with a semicolon for UI display."""
    unique_values = _unique_meaningful_values(values)
    return "; ".join(unique_values) if unique_values else "N/A"


def _build_fully_qualified_name(database=None, schema=None, object_name=None):
    """Build a safe database.schema.object name without returning None/null."""
    parts = []
    for value in (database, schema, object_name):
        cleaned = _clean_source_value(value)
        if _is_meaningful_value(cleaned):
            parts.append(cleaned)
    return ".".join(parts) if parts else "N/A"


def _fully_qualified_names_from_native_sources(native_sources, fallback_db="N/A"):
    """Build one or more FQNs from native SQL FROM/JOIN references."""
    fqns = []
    for ref in native_sources or []:
        db_name = _prefer_non_na(ref.get("database"), fallback_db)
        schema_name = ref.get("schema")
        object_name = ref.get("object")
        fqn = _build_fully_qualified_name(db_name, schema_name, object_name)
        if _is_meaningful_value(fqn):
            fqns.append(fqn)
    return _join_unique_meaningful(fqns)



def _m_string_unescape(value):
    """Decode common Power Query M string escaping inside quoted strings."""
    if value is None:
        return ""
    text = str(value)
    replacements = {
        '#(lf)': '\n',
        '#(cr)': '\r',
        '#(tab)': '\t',
        '#(cr,lf)': '\r\n',
        '#(lf,cr)': '\n\r',
    }
    for old, new in replacements.items():
        text = re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
    return text.replace('""', '"').strip()


def _normalise_sql_for_display(sql_text):
    """Keep native SQL readable while removing excessive blank lines/indentation."""
    if not sql_text:
        return "N/A"
    lines = [line.rstrip() for line in str(sql_text).replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    # Drop leading/trailing blank lines but preserve the query structure.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return '\n'.join(lines).strip() or "N/A"


def _extract_value_native_query(m_code):
    """Extract the SQL text from Value.NativeQuery(source, "...") when present."""
    m_code = str(m_code or "")
    match = re.search(
        r'Value\.NativeQuery\s*\(\s*[^,]+\s*,\s*"((?:[^"]|"")*)"',
        m_code,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "N/A"
    return _normalise_sql_for_display(_m_string_unescape(match.group(1)))


def _clean_sql_identifier(identifier):
    """Normalize SQL identifiers found in a native query."""
    if identifier is None:
        return "N/A"
    value = str(identifier).strip().strip(',;')
    value = re.sub(r'\s+', ' ', value)
    if value.startswith('('):
        return "N/A"
    value = value.strip('`').strip('"').strip('[]')
    return value or "N/A"


def _split_sql_identifier(identifier):
    """Split db.schema.table style identifiers while respecting simple quoting."""
    identifier = str(identifier or "").strip().strip(',;')
    if not identifier or identifier.startswith('('):
        return []
    parts = []
    current = []
    quote = None
    bracket = False
    for ch in identifier:
        if bracket:
            current.append(ch)
            if ch == ']':
                bracket = False
            continue
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', '`'):
            quote = ch
            current.append(ch)
            continue
        if ch == '[':
            bracket = True
            current.append(ch)
            continue
        if ch == '.':
            parts.append(_clean_sql_identifier(''.join(current)))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append(_clean_sql_identifier(''.join(current)))
    return [part for part in parts if part and part != "N/A"]


def _extract_native_query_sources(native_query, default_db="N/A"):
    """Return source table/view references found after FROM/JOIN in a native SQL query."""
    if not native_query or native_query == "N/A":
        return []

    source_refs = []
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+((?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)){0,2})',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(native_query):
        raw_identifier = match.group(1)
        if raw_identifier.strip().startswith('('):
            continue
        parts = _split_sql_identifier(raw_identifier)
        if not parts:
            continue

        db_name = default_db if default_db and default_db != "N/A" else "N/A"
        schema_name = "N/A"
        object_name = parts[-1]

        if len(parts) == 3:
            db_name, schema_name, object_name = parts[-3], parts[-2], parts[-1]
        elif len(parts) == 2:
            schema_name, object_name = parts[-2], parts[-1]

        source_refs.append({
            "database": db_name,
            "schema": schema_name,
            "object": object_name,
            "raw": '.'.join(parts),
        })

    # Preserve order and remove duplicates.
    unique = []
    seen = set()
    for ref in source_refs:
        key = (ref.get("database"), ref.get("schema"), ref.get("object"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def _extract_select_columns_from_native_query(native_query):
    """Best-effort extraction of SELECT list expressions from a native SQL query."""
    if not native_query or native_query == "N/A":
        return "N/A"
    match = re.search(r'\bSELECT\b(.*?)\bFROM\b', native_query, re.IGNORECASE | re.DOTALL)
    if not match:
        return "N/A"
    select_text = match.group(1)
    columns = []
    current = []
    depth = 0
    quote = None
    for ch in select_text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'", '`'):
            quote = ch
            current.append(ch)
            continue
        if ch == '(':
            depth += 1
        elif ch == ')' and depth > 0:
            depth -= 1
        if ch == ',' and depth == 0:
            value = ''.join(current).strip()
            if value:
                columns.append(value)
            current = []
            continue
        current.append(ch)
    if current:
        value = ''.join(current).strip()
        if value:
            columns.append(value)
    return '; '.join(columns) if columns else "N/A"



def _split_native_select_expressions(native_query):
    """Return individual SELECT expressions from a native SQL query.

    Example:
        SELECT DISTINCT EMPLOYEE_ID, GROSS_PAY + NET_PAY TOTAL_PAY
        -> ["EMPLOYEE_ID", "GROSS_PAY + NET_PAY TOTAL_PAY"]
    """
    if not native_query or native_query == "N/A":
        return []

    match = re.search(r'\bSELECT\b(.*?)\bFROM\b', str(native_query), re.IGNORECASE | re.DOTALL)
    if not match:
        return []

    select_text = match.group(1)
    expressions = []
    current = []
    depth = 0
    quote = None

    for ch in select_text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'", '`'):
            quote = ch
            current.append(ch)
            continue
        if ch == '(':
            depth += 1
        elif ch == ')' and depth > 0:
            depth -= 1
        if ch == ',' and depth == 0:
            expr = ''.join(current).strip().strip(',')
            if expr:
                expressions.append(expr)
            current = []
            continue
        current.append(ch)

    expr = ''.join(current).strip().strip(',')
    if expr:
        expressions.append(expr)

    if expressions:
        expressions[0] = re.sub(r'^\s*(DISTINCT|ALL)\s+', '', expressions[0], flags=re.IGNORECASE).strip()
    return [expr for expr in expressions if expr]


def _strip_sql_string_literals(sql_expr):
    """Remove string literals from SQL expression before identifier extraction."""
    if not sql_expr:
        return ""
    # Replace single-quoted strings. Handles doubled quotes inside strings well enough for lineage parsing.
    text = re.sub(r"'(?:''|[^'])*'", " ", str(sql_expr))
    # Replace double-quoted string/identifier wrappers with content; identifiers are handled later.
    return text


def _is_sql_keyword(token):
    """Return True when a token is a SQL keyword/function word, not a source column."""
    if token is None:
        return True
    return str(token).upper() in {
        "SELECT", "DISTINCT", "ALL", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "ON",
        "AS", "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE",
        "BETWEEN", "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "QUALIFY", "OVER", "PARTITION",
        "ROW", "ROWS", "RANGE", "CURRENT", "PRECEDING", "FOLLOWING", "ASC", "DESC", "TRUE", "FALSE",
        "CAST", "CONVERT", "TRY_CAST", "DATE", "DATE_TRUNC", "TO_DATE", "TO_TIMESTAMP", "SUM", "AVG", "MIN", "MAX",
        "COUNT", "COUNT_DISTINCT", "COALESCE", "NVL", "IFNULL", "IFF", "ROUND", "ABS", "UPPER", "LOWER", "TRIM",
        "LTRIM", "RTRIM", "CONCAT", "SUBSTR", "SUBSTRING", "REPLACE", "REGEXP_REPLACE"
    }


def _extract_native_select_alias_and_columns(select_expression):
    """Map one native SELECT expression to its output alias and true source columns.

    Examples:
        EMPLOYEE_ID -> alias EMPLOYEE_ID, columns EMPLOYEE_ID
        GROSS_PAY + NET_PAY TOTAL_PAY -> alias TOTAL_PAY, columns GROSS_PAY; NET_PAY
        FIRST_NAME||' '||LAST_NAME FULL_NAME -> alias FULL_NAME, columns FIRST_NAME; LAST_NAME
    """
    original_expr = str(select_expression or "").strip().strip(',;')
    if not original_expr:
        return "N/A", []

    expr = re.sub(r'^\s*(DISTINCT|ALL)\s+', '', original_expr, flags=re.IGNORECASE).strip()
    alias = "N/A"
    expr_without_alias = expr

    # Explicit alias: <expression> AS <alias>
    explicit_alias = re.search(r'\s+AS\s+("[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)\s*$', expr, re.IGNORECASE | re.DOTALL)
    if explicit_alias:
        alias = _clean_sql_identifier(explicit_alias.group(1))
        expr_without_alias = expr[:explicit_alias.start()].strip()
    else:
        # Implicit alias: <expression> <alias>. Only treat the final token as alias when
        # the expression contains operators/parentheses/whitespace before it.
        implicit_alias = re.search(r'\s+("[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)\s*$', expr, re.DOTALL)
        if implicit_alias:
            candidate = _clean_sql_identifier(implicit_alias.group(1))
            before = expr[:implicit_alias.start()].strip()
            if before and before != candidate and not before.endswith('.'):
                # Avoid misclassifying a plain qualified column as alias-only.
                if re.search(r'[\s+\-*/|()]', before):
                    alias = candidate
                    expr_without_alias = before

    if not _is_meaningful_value(alias):
        # No alias; use the last identifier/property in the expression as output name.
        parts = _split_sql_identifier(expr)
        alias = parts[-1] if parts else _clean_sql_identifier(expr)

    expression_for_columns = _strip_sql_string_literals(expr_without_alias)

    # Extract identifier chains like A.B.C or plain COLUMN. For qualified references,
    # keep only the last component because that is the physical column name.
    identifier_pattern = re.compile(
        r'(?:(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)\s*\.\s*)*(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)',
        re.IGNORECASE,
    )

    source_columns = []
    seen = set()
    for match in identifier_pattern.finditer(expression_for_columns):
        raw_identifier = match.group(0).strip()
        if not raw_identifier:
            continue

        # Skip function names followed by opening parenthesis.
        next_chars = expression_for_columns[match.end(): match.end() + 5]
        parts = _split_sql_identifier(raw_identifier)
        token = parts[-1] if parts else _clean_sql_identifier(raw_identifier)
        if not _is_meaningful_value(token):
            continue
        if next_chars.lstrip().startswith('(') and len(parts) == 1:
            continue
        if _is_sql_keyword(token):
            continue
        if re.fullmatch(r'\d+', token):
            continue

        key = token.lower()
        if key not in seen:
            seen.add(key)
            source_columns.append(token)

    # For plain EMPLOYEE_ID, the column extractor should return EMPLOYEE_ID.
    if not source_columns and _is_meaningful_value(alias):
        source_columns = [alias]

    return _clean_source_value(alias), source_columns


def _extract_native_query_column_map(native_query):
    """Build {semantic/alias column -> actual physical column(s)} from native SQL SELECT list."""
    if not native_query or native_query == "N/A":
        return {}

    column_map = {}
    for expr in _split_native_select_expressions(native_query):
        alias, source_columns = _extract_native_select_alias_and_columns(expr)
        if not _is_meaningful_value(alias):
            continue
        source_value = _join_unique_meaningful(source_columns) if source_columns else alias
        column_map[_normalise_name_for_join(alias)] = source_value
        # Also allow direct lookup by physical source columns.
        for source_col in source_columns:
            column_map.setdefault(_normalise_name_for_join(source_col), source_col)
    return column_map


def _resolve_native_actual_column(source_row, semantic_column):
    """Return actual native SQL source column(s) for a semantic alias when available."""
    if not isinstance(source_row, dict):
        return "N/A"
    source_type = str(source_row.get("Source Type") or "")
    if "native" not in source_type.lower():
        return "N/A"

    column_map = source_row.get("Native Query Column Map") or {}
    if isinstance(column_map, str):
        try:
            column_map = json.loads(column_map)
        except Exception:
            column_map = {}
    if not isinstance(column_map, dict):
        return "N/A"

    return _prefer_non_na(
        column_map.get(_normalise_name_for_join(semantic_column)),
        column_map.get(_normalise_name_for_join(str(semantic_column or '').split('.')[-1])),
    )

def _parse_power_query_source(m_code):
    """
    Extract source database/schema/table/view/native-query details from Power Query M.

    This version is deliberately defensive for mixed selections where one report uses
    normal navigation tables and another report uses Value.NativeQuery. It always
    returns the same keys and never returns None for display columns.
    """
    m_code = str(m_code or "")

    server_name = "N/A"
    db_name = "N/A"
    schema_name = "N/A"
    source_name = "N/A"
    source_type = "Unknown"

    native_query = _extract_value_native_query(m_code)
    native_query_columns = _extract_select_columns_from_native_query(native_query)

    # Common navigation table pattern:
    # Source{[Name="DB",Kind="Database"]}[Data]{[Name="SCHEMA",Kind="Schema"]}[Data]{[Name="TABLE",Kind="Table"]}[Data]
    nav_database = _first_regex_group(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"Database"\s*\]', m_code)
    nav_schema = _first_regex_group(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"Schema"\s*\]', m_code)
    nav_table = _first_regex_group(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"Table"\s*\]', m_code)
    nav_view = _first_regex_group(r'\[\s*Name\s*=\s*"([^"]+)"\s*,\s*Kind\s*=\s*"View"\s*\]', m_code)

    # Sql.Database("server", "database") pattern.
    sql_match = re.search(r'Sql\.Database\(\s*"([^"]+)"\s*,\s*"([^"]+)"', m_code, re.IGNORECASE | re.DOTALL)
    if sql_match:
        server_name = _clean_source_value(sql_match.group(1))
        db_name = _clean_source_value(sql_match.group(2))

    # Snowflake.Databases("account", "warehouse") pattern. The DB normally appears in navigation.
    snowflake_match = re.search(r'Snowflake\.Databases\(\s*"([^"]+)"\s*,\s*"([^"]+)"', m_code, re.IGNORECASE | re.DOTALL)
    if snowflake_match:
        server_name = _clean_source_value(snowflake_match.group(1))

    db_name = _prefer_non_na(nav_database, db_name)
    schema_name = _prefer_non_na(nav_schema, schema_name)

    native_sources = []
    if _is_meaningful_value(native_query):
        native_sources = _extract_native_query_sources(native_query, default_db=db_name)
        if native_sources:
            db_name = _join_unique_meaningful([_prefer_non_na(ref.get("database"), db_name) for ref in native_sources])
            schema_name = _join_unique_meaningful([ref.get("schema") for ref in native_sources])
            source_name = _join_unique_meaningful([ref.get("object") for ref in native_sources])
            fqn = _fully_qualified_names_from_native_sources(native_sources, fallback_db=nav_database if _is_meaningful_value(nav_database) else db_name)
        else:
            source_name = "Native Query"
            fqn = _build_fully_qualified_name(db_name, schema_name, source_name)
        source_type = "Native Query"
    elif _is_meaningful_value(nav_table):
        source_name = nav_table
        source_type = "Table"
        fqn = _build_fully_qualified_name(db_name, schema_name, source_name)
    elif _is_meaningful_value(nav_view):
        source_name = nav_view
        source_type = "View"
        fqn = _build_fully_qualified_name(db_name, schema_name, source_name)
    else:
        fqn = "Unknown Source (e.g., Local Excel File, Web Data, Dataflow, or Calculated Table)"

    return {
        "Source Server": _clean_source_value(server_name),
        "Source Database": _clean_source_value(db_name),
        "Source Schema": _clean_source_value(schema_name),
        "Source Name": _clean_source_value(source_name),
        "Source Type": _clean_source_value(source_type),
        "Native Query": native_query if _is_meaningful_value(native_query) else "N/A",
        "Query": native_query if _is_meaningful_value(native_query) else "N/A",
        "Native Query Columns": native_query_columns if _is_meaningful_value(native_query_columns) else "N/A",
        "Native Query Column Map": _extract_native_query_column_map(native_query),
        "Power Query M": m_code if m_code else "N/A",
        "Fully Qualified Name": _clean_source_value(fqn),
    }

def resolve_workspace_for_dataset(headers_spa, dataset_id):
    """Resolve the workspace that owns a dataset by using the Admin datasets endpoint."""
    if not dataset_id:
        return None

    url = f"https://api.powerbi.com/v1.0/myorg/admin/datasets?$filter=id eq '{dataset_id}'"
    response = requests.get(url, headers=headers_spa)
    if response.status_code == 200:
        datasets = response.json().get('value', [])
        if datasets:
            return datasets[0].get('workspaceId')
    return None


def resolve_workspace_for_dashboard(headers_spa, dashboard_id):
    """Resolve the workspace that owns a dashboard by using the Admin dashboards endpoint."""
    if not dashboard_id:
        return None

    url = f"https://api.powerbi.com/v1.0/myorg/admin/dashboards?$filter=id eq '{dashboard_id}'"
    response = requests.get(url, headers=headers_spa)
    if response.status_code == 200:
        dashboards = response.json().get('value', [])
        if dashboards:
            return dashboards[0].get('workspaceId')
    return None


def get_object_info(headersSPA, workspace_id, dataset_id, access_token, workspace_name_hint=None, dataset_name_hint=None, auth_headers=None):
    """
    Return source DB lineage for a semantic model as a clean list of dictionaries.

    The UI expects this function to return records that can be expanded using **record.
    Therefore this function never returns a string, DataFrame, or mixed type. On failure,
    it returns an empty list and writes a warning/error in Streamlit.
    """
    lineage_data = []
    conn, cursor = None, None

    if not workspace_id or not dataset_id:
        return lineage_data

    try:
        workspace_name, dataset_name = resolve_names_for_xmla(
            headersSPA,
            workspace_id,
            dataset_id,
            workspace_name_hint=workspace_name_hint,
            dataset_name_hint=dataset_name_hint,
            auth_headers=auth_headers,
        )
        if not workspace_name or not dataset_name:
            st.warning(f"Could not resolve workspace/model names for Dataset ID: {dataset_id}. Workspace ID: {workspace_id}")
            return lineage_data

        # Try all practical XMLA catalog names. The correct catalog should be the semantic model
        # name, but in some tenants the dataset-name REST resolution is blocked while the selected
        # report still works manually. Trying the dataset id as a fallback prevents one unresolved
        # model from breaking a multi-report selection.
        catalog_candidates = _unique_meaningful_values([dataset_name, dataset_name_hint, dataset_id])
        last_catalog_attempt = dataset_name
        for catalog_name in catalog_candidates:
            last_catalog_attempt = catalog_name
            conn, cursor = get_xmla_cursor(workspace_name, catalog_name, access_token)
            if cursor:
                dataset_name = catalog_name
                break
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

        if not cursor:
            st.warning(f"Could not open XMLA connection for Workspace: {workspace_name}, Dataset/Model: {last_catalog_attempt}")
            xmla_error = _last_xmla_connection_error()
            if xmla_error.get("error"):
                with st.expander("XMLA connection error details"):
                    st.code(xmla_error.get("error"))
                    st.caption(
                        "Check that the workspace is Premium/PPU/Fabric-backed, XMLA endpoint is enabled, "
                        "the signed-in user has semantic model build/read access, and MSOLAP is installed on this machine."
                    )
            return lineage_data

        query_tables = """
            SELECT
                [ID],
                [Name]
            FROM
                $SYSTEM.TMSCHEMA_TABLES
        """
        xmla_tables = execute_xmla_query(cursor, query_tables)
        if not xmla_tables:
            return lineage_data

        df_tables = pd.DataFrame.from_records(
            [tuple(row) for row in xmla_tables],
            columns=["ID", "Name"]
        )

        query_partitions = """
            SELECT
                [TableID],
                [Name] AS [PartitionName],
                [QueryDefinition],
                [SourceType]
            FROM
                $SYSTEM.TMSCHEMA_PARTITIONS
        """
        partition_columns = ["TableID", "Partition Name", "QueryDefinition", "Partition Source Type"]
        xmla_partitions = execute_xmla_query(cursor, query_partitions)

        # Some tenants/capacity versions may not expose SourceType in this DMV.
        # Fall back to the safer 3-column query instead of returning no output.
        if xmla_partitions is None:
            query_partitions = """
                SELECT
                    [TableID],
                    [Name] AS [PartitionName],
                    [QueryDefinition]
                FROM
                    $SYSTEM.TMSCHEMA_PARTITIONS
            """
            partition_columns = ["TableID", "Partition Name", "QueryDefinition"]
            xmla_partitions = execute_xmla_query(cursor, query_partitions)

        if not xmla_partitions:
            return lineage_data

        df_partitions = pd.DataFrame.from_records(
            [tuple(row) for row in xmla_partitions],
            columns=partition_columns
        )

        if "Partition Source Type" not in df_partitions.columns:
            df_partitions["Partition Source Type"] = "N/A"

        if df_tables.empty or df_partitions.empty:
            return lineage_data

        df_merged = pd.merge(
            df_tables,
            df_partitions,
            left_on="ID",
            right_on="TableID",
            how="inner"
        )

        for _, row in df_merged.iterrows():
            model_table_name = _clean_source_value(row.get('Name'))
            if model_table_name.startswith(("LocalDateTable", "DateTableTemplate", "Calculation")):
                continue

            m_code = str(row.get('QueryDefinition') or "")
            source_info = _parse_power_query_source(m_code)

            lineage_data.append({
                "Source Workspace Name": workspace_name,
                "Semantic Model Name": dataset_name,
                "Power BI Table Name": model_table_name,
                "Partition Name": _clean_source_value(row.get('Partition Name')),
                "Partition Source Type": _clean_source_value(row.get('Partition Source Type')),
                **source_info,
            })

        return lineage_data

    except Exception as e:
        st.error(f"Error fetching XMLA source lineage for Dataset {dataset_id}: {e}")
        return lineage_data
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

# --- MEASURE LINEAGE FUNCTIONS ---

def flatten_measure_dependencies(df):
    """
    Takes a raw DataFrame of mixed measure/column dependencies and recursively 
    flattens it so every measure points directly to its ultimate physical columns.
    """
    # 1. Inner recursive helper function
    def find_physical_deps(measure_name, path=None):
        if path is None:
            path = set()
        
        # Prevent infinite circular loops
        if measure_name in path:
            return []
        path.add(measure_name)
        
        dependencies = []
        # Find all dependency rows for the current measure
        rows = df[df['Measure Name'] == measure_name]
        
        for _, row in rows.iterrows():
            if row['Type'] == 'COLUMN':
                # Base Case: It's a physical column root
                dependencies.append((row['Source Table'], row['Source Column Name']))
            elif row['Type'] == 'MEASURE':
                # Recursive Case: Trace deeper into the nested measure
                nested_measure = row['Source Column Name']
                dependencies.extend(find_physical_deps(nested_measure, path.copy()))
                
        return list(set(dependencies)) # Returns unique (Table, Column) pairs

    # 2. Loop through ALL unique measures and fully explode them
    exploded_rows = []
    unique_measures = df['Measure Name'].unique()

    for measure in unique_measures:
        # Find the bedrock physical assets for this measure
        physical_assets = find_physical_deps(measure)
        
        for table, col in physical_assets:
            exploded_rows.append({
                'Measure Name': measure,
                'Source Table': table,
                'Source Column Name': col
            })

    # 3. Convert to DataFrame
    final_df = pd.DataFrame(exploded_rows)

    # 4. Sort to match the exact expected layout (if the dataframe isn't empty)
    if not final_df.empty:
        sorting_order = {measure: i for i, measure in enumerate(unique_measures)}
        final_df['sort_key'] = final_df['Measure Name'].map(sorting_order)
        final_df = final_df.sort_values(
            by=['sort_key', 'Source Table'], 
            ascending=[True, False]
        ).drop(columns=['sort_key'])

    return final_df


def _semantic_dependency_type(value):
    """Normalize Power BI dependency object types for display."""
    text = str(value or "").strip().upper()
    if text in {"CALC_COLUMN", "CALCULATED_COLUMN"}:
        return "CALC_COLUMN"
    if text in {"MEASURE", "COLUMN", "TABLE", "CALC_TABLE", "CALCULATED_TABLE"}:
        return "CALC_TABLE" if text == "CALCULATED_TABLE" else text
    return text or "N/A"


def _safe_len(row):
    """Return len(row) for XMLA row objects that may not behave like lists."""
    try:
        return len(row)
    except Exception:
        return 0


def _row_to_dict(row, headers):
    """Map an XMLA row tuple to a dictionary using the supplied header names."""
    return {headers[i]: row[i] if i < _safe_len(row) else None for i in range(len(headers))}


def _powerbi_data_type_name(value):
    """Convert common TMSCHEMA data type codes to readable values when possible."""
    text = str(value or "").strip()
    mapping = {
        "2": "Text/String",
        "6": "Whole Number/Int64",
        "8": "Decimal/Currency",
        "9": "DateTime",
        "10": "Date",
        "11": "Boolean",
        "17": "Binary",
        "19": "Variant/Any",
    }
    return mapping.get(text, text or "N/A")


def _powerbi_column_type_name(value, expression=None):
    """Classify TMSCHEMA_COLUMNS row as COLUMN, CALC_COLUMN, or system/internal column."""
    text = str(value or "").strip()
    if _is_meaningful_value(expression):
        return "CALC_COLUMN"
    # In TMSCHEMA_COLUMNS, Type=1 is usually a data column and Type=2 is a calculated column.
    if text == "2":
        return "CALC_COLUMN"
    return "COLUMN"


def _is_leaf_dependency_type(dep_type):
    """Return True for dependency objects that can be mapped to source DB columns."""
    return _semantic_dependency_type(dep_type) in {"COLUMN", "CALC_COLUMN"}


def _build_dependency_leaf_rows(raw_df):
    """Flatten MEASURE and CALC_COLUMN dependencies to column-level rows.

    Power BI DISCOVER_CALC_DEPENDENCY returns direct dependencies. A measure can depend
    on another measure, and a measure can also depend on a calculated column. This
    helper keeps CALC_COLUMN rows visible and recursively resolves nested measures /
    calculated columns down to the referenced semantic columns wherever possible.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=[
            "Target Object Type", "Target Table/View", "Target Object Name", "Target Expression",
            "Measure Name", "Dependency Table/View", "Dependency Object Name", "Dependency Object Type",
            "Dependency Expression", "Source Table", "Source Column Name", "Source Column Type",
        ])

    # Index direct dependencies by object name and type for recursion.
    by_object = {}
    for row in raw_df.to_dict("records"):
        obj_key = (_normalise_name_for_join(row.get("Target Object Name")), _semantic_dependency_type(row.get("Target Object Type")))
        by_object.setdefault(obj_key, []).append(row)
        # Also index by name only as a fallback because DMV rows sometimes omit table context.
        by_object.setdefault((_normalise_name_for_join(row.get("Target Object Name")), "*"), []).append(row)

    def find_leaf_rows(target_name, target_type, root_row, path=None):
        path = path or set()
        target_key = (_normalise_name_for_join(target_name), _semantic_dependency_type(target_type))
        if target_key in path:
            return []
        path.add(target_key)

        direct_rows = by_object.get(target_key) or by_object.get((_normalise_name_for_join(target_name), "*")) or []
        leaves = []

        for dep in direct_rows:
            dep_type = _semantic_dependency_type(dep.get("Dependency Object Type"))
            dep_name = dep.get("Dependency Object Name")
            dep_table = dep.get("Dependency Table/View")

            # Skip broad table-only dependencies; the column-level row will be present separately.
            if dep_type in {"TABLE", "CALC_TABLE"}:
                continue

            if dep_type == "MEASURE":
                nested = find_leaf_rows(dep_name, dep_type, root_row, path.copy())
                leaves.extend(nested)
                continue

            if dep_type == "CALC_COLUMN":
                # Keep the CALC_COLUMN itself visible, then also recurse to the base columns
                # referenced by that calculated column.
                leaves.append(dep)
                nested = find_leaf_rows(dep_name, dep_type, root_row, path.copy())
                leaves.extend(nested)
                continue

            if dep_type == "COLUMN":
                leaves.append(dep)

        # If nothing was found, preserve the root row so the user can still see the object.
        return leaves

    flattened = []
    roots = raw_df[raw_df["Target Object Type"].isin(["MEASURE", "CALC_COLUMN"])]
    for _, root in roots.iterrows():
        root_dict = root.to_dict()
        leaf_rows = find_leaf_rows(root_dict.get("Target Object Name"), root_dict.get("Target Object Type"), root_dict)
        if not leaf_rows:
            leaf_rows = [root_dict]

        for leaf in leaf_rows:
            dep_type = _semantic_dependency_type(leaf.get("Dependency Object Type"))
            dep_table = _prefer_non_na(leaf.get("Dependency Table/View"), leaf.get("Target Table/View"))
            dep_name = _prefer_non_na(leaf.get("Dependency Object Name"), leaf.get("Target Object Name"))
            flattened.append({
                "Target Object Type": _semantic_dependency_type(root_dict.get("Target Object Type")),
                "Target Table/View": root_dict.get("Target Table/View"),
                "Target Object Name": root_dict.get("Target Object Name"),
                "Target Expression": root_dict.get("Target Expression"),
                "Measure Name": root_dict.get("Target Object Name") if _semantic_dependency_type(root_dict.get("Target Object Type")) == "MEASURE" else "N/A",
                "Dependency Table/View": dep_table,
                "Dependency Object Name": dep_name,
                "Dependency Object Type": dep_type,
                "Dependency Expression": leaf.get("Dependency Expression"),
                # Backward-compatible names consumed by enrichment logic.
                "Source Table": dep_table,
                "Source Column Name": dep_name,
                "Source Column Type": dep_type,
            })

    final_df = pd.DataFrame(flattened).drop_duplicates()
    if final_df.empty:
        return final_df
    sort_cols = [col for col in ["Target Object Type", "Target Table/View", "Target Object Name", "Dependency Table/View", "Dependency Object Name"] if col in final_df.columns]
    return final_df.sort_values(sort_cols).reset_index(drop=True)


def get_raw_measure_dependencies(headersSPA, workspace_id, dataset_id, access_token, workspace_name_hint=None, dataset_name_hint=None, auth_headers=None):
    """
    Connect to XMLA and return lineage for MEASURE and CALC_COLUMN objects.

    v24 fix:
    - Do not rely on a single DISCOVER_CALC_DEPENDENCY projection.
    - Query all dependency rows first, then filter MEASURE/CALC_COLUMN in Python.
    - This avoids empty results when a tenant/capacity rejects WHERE IN or optional columns.
    - Uses the same workspace/model resolution hints used by Source DB Lineage.
    """
    empty_df = pd.DataFrame(columns=[
        "Target Object Type", "Target Table/View", "Target Object Name", "Target Expression",
        "Measure Name", "Dependency Table/View", "Dependency Object Name", "Dependency Object Type",
        "Dependency Expression", "Source Table", "Source Column Name", "Source Column Type",
    ])
    conn, cursor = None, None

    if not workspace_id or not dataset_id:
        return empty_df

    try:
        workspace_name, dataset_name = resolve_names_for_xmla(
            headersSPA,
            workspace_id,
            dataset_id,
            workspace_name_hint=workspace_name_hint,
            dataset_name_hint=dataset_name_hint,
            auth_headers=auth_headers,
        )
        if not workspace_name or not dataset_name:
            return empty_df

        for catalog_name in _unique_meaningful_values([dataset_name, dataset_name_hint, dataset_id]):
            conn, cursor = get_xmla_cursor(workspace_name, catalog_name, access_token)
            if cursor:
                dataset_name = catalog_name
                break
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

        if not cursor:
            return empty_df

        dependency_query_attempts = [
            (
                "full",
                [
                    "Target Object Type", "Target Table/View", "Target Object Name", "Target Expression",
                    "Dependency Object Type", "Dependency Table/View", "Dependency Object Name", "Dependency Expression", "Dependency Query",
                ],
                """
                SELECT
                    [OBJECT_TYPE],
                    [TABLE],
                    [OBJECT],
                    [EXPRESSION],
                    [REFERENCED_OBJECT_TYPE],
                    [REFERENCED_TABLE],
                    [REFERENCED_OBJECT],
                    [REFERENCED_EXPRESSION],
                    [QUERY]
                FROM
                    $SYSTEM.DISCOVER_CALC_DEPENDENCY
                """,
            ),
            (
                "without_query",
                [
                    "Target Object Type", "Target Table/View", "Target Object Name", "Target Expression",
                    "Dependency Object Type", "Dependency Table/View", "Dependency Object Name", "Dependency Expression",
                ],
                """
                SELECT
                    [OBJECT_TYPE],
                    [TABLE],
                    [OBJECT],
                    [EXPRESSION],
                    [REFERENCED_OBJECT_TYPE],
                    [REFERENCED_TABLE],
                    [REFERENCED_OBJECT],
                    [REFERENCED_EXPRESSION]
                FROM
                    $SYSTEM.DISCOVER_CALC_DEPENDENCY
                """,
            ),
            (
                "minimal",
                [
                    "Target Object Type", "Target Table/View", "Target Object Name",
                    "Dependency Object Type", "Dependency Table/View", "Dependency Object Name",
                ],
                """
                SELECT
                    [OBJECT_TYPE],
                    [TABLE],
                    [OBJECT],
                    [REFERENCED_OBJECT_TYPE],
                    [REFERENCED_TABLE],
                    [REFERENCED_OBJECT]
                FROM
                    $SYSTEM.DISCOVER_CALC_DEPENDENCY
                """,
            ),
        ]

        xmla_rows, selected_headers = None, None
        for _, headers, query in dependency_query_attempts:
            xmla_rows = execute_xmla_query(cursor, query)
            if xmla_rows:
                selected_headers = headers
                break

        if not xmla_rows or not selected_headers:
            return empty_df

        raw_rows = []
        for row in xmla_rows:
            item = _row_to_dict(row, selected_headers)
            target_type = _semantic_dependency_type(item.get("Target Object Type"))
            dep_type = _semantic_dependency_type(item.get("Dependency Object Type"))
            target_table = item.get("Target Table/View")
            target_name = item.get("Target Object Name")
            dep_table = item.get("Dependency Table/View")
            dep_name = item.get("Dependency Object Name")

            # Keep only the objects requested by lineage tabs. Filtering here is safer than
            # putting WHERE IN inside the DMV query, which can behave differently by endpoint.
            if target_type not in {"MEASURE", "CALC_COLUMN"}:
                continue
            if not target_name or not dep_name:
                continue
            if str(target_name).startswith("__") or str(dep_name) == "FormatString":
                continue
            if _is_internal_semantic_name(target_table) or _is_internal_semantic_name(dep_table):
                continue

            raw_rows.append({
                "Target Object Type": target_type,
                "Target Table/View": target_table,
                "Target Object Name": target_name,
                "Target Expression": item.get("Target Expression") or "N/A",
                "Dependency Table/View": dep_table,
                "Dependency Object Name": dep_name,
                "Dependency Object Type": dep_type,
                "Dependency Expression": item.get("Dependency Expression") or "N/A",
            })

        if not raw_rows:
            return empty_df

        return _build_dependency_leaf_rows(pd.DataFrame(raw_rows))

    except Exception as e:
        st.error(f"Error fetching measure/calculated-column lineage for Dataset {dataset_id}: {e}")
        return empty_df
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



# --- UPLOAD-ONLY REPORT LAYOUT + SEMANTIC LOOKUP HELPERS ---

def _is_internal_semantic_name(name):
    text = str(name or "")
    return text.startswith(("LocalDateTable", "DateTableTemplate", "Calculation", "__"))


def _normalise_name_for_join(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _record_to_source_key(record):
    return _normalise_name_for_join(record.get("Power BI Table Name") or record.get("Source Table") or record.get("Table Name"))


def get_semantic_model_objects(headersSPA, headersSP, workspace_id, dataset_id, access_token, workspace_name_hint=None, dataset_name_hint=None, auth_headers=None):
    """
    Return one semantic-model catalogue containing physical columns, calculated columns, and measures.

    v24 fix:
    - Power BI TMSCHEMA_COLUMNS exposes ExplicitName/InferredName and ExplicitDataType/InferredDataType
      in many XMLA endpoints, not always Name/DataType.
    - The previous query could return no rows because it asked for non-existent [Name]/[DataType].
    - This implementation tries multiple safe projections and normalizes the result.
    """
    objects = []

    if not dataset_id:
        return objects

    workspace_name = "N/A"
    dataset_name = "N/A"
    conn, cursor = None, None

    try:
        if workspace_id:
            workspace_name, dataset_name = resolve_names_for_xmla(
                headersSPA,
                workspace_id,
                dataset_id,
                workspace_name_hint=workspace_name_hint,
                dataset_name_hint=dataset_name_hint,
                auth_headers=auth_headers,
            )

        if workspace_id and access_token and workspace_name and dataset_name:
            for catalog_name in _unique_meaningful_values([dataset_name, dataset_name_hint, dataset_id]):
                conn, cursor = get_xmla_cursor(workspace_name, catalog_name, access_token)
                if cursor:
                    dataset_name = catalog_name
                    break
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None

        table_map = {}
        hidden_tables = set()

        if cursor:
            query_tables_attempts = [
                (
                    ["ID", "Name", "IsHidden"],
                    """
                    SELECT
                        [ID],
                        [Name],
                        [IsHidden]
                    FROM
                        $SYSTEM.TMSCHEMA_TABLES
                    """,
                ),
                (
                    ["ID", "Name"],
                    """
                    SELECT
                        [ID],
                        [Name]
                    FROM
                        $SYSTEM.TMSCHEMA_TABLES
                    """,
                ),
            ]
            for table_headers, table_query in query_tables_attempts:
                tables_rows = execute_xmla_query(cursor, table_query)
                if tables_rows:
                    for row in tables_rows:
                        item = _row_to_dict(row, table_headers)
                        table_id = item.get("ID")
                        table_name = item.get("Name")
                        is_hidden = str(item.get("IsHidden", "False")).strip().lower() in {"true", "1", "yes"}
                        table_map[table_id] = table_name
                        if is_hidden:
                            hidden_tables.add(table_id)
                    break

            column_query_attempts = [
                (
                    [
                        "ID", "TableID", "ExplicitName", "InferredName", "ExplicitDataType", "InferredDataType",
                        "IsHidden", "SourceColumn", "Type", "Expression",
                    ],
                    """
                    SELECT
                        [ID],
                        [TableID],
                        [ExplicitName],
                        [InferredName],
                        [ExplicitDataType],
                        [InferredDataType],
                        [IsHidden],
                        [SourceColumn],
                        [Type],
                        [Expression]
                    FROM
                        $SYSTEM.TMSCHEMA_COLUMNS
                    """,
                ),
                (
                    [
                        "ID", "TableID", "ExplicitName", "InferredName", "ExplicitDataType", "InferredDataType",
                        "IsHidden", "SourceColumn", "Type",
                    ],
                    """
                    SELECT
                        [ID],
                        [TableID],
                        [ExplicitName],
                        [InferredName],
                        [ExplicitDataType],
                        [InferredDataType],
                        [IsHidden],
                        [SourceColumn],
                        [Type]
                    FROM
                        $SYSTEM.TMSCHEMA_COLUMNS
                    """,
                ),
                (
                    ["ID", "TableID", "Name", "DataType", "IsHidden", "SourceColumn", "Type", "Expression"],
                    """
                    SELECT
                        [ID],
                        [TableID],
                        [Name],
                        [DataType],
                        [IsHidden],
                        [SourceColumn],
                        [Type],
                        [Expression]
                    FROM
                        $SYSTEM.TMSCHEMA_COLUMNS
                    """,
                ),
                (
                    ["ID", "TableID", "Name", "DataType", "IsHidden"],
                    """
                    SELECT
                        [ID],
                        [TableID],
                        [Name],
                        [DataType],
                        [IsHidden]
                    FROM
                        $SYSTEM.TMSCHEMA_COLUMNS
                    """,
                ),
            ]

            column_rows, column_headers = None, None
            for headers, query in column_query_attempts:
                column_rows = execute_xmla_query(cursor, query)
                if column_rows:
                    column_headers = headers
                    break

            if column_rows and column_headers:
                for row in column_rows:
                    row_dict = _row_to_dict(row, column_headers)
                    table_id = row_dict.get("TableID")
                    table_name = table_map.get(table_id, "Unknown")
                    column_name = _prefer_non_na(row_dict.get("ExplicitName"), row_dict.get("InferredName"), row_dict.get("Name"))
                    data_type = _prefer_non_na(row_dict.get("ExplicitDataType"), row_dict.get("InferredDataType"), row_dict.get("DataType"))
                    is_hidden = str(row_dict.get("IsHidden", "False")).strip().lower() in {"true", "1", "yes"}

                    if _is_internal_semantic_name(table_name) or _is_internal_semantic_name(column_name):
                        continue
                    if is_hidden or table_id in hidden_tables:
                        continue

                    column_expression = row_dict.get("Expression") or "N/A"
                    object_type = _powerbi_column_type_name(row_dict.get("Type"), column_expression)

                    objects.append({
                        "Semantic Workspace Name": workspace_name or "N/A",
                        "Semantic Model Name": dataset_name or "N/A",
                        "Semantic Table/View": table_name,
                        "Object Type": object_type,
                        "Semantic Object Name": column_name,
                        "Data Type": _powerbi_data_type_name(data_type),
                        "Source Column Name From Model": row_dict.get("SourceColumn", "N/A"),
                        "DAX Expression": column_expression if object_type == "CALC_COLUMN" else "N/A",
                    })

            measure_query_attempts = [
                (
                    ["Name", "TableID", "Expression", "IsHidden"],
                    """
                    SELECT
                        [Name],
                        [TableID],
                        [Expression],
                        [IsHidden]
                    FROM
                        $SYSTEM.TMSCHEMA_MEASURES
                    """,
                ),
                (
                    ["Name", "TableID", "Expression"],
                    """
                    SELECT
                        [Name],
                        [TableID],
                        [Expression]
                    FROM
                        $SYSTEM.TMSCHEMA_MEASURES
                    """,
                ),
            ]
            for measure_headers, measure_query in measure_query_attempts:
                measure_rows = execute_xmla_query(cursor, measure_query)
                if not measure_rows:
                    continue
                for row in measure_rows:
                    item = _row_to_dict(row, measure_headers)
                    measure_name = item.get("Name")
                    table_id = item.get("TableID")
                    is_hidden = str(item.get("IsHidden", "False")).strip().lower() in {"true", "1", "yes"}
                    table_name = table_map.get(table_id, "Unknown")

                    if is_hidden or _is_internal_semantic_name(measure_name) or str(measure_name) == "FormatString":
                        continue

                    objects.append({
                        "Semantic Workspace Name": workspace_name or "N/A",
                        "Semantic Model Name": dataset_name or "N/A",
                        "Semantic Table/View": table_name,
                        "Object Type": "MEASURE",
                        "Semantic Object Name": measure_name,
                        "Data Type": "Measure",
                        "Source Column Name From Model": "N/A",
                        "DAX Expression": item.get("Expression") or "",
                    })
                break

        # REST fallback for measures if XMLA is not available or returned no measures.
        existing_measure_names = {
            str(item.get("Semantic Object Name"))
            for item in objects
            if str(item.get("Object Type", "")).upper() == "MEASURE"
        }
        measures = get_measure_details(headersSP, dataset_id) or []
        for measure in measures:
            measure_name = measure.get("Measure Name")
            if not measure_name or measure_name in existing_measure_names:
                continue
            objects.append({
                "Semantic Workspace Name": workspace_name or "N/A",
                "Semantic Model Name": dataset_name or "N/A",
                "Semantic Table/View": measure.get("Home Table", "Unknown"),
                "Object Type": "MEASURE",
                "Semantic Object Name": measure_name,
                "Data Type": "Measure",
                "Source Column Name From Model": "N/A",
                "DAX Expression": measure.get("DAX Expression", ""),
            })

        return objects

    except Exception as e:
        st.warning(f"Could not fetch full semantic model object catalogue for Dataset {dataset_id}: {e}")
        return objects
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


def build_workspace_report_contexts(selected_art_keys, artifact_mapping):
    contexts = []
    for art_key in selected_art_keys:
        item = artifact_mapping[art_key]
        contexts.append({
            "Context Key": art_key,
            "Scope Type": "Workspace",
            "Container Name": item.get("Workspace Name"),
            "Workspace": item.get("Workspace Name"),
            "App Name": "N/A",
            "Source Report": item.get("Name"),
            "Report ID": item.get("ID"),
            "Dataset ID": item.get("Dataset ID"),
            "Target Workspace ID": item.get("Workspace ID"),
            "Report Type": item.get("Type"),
            "Report Format": item.get("Format"),
        })
    return contexts


def build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA):
    contexts = []
    for art_key in selected_art_keys:
        item = app_art_mapping[art_key]
        original_id = item.get("Original ID") or item.get("ID")
        cache_key = f"app_report_resolved_ids_v17_{original_id}"
        if cache_key not in st.session_state or not isinstance(st.session_state[cache_key], tuple):
            st.session_state[cache_key] = resolve_dataset_for_app_report(headersSPA, original_id)
        dataset_id, target_workspace_id = st.session_state.get(cache_key, (None, None))

        contexts.append({
            "Context Key": art_key,
            "Scope Type": "App",
            "Container Name": item.get("App Name"),
            "Workspace": "N/A",
            "App Name": item.get("App Name"),
            "Source Report": item.get("Name"),
            "Report ID": original_id,
            "Dataset ID": dataset_id,
            "Target Workspace ID": target_workspace_id,
            "Report Type": item.get("Type"),
            "Report Format": item.get("Format"),
        })
    return contexts


def _get_semantic_objects_for_contexts(contexts, headersSPA, headersSP, xmla_token, cache_prefix):
    rows = []
    for context in contexts:
        dataset_id = context.get("Dataset ID")
        workspace_id = context.get("Target Workspace ID")
        if not dataset_id:
            continue

        cache_key = f"{cache_prefix}_semantic_objects_v24_{workspace_id}_{dataset_id}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = get_semantic_model_objects(headersSPA, headersSP, workspace_id, dataset_id, xmla_token, workspace_name_hint=context.get("Workspace") if context.get("Scope Type") == "Workspace" else None, dataset_name_hint=context.get("Semantic Model Name"), auth_headers=[("MasterUser", headersSPA)])

        for obj in st.session_state.get(cache_key, []) or []:
            rows.append({
                "Scope Type": context.get("Scope Type"),
                "Container Name": context.get("Container Name"),
                "Workspace": context.get("Workspace"),
                "App Name": context.get("App Name"),
                "Source Report": context.get("Source Report"),
                "Report ID": context.get("Report ID"),
                "Dataset ID": dataset_id,
                **obj,
            })
    return rows


def _get_source_lineage_for_context(context, headersSPA, xmla_token, cache_prefix, auth_headers=None):
    dataset_id = context.get("Dataset ID")
    workspace_id = context.get("Target Workspace ID")
    if not dataset_id or not workspace_id:
        return []

    cache_key = f"{cache_prefix}_source_lineage_v24_{workspace_id}_{dataset_id}"
    if cache_key not in st.session_state:
        st.session_state[cache_key] = get_object_info(
            headersSPA,
            workspace_id,
            dataset_id,
            xmla_token,
            workspace_name_hint=context.get("Workspace") if context.get("Scope Type") == "Workspace" else None,
            dataset_name_hint=context.get("Semantic Model Name"),
            auth_headers=auth_headers,
        )
    return st.session_state.get(cache_key, []) or []


def _source_lineage_map(source_rows):
    """Build a resilient lookup for source lineage rows.

    Keys are created from Power BI table name, source name, and fully-qualified source
    object. This avoids N/A mismatches when a selected set contains both native-query
    and normal navigation-table reports.
    """
    lookup = {}

    def add_key(key_value, row):
        key = _normalise_name_for_join(key_value)
        if key and key not in lookup:
            lookup[key] = row

    for row in source_rows or []:
        if not isinstance(row, dict):
            continue

        # Primary key: semantic/Power BI table name. This is what visual layout and
        # measure DMV rows usually contain.
        add_key(row.get("Power BI Table Name"), row)

        # Secondary keys: physical source object(s). Helpful for native queries or
        # renamed semantic tables.
        for value in str(row.get("Source Name") or "").split(";"):
            add_key(value, row)

        for value in str(row.get("Fully Qualified Name") or "").split(";"):
            add_key(value, row)
            parts = [part for part in str(value).split(".") if part]
            if parts:
                add_key(parts[-1], row)

    return lookup


def _enrich_with_source_details(base_row, semantic_table, semantic_column, source_lookup):
    """Return clean source mapping fields used by lookup and lineage views.

    The function never returns None/null for display columns. Fully qualified name is
    rebuilt when the XMLA source row does not contain it or old session cache values
    are still present.
    """
    source_row = (
        source_lookup.get(_normalise_name_for_join(semantic_table))
        or source_lookup.get(_normalise_name_for_join(base_row.get("Semantic Table/View")))
        or source_lookup.get(_normalise_name_for_join(base_row.get("Power BI Table Name")))
        or {}
    )

    source_column_from_model = base_row.get("Source Column Name From Model") or semantic_column
    native_actual_column = _resolve_native_actual_column(source_row, semantic_column)
    source_column = _prefer_non_na(native_actual_column, source_column_from_model, semantic_column)

    source_db = _prefer_non_na(source_row.get("Source Database"), base_row.get("Exact Source Database"))
    source_schema = _prefer_non_na(source_row.get("Source Schema"), base_row.get("Exact Source Schema"))
    source_name = _prefer_non_na(source_row.get("Source Name"), base_row.get("Exact Source Table/View"), semantic_table)
    source_type = _prefer_non_na(source_row.get("Source Type"), base_row.get("Exact Source Object Type"))
    source_fqn = _prefer_non_na(
        source_row.get("Fully Qualified Name"),
        base_row.get("Fully Qualified Source Object"),
        _build_fully_qualified_name(source_db, source_schema, source_name),
    )

    return {
        "Semantic Workspace Name": _prefer_non_na(source_row.get("Source Workspace Name"), base_row.get("Semantic Workspace Name")),
        "Semantic Model Name": _prefer_non_na(source_row.get("Semantic Model Name"), base_row.get("Semantic Model Name")),
        "Query": _prefer_non_na(source_row.get("Query"), source_row.get("Native Query"), base_row.get("Query")),
        "Native Query Columns": _prefer_non_na(source_row.get("Native Query Columns")),
        "Exact Source Database": _prefer_non_na(source_db),
        "Exact Source Schema": _prefer_non_na(source_schema),
        "Exact Source Table/View": _prefer_non_na(source_name),
        "Exact Source Object Type": _prefer_non_na(source_type),
        "Exact Source Column Name": _prefer_non_na(source_column),
        "Fully Qualified Source Object": _prefer_non_na(source_fqn),
    }


def _get_snowflake_cortex_settings():
    """Return Snowflake Cortex measure-detail settings from config/app_settings.json."""
    return (Utils.load_app_settings().get("snowflake_cortex") or {}).copy()


def _get_openai_measure_definition_settings():
    """Return OpenAI measure-detail settings from config/app_settings.json."""
    return (Utils.load_app_settings().get("openai_measure_definitions") or {}).copy()


def _get_measure_definition_provider_order():
    """Return provider order for on-demand measure details."""
    settings = Utils.load_app_settings()
    configured = (settings.get("measure_definition") or {}).get("provider_order") or []
    if isinstance(configured, str):
        configured = [item.strip() for item in configured.split(",")]
    removed_providers = {"snowflake_metadata", "metadata", "metadata_fallback"}
    order = [
        str(item or "").strip().lower()
        for item in configured
        if str(item or "").strip() and str(item or "").strip().lower() not in removed_providers
    ]
    return order or ["snowflake_cortex", "openai"]


def _get_default_measure_definition_provider():
    """Return the default provider choice shown in the UI."""
    settings = Utils.load_app_settings()
    default_provider = (settings.get("measure_definition") or {}).get("default_provider") or "auto"
    default_provider = str(default_provider or "auto").strip().lower()
    if default_provider in {"snowflake_metadata", "metadata", "metadata_fallback"}:
        return "auto"
    return default_provider


def _trim_for_prompt(value, max_chars):
    text = str(value or "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "NAN"}:
        return ""
    text = re.sub(r"\s+", " ", text)
    if max_chars and len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text


def _measure_definition_prompt_payload(row, settings):
    dax_max_chars = int(settings.get("dax_expression_max_chars") or 3000)
    query_max_chars = int(settings.get("source_query_max_chars") or 1200)
    source_lineage_rows = []
    for source_row in row.get("_measure_source_rows", []) or []:
        source_lineage_rows.append({
            "semantic_table": _trim_for_prompt(
                _prefer_non_na(source_row.get("Semantic_Tables"), source_row.get("Target Table/View"), source_row.get("Semantic Table/View")),
                300,
            ),
            "semantic_object_name": _trim_for_prompt(
                _prefer_non_na(source_row.get("Semantic_Object_Name"), source_row.get("Semantic Object Name")),
                300,
            ),
            "semantic_object_type": _trim_for_prompt(
                _prefer_non_na(source_row.get("Semantic_Object_Type"), source_row.get("Semantic Object Type"), source_row.get("Target Object Type")),
                120,
            ),
            "dependency_expression": _trim_for_prompt(source_row.get("Dependency Expression"), dax_max_chars),
            "source_fully_qualified_name": _trim_for_prompt(
                _prefer_non_na(source_row.get("Source_Fully_Qualified_Name"), source_row.get("Fully Qualified Source Object")),
                600,
            ),
            "source_object_type": _trim_for_prompt(
                _prefer_non_na(source_row.get("Source_Object_Type"), source_row.get("Exact Source Object Type")),
                120,
            ),
            "source_column": _trim_for_prompt(
                _prefer_non_na(source_row.get("Source_Column_Name"), source_row.get("Exact Source Column Name")),
                300,
            ),
            "source_query": _trim_for_prompt(_prefer_non_na(source_row.get("Source_Query"), source_row.get("Query")), query_max_chars),
        })

    payload = {
        "workspace_name": _trim_for_prompt(_prefer_non_na(row.get("Workspace_Name"), row.get("Workspace")), 300),
        "report_name": _trim_for_prompt(_prefer_non_na(row.get("Report_Name"), row.get("Source Report")), 300),
        "semantic_model_name": _trim_for_prompt(row.get("Semantic Model Name"), 300),
        "measure_name": _trim_for_prompt(
            _prefer_non_na(
                row.get("Semantic_Measure_Name"),
                row.get("Measure Name"),
                row.get("Target Object Name"),
                row.get("Semantic_Object_Name"),
            ),
            300,
        ),
        "semantic_table": _trim_for_prompt(
            _prefer_non_na(row.get("Semantic_Tables"), row.get("Target Table/View"), row.get("Semantic Table/View")),
            300,
        ),
        "semantic_object_name": _trim_for_prompt(
            _prefer_non_na(row.get("Semantic_Object_Name"), row.get("Semantic Object Name")),
            300,
        ),
        "semantic_object_type": _trim_for_prompt(
            _prefer_non_na(row.get("Semantic_Object_Type"), row.get("Semantic Object Type"), row.get("Target Object Type")),
            120,
        ),
        "dax_expression": _trim_for_prompt(
            _prefer_non_na(row.get("Semantic_DAX_Expression"), row.get("Target Expression"), row.get("DAX Expression")),
            dax_max_chars,
        ),
        "dependency_expression": _trim_for_prompt(row.get("Dependency Expression"), dax_max_chars),
        "source_fully_qualified_name": _trim_for_prompt(
            _prefer_non_na(row.get("Source_Fully_Qualified_Name"), row.get("Fully Qualified Source Object")),
            600,
        ),
        "source_object_type": _trim_for_prompt(
            _prefer_non_na(row.get("Source_Object_Type"), row.get("Exact Source Object Type")),
            120,
        ),
        "source_column": _trim_for_prompt(
            _prefer_non_na(row.get("Source_Column_Name"), row.get("Exact Source Column Name")),
            300,
        ),
        "source_query": _trim_for_prompt(_prefer_non_na(row.get("Source_Query"), row.get("Query")), query_max_chars),
    }
    if source_lineage_rows:
        payload["source_lineage_rows"] = source_lineage_rows
    return payload


def _measure_definition_cache_name(key_prefix):
    return f"{key_prefix}_measure_definition_cache"


def _measure_definition_cache_key(row):
    """Create a stable cache key for one measure definition."""
    identity = {
        "dataset_id": row.get("Dataset ID"),
        "workspace": _prefer_non_na(row.get("Workspace_Name"), row.get("Workspace")),
        "report": _prefer_non_na(row.get("Report_Name"), row.get("Source Report")),
        "semantic_model": _prefer_non_na(row.get("Semantic Model Name"), row.get("Semantic_Model_Name")),
        "semantic_table": _prefer_non_na(row.get("Semantic_Tables"), row.get("Target Table/View")),
        "measure_name": _prefer_non_na(
            row.get("Semantic_Measure_Name"),
            row.get("Measure Name"),
            row.get("Target Object Name"),
            row.get("Semantic_Object_Name"),
        ),
        "dax_expression": _prefer_non_na(row.get("Semantic_DAX_Expression"), row.get("Target Expression"), row.get("DAX Expression")),
    }
    source_rows = row.get("_measure_source_rows", []) or []
    if source_rows:
        identity["source_lineage_rows"] = [
            {
                "source_fully_qualified_name": _prefer_non_na(
                    source_row.get("Source_Fully_Qualified_Name"),
                    source_row.get("Fully Qualified Source Object"),
                ),
                "source_object_type": _prefer_non_na(
                    source_row.get("Source_Object_Type"),
                    source_row.get("Exact Source Object Type"),
                ),
                "source_column": _prefer_non_na(
                    source_row.get("Source_Column_Name"),
                    source_row.get("Exact Source Column Name"),
                ),
                "semantic_object_name": _prefer_non_na(
                    source_row.get("Semantic_Object_Name"),
                    source_row.get("Semantic Object Name"),
                ),
            }
            for source_row in source_rows
        ]
    raw = json.dumps(identity, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _select_existing_columns(df, ordered_columns):
    """Return a dataframe with the requested columns in order, creating missing ones as N/A."""
    if df is None or df.empty:
        return df
    for column in ordered_columns:
        if column not in df.columns:
            df[column] = "N/A"
    return df[ordered_columns]


def _clean_dataframe_for_display(df, normalize_columns=True):
    """Remove user-facing N/A placeholders and optionally normalize column names.

    Important:
    - For final UI/download tables, ``normalize_columns=True`` keeps the rule that
      no displayed column has spaces.
    - For lineage-standardization logic, ``normalize_columns=False`` preserves the
      original internal column names long enough for mapping rules such as
      ``Source Report`` -> ``Target_Report_Name`` to work correctly.

    This fixes the issue where v28 converted column names too early and therefore
    some previously available columns were not mapped/displayed correctly.
    """
    if df is None or df.empty:
        return df
    display_df = df.copy()
    display_df = display_df.replace({
        "N/A": "",
        "NA": "",
        "None": "",
        "NULL": "",
        "null": "",
        "nan": "",
    }).fillna("")
    if normalize_columns:
        return _normalize_dataframe_column_names(display_df)
    return display_df


# Final UI naming standard:
# Target_* = Power BI/Fabric objects (workspace, app, report, semantic model, table, measure, visual)
# Source_* = physical database/source-system objects (Snowflake server/database/schema/table/view/query/columns)
_COLUMN_STANDARDIZATION_MAP = {
    "Scope Type": "Target_Scope_Type",
    "Container Name": "Target_Container_Name",
    "Workspace Name": "Target_Workspace_Name",
    "Workspace": "Target_Workspace_Name",
    "App Name": "Target_App_Name",
    "Source Report": "Target_Report_Name",
    "Source Dashboard": "Target_Dashboard_Name",
    "Report ID": "Target_Report_ID",
    "Dashboard ID": "Target_Dashboard_ID",
    "Dataset ID": "Target_Dataset_ID",
    "Source Workspace Name": "Target_Semantic_Workspace_Name",
    "Semantic Workspace Name": "Target_Semantic_Workspace_Name",
    "Semantic Model Name": "Target_Semantic_Model_Name",
    "Power BI Table Name": "Target_Semantic_Table_View",
    "Semantic Table/View": "Target_Semantic_Table_View",
    "Object Type": "Target_Semantic_Object_Type",
    "Semantic Object Type": "Target_Semantic_Object_Type",
    "Semantic Object Name": "Target_Semantic_Object_Name",
    "Measure Name": "Target_Measure_Name",
    "Data Type": "Target_Data_Type",
    "DAX Expression": "Target_DAX_Expression",
    "Target Object Type": "Target_Semantic_Object_Type",
    "Target Table/View": "Target_Semantic_Table_View",
    "Target Object Name": "Target_Semantic_Object_Name",
    "Target Expression": "Target_DAX_Expression",
    "Dependency Table/View": "Target_Dependency_Table_View",
    "Dependency Object Name": "Target_Dependency_Object_Name",
    "Dependency Object Type": "Target_Dependency_Object_Type",
    "Dependency Expression": "Target_Dependency_Expression",
    "Partition Name": "Target_Partition_Name",
    "Partition Source Type": "Target_Partition_Source_Type",
    "Page Name": "Target_Page_Name",
    "Page ID": "Target_Page_ID",
    "Visual Name": "Target_Visual_Name",
    "Visual ID": "Target_Visual_ID",
    "Visualization Type": "Target_Visualization_Type",
    "Field Role": "Target_Field_Role",
    "Field Type": "Target_Field_Type",
    "Table Name": "Target_Semantic_Table_View",
    "Column / Measure Name": "Target_Semantic_Object_Name",
    "Aggregation": "Target_Aggregation",
    "Query Reference": "Target_Query_Reference",

    "Source Server": "Source_Server",
    "Source Database": "Source_Database",
    "Exact Source Database": "Source_Database",
    "Source Schema": "Source_Schema",
    "Exact Source Schema": "Source_Schema",
    "Source Name": "Source_Table_View",
    "Exact Source Table/View": "Source_Table_View",
    "Source Type": "Source_Object_Type",
    "Exact Source Object Type": "Source_Object_Type",
    "Query": "Source_Query",
    "Native Query": "Source_Query",
    "Native Query Columns": "Source_Query_Output_Columns",
    "Exact Source Column Name": "Source_Column_Name",
    "Source Column Name From Model": "Source_Column_Name_From_Model",
    "Fully Qualified Name": "Source_Fully_Qualified_Object",
    "Fully Qualified Source Object": "Source_Fully_Qualified_Object",
}

# These columns identify different Power BI entities even when their displayed
# values happen to match, such as a report named the same as its workspace.
_DISPLAY_IDENTITY_COLUMNS = {
    "Target_Scope_Type",
    "Target_Container_Name",
    "Target_Workspace_Name",
    "Target_App_Name",
    "Target_Report_Name",
    "Target_Dashboard_Name",
    "Target_Report_ID",
    "Target_Dashboard_ID",
    "Target_Dataset_ID",
}


def _standardize_and_prune_display_dataframe(df):
    """Apply Target_/Source_ naming and remove empty/redundant UI columns."""
    if df is None or df.empty:
        return df

    cleaned = _clean_dataframe_for_display(df, normalize_columns=False)
    result = pd.DataFrame(index=cleaned.index)

    # Rename columns and coalesce duplicate names created by the standardization map.
    for original_col in cleaned.columns:
        standardized_col = _COLUMN_STANDARDIZATION_MAP.get(original_col, original_col)
        series = cleaned[original_col]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        series = series.astype(str).replace({"nan": "", "None": "", "N/A": ""}).fillna("")

        if standardized_col in result.columns:
            existing = result[standardized_col].astype(str).fillna("")
            result[standardized_col] = existing.where(existing.str.strip() != "", series)
        else:
            result[standardized_col] = series

    # Remove columns that are completely blank after cleanup.
    non_blank_cols = [col for col in result.columns if result[col].astype(str).str.strip().ne("").any()]
    result = result[non_blank_cols]

    # Remove exact duplicate columns by values, while preserving the first occurrence.
    unique_cols = []
    seen_signatures = set()
    for col in result.columns:
        signature = tuple(result[col].astype(str).fillna(""))
        preserve_distinct_meaning = col.startswith("Source_") or col in _DISPLAY_IDENTITY_COLUMNS
        if signature in seen_signatures and not preserve_distinct_meaning:
            continue
        seen_signatures.add(signature)
        unique_cols.append(col)
    result = result[unique_cols]

    # Hide semantic workspace when it duplicates the selected Power BI workspace.
    if "Target_Workspace_Name" in result.columns and "Target_Semantic_Workspace_Name" in result.columns:
        lhs = result["Target_Workspace_Name"].astype(str).str.strip().str.lower()
        rhs = result["Target_Semantic_Workspace_Name"].astype(str).str.strip().str.lower()
        if lhs.equals(rhs):
            result = result.drop(columns=["Target_Semantic_Workspace_Name"])

    return _normalize_dataframe_column_names(result)


def _apply_lineage_display_contract(df, view_name):
    """Apply final user-facing display names and remove columns not required per view.

    Internal processing still uses Target_/Source_ naming:
    - Target_* = Power BI/Fabric/Semantic model objects
    - Source_* = Snowflake/database/source-system objects

    This function is only for the final Streamlit display/download tables.
    """
    if df is None or df.empty:
        return df

    display_df = df.copy()

    view_contracts = {
        "source_db_lineage": {
            "rename": {
                "Target_Workspace_Name": "Workspace_Name",
                "Target_Report_Name": "Report_Name",
                "Target_Report_ID": "Report_ID",
                "Target_Dataset_ID": "Dataset_ID",
                "Target_Semantic_Table_View": "Semantic_Tables",
                "Source_Fully_Qualified_Object": "Source_Fully_Qualified_Name",
            },
            "columns": [
                "Workspace_Name",
                "Report_Name",
                "Report_ID",
                "Dataset_ID",
                "Semantic_Tables",
                "Source_Server",
                "Source_Object_Type",
                "Source_Query",
                "Source_Fully_Qualified_Name",
            ],
            "drop": [
                "Source_Database",
                "Source_Schema",
                "Source_Table_View",
                "Source_Query_Output_Columns",
            ],
        },
        "semantic_model_objects": {
            "rename": {
                "Target_Scope_Type": "Scope_Type",
                "Target_Workspace_Name": "Workspace_Name",
                "Target_Report_Name": "Report_Name",
                "Target_Semantic_Table_View": "Semantic_Tables",
                "Target_Semantic_Object_Type": "Semantic_Object_Type",
                "Target_Semantic_Object_Name": "Semantic_Object_Name",
                "Target_Data_Type": "Semantic_Data_Type",
                "Source_Column_Name_From_Model": "Semantic_Column_Name",
                "Target_DAX_Expression": "Semantic_DAX_Expression",
            },
            "columns": [
                "Scope_Type",
                "Workspace_Name",
                "Report_Name",
                "Semantic_Tables",
                "Semantic_Object_Type",
                "Semantic_Object_Name",
                "Semantic_Data_Type",
                "Semantic_Column_Name",
                "Semantic_DAX_Expression",
            ],
            "drop": [
                "Target_Report_ID",
                "Target_Dataset_ID",
                "Target_App_Name",
                "Target_Container_Name",
                "Target_Semantic_Workspace_Name",
                "Target_Semantic_Model_Name",
            ],
        },
        "measure_source_lineage": {
            "rename": {
                "Target_Scope_Type": "Scope_Type",
                "Target_Workspace_Name": "Workspace_Name",
                "Target_Report_Name": "Report_Name",
                "Target_Semantic_Object_Type": "Semantic_Object_Type",
                "Target_Semantic_Table_View": "Semantic_Tables",
                "Target_Semantic_Object_Name": "Semantic_Object_Name",
                "Target_DAX_Expression": "Semantic_DAX_Expression",
                "Target_Measure_Name": "Semantic_Measure_Name",
                "Source_Fully_Qualified_Object": "Source_Fully_Qualified_Name",
            },
            "columns": [
                "Scope_Type",
                "Workspace_Name",
                "Report_Name",
                "Semantic_Object_Type",
                "Semantic_Tables",
                "Semantic_Object_Name",
                "Semantic_DAX_Expression",
                "Semantic_Measure_Name",
                "Source_Query",
                "Source_Object_Type",
                "Source_Column_Name",
                "Source_Fully_Qualified_Name",
            ],
            "drop": [
                "Target_Report_ID",
                "Target_Dataset_ID",
                "Target_App_Name",
                "Target_Container_Name",
                "Target_Semantic_Workspace_Name",
                "Target_Semantic_Model_Name",
                "Source_Database",
                "Source_Schema",
                "Source_Table_View",
            ],
        },
    }

    contract = view_contracts.get(view_name)
    if not contract:
        return display_df

    # Drop explicitly unwanted columns before renaming.
    drop_cols = [col for col in contract.get("drop", []) if col in display_df.columns]
    if drop_cols:
        display_df = display_df.drop(columns=drop_cols)

    # Rename only display columns; internal data model remains unchanged.
    display_df = display_df.rename(columns=contract.get("rename", {}))

    # Fallback: measure name can be pruned as duplicate before reaching this display layer.
    if view_name == "measure_source_lineage" and "Semantic_Measure_Name" not in display_df.columns:
        if "Semantic_Object_Name" in display_df.columns:
            display_df["Semantic_Measure_Name"] = display_df["Semantic_Object_Name"]

    # Keep only the requested columns, in the exact requested order, if present.
    requested = contract.get("columns", [])
    ordered_existing = [col for col in requested if col in display_df.columns]
    display_df = display_df[ordered_existing]

    # Remove fully blank columns after view-specific cleanup.
    non_blank_cols = [col for col in display_df.columns if display_df[col].astype(str).str.strip().ne("").any()]
    return _normalize_dataframe_column_names(display_df[non_blank_cols])


def _handoff_value(row, column):
    value = row.get(column, "")
    text = str(value or "").strip()
    return "" if text.lower() in {"n/a", "na", "none", "null", "nan"} else text


def _split_source_column_names(value):
    """Return individual source columns from the semicolon-delimited parser output."""
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "na", "none", "null", "nan"}:
        return []

    columns = []
    seen = set()
    for item in text.split(";"):
        column = item.strip()
        marker = column.casefold()
        if not column or marker in seen:
            continue
        seen.add(marker)
        columns.append(column)
    return columns


def _explode_source_column_rows(display_df):
    """Create one lineage row per semicolon-delimited source column."""
    if display_df is None or display_df.empty or "Source_Column_Name" not in display_df.columns:
        return display_df

    expanded_rows = []
    for row in display_df.to_dict("records"):
        source_columns = _split_source_column_names(row.get("Source_Column_Name"))
        if not source_columns:
            expanded_rows.append(row)
            continue
        for source_column in source_columns:
            expanded_row = dict(row)
            expanded_row["Source_Column_Name"] = source_column
            expanded_rows.append(expanded_row)

    return pd.DataFrame(expanded_rows, columns=display_df.columns).reset_index(drop=True)


def _snowflake_handoff_label(row, include_source_column=False):
    parts = [
        _handoff_value(row, "Workspace_Name") or "No workspace",
        _handoff_value(row, "Report_Name") or "No report",
        _snowflake_object_domain_from_row(row) or "No source type",
        _handoff_value(row, "Source_Fully_Qualified_Name") or "No source object",
    ]
    if include_source_column:
        parts.append(_handoff_value(row, "Source_Column_Name") or "No source column")
    return " | ".join(parts)


def _snowflake_handoff_search_label(row, include_source_column=False):
    """Display Snowflake lineage choices with the most searchable lineage grain first."""
    source_object = _handoff_value(row, "Source_Fully_Qualified_Name") or "No source object"
    if include_source_column:
        parts = [
            _handoff_value(row, "Source_Column_Name") or "No source column",
            source_object,
            _snowflake_object_domain_from_row(row) or "No source type",
        ]
    else:
        parts = [
            source_object,
            _snowflake_object_domain_from_row(row) or "No source type",
        ]
    parts.extend([
        _handoff_value(row, "Report_Name") or "No report",
        _handoff_value(row, "Workspace_Name") or "No workspace",
    ])
    return " | ".join(parts)


def _get_snowflake_lineage_settings():
    """Return Snowflake lineage settings from config/app_settings.json."""
    return (Utils.load_app_settings().get("snowflake_lineage") or {}).copy()


def _snowflake_object_domain_from_row(row):
    raw_type = _handoff_value(row, "Source_Object_Type").upper()
    if "VIEW" in raw_type:
        return "VIEW"
    if "TABLE" in raw_type:
        return "TABLE"
    if "DYNAMIC" in raw_type:
        return "DYNAMIC TABLE"
    if "COLUMN" in raw_type:
        return "COLUMN"

    settings = _get_snowflake_lineage_settings()
    return str(settings.get("default_object_domain") or "VIEW").strip().upper()


def _merged_snowflake_connection_settings(feature_settings=None):
    settings = _get_snowflake_lineage_settings()
    for key, value in (feature_settings or {}).items():
        if key in {"account", "user", "password", "authenticator", "role", "warehouse", "database", "schema"}:
            if str(value or "").strip():
                settings[key] = value
    return settings


def _connect_snowflake(settings, config_name="snowflake_lineage"):
    try:
        import snowflake.connector  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "snowflake-connector-python is required for Snowflake features. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    required = ["account", "user"]
    missing = [key for key in required if not str(settings.get(key) or "").strip()]
    if missing:
        raise RuntimeError(f"Missing {config_name} config: {', '.join(missing)}")

    connect_kwargs = {
        "account": str(settings.get("account")).strip(),
        "user": str(settings.get("user")).strip(),
        "authenticator": str(settings.get("authenticator") or "snowflake").strip(),
    }
    optional_keys = ["password", "role", "warehouse", "database", "schema"]
    for key in optional_keys:
        value = str(settings.get(key) or "").strip()
        if value:
            connect_kwargs[key] = value

    return snowflake.connector.connect(**connect_kwargs)


def _connect_snowflake_for_lineage(settings):
    return _connect_snowflake(settings, "snowflake_lineage")


def _set_snowflake_statement_timeout(conn, timeout_seconds):
    timeout_seconds = int(timeout_seconds or 0)
    if timeout_seconds <= 0:
        return
    try:
        timeout_cursor = conn.cursor()
        timeout_cursor.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}")
        timeout_cursor.close()
    except Exception:
        pass


def _build_cortex_measure_definition_prompt(row, settings):
    instructions = str(settings.get("instructions") or "").strip()
    payload = _measure_definition_prompt_payload(row, settings)
    return (
        f"{instructions}\n\n"
        "Measure lineage metadata JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _build_openai_measure_definition_prompt(row, settings):
    payload = _measure_definition_prompt_payload(row, settings)
    return (
        "Measure lineage metadata JSON:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _extract_openai_response_text(response_json):
    if isinstance(response_json.get("output_text"), str) and response_json["output_text"].strip():
        return response_json["output_text"].strip()

    text_parts = []
    for output_item in response_json.get("output", []) or []:
        if not isinstance(output_item, dict):
            continue
        for content_item in output_item.get("content", []) or []:
            if not isinstance(content_item, dict):
                continue
            text = content_item.get("text")
            if text:
                text_parts.append(str(text))
    return "\n".join(text_parts).strip()


def _measure_definition_heading_text(line):
    """Return a normalized markdown heading text for measure detail sections."""
    candidate = str(line or "").strip()
    if not candidate or len(candidate) > 100:
        return ""
    candidate = re.sub(r"^#{1,6}\s*", "", candidate).strip()
    candidate = candidate.strip("* ").rstrip(":").strip()
    if not candidate or not re.fullmatch(r"[A-Za-z][A-Za-z /-]*", candidate):
        return ""
    return re.sub(r"\s+", " ", candidate).lower()


def _strip_unwanted_measure_definition_sections(definition):
    """Remove unwanted LLM sections before displaying/caching measure details."""
    text = str(definition or "").strip()
    if not text:
        return ""

    keep_headings = {
        "definition",
        "business meaning",
        "dax logic",
        "logic",
        "source lineage",
        "source notes",
    }
    output_lines = []
    skipping = False

    for line in text.splitlines():
        heading = _measure_definition_heading_text(line)
        if heading == "assumptions or gaps":
            skipping = True
            continue
        if skipping and heading in keep_headings:
            skipping = False
        if not skipping:
            output_lines.append(line)

    return "\n".join(output_lines).strip()


def _openai_model_supports_temperature(model):
    model_name = str(model or "").strip().lower()
    if model_name.startswith("gpt-5"):
        return False
    return True


def _openai_error_is_unsupported_temperature(response):
    try:
        error_json = response.json()
    except Exception:
        error_json = {}
    error = error_json.get("error") if isinstance(error_json, dict) else {}
    message = str((error or {}).get("message") or response.text or "").lower()
    param = str((error or {}).get("param") or "").lower()
    return response.status_code == 400 and (param == "temperature" or "temperature" in message)


def get_openai_measure_definition(row, settings):
    """Call the OpenAI Responses API for one selected measure definition."""
    if not settings.get("enabled", False):
        raise RuntimeError("Enable openai_measure_definitions in config/app_settings.json to use OpenAI.")

    api_key = str(settings.get("api_key") or "").strip()
    endpoint = str(settings.get("endpoint") or "https://api.openai.com/v1/responses").strip()
    model = str(settings.get("model") or "").strip()
    instructions = str(settings.get("instructions") or "").strip()
    timeout_seconds = int(settings.get("timeout_seconds") or 90)
    max_output_tokens = int(settings.get("max_output_tokens") or 900)

    missing = []
    if not api_key:
        missing.append("api_key")
    if not endpoint:
        missing.append("endpoint")
    if not model:
        missing.append("model")
    if not instructions:
        missing.append("instructions")
    if missing:
        raise RuntimeError(f"Missing openai_measure_definitions config: {', '.join(missing)}")

    request_body = {
        "model": model,
        "instructions": instructions,
        "input": _build_openai_measure_definition_prompt(row, settings),
        "max_output_tokens": max_output_tokens,
    }
    if settings.get("temperature") is not None and _openai_model_supports_temperature(model):
        request_body["temperature"] = float(settings.get("temperature"))

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(endpoint, headers=headers, json=request_body, timeout=timeout_seconds)
    if response.status_code >= 400 and "temperature" in request_body and _openai_error_is_unsupported_temperature(response):
        request_body.pop("temperature", None)
        response = requests.post(endpoint, headers=headers, json=request_body, timeout=timeout_seconds)

    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI API error {response.status_code}: {response.text}")

    definition = _extract_openai_response_text(response.json())
    if not definition:
        raise RuntimeError("OpenAI returned no measure detail text.")
    return _strip_unwanted_measure_definition_sections(definition)


def _quote_snowflake_identifier(identifier):
    return '"' + str(identifier or "").replace('"', '""') + '"'


def _split_snowflake_fqn(object_name):
    parts = [part.strip().strip('"') for part in str(object_name or "").split(".") if part.strip()]
    if len(parts) < 3:
        return None, None, None
    return parts[-3], parts[-2], parts[-1]


def _fetch_snowflake_object_metadata(conn, source_fqn, source_column):
    database, schema, object_name = _split_snowflake_fqn(source_fqn)
    if not database or not schema or not object_name:
        return {
            "source_fully_qualified_name": source_fqn,
            "source_column": source_column,
            "metadata_available": False,
            "metadata_message": "Source fully qualified name must be database.schema.object to query Snowflake metadata.",
        }

    metadata = {
        "source_database": database,
        "source_schema": schema,
        "source_object": object_name,
        "source_fully_qualified_name": source_fqn,
        "source_column": source_column,
        "metadata_available": True,
        "table_type": "",
        "table_comment": "",
        "column_data_type": "",
        "column_comment": "",
        "metadata_message": "",
    }
    info_schema = f"{_quote_snowflake_identifier(database)}.INFORMATION_SCHEMA"

    table_cursor = conn.cursor()
    try:
        table_cursor.execute(
            f"""
            SELECT TABLE_TYPE, COMMENT
            FROM {info_schema}.TABLES
            WHERE UPPER(TABLE_SCHEMA) = UPPER(%s)
              AND UPPER(TABLE_NAME) = UPPER(%s)
            LIMIT 1
            """,
            (schema, object_name),
        )
        table_row = table_cursor.fetchone()
        if table_row:
            metadata["table_type"] = str(table_row[0] or "")
            metadata["table_comment"] = str(table_row[1] or "")
    finally:
        table_cursor.close()

    if source_column:
        column_cursor = conn.cursor()
        try:
            column_cursor.execute(
                f"""
                SELECT DATA_TYPE, COMMENT
                FROM {info_schema}.COLUMNS
                WHERE UPPER(TABLE_SCHEMA) = UPPER(%s)
                  AND UPPER(TABLE_NAME) = UPPER(%s)
                  AND UPPER(COLUMN_NAME) = UPPER(%s)
                LIMIT 1
                """,
                (schema, object_name, source_column),
            )
            column_row = column_cursor.fetchone()
            if column_row:
                metadata["column_data_type"] = str(column_row[0] or "")
                metadata["column_comment"] = str(column_row[1] or "")
        finally:
            column_cursor.close()

    if not metadata["table_comment"] and not metadata["column_comment"]:
        metadata["metadata_message"] = "No Snowflake table/view or column comments were found for the selected source object."

    return metadata


def _describe_dax_pattern(dax_expression):
    dax = str(dax_expression or "").strip()
    dax_upper = dax.upper()
    patterns = [
        ("DISTINCTCOUNT", "counts distinct values"),
        ("COUNTROWS", "counts rows"),
        ("COUNT(", "counts non-blank values"),
        ("SUMX", "iterates rows and sums an expression"),
        ("SUM(", "sums a numeric expression or column"),
        ("AVERAGEX", "iterates rows and averages an expression"),
        ("AVERAGE(", "averages a numeric expression or column"),
        ("MIN(", "returns the minimum value"),
        ("MAX(", "returns the maximum value"),
    ]
    detected = [description for token, description in patterns if token in dax_upper]
    if "CALCULATE" in dax_upper:
        detected.append("evaluates logic under one or more filter conditions")
    if "DIVIDE" in dax_upper:
        detected.append("performs a guarded division")
    if detected:
        return "The DAX appears to " + ", and ".join(detected[:3]) + "."
    if dax:
        return "The measure result is defined by the supplied DAX expression."
    return "No DAX expression was available for this selected measure row."


def _build_metadata_measure_definition(row, metadata, provider_errors=None):
    payload = _measure_definition_prompt_payload(row, {"dax_expression_max_chars": 3000, "source_query_max_chars": 1200})
    measure_name = payload.get("measure_name") or "selected measure"
    semantic_table = payload.get("semantic_table") or "the semantic model"
    source_fqn = payload.get("source_fully_qualified_name") or metadata.get("source_fully_qualified_name") or "the mapped source object"
    source_column = payload.get("source_column") or metadata.get("source_column") or ""
    table_comment = metadata.get("table_comment") or ""
    column_comment = metadata.get("column_comment") or ""
    data_type = metadata.get("column_data_type") or ""
    table_type = metadata.get("table_type") or payload.get("source_object_type") or ""
    dax_logic = _describe_dax_pattern(payload.get("dax_expression"))

    source_bits = [source_fqn]
    if source_column:
        source_bits.append(source_column)
    source_text = ".".join(source_bits)

    lines = [
        f"**Definition:** `{measure_name}` is a Power BI measure in `{semantic_table}`. {dax_logic}",
        "",
        "**Business meaning:**",
    ]
    if column_comment:
        lines.append(f"- Snowflake column comment for `{source_text}`: {column_comment}")
    elif table_comment:
        lines.append(f"- Snowflake object comment for `{source_fqn}`: {table_comment}")
    else:
        lines.append("- No Snowflake comment metadata was available, so the business meaning is limited to the measure name, DAX, and lineage fields.")

    lines.extend([
        "",
        "**DAX logic:**",
        f"- `{payload.get('dax_expression') or 'No DAX expression found.'}`",
        "",
        "**Source lineage:**",
        f"- Source object: `{source_fqn}`" + (f" ({table_type})" if table_type else ""),
    ])
    if source_column:
        lines.append(f"- Source column: `{source_column}`" + (f" ({data_type})" if data_type else ""))
    if payload.get("source_query"):
        lines.append(f"- Source query/native SQL: `{payload.get('source_query')}`")

    lines.extend([
        "",
        "**Source notes:**",
    ])
    if metadata.get("metadata_message"):
        lines.append(f"- {metadata.get('metadata_message')}")
    if provider_errors:
        lines.append("- Earlier definition providers could not return a response.")
    lines.append("- This response uses only DAX, lineage, and Snowflake comments.")

    return "\n".join(lines)


def get_snowflake_metadata_measure_definition(row, settings, provider_errors=None):
    source_fqn = _prefer_non_na(row.get("Source_Fully_Qualified_Name"), row.get("Fully Qualified Source Object"))
    source_column = _prefer_non_na(row.get("Source_Column_Name"), row.get("Exact Source Column Name"))
    connection_settings = _merged_snowflake_connection_settings(settings)
    conn = _connect_snowflake(connection_settings, "snowflake_lineage / snowflake_cortex")
    try:
        _set_snowflake_statement_timeout(conn, settings.get("timeout_seconds") or 120)
        metadata = _fetch_snowflake_object_metadata(conn, source_fqn, source_column)
    finally:
        conn.close()
    return _build_metadata_measure_definition(row, metadata, provider_errors=provider_errors)


def _call_snowflake_cortex_measure_definition(row, settings):
    """Open Snowflake, call Cortex AI_COMPLETE for one selected measure, then close it."""
    model = str(settings.get("model") or "").strip()
    if not model:
        raise RuntimeError("Missing snowflake_cortex.model in config/app_settings.json.")

    instructions = str(settings.get("instructions") or "").strip()
    if not instructions:
        raise RuntimeError("Missing snowflake_cortex.instructions in config/app_settings.json.")

    function_name = str(settings.get("function") or "AI_COMPLETE").strip().upper()
    if function_name not in {"AI_COMPLETE", "SNOWFLAKE.CORTEX.COMPLETE"}:
        raise RuntimeError("snowflake_cortex.function must be AI_COMPLETE or SNOWFLAKE.CORTEX.COMPLETE.")

    model_parameters = {
        "temperature": float(settings.get("temperature") if settings.get("temperature") is not None else 0),
        "max_tokens": int(settings.get("max_tokens") or 900),
    }
    if bool(settings.get("guardrails", False)):
        model_parameters["guardrails"] = True

    prompt = _build_cortex_measure_definition_prompt(row, settings)
    connection_settings = _merged_snowflake_connection_settings(settings)
    conn = _connect_snowflake(connection_settings, "snowflake_lineage / snowflake_cortex")
    try:
        _set_snowflake_statement_timeout(conn, settings.get("timeout_seconds") or 120)
        cursor = conn.cursor()
        try:
            if function_name == "SNOWFLAKE.CORTEX.COMPLETE":
                query = "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s) AS MEASURE_DETAIL_DEFINITION"
                cursor.execute(query, (model, prompt))
            else:
                query = "SELECT AI_COMPLETE(%s, %s, PARSE_JSON(%s)) AS MEASURE_DETAIL_DEFINITION"
                cursor.execute(query, (model, prompt, json.dumps(model_parameters)))
            result = cursor.fetchone()
        finally:
            cursor.close()
    finally:
        conn.close()

    definition = str((result or [""])[0] or "").strip()
    if not definition:
        raise RuntimeError("Snowflake Cortex returned no measure detail text.")
    return _strip_unwanted_measure_definition_sections(definition)


def get_measure_definition(row, provider_choice="auto"):
    """Generate a measure detail using the selected provider or config order."""
    provider_choice = str(provider_choice or "auto").strip().lower()
    if provider_choice in {"auto", "config_order", "enabled"}:
        provider_order = _get_measure_definition_provider_order()
    else:
        provider_order = [provider_choice]

    snowflake_settings = _get_snowflake_cortex_settings()
    openai_settings = _get_openai_measure_definition_settings()
    errors = []
    tried_any = False

    for provider in provider_order:
        if provider in {"snowflake_cortex", "snowflake", "cortex"}:
            if not snowflake_settings.get("enabled", False):
                if provider_choice not in {"auto", "config_order", "enabled"}:
                    raise RuntimeError("Enable snowflake_cortex.enabled in config/app_settings.json to use Snowflake Cortex.")
                continue
            tried_any = True
            try:
                return _call_snowflake_cortex_measure_definition(row, snowflake_settings)
            except Exception as exc:
                errors.append(f"snowflake_cortex: {exc}")
                continue

        if provider in {"openai", "openai_measure_definitions", "open_api"}:
            if not openai_settings.get("enabled", False):
                if provider_choice not in {"auto", "config_order", "enabled"}:
                    raise RuntimeError("Enable openai_measure_definitions.enabled in config/app_settings.json to use OpenAI.")
                continue
            tried_any = True
            try:
                return get_openai_measure_definition(row, openai_settings)
            except Exception as exc:
                errors.append(f"openai: {exc}")
                continue

        if provider in {"snowflake_metadata", "metadata", "metadata_fallback"}:
            errors.append(
                "metadata: legacy metadata-only definitions are not used for measure definitions. "
                "Select OpenAI LLM or Snowflake Cortex."
            )
            continue

    if not tried_any:
        raise RuntimeError(
            "No measure definition provider is enabled. Enable snowflake_cortex.enabled "
            "or openai_measure_definitions.enabled."
        )

    raise RuntimeError("All enabled measure definition providers failed:\n" + "\n".join(errors))


def _fetch_snowflake_lineage_once(conn, object_name, object_domain, direction):
    """Run exactly one Snowflake lineage hop using DISTANCE=1."""
    if str(object_domain or "").strip().upper() == "COLUMN":
        query = """
            SELECT
                CONCAT_WS('.', SOURCE_OBJECT_DATABASE, SOURCE_OBJECT_SCHEMA, SOURCE_OBJECT_NAME) AS TABLE_NAME,
                SOURCE_COLUMN_NAME AS SOURCE_COLUMN,
                SOURCE_OBJECT_DOMAIN AS SOURCE_TYPE,
                DISTANCE AS LEVEL
            FROM TABLE(
                SNOWFLAKE.CORE.GET_LINEAGE(%s, %s, %s, %s)
            )
        """
    else:
        query = """
            SELECT
                CONCAT_WS('.', SOURCE_OBJECT_DATABASE, SOURCE_OBJECT_SCHEMA, SOURCE_OBJECT_NAME) AS TABLE_NAME,
                SOURCE_OBJECT_DOMAIN AS SOURCE_TYPE,
                DISTANCE AS LEVEL
            FROM TABLE(
                SNOWFLAKE.CORE.GET_LINEAGE(%s, %s, %s, %s)
            )
        """
    cursor = conn.cursor()
    try:
        cursor.execute(query, (object_name, object_domain, direction, 1))
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        cursor.close()


_COLUMN_LINEAGE_RESULT_COLUMNS = {
    "STARTING_SOURCE_FULLY_QUALIFIED_NAME": "Starting_Source_Fully_Qualified_Name",
    "STARTING_SOURCE_TYPE": "Starting_Source_Type",
    "SELECTED_SOURCE_COLUMN": "Selected_Source_Column",
    "PARENT_OBJECT_NAME": "Parent_Object_Name",
    "PARENT_OBJECT_TYPE": "Parent_Object_Type",
    "SOURCE_FULLY_QUALIFIED_NAME": "Source_Fully_Qualified_Name",
    "SOURCE_COLUMN_NAME": "Source_Column_Name",
    "SOURCE_OBJECT_TYPE": "Source_Object_Type",
    "LINEAGE_LEVEL": "Lineage_Level",
    "DIRECTION": "Direction",
    "COLUMN_TRANSFORMATION": "Column_Transformation",
    "MODIFICATION_SQL": "Modification_SQL",
}


def _validated_column_lineage_procedure_name(settings):
    procedure_name = str(
        settings.get("column_lineage_procedure")
        or "COMMON_DB.COMMON_SCHEMA.TRACE_COLUMN_LINEAGE"
    ).strip()
    identifier_parts = procedure_name.split(".")
    identifier_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
    if len(identifier_parts) != 3 or any(not identifier_pattern.fullmatch(part) for part in identifier_parts):
        raise RuntimeError(
            "snowflake_lineage.column_lineage_procedure must be an unquoted "
            "DATABASE.SCHEMA.PROCEDURE name."
        )
    return ".".join(identifier_parts)


def _fetch_snowflake_column_lineage(
    conn,
    object_name,
    column_name,
    direction,
    depth,
    procedure_name,
):
    """Call TRACE_COLUMN_LINEAGE and normalize its table result for the UI."""
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"CALL {procedure_name}(%s, %s, %s, %s)",
            (object_name, column_name, direction, depth),
        )
        raw_columns = [str(column[0]).strip().upper() for column in cursor.description]
        missing_columns = [
            column for column in _COLUMN_LINEAGE_RESULT_COLUMNS if column not in raw_columns
        ]
        if missing_columns:
            raise RuntimeError(
                f"{procedure_name} returned an unsupported result. Missing columns: "
                + ", ".join(missing_columns)
            )

        normalized_rows = []
        for values in cursor.fetchall():
            raw_row = dict(zip(raw_columns, values))
            normalized_rows.append({
                ui_column: raw_row.get(sql_column)
                for sql_column, ui_column in _COLUMN_LINEAGE_RESULT_COLUMNS.items()
            })
        return normalized_rows
    finally:
        cursor.close()


def get_snowflake_column_lineage(start_object_name, start_column_name, settings):
    """Open one Snowflake session, call the column-lineage procedure, and close it."""
    direction = str(settings.get("direction") or "UPSTREAM").strip().upper()
    max_depth = max(1, int(settings.get("max_depth") or 20))
    statement_timeout = int(settings.get("statement_timeout_seconds") or 120)
    procedure_name = _validated_column_lineage_procedure_name(settings)

    conn = _connect_snowflake_for_lineage(settings)
    try:
        _set_snowflake_statement_timeout(conn, statement_timeout)
        return _fetch_snowflake_column_lineage(
            conn,
            start_object_name,
            start_column_name,
            direction,
            max_depth,
            procedure_name,
        )
    finally:
        conn.close()


def get_recursive_snowflake_lineage(start_object_name, start_object_domain, settings):
    """Open a Snowflake session, recursively walk one-hop GET_LINEAGE, then close it."""
    direction = str(settings.get("direction") or "UPSTREAM").strip().upper()
    max_depth = max(1, int(settings.get("max_depth") or 20))
    statement_timeout = int(settings.get("statement_timeout_seconds") or 120)

    conn = _connect_snowflake_for_lineage(settings)
    try:
        _set_snowflake_statement_timeout(conn, statement_timeout)

        results = []
        visited = set()
        frontier = [(start_object_name, start_object_domain, 0)]

        while frontier:
            next_frontier = []
            for current_object, current_domain, parent_level in frontier:
                current_key = (str(current_object).upper(), str(current_domain).upper())
                if current_key in visited:
                    continue
                visited.add(current_key)

                if parent_level >= max_depth:
                    continue

                rows = _fetch_snowflake_lineage_once(conn, current_object, current_domain, direction)
                for row in rows:
                    child_object = str(row.get("TABLE_NAME") or "").strip()
                    child_column = str(row.get("SOURCE_COLUMN") or "").strip()
                    child_domain = str(row.get("SOURCE_TYPE") or "").strip().upper()
                    if not child_object or not child_domain:
                        continue

                    next_domain = "COLUMN" if child_column else child_domain
                    child_lineage_object = f"{child_object}.{child_column}" if child_column else child_object
                    level = parent_level + 1
                    results.append({
                        "Parent_Object_Name": current_object,
                        "Parent_Object_Type": current_domain,
                        "Source_Fully_Qualified_Name": child_object,
                        "Source_Column_Name": child_column,
                        "Source_Object_Type": next_domain,
                        "Lineage_Level": level,
                        "Direction": direction,
                    })

                    child_key = (child_lineage_object.upper(), next_domain.upper())
                    if child_key not in visited and level < max_depth:
                        next_frontier.append((child_lineage_object, next_domain, level))

            if not next_frontier:
                break
            frontier = next_frontier

        return results
    finally:
        conn.close()


def _lineage_graph_node_id(object_name, column_name=""):
    object_part = str(object_name or "").strip()
    column_part = str(column_name or "").strip()
    return f"{object_part}.{column_part}" if column_part else object_part


def _lineage_graph_label(object_name, column_name="", object_type=""):
    object_part = str(object_name or "").strip()
    column_part = str(column_name or "").strip()
    type_part = str(object_type or "").strip()
    if column_part:
        return f"{object_part}\n{column_part}"
    if type_part:
        return f"{object_part}\n{type_part}"
    return object_part


def _split_column_lineage_object(value):
    parts = [part for part in str(value or "").strip().split(".") if part]
    if len(parts) <= 3:
        return str(value or "").strip(), ""
    return ".".join(parts[:-1]), parts[-1]


def _build_snowflake_lineage_graph(lineage_rows, payload, lineage_grain, source_object, source_type):
    root_column = payload.get("source_column") if lineage_grain == "COLUMN" else ""
    root_object = payload.get("source_fully_qualified_name") or source_object
    root_id = _lineage_graph_node_id(root_object, root_column)
    if lineage_grain == "COLUMN" and root_column:
        root_label = _lineage_graph_label(root_object, root_column, "COLUMN")
    else:
        root_label = _lineage_graph_label(root_object, "", source_type)

    nodes = {
        root_id: {
            "id": root_id,
            "label": root_label,
            "objectName": root_object,
            "columnName": root_column or "",
            "type": source_type,
            "level": 0,
            "isRoot": True,
            "details": [],
        }
    }
    edges = []

    for row in lineage_rows or []:
        parent_object_name = row.get("Parent_Object_Name")
        parent_object_for_label, parent_column_for_label = (
            _split_column_lineage_object(parent_object_name) if lineage_grain == "COLUMN" else (parent_object_name, "")
        )
        parent_id = _lineage_graph_node_id(parent_object_name)
        child_object = row.get("Source_Fully_Qualified_Name")
        child_column = row.get("Source_Column_Name")
        child_type = row.get("Source_Object_Type")
        child_id = _lineage_graph_node_id(child_object, child_column)
        if not parent_id or not child_id:
            continue

        raw_lineage_level = row.get("Lineage_Level")
        try:
            lineage_level = int(raw_lineage_level if raw_lineage_level not in {None, ""} else 1)
        except (TypeError, ValueError):
            lineage_level = 1
        parent_level = max(0, lineage_level - 1)
        child_level = max(0, lineage_level)

        step_detail = {
            "level": lineage_level,
            "parentObject": str(parent_object_name or ""),
            "transformation": str(row.get("Column_Transformation") or ""),
            "modificationSql": str(row.get("Modification_SQL") or ""),
        }

        if parent_id not in nodes:
            nodes[parent_id] = {
                "id": parent_id,
                "label": _lineage_graph_label(parent_object_for_label, parent_column_for_label, row.get("Parent_Object_Type") or ""),
                "objectName": parent_object_for_label,
                "columnName": parent_column_for_label or "",
                "type": row.get("Parent_Object_Type") or "",
                "level": parent_level,
                "isRoot": parent_id == root_id,
                "details": [],
            }
        else:
            nodes[parent_id]["level"] = min(nodes[parent_id].get("level", parent_level), parent_level)

        if child_id not in nodes:
            nodes[child_id] = {
                "id": child_id,
                "label": _lineage_graph_label(child_object, child_column, child_type),
                "objectName": child_object,
                "columnName": child_column or "",
                "type": child_type or "",
                "level": child_level,
                "isRoot": False,
                "details": [],
            }
        else:
            nodes[child_id]["level"] = min(nodes[child_id].get("level", child_level), child_level)

        if lineage_grain == "COLUMN":
            child_details = nodes[child_id].setdefault("details", [])
            if step_detail not in child_details:
                child_details.append(step_detail)

        edge_key = (parent_id, child_id)
        # TRACE_COLUMN_LINEAGE includes the selected column as level 0 so its
        # transformation is available in the table. It is not a graph edge.
        if parent_id != child_id and edge_key not in {(edge["source"], edge["target"]) for edge in edges}:
            edges.append({
                "source": parent_id,
                "target": child_id,
                "transformation": step_detail["transformation"],
                "modificationSql": step_detail["modificationSql"],
            })

    child_counts = {}
    for edge in edges:
        child_counts[edge["source"]] = child_counts.get(edge["source"], 0) + 1
    for node in nodes.values():
        node["isLeaf"] = child_counts.get(node["id"], 0) == 0

    return {
        "rootId": root_id,
        "nodes": list(nodes.values()),
        "edges": edges,
        "grain": lineage_grain,
    }


def render_snowflake_lineage_diagram(lineage_rows, payload, lineage_grain, source_object, source_type, key_prefix):
    graph = _build_snowflake_lineage_graph(lineage_rows, payload, lineage_grain, source_object, source_type)
    if not graph["nodes"]:
        return

    component_id = f"{key_prefix}_lineage_diagram_{hashlib.md5(json.dumps(graph, sort_keys=True).encode('utf-8')).hexdigest()[:10]}"
    title = "Snowflake Column Lineage Diagram" if lineage_grain == "COLUMN" else "Snowflake Table Lineage Diagram"
    graph_json = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    title_json = json.dumps(title)
    component_json = json.dumps(component_id)
    diagram_html = f"""
    <div id={component_json} class="sf-lineage-shell">
      <div class="sf-lineage-header">
        <div class="sf-lineage-title"></div>
        <div class="sf-lineage-legend">
          <span><i class="root"></i>Selected source</span>
          <span><i class="expandable"></i>Upstream object</span>
          <span><i class="leaf"></i>Last node</span>
        </div>
      </div>
      <div class="sf-lineage-workspace">
        <div class="sf-lineage-scroll">
          <svg class="sf-lineage-svg" role="img"></svg>
        </div>
        <aside class="sf-lineage-detail" hidden>
          <div class="sf-lineage-detail-header">
            <div>
              <div class="sf-lineage-detail-title"></div>
              <div class="sf-lineage-detail-context"></div>
            </div>
            <button class="sf-lineage-detail-close" type="button" title="Close details" aria-label="Close details">&times;</button>
          </div>
          <div class="sf-lineage-detail-grid">
            <div>
              <div class="sf-lineage-detail-label">Column transformation</div>
              <pre class="sf-lineage-transformation"></pre>
            </div>
            <div>
              <div class="sf-lineage-detail-label">Modification SQL</div>
              <pre class="sf-lineage-modification-sql"></pre>
            </div>
          </div>
        </aside>
      </div>
    </div>
    <style>
      .sf-lineage-shell {{
        font-family: Inter, "Segoe UI", Arial, sans-serif;
        color: #0f172a;
        background: #ffffff;
      }}
      .sf-lineage-header {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 16px;
        margin: 0 0 10px 0;
      }}
      .sf-lineage-title {{
        font-size: 16px;
        font-weight: 700;
        letter-spacing: 0;
      }}
      .sf-lineage-legend {{
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
        color: #475569;
        font-size: 12px;
        font-weight: 600;
      }}
      .sf-lineage-legend span {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }}
      .sf-lineage-legend i {{
        width: 12px;
        height: 12px;
        border-radius: 4px;
        border: 1px solid #94a3b8;
        display: inline-block;
      }}
      .sf-lineage-legend .root {{
        background: #dbeafe;
        border-color: #60a5fa;
      }}
      .sf-lineage-legend .expandable {{
        background: #dcfce7;
        border-color: #86efac;
      }}
      .sf-lineage-legend .leaf {{
        background: #ffe4e6;
        border-color: #fda4af;
      }}
      .sf-lineage-workspace {{
        position: relative;
        display: grid;
        grid-template-columns: minmax(0, 1fr);
        gap: 12px;
        align-items: stretch;
        min-width: 0;
      }}
      .sf-lineage-workspace.has-details {{
        grid-template-columns: minmax(0, 1fr) minmax(360px, 430px);
      }}
      .sf-lineage-detail {{
        position: sticky;
        top: 0;
        display: flex;
        flex-direction: column;
        box-sizing: border-box;
        height: 710px;
        min-width: 0;
        overflow: hidden;
        border: 1px solid #cbd5e1;
        border-left: 4px solid #2563eb;
        border-radius: 6px;
        background: #ffffff;
        margin: 0;
        padding: 12px 14px 14px;
        box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08);
      }}
      .sf-lineage-detail[hidden] {{
        display: none;
      }}
      .sf-lineage-detail-header {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 10px;
      }}
      .sf-lineage-detail-title {{
        color: #0f172a;
        font-size: 14px;
        font-weight: 750;
        overflow-wrap: anywhere;
      }}
      .sf-lineage-detail-context {{
        color: #64748b;
        font-size: 11px;
        margin-top: 3px;
        overflow-wrap: anywhere;
      }}
      .sf-lineage-detail-close {{
        width: 30px;
        height: 30px;
        border: 1px solid #cbd5e1;
        border-radius: 4px;
        background: #ffffff;
        color: #475569;
        cursor: pointer;
        font-size: 20px;
        line-height: 24px;
        flex: 0 0 auto;
      }}
      .sf-lineage-detail-close:hover {{
        border-color: #64748b;
        color: #0f172a;
      }}
      .sf-lineage-detail-grid {{
        display: grid;
        grid-template-rows: minmax(120px, 0.45fr) minmax(240px, 1fr);
        gap: 12px;
        flex: 1;
        min-height: 0;
      }}
      .sf-lineage-detail-grid > div {{
        display: flex;
        flex-direction: column;
        min-height: 0;
      }}
      .sf-lineage-detail-label {{
        color: #334155;
        font-size: 11px;
        font-weight: 700;
        margin-bottom: 5px;
        text-transform: uppercase;
      }}
      .sf-lineage-detail pre {{
        box-sizing: border-box;
        width: 100%;
        min-height: 0;
        max-height: none;
        flex: 1;
        overflow: auto;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        margin: 0;
        padding: 9px 10px;
        border: 1px solid #e2e8f0;
        border-radius: 4px;
        background: #f8fafc;
        color: #1e293b;
        font: 11px/1.45 Consolas, "SFMono-Regular", monospace;
      }}
      .sf-lineage-scroll {{
        position: relative;
        box-sizing: border-box;
        border: 1px solid #d7e0ec;
        border-radius: 8px;
        background: #f8fafc;
        overflow: auto;
        height: 710px;
        min-width: 0;
      }}
      .sf-lineage-svg {{
        display: block;
      }}
      .sf-node rect {{
        stroke-width: 1.4px;
        rx: 8px;
        transition: opacity 140ms ease, stroke 140ms ease, stroke-width 140ms ease;
      }}
      .sf-node text {{
        fill: #0f172a;
        font-size: 12px;
        font-weight: 600;
        transition: opacity 140ms ease;
      }}
      .sf-node .sf-type {{
        fill: #475569;
        font-size: 10px;
        font-weight: 500;
      }}
      .sf-node .sf-column {{
        fill: #1e293b;
        font-size: 12px;
        font-weight: 800;
      }}
      .sf-node .sf-transform-summary {{
        fill: #334155;
        font-size: 10px;
        font-weight: 600;
      }}
      .sf-node .sf-sql-summary {{
        fill: #64748b;
        font-size: 9px;
        font-weight: 500;
      }}
      .sf-column-pill {{
        fill: #ffffff;
        stroke: #cbd5e1;
        stroke-width: 1px;
        rx: 6px;
      }}
      .sf-node.clickable {{
        cursor: pointer;
      }}
      .sf-node.is-active rect {{
        stroke: #2563eb;
        stroke-width: 2.4px;
      }}
      .sf-node.is-muted {{
        opacity: 0.24;
      }}
      .sf-edge-group {{
        pointer-events: none;
        transition: opacity 140ms ease;
      }}
      .sf-edge-halo {{
        fill: none;
        stroke: #f8fafc;
        stroke-width: 6px;
        stroke-linecap: round;
        opacity: 0.96;
      }}
      .sf-edge {{
        fill: none;
        stroke: #64748b;
        stroke-width: 1.45px;
        stroke-linecap: round;
        opacity: 0.58;
        transition: opacity 140ms ease, stroke 140ms ease, stroke-width 140ms ease;
      }}
      .sf-edge-group.is-active .sf-edge {{
        stroke: #2563eb;
        stroke-width: 2.6px;
        opacity: 1;
      }}
      .sf-edge-group.is-active .sf-edge-halo {{
        stroke-width: 7px;
      }}
      .sf-edge-group.is-muted {{
        opacity: 0.06;
      }}
      .sf-edge-arrow {{
        fill: #64748b;
      }}
      .sf-edge-arrow-active {{
        fill: #2563eb;
      }}
      .sf-expand-badge {{
        fill: #ffffff;
        stroke: #2563eb;
        stroke-width: 1.2px;
      }}
      .sf-expand-label {{
        fill: #1d4ed8;
        font-size: 12px;
        font-weight: 800;
      }}
      .sf-collapse-badge {{
        fill: #ffffff;
        stroke: #64748b;
        stroke-width: 1.2px;
      }}
      .sf-collapse-label {{
        fill: #475569;
        font-size: 13px;
        font-weight: 900;
      }}
      .sf-detail-badge {{
        fill: #eff6ff;
        stroke: #2563eb;
        stroke-width: 1.2px;
      }}
      .sf-detail-label {{
        fill: #1d4ed8;
        font-size: 12px;
        font-weight: 800;
      }}
      @media (max-width: 980px) {{
        .sf-lineage-workspace.has-details {{
          grid-template-columns: minmax(0, 1fr) minmax(300px, 38%);
        }}
      }}
      @media (max-width: 760px) {{
        .sf-lineage-workspace.has-details {{
          grid-template-columns: 1fr;
        }}
        .sf-lineage-detail {{
          position: absolute;
          z-index: 20;
          top: 10px;
          right: 10px;
          width: calc(100% - 20px);
          height: 680px;
        }}
      }}
    </style>
    <script>
      (function() {{
        const mount = document.getElementById({component_json});
        const graph = {graph_json};
        const title = {title_json};
        const svg = mount.querySelector(".sf-lineage-svg");
        const workspace = mount.querySelector(".sf-lineage-workspace");
        const detailPanel = mount.querySelector(".sf-lineage-detail");
        const detailTitle = mount.querySelector(".sf-lineage-detail-title");
        const detailContext = mount.querySelector(".sf-lineage-detail-context");
        const transformationDetail = mount.querySelector(".sf-lineage-transformation");
        const modificationSqlDetail = mount.querySelector(".sf-lineage-modification-sql");
        mount.querySelector(".sf-lineage-title").textContent = title;
        mount.querySelector(".sf-lineage-detail-close").addEventListener("click", () => {{
          detailPanel.hidden = true;
          workspace.classList.remove("has-details");
          requestAnimationFrame(render);
        }});

        const nodeById = new Map(graph.nodes.map(node => [node.id, node]));
        const childrenById = new Map();
        const parentsById = new Map();
        graph.edges.forEach(edge => {{
          if (!childrenById.has(edge.source)) childrenById.set(edge.source, []);
          childrenById.get(edge.source).push(edge.target);
          if (!parentsById.has(edge.target)) parentsById.set(edge.target, []);
          parentsById.get(edge.target).push(edge.source);
        }});
        const maxLevel = Math.max(0, ...graph.nodes.map(node => node.level || 0));
        const expandedUntil = {{}};
        let renderedNodeGroups = new Map();
        let renderedEdgeGroups = [];

        function detailValue(value) {{
          const text = String(value || "").trim();
          return text || "Not available";
        }}

        function detailBlocks(details, fieldName) {{
          return details.map((detail, index) => {{
            const pathHeader = details.length > 1
              ? `Path ${{index + 1}} | Level ${{detail.level ?? ""}}\\nParent: ${{detail.parentObject || "Not available"}}\\n`
              : "";
            return pathHeader + detailValue(detail[fieldName]);
          }}).join("\\n\\n");
        }}

        function showNodeDetails(node) {{
          const details = Array.isArray(node?.details) ? node.details : [];
          if (!details.length) return;
          const panelWasHidden = detailPanel.hidden;
          detailTitle.textContent = node.columnName
            ? `${{node.objectName}} | ${{node.columnName}}`
            : String(node.objectName || node.label || "Lineage step");
          const levels = Array.from(new Set(details.map(detail => detail.level))).join(", ");
          detailContext.textContent = `Level ${{levels || node.level || 0}}${{node.type ? " | " + node.type : ""}}`;
          transformationDetail.textContent = detailBlocks(details, "transformation");
          modificationSqlDetail.textContent = detailBlocks(details, "modificationSql");
          detailPanel.hidden = false;
          workspace.classList.add("has-details");
          if (panelWasHidden) requestAnimationFrame(render);
        }}

        function expansionBase(node) {{
          return Math.max(2, node.level || 0);
        }}

        function isExpanded(node) {{
          return (expandedUntil[node.id] || 0) > expansionBase(node);
        }}

        function expandNode(node) {{
          const current = expandedUntil[node.id] || expansionBase(node);
          expandedUntil[node.id] = Math.min(maxLevel, current + 2);
        }}

        function collapseNode(nodeId) {{
          const queue = [nodeId];
          while (queue.length) {{
            const current = queue.shift();
            delete expandedUntil[current];
            (childrenById.get(current) || []).forEach(childId => queue.push(childId));
          }}
        }}

        function centerStage() {{
          const stage = mount.querySelector(".sf-lineage-scroll");
          if (!stage) return;
          requestAnimationFrame(() => {{
            stage.scrollLeft = Math.max(0, (stage.scrollWidth - stage.clientWidth) / 2);
          }});
        }}

        function visibleIds() {{
          const visible = new Set();
          const queue = [{{ id: graph.rootId, limit: 2 }}];
          while (queue.length) {{
            const item = queue.shift();
            if (visible.has(item.id)) continue;
            visible.add(item.id);
            const node = nodeById.get(item.id);
            const ownLimit = Math.max(item.limit || 2, expandedUntil[item.id] || 0);
            const children = childrenById.get(item.id) || [];
            children.forEach(childId => {{
              const child = nodeById.get(childId);
              if (!child) return;
              if ((child.level || 0) <= ownLimit) {{
                queue.push({{ id: childId, limit: ownLimit }});
              }}
            }});
          }}
          graph.nodes.forEach(node => {{
            if ((node.level || 0) <= 2) visible.add(node.id);
          }});
          return visible;
        }}

        function splitLabel(label) {{
          const rawLines = String(label || "").split("\\n");
          const lines = [];
          rawLines.forEach(raw => {{
            const text = raw.trim();
            if (!text) return;
            if (text.length <= 42) {{
              lines.push(text);
              return;
            }}
            let rest = text;
            while (rest.length > 42) {{
              let cut = rest.lastIndexOf(".", 42);
              if (cut < 18) cut = rest.lastIndexOf("_", 42);
              if (cut < 18) cut = 42;
              lines.push(rest.slice(0, cut + 1));
              rest = rest.slice(cut + 1);
            }}
            if (rest) lines.push(rest);
          }});
          return lines.slice(0, 4);
        }}

        function splitObjectName(node) {{
          const maxLines = node.columnName ? 2 : 4;
          return splitLabel(node.objectName || node.label).slice(0, maxLines);
        }}

        function truncateText(text, maxLength) {{
          const raw = String(text || "");
          if (raw.length <= maxLength) return raw;
          return raw.slice(0, Math.max(0, maxLength - 3)) + "...";
        }}

        function nodeColor(node) {{
          if (node.isLeaf) return "#ffe4e6";
          if (node.isRoot) return "#dbeafe";
          if ((childrenById.get(node.id) || []).length) return "#dcfce7";
          return "#e2e8f0";
        }}

        function orderLevels(levels) {{
          const levelKeys = Array.from(levels.keys()).sort((a, b) => a - b);
          levels.forEach(nodes => nodes.sort((a, b) => String(a.label).localeCompare(String(b.label))));

          function indexMap(level) {{
            return new Map((levels.get(level) || []).map((node, index) => [node.id, index]));
          }}

          function barycenter(nodeIds, positions) {{
            const values = nodeIds.map(id => positions.get(id)).filter(value => value !== undefined);
            if (!values.length) return Number.POSITIVE_INFINITY;
            return values.reduce((sum, value) => sum + value, 0) / values.length;
          }}

          for (let pass = 0; pass < 4; pass += 1) {{
            for (let keyIndex = 1; keyIndex < levelKeys.length; keyIndex += 1) {{
              const level = levelKeys[keyIndex];
              const previousPositions = indexMap(levelKeys[keyIndex - 1]);
              levels.get(level).sort((a, b) => {{
                const aCenter = barycenter(parentsById.get(a.id) || [], previousPositions);
                const bCenter = barycenter(parentsById.get(b.id) || [], previousPositions);
                return aCenter - bCenter || String(a.label).localeCompare(String(b.label));
              }});
            }}
            for (let keyIndex = levelKeys.length - 2; keyIndex >= 0; keyIndex -= 1) {{
              const level = levelKeys[keyIndex];
              const nextPositions = indexMap(levelKeys[keyIndex + 1]);
              levels.get(level).sort((a, b) => {{
                const aCenter = barycenter(childrenById.get(a.id) || [], nextPositions);
                const bCenter = barycenter(childrenById.get(b.id) || [], nextPositions);
                return aCenter - bCenter || String(a.label).localeCompare(String(b.label));
              }});
            }}
          }}
        }}

        function edgeKey(sourceId, targetId) {{
          return JSON.stringify([sourceId, targetId]);
        }}

        function lineageBranch(nodeId, visible) {{
          const nodeIds = new Set([nodeId]);
          const edgeKeys = new Set();
          const parentQueue = [nodeId];
          const childQueue = [nodeId];

          while (parentQueue.length) {{
            const current = parentQueue.shift();
            (parentsById.get(current) || []).forEach(parentId => {{
              if (!visible.has(parentId)) return;
              edgeKeys.add(edgeKey(parentId, current));
              if (!nodeIds.has(parentId)) {{
                nodeIds.add(parentId);
                parentQueue.push(parentId);
              }}
            }});
          }}

          while (childQueue.length) {{
            const current = childQueue.shift();
            (childrenById.get(current) || []).forEach(childId => {{
              if (!visible.has(childId)) return;
              edgeKeys.add(edgeKey(current, childId));
              if (!nodeIds.has(childId)) {{
                nodeIds.add(childId);
                childQueue.push(childId);
              }}
            }});
          }}
          return {{ nodeIds, edgeKeys }};
        }}

        function focusBranch(nodeId, visible) {{
          const branch = lineageBranch(nodeId, visible);
          renderedNodeGroups.forEach((group, id) => {{
            group.classList.toggle("is-active", id === nodeId);
            group.classList.toggle("is-muted", !branch.nodeIds.has(id));
          }});
          renderedEdgeGroups.forEach(item => {{
            const active = branch.edgeKeys.has(item.key);
            item.group.classList.toggle("is-active", active);
            item.group.classList.toggle("is-muted", !active);
            item.path.setAttribute("marker-end", active ? "url(#arrowActive)" : "url(#arrow)");
          }});
        }}

        function clearBranchFocus() {{
          renderedNodeGroups.forEach(group => group.classList.remove("is-active", "is-muted"));
          renderedEdgeGroups.forEach(item => {{
            item.group.classList.remove("is-active", "is-muted");
            item.path.setAttribute("marker-end", "url(#arrow)");
          }});
        }}

        function render() {{
          const visible = visibleIds();
          const visibleNodes = graph.nodes.filter(node => visible.has(node.id));
          const levels = new Map();
          visibleNodes.forEach(node => {{
            const level = node.level || 0;
            if (!levels.has(level)) levels.set(level, []);
            levels.get(level).push(node);
          }});
          orderLevels(levels);

          const positions = new Map();
          const stage = mount.querySelector(".sf-lineage-scroll");
          const nodeWidth = 330;
          const nodeHeight = graph.grain === "COLUMN" ? 126 : 76;
          const siblingGap = 34;
          const levelGap = graph.grain === "COLUMN" ? 82 : 92;
          const marginX = 52;
          const marginY = 38;
          const levelKeys = Array.from(levels.keys());
          const layoutMaxLevel = Math.max(2, ...levelKeys);
          let maxLevelWidth = nodeWidth;
          levels.forEach(nodes => {{
            const levelWidth = nodes.length * nodeWidth + Math.max(0, nodes.length - 1) * siblingGap;
            maxLevelWidth = Math.max(maxLevelWidth, levelWidth);
          }});

          const viewportWidth = stage ? Math.max(860, stage.clientWidth - 4) : 860;
          const width = Math.max(viewportWidth, marginX * 2 + maxLevelWidth);
          const height = Math.max(540, marginY * 2 + (layoutMaxLevel + 1) * nodeHeight + layoutMaxLevel * levelGap);
          levels.forEach((nodes, level) => {{
            const levelWidth = nodes.length * nodeWidth + Math.max(0, nodes.length - 1) * siblingGap;
            const startX = (width - levelWidth) / 2;
            const y = marginY + level * (nodeHeight + levelGap);
            nodes.forEach((node, index) => {{
              positions.set(node.id, {{
                x: startX + index * (nodeWidth + siblingGap),
                y,
              }});
            }});
          }});
          svg.setAttribute("width", width);
          svg.setAttribute("height", height);
          svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
          svg.innerHTML = "";
          renderedNodeGroups = new Map();
          renderedEdgeGroups = [];

          const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
          const filter = document.createElementNS("http://www.w3.org/2000/svg", "filter");
          filter.setAttribute("id", "cardShadow");
          filter.setAttribute("x", "-12%");
          filter.setAttribute("y", "-18%");
          filter.setAttribute("width", "124%");
          filter.setAttribute("height", "140%");
          const shadow = document.createElementNS("http://www.w3.org/2000/svg", "feDropShadow");
          shadow.setAttribute("dx", "0");
          shadow.setAttribute("dy", "5");
          shadow.setAttribute("stdDeviation", "5");
          shadow.setAttribute("flood-color", "#0f172a");
          shadow.setAttribute("flood-opacity", "0.10");
          filter.appendChild(shadow);
          defs.appendChild(filter);
          const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
          marker.setAttribute("id", "arrow");
          marker.setAttribute("markerWidth", "10");
          marker.setAttribute("markerHeight", "10");
          marker.setAttribute("refX", "8");
          marker.setAttribute("refY", "3");
          marker.setAttribute("orient", "auto");
          const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
          path.setAttribute("d", "M0,0 L0,6 L9,3 z");
          path.setAttribute("class", "sf-edge-arrow");
          marker.appendChild(path);
          defs.appendChild(marker);
          const activeMarker = marker.cloneNode(true);
          activeMarker.setAttribute("id", "arrowActive");
          activeMarker.querySelector("path").setAttribute("class", "sf-edge-arrow-active");
          defs.appendChild(activeMarker);
          svg.appendChild(defs);

          const visibleEdges = graph.edges.filter(edge => visible.has(edge.source) && visible.has(edge.target));
          const edgesBySource = new Map();
          const edgesByTarget = new Map();
          visibleEdges.forEach(edge => {{
            if (!edgesBySource.has(edge.source)) edgesBySource.set(edge.source, []);
            if (!edgesByTarget.has(edge.target)) edgesByTarget.set(edge.target, []);
            edgesBySource.get(edge.source).push(edge);
            edgesByTarget.get(edge.target).push(edge);
          }});
          edgesBySource.forEach(edges => edges.sort((a, b) => {{
            const aPosition = positions.get(a.target);
            const bPosition = positions.get(b.target);
            return (aPosition ? aPosition.x : 0) - (bPosition ? bPosition.x : 0);
          }}));
          edgesByTarget.forEach(edges => edges.sort((a, b) => {{
            const aPosition = positions.get(a.source);
            const bPosition = positions.get(b.source);
            return (aPosition ? aPosition.x : 0) - (bPosition ? bPosition.x : 0);
          }}));

          function connectionPortX(edge, groupedEdges, groupId, nodeX) {{
            const connections = groupedEdges.get(groupId) || [edge];
            const index = Math.max(0, connections.indexOf(edge));
            return nodeX + nodeWidth * ((index + 1) / (connections.length + 1));
          }}

          visibleEdges.forEach(edge => {{
            const source = positions.get(edge.source);
            const target = positions.get(edge.target);
            if (!source || !target) return;
            const startX = connectionPortX(edge, edgesByTarget, edge.target, target.x);
            const startY = target.y - 9;
            const endX = connectionPortX(edge, edgesBySource, edge.source, source.x);
            const endY = source.y + nodeHeight + 9;
            const midY = (startY + endY) / 2;
            const pathData = `M ${{startX}} ${{startY}} C ${{startX}} ${{midY}}, ${{endX}} ${{midY}}, ${{endX}} ${{endY}}`;

            const edgeGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
            edgeGroup.setAttribute("class", "sf-edge-group");

            const halo = document.createElementNS("http://www.w3.org/2000/svg", "path");
            halo.setAttribute("d", pathData);
            halo.setAttribute("class", "sf-edge-halo");
            edgeGroup.appendChild(halo);

            const edgePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
            edgePath.setAttribute("d", pathData);
            edgePath.setAttribute("class", "sf-edge");
            edgePath.setAttribute("marker-end", "url(#arrow)");
            edgeGroup.appendChild(edgePath);

            const titleElement = document.createElementNS("http://www.w3.org/2000/svg", "title");
            titleElement.textContent = `${{nodeById.get(edge.target)?.label || edge.target}} -> ${{nodeById.get(edge.source)?.label || edge.source}}`;
            edgeGroup.appendChild(titleElement);

            svg.appendChild(edgeGroup);
            renderedEdgeGroups.push({{
              key: edgeKey(edge.source, edge.target),
              group: edgeGroup,
              path: edgePath,
            }});
          }});

          visibleNodes.forEach(node => {{
            const position = positions.get(node.id);
            const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
            const children = childrenById.get(node.id) || [];
            const hasHiddenChildren = children.some(childId => !visible.has(childId));
            group.setAttribute("class", `sf-node${{children.length ? " clickable" : ""}}`);
            group.setAttribute("transform", `translate(${{position.x}}, ${{position.y}})`);
            group.addEventListener("mouseenter", () => focusBranch(node.id, visible));
            group.addEventListener("mouseleave", clearBranchFocus);
            if (children.length) {{
              group.addEventListener("click", () => {{
                if (hasHiddenChildren) {{
                  expandNode(node);
                  render();
                }}
              }});
            }}

            const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
            rect.setAttribute("width", nodeWidth);
            rect.setAttribute("height", nodeHeight);
            rect.setAttribute("rx", "8");
            rect.setAttribute("ry", "8");
            rect.setAttribute("fill", nodeColor(node));
            rect.setAttribute("stroke", node.isLeaf ? "#fb7185" : node.isRoot ? "#60a5fa" : "#86efac");
            rect.setAttribute("filter", "url(#cardShadow)");
            group.appendChild(rect);

            const titleElement = document.createElementNS("http://www.w3.org/2000/svg", "title");
            const primaryDetail = Array.isArray(node.details) && node.details.length ? node.details[0] : null;
            const nodeTitle = node.columnName
              ? `${{node.objectName}} | Column: ${{node.columnName}}`
              : String(node.objectName || node.label || "");
            titleElement.textContent = primaryDetail
              ? `${{nodeTitle}}\\nTransformation: ${{detailValue(primaryDetail.transformation)}}\\nModification SQL: ${{detailValue(primaryDetail.modificationSql)}}`
              : nodeTitle;
            group.appendChild(titleElement);

            const lines = splitObjectName(node);
            lines.forEach((lineText, index) => {{
              const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
              text.setAttribute("x", "14");
              text.setAttribute("y", String(20 + index * 14));
              text.textContent = lineText;
              group.appendChild(text);
            }});

            if (node.columnName) {{
              const pillY = 48;
              const pill = document.createElementNS("http://www.w3.org/2000/svg", "rect");
              pill.setAttribute("x", "12");
              pill.setAttribute("y", String(pillY));
              pill.setAttribute("width", String(nodeWidth - 24));
              pill.setAttribute("height", "25");
              pill.setAttribute("class", "sf-column-pill");
              group.appendChild(pill);

              const columnText = document.createElementNS("http://www.w3.org/2000/svg", "text");
              columnText.setAttribute("x", "22");
              columnText.setAttribute("y", String(pillY + 17));
              columnText.setAttribute("class", "sf-column");
              columnText.textContent = "Column: " + truncateText(node.columnName, 34);
              group.appendChild(columnText);
            }}

            if (graph.grain === "COLUMN") {{
              const transformationSummary = document.createElementNS("http://www.w3.org/2000/svg", "text");
              transformationSummary.setAttribute("x", "14");
              transformationSummary.setAttribute("y", "91");
              transformationSummary.setAttribute("class", "sf-transform-summary");
              transformationSummary.textContent = "Transform: " + truncateText(
                primaryDetail ? detailValue(primaryDetail.transformation) : "Not available",
                44
              );
              group.appendChild(transformationSummary);

              const sqlSummary = document.createElementNS("http://www.w3.org/2000/svg", "text");
              sqlSummary.setAttribute("x", "14");
              sqlSummary.setAttribute("y", "106");
              sqlSummary.setAttribute("class", "sf-sql-summary");
              sqlSummary.textContent = "Modification SQL: " + (
                primaryDetail && String(primaryDetail.modificationSql || "").trim()
                  ? "available"
                  : "not available"
              );
              group.appendChild(sqlSummary);
            }}

            const meta = document.createElementNS("http://www.w3.org/2000/svg", "text");
            meta.setAttribute("x", "14");
            meta.setAttribute("y", String(nodeHeight - 12));
            meta.setAttribute("class", "sf-type");
            const nodeExpanded = isExpanded(node);
            meta.textContent = `Level ${{node.level || 0}}${{node.type ? " | " + node.type : ""}}${{node.isLeaf ? " | last node" : nodeExpanded ? " | expanded" : hasHiddenChildren ? " | more" : ""}}`;
            group.appendChild(meta);

            function addBadge(cx, label, badgeClass, labelClass, titleText, onClick) {{
              const badgeGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
              badgeGroup.setAttribute("class", "sf-node-action");
              badgeGroup.style.cursor = "pointer";
              const badgeTitle = document.createElementNS("http://www.w3.org/2000/svg", "title");
              badgeTitle.textContent = titleText;
              badgeGroup.appendChild(badgeTitle);
              const badge = document.createElementNS("http://www.w3.org/2000/svg", "circle");
              badge.setAttribute("cx", String(cx));
              badge.setAttribute("cy", "20");
              badge.setAttribute("r", "11");
              badge.setAttribute("class", badgeClass);
              badgeGroup.appendChild(badge);
              const badgeText = document.createElementNS("http://www.w3.org/2000/svg", "text");
              badgeText.setAttribute("x", String(cx));
              badgeText.setAttribute("y", "24");
              badgeText.setAttribute("text-anchor", "middle");
              badgeText.setAttribute("class", labelClass);
              badgeText.textContent = label;
              badgeGroup.appendChild(badgeText);
              badgeGroup.addEventListener("click", event => {{
                event.stopPropagation();
                onClick();
              }});
              group.appendChild(badgeGroup);
            }}

            if (primaryDetail) {{
              addBadge(
                nodeWidth - 76,
                "i",
                "sf-detail-badge",
                "sf-detail-label",
                "View column transformation and modification SQL",
                () => showNodeDetails(node)
              );
            }}

            if (hasHiddenChildren) {{
              addBadge(
                nodeExpanded ? nodeWidth - 48 : nodeWidth - 22,
                "+",
                "sf-expand-badge",
                "sf-expand-label",
                "Expand next lineage levels",
                () => {{
                  expandNode(node);
                  render();
                }}
              );
            }}
            if (nodeExpanded) {{
              addBadge(
                nodeWidth - 22,
                "-",
                "sf-collapse-badge",
                "sf-collapse-label",
                "Collapse expanded lineage levels",
                () => {{
                  collapseNode(node.id);
                  render();
                }}
              );
            }}
            svg.appendChild(group);
            renderedNodeGroups.set(node.id, group);
          }});
          centerStage();
        }}

        render();
      }})();
    </script>
    """
    components.html(diagram_html, height=820, scrolling=True)


def render_recursive_snowflake_lineage(payload, key_prefix):
    settings = _get_snowflake_lineage_settings()
    if not settings.get("enabled", False):
        st.info("Enable snowflake_lineage in config/app_settings.json to run recursive Snowflake lineage.")
        return

    source_object = payload.get("source_fully_qualified_name")
    source_column = payload.get("source_column")
    lineage_grain = payload.get("lineage_grain") or "OBJECT"
    source_type = (
        payload.get("source_object_type")
        or payload.get("source_type")
        or str(settings.get("default_object_domain") or "VIEW").strip().upper()
    )
    if lineage_grain == "COLUMN":
        if not source_column:
            st.warning("Source column is required for Snowflake column lineage.")
            return
        source_type = "COLUMN"

    if not source_object:
        st.warning("Source fully qualified name is required for Snowflake lineage.")
        return

    result_key = f"{key_prefix}_recursive_snowflake_lineage_result"
    request_key = "|".join([
        str(source_object or ""),
        str(source_column or ""),
        str(source_type or ""),
        str(lineage_grain or ""),
        str(settings.get("direction") or ""),
        str(settings.get("max_depth") or ""),
        str(settings.get("column_lineage_procedure") or ""),
    ])
    cached = st.session_state.get(result_key)
    if cached and cached.get("request_key") == request_key:
        lineage_rows = cached.get("rows", [])
    else:
        try:
            spinner_text = (
                "Fetching Snowflake column lineage and transformation details..."
                if lineage_grain == "COLUMN"
                else "Fetching recursive Snowflake table lineage..."
            )
            with st.spinner(spinner_text):
                if lineage_grain == "COLUMN":
                    lineage_rows = get_snowflake_column_lineage(
                        source_object,
                        source_column,
                        settings,
                    )
                else:
                    lineage_rows = get_recursive_snowflake_lineage(source_object, source_type, settings)
            st.session_state[result_key] = {"request_key": request_key, "rows": lineage_rows}
        except Exception as exc:
            st.error(
                "Could not fetch Snowflake column lineage."
                if lineage_grain == "COLUMN"
                else "Could not fetch recursive Snowflake table lineage."
            )
            st.exception(exc)
            return

    if not lineage_rows:
        st.info("Snowflake returned no additional lineage for the selected object.")
        return

    lineage_df = pd.DataFrame(lineage_rows)
    lineage_df.insert(0, "Workspace_Name", payload.get("workspace_name", ""))
    lineage_df.insert(1, "Report_Name", payload.get("report_name", ""))
    if lineage_grain != "COLUMN":
        lineage_df.insert(2, "Starting_Source_Fully_Qualified_Name", payload.get("source_fully_qualified_name", source_object))
        lineage_df.insert(3, "Starting_Source_Type", source_type)
        if source_column:
            lineage_df.insert(4, "Selected_Source_Column", source_column)

    st.write("#### Snowflake Column Lineage" if lineage_grain == "COLUMN" else "#### Snowflake Table Lineage")
    lineage_level_values = (
        lineage_df["Lineage_Level"]
        if "Lineage_Level" in lineage_df.columns
        else pd.Series(dtype="float64")
    )
    lineage_levels = pd.to_numeric(lineage_level_values, errors="coerce").dropna()
    if not lineage_levels.empty:
        returned_depth = int(lineage_levels.max())
        configured_depth = max(1, int(settings.get("max_depth") or 20))
        st.caption(
            f"Returned lineage depth: {returned_depth} | "
            f"Configured maximum depth: {configured_depth}"
        )
        if returned_depth >= configured_depth:
            st.warning(
                f"The result reached max_depth={configured_depth}. "
                "Increase snowflake_lineage.max_depth to check for deeper lineage."
            )
    lineage_column_config = None
    if lineage_grain == "COLUMN":
        lineage_column_config = {
            "Column_Transformation": st.column_config.TextColumn(
                "Column_Transformation",
                width="large",
                help="Exact expression used to produce this column at the lineage level.",
            ),
            "Modification_SQL": st.column_config.TextColumn(
                "Modification_SQL",
                width="large",
                help="SQL that created or last modified the lineage object.",
            ),
        }
    st.dataframe(
        lineage_df,
        use_container_width=True,
        hide_index=True,
        column_config=lineage_column_config,
    )
    render_csv_download(
        lineage_df,
        "Download Snowflake lineage as CSV",
        "snowflake_column_lineage.csv" if lineage_grain == "COLUMN" else "snowflake_table_lineage.csv",
        f"{key_prefix}_snowflake_lineage_download",
    )
    render_snowflake_lineage_diagram(
        lineage_rows,
        payload,
        lineage_grain,
        source_object,
        source_type,
        key_prefix,
    )


def _measure_detail_label(row):
    measure_name = (
        _handoff_value(row, "Semantic_Measure_Name")
        or _handoff_value(row, "Semantic_Object_Name")
        or "No measure"
    )
    return " | ".join([
        _handoff_value(row, "Workspace_Name") or "No workspace",
        _handoff_value(row, "Report_Name") or "No report",
        measure_name,
    ])


def _measure_detail_group_key(row):
    """Group one measure across its many source-column lineage rows."""
    measure_name = (
        _handoff_value(row, "Semantic_Measure_Name")
        or _handoff_value(row, "Semantic_Object_Name")
        or _handoff_value(row, "Measure Name")
    )
    return (
        _handoff_value(row, "Workspace_Name") or _handoff_value(row, "Workspace"),
        _handoff_value(row, "Report_Name") or _handoff_value(row, "Source Report"),
        measure_name,
        _handoff_value(row, "Semantic_DAX_Expression") or _handoff_value(row, "Target Expression"),
    )


def _build_measure_detail_rows(rows):
    """Return one selector row per measure, carrying all nested source rows."""
    grouped_rows = []
    groups = {}
    source_seen = {}

    for row in rows:
        group_key = _measure_detail_group_key(row)
        if group_key not in groups:
            aggregate_row = dict(row)
            aggregate_row["_measure_source_rows"] = []
            groups[group_key] = aggregate_row
            source_seen[group_key] = set()
            grouped_rows.append(aggregate_row)

        source_key = (
            _handoff_value(row, "Semantic_Tables"),
            _handoff_value(row, "Semantic_Object_Name"),
            _handoff_value(row, "Semantic_Object_Type"),
            _handoff_value(row, "Source_Fully_Qualified_Name"),
            _handoff_value(row, "Source_Object_Type"),
            _handoff_value(row, "Source_Column_Name"),
            _handoff_value(row, "Source_Query"),
        )
        if source_key in source_seen[group_key]:
            continue
        source_seen[group_key].add(source_key)
        groups[group_key]["_measure_source_rows"].append(dict(row))

    return grouped_rows


def _measure_definition_provider_options():
    return [
        ("auto", "Auto (enabled provider order)"),
        ("openai", "OpenAI LLM"),
        ("snowflake_cortex", "Snowflake Cortex"),
    ]


def _measure_definition_provider_label(provider_key):
    labels = dict(_measure_definition_provider_options())
    return labels.get(str(provider_key or "auto"), "Auto (enabled provider order)")


def render_measure_detail_definition_form(display_df, key_prefix):
    """Let the user select one measure row and fetch its detail using Snowflake."""
    if display_df is None or display_df.empty:
        return

    required_columns = ["Workspace_Name", "Report_Name", "Semantic_DAX_Expression"]
    missing_columns = [column for column in required_columns if column not in display_df.columns]
    if missing_columns:
        st.info(f"Measure detail needs these columns: {', '.join(missing_columns)}")
        return

    rows = []
    for row in display_df.to_dict("records"):
        measure_name = (
            _handoff_value(row, "Semantic_Measure_Name")
            or _handoff_value(row, "Semantic_Object_Name")
        )
        if measure_name or _handoff_value(row, "Semantic_DAX_Expression"):
            rows.append(row)

    if not rows:
        st.info("No measure rows are available for detailed definition.")
        return

    unique_rows = _build_measure_detail_rows(rows)

    st.write("#### Measure Detail Definition")
    provider_options = _measure_definition_provider_options()
    default_provider = _get_default_measure_definition_provider()
    default_provider_index = next(
        (index for index, (provider_key, _) in enumerate(provider_options) if provider_key == default_provider),
        0,
    )
    provider_choice = render_searchable_single_select(
        "Definition type",
        options=[provider_key for provider_key, _ in provider_options],
        index=default_provider_index,
        format_func=_measure_definition_provider_label,
        key=f"{key_prefix}_measure_detail_provider",
    )
    selected_indexes = render_searchable_multiselect(
        "Measure detail input",
        options=list(range(len(unique_rows))),
        default=[],
        format_func=lambda index: _measure_detail_label(unique_rows[index]),
        key=f"{key_prefix}_measure_detail_row",
    )
    submitted = st.button("Get detailed measure definitions", key=f"{key_prefix}_measure_detail_submit")

    detail_state_key = f"{key_prefix}_measure_detail_definition_selection"
    cache_name = _measure_definition_cache_name(key_prefix)
    if cache_name not in st.session_state:
        st.session_state[cache_name] = {}

    if submitted and provider_choice:
        if not selected_indexes:
            st.warning("Select at least one measure to generate a detailed definition.")
        else:
            selections = []
            failures = []
            with st.spinner("Generating detailed measure definitions..."):
                for selected_index in selected_indexes:
                    selected_row = unique_rows[selected_index]
                    cache_key = f"{provider_choice}:{_measure_definition_cache_key(selected_row)}"
                    selections.append({
                        "cache_key": cache_key,
                        "label": _measure_detail_label(selected_row),
                        "provider": provider_choice,
                        "provider_label": _measure_definition_provider_label(provider_choice),
                        "row": selected_row,
                    })

                    if cache_key in st.session_state[cache_name]:
                        continue
                    try:
                        st.session_state[cache_name][cache_key] = get_measure_definition(selected_row, provider_choice)
                    except Exception as exc:
                        failures.append(f"{_measure_detail_label(selected_row)}\n{exc}")

            st.session_state[detail_state_key] = selections
            if failures:
                st.error("Could not generate the detailed measure definition for one or more selected measures.")
                st.code("\n\n".join(failures))

    selection_state = st.session_state.get(detail_state_key)
    if isinstance(selection_state, dict):
        selections = [selection_state]
    else:
        selections = list(selection_state or [])

    for selection in selections:
        definition = st.session_state.get(cache_name, {}).get(selection.get("cache_key"))
        if definition:
            definition = _strip_unwanted_measure_definition_sections(definition)
            st.session_state[cache_name][selection.get("cache_key")] = definition
            with st.container(border=True):
                st.markdown(f"**{selection.get('label', 'Selected measure')}**")
                st.caption(f"Definition type: {selection.get('provider_label', 'Auto')}")
                st.markdown(definition)


def render_snowflake_lineage_handoff_form(display_df, key_prefix, include_source_column=False):
    """Select a source object and render recursive Snowflake table lineage below."""
    if display_df is None or display_df.empty:
        return

    if include_source_column:
        display_df = _explode_source_column_rows(display_df)

    required_columns = ["Workspace_Name", "Report_Name", "Source_Object_Type", "Source_Fully_Qualified_Name"]
    if include_source_column:
        required_columns.append("Source_Column_Name")

    missing_columns = [column for column in required_columns if column not in display_df.columns]
    if missing_columns:
        st.info(f"Snowflake lineage handoff needs these columns: {', '.join(missing_columns)}")
        return

    rows = [
        row
        for row in display_df.to_dict("records")
        if _handoff_value(row, "Source_Fully_Qualified_Name")
    ]
    if include_source_column:
        rows = [row for row in rows if _handoff_value(row, "Source_Column_Name")]

    if not rows:
        st.info("No source object values are available for Snowflake lineage handoff.")
        return

    # De-duplicate exact payloads while keeping table order.
    unique_rows = []
    seen = set()
    for row in rows:
        payload_key = tuple(_handoff_value(row, column) for column in required_columns) + (_snowflake_object_domain_from_row(row),)
        if payload_key in seen:
            continue
        seen.add(payload_key)
        unique_rows.append(row)

    selected_indexes = render_searchable_multiselect(
        "Search fully qualified source object",
        options=list(range(len(unique_rows))),
        default=[],
        format_func=lambda index: _snowflake_handoff_search_label(unique_rows[index], include_source_column),
        key=f"{key_prefix}_snowflake_lineage_row",
        help_text="Open the dropdown and type the fully qualified source name to filter lineage inputs.",
    )
    submitted = st.button(
        "Get Snowflake column lineage" if include_source_column else "Get Snowflake table lineage",
        key=f"{key_prefix}_snowflake_lineage_submit",
    )

    if submitted:
        if not selected_indexes:
            st.warning("Select at least one Snowflake lineage input.")
        else:
            payloads = []
            for selected_index in selected_indexes:
                selected_row = unique_rows[selected_index]
                payload = {
                    "workspace_name": _handoff_value(selected_row, "Workspace_Name"),
                    "report_name": _handoff_value(selected_row, "Report_Name"),
                    "source_object_type": _snowflake_object_domain_from_row(selected_row),
                    "source_fully_qualified_name": _handoff_value(selected_row, "Source_Fully_Qualified_Name"),
                    "lineage_grain": "COLUMN" if include_source_column else "OBJECT",
                }
                if include_source_column:
                    payload["source_column"] = _handoff_value(selected_row, "Source_Column_Name")
                payloads.append(payload)

            st.session_state[f"{key_prefix}_snowflake_lineage_payload"] = payloads

    payload_key = f"{key_prefix}_snowflake_lineage_payload"
    payload_state = st.session_state.get(payload_key)
    payloads = payload_state if isinstance(payload_state, list) else ([payload_state] if payload_state else [])
    valid_payloads = []
    for payload in payloads:
        if include_source_column and len(_split_source_column_names(payload.get("source_column"))) > 1:
            continue
        valid_payloads.append(payload)
    if len(valid_payloads) != len(payloads):
        st.session_state[payload_key] = valid_payloads

    for payload_index, payload in enumerate(valid_payloads):
        lineage_key_seed = "|".join([
            payload.get("source_fully_qualified_name", ""),
            payload.get("source_column", ""),
            payload.get("lineage_grain", ""),
        ])
        lineage_key = hashlib.md5(lineage_key_seed.encode("utf-8")).hexdigest()[:10]
        render_recursive_snowflake_lineage(payload, f"{key_prefix}_{payload_index}_{lineage_key}")


def render_source_db_lineage_records(records, empty_message, download_key=None):
    """Render source DB lineage with native SQL visible and raw Power Query M hidden from the UI."""
    if not records:
        st.info(empty_message)
        return []

    requested_columns = [
        "Workspace Name",
        "Workspace",
        "App Name",
        "Source Report",
        "Source Dashboard",
        "Report ID",
        "Dataset ID",
        "Source Workspace Name",
        "Semantic Model Name",
        "Power BI Table Name",
        "Partition Name",
        "Partition Source Type",
        "Source Server",
        "Source Database",
        "Source Schema",
        "Source Name",
        "Source Type",
        "Query",
        "Native Query Columns",
        "Fully Qualified Name",
    ]

    df = _select_existing_columns(pd.DataFrame(records), requested_columns)
    display_df = _standardize_and_prune_display_dataframe(df)
    display_df = _apply_lineage_display_contract(display_df, "source_db_lineage")
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    if download_key:
        st.download_button(
            "Download source DB lineage as CSV",
            data=display_df.to_csv(index=False).encode("utf-8"),
            file_name="source_db_lineage_native_query.csv",
            mime="text/csv",
            key=download_key,
        )
    render_snowflake_lineage_handoff_form(
        display_df,
        f"{download_key or 'source_db_lineage'}_handoff",
        include_source_column=False,
    )

    return display_df.to_dict("records")


def _get_measure_lineage_rows_for_contexts(contexts, headersSPA, xmla_token, cache_prefix):
    rows = []
    for context in contexts:
        dataset_id = context.get("Dataset ID")
        workspace_id = context.get("Target Workspace ID")
        if not dataset_id or not workspace_id:
            continue

        cache_key = f"{cache_prefix}_measure_lineage_v24_{workspace_id}_{dataset_id}"
        if cache_key not in st.session_state:
            st.session_state[cache_key] = get_raw_measure_dependencies(headersSPA, workspace_id, dataset_id, xmla_token, workspace_name_hint=context.get("Workspace") if context.get("Scope Type") == "Workspace" else None, dataset_name_hint=context.get("Semantic Model Name"), auth_headers=[("MasterUser", headersSPA)])

        measure_lineage_df = st.session_state.get(cache_key)
        if measure_lineage_df is None or getattr(measure_lineage_df, "empty", True):
            continue

        source_lookup = _source_lineage_map(_get_source_lineage_for_context(context, headersSPA, xmla_token, cache_prefix))
        for row in measure_lineage_df.to_dict("records"):
            semantic_table = _prefer_non_na(row.get("Dependency Table/View"), row.get("Source Table"), row.get("Target Table/View"))
            semantic_column = _prefer_non_na(row.get("Dependency Object Name"), row.get("Source Column Name"), row.get("Target Object Name"))
            semantic_object_type = _prefer_non_na(row.get("Dependency Object Type"), row.get("Source Column Type"), "COLUMN")
            source_details = _enrich_with_source_details(row, semantic_table, semantic_column, source_lookup)
            lineage_record = {
                "Scope Type": context.get("Scope Type"),
                "Workspace": context.get("Workspace") if context.get("Scope Type") == "Workspace" else "N/A",
                "App Name": context.get("App Name") if context.get("Scope Type") == "App" else "N/A",
                "Source Report": context.get("Source Report"),
                "Report ID": context.get("Report ID"),
                "Dataset ID": dataset_id,
                "Semantic Workspace Name": source_details.get("Semantic Workspace Name", "N/A"),
                "Semantic Model Name": source_details.get("Semantic Model Name", "N/A"),
                "Target Object Type": row.get("Target Object Type", row.get("Semantic Object Type", "N/A")),
                "Target Table/View": row.get("Target Table/View", "N/A"),
                "Target Object Name": row.get("Target Object Name", row.get("Measure Name", "N/A")),
                "Target Expression": row.get("Target Expression", "N/A"),
                "Measure Name": row.get("Measure Name", "N/A"),
                "Semantic Table/View": semantic_table,
                "Semantic Object Name": semantic_column,
                "Semantic Object Type": semantic_object_type,
                "Dependency Expression": row.get("Dependency Expression", "N/A"),
                "Query": source_details.get("Query", "N/A"),
                "Exact Source Database": source_details.get("Exact Source Database", "N/A"),
                "Exact Source Schema": source_details.get("Exact Source Schema", "N/A"),
                "Exact Source Table/View": source_details.get("Exact Source Table/View", "N/A"),
                "Exact Source Object Type": source_details.get("Exact Source Object Type", "N/A"),
                "Exact Source Column Name": source_details.get("Exact Source Column Name", "N/A"),
                "Fully Qualified Source Object": source_details.get("Fully Qualified Source Object", "N/A"),
            }
            rows.append(lineage_record)
    return rows


def _layout_session_key(scope_key, report_id):
    return f"{scope_key}_uploaded_layout_records_{report_id}"


def get_uploaded_layout_records(scope_key, report_id):
    return st.session_state.get(_layout_session_key(scope_key, report_id), []) or []


def get_uploaded_layout_records_for_contexts(scope_key, contexts):
    records = []
    for context in contexts or []:
        records.extend(get_uploaded_layout_records(scope_key, context.get("Report ID")))
    return records


def has_uploaded_layout_records_for_report_ids(scope_key, report_ids):
    for report_id in report_ids or []:
        if report_id and get_uploaded_layout_records(scope_key, report_id):
            return True
    return False


def render_semantic_model_objects_view(contexts, headersSPA, headersSP, xmla_token, cache_prefix, download_key):
    if not contexts:
        st.info("Select at least one report first.")
        return []

    rows = _get_semantic_objects_for_contexts(contexts, headersSPA, headersSP, xmla_token, cache_prefix)
    if not rows:
        st.info("No semantic model columns/measures found. Check XMLA permissions or dataset access.")
        return []

    requested_columns = [
        "Scope Type",
        "Workspace",
        "App Name",
        "Source Report",
        "Report ID",
        "Dataset ID",
        "Semantic Workspace Name",
        "Semantic Model Name",
        "Semantic Table/View",
        "Object Type",
        "Semantic Object Name",
        "Data Type",
        "Source Column Name From Model",
        "DAX Expression",
    ]

    df = _select_existing_columns(pd.DataFrame(rows), requested_columns)
    display_df = _standardize_and_prune_display_dataframe(df)
    display_df = _apply_lineage_display_contract(display_df, "semantic_model_objects")
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download semantic model objects as CSV",
        data=display_df.to_csv(index=False).encode("utf-8"),
        file_name="semantic_model_columns_and_measures.csv",
        mime="text/csv",
        key=download_key,
    )
    return display_df.to_dict("records")


def render_measure_source_lineage_view(contexts, headersSPA, xmla_token, cache_prefix, download_key):
    if not contexts:
        st.info("Select at least one report first.")
        return []

    rows = _get_measure_lineage_rows_for_contexts(contexts, headersSPA, xmla_token, cache_prefix)
    if not rows:
        st.info("No measure-to-source-column lineage found. Measures may not exist, XMLA may be blocked, or lineage DMV may not expose dependencies.")
        return []

    requested_columns = [
        "Scope Type",
        "Workspace",
        "App Name",
        "Source Report",
        "Report ID",
        "Dataset ID",
        "Semantic Workspace Name",
        "Semantic Model Name",
        "Target Object Type",
        "Target Table/View",
        "Target Object Name",
        "Target Expression",
        "Measure Name",
        "Semantic Table/View",
        "Semantic Object Name",
        "Semantic Object Type",
        "Dependency Expression",
        "Query",
        "Exact Source Database",
        "Exact Source Schema",
        "Exact Source Table/View",
        "Exact Source Object Type",
        "Exact Source Column Name",
        "Fully Qualified Source Object",
    ]

    df = _select_existing_columns(pd.DataFrame(rows), requested_columns)
    display_df = _standardize_and_prune_display_dataframe(df)
    display_df = _apply_lineage_display_contract(display_df, "measure_source_lineage")
    display_df = _explode_source_column_rows(display_df)
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.download_button(
        "Download measure source lineage as CSV",
        data=display_df.to_csv(index=False).encode("utf-8"),
        file_name="measure_source_lineage_tables_columns.csv",
        mime="text/csv",
        key=download_key,
    )
    render_measure_detail_definition_form(
        display_df,
        f"{cache_prefix}_measure_source_lineage",
    )
    render_snowflake_lineage_handoff_form(
        display_df,
        f"{cache_prefix}_measure_source_lineage",
        include_source_column=True,
    )
    return display_df.to_dict("records")


def _safe_widget_key(value):
    """Create a stable Streamlit widget key fragment from an arbitrary file/report name."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "value"))[:120]


def _infer_report_context_index_from_filename(file_name, contexts):
    """Best-effort mapping of an uploaded PBIX/PBIR file to one selected report context."""
    normalized_file = _normalise_name_for_join(file_name)
    if not normalized_file:
        return 0

    for idx, context in enumerate(contexts):
        report_id = _normalise_name_for_join(context.get("Report ID"))
        if report_id and report_id in normalized_file:
            return idx

    best_idx = 0
    best_score = 0
    for idx, context in enumerate(contexts):
        report_name = str(context.get("Source Report") or "")
        normalized_report = _normalise_name_for_join(report_name)
        if normalized_report and normalized_report in normalized_file:
            score = len(normalized_report)
            if score > best_score:
                best_score = score
                best_idx = idx
    return best_idx


def _normalize_layout_records_for_context(records, context, report_id):
    return [
        {
            "Scope Type": context.get("Scope Type"),
            "Container Name": context.get("Container Name"),
            "Workspace": context.get("Workspace"),
            "App Name": context.get("App Name"),
            "Source Report": context.get("Source Report"),
            "Report ID": report_id,
            "Dataset ID": context.get("Dataset ID"),
            **record,
        }
        for record in records or []
        if isinstance(record, dict)
    ]


def _automatic_layout_attempt_key(scope_key, context, source_name):
    return (
        f"{scope_key}_automatic_layout_v2_{source_name}_"
        f"{context.get('Target Workspace ID')}_{context.get('Report ID')}"
    )


def render_upload_only_report_layout_view(
    contexts,
    scope_key,
    download_key,
    powerbi_headers=None,
    fabric_headers=None,
):
    """Retrieve report definitions automatically and retain manual upload as a fallback."""
    if not contexts:
        st.info("Select one or more reports to retrieve their layout metadata.")
        return []

    needs_automatic_retrieval = any(
        not get_uploaded_layout_records(scope_key, context.get("Report ID"))
        for context in contexts
    )
    if not fabric_headers and needs_automatic_retrieval:
        fabric_headers = render_fabric_definition_authorization(f"{scope_key}_report_layout")
    source_name = "fabric" if fabric_headers else "powerbi_export"
    automatic_states = []

    for context in contexts:
        report_id = context.get("Report ID")
        workspace_id = context.get("Target Workspace ID")
        existing_records = get_uploaded_layout_records(scope_key, report_id)
        attempt_key = _automatic_layout_attempt_key(scope_key, context, source_name)

        if existing_records:
            automatic_states.append({"status": "cached", "context": context, "count": len(existing_records)})
            continue
        if not report_id or not workspace_id:
            automatic_states.append({
                "status": "failed",
                "context": context,
                "error": "Workspace ID or report ID could not be resolved.",
            })
            continue
        if str(context.get("Report Type") or "").lower() == "paginatedreport":
            automatic_states.append({
                "status": "skipped",
                "context": context,
                "error": "Paginated reports do not use the Power BI report definition parser.",
            })
            continue
        if not fabric_headers:
            automatic_states.append({"status": "authorization_required", "context": context})
            continue

        if attempt_key not in st.session_state:
            with st.spinner(f"Retrieving report definition for {context.get('Source Report') or report_id}..."):
                parsed_records = get_report_visual_usage(
                    powerbi_headers,
                    workspace_id,
                    report_id,
                    fabric_headers=fabric_headers,
                    report_format=context.get("Report Format"),
                )
            usable_records = [
                record for record in parsed_records or []
                if isinstance(record, dict)
                and str(record.get("Visual ID") or "").strip().lower() not in {"", "n/a", "none"}
            ]
            if usable_records and not _visual_usage_block_reason(usable_records):
                normalized_records = _normalize_layout_records_for_context(usable_records, context, report_id)
                st.session_state[_layout_session_key(scope_key, report_id)] = normalized_records
                st.session_state[attempt_key] = {
                    "status": "success",
                    "context": context,
                    "count": len(normalized_records),
                }
            else:
                error_detail = "; ".join(
                    str(record.get("Error Detail") or record.get("Status") or "")
                    for record in parsed_records or []
                    if isinstance(record, dict)
                )
                st.session_state[attempt_key] = {
                    "status": "failed",
                    "context": context,
                    "error": error_detail or "No visual metadata was returned.",
                }
        automatic_states.append(st.session_state.get(attempt_key) or {})

    success_states = [state for state in automatic_states if state.get("status") in {"success", "cached"}]
    failed_states = [state for state in automatic_states if state.get("status") == "failed"]
    if success_states:
        report_count = len(success_states)
        record_count = sum(int(state.get("count") or 0) for state in success_states)
        st.success(f"Report layout metadata is available for {report_count} report(s), with {record_count} visual-field rows.")
    for state in failed_states:
        context = state.get("context") or {}
        st.warning(f"{context.get('Source Report') or context.get('Report ID')}: automatic layout retrieval failed.")
        with st.expander(f"Automatic retrieval details - {context.get('Source Report') or 'report'}", expanded=False):
            st.code(str(state.get("error") or "No error detail was returned."))

    if failed_states:
        if st.button("Retry automatic layout retrieval", key=f"{scope_key}_retry_automatic_layout"):
            for context in contexts:
                for candidate_source in ("fabric", "powerbi_export"):
                    st.session_state.pop(_automatic_layout_attempt_key(scope_key, context, candidate_source), None)
            st.rerun()

    report_labels = [ctx.get("Context Key") for ctx in contexts]
    with st.expander("Manual report layout fallback", expanded=not success_states):
        uploaded_files = st.file_uploader(
            "Upload one or more PBIX / PBIP ZIP / PBIR definition ZIP / Report Layout JSON files",
            type=["pbix", "zip", "json", "pbir", "pbip"],
            accept_multiple_files=True,
            key=f"{scope_key}_layout_multi_file_uploader",
        )

        if uploaded_files:
            for file_index, uploaded_file in enumerate(uploaded_files):
                file_name = getattr(uploaded_file, "name", f"uploaded_report_{file_index + 1}")
                default_index = _infer_report_context_index_from_filename(file_name, contexts)
                selected_label = render_searchable_single_select(
                    f"Map uploaded file to report: {file_name}",
                    options=report_labels,
                    index=default_index if 0 <= default_index < len(report_labels) else 0,
                    key=f"{scope_key}_layout_upload_map_{file_index}_{_safe_widget_key(file_name)}",
                )
                selected_context = next(
                    (ctx for ctx in contexts if ctx.get("Context Key") == selected_label),
                    contexts[0],
                )
                report_id = selected_context.get("Report ID") or f"Manual Upload {file_index + 1}"
                parsed_records = parse_uploaded_report_layout(uploaded_file, report_id=report_id)
                st.session_state[_layout_session_key(scope_key, report_id)] = _normalize_layout_records_for_context(
                    parsed_records,
                    selected_context,
                    report_id,
                )

    records = get_uploaded_layout_records_for_contexts(scope_key, contexts)
    if not records:
        st.warning("No report visual metadata is available. Complete Fabric authorization or use the manual fallback.")
        return []

    st.caption(f"Showing layout records for {len({row.get('Report ID') for row in records})} report(s).")
    render_visual_usage_records(records, "No layout/visual fields found in selected reports.", download_key)
    return records


def _visual_query_reference_parts(query_reference):
    """Return useful semantic candidates from a Power BI visual query reference."""
    raw = _prefer_non_na(query_reference)
    if raw == "N/A":
        return []

    candidates = []

    def add(value):
        text = str(value or "").strip().strip("'\"")
        text = text.replace("[", "").replace("]", "")
        if not text or text.lower() in {"n/a", "na", "none", "null", "nan"}:
            return
        marker = _normalise_name_for_join(text)
        if marker and marker not in {_normalise_name_for_join(item) for item in candidates}:
            candidates.append(text)

    add(raw)
    current = raw.strip()
    function_match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*\((.*)\)$", current)
    if function_match:
        current = function_match.group(1).strip()
        add(current)

    if "." in current:
        _, object_part = current.rsplit(".", 1)
        add(object_part)

    return candidates


def _visual_semantic_name_candidates(visual):
    """Candidate semantic object names when visual field display and model names differ."""
    candidates = []
    seen = set()

    def add(value):
        for candidate in _visual_query_reference_parts(value) or [value]:
            marker = _normalise_name_for_join(candidate)
            if marker and marker not in seen:
                seen.add(marker)
                candidates.append(candidate)

    add(visual.get("Column / Measure Name"))
    add(visual.get("Query Reference"))
    return candidates


def _visual_semantic_table_candidates(visual):
    candidates = []
    seen = set()

    def add(value):
        text = _prefer_non_na(value)
        if text == "N/A":
            return
        marker = _normalise_name_for_join(text)
        if marker and marker not in seen:
            seen.add(marker)
            candidates.append(text)

    add(visual.get("Table Name"))
    query_ref = _prefer_non_na(visual.get("Query Reference"))
    if query_ref != "N/A":
        function_match = re.match(r"^[A-Za-z_][A-Za-z0-9_]*\((.*)\)$", query_ref)
        current = function_match.group(1).strip() if function_match else query_ref
        if "." in current:
            add(current.rsplit(".", 1)[0].strip("'\""))

    return candidates


def _semantic_lookup_keys(dataset_id, semantic_table, semantic_object, object_type):
    return (
        dataset_id,
        _normalise_name_for_join(semantic_table),
        _normalise_name_for_join(semantic_object),
        _semantic_dependency_type(object_type),
    )


def render_visual_source_lookup_view(contexts, headersSPA, headersSP, xmla_token, scope_key, cache_prefix, download_key):
    """Join retrieved report visual fields to semantic columns/measures and source table/view lineage."""
    if not contexts:
        st.info("Select at least one report first.")
        return []

    layout_records = get_uploaded_layout_records_for_contexts(scope_key, contexts)

    if not layout_records:
        st.warning("Retrieve or upload a report definition in the 'Report Layout' tab first.")
        return []

    semantic_rows = _get_semantic_objects_for_contexts(contexts, headersSPA, headersSP, xmla_token, cache_prefix)
    semantic_lookup = {}
    semantic_lookup_by_name = {}
    for row in semantic_rows:
        dataset_id = row.get("Dataset ID")
        object_type = _semantic_dependency_type(row.get("Object Type"))
        key = _semantic_lookup_keys(
            dataset_id,
            row.get("Semantic Table/View"),
            row.get("Semantic Object Name"),
            object_type,
        )
        semantic_lookup[key] = row
        for candidate_name in [
            row.get("Semantic Object Name"),
            row.get("Measure Name"),
            row.get("Source Column Name From Model"),
        ]:
            candidate_key = _normalise_name_for_join(candidate_name)
            if candidate_key:
                semantic_lookup_by_name.setdefault((dataset_id, candidate_key, object_type), row)
                semantic_lookup_by_name.setdefault((dataset_id, candidate_key, "ANY"), row)

    source_lookup_by_dataset = {}
    dependency_lineage_by_dataset_object = {}
    for context in contexts:
        dataset_id = context.get("Dataset ID")
        if not dataset_id:
            continue
        source_lookup_by_dataset[dataset_id] = _source_lineage_map(_get_source_lineage_for_context(context, headersSPA, xmla_token, cache_prefix, auth_headers=[("MasterUser", headersSP)]))
        for line in _get_measure_lineage_rows_for_contexts([context], headersSPA, xmla_token, cache_prefix):
            # Key by both measure/calc-column object name and the visible semantic object name.
            # This allows Visual Source Lookup to resolve visuals that use calculated columns,
            # not only visuals that use measures.
            for candidate_name in [line.get("Target Object Name"), line.get("Measure Name"), line.get("Semantic Object Name")]:
                if _is_meaningful_value(candidate_name):
                    dependency_key = (dataset_id, _normalise_name_for_join(candidate_name))
                    dependency_lineage_by_dataset_object.setdefault(dependency_key, []).append(line)

    lookup_rows = []
    for visual in layout_records:
        dataset_id = visual.get("Dataset ID")
        semantic_table = visual.get("Table Name")
        semantic_object = visual.get("Column / Measure Name")
        field_type = str(visual.get("Field Type") or "")
        object_type = "MEASURE" if "measure" in field_type.lower() else "COLUMN"
        name_candidates = _visual_semantic_name_candidates(visual) or [semantic_object]
        table_candidates = _visual_semantic_table_candidates(visual) or [semantic_table]

        common = {
            "Report_Name": visual.get("Source Report") or visual.get("Report"),
            "Page_Name": visual.get("Page Name"),
            "Visual_Name": visual.get("Visual Name"),
            "Visualization_Type": visual.get("Visualization Type"),
            "Field_Role": visual.get("Field Role"),
            "Field Type": visual.get("Field Type"),
            "Table Name": visual.get("Table Name"),
            "Column / Measure Name": visual.get("Column / Measure Name"),
            "Visual_Field_Name": visual.get("Column / Measure Name"),
            "Visual_Table_Name": visual.get("Table Name"),
            "Aggregation": visual.get("Aggregation"),
            "Query Reference": visual.get("Query Reference"),
            "Visual_Query_Reference": visual.get("Query Reference"),
            "Visual ID": visual.get("Visual ID"),
            "Visual X": visual.get("Visual X"),
            "Visual Y": visual.get("Visual Y"),
            "Visual Width": visual.get("Visual Width"),
            "Visual Height": visual.get("Visual Height"),
            "Semantic_Tables": semantic_table,
            "Semantic_Object_Name": semantic_object,
            "Semantic_Object_Type": object_type,
            "Source_Query": visual.get("Query Reference", "N/A"),
        }

        dependency_lines = []
        dependency_match_name = ""
        for candidate_name in name_candidates:
            dependency_lines = dependency_lineage_by_dataset_object.get((dataset_id, _normalise_name_for_join(candidate_name)), [])
            if dependency_lines:
                dependency_match_name = candidate_name
                break

        if dependency_lines:
            for line in dependency_lines:
                matched_table = _prefer_non_na(line.get("Target Table/View"), line.get("Semantic Table/View"), semantic_table)
                matched_object = _prefer_non_na(line.get("Target Object Name"), line.get("Measure Name"), line.get("Semantic Object Name"), semantic_object)
                lookup_rows.append({
                    **common,
                    "Semantic_Tables": matched_table,
                    "Semantic_Object_Name": matched_object,
                    "Semantic_Object_Type": _prefer_non_na(line.get("Target Object Type"), common.get("Semantic_Object_Type")),
                    "Matched_Semantic_Table": matched_table,
                    "Matched_Semantic_Object_Name": matched_object,
                    "Match_Method": f"Measure lineage matched on '{dependency_match_name}'",
                    "Source_Query": _prefer_non_na(line.get("Query"), common.get("Source_Query")),
                    "Source_Object_Type": line.get("Exact Source Object Type", "N/A"),
                    "Source_Column_Name": line.get("Exact Source Column Name", "N/A"),
                    "Source_Fully_Qualified_Name": line.get("Fully Qualified Source Object", "N/A"),
                })
            continue

        semantic_row = {}
        matched_table = semantic_table
        matched_object = semantic_object
        matched_type = object_type
        match_method = "No semantic object match"
        type_candidates = [object_type, "MEASURE", "CALC_COLUMN", "COLUMN"]

        for table_candidate in table_candidates:
            if semantic_row:
                break
            for name_candidate in name_candidates:
                for type_candidate in type_candidates:
                    semantic_row = semantic_lookup.get(
                        _semantic_lookup_keys(dataset_id, table_candidate, name_candidate, type_candidate)
                    )
                    if semantic_row:
                        matched_table = _prefer_non_na(semantic_row.get("Semantic Table/View"), table_candidate)
                        matched_object = _prefer_non_na(semantic_row.get("Semantic Object Name"), name_candidate)
                        matched_type = _semantic_dependency_type(semantic_row.get("Object Type"))
                        match_method = f"Semantic object matched on table/name '{table_candidate}.{name_candidate}'"
                        break
                if semantic_row:
                    break

        if not semantic_row:
            for name_candidate in name_candidates:
                for type_candidate in type_candidates:
                    semantic_row = (
                        semantic_lookup_by_name.get((dataset_id, _normalise_name_for_join(name_candidate), type_candidate))
                        or semantic_lookup_by_name.get((dataset_id, _normalise_name_for_join(name_candidate), "ANY"))
                    )
                    if semantic_row:
                        matched_table = _prefer_non_na(semantic_row.get("Semantic Table/View"), semantic_table)
                        matched_object = _prefer_non_na(semantic_row.get("Semantic Object Name"), name_candidate)
                        matched_type = _semantic_dependency_type(semantic_row.get("Object Type"))
                        match_method = f"Semantic object matched on name '{name_candidate}'"
                        break
                if semantic_row:
                    break

        source_details = _enrich_with_source_details(
            semantic_row,
            matched_table,
            matched_object,
            source_lookup_by_dataset.get(dataset_id, {}),
        )
        lookup_rows.append({
            **common,
            "Semantic_Tables": matched_table,
            "Semantic_Object_Name": matched_object,
            "Semantic_Object_Type": matched_type,
            "Matched_Semantic_Table": matched_table,
            "Matched_Semantic_Object_Name": matched_object,
            "Match_Method": match_method,
            "Source_Query": _prefer_non_na(source_details.get("Query"), common.get("Source_Query")),
            "Source_Object_Type": source_details.get("Exact Source Object Type", "N/A"),
            "Source_Column_Name": source_details.get("Exact Source Column Name", "N/A"),
            "Source_Fully_Qualified_Name": source_details.get("Fully Qualified Source Object", "N/A"),
        })

    if not lookup_rows:
        st.info("No lookup rows could be created from the retrieved report layout.")
        return []

    requested_columns = [
        "Report_Name",
        "Page_Name",
        "Visual_Name",
        "Visualization_Type",
        "Field_Role",
        "Field Type",
        "Table Name",
        "Column / Measure Name",
        "Visual_Field_Name",
        "Visual_Table_Name",
        "Aggregation",
        "Query Reference",
        "Visual_Query_Reference",
        "Visual ID",
        "Visual X",
        "Visual Y",
        "Visual Width",
        "Visual Height",
        "Semantic_Tables",
        "Semantic_Object_Name",
        "Semantic_Object_Type",
        "Matched_Semantic_Table",
        "Matched_Semantic_Object_Name",
        "Match_Method",
        "Source_Query",
        "Source_Object_Type",
        "Source_Column_Name",
        "Source_Fully_Qualified_Name",
    ]

    df = _select_existing_columns(pd.DataFrame(lookup_rows), requested_columns)
    display_df = _clean_dataframe_for_display(df)
    st.dataframe(display_df, use_container_width=True, hide_index=True)
    st.download_button(
        "⬇️ Download visual-to-source lookup as CSV",
        data=display_df.to_csv(index=False).encode("utf-8"),
        file_name="visual_measure_datasource_lookup.csv",
        mime="text/csv",
        key=download_key,
    )
    return display_df.to_dict("records")


# --- STREAMLIT APP CONFIGURATION ---
st.set_page_config(page_title="PBI Lineage Explorer", layout="wide")

st.markdown(
    """
    <style>
        .stApp {
            background: #f4f7fb;
            color: #172033;
        }
        header[data-testid="stHeader"] {
            display: none;
        }
        div[data-testid="stHorizontalBlock"]:has(.app-top-strip) {
            position: sticky;
            top: 0;
            z-index: 990;
            align-items: center;
            height: 76px;
            box-sizing: border-box;
            overflow: hidden;
            gap: 0.65rem;
            padding: 0.7rem 1rem;
            margin-bottom: 1rem;
            border: 0;
            border-top: 3px solid #2563eb;
            border-bottom: 1px solid #dbe3ef;
            background: rgba(255, 255, 255, 0.98);
            box-shadow: 0 4px 12px rgba(30, 64, 175, 0.05);
        }
        div[data-testid="stHorizontalBlock"]:has(.app-top-strip) > div[data-testid="stColumn"] {
            display: flex;
            align-items: center;
            height: 100%;
            min-width: 0;
            overflow: hidden;
        }
        div[data-testid="stHorizontalBlock"]:has(.app-top-strip) > div[data-testid="stColumn"] > div {
            width: 100%;
        }
        div[data-testid="stHorizontalBlock"]:has(.app-top-strip) [data-testid="stMarkdownContainer"] {
            display: flex;
            align-items: center;
            min-height: 46px;
            margin: 0;
            overflow: hidden;
        }
        div[data-testid="stHorizontalBlock"]:has(.app-top-strip) div.stButton > button {
            min-height: 42px;
            padding: 0.35rem 0.65rem;
            white-space: nowrap;
        }
        .app-top-strip {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            min-width: 0;
            height: 46px;
            overflow: hidden;
        }
        .app-brand-mark {
            width: 44px;
            height: 40px;
            display: grid;
            place-items: center;
            flex: 0 0 auto;
            border-radius: 6px;
            background: #2563eb;
            color: #ffffff;
            font-size: 0.72rem;
            font-weight: 800;
            letter-spacing: 0;
            box-shadow: inset -6px -6px 0 #1d4ed8;
        }
        .app-brand-copy {
            min-width: 0;
            display: flex;
            align-items: center;
            overflow: hidden;
        }
        .app-top-title {
            font-size: 1.28rem;
            line-height: 1;
            font-weight: 800;
            color: #0f172a;
            margin: 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .app-top-caption {
            display: none;
        }
        .app-status-panel {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0.4rem;
            min-height: 38px;
        }
        .app-status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #16a34a;
            box-shadow: 0 0 0 3px #dcfce7;
        }
        .app-status-text {
            color: #166534;
            font-weight: 700;
            font-size: 0.76rem;
        }
        section[data-testid="stSidebar"],
        button[data-testid="stSidebarCollapsedControl"] {
            display: none;
        }
        .main .block-container {
            max-width: 1540px;
            padding-top: 0.45rem;
            padding-bottom: 3rem;
            padding-left: 1.6rem;
            padding-right: 1.6rem;
        }
        div[data-testid="stTabs"] button[role="tab"] {
            border-radius: 0;
            font-weight: 600;
            min-height: 44px;
            padding-left: 0.85rem;
            padding-right: 0.85rem;
            color: #475569;
        }
        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
            color: #1d4ed8;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            overflow: hidden;
            background: #ffffff;
        }
        div.stButton > button,
        div[data-testid="stDownloadButton"] > button {
            min-height: 38px;
            border-radius: 6px;
            border-color: #cbd5e1;
            background: #ffffff;
            color: #1e293b;
            font-weight: 650;
            transition: border-color 120ms ease, background 120ms ease, color 120ms ease;
        }
        div.stButton > button:hover,
        div[data-testid="stDownloadButton"] > button:hover {
            border-color: #2563eb;
            color: #1d4ed8;
            background: #f8fbff;
        }
        div.stButton > button[kind="primary"] {
            border-color: #2563eb;
            background: #2563eb;
            color: #ffffff;
        }
        div.stButton > button[kind="primary"]:hover {
            border-color: #1d4ed8;
            background: #1d4ed8;
            color: #ffffff;
        }
        div[data-baseweb="select"] > div,
        div[data-baseweb="input"] > div {
            border-radius: 6px;
            border-color: #d7e0ec;
            background: #ffffff;
        }
        .page-header {
            border-bottom: 1px solid #dbe3ef;
            padding: 0.8rem 0 1rem 0;
            margin: 0 0 1rem 0;
        }
        .page-eyebrow {
            color: #2563eb;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0;
            margin-bottom: 0.25rem;
        }
        .page-header h1,
        .page-header h2 {
            color: #0f172a;
            font-size: 1.7rem;
            line-height: 1.2;
            margin: 0 0 0.35rem 0;
        }
        .page-header p {
            color: #64748b;
            font-size: 0.96rem;
            margin: 0;
            max-width: 920px;
        }
        .page-header.centered {
            text-align: center;
        }
        .page-header.centered p {
            margin-left: auto;
            margin-right: auto;
        }
        .login-shell {
            max-width: 1040px;
            margin: 3.5vh auto 0 auto;
            padding: 1.35rem 1.5rem 1.5rem 1.5rem;
            border: 1px solid #dbe3ef;
            border-radius: 8px;
            background: #ffffff;
            box-shadow: none;
        }
        .login-title {
            color: #0f172a;
            font-size: 2rem;
            line-height: 1.15;
            font-weight: 750;
            margin: 0 0 0.45rem 0;
        }
        .login-copy {
            color: #475569;
            font-size: 1rem;
            max-width: 700px;
            margin-bottom: 1.5rem;
        }
        .login-steps {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 1rem;
            margin: 1.4rem 0 0 0;
        }
        .auth-step-table {
            display: grid;
            grid-template-columns: 1fr;
            border: 1px solid #dbe3ef;
            border-radius: 8px;
            background: #ffffff;
            overflow: hidden;
            margin-top: 1.1rem;
        }
        .auth-step-cell {
            padding: 1rem 1.05rem;
            border-right: 0;
            border-bottom: 1px solid #e2e8f0;
            min-height: 78px;
            text-align: center;
        }
        .auth-step-cell:last-child {
            border-bottom: 0;
        }
        .auth-step-cell strong {
            display: block;
            color: #0f172a;
            font-size: 0.94rem;
            margin-bottom: 0.35rem;
        }
        .auth-step-cell span {
            color: #64748b;
            font-size: 0.86rem;
            line-height: 1.45;
        }
        .auth-action-spacer {
            height: 1.15rem;
        }
        .home-hero {
            padding: 1.35rem 1rem 1.15rem;
            margin: 0 0 0.2rem 0;
            text-align: center;
            border-bottom: 1px solid #dbe3ef;
            background: #eef5ff;
        }
        .home-hero h1 {
            color: #102a56;
            font-size: 1.85rem;
            line-height: 1.2;
            margin: 0.1rem 0 0.4rem;
        }
        .home-hero p {
            max-width: 820px;
            margin: 0 auto;
            color: #526581;
            font-size: 0.92rem;
        }
        .inventory-metric-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.9rem;
            margin: 1.1rem 0 1.25rem;
        }
        .inventory-metric {
            position: relative;
            min-height: 108px;
            padding: 0.85rem 1rem 0.85rem 1.15rem;
            border: 1px solid #dbe3ef;
            border-radius: 7px;
            background: #ffffff;
            overflow: hidden;
        }
        .inventory-metric::before {
            content: "";
            position: absolute;
            inset: 0 auto 0 0;
            width: 5px;
            background: #2563eb;
        }
        .inventory-metric.accent-green::before {
            background: #059669;
        }
        .inventory-metric.accent-coral::before {
            background: #ea580c;
        }
        .inventory-metric span,
        .inventory-metric small {
            display: block;
        }
        .inventory-metric span {
            color: #526581;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
        }
        .inventory-metric strong {
            display: block;
            color: #0f172a;
            font-size: 1.65rem;
            line-height: 1.1;
            margin: 0.32rem 0 0.2rem;
        }
        .inventory-metric small {
            color: #64748b;
            font-size: 0.78rem;
        }
        .section-heading {
            display: flex;
            flex-direction: column;
            gap: 0.1rem;
            margin: 0.35rem 0 0.55rem;
        }
        .section-heading strong {
            color: #102a56;
            font-size: 1.05rem;
        }
        .section-heading span {
            color: #64748b;
            font-size: 0.8rem;
        }
        .report-section-heading {
            margin-top: 1.25rem;
            padding-top: 1rem;
            border-top: 1px solid #dbe3ef;
        }
        .quick-action-copy {
            min-height: 116px;
        }
        .quick-action-number {
            display: inline-grid;
            place-items: center;
            width: 32px;
            height: 32px;
            margin-bottom: 0.7rem;
            border: 1px solid #bfdbfe;
            border-radius: 6px;
            background: #eff6ff;
            color: #1d4ed8;
            font-size: 0.75rem;
            font-weight: 800;
        }
        .quick-action-copy strong {
            display: block;
            color: #172033;
            font-size: 0.96rem;
            margin-bottom: 0.35rem;
        }
        .quick-action-copy p {
            color: #64748b;
            font-size: 0.8rem;
            line-height: 1.4;
            margin: 0;
        }
        .report-row-card {
            display: grid;
            grid-template-columns: 38px minmax(0, 1fr) auto;
            align-items: center;
            gap: 0.7rem;
            min-height: 56px;
            margin-bottom: 0.6rem;
        }
        .report-row-icon {
            width: 36px;
            height: 36px;
            display: grid;
            place-items: center;
            border-radius: 6px;
            background: #e8f1ff;
            color: #1d4ed8;
            font-weight: 800;
        }
        .report-row-copy {
            min-width: 0;
        }
        .report-row-copy strong,
        .report-row-copy span {
            display: block;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .report-row-copy strong {
            color: #172033;
            font-size: 0.9rem;
        }
        .report-row-copy span {
            color: #64748b;
            font-size: 0.75rem;
            margin-top: 0.15rem;
        }
        .report-row-badge {
            padding: 0.18rem 0.45rem;
            border: 1px solid #c7d2fe;
            border-radius: 999px;
            background: #eef2ff;
            color: #3730a3;
            font-size: 0.68rem;
            font-weight: 700;
        }
        .guided-selection-title {
            color: #0f172a;
            font-size: 1.05rem;
            font-weight: 700;
            margin-bottom: 0.2rem;
        }
        .guided-selection-help {
            color: #64748b;
            font-size: 0.86rem;
            margin-bottom: 0.85rem;
        }
        .selected-context-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 0.85rem;
            margin: 1rem 0 1.25rem 0;
        }
        .selected-context-card {
            border: 1px solid #dbe3ef;
            border-radius: 6px;
            background: #ffffff;
            padding: 0.9rem 1rem;
        }
        .selected-context-card span {
            display: block;
            color: #64748b;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0;
            margin-bottom: 0.35rem;
        }
        .selected-context-card strong {
            color: #0f172a;
            font-size: 0.96rem;
            line-height: 1.35;
            word-break: break-word;
        }
        .login-step,
        .workflow-card {
            border: 0;
            border-top: 3px solid #dbeafe;
            border-radius: 0;
            background: transparent;
            padding: 0.85rem 0 0 0;
        }
        .login-step strong,
        .workflow-card strong {
            display: block;
            color: #0f172a;
            font-size: 0.95rem;
            margin-bottom: 0.25rem;
        }
        .login-step span,
        .workflow-card span {
            color: #64748b;
            font-size: 0.86rem;
        }
        .workflow-heading {
            margin: 0 0 1rem 0;
        }
        .workflow-heading h2 {
            color: #0f172a;
            font-size: 1.65rem;
            margin-bottom: 0.35rem;
        }
        .workflow-heading p {
            color: #64748b;
            margin: 0;
        }
        .workflow-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1rem;
            margin: 0.75rem 0 1rem 0;
            max-width: 1180px;
            margin-left: auto;
            margin-right: auto;
        }
        .workflow-card {
            min-height: 0;
            background: #ffffff;
            border: 1px solid #dbe3ef;
            border-radius: 8px;
            padding: 1rem;
            text-align: center;
        }
        .workflow-card .workflow-kicker {
            color: #2563eb;
            font-size: 0.78rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0;
            margin-bottom: 0.35rem;
        }
        .workflow-card strong {
            font-size: 1.08rem;
        }
        .direct-lookup-panel {
            border: 1px solid #dbe3ef;
            border-radius: 8px;
            background: #ffffff;
            padding: 1rem;
            margin: 0.75rem 0 1rem 0;
        }
        .direct-context-line {
            color: #475569;
            font-size: 0.9rem;
            padding: 0.65rem 0.8rem;
            border: 1px solid #e2e8f0;
            border-radius: 6px;
            background: #f8fafc;
            margin-bottom: 0.9rem;
        }
        @media (max-width: 900px) {
            .login-steps,
            .auth-step-table,
            .workflow-grid,
            .selected-context-grid,
            .inventory-metric-grid {
                grid-template-columns: 1fr;
            }
            div[data-testid="stHorizontalBlock"]:has(.app-top-strip) {
                position: static;
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 0.45rem;
                height: auto;
                min-height: auto;
                padding: 0.55rem;
                overflow: visible;
            }
            div[data-testid="stHorizontalBlock"]:has(.app-top-strip) > div[data-testid="stColumn"] {
                display: block;
                width: auto !important;
                height: auto;
                min-width: 0 !important;
                flex: none !important;
            }
            div[data-testid="stHorizontalBlock"]:has(.app-top-strip) > div[data-testid="stColumn"]:first-child {
                grid-column: 1 / -1;
            }
            div[data-testid="stHorizontalBlock"]:has(.app-top-strip) > div[data-testid="stColumn"]:nth-child(5) {
                display: none;
            }
            div[data-testid="stHorizontalBlock"]:has(.app-top-strip) div.stButton > button {
                min-height: 36px;
                padding-left: 0.25rem;
                padding-right: 0.25rem;
                font-size: 0.72rem;
            }
            .auth-step-cell {
                border-right: 0;
                border-bottom: 1px solid #e2e8f0;
            }
            .auth-step-cell:last-child {
                border-bottom: 0;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)

if 'auth_bundle' not in st.session_state:
    st.session_state.auth_bundle = None
if 'workflow_mode' not in st.session_state:
    st.session_state.workflow_mode = "landing"


# --- APP ROUTING ---
if not check_authenticated_session(logout_and_clear_session):
    render_login_page(clear_streamlit_session_state, get_all_tokens)
    st.stop()

headersMU = {'Authorization': f"Bearer {st.session_state.auth_bundle['mu']}", 'Content-Type': 'application/json'}
headersSP = headersMU
headersSPA = headersMU

workflow_mode = st.session_state.get("workflow_mode", "landing")
if workflow_mode not in {"landing", "guided", "direct_measure"}:
    workflow_mode = "landing"
    st.session_state.workflow_mode = workflow_mode

if workflow_mode == "landing":
    render_workflow_choice_page(
        headersSP,
        get_workspace_inventory=get_workspace_inventory,
        get_artifacts=get_artifacts,
        logout_and_clear_session=logout_and_clear_session,
        clear_streamlit_session_state=clear_streamlit_session_state,
    )
    st.stop()

if workflow_mode == "direct_measure":
    render_direct_measure_lookup_page(
        headersSPA,
        headersSP,
        headersMU,
        get_workspace_inventory=get_workspace_inventory,
        get_artifacts=get_artifacts,
        render_measure_source_lineage_view=render_measure_source_lineage_view,
        safe_widget_key=_safe_widget_key,
        logout_and_clear_session=logout_and_clear_session,
        clear_streamlit_session_state=clear_streamlit_session_state,
    )
    st.stop()

st.session_state.workflow_mode = "guided"


render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, "Guided workflow")
st.markdown(
    """
    <div class="page-header">
        <div class="page-eyebrow">Explore lineage</div>
        <h2>Workspace and report explorer</h2>
        <p>Select one or more workspaces and reports to inspect inventory, semantic objects, source data, measures, and visual-level lineage.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

# --- GUIDED SELECTION ---


# --- MAIN PAGE: POWER BI LINEAGE ---

if st.session_state.auth_bundle:
    # Master User Only mode: every API/header variable intentionally uses the same delegated user token.
    headersMU = {'Authorization': f"Bearer {st.session_state.auth_bundle['mu']}", 'Content-Type': 'application/json'}
    headersSP = headersMU
    headersSPA = headersMU

    selected_scope = "workspace"
    st.session_state.view_mode = "workspace"

    if 'view_mode' in st.session_state:
        
        # ==========================================
        # WORKSPACE VIEW - TAB-FIRST LAYOUT
        # ==========================================
        if st.session_state.view_mode == "workspace":
            if 'workspaces_list' not in st.session_state:
                with st.spinner("Fetching Workspaces..."):
                    st.session_state.workspaces_list = get_workspace_inventory(headersSP)
            
            workspaces = st.session_state.workspaces_list
            selected_ws_names = []
            all_reports_data = []
            all_dashboards_data = []
            all_users_data = []
            artifact_choice = "Report"
            artifact_mapping = {}
            selected_art_keys = []

            if workspaces:
                ws_mapping = {ws['name']: ws['id'] for ws in workspaces}
                with st.container(border=True):
                    st.markdown(
                        """
                        <div class="guided-selection-title">Selection</div>
                        <div class="guided-selection-help">Choose workspace(s), then choose report(s) from those workspaces.</div>
                        """,
                        unsafe_allow_html=True,
                    )
                    selection_ws_col, selection_report_col = st.columns(2)
                    with selection_ws_col:
                        selected_ws_names = render_checkbox_selector(
                            "Workspace",
                            list(ws_mapping.keys()),
                            key="workspace_selector",
                        )

                if selected_ws_names:
                    with st.spinner("Fetching artifacts for selected workspace(s)..."):
                        for ws_name in selected_ws_names:
                            ws_id = ws_mapping[ws_name]
                            cache_key_reports = f"reports_{ws_id}"
                            cache_key_dashboards = f"dashboards_{ws_id}"
                            cache_key_users = f"ws_users_{ws_id}"

                            if cache_key_reports not in st.session_state:
                                raw_reports = get_artifacts(headersSP, ws_id, 'report')
                                st.session_state[cache_key_reports] = [{
                                    'Workspace Name': ws_name,
                                    'Workspace ID': ws_id,
                                    'Name': r.get('name'),
                                    'ID': r.get('id'),
                                    'Type': r.get('reportType'),
                                    'Format': r.get('format'),
                                    'Dataset ID': r.get('datasetId'),
                                    'Embed URL': r.get('embedUrl')
                                } for r in raw_reports]

                            if cache_key_dashboards not in st.session_state:
                                raw_dashboards = get_artifacts(headersSP, ws_id, 'dashboard')
                                st.session_state[cache_key_dashboards] = [{
                                    'Workspace Name': ws_name,
                                    'Workspace ID': ws_id,
                                    'Name': d.get('displayName'),
                                    'ID': d.get('id'),
                                    'Type': 'Dashboard'
                                } for d in raw_dashboards]

                            if cache_key_users not in st.session_state:
                                raw_users = get_workspace_users(headersSP, ws_id)
                                st.session_state[cache_key_users] = [{
                                    'Workspace Name': ws_name,
                                    **u
                                } for u in raw_users]

                            all_reports_data.extend(st.session_state[cache_key_reports])
                            all_dashboards_data.extend(st.session_state[cache_key_dashboards])
                            all_users_data.extend(st.session_state[cache_key_users])

                    options_data = all_reports_data
                    if options_data:
                        artifact_mapping = {f"{item['Workspace Name']} ➔ {item['Name']}": item for item in options_data}
                        with selection_report_col:
                            selected_art_keys = render_checkbox_selector(
                                "Report",
                                list(artifact_mapping.keys()),
                                key="ws_artifact_checkbox_selector",
                            )
                    else:
                        with selection_report_col:
                            st.info("No reports available for the selected workspace(s).")
                else:
                    with selection_report_col:
                        st.info("Select at least one workspace to load reports.")

                selected_workspace_label = ", ".join(selected_ws_names) if selected_ws_names else "No workspace selected"
                selected_report_label = ", ".join(
                    (artifact_mapping.get(art_key) or {}).get("Name") or art_key
                    for art_key in selected_art_keys
                ) if selected_art_keys else "No report selected"
                st.markdown(
                    f"""
                    <div class="selected-context-grid">
                        <div class="selected-context-card">
                            <span>Workspace selected</span>
                            <strong>{html.escape(selected_workspace_label)}</strong>
                        </div>
                        <div class="selected-context-card">
                            <span>Report selected</span>
                            <strong>{html.escape(selected_report_label)}</strong>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            workspace_tab_labels = ["Workspaces Inventory"]
            if selected_ws_names:
                workspace_tab_labels.append("Workspace Artifacts")
            if selected_art_keys:
                workspace_tab_labels.append("Lineage Analysis")
                workspace_tab_labels.append("Visual Details")

            workspace_visual_lineage_tab_label = "Visual Item Lineage"
            workspace_uploaded_visual_lineage_ready = False
            if selected_art_keys and artifact_choice == "Report":
                workspace_selected_report_ids = [
                    (artifact_mapping.get(art_key) or {}).get("ID")
                    for art_key in selected_art_keys
                ]
                workspace_uploaded_visual_lineage_ready = has_uploaded_layout_records_for_report_ids(
                    "workspace",
                    workspace_selected_report_ids,
                )
                if workspace_uploaded_visual_lineage_ready:
                    workspace_tab_labels.append(workspace_visual_lineage_tab_label)

            workspace_tabs = st.tabs(workspace_tab_labels)
            workspace_tab_map = dict(zip(workspace_tab_labels, workspace_tabs))

            with workspace_tab_map["Workspaces Inventory"]:
                st.write("### Workspaces Inventory")
                if workspaces:
                    df_ws = pd.DataFrame(workspaces)[['name', 'id']]
                    st.dataframe(df_ws, use_container_width=True, hide_index=True)
                    render_csv_download(
                        df_ws,
                        "Download workspaces inventory as CSV",
                        "workspaces_inventory.csv",
                        "workspace_inventory_download",
                    )
                    if selected_ws_names:
                        st.success(f"{len(selected_ws_names)} workspace(s) selected above.")
                    else:
                        st.info("Select one or more workspaces above to load reports.")
                else:
                    st.info("No workspaces found.")

            if selected_ws_names:
                with workspace_tab_map["Workspace Artifacts"]:
                    st.write("### Workspace Artifacts")
                    st.caption("Reports, dashboards, and workspace-level access for the selected workspace(s).")
                    tab1, tab2, tab3 = st.tabs(["Reports", "Dashboards", "Workspace Access"])

                    with tab1:
                        if all_reports_data:
                            df_reports = pd.DataFrame(all_reports_data)
                            report_cols = [c for c in ['Workspace Name', 'Name', 'ID', 'Workspace ID', 'Dataset ID', 'Type'] if c in df_reports.columns]
                            st.dataframe(df_reports[report_cols], use_container_width=True, hide_index=True)
                            render_csv_download(
                                df_reports,
                                "Download report datasets as CSV",
                                "workspace_report_datasets.csv",
                                "workspace_report_datasets_download",
                            )
                        else:
                            st.info("No reports found in the selected workspace(s).")

                    with tab2:
                        if all_dashboards_data:
                            df_dashboards = pd.DataFrame(all_dashboards_data)
                            dashboard_cols = [c for c in ['Workspace Name', 'Name', 'ID', 'Workspace ID', 'Type'] if c in df_dashboards.columns]
                            st.dataframe(df_dashboards[dashboard_cols], use_container_width=True, hide_index=True)
                            render_csv_download(
                                df_dashboards,
                                "Download dashboards as CSV",
                                "workspace_dashboards.csv",
                                "workspace_dashboards_download",
                            )
                        else:
                            st.info("No dashboards found in the selected workspace(s).")

                    with tab3:
                        if all_users_data:
                            df_users = pd.DataFrame(all_users_data)
                            rename_map = {'displayName': 'Name', 'emailAddress': 'Email / Identifier', 'groupUserAccessRight': 'Access Level', 'principalType': 'Account Type'}
                            cols_to_keep = ['Workspace Name'] + [c for c in rename_map.keys() if c in df_users.columns]
                            display_users = df_users[cols_to_keep].rename(columns=rename_map)
                            st.dataframe(display_users, use_container_width=True, hide_index=True)
                            render_csv_download(
                                display_users,
                                "Download workspace access as CSV",
                                "workspace_access.csv",
                                "workspace_access_download",
                            )
                        else:
                            st.info("No user access data found.")

            if selected_art_keys:
                total_items = len(selected_art_keys)
                xmla_token = st.session_state.auth_bundle['sp']

                with workspace_tab_map["Lineage Analysis"]:
                    st.write("### Artifact Lineage")
                    st.caption(f"Showing lineage for {len(selected_art_keys)} selected report(s). Change report selections above.")
                    deep_dive_tab_source, deep_dive_tab_semantic, deep_dive_tab_measure_lineage = st.tabs([
                        "Source DB Lineage",
                        "Semantic Model Objects",
                        "Measure Source Lineage"
                    ])

                    with deep_dive_tab_source:
                        if artifact_choice == "Report":
                            all_tables_data = []
                            progress_bar = st.progress(0, text="Initializing source lineage fetch...")
                            for idx, art_key in enumerate(selected_art_keys):
                                selected_item = artifact_mapping[art_key]
                                original_id = selected_item.get('ID')
                                report_type = selected_item.get('Type')
                                art_name = selected_item.get('Name')
                                Workspace_name = selected_item.get('Workspace Name')
                                progress_bar.progress((idx + 1) / total_items, text=f"Resolving source lineage for: {art_name}...")

                                if report_type == 'PaginatedReport':
                                    st.warning(f"'{art_name}' in Workspace '{Workspace_name}' is a Paginated Report (.rdl) and does not use standard Datasets.")
                                    continue

                                dataset_id = selected_item.get('Dataset ID')
                                target_workspace_id = selected_item.get('Workspace ID')
                                if not dataset_id or not target_workspace_id:
                                    cache_key_resolved_tables = f"workspace_report_resolved_ids_v17_{original_id}"
                                    if cache_key_resolved_tables not in st.session_state or not isinstance(st.session_state[cache_key_resolved_tables], tuple):
                                        res_ds_id, res_ws_id = resolve_dataset_for_app_report(headersSPA, original_id)
                                        st.session_state[cache_key_resolved_tables] = (res_ds_id, res_ws_id)
                                    res_dataset_id, res_workspace_id = st.session_state[cache_key_resolved_tables]
                                    dataset_id = dataset_id or res_dataset_id
                                    target_workspace_id = target_workspace_id or res_workspace_id

                                if dataset_id and target_workspace_id:
                                    cache_key_tables = f"source_db_lineage_v18_{target_workspace_id}_{dataset_id}"
                                    if cache_key_tables not in st.session_state:
                                        st.session_state[cache_key_tables] = get_object_info(
                                            headersSPA,
                                            target_workspace_id,
                                            dataset_id,
                                            xmla_token,
                                            workspace_name_hint=Workspace_name,
                                            dataset_name_hint=selected_item.get("Dataset Name") or art_name,
                                            auth_headers=[("MasterUser", headersMU)],
                                        )
                                    tables = st.session_state.get(cache_key_tables, [])
                                    for t_info in tables:
                                        if isinstance(t_info, dict):
                                            all_tables_data.append({"Workspace Name": Workspace_name, "Source Report": art_name, "Report ID": original_id, "Dataset ID": dataset_id, **t_info})
                                        else:
                                            all_tables_data.append({"Workspace Name": Workspace_name, "Source Report": art_name, "Report ID": original_id, "Dataset ID": dataset_id, "Table Name": str(t_info)})
                                else:
                                    st.error(f"Could not resolve Dataset ID / Workspace ID for Workspace report: {art_name}")
                            progress_bar.empty()
                            render_source_db_lineage_records(
                                all_tables_data,
                                "No source DB lineage returned for selected reports.",
                                "workspace_source_db_lineage_download",
                            )

                        elif artifact_choice == "Dashboard":
                            all_dashboard_tiles = []
                            all_dashboard_source_lineage = []
                            progress_bar = st.progress(0, text="Initializing dashboard source lineage...")
                            for idx, art_key in enumerate(selected_art_keys):
                                selected_item = artifact_mapping[art_key]
                                art_id = selected_item['ID']
                                target_ws_id = selected_item['Workspace ID']
                                art_name = selected_item['Name']
                                ws_name = selected_item['Workspace Name']
                                progress_bar.progress((idx + 1) / total_items, text=f"Fetching source lineage for Dashboard: {art_name}...")
                                cache_key_tiles = f"tiles_{target_ws_id}_{art_id}"
                                if cache_key_tiles not in st.session_state:
                                    st.session_state[cache_key_tiles] = get_dashboard_tiles(headersSP, target_ws_id, art_id)
                                tiles = st.session_state.get(cache_key_tiles, [])
                                unique_dataset_ids = set()
                                for tile in tiles:
                                    ds_id = tile.get('datasetId')
                                    all_dashboard_tiles.append({
                                        "Workspace": ws_name,
                                        "Source Dashboard": art_name,
                                        "Tile Title": tile.get('title', 'Untitled'),
                                        "Tile ID": tile.get('id'),
                                        "Dataset ID": ds_id
                                    })
                                    if ds_id:
                                        unique_dataset_ids.add((ws_name, art_name, target_ws_id, ds_id))
                                for w_name, dash_name, default_ws_id, ds_id in unique_dataset_ids:
                                    cache_key_dataset_ws = f"dataset_workspace_{ds_id}"
                                    if cache_key_dataset_ws not in st.session_state:
                                        st.session_state[cache_key_dataset_ws] = resolve_workspace_for_dataset(headersSPA, ds_id) or default_ws_id
                                    dataset_workspace_id = st.session_state.get(cache_key_dataset_ws) or default_ws_id
                                    cache_key_source_lineage = f"source_db_lineage_v18_{dataset_workspace_id}_{ds_id}"
                                    if cache_key_source_lineage not in st.session_state:
                                        st.session_state[cache_key_source_lineage] = get_object_info(headersSPA, dataset_workspace_id, ds_id, xmla_token, workspace_name_hint=w_name, auth_headers=[("MasterUser", headersMU)])
                                    source_lineage = st.session_state.get(cache_key_source_lineage, [])
                                    for source_info in source_lineage:
                                        all_dashboard_source_lineage.append({
                                            "Workspace": w_name,
                                            "Source Dashboard": dash_name,
                                            "Dataset ID": ds_id,
                                            **source_info
                                        })
                            progress_bar.empty()
                            st.write("##### 🖼️ Dashboard Tiles Info")
                            if all_dashboard_tiles:
                                df_dashboard_tiles = pd.DataFrame(all_dashboard_tiles)
                                st.dataframe(df_dashboard_tiles, use_container_width=True, hide_index=True)
                                render_csv_download(
                                    df_dashboard_tiles,
                                    "Download dashboard tile datasets as CSV",
                                    "workspace_dashboard_tile_datasets.csv",
                                    "workspace_dashboard_tile_datasets_download",
                                )
                            else:
                                st.info("No tiles found in selected dashboards.")
                            st.markdown("---")
                            st.write("##### Source DB Lineage")
                            render_source_db_lineage_records(
                                all_dashboard_source_lineage,
                                "No source DB lineage found from selected dashboard tiles.",
                                "workspace_dashboard_source_db_lineage_download",
                            )

                    with deep_dive_tab_semantic:
                        if artifact_choice == "Report":
                            workspace_report_contexts = build_workspace_report_contexts(selected_art_keys, artifact_mapping)
                            render_semantic_model_objects_view(
                                workspace_report_contexts,
                                headersSPA,
                                headersSP,
                                xmla_token,
                                "workspace",
                                "workspace_semantic_model_objects_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Semantic object catalogue is report/model based. Select Report to view all semantic columns and measures.")

                    with deep_dive_tab_measure_lineage:
                        if artifact_choice == "Report":
                            workspace_report_contexts = build_workspace_report_contexts(selected_art_keys, artifact_mapping)
                            render_measure_source_lineage_view(
                                workspace_report_contexts,
                                headersSPA,
                                xmla_token,
                                "workspace",
                                "workspace_measure_source_lineage_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Measure lineage is report/model based. Select Report to view measure-to-source-column lineage.")

                with workspace_tab_map["Visual Details"]:
                    st.write("### Visual Details")
                    st.caption("Retrieve report definitions automatically and build visual-to-source lookup from their layout metadata.")
                    visual_layout_tab, visual_lookup_tab = st.tabs([
                        "Report Layout",
                        "Visual Source Lookup"
                    ])

                    with visual_layout_tab:
                        if artifact_choice == "Report":
                            workspace_report_contexts = build_workspace_report_contexts(selected_art_keys, artifact_mapping)
                            render_upload_only_report_layout_view(
                                workspace_report_contexts,
                                "workspace",
                                "workspace_uploaded_report_layout_download",
                                powerbi_headers=headersSPA,
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Report definition retrieval is available for Reports. For dashboards, select the source report.")

                    with visual_lookup_tab:
                        if artifact_choice == "Report":
                            workspace_report_contexts = build_workspace_report_contexts(selected_art_keys, artifact_mapping)
                            render_visual_source_lookup_view(
                                workspace_report_contexts,
                                headersSPA,
                                headersSP,
                                xmla_token,
                                "workspace",
                                "workspace",
                                "workspace_visual_source_lookup_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Lookup view is report-layout based. Select the source Report first.")

                if workspace_uploaded_visual_lineage_ready:
                    with workspace_tab_map[workspace_visual_lineage_tab_label]:
                        st.write("### Visual Item Lineage")
                        st.caption("Joined visual item, semantic object, measure dependency, and source database lineage from retrieved report layouts.")
                        workspace_report_contexts = build_workspace_report_contexts(selected_art_keys, artifact_mapping)
                        render_visual_source_lookup_view(
                            workspace_report_contexts,
                            headersSPA,
                            headersSP,
                            xmla_token,
                            "workspace",
                            "workspace",
                            "workspace_visual_item_lineage_download",
                        )

        # ==========================================
        # APP VIEW - WORKSPACE-LIKE FLOW (NO AUDIENCE)
        # ==========================================
        elif st.session_state.view_mode == "app":
            if 'apps_list' not in st.session_state:
                with st.spinner("Fetching Apps..."):
                    st.session_state.apps_list = get_all_app_details(headersSPA)

            apps = st.session_state.apps_list
            selected_app_names = []
            all_app_reports = []
            all_app_dashboards = []
            all_app_users = []
            artifact_choice = st.session_state.get("app_radio", "Report")
            app_art_mapping = {}
            selected_art_keys = []

            if apps:
                app_mapping = {app['name']: app['id'] for app in apps}
                with st.sidebar:
                    st.subheader("2. App")
                    selected_app_names = render_checkbox_selector(
                        "Select app(s)",
                        list(app_mapping.keys()),
                        key="app_selector",
                    )

                if selected_app_names:
                    with st.spinner("Fetching artifacts for selected app(s)..."):
                        for app_name in selected_app_names:
                            app_id = app_mapping[app_name]
                            cache_key_app_reports = f"app_reports_{app_id}"
                            cache_key_app_dashboards = f"app_dashboards_{app_id}"
                            cache_key_app_users = f"app_users_{app_id}"

                            if cache_key_app_reports not in st.session_state:
                                raw_reports = get_app_artifacts(headersMU, app_id, 'report')
                                st.session_state[cache_key_app_reports] = [{
                                    'App Name': app_name,
                                    'App ID': app_id,
                                    'Name': r.get('name'),
                                    'ID': r.get('originalReportObjectId') or r.get('id'),
                                    'Type': r.get('reportType', 'Report'),
                                    'Format': r.get('format'),
                                    'Original ID': r.get('originalReportObjectId') or r.get('id'),
                                    'Workspace ID': r.get('workspaceId'),
                                    'Dataset ID': r.get('datasetId'),
                                    'Embed URL': r.get('embedUrl')
                                } for r in raw_reports]

                            if cache_key_app_dashboards not in st.session_state:
                                raw_dashboards = get_app_artifacts(headersMU, app_id, 'dashboard')
                                st.session_state[cache_key_app_dashboards] = [{
                                    'App Name': app_name,
                                    'App ID': app_id,
                                    'Name': d.get('displayName'),
                                    'ID': d.get('id'),
                                    'Original ID': d.get('originalDashboardObjectId', d.get('id')),
                                    'Workspace ID': d.get('workspaceId'),
                                    'Type': 'Dashboard'
                                } for d in raw_dashboards]

                            if cache_key_app_users not in st.session_state:
                                raw_users = get_app_users(headersSPA, app_id)
                                st.session_state[cache_key_app_users] = [{
                                    'App Name': app_name,
                                    'App ID': app_id,
                                    **u
                                } for u in raw_users]

                            all_app_reports.extend(st.session_state[cache_key_app_reports])
                            all_app_dashboards.extend(st.session_state[cache_key_app_dashboards])
                            all_app_users.extend(st.session_state[cache_key_app_users])

                    with st.sidebar:
                        st.subheader("3. Artifact")
                        artifact_choice = st.radio(
                            "App artifact type",
                            ["Report", "Dashboard"],
                            horizontal=False,
                            key="app_radio"
                        )

                    options_data = all_app_reports if artifact_choice == "Report" else all_app_dashboards
                    if options_data:
                        app_art_mapping = {
                            f"{item.get('App Name', 'N/A')} ➔ {item.get('Name', 'Unnamed')}": item
                            for item in options_data
                        }
                        with st.sidebar:
                            selected_art_keys = render_checkbox_selector(
                                f"Select app {artifact_choice.lower()}(s)",
                                list(app_art_mapping.keys()),
                                key="app_artifact_checkbox_selector",
                            )
                    else:
                        with st.sidebar:
                            st.info(f"No app {artifact_choice.lower()}s found for the selected app(s).")

            app_tab_labels = ["📱 Apps Inventory"]
            if selected_app_names:
                app_tab_labels.append("🗂️ Combined App Artifacts")
            if selected_art_keys:
                app_tab_labels.append("🔍 App Deep Dive: Lineage")
                app_tab_labels.append("🧩 Visual Level Details")

            app_visual_lineage_tab_label = "Visual Item Lineage"
            app_uploaded_visual_lineage_ready = False
            if selected_art_keys and artifact_choice == "Report":
                app_selected_report_ids = [
                    (app_art_mapping.get(art_key) or {}).get("Original ID")
                    or (app_art_mapping.get(art_key) or {}).get("ID")
                    for art_key in selected_art_keys
                ]
                app_uploaded_visual_lineage_ready = has_uploaded_layout_records_for_report_ids(
                    "app",
                    app_selected_report_ids,
                )
                if app_uploaded_visual_lineage_ready:
                    app_tab_labels.append(app_visual_lineage_tab_label)

            app_tabs = st.tabs(app_tab_labels)
            app_tab_map = dict(zip(app_tab_labels, app_tabs))

            with app_tab_map["📱 Apps Inventory"]:
                st.write("### 📱 Apps Inventory")
                if apps:
                    df_apps = pd.DataFrame(apps)[['name', 'id']]
                    st.dataframe(df_apps, use_container_width=True, hide_index=True)
                    render_csv_download(
                        df_apps,
                        "Download apps inventory as CSV",
                        "apps_inventory.csv",
                        "apps_inventory_download",
                    )
                    if selected_app_names:
                        st.success(f"{len(selected_app_names)} app(s) selected from the left panel.")
                    else:
                        st.info("Select one or more apps from the left panel to load app reports and dashboards.")
                else:
                    st.info("No apps found.")

            if selected_app_names:
                with app_tab_map["🗂️ Combined App Artifacts"]:
                    st.write("### 🗂️ Combined App Artifacts")
                    tab1, tab2, tab3 = st.tabs(["📊 App Reports", "📈 App Dashboards", "👥 App Access"])

                    with tab1:
                        if all_app_reports:
                            df_reports = pd.DataFrame(all_app_reports)
                            cols = [c for c in ['App Name', 'Name', 'ID', 'Original ID', 'Workspace ID', 'Dataset ID', 'Type'] if c in df_reports.columns]
                            st.dataframe(df_reports[cols], use_container_width=True, hide_index=True)
                            render_csv_download(
                                df_reports,
                                "Download app report datasets as CSV",
                                "app_report_datasets.csv",
                                "app_report_datasets_download",
                            )
                        else:
                            st.info("No reports found in the selected app(s).")

                    with tab2:
                        if all_app_dashboards:
                            df_dashboards = pd.DataFrame(all_app_dashboards)
                            cols = [c for c in ['App Name', 'Name', 'ID', 'Original ID', 'Workspace ID', 'Type'] if c in df_dashboards.columns]
                            st.dataframe(df_dashboards[cols], use_container_width=True, hide_index=True)
                            render_csv_download(
                                df_dashboards,
                                "Download app dashboards as CSV",
                                "app_dashboards.csv",
                                "app_dashboards_download",
                            )
                        else:
                            st.info("No dashboards found in the selected app(s).")

                    with tab3:
                        if all_app_users:
                            df_users = pd.DataFrame(all_app_users)
                            rename_map = {
                                'displayName': 'Name',
                                'emailAddress': 'Email',
                                'appUserAccessRight': 'Access Level',
                                'principalType': 'Type'
                            }
                            cols_to_keep = [c for c in ['App Name', 'App ID'] if c in df_users.columns] + [c for c in rename_map.keys() if c in df_users.columns]
                            display_app_users = df_users[cols_to_keep].rename(columns=rename_map)
                            st.dataframe(display_app_users, use_container_width=True, hide_index=True)
                            render_csv_download(
                                display_app_users,
                                "Download app access as CSV",
                                "app_access.csv",
                                "app_access_download",
                            )
                            st.caption("Flattened app access from the App/Admin APIs. Audience-level mapping is intentionally removed from this version.")
                        else:
                            st.info("No user data found. Ensure the signed-in account has app/admin access.")

                        render_internal_app_audience_test(
                            app_mapping,
                            selected_app_names,
                            headersMU,
                            headersSPA,
                            headersSP,
                            all_app_reports,
                            all_app_dashboards,
                            apps,
                        )

            if selected_art_keys:
                total_app_items = len(selected_art_keys)
                xmla_token = st.session_state.auth_bundle['spa']

                with app_tab_map["🔍 App Deep Dive: Lineage"]:
                    st.write("### 🔍 App Deep Dive: Lineage")
                    st.caption(f"Showing lineage for {len(selected_art_keys)} selected app {artifact_choice.lower()}(s). Change selections from the left panel.")
                    deep_dive_tab_source, deep_dive_tab_semantic, deep_dive_tab_measure_lineage = st.tabs([
                        "🔌 Source DB Lineage",
                        "📐 Semantic Model Objects",
                        "🔗 Measure Source Lineage"
                    ])

                    with deep_dive_tab_source:
                        if artifact_choice == "Report":
                            all_app_source_lineage = []
                            progress_bar = st.progress(0, text="Initializing App Source DB Lineage Fetch...")
                            for idx, art_key in enumerate(selected_art_keys):
                                selected_item = app_art_mapping[art_key]
                                original_id = selected_item.get('Original ID') or selected_item.get('ID')
                                report_type = selected_item.get('Type')
                                art_name = selected_item.get('Name')
                                app_name = selected_item.get('App Name')
                                progress_bar.progress((idx + 1) / total_app_items, text=f"Resolving source lineage for: {art_name}...")
                                if report_type == 'PaginatedReport':
                                    st.warning(f"'{art_name}' in App '{app_name}' is a Paginated Report (.rdl) and does not use standard Datasets.")
                                    continue

                                # Prefer IDs already returned by the app report API. Fall back to Admin report lookup only when missing.
                                dataset_id = selected_item.get('Dataset ID')
                                target_workspace_id = selected_item.get('Workspace ID')
                                if not dataset_id or not target_workspace_id:
                                    cache_key_resolved_ids = f"app_report_resolved_ids_v17_{original_id}"
                                    if cache_key_resolved_ids not in st.session_state or not isinstance(st.session_state[cache_key_resolved_ids], tuple):
                                        res_ds_id, res_ws_id = resolve_dataset_for_app_report(headersSPA, original_id)
                                        st.session_state[cache_key_resolved_ids] = (res_ds_id, res_ws_id)
                                    resolved_dataset_id, resolved_workspace_id = st.session_state[cache_key_resolved_ids]
                                    dataset_id = dataset_id or resolved_dataset_id
                                    target_workspace_id = target_workspace_id or resolved_workspace_id

                                if dataset_id and target_workspace_id:
                                    cache_key_source_lineage = f"source_db_lineage_v18_{target_workspace_id}_{dataset_id}"
                                    if cache_key_source_lineage not in st.session_state:
                                        st.session_state[cache_key_source_lineage] = get_object_info(headersSPA, target_workspace_id, dataset_id, xmla_token, dataset_name_hint=art_name, auth_headers=[("MasterUser", headersMU)])
                                    source_lineage = st.session_state.get(cache_key_source_lineage, [])
                                    for source_info in source_lineage:
                                        all_app_source_lineage.append({
                                            "App Name": app_name,
                                            "Source Report": art_name,
                                            "Report ID": original_id,
                                            "Dataset ID": dataset_id,
                                            **source_info
                                        })
                                else:
                                    st.error(f"Could not resolve Dataset ID / Workspace ID for App report: {art_name}")
                            progress_bar.empty()
                            render_source_db_lineage_records(
                                all_app_source_lineage,
                                "No source DB lineage returned for selected app reports.",
                                "app_source_db_lineage_download",
                            )
                        elif artifact_choice == "Dashboard":
                            all_app_tiles = []
                            all_app_dashboard_source_lineage = []
                            progress_bar = st.progress(0, text="Initializing App Dashboard Source DB Lineage...")
                            for idx, art_key in enumerate(selected_art_keys):
                                selected_item = app_art_mapping[art_key]
                                art_id = selected_item['ID']
                                app_id = selected_item['App ID']
                                art_name = selected_item['Name']
                                app_name = selected_item['App Name']
                                progress_bar.progress((idx + 1) / total_app_items, text=f"Fetching source lineage for App Dashboard: {art_name}...")
                                cache_key_app_tiles = f"app_tiles_{app_id}_{art_id}"
                                if cache_key_app_tiles not in st.session_state:
                                    st.session_state[cache_key_app_tiles] = get_app_dashboard_tiles(headersMU, app_id, art_id)
                                tiles = st.session_state.get(cache_key_app_tiles, [])
                                unique_dataset_ids = set()
                                for tile in tiles:
                                    ds_id = tile.get('datasetId')
                                    all_app_tiles.append({
                                        "App Name": app_name,
                                        "Source Dashboard": art_name,
                                        "Tile Title": tile.get('title', 'Untitled'),
                                        "Tile ID": tile.get('id'),
                                        "Dataset ID": ds_id
                                    })
                                    if ds_id:
                                        unique_dataset_ids.add((app_name, art_name, ds_id))
                                for a_name, dash_name, ds_id in unique_dataset_ids:
                                    cache_key_dataset_ws = f"dataset_workspace_{ds_id}"
                                    if cache_key_dataset_ws not in st.session_state:
                                        st.session_state[cache_key_dataset_ws] = resolve_workspace_for_dataset(headersSPA, ds_id)
                                    target_workspace_id = st.session_state.get(cache_key_dataset_ws)
                                    if not target_workspace_id:
                                        st.warning(f"Could not resolve Workspace ID for Dataset ID: {ds_id}")
                                        continue
                                    cache_key_source_lineage = f"source_db_lineage_v18_{target_workspace_id}_{ds_id}"
                                    if cache_key_source_lineage not in st.session_state:
                                        st.session_state[cache_key_source_lineage] = get_object_info(headersSPA, target_workspace_id, ds_id, xmla_token, auth_headers=[("MasterUser", headersMU)])
                                    source_lineage = st.session_state.get(cache_key_source_lineage, [])
                                    for source_info in source_lineage:
                                        all_app_dashboard_source_lineage.append({
                                            "App Name": a_name,
                                            "Source Dashboard": dash_name,
                                            "Dataset ID": ds_id,
                                            **source_info
                                        })
                            progress_bar.empty()
                            st.write("##### 🖼️ App Dashboard Tiles Info")
                            if all_app_tiles:
                                df_app_tiles = pd.DataFrame(all_app_tiles)
                                st.dataframe(df_app_tiles, use_container_width=True, hide_index=True)
                                render_csv_download(
                                    df_app_tiles,
                                    "Download app dashboard tile datasets as CSV",
                                    "app_dashboard_tile_datasets.csv",
                                    "app_dashboard_tile_datasets_download",
                                )
                            else:
                                st.info("No tiles found in selected App dashboards.")
                            st.markdown("---")
                            st.write("##### 🔌 Source DB Lineage")
                            render_source_db_lineage_records(
                                all_app_dashboard_source_lineage,
                                "No source DB lineage found from selected App dashboard tiles.",
                                "app_dashboard_source_db_lineage_download",
                            )

                    with deep_dive_tab_semantic:
                        if artifact_choice == "Report":
                            app_report_contexts = build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA)
                            render_semantic_model_objects_view(
                                app_report_contexts,
                                headersSPA,
                                headersSP,
                                xmla_token,
                                "app",
                                "app_semantic_model_objects_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Semantic object catalogue is report/model based. Select App Report to view all semantic columns and measures.")

                    with deep_dive_tab_measure_lineage:
                        if artifact_choice == "Report":
                            app_report_contexts = build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA)
                            render_measure_source_lineage_view(
                                app_report_contexts,
                                headersSPA,
                                xmla_token,
                                "app",
                                "app_measure_source_lineage_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Measure lineage is report/model based. Select App Report to view measure-to-source-column lineage.")

                with app_tab_map["🧩 Visual Level Details"]:
                    st.write("### 🧩 Visual Level Details")
                    st.caption("Retrieve report definitions automatically and build visual-to-source lookup from their layout metadata.")
                    app_visual_layout_tab, app_visual_lookup_tab = st.tabs([
                        "🧩 Report Layout",
                        "🔎 Visual Source Lookup"
                    ])
                    with app_visual_layout_tab:
                        if artifact_choice == "Report":
                            app_report_contexts = build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA)
                            render_upload_only_report_layout_view(
                                app_report_contexts,
                                "app",
                                "app_uploaded_report_layout_download",
                                powerbi_headers=headersSPA,
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Report definition retrieval is available for Reports. For dashboards, select the source App Report.")
                    with app_visual_lookup_tab:
                        if artifact_choice == "Report":
                            app_report_contexts = build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA)
                            render_visual_source_lookup_view(
                                app_report_contexts,
                                headersSPA,
                                headersSP,
                                xmla_token,
                                "app",
                                "app",
                                "app_visual_source_lookup_download",
                            )
                        elif artifact_choice == "Dashboard":
                            st.info("Lookup view is report-layout based. Select the source App Report first.")

                if app_uploaded_visual_lineage_ready:
                    with app_tab_map[app_visual_lineage_tab_label]:
                        st.write("### Visual Item Lineage")
                        st.caption("Joined visual item, semantic object, measure dependency, and source database lineage from retrieved app report layouts.")
                        app_report_contexts = build_app_report_contexts(selected_art_keys, app_art_mapping, headersSPA)
                        render_visual_source_lookup_view(
                            app_report_contexts,
                            headersSPA,
                            headersSP,
                            xmla_token,
                            "app",
                            "app",
                            "app_visual_item_lineage_download",
                        )
else:
    st.warning("Please use the top Login button to authenticate and retrieve access tokens.")
