import hashlib
import re
from difflib import SequenceMatcher

import streamlit as st


def render_searchable_single_select(
    label,
    options,
    key,
    index=0,
    format_func=None,
    help_text=None,
    search_placeholder="Type to search...",
    max_results=150,
    explicit_search=False,
):
    """Render a single selector, optionally with an explicit search box."""
    options = list(options or [])
    if not options:
        st.info(f"No options available for {label}.")
        return None

    display_func = format_func if callable(format_func) else str
    widget_key = f"{key}__search_select"
    selected_value_key = f"{key}__selected_value"

    if selected_value_key in st.session_state and st.session_state[selected_value_key] not in options:
        st.session_state.pop(selected_value_key, None)

    if not explicit_search:
        if widget_key in st.session_state and st.session_state[widget_key] not in options:
            st.session_state.pop(widget_key, None)

        safe_index = min(max(index, 0), len(options) - 1)
        return st.selectbox(
            label,
            options=options,
            index=safe_index,
            format_func=display_func,
            key=widget_key,
            help=help_text,
        )

    query_key = f"{key}__search_query"
    if query_key not in st.session_state:
        st.session_state[query_key] = ""
    query = st.text_input(
        label,
        key=query_key,
        placeholder=search_placeholder,
        help=help_text,
    )

    filtered_options = _filter_single_select_options(options, query, display_func, max_results=max_results)
    if not filtered_options:
        st.warning("No matching records found.")
        if widget_key in st.session_state:
            st.session_state.pop(widget_key, None)
        return None

    selected_value = st.session_state.get(selected_value_key)
    if selected_value in filtered_options:
        filtered_index = filtered_options.index(selected_value)
    else:
        safe_index = min(max(index, 0), len(options) - 1)
        default_value = options[safe_index]
        filtered_index = filtered_options.index(default_value) if default_value in filtered_options else 0

    if widget_key in st.session_state and st.session_state[widget_key] not in filtered_options:
        st.session_state.pop(widget_key, None)

    selected = st.selectbox(
        "Matching results",
        options=filtered_options,
        index=filtered_index,
        format_func=display_func,
        key=widget_key,
    )
    st.session_state[selected_value_key] = selected

    if query and len(filtered_options) < len(options):
        st.caption(f"Showing {len(filtered_options)} of {len(options)} matching records.")

    return selected


def _normalize_search_value(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _option_display_label(option, format_func=None):
    display_func = format_func if callable(format_func) else str
    try:
        label = display_func(option)
    except Exception:
        label = option
    return str(label or "")


def _filter_single_select_options(options, query, format_func=None, max_results=150):
    """Return closest matching options for the explicit search selector."""
    options = list(options or [])
    max_results = max(1, int(max_results or 150))
    normalized_query = _normalize_search_value(query)
    if not normalized_query:
        return options[:max_results]

    query_terms = [term for term in normalized_query.split(" ") if term]
    ranked = []
    fuzzy_candidates = []

    for position, option in enumerate(options):
        label = _option_display_label(option, format_func)
        normalized_label = _normalize_search_value(label)
        if not normalized_label:
            continue

        score = None
        label_words = normalized_label.split(" ")
        if normalized_label == normalized_query:
            score = 0
        elif normalized_label.startswith(normalized_query):
            score = 1
        elif any(word.startswith(normalized_query) for word in label_words):
            score = 2
        elif normalized_query in normalized_label:
            score = 3
        elif query_terms and all(term in normalized_label for term in query_terms):
            score = 4
        elif len(normalized_query) >= 3:
            fuzzy_candidates.append((position, option, normalized_label, label_words))

        if score is not None:
            ranked.append((score, position, option))

    if not ranked and fuzzy_candidates:
        for position, option, normalized_label, label_words in fuzzy_candidates:
            full_ratio = SequenceMatcher(None, normalized_query, normalized_label).ratio()
            word_ratio = max(
                (SequenceMatcher(None, normalized_query, word).ratio() for word in label_words),
                default=0,
            )
            ratio = max(full_ratio, word_ratio)
            if ratio >= 0.58:
                ranked.append((5 + (1 - ratio), position, option))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return [option for _, _, option in ranked[:max_results]]


def render_searchable_multiselect(
    label,
    options,
    key,
    default=None,
    help_text=None,
    search_placeholder="Type to search...",
    format_func=None,
):
    """Render a native searchable multiselect for long workspace/app/report lists."""
    options = list(options or [])
    display_func = format_func if callable(format_func) else str
    default = [item for item in list(default or []) if item in options]
    option_hash_key = f"{key}__option_hash"
    widget_key = f"{key}__search_multiselect"
    option_hash_source = "||".join(_option_display_label(option, display_func) for option in options)
    option_hash = hashlib.md5(option_hash_source.encode("utf-8")).hexdigest()

    if st.session_state.get(option_hash_key) != option_hash:
        st.session_state[option_hash_key] = option_hash
        st.session_state[widget_key] = default.copy()

    if widget_key in st.session_state:
        st.session_state[widget_key] = [item for item in st.session_state[widget_key] if item in options]

    with st.container(border=True):
        if not options:
            st.info("No options available.")
            return []
        current_count = len(st.session_state.get(widget_key, default))
        st.caption(f"Selected {current_count} of {len(options)}")
        widget_default = None if widget_key in st.session_state else default
        return st.multiselect(
            label,
            options=options,
            default=widget_default,
            key=widget_key,
            help=help_text,
            format_func=display_func,
        )


def render_checkbox_selector(label, options, key, default=None, help_text=None):
    """Backward-compatible wrapper for searchable multi-select controls."""
    return render_searchable_multiselect(label, options, key, default=default, help_text=help_text)


def render_csv_download(df, label, file_name, key):
    """Render a CSV download button for a displayed dataframe."""
    if df is None or getattr(df, "empty", True):
        return
    st.download_button(
        label,
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=file_name,
        mime="text/csv",
        key=key,
    )

