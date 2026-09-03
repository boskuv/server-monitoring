from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    telegram_chat_id: str
    disk_warn_percent: int = 80
    state_file: str = "/data/net_state.json"
    hostname_override: str | None = None
    log_checks_enabled: bool = False
    log_checks_file: str = "/config/log_checks.yaml"
    log_state_file: str = "/data/log_state.json"
    log_default_lookback_minutes: int = 15
    report_mode: str = "always"
    report_daily_hour: int = 9
    report_daily_minute: int = 0
    report_state_file: str = "/data/report_state.json"
    cpu_alert_percent: float = 0
    ram_alert_percent: float = 0
    disk_alert_percent: float = 0
    load_alert_per_core: float = 0
    net_rx_alert_mbps: float = 0
    net_tx_alert_mbps: float = 0
    log_errors_trigger_alert: bool = True
    alert_cooldown_minutes: int = 120
    log_report_attach_html: bool = True
