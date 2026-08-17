import html
import hashlib
import time

import streamlit as st

from pbi_modules.setup_controller import (
    SETUP_STATE_KEY,
    powerbi_is_ready,
    powerbi_scope_allows,
    powerbi_scope_fingerprint,
    snowflake_is_ready,
    validated_powerbi_report,
)
from pbi_modules.connection_sidebar import render_connection_sidebar


_EXCLUDED_WORKSPACE_NAMES = frozenset({"admin monitoring"})
_WORKFLOW_QUERY_PARAM = "workflow"
_SHARED_APP_CACHE_CLEAR_REQUEST_KEY = "_clear_shared_lineage_cache_v1"
_VALID_WORKFLOW_MODES = {
    "landing",
    "guided",
    "report_lineage",
    "table_impact",
    "measure_impact",
}


def _query_param_value(name):
    """Return one normalized Streamlit query-param value."""
    try:
        value = st.query_params.get(name)
    except Exception:
        return ""
    if isinstance(value, list):
        value = value[-1] if value else ""
    return str(value or "").strip()


def _workflow_params(mode, context=None):
    """Build normalized workflow query parameters."""
    safe_mode = str(mode or "").strip()
    if safe_mode not in _VALID_WORKFLOW_MODES:
        safe_mode = "landing"

    params = {_WORKFLOW_QUERY_PARAM: safe_mode}
    if safe_mode == "report_lineage" and isinstance(context, dict):
        report_id = str(context.get("Report ID") or "").strip()
        workspace_id = str(
            context.get("Report Workspace ID")
            or context.get("Workspace ID")
            or context.get("Target Workspace ID")
            or ""
        ).strip()
        dataset_id = str(
            context.get("Dataset ID")
            or context.get("Primary Dataset ID")
            or ""
        ).strip()
        dataset_workspace_id = str(
            context.get("Semantic Model Workspace ID")
            or context.get("Dataset Workspace ID")
            or ""
        ).strip()
        if report_id:
            params["report_id"] = report_id
        if workspace_id:
            params["workspace_id"] = workspace_id
        if dataset_id:
            params["dataset_id"] = dataset_id
        if dataset_workspace_id:
            params["dataset_workspace_id"] = dataset_workspace_id
        for source_key, query_key in (
            ("Source Report", "report_name"),
            ("Report Name", "report_name"),
            ("Workspace", "workspace_name"),
            ("Workspace Name", "workspace_name"),
            ("Report Type", "report_type"),
            ("Report Format", "report_format"),
        ):
            value = str(context.get(source_key) or "").strip()
            if value and query_key not in params:
                params[query_key] = value

    return params


def _replace_workflow_query_params(mode, context=None):
    """Keep the browser URL aligned with the current app workflow."""
    try:
        st.query_params.clear()
        st.query_params.update(_workflow_params(mode, context))
    except Exception:
        return


def apply_workflow_query_params():
    """Hydrate workflow/report context from a shareable app URL."""
    requested_mode = _query_param_value(_WORKFLOW_QUERY_PARAM)
    if requested_mode not in _VALID_WORKFLOW_MODES:
        return

    st.session_state.workflow_mode = requested_mode
    if requested_mode != "report_lineage":
        return

    report_id = _query_param_value("report_id")
    if not report_id:
        return

    workspace_id = _query_param_value("workspace_id")
    setup_state = st.session_state.get(SETUP_STATE_KEY)
    if isinstance(setup_state, dict) and not powerbi_scope_allows(
        setup_state,
        workspace_id,
        report_id,
    ):
        st.session_state.workflow_mode = "landing"
        st.session_state["powerbi_scope_warning_v1"] = (
            "The requested report is outside this session's configured Power BI scope."
        )
        _replace_workflow_query_params("landing")
        return

    record = validated_powerbi_report(setup_state, workspace_id, report_id)
    if record is None:
        # With no explicit report allowlist, accept deep links only after this
        # signed-in session has fetched the report from Power BI inventory.
        for candidate in st.session_state.get("direct_lookup_report_records_v3") or []:
            if not isinstance(candidate, dict):
                continue
            if (
                str(candidate.get("Workspace ID") or "").strip().casefold()
                == workspace_id.casefold()
                and str(candidate.get("Report ID") or "").strip().casefold()
                == report_id.casefold()
            ):
                record = dict(candidate)
                break
    if record is None:
        st.session_state.workflow_mode = "landing"
        st.session_state["powerbi_scope_warning_v1"] = (
            "The requested report has not been validated for this runtime session."
        )
        _replace_workflow_query_params("landing")
        return
    context = direct_report_context(record)
    existing = st.session_state.get("direct_measure_active_context")
    if not isinstance(existing, dict) or str(existing.get("Report ID") or "") != report_id:
        st.session_state.direct_measure_active_context = context
        _remember_recent_report(record)


