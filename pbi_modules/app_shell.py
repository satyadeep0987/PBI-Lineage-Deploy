import html
import time

import streamlit as st


def _render_pending_device_flow(get_all_tokens):
    """Render the follow-up step for Microsoft device-code sign-in."""
    flow = st.session_state.get("msal_device_flow")
    if not isinstance(flow, dict):
        return False

    verification_uri = (
        flow.get("verification_uri")
        or flow.get("verification_url")
        or "https://microsoft.com/devicelogin"
    )
    user_code = str(flow.get("user_code") or "").strip()
    st.info(f"Open {verification_uri} and enter this code:")
    if user_code:
        st.code(user_code)

    complete_col, restart_col = st.columns([1, 1])
    with complete_col:
        if st.button("I completed sign-in", type="primary", use_container_width=True):
            with st.spinner("Checking Microsoft sign-in..."):
                result = get_all_tokens(prompt_behavior="none")
                if result:
                    st.session_state.auth_bundle = result
                    st.session_state.workflow_mode = "landing"
                    st.rerun()
    with restart_col:
        if st.button("Restart sign-in", use_container_width=True):
            st.session_state.pop("msal_device_flow", None)
            st.rerun()

    return True


def _set_workflow(mode):
    st.session_state.workflow_mode = mode
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
    st.session_state.workflow_mode = "direct_measure"
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
    """Render the authenticated application header."""
    top_col_title, home_col, explore_col, measure_col, top_col_status, logout_col = st.columns(
        [3.4, 0.72, 0.82, 0.9, 0.82, 0.76],
        vertical_alignment="center",
    )

    with top_col_title:
        st.markdown(
            """
            <div class="app-top-strip">
                <div class="app-brand-mark">PBI</div>
                <div class="app-brand-copy">
                    <div class="app-top-title">PBI Lineage Explorer</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with home_col:
        if st.button("Home", key="top_home", use_container_width=True):
            _set_workflow("landing")

    with explore_col:
        if st.button("Explore", key="top_explore", use_container_width=True):
            _set_workflow("guided")

    with measure_col:
        if st.button("Measures", key="top_measures", use_container_width=True):
            _set_workflow("direct_measure")

    with top_col_status:
        st.markdown(
            """
            <div class="app-status-panel">
                <span class="app-status-dot"></span>
                <span class="app-status-text">Connected</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with logout_col:
        if st.button("Logout", key="top_logout", use_container_width=True):
            with st.spinner("Releasing tokens and clearing session cache..."):
                try:
                    logout_and_clear_session()
                except Exception as e:
                    st.error(f"Error releasing tokens: {e}")
                    clear_streamlit_session_state(keep_auth=False)
                st.rerun()


def render_login_page(clear_streamlit_session_state, get_all_tokens):
    """Render the external authentication entry point."""
    spacer_left, login_col, spacer_right = st.columns([0.16, 1, 0.16])
    with login_col:
        with st.container(border=True):
            st.markdown(
                """
                <div class="page-header centered">
                    <div class="page-eyebrow">Authentication</div>
                    <h1>PBI Lineage Explorer</h1>
                    <p>
                        Sign in with your organization account. Authentication opens the Microsoft identity
                        flow, validates the credentials, and returns you to this app when the token is available.
                    </p>
                </div>
                <div class="auth-step-table">
                    <div class="auth-step-cell">
                        <strong>1. Authenticate</strong>
                        <span>Use the external Microsoft sign-in flow.</span>
                    </div>
                    <div class="auth-step-cell">
                        <strong>2. Choose Flow</strong>
                        <span>Pick guided exploration or direct measure lookup.</span>
                    </div>
                    <div class="auth-step-cell">
                        <strong>3. Analyze</strong>
                        <span>Review definitions, source lineage, and Snowflake lineage.</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.markdown('<div class="auth-action-spacer"></div>', unsafe_allow_html=True)
            if _render_pending_device_flow(get_all_tokens):
                return

            action_left, action_col, action_right = st.columns([1, 1.4, 1])
            with action_col:
                if st.button("Continue to sign in", type="primary", use_container_width=True):
                    clear_streamlit_session_state(keep_auth=False)
                    with st.spinner("Opening external authentication..."):
                        result = get_all_tokens(prompt_behavior="select_account")
                        if result:
                            st.session_state.auth_bundle = result
                            st.session_state.workflow_mode = "landing"
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


def get_accessible_inventory(headers, get_workspace_inventory, get_artifacts):
    """Load and cache workspaces and reports used by the home and measure views."""
    cache_key = "accessible_lineage_inventory_v2"
    cached = st.session_state.get(cache_key)
    if isinstance(cached, dict):
        return cached

    workspaces = list(get_workspace_inventory(headers) or [])
    reports = []
    for workspace in workspaces:
        workspace_id = workspace.get("id")
        workspace_name = workspace.get("name")
        if not workspace_id:
            continue
        for report in get_artifacts(headers, workspace_id, "report") or []:
            reports.append({
                "Workspace Name": workspace_name,
                "Workspace ID": workspace_id,
                "Report Name": report.get("name"),
                "Report ID": report.get("id"),
                "Dataset ID": report.get("datasetId"),
                "Report Type": report.get("reportType"),
                "Report Format": report.get("format"),
                "Embed URL": report.get("embedUrl"),
            })

    reports.sort(key=lambda row: (
        str(row.get("Report Name") or "").lower(),
        str(row.get("Workspace Name") or "").lower(),
    ))
    inventory = {"workspaces": workspaces, "reports": reports}
    st.session_state[cache_key] = inventory
    st.session_state["direct_lookup_report_records_v1"] = reports
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
    with st.spinner("Loading your Power BI inventory..."):
        inventory = get_accessible_inventory(headers, get_workspace_inventory, get_artifacts)
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
        if st.button("Search", type="primary", use_container_width=True, disabled=not reports):
            if selected_report_index is None:
                st.warning("Select a report from the search results.")
            else:
                _activate_direct_report(reports[selected_report_index])

    st.markdown(_inventory_summary_html(workspaces, reports), unsafe_allow_html=True)

    section_title_col, refresh_col = st.columns([5, 1], vertical_alignment="center")
    with section_title_col:
        st.markdown('<div class="section-heading"><strong>Quick actions</strong><span>Start with the task you need.</span></div>', unsafe_allow_html=True)
    with refresh_col:
        if st.button("Refresh inventory", use_container_width=True):
            st.session_state.pop("accessible_lineage_inventory_v2", None)
            st.session_state.pop("direct_lookup_report_records_v1", None)
            st.rerun()

    quick_actions = [
        ("01", "Explore a report", "Workspace, report, semantic objects, and lineage", "Explore", "guided"),
        ("02", "Find a measure", "Definitions, DAX logic, and column lineage", "Find measure", "direct_measure"),
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
                if st.button(button_label, key=f"home_action_{number}", use_container_width=True):
                    _set_workflow(mode)

    st.markdown('<div class="section-heading report-section-heading"><strong>Accessible reports</strong><span>Open a report directly in measure and lineage analysis.</span></div>', unsafe_allow_html=True)
    if not reports:
        st.info("No accessible reports were returned for this account.")
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
    cache_key = "direct_lookup_report_records_v1"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    records = get_accessible_inventory(headers, get_workspace_inventory, get_artifacts).get("reports") or []
    st.session_state[cache_key] = records
    return records


def direct_report_context(record):
    label = direct_lookup_report_label(record)
    return {
        "Context Key": label,
        "Scope Type": "Workspace",
        "Container Name": record.get("Workspace Name"),
        "Workspace": record.get("Workspace Name"),
        "App Name": "N/A",
        "Source Report": record.get("Report Name"),
        "Report ID": record.get("Report ID"),
        "Dataset ID": record.get("Dataset ID"),
        "Target Workspace ID": record.get("Workspace ID"),
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
    render_measure_source_lineage_view,
    safe_widget_key,
    logout_and_clear_session,
    clear_streamlit_session_state,
):
    """Search a report by name and render measure definitions/lineage directly."""
    render_app_top_bar(logout_and_clear_session, clear_streamlit_session_state, "Direct measure lookup")
    st.markdown(
        """
        <div class="page-header">
            <div class="page-eyebrow">Measure Search</div>
            <h2>Direct Measure Lookup</h2>
            <p>Search the reports visible to the signed-in account, open one report, then work with its measure definitions and source lineage directly.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    refresh_col, _ = st.columns([1, 4])
    with refresh_col:
        if st.button("Refresh reports", use_container_width=True):
            st.session_state.pop("accessible_lineage_inventory_v2", None)
            st.session_state.pop("direct_lookup_report_records_v1", None)
            st.session_state.pop("direct_measure_active_context", None)
            st.rerun()

    with st.spinner("Loading accessible reports..."):
        records = get_direct_lookup_report_records(headersSP, get_workspace_inventory, get_artifacts)

    if not records:
        st.info("No reports were returned for the signed-in user.")
        return

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

    xmla_token = st.session_state.auth_bundle["spa"]
    render_measure_source_lineage_view(
        [context],
        headersSPA,
        xmla_token,
        f"direct_measure_lookup_{safe_widget_key(context.get('Report ID'))}",
        "direct_measure_lookup_download",
    )
