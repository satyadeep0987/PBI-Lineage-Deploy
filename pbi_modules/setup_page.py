"""Compatibility entry points for the sidebar-based runtime connections.

The application no longer has a blocking setup page or an OAuth redirect-state
bridge. Power BI and Snowflake connect independently from the persistent
sidebar while the normal Home page remains visible.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from pbi_modules.connection_sidebar import render_connection_sidebar


def render_setup_page() -> None:
    """Render the connection controls in the sidebar for legacy callers."""
    with st.sidebar:
        render_connection_sidebar()


def handle_entra_callback_if_present() -> bool:
    """The active local interactive-browser flow has no app query callback."""
    return False


def forget_pending_setup(_state: Any = None) -> None:
    """Compatibility no-op; the active flow stores no pending redirect record."""
    return
