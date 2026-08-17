"""Independent, session-only connection controls for the application sidebar."""

from __future__ import annotations

import copy
from typing import Any, Dict

import msal
import requests
import streamlit as st

from pbi_modules.setup_controller import (
    MANAGED_AGENT_FIELDS,
    SETUP_STATE_KEY,
    SetupAuthenticationError,
    SetupValidationError,
    authenticate_powerbi_interactive,
    claude_is_ready,
    configure_claude,
    configure_powerbi,
    configure_powerbi_scope,
    configure_snowflake,
    connect_snowflake_external_browser,
    disconnect_powerbi,
    disconnect_snowflake,
    ensure_setup_state,
    legacy_auth_bundle,
    powerbi_is_ready,
    redact_text,
    secret_values,
    set_claude_api_key,
    snowflake_is_ready,
    validate_powerbi_targets,
)


def _runtime_state() -> Dict[str, Any]:
    state = ensure_setup_state(st.session_state.get(SETUP_STATE_KEY))
    st.session_state[SETUP_STATE_KEY] = state
    return state


def purge_connection_derived_state(*, preserve_auth_bundle: bool) -> None:
    """Remove provider-derived UI/data state while preserving live providers.

    This deliberately uses a tiny allowlist. It prevents metadata, lineage,
    uploads, PowerAI evidence, and selection state from crossing identities or
    Snowflake database sessions.
    """
    setup_state = st.session_state.get(SETUP_STATE_KEY)
    auth_bundle = st.session_state.get("auth_bundle") if preserve_auth_bundle else None
    st.session_state.clear()
    if isinstance(setup_state, dict):
        st.session_state[SETUP_STATE_KEY] = setup_state
    st.session_state.auth_bundle = auth_bundle
    st.session_state.workflow_mode = "landing"


def _remove_msal_accounts() -> None:
    bundle = st.session_state.get("auth_bundle") or {}
    client = bundle.get("clientapp_mu") if isinstance(bundle, dict) else None
    if client is not None:
        try:
            for account in client.get_accounts() or []:
                client.remove_account(account)
        except Exception:
            pass


def _connection_message(status: Dict[str, Any]) -> None:
    state = str(status.get("state") or "not_configured")
    message = str(status.get("message") or "Not connected.")
    if state == "ready":
        st.success(message, icon=":material/check_circle:")
    elif state in {"error", "denied", "timed_out"}:
        st.warning(message, icon=":material/warning:")
    else:
        st.caption(message)


def _render_powerbi_login(state: Dict[str, Any]) -> None:
    ready = powerbi_is_ready(state)
    public = ((state.get("public") or {}).get("powerbi") or {})
    status = ((state.get("status") or {}).get("powerbi") or {})
    with st.expander("1. Power BI login", expanded=not ready, icon=":material/analytics:"):
        _connection_message(status)
        with st.form("sidebar_powerbi_login_form"):
            tenant_id = st.text_input(
                "Tenant ID *",
                value=str(public.get("tenant_id") or ""),
                placeholder="00000000-0000-0000-0000-000000000000",
                disabled=ready,
            )
            client_id = st.text_input(
                "Client ID *",
                value=str(public.get("client_id") or ""),
                placeholder="00000000-0000-0000-0000-000000000000",
                disabled=ready,
            )
            connect = st.form_submit_button(
                "Connected" if ready else "Connect Power BI",
                type="primary",
                use_container_width=True,
                disabled=ready,
            )
        st.caption(
            "Only Tenant ID and Client ID are mandatory. Browser sign-in opens on "
            "the machine running Streamlit."
        )

        if connect:
            try:
                configure_powerbi(
                    state,
                    {"tenant_id": tenant_id, "client_id": client_id},
                )
                with st.spinner("Opening Microsoft sign-in and validating Power BI..."):
                    authenticate_powerbi_interactive(state, msal)
                    validate_powerbi_targets(state, requests)
                    st.session_state.auth_bundle = legacy_auth_bundle(state, msal)
                purge_connection_derived_state(preserve_auth_bundle=True)
                st.rerun()
            except (SetupValidationError, SetupAuthenticationError) as exc:
                disconnect_powerbi(state, keep_configuration=True)
                st.session_state.auth_bundle = None
                purge_connection_derived_state(preserve_auth_bundle=False)
                st.error(redact_text(str(exc), secret_values(state)))
            except Exception:
                disconnect_powerbi(state, keep_configuration=True)
                st.session_state.auth_bundle = None
                purge_connection_derived_state(preserve_auth_bundle=False)
                st.error("Power BI connection could not be completed. Verify tenant access and retry.")

        # A validation failure is terminal for the issued token, but surface a
        # stable error state instead of leaving the panel stuck at Authenticating.
        if not ready and str(status.get("state") or "") == "authenticated":
            state["status"]["powerbi"] = {
                "state": "error",
                "message": "Power BI access validation did not complete. Retry the connection.",
            }

        if ready and st.button(
            "Disconnect Power BI",
            key="sidebar_disconnect_powerbi",
            icon=":material/link_off:",
            use_container_width=True,
        ):
            _remove_msal_accounts()
            disconnect_powerbi(state, keep_configuration=True)
            purge_connection_derived_state(preserve_auth_bundle=False)
            st.rerun()


