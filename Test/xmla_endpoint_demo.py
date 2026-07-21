"""Standalone Power BI XMLA endpoint smoke test.

This script deliberately does not import ``streamlit_app.py`` or mutate app state.
It reuses the repository's credential loader and Windows ADO/MSOLAP connector.

Examples:
    python xmla_endpoint_demo.py --workspace "Finance" --dataset "Sales Model"
    python xmla_endpoint_demo.py --workspace "Finance" --dataset "Sales Model" \
        --auth-mode ServicePrincipal
    python xmla_endpoint_demo.py --workspace "Finance" --dataset "Sales Model" \
        --auth-mode all --probe connection,tables,measures

Credentials are loaded from the same ignored local JSON file, environment
variables, or Streamlit secrets used by the application. Tokens and client
secrets are never printed. All XMLA statements in this file are read-only.
"""

from __future__ import annotations

import argparse
import gc
import os
import platform
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlsplit, urlunsplit

import msal

from utils import Utils
from xmla_ado_com import connect_xmla


AUTH_MODES = ("MasterUser", "ServicePrincipal", "ServicePrincipal-Admin")
PROBE_NAMES = ("connection", "tables", "columns", "measures", "partitions", "dependencies")


@dataclass(frozen=True)
class Probe:
    name: str
    attempts: Sequence[str]


PROBES: Dict[str, Probe] = {
    "connection": Probe(
        "connection",
        (
            "SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS",
        ),
    ),
    "tables": Probe(
        "tables",
        (
            "SELECT [ID], [Name], [IsHidden] FROM $SYSTEM.TMSCHEMA_TABLES",
            "SELECT [ID], [Name] FROM $SYSTEM.TMSCHEMA_TABLES",
        ),
    ),
    "columns": Probe(
        "columns",
        (
            (
                "SELECT [ID], [TableID], [ExplicitName], [InferredName], "
                "[ExplicitDataType], [InferredDataType], [IsHidden], [SourceColumn], "
                "[Type], [Expression] FROM $SYSTEM.TMSCHEMA_COLUMNS"
            ),
            (
                "SELECT [ID], [TableID], [Name], [DataType], [IsHidden] "
                "FROM $SYSTEM.TMSCHEMA_COLUMNS"
            ),
        ),
    ),
    "measures": Probe(
        "measures",
        (
            (
                "SELECT [Name], [TableID], [Expression], [IsHidden] "
                "FROM $SYSTEM.TMSCHEMA_MEASURES"
            ),
            "SELECT [Name], [TableID], [Expression] FROM $SYSTEM.TMSCHEMA_MEASURES",
        ),
    ),
    "partitions": Probe(
        "partitions",
        (
            (
                "SELECT [TableID], [Name], [QueryDefinition], [SourceType] "
                "FROM $SYSTEM.TMSCHEMA_PARTITIONS"
            ),
            (
                "SELECT [TableID], [Name], [QueryDefinition] "
                "FROM $SYSTEM.TMSCHEMA_PARTITIONS"
            ),
        ),
    ),
    "dependencies": Probe(
        "dependencies",
        (
            (
                "SELECT [OBJECT_TYPE], [TABLE], [OBJECT], [EXPRESSION], "
                "[REFERENCED_OBJECT_TYPE], [REFERENCED_TABLE], [REFERENCED_OBJECT], "
                "[REFERENCED_EXPRESSION] FROM $SYSTEM.DISCOVER_CALC_DEPENDENCY"
            ),
            (
                "SELECT [OBJECT_TYPE], [TABLE], [OBJECT], [REFERENCED_OBJECT_TYPE], "
                "[REFERENCED_TABLE], [REFERENCED_OBJECT] "
                "FROM $SYSTEM.DISCOVER_CALC_DEPENDENCY"
            ),
        ),
    ),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test a Power BI workspace XMLA endpoint with read-only DMV queries.",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Power BI workspace name as shown in the service (not its GUID).",
    )
    parser.add_argument(
        "--dataset",
        required=True,
        help="Semantic model name used as the XMLA Initial Catalog (not its GUID).",
    )
    parser.add_argument(
        "--auth-mode",
        default="MasterUser",
        choices=(*AUTH_MODES, "all"),
        help="Configured identity to test. 'all' tests all three identities.",
    )
    parser.add_argument(
        "--probe",
        default="all",
        help=(
            "Comma-separated probes: connection,tables,columns,measures,partitions,dependencies. "
            "Default: all."
        ),
    )
    parser.add_argument(
        "--config",
        help="Optional path to a local Power BI auth JSON file. The file is read only.",
    )
    parser.add_argument(
        "--show-rows",
        action="store_true",
        help="Show sample metadata rows. Expressions can reveal model/source names.",
    )
    parser.add_argument(
        "--row-limit",
        type=int,
        default=5,
        help="Maximum sample rows shown per probe when --show-rows is used (default: 5).",
    )
    args = parser.parse_args()
    if not 1 <= args.row_limit <= 50:
        parser.error("--row-limit must be between 1 and 50")
    args.probes = _parse_probe_names(args.probe, parser)
    return args


