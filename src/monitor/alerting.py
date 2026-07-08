import html
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from monitor.log_collector import LogScanSummary


@dataclass
class SendDecision:
    should_send: bool
    report_type: str  # full | alert | skip
    reasons: list[str] = field(default_factory=list)


def _load_report_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_report_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _esc(value: str | float | int) -> str:
    return html.escape(str(value))


def _evaluate_thresholds(metrics, settings) -> list[str]:
    reasons: list[str] = []

    if settings.cpu_alert_percent > 0 and metrics.cpu.percent >= settings.cpu_alert_percent:
        reasons.append(
            f"CPU {_esc(metrics.cpu.percent)}% (threshold {settings.cpu_alert_percent}%)"
        )

    if settings.ram_alert_percent > 0 and metrics.memory.percent >= settings.ram_alert_percent:
        reasons.append(
            f"RAM {_esc(metrics.memory.percent)}% (threshold {settings.ram_alert_percent}%)"
        )

    if settings.load_alert_per_core > 0 and metrics.cpu.cores > 0:
        load_per_core = metrics.cpu.load_1 / metrics.cpu.cores
        if load_per_core >= settings.load_alert_per_core:
            reasons.append(
                f"Load {_esc(metrics.cpu.load_1)} / {_esc(metrics.cpu.cores)} cores "
                f"(threshold {_esc(settings.load_alert_per_core)}/core)"
            )

    if settings.disk_alert_percent > 0:
        for disk in metrics.disks:
            if disk.percent >= settings.disk_alert_percent:
                reasons.append(
                    f"Disk {_esc(disk.mountpoint)} {_esc(disk.percent)}% "
                    f"(threshold {settings.disk_alert_percent}%)"
                )

    for iface in metrics.network:
        if iface.rx_mbps is None or iface.tx_mbps is None:
            continue
        if settings.net_rx_alert_mbps > 0 and iface.rx_mbps >= settings.net_rx_alert_mbps:
            reasons.append(
                f"Network {_esc(iface.name)} RX {_esc(iface.rx_mbps)} Mbps "
                f"(threshold {settings.net_rx_alert_mbps} Mbps)"
            )
        if settings.net_tx_alert_mbps > 0 and iface.tx_mbps >= settings.net_tx_alert_mbps:
            reasons.append(
                f"Network {_esc(iface.name)} TX {_esc(iface.tx_mbps)} Mbps "
                f"(threshold {settings.net_tx_alert_mbps} Mbps)"
            )

    return reasons


def _evaluate_log_alerts(log_summary: LogScanSummary | None, settings) -> list[str]:
    if not settings.log_errors_trigger_alert:
        return []
    if log_summary is None or log_summary.status != "ok":
        return []

    reasons: list[str] = []
    for container in log_summary.containers:
        if container.status == "errors" and container.error_count > 0:
            reasons.append(
                f"Logs {_esc(container.name)}: {_esc(container.error_count)} errors"
            )
    for host_log in log_summary.host_logs:
        if host_log.status == "errors" and host_log.error_count > 0:
            reasons.append(
                f"Host log {_esc(host_log.name)}: {_esc(host_log.error_count)} events"
            )
    return reasons


def _is_daily_due(now_utc: datetime, last_daily_date: str | None, settings) -> bool:
    today = now_utc.strftime("%Y-%m-%d")
    if last_daily_date == today:
        return False

    daily_minutes = settings.report_daily_hour * 60 + settings.report_daily_minute
    now_minutes = now_utc.hour * 60 + now_utc.minute
    return now_minutes >= daily_minutes


def decide_report(
    metrics,
    log_summary: LogScanSummary | None,
    settings,
    report_state_path: str,
) -> SendDecision:
    if settings.report_mode == "always":
        return SendDecision(should_send=True, report_type="full", reasons=["always"])

    state = _load_report_state(report_state_path)
    now_utc = datetime.now(timezone.utc)
    threshold_reasons = _evaluate_thresholds(metrics, settings)
    log_reasons = _evaluate_log_alerts(log_summary, settings)
    alert_reasons = threshold_reasons + log_reasons

    if _is_daily_due(now_utc, state.get("last_daily_sent_date"), settings):
        return SendDecision(
            should_send=True,
            report_type="full",
            reasons=["scheduled daily report"],
        )

    if alert_reasons:
        last_alert_ts = state.get("last_alert_ts", 0)
        elapsed = time.time() - float(last_alert_ts)
        if elapsed >= settings.alert_cooldown_minutes * 60:
            return SendDecision(
                should_send=True,
                report_type="alert",
                reasons=alert_reasons,
            )

    return SendDecision(should_send=False, report_type="skip", reasons=[])


def record_sent(decision: SendDecision, report_state_path: str) -> None:
    if not decision.should_send:
        return

    state = _load_report_state(report_state_path)
    now_utc = datetime.now(timezone.utc)

    if decision.report_type == "full":
        state["last_daily_sent_date"] = now_utc.strftime("%Y-%m-%d")

    if decision.report_type == "alert":
        state["last_alert_ts"] = time.time()

    _save_report_state(report_state_path, state)


def format_alert_report(metrics, reasons: list[str]) -> str:
    system = metrics.system
    lines = [
        f"⚠️ <b>ALERT</b> — {_esc(system.hostname)} ({_esc(system.timestamp_utc)})",
        "",
    ]
    for reason in reasons:
        lines.append(f"• {reason}")
    return "\n".join(lines)