def filter_excluded_workspaces(workspaces):
    """Remove workspaces excluded from this application's lineage inventory."""
    return [
        workspace
        for workspace in workspaces or []
        if isinstance(workspace, dict)
        and str(workspace.get("name") or "").strip().casefold()
        not in _EXCLUDED_WORKSPACE_NAMES
    ]


def _set_workflow(mode):
    st.session_state.workflow_mode = mode
    _replace_workflow_query_params(mode)
    st.rerun()


def _remember_recent_report(record):
    if not isinstance(record, dict):
        return
    report_id = str(record.get("Report ID") or "").strip()
    workspace_id = str(record.get("Workspace ID") or "").strip()
    if not report_id:
        return

    recent = list(st.session_state.get("recent_lineage_reports") or [])
    recent = [
        item for item in recent
        if not (
            str(item.get("Report ID") or "").strip() == report_id
            and str(item.get("Workspace ID") or "").strip() == workspace_id
        )
    ]
    recent.insert(0, dict(record))
    st.session_state.recent_lineage_reports = recent[:8]


def _activate_direct_report(record):
    st.session_state.direct_measure_active_context = direct_report_context(record)
    _remember_recent_report(record)
    st.session_state.workflow_mode = "report_lineage"
    _replace_workflow_query_params("report_lineage", st.session_state.direct_measure_active_context)
    st.rerun()


def check_authenticated_session(logout_and_clear_session):
    """Clear expired auth before any authenticated page renders."""
    bundle = st.session_state.get("auth_bundle")
    if not bundle:
        return False

    expiry = bundle.get("expires_at", 0)
    remaining = int(expiry - time.time())
    if remaining <= 0:
        logout_and_clear_session()
        st.warning("Your Power BI session expired. Sign in again to continue.")
        st.rerun()
    return True