def _parse_probe_names(raw_value: str, parser: argparse.ArgumentParser) -> List[str]:
    value = str(raw_value or "").strip().lower()
    if value == "all":
        return list(PROBE_NAMES)

    names: List[str] = []
    for item in value.split(","):
        name = item.strip()
        if not name:
            continue
        if name not in PROBES:
            parser.error(f"Unknown probe '{name}'. Choose from: {', '.join(PROBE_NAMES)}, all")
        if name not in names:
            names.append(name)
    if not names:
        parser.error("--probe must include at least one probe name")
    return names


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


def _access_token_env_names(auth_mode: str) -> Tuple[str, str]:
    mode_name = re.sub(r"[^A-Z0-9]+", "_", auth_mode.upper()).strip("_")
    return f"PBI_{mode_name}_ACCESS_TOKEN", "PBI_XMLA_ACCESS_TOKEN"


def _token_from_environment(auth_mode: str) -> Optional[str]:
    for variable_name in _access_token_env_names(auth_mode):
        value = str(os.getenv(variable_name) or "").strip()
        if value:
            return value.removeprefix("Bearer ").strip()
    return None


def _acquire_master_user_token(config: Dict[str, Any]) -> str:
    supplied_token = _token_from_environment("MasterUser")
    if supplied_token:
        print("  Authentication: using access token from an environment variable")
        return supplied_token

    tenant_id = str(config.get("tenant_id") or "").strip()
    client_id = str(config.get("client_id") or "").strip()
    authority = _tenant_authority(config.get("authority"), tenant_id)

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

    access_token = str((result or {}).get("access_token") or "").strip()
    if not access_token:
        detail = (result or {}).get("error_description") or (result or {}).get("error")
        raise RuntimeError(f"Token acquisition failed: {detail or 'access token was not returned'}")
    return access_token


def _xmla_urls(workspace_name: str) -> List[str]:
    raw_name = workspace_name.strip()
    raw_url = f"powerbi://api.powerbi.com/v1.0/myorg/{raw_name}"
    encoded_url = f"powerbi://api.powerbi.com/v1.0/myorg/{quote(raw_name, safe='')}"
    return list(dict.fromkeys((raw_url, encoded_url)))


