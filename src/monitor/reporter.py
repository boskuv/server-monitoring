import html

from monitor.log_collector import LogScanSummary

TELEGRAM_MAX_LENGTH = 4096


def _format_uptime(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, _ = divmod(remainder, 3600)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h"
    minutes, _ = divmod(remainder, 60)
    return f"{minutes}m"


def _esc(value: str | float | int) -> str:
    return html.escape(str(value))


def _format_log_section(log_summary: LogScanSummary | None) -> list[str]:
    lines: list[str] = []
    lines.append("")

    if log_summary is None or log_summary.status == "disabled":
        lines.append("<b>Docker Logs:</b> disabled")
        return lines

    if log_summary.status == "unavailable":
        lines.append(
            "<b>Docker Logs:</b> unavailable "
            f"({_esc(log_summary.message or 'cannot connect to Docker')})"
        )
        return lines

    if log_summary.status == "misconfigured":
        lines.append(
            "<b>Docker Logs:</b> misconfigured "
            f"({_esc(log_summary.message or 'invalid config')})"
        )
        return lines

    interval = log_summary.interval_label or "unknown"
    lines.append(
        f"<b>Docker Logs</b> (since last check, {_esc(interval)}):"
    )

    if not log_summary.containers:
        lines.append("  No containers configured")
        return lines

    has_errors = any(c.status == "errors" for c in log_summary.containers)
    if not has_errors and all(c.status == "ok" for c in log_summary.containers):
        lines.append(
            f"  All containers clean ({_esc(len(log_summary.containers))} checked)"
        )
        return lines

    for container in log_summary.containers:
        if container.status == "ok":
            lines.append(f"  {_esc(container.name)}: no errors")
        elif container.status == "not_running":
            lines.append(f"  {_esc(container.name)}: not running")
        elif container.status == "not_found":
            lines.append(f"  {_esc(container.name)}: not found")
        elif container.status == "misconfigured":
            lines.append(f"  ⚠️ {_esc(container.name)}: misconfigured pattern")
        elif container.status == "errors":
            lines.append(
                f"  ⚠️ {_esc(container.name)}: {_esc(container.error_count)} errors"
            )
            if container.groups:
                group_parts = [
                    f"{_esc(key)} ({_esc(count)}x)"
                    for key, count in sorted(
                        container.groups.items(),
                        key=lambda item: item[1],
                        reverse=True,
                    )
                ]
                lines.append(f"    {_esc(', '.join(group_parts))}")
            for sample in container.samples:
                lines.append(f"    - {_esc(sample)}")
        else:
            lines.append(f"  {_esc(container.name)}: {_esc(container.status)}")

    return lines


def _truncate_report(text: str, max_len: int = TELEGRAM_MAX_LENGTH) -> str:
    if len(text) <= max_len:
        return text

    trimmed = text[: max_len - 20]
    if "\n" in trimmed:
        trimmed = trimmed.rsplit("\n", 1)[0]
    return trimmed + "\n... (truncated)"


def format_report(
    metrics,
    disk_warn_percent: int,
    log_summary: LogScanSummary | None = None,
) -> str:
    system = metrics.system
    lines: list[str] = []

    lines.append(
        f"🖥 <b>Server Report</b> — {_esc(system.hostname)} "
        f"({_esc(system.timestamp_utc)})"
    )
    lines.append("")

    cpu = metrics.cpu
    lines.append(
        f"<b>CPU:</b> {_esc(cpu.percent)}% | "
        f"Load: {_esc(cpu.load_1)} / {_esc(cpu.load_5)} / {_esc(cpu.load_15)} "
        f"({_esc(cpu.cores)} cores)"
    )

    mem = metrics.memory
    swap = metrics.swap
    lines.append(
        f"<b>RAM:</b> {_esc(mem.used_gb)} / {_esc(mem.total_gb)} GB "
        f"({_esc(mem.percent)}%) | "
        f"<b>Swap:</b> {_esc(swap.used_gb)} / {_esc(swap.total_gb)} GB "
        f"({_esc(swap.percent)}%)"
    )
    lines.append(f"<b>Uptime:</b> {_esc(_format_uptime(system.uptime_seconds))}")
    lines.append("")

    lines.append("<b>Disk:</b>")
    if metrics.disks:
        for disk in metrics.disks:
            prefix = "⚠️ " if disk.percent >= disk_warn_percent else "  "
            lines.append(
                f"{prefix}<code>{_esc(disk.mountpoint.ljust(12))}</code> "
                f"{_esc(disk.used_gb)} / {_esc(disk.total_gb)} GB "
                f"({_esc(disk.percent)}%)"
            )
    else:
        lines.append("  No mount points found")
    lines.append("")

    if metrics.network_interval_label:
        lines.append(
            f"<b>Network</b> (since last check, {_esc(metrics.network_interval_label)}):"
        )
    else:
        lines.append("<b>Network</b> (first run — totals only):")

    if metrics.network:
        for iface in metrics.network:
            if iface.rx_mbps is not None and iface.tx_mbps is not None:
                lines.append(
                    f"  <code>{_esc(iface.name.ljust(10))}</code> "
                    f"↓ {_esc(iface.rx_mbps)} Mbps  ↑ {_esc(iface.tx_mbps)} Mbps"
                )
            else:
                rx_gb = round(iface.rx_bytes / (1024**3), 2)
                tx_gb = round(iface.tx_bytes / (1024**3), 2)
                lines.append(
                    f"  <code>{_esc(iface.name.ljust(10))}</code> "
                    f"↓ {_esc(rx_gb)} GB  ↑ {_esc(tx_gb)} GB"
                )
    else:
        lines.append("  No network interfaces found")

    lines.extend(_format_log_section(log_summary))
    return _truncate_report("\n".join(lines))
