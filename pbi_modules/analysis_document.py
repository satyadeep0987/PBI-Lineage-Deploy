"""Local Markdown documentation for completed Claude lineage analyses."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Iterable, Mapping, Optional


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _fenced_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            pass
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text.replace("```", "` ` `")


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
    """Build a self-contained report without invoking a model or external service."""
    context = dict(context or {})
    trace_rows = [dict(row) for row in trace or [] if isinstance(row, Mapping)]
    evidence = [dict(item) for item in evidence_packets or [] if isinstance(item, Mapping)]
    usage = dict(usage or {})
    orchestration = dict(orchestration or {})
    timestamp_value = datetime.now().timestamp() if created_at is None else created_at
    generated = datetime.fromtimestamp(timestamp_value).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    lines = [
        "# Power BI Lineage Analysis",
        "",
        f"Generated locally: {generated}",
        "",
        "## Request",
        "",
        question.strip() or "No question was recorded.",
        "",
        "## Findings",
        "",
        answer.strip() or "No Claude response was recorded.",
        "",
        "## Analysis Flow",
        "",
        "```mermaid",
        "flowchart LR",
        "    Q[User request] --> A[Claude lineage analysis]",
        "    A --> T[Local read-only lineage tools]",
        "    T --> E[Evidence returned by the application]",
        "    E --> F[Documented findings]",
        "```",
        "",
        "## Analysis Context",
        "",
        "| Field | Value |",
        "|---|---|",
    ]
    context_rows = [
        ("Workspaces", ", ".join(context.get("workspace_names") or []) or "Not recorded"),
        ("Model", context.get("model") or "Not recorded"),
        ("Runtime", context.get("runtime") or "Not recorded"),
        ("Requested strategy", context.get("strategy") or "Not recorded"),
        ("Executed strategy", orchestration.get("mode") or "Not recorded"),
        (
            "Selected agents",
            ", ".join(orchestration.get("selected_agents") or []) or "general_lineage",
        ),
        ("Input tokens", usage.get("input_tokens") or 0),
        ("Output tokens", usage.get("output_tokens") or 0),
    ]
    lines.extend(f"| {_markdown_cell(label)} | {_markdown_cell(value)} |" for label, value in context_rows)

    lines.extend(["", "## Evidence Activity", ""])
    if trace_rows:
        lines.extend(
            [
                "| Agent | Tool | Status | Duration ms | Summary |",
                "|---|---|---|---:|---|",
            ]
        )
        for row in trace_rows:
            lines.append(
                "| "
                + " | ".join(
                    _markdown_cell(row.get(field))
                    for field in ("agent", "tool", "status", "duration_ms", "summary")
                )
                + " |"
            )
    else:
        lines.append("No tool activity was recorded for this response.")

    lines.extend(["", "## Returned Evidence", ""])
    if evidence:
        for index, packet in enumerate(evidence, start=1):
            lines.extend(
                [
                    f"### Evidence {index}: {_markdown_cell(packet.get('tool') or packet.get('agent') or 'Local evidence')}",
                    "",
                    "**Input**",
                    "",
                    "```json",
                    _fenced_json(packet.get("input") or {}),
                    "```",
                    "",
                    "**Output**",
                    "",
                    "```json",
                    _fenced_json(packet.get("content") or packet),
                    "```",
                    "",
                ]
            )
    else:
        lines.append("No detailed evidence packet was retained for this response.")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "This document was assembled locally by PBI Lineage Explorer. No additional Claude request was made to generate or format it.",
        ]
    )
    return "\n".join(lines) + "\n"
