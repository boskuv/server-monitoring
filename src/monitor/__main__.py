import sys

from monitor.collectors import collect_all
from monitor.config import Settings
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

    report = format_report(metrics, disk_warn_percent=settings.disk_warn_percent)

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
