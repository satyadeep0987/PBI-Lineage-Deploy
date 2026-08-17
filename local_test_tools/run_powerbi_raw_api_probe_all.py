"""Run every direct Power BI raw probe function and store local JSON outputs.

This runner is intentionally separate from powerbi_raw_api_probe.py so the probe
file stays simple: one direct function per API. The runner discovers IDs from
earlier calls when explicit IDs are not supplied, writes one JSON file per
function, and writes an index.json summary.

Tokens are redacted in saved token-function outputs. API response bodies are
stored raw as returned by Power BI/Fabric/XMLA.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import powerbi_raw_api_probe as probe


TOKEN_KEYS = {
    "access_token",
    "authorization_header",
    "id_token",
    "refresh_token",
    "client_secret",
    "password",
    "secret",
}


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


def _redact_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if str(key).casefold() in TOKEN_KEYS:
                cleaned[str(key)] = "<redacted>" if item else item
            else:
                cleaned[str(key)] = _redact_tokens(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_tokens(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer <redacted>", value)
    return value


def _clean_token(token_or_header: str) -> str:
    return re.sub(r"^Bearer\s+", "", str(token_or_header or "").strip(), flags=re.IGNORECASE)


def _env_first(names: Sequence[str]) -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2, ensure_ascii=False), encoding="utf-8")


def _safe_filename(name: str, index: int) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    return f"{index:03d}_{clean or 'output'}.json"


def _first_value(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _first_record(response: Mapping[str, Any]) -> Dict[str, Any]:
    values = response.get("json", {}).get("value") if isinstance(response.get("json"), dict) else None
    if isinstance(values, list) and values:
        first = values[0]
        return first if isinstance(first, dict) else {}
    return {}


def _records(response: Mapping[str, Any]) -> List[Dict[str, Any]]:
    values = response.get("json", {}).get("value") if isinstance(response.get("json"), dict) else None
    return [item for item in values or [] if isinstance(item, dict)]


def _pick_id(explicit_value: str, records: Iterable[Mapping[str, Any]]) -> str:
    if explicit_value:
        return explicit_value
    for record in records:
        value = str(record.get("id") or "").strip()
        if value:
            return value
    return ""


def _pick_name(explicit_value: str, records: Iterable[Mapping[str, Any]]) -> str:
    if explicit_value:
        return explicit_value
    for record in records:
        value = str(record.get("name") or record.get("displayName") or "").strip()
        if value:
            return value
    return ""


def _scan_workspaces(scan_result: Any) -> List[Dict[str, Any]]:
    if not isinstance(scan_result, dict):
        return []
    scan_json = scan_result.get("json")
    if not isinstance(scan_json, dict):
        return []
    return [item for item in scan_json.get("workspaces") or [] if isinstance(item, dict)]


def _scan_dashboards(scan_result: Any) -> List[Dict[str, Any]]:
    dashboards: List[Dict[str, Any]] = []
    for workspace in _scan_workspaces(scan_result):
        dashboards.extend(
            item for item in workspace.get("dashboards") or [] if isinstance(item, dict)
        )
    return dashboards


class RawRun:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.index = 0
        self.entries: List[Dict[str, Any]] = []

    def save(
        self,
        function_name: str,
        arguments: Mapping[str, Any],
        output: Any = None,
        status: str = "called",
        reason: str = "",
        redact: bool = False,
    ) -> Any:
        self.index += 1
        safe_args = _redact_tokens(dict(arguments))
        safe_output = _redact_tokens(output) if redact else output
        payload = {
            "function": function_name,
            "status": status,
            "reason": reason,
            "arguments": safe_args,
            "output": safe_output,
        }
        file_name = _safe_filename(function_name, self.index)
        file_path = self.output_dir / file_name
        _write_json(file_path, payload)
        self.entries.append(
            {
                "function": function_name,
                "status": status,
                "reason": reason,
                "file": str(file_path),
            }
        )
        return output

    def call(
        self,
        function_name: str,
        func: Callable[..., Any],
        arguments: Mapping[str, Any],
        *,
        redact: bool = False,
        skip_reason: str = "",
    ) -> Any:
        if skip_reason:
            return self.save(function_name, arguments, None, status="skipped", reason=skip_reason, redact=redact)
        try:
            output = func(**dict(arguments))
            return self.save(function_name, arguments, output, status="called", redact=redact)
        except Exception as error:
            return self.save(
                function_name,
                arguments,
                {"error": str(error), "error_type": type(error).__name__},
                status="error",
                redact=True,
            )

    def finish(self, context: Mapping[str, Any]) -> Path:
        summary = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "output_dir": str(self.output_dir),
            "context": _redact_tokens(dict(context)),
            "entries": self.entries,
            "counts": {
                "called": sum(1 for item in self.entries if item["status"] == "called"),
                "skipped": sum(1 for item in self.entries if item["status"] == "skipped"),
                "error": sum(1 for item in self.entries if item["status"] == "error"),
            },
        }
        index_path = self.output_dir / "index.json"
        _write_json(index_path, summary)
        return index_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all local raw Power BI/Fabric/XMLA probe functions.")
    parser.add_argument("--powerbi-token", default="", help="Existing Power BI bearer token.")
    parser.add_argument("--fabric-token", default="", help="Existing Fabric bearer token for getDefinition.")
    parser.add_argument("--powerbi-auth-mode", default="ServicePrincipal-Admin")
    parser.add_argument("--fabric-auth-mode", default="ServicePrincipal-Admin")
    parser.add_argument("--allow-device-code", action="store_true", help="Allow interactive device-code auth if no token/SP token works.")
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--workspace-name", default="")
    parser.add_argument("--report-id", default="")
    parser.add_argument("--dataset-id", default="")
    parser.add_argument("--dataset-name", default="")
    parser.add_argument("--dashboard-id", default="")
    parser.add_argument("--app-id", default="")
    parser.add_argument("--metadata-base-url", default="")
    parser.add_argument("--app-identifier", default="")
    parser.add_argument("--include-binary-export", action="store_true")
    parser.add_argument("--max-scanner-polls", type=int, default=8)
    parser.add_argument("--max-fabric-polls", type=int, default=20)
    parser.add_argument("--output-root", default="local_test_tools/raw_api_outputs")
    return parser.parse_args()


def _token_from_args_or_env(argument_value: str, env_names: Sequence[str]) -> str:
    return _clean_token(argument_value or _env_first(env_names))


def _service_principal_token(run: RawRun, function_name: str, auth_mode: str, audience: str) -> str:
    output = run.call(
        function_name,
        probe.get_token_service_principal,
        {"auth_mode": auth_mode, "audience": audience},
        redact=True,
    )
    return str(output.get("access_token") or "").strip() if isinstance(output, dict) else ""


def _is_master_user_mode(auth_mode: str) -> bool:
    value = str(auth_mode or "").replace("-", "").replace("_", "").casefold()
    return "masteruser" in value


def _device_code_token(run: RawRun, auth_mode: str, audience: str) -> str:
    token_function = probe.get_token_delegated_device_code
    function_name = "get_token_delegated_device_code"
    try:
        config = probe.load_auth_config(auth_mode)
        if config.get("client_secret"):
            token_function = probe.get_token_confidential_device_code
            function_name = "get_token_confidential_device_code"
    except Exception:
        pass

    output = run.call(
        function_name,
        token_function,
        {"auth_mode": auth_mode, "audience": audience},
        redact=True,
    )
    return str(output.get("access_token") or "").strip() if isinstance(output, dict) else ""


def _get_powerbi_token(run: RawRun, args: argparse.Namespace) -> str:
    token = _token_from_args_or_env(
        args.powerbi_token,
        [
            "PBI_RAW_POWERBI_TOKEN",
            "PBI_ACCESS_TOKEN",
            "PBI_DOWNLOAD_ACCESS_TOKEN",
            "PBI_XMLA_ACCESS_TOKEN",
        ],
    )
    if token:
        run.call(
            "get_token_existing_bearer",
            probe.get_token_existing_bearer,
            {"existing_bearer_token": token},
            redact=True,
        )
        return token

    if args.allow_device_code and _is_master_user_mode(args.powerbi_auth_mode):
        token = _device_code_token(run, args.powerbi_auth_mode, "powerbi")
        return token

    token = _service_principal_token(run, "get_token_service_principal_powerbi", args.powerbi_auth_mode, "powerbi")
    if token:
        return token

    if args.allow_device_code:
        return _device_code_token(run, args.powerbi_auth_mode, "powerbi")

    return ""


def _get_fabric_token(run: RawRun, args: argparse.Namespace, powerbi_token: str) -> str:
    token = _token_from_args_or_env(
        args.fabric_token,
        [
            "PBI_RAW_FABRIC_TOKEN",
            "PBI_FABRIC_ACCESS_TOKEN",
        ],
    )
    if token:
        run.call(
            "get_token_existing_bearer_fabric",
            probe.get_token_existing_bearer,
            {"existing_bearer_token": token},
            redact=True,
        )
        return token

    if args.allow_device_code and _is_master_user_mode(args.fabric_auth_mode):
        token = _device_code_token(run, args.fabric_auth_mode, "fabric")
        return token

    token = _service_principal_token(run, "get_token_service_principal_fabric", args.fabric_auth_mode, "fabric")
    if token:
        return token

    if args.allow_device_code:
        token = _device_code_token(run, args.fabric_auth_mode, "fabric")
        if token:
            return token

    return powerbi_token


def main() -> int:
    args = _parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (PROJECT_ROOT / args.output_root / timestamp).resolve()
    run = RawRun(output_dir)

    powerbi_token = _get_powerbi_token(run, args)
    if not powerbi_token:
        run.save(
            "powerbi_api_calls",
            {},
            None,
            status="skipped",
            reason="No Power BI token was available. Provide --powerbi-token, PBI_RAW_POWERBI_TOKEN, or working service-principal config.",
        )
        index_path = run.finish({"output_dir": str(output_dir)})
        print(index_path)
        return 2

    fabric_token = _get_fabric_token(run, args, powerbi_token)

    workspaces = run.call("get_workspaces", probe.get_workspaces, {"access_token": powerbi_token})
    workspace_records = _records(workspaces) if isinstance(workspaces, dict) else []
    workspace_id = _pick_id(args.workspace_id, workspace_records)
    workspace_name = _pick_name(args.workspace_name, workspace_records)

    workspace = run.call(
        "get_workspace",
        probe.get_workspace,
        {"access_token": powerbi_token, "workspace_id": workspace_id},
        skip_reason="" if workspace_id else "No workspace_id was supplied or discovered.",
    )
    if isinstance(workspace, dict) and isinstance(workspace.get("json"), dict):
        workspace_name = workspace_name or str(workspace["json"].get("name") or "")

    reports = run.call(
        "get_workspace_reports",
        probe.get_workspace_reports,
        {"access_token": powerbi_token, "workspace_id": workspace_id},
        skip_reason="" if workspace_id else "No workspace_id was supplied or discovered.",
    )
    report_records = _records(reports) if isinstance(reports, dict) else []
    report_id = _pick_id(args.report_id, report_records)
    report = _first_record(reports) if isinstance(reports, dict) else {}
    dataset_id_from_report = str(report.get("datasetId") or "").strip()

    dashboards = run.call(
        "get_workspace_dashboards",
        probe.get_workspace_dashboards,
        {"access_token": powerbi_token, "workspace_id": workspace_id},
        skip_reason="" if workspace_id else "No workspace_id was supplied or discovered.",
    )
    dashboard_records = _records(dashboards) if isinstance(dashboards, dict) else []
    dashboard_id = _pick_id(args.dashboard_id, dashboard_records)

    datasets = run.call(
        "get_workspace_datasets",
        probe.get_workspace_datasets,
        {"access_token": powerbi_token, "workspace_id": workspace_id},
        skip_reason="" if workspace_id else "No workspace_id was supplied or discovered.",
    )
    dataset_records = _records(datasets) if isinstance(datasets, dict) else []
    dataset_id = args.dataset_id or dataset_id_from_report or _pick_id("", dataset_records)
    dataset_name = args.dataset_name or _pick_name("", [item for item in dataset_records if str(item.get("id")) == dataset_id]) or _pick_name("", dataset_records)

    run.call(
        "get_workspace_report",
        probe.get_workspace_report,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "report_id": report_id},
        skip_reason="" if workspace_id and report_id else "workspace_id/report_id missing.",
    )
    run.call(
        "get_workspace_dataset",
        probe.get_workspace_dataset,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "dataset_id": dataset_id},
        skip_reason="" if workspace_id and dataset_id else "workspace_id/dataset_id missing.",
    )
    run.call(
        "get_workspace_users",
        probe.get_workspace_users,
        {"access_token": powerbi_token, "workspace_id": workspace_id},
        skip_reason="" if workspace_id else "workspace_id missing.",
    )
    run.call(
        "get_workspace_report_users",
        probe.get_workspace_report_users,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "report_id": report_id},
        skip_reason="" if workspace_id and report_id else "workspace_id/report_id missing.",
    )
    admin_apps = run.call("get_admin_apps", probe.get_admin_apps, {"access_token": powerbi_token, "top": 50})
    app_records = _records(admin_apps) if isinstance(admin_apps, dict) else []
    app_id = args.app_id or _pick_id("", app_records) or str((_first_record(admin_apps) or {}).get("appId") or "")

    run.call(
        "get_app_reports",
        probe.get_app_reports,
        {"access_token": powerbi_token, "app_id": app_id},
        skip_reason="" if app_id else "app_id missing.",
    )
    run.call(
        "get_app_dashboards",
        probe.get_app_dashboards,
        {"access_token": powerbi_token, "app_id": app_id},
        skip_reason="" if app_id else "app_id missing.",
    )
    run.call(
        "get_admin_app_users",
        probe.get_admin_app_users,
        {"access_token": powerbi_token, "app_id": app_id},
        skip_reason="" if app_id else "app_id missing.",
    )
    run.call(
        "get_admin_report_users",
        probe.get_admin_report_users,
        {"access_token": powerbi_token, "report_id": report_id},
        skip_reason="" if report_id else "report_id missing.",
    )
    run.call("get_admin_groups", probe.get_admin_groups, {"access_token": powerbi_token, "filter_expr": f"id eq '{workspace_id}'" if workspace_id else None})
    run.call("get_admin_reports", probe.get_admin_reports, {"access_token": powerbi_token, "filter_expr": f"id eq '{report_id}'" if report_id else None})
    run.call("get_admin_datasets", probe.get_admin_datasets, {"access_token": powerbi_token, "filter_expr": f"id eq '{dataset_id}'" if dataset_id else None})

    run.call(
        "post_dataset_execute_queries",
        probe.post_dataset_execute_queries,
        {
            "access_token": powerbi_token,
            "dataset_id": dataset_id,
            "query": "SELECT [TABLE_NAME] FROM $SYSTEM.DBSCHEMA_TABLES WHERE [TABLE_TYPE] = 'TABLE'",
        },
        skip_reason="" if dataset_id else "dataset_id missing.",
    )
    run.call(
        "post_dataset_table_names_query",
        probe.post_dataset_table_names_query,
        {"access_token": powerbi_token, "dataset_id": dataset_id},
        skip_reason="" if dataset_id else "dataset_id missing.",
    )
    run.call(
        "post_dataset_measure_names_query",
        probe.post_dataset_measure_names_query,
        {"access_token": powerbi_token, "dataset_id": dataset_id},
        skip_reason="" if dataset_id else "dataset_id missing.",
    )
    run.call(
        "post_dataset_measure_details_query",
        probe.post_dataset_measure_details_query,
        {"access_token": powerbi_token, "dataset_id": dataset_id},
        skip_reason="" if dataset_id else "dataset_id missing.",
    )
    run.call(
        "get_report_pages",
        probe.get_report_pages,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "report_id": report_id},
        skip_reason="" if workspace_id and report_id else "workspace_id/report_id missing.",
    )
    run.call(
        "export_report",
        probe.export_report,
        {
            "access_token": powerbi_token,
            "workspace_id": workspace_id,
            "report_id": report_id,
            "download_type": "LiveConnect",
            "include_binary_body": bool(args.include_binary_export),
        },
        skip_reason="" if workspace_id and report_id else "workspace_id/report_id missing.",
    )

    scanner_post = run.call(
        "post_admin_workspace_get_info",
        probe.post_admin_workspace_get_info,
        {"access_token": powerbi_token, "workspace_ids": [workspace_id], "lineage": True},
        skip_reason="" if workspace_id else "workspace_id missing.",
    )
    scan_id = str(scanner_post.get("json", {}).get("id") or "").strip() if isinstance(scanner_post, dict) and isinstance(scanner_post.get("json"), dict) else ""
    run.call(
        "get_admin_workspace_scan_status",
        probe.get_admin_workspace_scan_status,
        {"access_token": powerbi_token, "scan_id": scan_id},
        skip_reason="" if scan_id else "scan_id missing from post_admin_workspace_get_info.",
    )
    direct_scan_result = run.call(
        "get_admin_workspace_scan_result",
        probe.get_admin_workspace_scan_result,
        {"access_token": powerbi_token, "scan_id": scan_id},
        skip_reason="" if scan_id else "scan_id missing from post_admin_workspace_get_info.",
    )
    if not dashboard_id:
        dashboard_id = _pick_id("", _scan_dashboards(direct_scan_result))
    run.call(
        "run_admin_workspace_scanner",
        probe.run_admin_workspace_scanner,
        {"access_token": powerbi_token, "workspace_ids": [workspace_id], "max_poll_attempts": args.max_scanner_polls},
        skip_reason="" if workspace_id else "workspace_id missing.",
    )

    run.call(
        "get_workspace_dashboard_users",
        probe.get_workspace_dashboard_users,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "dashboard_id": dashboard_id},
        skip_reason="" if workspace_id and dashboard_id else "workspace_id/dashboard_id missing.",
    )
    run.call(
        "get_dashboard_tiles",
        probe.get_dashboard_tiles,
        {"access_token": powerbi_token, "workspace_id": workspace_id, "dashboard_id": dashboard_id},
        skip_reason="" if workspace_id and dashboard_id else "workspace_id/dashboard_id missing.",
    )
    run.call(
        "get_app_dashboard_tiles",
        probe.get_app_dashboard_tiles,
        {"access_token": powerbi_token, "app_id": app_id, "dashboard_id": dashboard_id},
        skip_reason="" if app_id and dashboard_id else "app_id/dashboard_id missing.",
    )
    run.call(
        "get_admin_dashboard_users",
        probe.get_admin_dashboard_users,
        {"access_token": powerbi_token, "dashboard_id": dashboard_id},
        skip_reason="" if dashboard_id else "dashboard_id missing.",
    )
    run.call("get_admin_dashboards", probe.get_admin_dashboards, {"access_token": powerbi_token, "filter_expr": f"id eq '{dashboard_id}'" if dashboard_id else None})

    fabric_post = run.call(
        "post_fabric_report_get_definition",
        probe.post_fabric_report_get_definition,
        {"access_token": fabric_token, "workspace_id": workspace_id, "report_id": report_id, "report_format": "pbir"},
        skip_reason="" if fabric_token and workspace_id and report_id else "fabric_token/workspace_id/report_id missing.",
    )
    fabric_operation_headers = fabric_post.get("headers") if isinstance(fabric_post, dict) and isinstance(fabric_post.get("headers"), dict) else {}
    fabric_operation_id = str(fabric_operation_headers.get("x-ms-operation-id") or "").strip()
    fabric_operation_url = str(fabric_operation_headers.get("Location") or "").strip()
    fabric_operation_argument = fabric_operation_url or fabric_operation_id
    fabric_operation = run.call(
        "get_fabric_operation",
        probe.get_fabric_operation,
        {"access_token": fabric_token, "operation_id_or_url": fabric_operation_argument},
        skip_reason="" if fabric_token and fabric_operation_argument else "operation_id is available only from a 202 Fabric getDefinition response.",
    )
    operation_headers = fabric_operation.get("headers") if isinstance(fabric_operation, dict) and isinstance(fabric_operation.get("headers"), dict) else {}
    operation_result_url = str(operation_headers.get("Location") or "").strip()
    if (
        not operation_result_url
        and fabric_operation_id
        and isinstance(fabric_operation, dict)
        and isinstance(fabric_operation.get("json"), dict)
        and str(fabric_operation["json"].get("status") or "").casefold() == "succeeded"
    ):
        operation_result_url = f"https://api.fabric.microsoft.com/v1/operations/{fabric_operation_id}/result"
    run.call(
        "get_fabric_operation_result",
        probe.get_fabric_operation_result,
        {"access_token": fabric_token, "operation_id_or_url": operation_result_url},
        skip_reason="" if fabric_token and operation_result_url else "operation result URL is available only after a completed Fabric operation.",
    )
    run.call(
        "run_fabric_report_get_definition",
        probe.run_fabric_report_get_definition,
        {
            "access_token": fabric_token,
            "workspace_id": workspace_id,
            "report_id": report_id,
            "report_format": "pbir",
            "max_poll_attempts": args.max_fabric_polls,
        },
        skip_reason="" if fabric_token and workspace_id and report_id else "fabric_token/workspace_id/report_id missing.",
    )

    report_url = str(_first_value(report, ["webUrl"]) or _first_value(report, ["embedUrl"]) or "")
    run.call(
        "get_powerbi_web_page",
        probe.get_powerbi_web_page,
        {"access_token": powerbi_token, "url": report_url},
        skip_reason="" if report_url else "report webUrl/embedUrl missing.",
    )
    run.call(
        "get_internal_app_audience_metadata",
        probe.get_internal_app_audience_metadata,
        {"access_token": powerbi_token, "metadata_base_url": args.metadata_base_url, "app_identifier": args.app_identifier or app_id},
        skip_reason="" if args.metadata_base_url and (args.app_identifier or app_id) else "metadata_base_url and app identifier are required.",
    )

    xmla_arguments = {
        "access_token": powerbi_token,
        "workspace_name": workspace_name,
        "dataset_name": dataset_name,
    }
    xmla_skip = "" if workspace_name and dataset_name else "workspace_name/dataset_name missing."
    run.call("execute_xmla_dmv", probe.execute_xmla_dmv, {**xmla_arguments, "query": "SELECT [CATALOG_NAME] FROM $SYSTEM.DBSCHEMA_CATALOGS"}, skip_reason=xmla_skip)
    run.call("xmla_catalogs", probe.xmla_catalogs, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_tables", probe.xmla_tables, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_columns", probe.xmla_columns, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_measures", probe.xmla_measures, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_partitions", probe.xmla_partitions, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_expressions", probe.xmla_expressions, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_relationships", probe.xmla_relationships, xmla_arguments, skip_reason=xmla_skip)
    run.call("xmla_calc_dependency", probe.xmla_calc_dependency, xmla_arguments, skip_reason=xmla_skip)

    context = {
        "workspace_id": workspace_id,
        "workspace_name": workspace_name,
        "report_id": report_id,
        "dataset_id": dataset_id,
        "dataset_name": dataset_name,
        "dashboard_id": dashboard_id,
        "app_id": app_id,
        "output_dir": str(output_dir),
    }
    index_path = run.finish(context)
    print(index_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
