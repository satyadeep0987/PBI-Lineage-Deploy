"""Standalone Power BI report download diagnostic.

The script tests the supported Reports - Export Report In Group API. It does
not import or modify the Streamlit application. Successful files are written
under the git-ignored ``downloads`` directory unless --output-dir is supplied.

Examples:
    python powerbi_report_download_demo.py \
        --workspace "Sales&Marketing" --report "Executive Sales"
    python powerbi_report_download_demo.py \
        --workspace "Sales&Marketing" --report "Executive Sales" \
        --auth-mode all --download-type auto
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

import msal
import requests

from utils import Utils


POWER_BI_API = "https://api.powerbi.com/v1.0/myorg"
POWER_BI_DEFAULT_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_DEFAULT_SCOPE = "https://api.fabric.microsoft.com/.default"
FABRIC_REPORT_SCOPE = "https://api.fabric.microsoft.com/Report.ReadWrite.All"
AUTH_MODES = ("MasterUser", "ServicePrincipal", "ServicePrincipal-Admin")
DOWNLOAD_TYPES = ("auto", "LiveConnect", "IncludeModel", "Default")


@dataclass(frozen=True)
class DownloadAttempt:
    mode: str
    status_code: int
    error_code: str = ""
    error_message: str = ""
    request_id: str = ""
    file_path: Optional[Path] = None
    byte_count: int = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test automatic Power BI .pbix/.rdl report download through the REST API.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Exact workspace name or workspace GUID.",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Exact report name or report GUID.",
    )
    parser.add_argument(
        "--auth-mode",
        default="MasterUser",
        choices=(*AUTH_MODES, "all"),
        help="Configured identity to test. 'all' tests all configured identities.",
    )
    parser.add_argument(
        "--download-type",
        default="auto",
        choices=DOWNLOAD_TYPES,
        help="PBIX mode. 'auto' tries LiveConnect, IncludeModel, then the API default.",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Directory for successful downloads (default: downloads).",
    )
    parser.add_argument(
        "--config",
        help="Optional path to a local Power BI auth JSON file. The file is read only.",
    )
    parser.add_argument(
        "--skip-definition-fallback",
        action="store_true",
        help="Do not try the Fabric Get Report Definition API after PBIX export fails.",
    )
    return parser.parse_args()


def _tenant_authority(authority: Any, tenant_id: Any) -> str:
    tenant = str(tenant_id or "").strip()
    raw_authority = str(authority or "https://login.microsoftonline.com").strip().rstrip("/")
    parsed = urlsplit(raw_authority)
    if parsed.scheme and parsed.netloc:
        path_parts = [part for part in parsed.path.split("/") if part]
        if not path_parts or path_parts[-1].lower() in {"common", "organizations", "consumers"}:
            path_parts = [tenant]
        return urlunsplit((parsed.scheme, parsed.netloc, "/" + "/".join(path_parts), "", ""))
    return f"https://login.microsoftonline.com/{tenant}"


def _access_token_env_names(auth_mode: str) -> Tuple[str, ...]:
    mode_name = re.sub(r"[^A-Z0-9]+", "_", auth_mode.upper()).strip("_")
    return (
        f"PBI_{mode_name}_ACCESS_TOKEN",
        "PBI_DOWNLOAD_ACCESS_TOKEN",
        "PBI_XMLA_ACCESS_TOKEN",
    )


def _token_from_environment(auth_mode: str) -> Optional[str]:
    for variable_name in _access_token_env_names(auth_mode):
        value = str(os.getenv(variable_name) or "").strip()
        if value:
            return re.sub(r"^Bearer\s+", "", value, flags=re.IGNORECASE).strip()
    return None


def _fabric_token_from_environment(auth_mode: str) -> Optional[str]:
    mode_name = re.sub(r"[^A-Z0-9]+", "_", auth_mode.upper()).strip("_")
    for variable_name in (f"PBI_{mode_name}_FABRIC_ACCESS_TOKEN", "PBI_FABRIC_ACCESS_TOKEN"):
        value = str(os.getenv(variable_name) or "").strip()
        if value:
            return re.sub(r"^Bearer\s+", "", value, flags=re.IGNORECASE).strip()
    return None


def _load_config(auth_mode: str) -> Dict[str, Any]:
    result = Utils.validate_config(auth_mode)
    if isinstance(result, str):
        raise RuntimeError(result)
    return result


def _acquire_token(auth_mode: str, config: Dict[str, Any]) -> str:
    supplied_token = _token_from_environment(auth_mode)
    if supplied_token:
        print("  Authentication: using access token from an environment variable")
        return supplied_token

    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    authority = _tenant_authority(config.get("authority"), tenant_id)

    if auth_mode == "MasterUser":
        scopes = [str(scope).strip() for scope in config.get("scope", []) if str(scope).strip()]
        if not scopes:
            raise RuntimeError("MasterUser has no delegated Power BI scopes configured.")
        app = msal.PublicClientApplication(client_id=client_id, authority=authority)
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            detail = flow.get("error_description") or flow.get("error") or "unknown device-flow error"
            raise RuntimeError(f"Could not start device-code authentication: {detail}")
        print("  Authentication: delegated device-code flow")
        print(f"  {flow.get('message')}")
        result = app.acquire_token_by_device_flow(flow)
    else:
        client_secret = str(config.get("client_secret") or "").strip()
        if not client_secret:
            raise RuntimeError(f"{auth_mode} requires client_secret in the selected configuration.")
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=authority,
        )
        print("  Authentication: service principal client-credentials flow")
        result = app.acquire_token_for_client(scopes=[POWER_BI_DEFAULT_SCOPE])

    access_token = str((result or {}).get("access_token") or "").strip()
    if not access_token:
        detail = (result or {}).get("error_description") or (result or {}).get("error")
        raise RuntimeError(f"Token acquisition failed: {detail or 'access token was not returned'}")
    return access_token


def _acquire_fabric_token(auth_mode: str, config: Dict[str, Any]) -> str:
    supplied_token = _fabric_token_from_environment(auth_mode)
    if supplied_token:
        print("    Authentication: using Fabric access token from an environment variable")
        return supplied_token

    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    authority = _tenant_authority(config.get("authority"), tenant_id)

    if auth_mode == "MasterUser":
        app = msal.PublicClientApplication(client_id=client_id, authority=authority)
        flow = app.initiate_device_flow(scopes=[FABRIC_REPORT_SCOPE])
        if "user_code" not in flow:
            detail = flow.get("error_description") or flow.get("error") or "unknown device-flow error"
            raise RuntimeError(f"Could not start Fabric device-code authentication: {detail}")
        print("    Authentication: delegated Fabric device-code flow")
        print(f"    {flow.get('message')}")
        result = app.acquire_token_by_device_flow(flow)
    else:
        client_secret = str(config.get("client_secret") or "").strip()
        if not client_secret:
            raise RuntimeError(f"{auth_mode} requires client_secret in the selected configuration.")
        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=authority,
        )
        print("    Authentication: service principal token for Fabric API")
        result = app.acquire_token_for_client(scopes=[FABRIC_DEFAULT_SCOPE])

    access_token = str((result or {}).get("access_token") or "").strip()
    if not access_token:
        detail = (result or {}).get("error_description") or (result or {}).get("error")
        raise RuntimeError(f"Fabric token acquisition failed: {detail or 'access token was not returned'}")
    return access_token


def _sanitize_error(error: BaseException, protected_values: Iterable[str]) -> str:
    message = str(error)
    for protected in protected_values:
        secret = str(protected or "")
        if secret:
            message = message.replace(secret, "<redacted>")
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer <redacted>", message)
    return message


def _headers(access_token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _response_error(response: requests.Response) -> Tuple[str, str]:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        body = None

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = error.get("message")
            if isinstance(message, dict):
                message = message.get("value") or json.dumps(message)
            return code, str(message or "")
        return str(body.get("errorCode") or body.get("code") or ""), str(body.get("message") or "")

    raw_text = str(response.text or "").strip().replace("\r", " ").replace("\n", " ")
    return "", raw_text[:1000]


def _request_id(response: requests.Response) -> str:
    for name in ("requestId", "RequestId", "x-ms-request-id", "ActivityId"):
        if response.headers.get(name):
            return str(response.headers[name])
    return ""


def _get_json(url: str, headers: Dict[str, str], timeout: int = 60) -> Dict[str, Any]:
    response = requests.get(url, headers=headers, timeout=timeout)
    if response.status_code != 200:
        code, message = _response_error(response)
        detail = f"HTTP {response.status_code} {code}: {message}".strip()
        raise RuntimeError(detail)
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _is_guid(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            str(value or "").strip(),
        )
    )


def _resolve_workspace(headers: Dict[str, str], workspace_input: str) -> Dict[str, Any]:
    requested = workspace_input.strip()
    if _is_guid(requested):
        workspace = _get_json(f"{POWER_BI_API}/groups/{requested}", headers)
        if not workspace.get("id"):
            workspace["id"] = requested
        return workspace

    payload = _get_json(f"{POWER_BI_API}/groups?$top=5000", headers)
    workspaces = payload.get("value") or []
    matches = [item for item in workspaces if str(item.get("name") or "").casefold() == requested.casefold()]
    if not matches:
        similar = [
            str(item.get("name"))
            for item in workspaces
            if requested.casefold() in str(item.get("name") or "").casefold()
        ][:8]
        suffix = f" Similar visible workspaces: {', '.join(similar)}" if similar else ""
        raise RuntimeError(
            f"Workspace '{requested}' is not visible to this identity.{suffix} "
            "Check workspace membership and service-principal tenant settings."
        )
    if len(matches) > 1:
        raise RuntimeError(f"More than one visible workspace is named '{requested}'; use the workspace GUID.")
    workspace = matches[0]
    workspace_id = str(workspace.get("id") or "")
    if workspace_id:
        try:
            details = _get_json(f"{POWER_BI_API}/groups/{workspace_id}", headers)
            workspace = {**workspace, **details}
        except Exception:
            # The list result is sufficient for the export test; details are diagnostic only.
            pass
    return workspace


def _resolve_report(headers: Dict[str, str], workspace_id: str, report_input: str) -> Dict[str, Any]:
    requested = report_input.strip()
    payload = _get_json(f"{POWER_BI_API}/groups/{workspace_id}/reports", headers)
    reports = payload.get("value") or []
    if _is_guid(requested):
        matches = [item for item in reports if str(item.get("id") or "").casefold() == requested.casefold()]
    else:
        matches = [item for item in reports if str(item.get("name") or "").casefold() == requested.casefold()]
    if not matches:
        similar = [
            str(item.get("name"))
            for item in reports
            if requested.casefold() in str(item.get("name") or "").casefold()
        ][:8]
        suffix = f" Similar visible reports: {', '.join(similar)}" if similar else ""
        raise RuntimeError(f"Report '{requested}' was not found in the selected workspace.{suffix}")
    if len(matches) > 1:
        raise RuntimeError(f"More than one report is named '{requested}'; use the report GUID.")
    return matches[0]


def _semantic_model_details(
    headers: Dict[str, str], workspace_id: str, dataset_id: str
) -> Tuple[Optional[bool], Dict[str, Any]]:
    if not dataset_id:
        return None, {}
    try:
        payload = _get_json(f"{POWER_BI_API}/groups/{workspace_id}/datasets", headers)
    except Exception:
        return None, {}
    for item in payload.get("value") or []:
        if str(item.get("id") or "").casefold() == dataset_id.casefold():
            return True, item
    return False, {}


def _download_modes(requested_mode: str) -> Sequence[str]:
    if requested_mode == "auto":
        return ("LiveConnect", "IncludeModel", "Default")
    return (requested_mode,)


def _safe_filename(value: str) -> str:
    clean = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or "")).strip(" .")
    return clean or "powerbi-report"


def _filename_from_response(response: requests.Response, fallback: str) -> str:
    disposition = str(response.headers.get("Content-Disposition") or "")
    encoded_match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    if encoded_match:
        return _safe_filename(unquote(encoded_match.group(1).strip()))
    quoted_match = re.search(r'filename="([^"]+)"', disposition, flags=re.IGNORECASE)
    if quoted_match:
        return _safe_filename(quoted_match.group(1).strip())
    plain_match = re.search(r"filename=([^;]+)", disposition, flags=re.IGNORECASE)
    if plain_match:
        return _safe_filename(plain_match.group(1).strip().strip('"'))
    return _safe_filename(fallback)


def _available_path(output_dir: Path, filename: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    number = 2
    while True:
        alternate = output_dir / f"{stem}-{number}{suffix}"
        if not alternate.exists():
            return alternate
        number += 1


def _diagnostic_hint(status_code: int, error_code: str, error_message: str) -> str:
    combined = f"{error_code} {error_message}".casefold()
    if "premiumfiles" in combined or "largesemanticmodel" in combined:
        return "Large semantic-model storage reports cannot be downloaded through this REST API."
    if status_code == 401:
        return "The token is expired, has the wrong Power BI audience, or was issued by the wrong tenant."
    if status_code == 403:
        return (
            "The identity normally needs at least Contributor access to the report workspace and, for a "
            "cross-workspace model, the semantic-model workspace. Also check the tenant report-download setting."
        )
    if status_code == 404:
        return "The report/workspace is not visible to this identity, or an app/virtual report ID was supplied."
    if status_code == 400:
        return (
            "The selected download mode or report/model state is unsupported. Common cases include Direct Lake or "
            "incremental refresh with IncludeModel, rebinding, template apps, usage metrics, and deployment/Git artifacts."
        )
    if status_code >= 500:
        return "Power BI rejected the export internally; inspect the error code and request ID before retrying."
    return "Inspect the Power BI error and request ID shown above."


def _download_report(
    headers: Dict[str, str],
    workspace_id: str,
    report_id: str,
    report_name: str,
    auth_mode: str,
    requested_mode: str,
    output_dir: Path,
) -> List[DownloadAttempt]:
    attempts: List[DownloadAttempt] = []
    endpoint = f"{POWER_BI_API}/groups/{workspace_id}/reports/{report_id}/Export"

    for mode in _download_modes(requested_mode):
        params: Dict[str, str] = {"preferClientRouting": "true"}
        if mode != "Default":
            params["downloadType"] = mode
        print(f"  Download attempt: {mode}")
        try:
            response = requests.get(endpoint, headers=headers, params=params, timeout=(30, 900), stream=True)
        except Exception as exc:
            message = _sanitize_error(exc, ())
            print(f"    [FAIL] Request error: {message}")
            attempts.append(DownloadAttempt(mode=mode, status_code=0, error_message=message))
            continue

        request_id = _request_id(response)
        if response.status_code != 200:
            error_code, error_message = _response_error(response)
            print(f"    [FAIL] HTTP {response.status_code} {error_code}: {error_message}")
            if request_id:
                print(f"    Request ID: {request_id}")
            print(f"    Reason: {_diagnostic_hint(response.status_code, error_code, error_message)}")
            attempts.append(
                DownloadAttempt(
                    mode=mode,
                    status_code=response.status_code,
                    error_code=error_code,
                    error_message=error_message,
                    request_id=request_id,
                )
            )
            response.close()
            continue

        extension = ".rdl" if "rdl" in str(response.headers.get("Content-Type") or "").casefold() else ".pbix"
        fallback_name = f"{report_name}-{auth_mode}-{mode}{extension}"
        filename = _filename_from_response(response, fallback_name)
        if not Path(filename).suffix:
            filename += extension
        output_dir.mkdir(parents=True, exist_ok=True)
        file_path = _available_path(output_dir, filename)
        byte_count = 0
        with file_path.open("wb") as output_file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output_file.write(chunk)
                    byte_count += len(chunk)
        response.close()
        print(f"    [PASS] Saved {byte_count:,} bytes to {file_path.resolve()}")
        attempts.append(
            DownloadAttempt(
                mode=mode,
                status_code=200,
                request_id=request_id,
                file_path=file_path,
                byte_count=byte_count,
            )
        )
        if requested_mode == "auto":
            break

    return attempts


def _fabric_http_error(response: requests.Response, operation: str) -> RuntimeError:
    error_code, error_message = _response_error(response)
    request_id = _request_id(response)
    detail = f"{operation} failed: HTTP {response.status_code} {error_code}: {error_message}".strip()
    if request_id:
        detail += f" (Request ID: {request_id})"
    return RuntimeError(detail)


def _retry_after_seconds(response: requests.Response, default: int = 5) -> int:
    try:
        return max(1, min(int(response.headers.get("Retry-After") or default), 30))
    except (TypeError, ValueError):
        return default


def _get_fabric_report_definition(
    headers: Dict[str, str], workspace_id: str, report_id: str, report_format: str
) -> Dict[str, Any]:
    endpoint = f"{FABRIC_API}/workspaces/{workspace_id}/reports/{report_id}/getDefinition"
    params: Dict[str, str] = {}
    if report_format.casefold() == "pbirlegacy":
        params["format"] = "PBIR-Legacy"
    elif report_format.casefold() == "pbir":
        params["format"] = "PBIR"

    response = requests.post(endpoint, headers=headers, params=params, timeout=60)
    if response.status_code == 200:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    if response.status_code != 202:
        raise _fabric_http_error(response, "Fabric Get Report Definition")

    operation_id = str(response.headers.get("x-ms-operation-id") or "").strip()
    state_url = str(response.headers.get("Location") or "").strip()
    if not state_url and operation_id:
        state_url = f"{FABRIC_API}/operations/{operation_id}"
    if not state_url:
        raise RuntimeError("Fabric accepted the definition request but returned no operation URL.")

    retry_seconds = _retry_after_seconds(response)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        time.sleep(retry_seconds)
        poll = requests.get(state_url, headers=headers, timeout=60)
        if poll.status_code not in (200, 202):
            raise _fabric_http_error(poll, "Fabric definition operation polling")

        try:
            operation = poll.json()
        except (ValueError, json.JSONDecodeError):
            operation = {}
        if isinstance(operation, dict) and isinstance(operation.get("definition"), dict):
            return operation

        status = str(operation.get("status") or "").casefold() if isinstance(operation, dict) else ""
        if status == "succeeded":
            result_url = str(poll.headers.get("Location") or "").strip()
            if not result_url or result_url.rstrip("/") == state_url.rstrip("/"):
                if not operation_id:
                    raise RuntimeError("Fabric completed the operation but returned no result URL.")
                result_url = f"{FABRIC_API}/operations/{operation_id}/result"
            result = requests.get(result_url, headers=headers, timeout=60)
            if result.status_code != 200:
                raise _fabric_http_error(result, "Fabric definition result retrieval")
            payload = result.json()
            return payload if isinstance(payload, dict) else {}
        if status in {"failed", "cancelled"}:
            error = operation.get("error") if isinstance(operation, dict) else None
            raise RuntimeError(f"Fabric definition operation {status}: {json.dumps(error or operation)}")

        retry_seconds = _retry_after_seconds(poll, retry_seconds)

    raise RuntimeError("Fabric Get Report Definition did not finish within 15 minutes.")


def _decode_definition_part(payload: str) -> bytes:
    encoded = str(payload or "").strip()
    encoded += "=" * (-len(encoded) % 4)
    return base64.b64decode(encoded, validate=True)


def _definition_part_path(raw_path: Any) -> str:
    normalized = str(raw_path or "").replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or (path.parts and path.parts[0].endswith(":")):
        raise RuntimeError(f"Fabric returned an unsafe definition part path: {raw_path!r}")
    return str(path)


def _save_report_definition(
    payload: Dict[str, Any], output_dir: Path, report_name: str, auth_mode: str
) -> Tuple[Path, int, str]:
    definition = payload.get("definition")
    if not isinstance(definition, dict):
        raise RuntimeError("Fabric response did not contain a report definition.")
    parts = definition.get("parts")
    if not isinstance(parts, list) or not parts:
        raise RuntimeError("Fabric report definition contained no parts.")

    decoded_parts: List[Tuple[str, bytes]] = []
    written_paths = set()
    for part in parts:
        if not isinstance(part, dict) or part.get("payloadType") != "InlineBase64":
            raise RuntimeError("Fabric returned an unsupported report definition part.")
        part_path = _definition_part_path(part.get("path"))
        if part_path in written_paths:
            raise RuntimeError(f"Fabric returned the definition part more than once: {part_path}")
        decoded_parts.append((part_path, _decode_definition_part(str(part.get("payload") or ""))))
        written_paths.add(part_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename(f"{report_name}-{auth_mode}-report-definition.zip")
    file_path = _available_path(output_dir, filename)
    with zipfile.ZipFile(file_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for part_path, part_bytes in decoded_parts:
            archive.writestr(part_path, part_bytes)

    return file_path, len(written_paths), str(definition.get("format") or "not returned")


def _download_report_definition(
    config: Dict[str, Any], auth_mode: str, workspace_id: str, report: Dict[str, Any], output_dir: Path
) -> Optional[Path]:
    print("  Definition fallback: Fabric Get Report Definition")
    fabric_token = ""
    try:
        fabric_token = _acquire_fabric_token(auth_mode, config)
        payload = _get_fabric_report_definition(
            _headers(fabric_token),
            workspace_id,
            str(report.get("id") or ""),
            str(report.get("format") or ""),
        )
        file_path, part_count, definition_format = _save_report_definition(
            payload, output_dir, str(report.get("name") or "powerbi-report"), auth_mode
        )
        print(f"    [PASS] Saved {part_count} definition part(s) to {file_path.resolve()}")
        print(f"    Definition format: {definition_format}")
        print("    Note: this ZIP contains report layout/visual metadata, not semantic-model data or a full PBIX.")
        return file_path
    except Exception as exc:
        print(f"    [FAIL] {_sanitize_error(exc, (fabric_token, str(config.get('client_secret') or '')))}")
        print(
            "    Requirement: Fabric Report.ReadWrite.All delegated consent and report read/write permission. "
            "The API is also blocked for reports with encrypted sensitivity labels."
        )
        return None
    finally:
        fabric_token = ""


def _test_identity(args: argparse.Namespace, auth_mode: str, output_dir: Path) -> str:
    print(f"\n=== {auth_mode} ===")
    access_token = ""
    protected_values: List[str] = []
    try:
        config = _load_config(auth_mode)
        protected_values.append(str(config.get("client_secret") or ""))
        access_token = _acquire_token(auth_mode, config)
        protected_values.append(access_token)
        headers = _headers(access_token)

        workspace = _resolve_workspace(headers, args.workspace)
        workspace_id = str(workspace.get("id") or "")
        workspace_name = str(workspace.get("name") or args.workspace)
        print(f"  Workspace: {workspace_name} ({workspace_id})")
        if workspace.get("isReadOnly") is True:
            print("  Workspace access: read-only (Viewer access is insufficient for report download)")
        elif workspace.get("isReadOnly") is False:
            print("  Workspace access: editable (consistent with Contributor or higher)")
        else:
            print("  Workspace access: editability was not returned by the API")
        if workspace.get("isOnDedicatedCapacity") is not None:
            capacity_mode = "dedicated capacity" if workspace.get("isOnDedicatedCapacity") else "shared capacity"
            print(f"  Workspace capacity: {capacity_mode}")

        report = _resolve_report(headers, workspace_id, args.report)
        report_id = str(report.get("id") or "")
        report_name = str(report.get("name") or args.report)
        dataset_id = str(report.get("datasetId") or "")
        print(f"  Report: {report_name} ({report_id})")
        print(f"  Report format: {report.get('format') or report.get('reportType') or 'not returned'}")
        print(f"  Is owned by identity: {report.get('isOwnedByMe', 'not returned')}")
        print(f"  Semantic model ID: {dataset_id or 'not returned'}")

        model_is_local, model = _semantic_model_details(headers, workspace_id, dataset_id)
        if model_is_local is True:
            print("  Semantic model location: same workspace")
            print(f"  Semantic model storage mode: {model.get('targetStorageMode') or 'not returned'}")
            print(f"  Semantic model provider: {model.get('ContentProviderType') or 'not returned'}")
        elif model_is_local is False:
            print(
                "  Semantic model location: not found in this workspace; this is likely a cross-workspace live connection."
            )
            print("  Permission note: Contributor access is also required on the semantic-model workspace.")
        else:
            print("  Semantic model location: could not be verified with this identity")

        attempts = _download_report(
            headers,
            workspace_id,
            report_id,
            report_name,
            auth_mode,
            args.download_type,
            output_dir,
        )
        if attempts and all(attempt.status_code == 403 for attempt in attempts):
            print("  Diagnostic conclusion:")
            if workspace.get("isReadOnly") is True:
                print("    The identity has read-only workspace access; assign Contributor, Member, or Admin.")
            elif str(model.get("targetStorageMode") or "").casefold() == "premiumfiles":
                print("    This model uses large semantic-model storage, which Microsoft excludes from REST PBIX download.")
            else:
                print(
                    "    The identity can resolve the report and local model, but Power BI forbids PBIX export. "
                    "If the tenant download policy is confirmed for this user, the remaining cause is a report/model "
                    "state that the PBIX Export API does not support (for example rebinding, deployment pipeline/Git, "
                    "a copied report, or another service-side artifact restriction)."
                )
        pbix_downloaded = any(attempt.status_code == 200 and attempt.file_path for attempt in attempts)
        if pbix_downloaded:
            return "PBIX"

        if not args.skip_definition_fallback:
            definition_path = _download_report_definition(config, auth_mode, workspace_id, report, output_dir)
            if definition_path:
                return "DEFINITION"
        return "FAIL"
    except KeyboardInterrupt:
        print("\n  [FAIL] Test cancelled.")
        return "FAIL"
    except Exception as exc:
        print(f"  [FAIL] {_sanitize_error(exc, protected_values)}")
        return "FAIL"
    finally:
        access_token = ""


def main() -> int:
    args = _parse_args()
    if args.config:
        config_path = os.path.abspath(os.path.expanduser(args.config))
        if not os.path.isfile(config_path):
            print(f"Configuration file not found: {config_path}", file=sys.stderr)
            return 2
        os.environ["PBI_AUTH_CONFIG_PATH"] = config_path

    output_dir = Path(args.output_dir).expanduser().resolve()
    modes = AUTH_MODES if args.auth_mode == "all" else (args.auth_mode,)

    print("Power BI automatic report download diagnostic")
    print(f"Workspace input: {args.workspace}")
    print(f"Report input: {args.report}")
    print(f"Download type: {args.download_type}")
    print(f"Output directory: {output_dir}")

    results = {mode: _test_identity(args, mode, output_dir) for mode in modes}
    print("\n=== Summary ===")
    for mode, result in results.items():
        if result == "PBIX":
            print(f"  PASS: {mode} (full PBIX/RDL downloaded)")
        elif result == "DEFINITION":
            print(f"  PASS: {mode} (report definition downloaded; full PBIX remains blocked)")
        else:
            print(f"  FAIL: {mode}")
    return 0 if all(result != "FAIL" for result in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
