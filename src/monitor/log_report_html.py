import re
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from monitor.log_collector import LogScanSummary

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _sorted_groups(groups: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(groups.items(), key=lambda item: item[1], reverse=True)


def _error_entries(log_summary: LogScanSummary) -> list[dict]:
    entries: list[dict] = []
    for entry in (*log_summary.containers, *log_summary.host_logs):
        if entry.status != "errors" or entry.error_count <= 0:
            continue
        entries.append(
            {
                "name": entry.name,
                "error_count": entry.error_count,
                "interval_label": entry.interval_label,
                "groups": _sorted_groups(entry.groups),
                "samples": entry.samples,
            }
        )
    return entries


def render_log_errors_html(
    log_summary: LogScanSummary,
    hostname: str,
    generated_at: str | None = None,
) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("log_report.html")
    return template.render(
        hostname=hostname,
        generated_at=generated_at
        or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        interval_label=log_summary.interval_label,
        entries=_error_entries(log_summary),
    )


def format_log_attachment_caption(log_summary: LogScanSummary) -> str:
    parts: list[str] = []
    for entry in (*log_summary.containers, *log_summary.host_logs):
        if entry.status == "errors" and entry.error_count > 0:
            parts.append(f"{entry.name}: {entry.error_count} events")
    return "Log errors — " + ", ".join(parts)


def build_log_attachment_filename(hostname: str, now_utc: datetime | None = None) -> str:
    timestamp = (now_utc or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    safe_host = re.sub(r"[^\w.-]+", "-", hostname).strip("-") or "server"
    return f"log-errors-{safe_host}-{timestamp}.html"
