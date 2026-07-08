import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import docker
from docker.errors import DockerException

from monitor.log_config import ContainerRule, HostLogRule, LogChecksConfig, load_log_checks


@dataclass
class ContainerLogResult:
    name: str
    status: str
    error_count: int = 0
    groups: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    interval_label: str | None = None


@dataclass
class HostLogResult:
    name: str
    status: str
    error_count: int = 0
    groups: dict[str, int] = field(default_factory=dict)
    samples: list[str] = field(default_factory=list)
    interval_label: str | None = None


@dataclass
class LogScanSummary:
    status: str
    containers: list[ContainerLogResult] = field(default_factory=list)
    host_logs: list[HostLogResult] = field(default_factory=list)
    interval_label: str | None = None
    message: str | None = None


def _format_interval(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if minutes:
        return f"{hours}h {minutes}m"
    return f"{hours}h"


def _load_log_state(path: str) -> dict:
    state_path = Path(path)
    if not state_path.exists():
        return {"containers": {}, "host_logs": {}}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("containers", {})
            data.setdefault("host_logs", {})
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"containers": {}, "host_logs": {}}


def _save_log_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _matches_name(pattern: str, container_name: str) -> bool:
    try:
        return re.search(pattern, container_name) is not None
    except re.error:
        return pattern in container_name


def _strip_container_name(name: str) -> str:
    return name.lstrip("/")


def _truncate_line(line: str, max_len: int = 200) -> str:
    line = line.strip()
    if len(line) <= max_len:
        return line
    return line[: max_len - 3] + "..."


def _build_group_key(extract_rules: list, line: str) -> str | None:
    parts: list[str] = []
    for rule in extract_rules:
        try:
            match = re.search(rule.pattern, line)
        except re.error:
            continue
        if match:
            value = match.group(1) if match.groups() else match.group(0)
            parts.append(f"{rule.label}={value}")
    return ", ".join(parts) if parts else None


def _parse_log_timestamp(line: str) -> float | None:
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)",
        line,
    )
    if not match:
        return None
    raw = match.group(1).rstrip("Z")
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _find_container(client: docker.DockerClient, rule: ContainerRule):
    running_match = None
    stopped_match = None

    for container in client.containers.list(all=True):
        names = [
            _strip_container_name(container.name),
            _strip_container_name(container.attrs.get("Name", "")),
        ]
        if any(_matches_name(rule.match, name) for name in names if name):
            if container.status == "running":
                running_match = container
                break
            if stopped_match is None:
                stopped_match = container

    return running_match, stopped_match


def _scan_container_logs(
    container,
    rule: ContainerRule,
    since_ts: float,
    now: float,
) -> ContainerLogResult:
    try:
        error_re = re.compile(rule.error_pattern)
    except re.error as exc:
        return ContainerLogResult(
            name=rule.name,
            status="misconfigured",
            interval_label=_format_interval(now - since_ts),
        )

    logs = container.logs(since=int(since_ts), timestamps=True).decode(
        "utf-8", errors="replace"
    )

    error_count = 0
    groups: dict[str, int] = {}
    samples: list[str] = []

    for line in logs.splitlines():
        if not line.strip():
            continue

        content = line
        ts = _parse_log_timestamp(line)
        if ts is not None and ts < since_ts:
            continue

        if " " in line and ts is not None:
            content = line.split(" ", 1)[1]

        if not error_re.search(content):
            continue

        error_count += 1
        group_key = _build_group_key(rule.extract, content)
        if group_key:
            groups[group_key] = groups.get(group_key, 0) + 1
        else:
            groups["(unmatched)"] = groups.get("(unmatched)", 0) + 1

        if len(samples) < rule.max_samples:
            samples.append(_truncate_line(content))

    status = "errors" if error_count else "ok"
    return ContainerLogResult(
        name=rule.name,
        status=status,
        error_count=error_count,
        groups=groups,
        samples=samples,
        interval_label=_format_interval(now - since_ts),
    )


_FIRST_RUN_TAIL_BYTES = 1024 * 1024