def _render_optional_powerbi_scope(state: Dict[str, Any]) -> None:
    public = ((state.get("public") or {}).get("powerbi") or {})
    pbi_configured = bool(public.get("tenant_id") and public.get("client_id"))
    with st.expander(
        "Power BI scope (optional)",
        expanded=False,
        icon=":material/filter_alt:",
    ):
        st.caption(
            "Leave both blank to discover accessible workspace reports. "
            "A Workspace ID alone limits inventory to that workspace."
        )
        with st.form("sidebar_powerbi_scope_form"):
            workspace_id = st.text_input(
                "Workspace ID",
                value=str(public.get("workspace_id") or ""),
                placeholder="Optional workspace UUID",
                disabled=not pbi_configured,
            )
            report_ids = st.text_area(
                "Report IDs",
                value="\n".join(str(item) for item in public.get("report_ids") or []),
                placeholder="Optional; one UUID per line",
                height=84,
                disabled=not pbi_configured,
            )
            apply_scope = st.form_submit_button(
                "Apply optional scope",
                use_container_width=True,
                disabled=not pbi_configured,
            )
        if apply_scope:
            previous_scope = copy.deepcopy(public)
            try:
                configure_powerbi_scope(state, workspace_id, report_ids)
                credentials = ((state.get("credentials") or {}).get("powerbi") or {})
                if credentials.get("access_token"):
                    with st.spinner("Validating the optional Power BI scope..."):
                        validate_powerbi_targets(state, requests)
                        st.session_state.auth_bundle = legacy_auth_bundle(state, msal)
                purge_connection_derived_state(preserve_auth_bundle=True)
                st.rerun()
            except (SetupValidationError, SetupAuthenticationError) as exc:
                state.setdefault("public", {})["powerbi"] = previous_scope
                if previous_scope.get("validated_scope"):
                    state.setdefault("status", {})["powerbi"] = {
                        "state": "ready",
                        "message": "Connected. The previous optional scope remains active.",
                    }
                st.error(redact_text(str(exc), secret_values(state)))


