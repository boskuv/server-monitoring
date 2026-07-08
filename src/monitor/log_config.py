from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExtractRule(BaseModel):
    label: str
    pattern: str


class ContainerRule(BaseModel):
    name: str
    match: str
    error_pattern: str
    extract: list[ExtractRule] = Field(default_factory=list)
    max_samples: int = 2


class HostLogRule(BaseModel):
    name: str
    path: str
    error_pattern: str
    extract: list[ExtractRule] = Field(default_factory=list)
    max_samples: int = 3


class LogChecksConfig(BaseModel):
    enabled: bool = True
    containers: list[ContainerRule] = Field(default_factory=list)
    host_logs: list[HostLogRule] = Field(default_factory=list)


def load_log_checks(path: str) -> LogChecksConfig | None:
    config_path = Path(path)
    if not config_path.is_file():
        return None

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not raw:
        return LogChecksConfig(enabled=False)

    return LogChecksConfig.model_validate(raw)
