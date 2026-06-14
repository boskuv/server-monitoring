import sys

from monitor.alerting import decide_report, format_alert_report, record_sent
from monitor.collectors import collect_all
from monitor.config import Settings
from monitor.log_collector import collect_log_errors
from monitor.reporter import format_report
from monitor.telegram import send_message


def main() -> int:
    try:
        settings = Settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    metrics = collect_all(
        state_file=settings.state_file,
        hostname_override=settings.hostname_override,
    )

    log_summary = collect_log_errors(
        enabled=settings.log_checks_enabled,
        config_path=settings.log_checks_file,
        state_path=settings.log_state_file,
        default_lookback_minutes=settings.log_default_lookback_minutes,
    )

    decision = decide_report(
        metrics=metrics,
        log_summary=log_summary,
        settings=settings,
        report_state_path=settings.report_state_file,
    )

    if not decision.should_send:
        print(
            "Report skipped (no alert, no scheduled daily). "
            f"Mode: {settings.report_mode}"
        )
        return 0

    if decision.report_type == "alert":
        report = format_alert_report(metrics, decision.reasons)
    else:
        report = format_report(
            metrics,
            disk_warn_percent=settings.disk_warn_percent,
            log_summary=log_summary,
        )

    try:
        send_message(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            text=report,
        )
    except Exception as exc:
        print(f"Failed to send Telegram message: {exc}", file=sys.stderr)
        return 1

    record_sent(decision, settings.report_state_file)
    print(f"Report sent ({decision.report_type}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