def _ole_db_value(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _connection_string(
    workspace_url: str,
    dataset_name: str,
    password: str,
    user_id: Optional[str] = None,
) -> str:
    properties = [
        "Provider=MSOLAP",
        f"Data Source={_ole_db_value(workspace_url)}",
        f"Initial Catalog={_ole_db_value(dataset_name)}",
    ]
    if user_id:
        properties.append(f"User ID={_ole_db_value(user_id)}")
    properties.append(f"Password={_ole_db_value(password)}")
    return ";".join(properties) + ";"


def _sanitize_error(error: BaseException, protected_values: Iterable[str]) -> str:
    message = str(error)
    for protected in protected_values:
        secret = str(protected or "")
        if secret:
            message = message.replace(secret, "<redacted>")
    message = re.sub(r"(?i)(password|client_secret)\s*=\s*[^;\s]+", r"\1=<redacted>", message)
    message = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer <redacted>", message)
    return message


def _connect(
    workspace_name: str,
    dataset_name: str,
    password: str,
    user_id: Optional[str] = None,
):
    failures: List[str] = []
    for workspace_url in _xmla_urls(workspace_name):
        try:
            connection = connect_xmla(
                _connection_string(workspace_url, dataset_name, password, user_id=user_id)
            )
            return connection, connection.cursor(), workspace_url
        except Exception as exc:
            failures.append(_sanitize_error(exc, (password,)))
    detail = failures[-1] if failures else "No XMLA URL candidate was attempted."
    raise RuntimeError(f"XMLA connection failed: {detail}")


def _column_names(cursor: Any) -> List[str]:
    return [str(item[0]) for item in (cursor.description or []) if item]


def _preview_value(value: Any, max_length: int = 120) -> str:
    text = "<null>" if value is None else str(value).replace("\r", " ").replace("\n", " ")
    return text if len(text) <= max_length else text[: max_length - 3] + "..."


def _show_rows(columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int) -> None:
    if not rows:
        print("    Sample: no rows returned")
        return
    width = len(columns)
    print("    Sample columns: " + " | ".join(columns))
    for row in rows[:limit]:
        values = [_preview_value(row[index] if index < len(row) else None) for index in range(width)]
        print("    - " + " | ".join(values))


def _run_probe(cursor: Any, probe: Probe, show_rows: bool, row_limit: int) -> Tuple[bool, str]:
    errors: List[str] = []
    for attempt_number, query in enumerate(probe.attempts, start=1):
        try:
            cursor.execute(query)
            rows = cursor.fetchall()
            columns = _column_names(cursor)
            print(f"  [PASS] {probe.name}: {len(rows)} row(s), {len(columns)} column(s)")
            if attempt_number > 1:
                print(f"    Compatibility fallback used: query attempt {attempt_number}")
            if show_rows:
                _show_rows(columns, rows, row_limit)
            return True, ""
        except Exception as exc:
            errors.append(_sanitize_error(exc, ()))
    detail = errors[-1] if errors else "No query attempt was made."
    print(f"  [FAIL] {probe.name}: {detail}")
    return False, detail


def _load_config(auth_mode: str) -> Dict[str, Any]:
    result = Utils.validate_config(auth_mode)
    if isinstance(result, str):
        raise RuntimeError(result)
    return result


def _check_windows_dependencies() -> Tuple[bool, str]:
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except Exception:
        return False, "pywin32 is missing. Install project dependencies with: python -m pip install -r requirements.txt"

    ado_connection = None
    initialized = False
    try:
        pythoncom.CoInitialize()
        initialized = True
        ado_connection = win32com.client.Dispatch("ADODB.Connection")
        ado_connection.Provider = "MSOLAP"
        provider_name = str(ado_connection.Provider or "MSOLAP")
        return True, f"pywin32, ADO, and {provider_name} are available"
    except Exception as exc:
        return False, f"MSOLAP provider preflight failed: {_sanitize_error(exc, ())}"
    finally:
        ado_connection = None
        gc.collect()
        if initialized:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _test_identity(
    auth_mode: str,
    workspace_name: str,
    dataset_name: str,
    probe_names: Sequence[str],
    show_rows: bool,
    row_limit: int,
) -> bool:
    print(f"\n=== {auth_mode} ===")
    connection = None
    cursor = None
    password = ""
    user_id = None
    protected_values: List[str] = []
    try:
        config = _load_config(auth_mode)
        if auth_mode == "MasterUser":
            password = _acquire_master_user_token(config)
        else:
            tenant_id = str(config.get("tenant_id") or "").strip()
            client_id = str(config.get("client_id") or "").strip()
            password = str(config.get("client_secret") or "").strip()
            if not password:
                raise RuntimeError(f"{auth_mode} requires client_secret in the selected configuration.")
            user_id = f"app:{client_id}@{tenant_id}"
            print("  Authentication: XMLA service principal using application ID and client secret")

        protected_values.append(password)
        connection, cursor, workspace_url = _connect(
            workspace_name,
            dataset_name,
            password,
            user_id=user_id,
        )
        print(f"  Endpoint: {workspace_url}")
        print(f"  Initial catalog: {dataset_name}")

        passed = True
        for probe_name in probe_names:
            probe_passed, _ = _run_probe(cursor, PROBES[probe_name], show_rows, row_limit)
            passed = probe_passed and passed
        return passed
    except KeyboardInterrupt:
        print("\n  [FAIL] Test cancelled.")
        return False
    except Exception as exc:
        print(f"  [FAIL] {_sanitize_error(exc, protected_values)}")
        return False
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        password = ""


def main() -> int:
    args = _parse_args()
    if args.config:
        config_path = os.path.abspath(os.path.expanduser(args.config))
        if not os.path.isfile(config_path):
            print(f"Configuration file not found: {config_path}", file=sys.stderr)
            return 2
        os.environ["PBI_AUTH_CONFIG_PATH"] = config_path

    print("Power BI XMLA endpoint smoke test")
    print(f"Runtime: {platform.system()} {platform.release()} | Python {platform.python_version()}")
    print(f"Workspace: {args.workspace}")
    print(f"Semantic model: {args.dataset}")
    print(f"Probes: {', '.join(args.probes)}")

    if platform.system() != "Windows":
        print("[FAIL] This connector requires Windows, pywin32, and the MSOLAP provider.")
        return 2

    dependencies_ready, dependency_message = _check_windows_dependencies()
    print(f"[{'PASS' if dependencies_ready else 'FAIL'}] Dependency preflight: {dependency_message}")
    if not dependencies_ready:
        return 2

    modes = AUTH_MODES if args.auth_mode == "all" else (args.auth_mode,)
    results = {
        mode: _test_identity(
            mode,
            args.workspace,
            args.dataset,
            args.probes,
            args.show_rows,
            args.row_limit,
        )
        for mode in modes
    }

    print("\n=== Summary ===")
    for mode, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}: {mode}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