def _render_snowflake_login(state: Dict[str, Any]) -> None:
    ready = snowflake_is_ready(state)
    public = ((state.get("public") or {}).get("snowflake") or {})
    status = ((state.get("status") or {}).get("snowflake") or {})
    with st.expander(
        "2. Snowflake database",
        expanded=not ready,
        icon=":material/database:",
    ):
        _connection_message(status)
        with st.form("sidebar_snowflake_login_form"):
            account = st.text_input(
                "Account Identifier *",
                value=str(public.get("account") or ""),
                placeholder="myorg-myaccount",
                disabled=ready,
            )
            database = st.text_input(
                "Database *",
                value=str(public.get("database") or ""),
                placeholder="ANALYTICS",
                disabled=ready,
            )
            warehouse = st.text_input(
                "Warehouse *",
                value=str(public.get("warehouse") or ""),
                placeholder="LINEAGE_WH",
                disabled=ready,
            )
            role = st.text_input(
                "Role *",
                value=str(public.get("role") or ""),
                placeholder="LINEAGE_READER",
                disabled=ready,
            )
            user = st.text_input(
                "SSO Login Name *",
                value=str(public.get("user_override") or ""),
                placeholder="reader@example.com",
                disabled=ready,
                help="A non-secret Snowflake LOGIN_NAME used by browser SSO.",
            )
            read_only = st.checkbox(
                "Dedicated read-only role",
                value=ready,
                disabled=ready,
            )
            connect = st.form_submit_button(
                "Connected" if ready else "Connect Snowflake",
                type="primary",
                use_container_width=True,
                disabled=ready,
            )
        st.caption(
            "Browser SSO opens on the machine running Streamlit; no Snowflake password is collected."
        )

        if connect:
            try:
                configure_snowflake(
                    state,
                    {
                        "account": account,
                        "database": database,
                        "warehouse": warehouse,
                        "role": role,
                        "user": user,
                        "read_only_confirmed": read_only,
                    },
                )
                with st.spinner("Opening Snowflake SSO and validating the database context..."):
                    connect_snowflake_external_browser(state)
                purge_connection_derived_state(preserve_auth_bundle=True)
                st.rerun()
            except (SetupValidationError, SetupAuthenticationError) as exc:
                purge_connection_derived_state(preserve_auth_bundle=True)
                st.error(redact_text(str(exc), secret_values(state)))
            except Exception:
                purge_connection_derived_state(preserve_auth_bundle=True)
                st.error("Snowflake connection could not be completed. Verify SSO access and retry.")

        if ready and st.button(
            "Disconnect Snowflake",
            key="sidebar_disconnect_snowflake",
            icon=":material/link_off:",
            use_container_width=True,
        ):
            disconnect_snowflake(state, keep_configuration=True)
            purge_connection_derived_state(preserve_auth_bundle=True)
            st.rerun()


def _render_powerai_optional(state: Dict[str, Any]) -> None:
    public = ((state.get("public") or {}).get("claude") or {})
    ready = claude_is_ready(state)
    with st.expander("PowerAI (optional)", expanded=False, icon=":material/auto_awesome:"):
        if ready:
            st.success("PowerAI is configured for this session.")
            st.text_input(
                "Agent mode",
                value=str(public.get("mode") or "single").replace("_", " ").title(),
                disabled=True,
            )
            if st.button(
                "Disable PowerAI",
                key="sidebar_disable_powerai",
                use_container_width=True,
            ):
                configure_claude(state, {"mode": "disabled"})
                st.rerun()
            return

        mode_label = st.selectbox(
            "Agent mode",
            ("Disabled", "Single agent", "Managed multi-agent"),
            index={"disabled": 0, "single": 1, "managed_multi": 2}.get(
                str(public.get("mode") or "disabled"), 0
            ),
            key="sidebar_powerai_mode",
        )
        mode = {
            "Disabled": "disabled",
            "Single agent": "single",
            "Managed multi-agent": "managed_multi",
        }[mode_label]
        with st.form("sidebar_powerai_form", clear_on_submit=True):
            api_key = st.text_input(
                "Claude API Key",
                type="password",
                disabled=mode == "disabled",
            )
            environment_id = ""
            agent_values: Dict[str, str] = {}
            if mode == "managed_multi":
                environment_id = st.text_input("Managed Agent Environment ID")
                for field in MANAGED_AGENT_FIELDS:
                    agent_values[field] = st.text_input(field.replace("_", " ").title())
            save = st.form_submit_button("Save PowerAI", use_container_width=True)
        if save:
            try:
                configure_claude(
                    state,
                    {
                        "mode": mode,
                        "environment_id": environment_id,
                        **agent_values,
                    },
                )
                if mode != "disabled":
                    set_claude_api_key(state, api_key)
                st.rerun()
            except SetupValidationError as exc:
                st.error(redact_text(str(exc), secret_values(state)))


def render_connection_sidebar() -> Dict[str, Any]:
    """Render independent provider panels and return the current runtime state."""
    state = _runtime_state()
    st.markdown('<div class="lineage-nav-label">Connections</div>', unsafe_allow_html=True)
    _render_powerbi_login(state)
    _render_optional_powerbi_scope(state)
    _render_snowflake_login(state)
    _render_powerai_optional(state)
    return state
