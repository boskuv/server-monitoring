import html


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


def format_report(metrics, disk_warn_percent: int) -> str:
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

    return "\n".join(lines)
