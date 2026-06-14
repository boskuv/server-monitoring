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
