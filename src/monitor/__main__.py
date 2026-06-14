import sys

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

    print("Report sent successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
