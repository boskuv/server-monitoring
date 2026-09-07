import sys

from monitor.alerting import decide_report, format_alert_report, record_sent
from monitor.collectors import collect_all
from monitor.config import Settings
from monitor.log_collector import collect_log_errors, save_log_state
from monitor.log_report_html import (
    build_log_attachment_filename,
    format_log_attachment_caption,
    render_log_errors_html,
)
from monitor.reporter import format_report, log_summary_has_errors
from monitor.telegram import send_document, send_message


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

    # Logs are never used for alert decisions — only metrics. Scan + offset
    # commit happen only for full/daily reports after a successful send.
    decision = decide_report(
        metrics=metrics,
        log_summary=None,
        settings=settings,
        report_state_path=settings.report_state_file,
    )

    if not decision.should_send:
        print(
            "Report skipped (no alert, no scheduled daily). "
            f"Mode: {settings.report_mode}"
        )
        return 0

    log_summary = None
    pending_log_state = None
    if decision.report_type == "full":
        log_summary, pending_log_state = collect_log_errors(
            enabled=settings.log_checks_enabled,
            config_path=settings.log_checks_file,
            state_path=settings.log_state_file,
            default_lookback_minutes=settings.log_default_lookback_minutes,
        )

    if decision.report_type == "alert":
        report = format_alert_report(metrics, decision.reasons)
    else:
        report = format_report(
            metrics,
            disk_warn_percent=settings.disk_warn_percent,
            log_summary=log_summary,
        )

    attach_html = (
        settings.log_report_attach_html
        and decision.report_type == "full"
        and log_summary_has_errors(log_summary)
        and log_summary is not None
    )

    try:
        send_message(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            text=report,
        )

        if attach_html:
            html_report = render_log_errors_html(
                log_summary,
                hostname=metrics.system.hostname,
            )
            send_document(
                token=settings.telegram_bot_token,
                chat_id=settings.telegram_chat_id,
                filename=build_log_attachment_filename(metrics.system.hostname),
                content=html_report.encode("utf-8"),
                caption=format_log_attachment_caption(log_summary),
            )
    except Exception as exc:
        print(f"Failed to send Telegram message: {exc}", file=sys.stderr)
        return 1

    if pending_log_state is not None:
        save_log_state(settings.log_state_file, pending_log_state)

    record_sent(decision, settings.report_state_file)
    attachment_note = " + HTML log attachment" if attach_html else ""
    print(f"Report sent ({decision.report_type}){attachment_note}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
