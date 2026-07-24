"""Local Markdown documentation for completed PowerAI lineage analyses."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Iterable, Mapping, Optional


def _safe_filename_fragment(value: Any) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "analysis")).strip("_")
    return normalized[:60] or "analysis"


def claude_analysis_markdown_filename(question: str, created_at: Optional[float] = None) -> str:
    timestamp_value = datetime.now().timestamp() if created_at is None else created_at
    timestamp = datetime.fromtimestamp(timestamp_value).strftime(
        "%Y%m%d_%H%M%S"
    )
    return f"pbi_lineage_{_safe_filename_fragment(question)}_{timestamp}.md"


def build_claude_analysis_markdown(
    *,
    question: str,
    answer: str,
    context: Optional[Mapping[str, Any]] = None,
    trace: Optional[Iterable[Mapping[str, Any]]] = None,
    usage: Optional[Mapping[str, Any]] = None,
    orchestration: Optional[Mapping[str, Any]] = None,
    evidence_packets: Optional[Iterable[Mapping[str, Any]]] = None,
    created_at: Optional[float] = None,
) -> str:
    """Build the downloadable chat output without metadata or evidence sections."""
    return (answer.strip() or "No PowerAI response was recorded.") + "\n"
