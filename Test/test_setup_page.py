"""UI and one-time OAuth bridge tests for the session-only setup page."""

from __future__ import annotations

from pathlib import Path
import unittest

from streamlit.testing.v1 import AppTest

from pbi_modules import setup_controller as setup


class SetupPageTests(unittest.TestCase):
    def test_real_application_starts_on_home_with_connection_sidebar(self):
        app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        app = AppTest.from_file(str(app_path), default_timeout=60).run(timeout=60)

        self.assertFalse(list(app.exception))
        self.assertTrue(
            any("What would you like to explore?" in str(item.value) for item in app.markdown)
        )
        buttons = [item for item in app.button if item.label == "Home"]
        self.assertTrue(buttons and not buttons[0].disabled)
        self.assertTrue(any(item.label == "Connect Power BI" for item in app.button))
        self.assertTrue(any(item.label == "Connect Snowflake" for item in app.button))

    def test_connected_powerbi_controls_lock_without_blocking_snowflake(self):
        app = AppTest.from_string(
            """
import streamlit as st
from pbi_modules.connection_sidebar import render_connection_sidebar
from pbi_modules import setup_controller as setup

if setup.SETUP_STATE_KEY not in st.session_state:
    state = setup.create_empty_setup_state(now=1)
    setup.configure_powerbi(state, {
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
    })
    state["credentials"]["powerbi"]["access_token"] = "test-token"
    state["credentials"]["powerbi"]["expires_at"] = 9999999999
    state["public"]["powerbi"]["validated_scope"] = {
        "mode": "all_accessible",
        "fingerprint": setup.powerbi_scope_fingerprint(state),
    }
    state["status"]["powerbi"] = {"state": "ready", "message": "Connected."}
    st.session_state[setup.SETUP_STATE_KEY] = state
    st.session_state["workspaces_list_v2"] = ["stale-workspace"]
    st.session_state["source_db_lineage_v18_stale"] = {"secret": "old"}
with st.sidebar:
    st.markdown('<div class="lineage-sidebar-brand">PBI</div>', unsafe_allow_html=True)
    render_connection_sidebar()
""",
            default_timeout=30,
        ).run(timeout=30)

        self.assertFalse(list(app.exception))
        inputs = {item.label: item for item in app.text_input}
        self.assertTrue(inputs["Tenant ID *"].disabled)
        self.assertTrue(inputs["Client ID *"].disabled)
        buttons = {item.label: item for item in app.button}
        self.assertTrue(buttons["Connected"].disabled)
        self.assertFalse(buttons["Connect Snowflake"].disabled)

        buttons["Disconnect Power BI"].click().run(timeout=30)
        remaining = app.session_state.filtered_state
        self.assertNotIn("workspaces_list_v2", remaining)
        self.assertNotIn("source_db_lineage_v18_stale", remaining)

    def test_home_remains_visible_with_separate_sidebar_connections(self):
        app = AppTest.from_string(
            """
import streamlit as st
from pbi_modules.connection_sidebar import render_connection_sidebar

st.markdown('<div class="home-hero"><h1>What would you like to explore?</h1></div>', unsafe_allow_html=True)
with st.sidebar:
    st.markdown('<div class="lineage-sidebar-brand">PBI Lineage Explorer</div>', unsafe_allow_html=True)
    render_connection_sidebar()
""",
            default_timeout=30,
        )
        app.run(timeout=30)

        self.assertFalse(list(app.exception))
        self.assertTrue(
            any("What would you like to explore?" in str(item.value) for item in app.markdown)
        )
        labels = {
            getattr(widget, "label", "")
            for widget in (
                list(app.text_input)
                + list(app.text_area)
                + list(app.selectbox)
                + list(app.checkbox)
            )
        }
        self.assertTrue(
            {
                "Tenant ID *",
                "Client ID *",
                "Workspace ID",
                "Report IDs",
                "Account Identifier *",
                "Database *",
                "Warehouse *",
                "Role *",
                "SSO Login Name *",
                "Dedicated read-only role",
            }.issubset(labels)
        )
        button_labels = {item.label for item in app.button}
        self.assertIn("Connect Power BI", button_labels)
        self.assertIn("Connect Snowflake", button_labels)
        self.assertFalse(any("snowflake password" in label.casefold() for label in labels))


if __name__ == "__main__":
    unittest.main()