def _scan_host_log(
    rule: HostLogRule,
    state_entry: dict | None,
    now: float,
    default_lookback_minutes: int,
) -> tuple[HostLogResult, dict]:
    file_path = Path(rule.path)
    if not file_path.is_file():
        return (
            HostLogResult(name=rule.name, status="not_found"),
            state_entry or {},
        )

    try:
        error_re = re.compile(rule.error_pattern)
    except re.error:
        return (
            HostLogResult(name=rule.name, status="misconfigured"),
            state_entry or {},
        )

    try:
        stat = file_path.stat()
    except OSError:
        return (
            HostLogResult(name=rule.name, status="not_found"),
            state_entry or {},
        )

    inode = stat.st_ino
    file_size = stat.st_size
    last_inode = state_entry.get("inode") if state_entry else None
    last_offset = int(state_entry.get("last_offset", 0)) if state_entry else 0

    if last_inode is None:
        last_offset = max(0, file_size - _FIRST_RUN_TAIL_BYTES)
        interval_label = _format_interval(default_lookback_minutes * 60)
    elif last_inode != inode or file_size < last_offset:
        last_offset = 0
        since_ts = float(state_entry.get("last_check_ts", now))
        interval_label = _format_interval(now - since_ts)
    else:
        since_ts = float(state_entry.get("last_check_ts", now))
        interval_label = _format_interval(now - since_ts)

    error_count = 0
    groups: dict[str, int] = {}
    samples: list[str] = []

    try:
        with file_path.open("rb") as handle:
            handle.seek(last_offset)
            raw = handle.read()
    except OSError:
        return (
            HostLogResult(
                name=rule.name,
                status="unavailable",
                interval_label=interval_label,
            ),
            {"inode": inode, "last_offset": file_size, "last_check_ts": now},
        )

    new_offset = last_offset + len(raw)
    text = raw.decode("utf-8", errors="replace")

    for line in text.splitlines():
        if not line.strip():
            continue

        if not error_re.search(line):
            continue

        error_count += 1
        group_key = _build_group_key(rule.extract, line)
        if group_key:
            groups[group_key] = groups.get(group_key, 0) + 1
        else:
            groups["(unmatched)"] = groups.get("(unmatched)", 0) + 1

        if len(samples) < rule.max_samples:
            samples.append(_truncate_line(line))

    status = "errors" if error_count else "ok"
    new_state = {
        "inode": inode,
        "last_offset": new_offset,
        "last_check_ts": now,
    }
    return (
        HostLogResult(
            name=rule.name,
            status=status,
            error_count=error_count,
            groups=groups,
            samples=samples,
            interval_label=interval_label,
        ),
        new_state,
    )


def _resolve_since_ts(
    state: dict,
    rule_name: str,
    now: float,
    default_lookback_minutes: int,
) -> tuple[float, str | None]:
    containers = state.get("containers", {})
    entry = containers.get(rule_name)
    if entry and "last_check_ts" in entry:
        since_ts = float(entry["last_check_ts"])
        interval_label = _format_interval(now - since_ts)
        return since_ts, interval_label

    since_ts = now - (default_lookback_minutes * 60)
    return since_ts, _format_interval(default_lookback_minutes * 60)


def collect_log_errors(
    enabled: bool,
    config_path: str,
    state_path: str,
    default_lookback_minutes: int,
) -> LogScanSummary:
    if not enabled:
        return LogScanSummary(status="disabled", message="disabled")

    config: LogChecksConfig | None
    try:
        config = load_log_checks(config_path)
    except Exception as exc:
        print(f"Log checks config error: {exc}", file=sys.stderr)
        return LogScanSummary(status="misconfigured", message=str(exc))

    if config is None:
        return LogScanSummary(status="disabled", message="config file not found")

    if not config.enabled or (not config.containers and not config.host_logs):
        return LogScanSummary(status="disabled", message="disabled")

    state = _load_log_state(state_path)
    now = time.time()
    results: list[ContainerLogResult] = []
    host_results: list[HostLogResult] = []
    global_interval: str | None = None

    docker_available = True
    if config.containers:
        try:
            client = docker.from_env()
            client.ping()
        except DockerException as exc:
            print(f"Docker unavailable: {exc}", file=sys.stderr)
            docker_available = False

    if config.containers and not docker_available:
        return LogScanSummary(
            status="unavailable",
            message="cannot connect to Docker",
        )

    client = docker.from_env() if config.containers else None

    for rule in config.containers:
        running, stopped = _find_container(client, rule)
        since_ts, interval_label = _resolve_since_ts(
            state,
            rule.name,
            now,
            default_lookback_minutes,
        )
        if global_interval is None:
            global_interval = interval_label

        if running is None and stopped is None:
            results.append(
                ContainerLogResult(
                    name=rule.name,
                    status="not_found",
                    interval_label=interval_label,
                )
            )
            continue

        if running is None:
            results.append(
                ContainerLogResult(
                    name=rule.name,
                    status="not_running",
                    interval_label=interval_label,
                )
            )
            state.setdefault("containers", {})[rule.name] = {"last_check_ts": now}
            continue

        result = _scan_container_logs(running, rule, since_ts, now)
        results.append(result)
        state.setdefault("containers", {})[rule.name] = {"last_check_ts": now}

    for rule in config.host_logs:
        state_entry = state.get("host_logs", {}).get(rule.name)
        result, new_entry = _scan_host_log(
            rule,
            state_entry,
            now,
            default_lookback_minutes,
        )
        host_results.append(result)
        state.setdefault("host_logs", {})[rule.name] = new_entry
        if global_interval is None and result.interval_label:
            global_interval = result.interval_label

    _save_log_state(state_path, state)

    return LogScanSummary(
        status="ok",
        containers=results,
        host_logs=host_results,
        interval_label=global_interval,
    )
