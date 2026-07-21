"""
Utility configuration loader for the Power BI Lineage Streamlit app.

The main Streamlit app imports `Utils.validate_config(auth_mode)`.
This file lets the app read credentials from:
1. JSON config file, or
2. environment variables, or
3. Streamlit secrets.

Recommended local setup:
    Copy config/powerbi_auth_config.template.json to config/powerbi_auth_config.json
    Fill tenant_id and client_id for the organization-owned Entra App Registration.

Recommended production setup:
    Use Streamlit Community Cloud secrets. Do not commit filled config files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Union


class Utils:
    """Configuration helper expected by the Streamlit app."""

    DEFAULT_APP_SETTINGS: Dict[str, Any] = {
        "measure_definition": {
            "default_provider": "auto",
            "provider_order": ["snowflake_cortex", "openai"],
        },
        "snowflake_cortex": {
            "enabled": False,
            "function": "AI_COMPLETE",
            "model": "mistral-large2",
            "timeout_seconds": 120,
            "max_tokens": 900,
            "temperature": 0,
            "guardrails": False,
            "dax_expression_max_chars": 3000,
            "source_query_max_chars": 1200,
            "instructions": (
                "You are a Power BI semantic model analyst. Explain the selected measure lineage row "
                "in normal business English. Use only the provided measure name, DAX expression, semantic "
                "table names, dependency fields, and source lineage. Do not invent business meaning that is "
                "not present in the metadata. Include only: Definition, Business meaning, DAX logic, "
                "and Source lineage."
            ),
        },
        "openai_measure_definitions": {
            "enabled": False,
            "api_key": "",
            "endpoint": "https://api.openai.com/v1/responses",
            "model": "gpt-5-nano",
            "timeout_seconds": 90,
            "max_output_tokens": 900,
            "temperature": None,
            "dax_expression_max_chars": 3000,
            "source_query_max_chars": 1200,
            "instructions": (
                "You are a Power BI semantic model analyst. Explain the selected measure lineage row "
                "in normal business English. Use only the provided measure name, DAX expression, semantic "
                "table names, dependency fields, and source lineage. Do not invent business meaning that is "
                "not present in the metadata. Include only: Definition, Business meaning, DAX logic, "
                "and Source lineage."
            ),
        },
        "snowflake_lineage": {
            "enabled": False,
            "account": "",
            "user": "",
            "password": "",
            "authenticator": "snowflake",
            "role": "",
            "warehouse": "",
            "database": "",
            "schema": "",
            "direction": "UPSTREAM",
            "default_object_domain": "VIEW",
            "max_depth": 20,
            "statement_timeout_seconds": 120,
        }
    }

    @staticmethod
    def _candidate_config_paths() -> List[Path]:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent
        env_path = os.getenv("PBI_AUTH_CONFIG_PATH")
        paths: List[Path] = []
        if env_path:
            paths.append(Path(env_path).expanduser())
        paths.extend([
            Path.cwd() / "config" / "powerbi_auth_config.json",
            project_root / "config" / "powerbi_auth_config.json",
            script_dir / "powerbi_auth_config.json",
        ])
        unique_paths: List[Path] = []
        seen = set()
        for path in paths:
            marker = str(path.resolve())
            if marker not in seen:
                seen.add(marker)
                unique_paths.append(path)
        return unique_paths

    @staticmethod
    def _load_config_file() -> Dict[str, Any]:
        for path in Utils._candidate_config_paths():
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
        return {}

    @staticmethod
    def _candidate_app_settings_paths() -> List[Path]:
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent
        paths = [
            Path.cwd() / "config" / "app_settings.template.json",
            project_root / "config" / "app_settings.template.json",
            Path.cwd() / "config" / "app_settings.json",
            project_root / "config" / "app_settings.json",
            script_dir / "app_settings.json",
        ]
        unique_paths: List[Path] = []
        seen = set()
        for path in paths:
            marker = str(path.resolve())
            if marker not in seen:
                seen.add(marker)
                unique_paths.append(path)
        return unique_paths

    @staticmethod
    def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = Utils._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _plain_dict(value: Any) -> Any:
        """Convert Streamlit's secrets wrapper into normal Python containers."""
        if hasattr(value, "items"):
            return {str(k): Utils._plain_dict(v) for k, v in value.items()}
        if isinstance(value, list):
            return [Utils._plain_dict(v) for v in value]
        return value

    @staticmethod
    def _load_streamlit_secrets() -> Dict[str, Any]:
        try:
            import streamlit as st  # Imported lazily so utility tests can run outside Streamlit.

            return Utils._plain_dict(st.secrets)
        except Exception:
            return {}

    @staticmethod
    def load_app_settings() -> Dict[str, Any]:
        """Load shared non-Power-BI application settings from config/app_settings.json.

        Load order:
            1. Built-in safe defaults.
            2. config/app_settings.template.json, when present.
            3. config/app_settings.json, when present.

        The local app_settings.json file wins over the template.
        """
        settings = Utils._deep_merge({}, Utils.DEFAULT_APP_SETTINGS)
        for path in Utils._candidate_app_settings_paths():
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    settings = Utils._deep_merge(settings, json.load(f))
        secrets = Utils._load_streamlit_secrets()
        app_settings = secrets.get("app_settings")
        if isinstance(app_settings, dict):
            settings = Utils._deep_merge(settings, app_settings)
        for section in [
            "measure_definition",
            "snowflake_cortex",
            "openai_measure_definitions",
            "snowflake_lineage",
        ]:
            section_settings = secrets.get(section)
            if isinstance(section_settings, dict):
                settings = Utils._deep_merge(settings, {section: section_settings})
        return settings

    @staticmethod
    def _split_scopes(value: Union[str, List[str], None]) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        raw = str(value).replace(",", " ")
        return [item.strip() for item in raw.split() if item.strip()]

    @staticmethod
    def _env_config(auth_mode: str) -> Dict[str, Any]:
        # MasterUser is the primary supported mode in the latest app.
        prefix_map = {
            "masteruser": "PBI_MASTER_USER",
            "serviceprincipal": "PBI_SERVICE_PRINCIPAL",
            "serviceprincipal-admin": "PBI_SERVICE_PRINCIPAL_ADMIN",
        }
        prefix = prefix_map.get(auth_mode.lower(), "PBI_MASTER_USER")
        default_authority = "https://login.microsoftonline.com/organizations"
        return {
            "authenticate_mode": os.getenv(f"{prefix}_AUTHENTICATE_MODE", auth_mode),
            "tenant_id": os.getenv(f"{prefix}_TENANT_ID", os.getenv("PBI_TENANT_ID", "")),
            "client_id": os.getenv(f"{prefix}_CLIENT_ID", os.getenv("PBI_CLIENT_ID", "")),
            "client_secret": os.getenv(f"{prefix}_CLIENT_SECRET", os.getenv("PBI_CLIENT_SECRET", "")),
            "authority": os.getenv(f"{prefix}_AUTHORITY", os.getenv("PBI_AUTHORITY", default_authority)),
            "scope": Utils._split_scopes(
                os.getenv(
                    f"{prefix}_SCOPES",
                    os.getenv(
                        "PBI_SCOPES",
                        "https://analysis.windows.net/powerbi/api/App.Read.All "
                        "https://analysis.windows.net/powerbi/api/Report.Read.All "
                        "https://analysis.windows.net/powerbi/api/Dashboard.Read.All "
                        "https://analysis.windows.net/powerbi/api/Dataset.Read.All "
                        "https://analysis.windows.net/powerbi/api/Workspace.Read.All "
                        "https://analysis.windows.net/powerbi/api/Tenant.Read.All"
                    ),
                )
            ),
        }

    @staticmethod
    def _streamlit_auth_config(auth_mode: str) -> Dict[str, Any]:
        """Read optional auth config from [powerbi] Streamlit secrets sections."""
        secrets = Utils._load_streamlit_secrets()
        powerbi = secrets.get("powerbi")
        if not isinstance(powerbi, dict):
            return {}

        mode_key_map = {
            "masteruser": ["MasterUser", "masteruser", "master_user"],
            "serviceprincipal": ["ServicePrincipal", "serviceprincipal", "service_principal"],
            "serviceprincipal-admin": [
                "ServicePrincipal-Admin",
                "serviceprincipal-admin",
                "service_principal_admin",
            ],
        }
        merged: Dict[str, Any] = {}
        for key, value in powerbi.items():
            if not isinstance(value, dict):
                merged[key] = value
        for key in mode_key_map.get(auth_mode.lower(), [auth_mode]):
            value = powerbi.get(key)
            if isinstance(value, dict):
                merged = Utils._deep_merge(merged, value)
        return merged

    @staticmethod
    def validate_config(auth_mode: str) -> Union[Dict[str, Any], str]:
        """Return config expected by the app or an error string.

        Required keys returned:
            authenticate_mode, tenant_id, client_id, client_secret, authority, scope
        """
        file_config = Utils._load_config_file()
        mode_config = file_config.get(auth_mode) or file_config.get(auth_mode.lower()) or {}
        env_config = Utils._env_config(auth_mode)
        streamlit_config = Utils._streamlit_auth_config(auth_mode)

        # Environment/root secrets fill defaults, section secrets override them,
        # and a local JSON file wins for explicit local development.
        config = {**env_config, **streamlit_config, **mode_config}
        config["scope"] = Utils._split_scopes(config.get("scope"))

        missing = []
        for key in ["authenticate_mode", "tenant_id", "client_id", "authority", "scope"]:
            value = config.get(key)
            if value in (None, "", []):
                missing.append(key)

        if missing:
            return (
                f"Missing Power BI auth config for {auth_mode}: {', '.join(missing)}. "
                "Set environment variables or create config/powerbi_auth_config.json from the template."
            )

        return config