def render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, mode_label=None):
    """Render persistent authenticated navigation in the left sidebar."""
    normalized_mode = str(mode_label or "").strip().casefold()
    destination_aliases = {
        "home": "landing",
        "guided workflow": "guided",
        "explore": "guided",
        "direct measure lookup": "report_lineage",
        "report lineage": "report_lineage",
        "table impact": "table_impact",
        "measure impact": "measure_impact",
        "claude agent": "landing",
    }
    active_destination = destination_aliases.get(normalized_mode, "guided")

    with st.sidebar:
        st.markdown(
            """
            <div class="lineage-sidebar-brand">
                <div class="lineage-sidebar-mark">PBI</div>
                <div class="lineage-sidebar-copy">
                    <div class="lineage-sidebar-title">Lineage</div>
                    <div class="lineage-sidebar-caption">Explorer</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        setup_state = render_connection_sidebar()
        powerbi_connected = powerbi_is_ready(setup_state)
        snowflake_connected = snowflake_is_ready(setup_state)

        st.markdown('<div class="lineage-nav-label">Navigation</div>', unsafe_allow_html=True)

        report_context = st.session_state.get("direct_measure_active_context")
        if not isinstance(report_context, dict):
            report_context = None
        nav_items = [
            ("Home", "landing", "home", None),
            ("Explore", "guided", "explore", None),
            ("Report Lineage", "report_lineage", "account_tree", report_context),
            ("Table Impact", "table_impact", "table_view", None),
            ("Measure Impact", "measure_impact", "functions", None),
        ]
        for label, mode, icon, context in nav_items:
            if st.button(
                label,
                key=f"sidebar_nav_{mode}",
                icon=f":material/{icon}:",
                type="primary" if active_destination == mode else "secondary",
                use_container_width=True,
                disabled=mode != "landing" and not powerbi_connected,
            ):
                st.session_state.workflow_mode = mode
                _replace_workflow_query_params(mode, context)
                st.rerun()

        st.markdown(
            f"""
            <div class="lineage-sidebar-separator"></div>
            <div class="lineage-sidebar-status">
                <span class="app-status-dot" style="background:{'#16a34a' if powerbi_connected else '#94a3b8'};box-shadow:{'0 0 0 3px #dcfce7' if powerbi_connected else '0 0 0 3px #e2e8f0'}"></span>
                <span>
                    <strong>Power BI {'connected' if powerbi_connected else 'not connected'}</strong>
                    <small>Microsoft browser session</small>
                </span>
            </div>
            <div class="lineage-sidebar-status">
                <span class="app-status-dot" style="background:{'#16a34a' if snowflake_connected else '#94a3b8'};box-shadow:{'0 0 0 3px #dcfce7' if snowflake_connected else '0 0 0 3px #e2e8f0'}"></span>
                <span>
                    <strong>Snowflake {'connected' if snowflake_connected else 'not connected'}</strong>
                    <small>Database browser SSO</small>
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.button(
            "Clear session",
            key="top_logout",
            icon=":material/logout:",
            use_container_width=True,
        ):
            with st.spinner("Releasing tokens and clearing session cache..."):
                try:
                    logout_and_clear_session()
                except Exception as e:
                    st.error(f"Error releasing tokens: {e}")
                    clear_streamlit_session_state(keep_auth=False)
                st.rerun()


def _inventory_summary_html(workspaces, reports):
    dataset_count = len({str(row.get("Dataset ID")) for row in reports if row.get("Dataset ID")})
    return f"""
    <div class="inventory-metric-grid">
        <div class="inventory-metric accent-blue">
            <span>Workspaces</span>
            <strong>{len(workspaces)}</strong>
            <small>Accessible containers</small>
        </div>
        <div class="inventory-metric accent-green">
            <span>Reports</span>
            <strong>{len(reports)}</strong>
            <small>Available for exploration</small>
        </div>
        <div class="inventory-metric accent-coral">
            <span>Semantic models</span>
            <strong>{dataset_count}</strong>
            <small>Distinct report datasets</small>
        </div>
    </div>
    """


def _report_card(record, badge="Report"):
    report_name = html.escape(str(record.get("Report Name") or "Unnamed report"))
    workspace_name = html.escape(str(record.get("Workspace Name") or "Unknown workspace"))
    dataset_id = html.escape(str(record.get("Dataset ID") or "No dataset ID"))
    return f"""
    <div class="report-row-card">
        <div class="report-row-icon">R</div>
        <div class="report-row-copy">
            <strong>{report_name}</strong>
            <span>{workspace_name} | {dataset_id}</span>
        </div>
        <span class="report-row-badge">{html.escape(badge)}</span>
    </div>
    """


def _auth_cache_fingerprint(headers):
    """Fingerprint the bearer token without exposing it in cached data."""
    auth_value = ""
    if isinstance(headers, dict):
        auth_value = str(headers.get("Authorization") or "").strip()
    if auth_value.lower().startswith("bearer "):
        auth_value = auth_value[7:].strip()
    if not auth_value:
        return "anonymous"
    return hashlib.sha256(auth_value.encode("utf-8")).hexdigest()


@st.cache_data(show_spinner=False, ttl=60 * 60)
def _load_accessible_inventory_cached(
    token_fingerprint,
    scope_fingerprint,
    _headers,
    _get_workspace_inventory,
    _get_artifacts,
):
    """Process-wide inventory cache shared by browser tabs for the same token."""
    workspaces = filter_excluded_workspaces(_get_workspace_inventory(_headers) or [])
    reports = []
    for workspace in workspaces:
        workspace_id = workspace.get("id")
        workspace_name = workspace.get("name")
        if not workspace_id:
            continue
        for report in _get_artifacts(_headers, workspace_id, "report") or []:
            reports.append({
                "Workspace Name": workspace_name,
                "Workspace ID": workspace_id,
                "Report Name": report.get("name"),
                "Report ID": report.get("id"),
                "Dataset ID": report.get("datasetId"),
                "Dataset Workspace ID": report.get("datasetWorkspaceId"),
                "Report Type": report.get("reportType"),
                "Report Format": report.get("format"),
                "Embed URL": report.get("embedUrl"),
            })

    reports.sort(key=lambda row: (
        str(row.get("Report Name") or "").lower(),
        str(row.get("Workspace Name") or "").lower(),
    ))
    return {"workspaces": workspaces, "reports": reports}


def _clear_accessible_inventory_cache():
    """Clear process-wide inventory cache after an explicit user refresh."""
    try:
        _load_accessible_inventory_cached.clear()
    except Exception:
        return


def get_accessible_inventory(headers, get_workspace_inventory, get_artifacts):
    """Load the allowed workspace/report inventory used throughout the application."""
    cache_key = "accessible_lineage_inventory_v4"
    provenance_key = "accessible_lineage_inventory_provenance_v1"
    provenance = "|".join(
        [
            _auth_cache_fingerprint(headers),
            powerbi_scope_fingerprint(st.session_state.get(SETUP_STATE_KEY) or {}),
        ]
    )
    cached = st.session_state.get(cache_key)
    if (
        isinstance(cached, dict)
        and st.session_state.get(provenance_key) == provenance
    ):
        return cached

    inventory = _load_accessible_inventory_cached(
        provenance.split("|", 1)[0],
        provenance.split("|", 1)[1],
        headers,
        get_workspace_inventory,
        get_artifacts,
    )
    st.session_state[cache_key] = inventory
    st.session_state[provenance_key] = provenance
    st.session_state["direct_lookup_report_records_v3"] = inventory.get("reports") or []
    return inventory


def render_workflow_choice_page(
    headers,
    *,
    get_workspace_inventory,
    get_artifacts,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    """Render the authenticated search-first lineage home."""
    render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, "Home")
    powerbi_connected = powerbi_is_ready(st.session_state.get(SETUP_STATE_KEY))
    scope_warning = st.session_state.pop("powerbi_scope_warning_v1", None)
    if scope_warning:
        st.warning(str(scope_warning))
    if powerbi_connected:
        with st.spinner("Loading your Power BI inventory..."):
            inventory = get_accessible_inventory(headers, get_workspace_inventory, get_artifacts)
    else:
        inventory = {"workspaces": [], "reports": []}
    workspaces = inventory.get("workspaces") or []
    reports = inventory.get("reports") or []

    st.markdown(
        """
        <div class="home-hero">
            <div class="page-eyebrow">Power BI data estate</div>
            <h1>What would you like to explore?</h1>
            <p>Find a report, inspect its semantic model, and trace measures and source data from one workspace.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if not powerbi_connected:
        st.info(
            "The Home page is ready. Connect Power BI from the sidebar to load inventory "
            "and enable lineage navigation. Snowflake can be connected separately."
        )

    search_space, search_col, search_action, search_tail = st.columns([0.8, 5.8, 1.2, 0.8], vertical_alignment="bottom")
    with search_col:
        selected_report_index = st.selectbox(
            "Search reports",
            options=list(range(len(reports))),
            index=None,
            format_func=lambda index: direct_lookup_report_label(reports[index]),
            placeholder="Type a report name, workspace, or dataset ID",
            key="home_report_search",
            label_visibility="collapsed",
        ) if reports else None
    with search_action:
        if st.button(
            "Search",
            type="primary",
            use_container_width=True,
            disabled=not powerbi_connected or not reports,
        ):
            if selected_report_index is None:
                st.warning("Select a report from the search results.")
            else:
                _activate_direct_report(reports[selected_report_index])

    st.markdown(_inventory_summary_html(workspaces, reports), unsafe_allow_html=True)

    section_title_col, refresh_col = st.columns([5, 1], vertical_alignment="center")
    with section_title_col:
        st.markdown('<div class="section-heading"><strong>Quick actions</strong><span>Start with the task you need.</span></div>', unsafe_allow_html=True)
    with refresh_col:
        if st.button(
            "Refresh inventory",
            use_container_width=True,
            disabled=not powerbi_connected,
        ):
            _clear_accessible_inventory_cache()
            st.session_state[_SHARED_APP_CACHE_CLEAR_REQUEST_KEY] = True
            st.session_state.pop("accessible_lineage_inventory_v4", None)
            st.session_state.pop("direct_lookup_report_records_v3", None)
            st.session_state.pop("table_impact_analysis_result", None)
            st.session_state.pop("measure_impact_analysis_result", None)
            st.rerun()

    quick_actions = [
        ("01", "Explore a report", "Workspace, report, semantic objects, and lineage", "Explore", "guided"),
        ("02", "Report lineage", "Definitions, DAX logic, and visual lineage", "Open lineage", "report_lineage"),
    ]
    action_layout = st.columns([0.75, 1.5, 1.5, 0.75])
    for column, (number, title, copy, button_label, mode) in zip(action_layout[1:3], quick_actions):
        with column:
            with st.container(border=True):
                st.markdown(
                    f"""
                    <div class="quick-action-copy">
                        <span class="quick-action-number">{number}</span>
                        <strong>{html.escape(title)}</strong>
                        <p>{html.escape(copy)}</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
                if st.button(
                    button_label,
                    key=f"home_action_{number}",
                    use_container_width=True,
                    disabled=not powerbi_connected,
                ):
                    _set_workflow(mode)

    st.markdown('<div class="section-heading report-section-heading"><strong>Accessible reports</strong><span>Open a report directly in report lineage analysis.</span></div>', unsafe_allow_html=True)
    if not reports:
        st.info(
            "Connect Power BI from the sidebar to load accessible reports."
            if not powerbi_connected
            else "No accessible reports were returned for this account."
        )
    else:
        report_columns = st.columns(2)
        for index, record in enumerate(reports[:6]):
            with report_columns[index % 2]:
                with st.container(border=True):
                    st.markdown(_report_card(record), unsafe_allow_html=True)
                    if st.button(
                        "Open report",
                        key=f"home_accessible_report_{record.get('Workspace ID')}_{record.get('Report ID')}",
                        use_container_width=True,
                    ):
                        _activate_direct_report(record)

    recent_reports = list(st.session_state.get("recent_lineage_reports") or [])
    if recent_reports:
        st.markdown('<div class="section-heading report-section-heading"><strong>Recently viewed</strong><span>Your latest report analysis in this session.</span></div>', unsafe_allow_html=True)
        recent_columns = st.columns(2)
        for index, record in enumerate(recent_reports[:4]):
            with recent_columns[index % 2]:
                with st.container(border=True):
                    st.markdown(_report_card(record, badge="Recent"), unsafe_allow_html=True)
                    if st.button(
                        "Reopen report",
                        key=f"home_recent_report_{record.get('Workspace ID')}_{record.get('Report ID')}",
                        use_container_width=True,
                    ):
                        _activate_direct_report(record)


def direct_lookup_report_label(record):
    return " | ".join(
        str(value)
        for value in [
            record.get("Workspace Name"),
            record.get("Report Name"),
            record.get("Dataset ID"),
        ]
        if value
    )


def get_direct_lookup_report_records(headers, get_workspace_inventory, get_artifacts):
    """Return report records visible through the signed-in user's workspaces."""
    cache_key = "direct_lookup_report_records_v3"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    records = get_accessible_inventory(headers, get_workspace_inventory, get_artifacts).get("reports") or []
    st.session_state[cache_key] = records
    return records


def direct_report_context(record):
    label = direct_lookup_report_label(record)
    report_workspace_id = record.get("Report Workspace ID") or record.get("Workspace ID")
    semantic_workspace_id = (
        record.get("Semantic Model Workspace ID")
        or record.get("Dataset Workspace ID")
        or record.get("datasetWorkspaceId")
        or report_workspace_id
    )
    has_explicit_model_workspace = bool(
        record.get("Semantic Model Workspace ID")
        or record.get("Dataset Workspace ID")
        or record.get("datasetWorkspaceId")
    )
    return {
        "Context Key": label,
        "Scope Type": "Workspace",
        "Container Name": record.get("Workspace Name"),
        "Workspace": record.get("Workspace Name"),
        "App Name": "N/A",
        "Source Report": record.get("Report Name"),
        "Report ID": record.get("Report ID"),
        "Dataset ID": record.get("Dataset ID"),
        "Primary Dataset ID": record.get("Primary Dataset ID") or record.get("Dataset ID"),
        "Report Workspace ID": report_workspace_id,
        "Report Workspace": record.get("Report Workspace") or record.get("Workspace Name"),
        "Semantic Model Workspace ID": semantic_workspace_id,
        "Semantic Workspace Name": (
            record.get("Semantic Workspace Name")
            or record.get("Semantic Model Workspace")
            or (record.get("Workspace Name") if semantic_workspace_id == report_workspace_id else None)
        ),
        # Backward-compatible alias. Model/XMLA code should prefer
        # Semantic Model Workspace ID and report-definition code should use
        # Report Workspace ID.
        "Target Workspace ID": semantic_workspace_id,
        "Semantic Workspace Resolution": (
            record.get("Semantic Workspace Resolution")
            or ("Report inventory" if has_explicit_model_workspace else "Report workspace fallback")
        ),
        "Model Role": record.get("Model Role") or "Primary",
        "Lineage Depth": record.get("Lineage Depth", 0),
        "Report Type": record.get("Report Type"),
        "Report Format": record.get("Report Format"),
    }


def render_direct_measure_lookup_page(
    headersSPA,
    headersSP,
    headersMU,
    *,
    get_workspace_inventory,
    get_artifacts,
    render_source_db_lineage_view,
    render_semantic_model_objects_view,
    render_measure_source_lineage_view,
    render_report_layout_view,
    render_report_details_export,
    render_visual_source_lookup_view,
    safe_widget_key,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    """Search a report and render its report, semantic, and visual lineage."""
    render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, "Report Lineage")
    st.markdown(
        """
        <div class="page-header">
            <div class="page-eyebrow">Report Analysis</div>
            <h2>Report Lineage</h2>
            <p>Search the reports visible to the signed-in account, then inspect source data, semantic objects, measures, and visual-level lineage.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    refresh_col, _ = st.columns([1, 4])
    with refresh_col:
        if st.button("Refresh reports", use_container_width=True):
            _clear_accessible_inventory_cache()
            st.session_state[_SHARED_APP_CACHE_CLEAR_REQUEST_KEY] = True
            st.session_state.pop("accessible_lineage_inventory_v4", None)
            st.session_state.pop("direct_lookup_report_records_v3", None)
            st.session_state.pop("direct_measure_active_context", None)
            st.rerun()

    with st.spinner("Loading accessible reports..."):
        records = get_direct_lookup_report_records(headersSP, get_workspace_inventory, get_artifacts)

    if not records:
        st.info("No reports were returned for the signed-in user.")
        return

    xmla_token = st.session_state.auth_bundle["spa"]

    with st.container(border=True):
        selected_index = st.selectbox(
            "Search report",
            options=list(range(len(records))),
            index=None,
            format_func=lambda index: direct_lookup_report_label(records[index]),
            placeholder="Type a report name, workspace, or dataset ID",
            key="direct_measure_report_lookup",
            help="Search uses closest matching report, workspace, and dataset values.",
        )
        load_col, meta_col = st.columns([1.2, 4])
        with load_col:
            if st.button("Open selected report", type="primary", use_container_width=True):
                if selected_index is None:
                    st.warning("Select one report to open.")
                else:
                    selected_record = records[selected_index]
                    _activate_direct_report(selected_record)
        with meta_col:
            st.caption(f"{len(records)} report(s) are available from workspace access.")

    context = st.session_state.get("direct_measure_active_context")
    if not context:
        st.info("Search for a report, select the closest match, and open it to load measure lineage.")
        return

    st.markdown(
        f"""
        <div class="direct-context-line">
            Loaded report: <strong>{html.escape(str(context.get("Workspace") or ""))}</strong>
            | <strong>{html.escape(str(context.get("Source Report") or ""))}</strong>
            | Dataset: {html.escape(str(context.get("Dataset ID") or ""))}
        </div>
        """,
        unsafe_allow_html=True,
    )

    report_key = safe_widget_key(context.get("Report ID"))
    scope_key = f"report_lineage_{report_key}"
    lineage_tab, visual_details_tab, visual_item_lineage_tab = st.tabs([
        "Lineage Analysis",
        "Visual Details",
        "Visual Item Lineage",
    ])

    with lineage_tab:
        st.write("### Artifact Lineage")
        st.caption("Showing lineage for 1 selected report. Change the report selection above to inspect another report.")
        source_lineage_tab, semantic_objects_tab, measure_lineage_tab = st.tabs([
            "Source DB Lineage",
            "Semantic Model Objects",
            "Measure Source Lineage",
        ])

        with source_lineage_tab:
            render_source_db_lineage_view(
                [context],
                headersSPA,
                xmla_token,
                f"report_lineage_{report_key}",
                f"report_lineage_source_db_download_{report_key}",
            )

        with semantic_objects_tab:
            render_semantic_model_objects_view(
                [context],
                headersSPA,
                headersSP,
                xmla_token,
                f"report_lineage_{report_key}",
                f"report_lineage_semantic_objects_download_{report_key}",
            )

        with measure_lineage_tab:
            render_measure_source_lineage_view(
                [context],
                headersSPA,
                xmla_token,
                f"report_lineage_{report_key}",
                f"report_lineage_lookup_download_{report_key}",
            )

        render_report_details_export(
            context,
            headersSPA,
            headersSP,
            xmla_token,
            scope_key,
        )

    with visual_details_tab:
        st.write("### Visual Details")
        st.caption("Retrieve the report definition and inspect its pages, visuals, and semantic fields.")
        render_report_layout_view(
            [context],
            scope_key,
            f"report_lineage_report_layout_download_{report_key}",
            powerbi_headers=headersSPA,
        )

    with visual_item_lineage_tab:
        st.write("### Visual Item Lineage")
        st.caption("Join each report visual item to its semantic object and source database lineage.")
        render_visual_source_lookup_view(
            [context],
            headersSPA,
            headersSP,
            xmla_token,
            scope_key,
            scope_key,
            f"report_lineage_visual_lineage_download_{report_key}",
        )


def _render_impact_page(
    headersSPA,
    headersSP,
    *,
    title,
    eyebrow,
    description,
    render_analysis_view,
    get_workspace_inventory,
    get_artifacts,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, title)
    st.markdown(
        f"""
        <div class="page-header">
            <div class="page-eyebrow">{html.escape(eyebrow)}</div>
            <h2>{html.escape(title)}</h2>
            <p>{html.escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    refresh_col, _ = st.columns([1, 4])
    with refresh_col:
        if st.button("Refresh inventory", key=f"refresh_{title.casefold().replace(' ', '_')}", use_container_width=True):
            _clear_accessible_inventory_cache()
            st.session_state[_SHARED_APP_CACHE_CLEAR_REQUEST_KEY] = True
            st.session_state.pop("accessible_lineage_inventory_v4", None)
            st.session_state.pop("direct_lookup_report_records_v3", None)
            st.session_state.pop("table_impact_analysis_result", None)
            st.session_state.pop("measure_impact_analysis_result", None)
            st.rerun()

    with st.spinner("Loading accessible reports..."):
        records = get_direct_lookup_report_records(headersSP, get_workspace_inventory, get_artifacts)
    if not records:
        st.info("No reports were returned for the signed-in user.")
        return

    render_analysis_view(
        records,
        headersSPA,
        headersSP,
        st.session_state.auth_bundle["spa"],
    )


def render_table_impact_page(
    headersSPA,
    headersSP,
    *,
    get_workspace_inventory,
    get_artifacts,
    render_table_impact_analysis_view,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    """Render a standalone table-to-measure/report impact search."""
    _render_impact_page(
        headersSPA,
        headersSP,
        title="Table Impact",
        eyebrow="Reverse Lineage",
        description="Search a semantic or source table and trace its dependent measures, connected models, reports, and cached visual usage.",
        render_analysis_view=render_table_impact_analysis_view,
        get_workspace_inventory=get_workspace_inventory,
        get_artifacts=get_artifacts,
        logout_and_clear_session=logout_and_clear_session,
        clear_streamlit_session_state=clear_streamlit_session_state,
    )


def render_measure_impact_page(
    headersSPA,
    headersSP,
    *,
    get_workspace_inventory,
    get_artifacts,
    render_measure_impact_analysis_view,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    """Render a standalone measure-to-report/source impact search."""
    _render_impact_page(
        headersSPA,
        headersSP,
        title="Measure Impact",
        eyebrow="Reverse Lineage",
        description="Search a measure and trace the connected reports, semantic dependencies, physical source objects, and cached visual usage.",
        render_analysis_view=render_measure_impact_analysis_view,
        get_workspace_inventory=get_workspace_inventory,
        get_artifacts=get_artifacts,
        logout_and_clear_session=logout_and_clear_session,
        clear_streamlit_session_state=clear_streamlit_session_state,
    )
