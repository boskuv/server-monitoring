import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from monitor.collectors import _container_id

CLEAN_EXIT_CODES = frozenset({0, 143})
_UP_STATUSES = frozenset({"running", "paused", "restarting"})
_MAX_EVENTS = 20


@dataclass
class ContainerEvent:
    name: str
    kind: str  # restarted | stopped | crashed | removed
    detail: str


@dataclass
class ContainerWatch:
    status: str  # disabled | unavailable | baseline | ok
    events: list[ContainerEvent] = field(default_factory=list)
    message: str | None = None
    new_state: dict | None = None


def _load_state(path: str) -> dict | None:
    state_path = Path(path)
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    containers = data.get("containers")
    if not isinstance(containers, dict):
        return None
    return data


def save_container_state(path: str, state: dict) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _real_timestamp(value: str) -> bool:
    return bool(value) and not value.startswith("0001-")


def _format_docker_time(value: str) -> str | None:
    if not _real_timestamp(value):
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Docker uses nanoseconds; datetime accepts microseconds.
    if "." in text:
        head, tail = text.split(".", 1)
        frac, sep, tz = "", "", ""
        if "+" in tail:
            frac, tz = tail.split("+", 1)
            sep = "+"
        elif tail.count("-") == 1:
            frac, tz = tail.split("-", 1)
            sep = "-"
        else:
            frac = tail
        text = f"{head}.{frac[:6]}{sep}{tz}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _record_from_attrs(attrs: dict) -> dict:
    state = attrs.get("State") or {}
    container_id = str(attrs.get("Id") or "")
    name = str(attrs.get("Name") or "").lstrip("/") or container_id[:12] or "unknown"
    return {
        "name": name,
        "status": str(state.get("Status") or "unknown"),
        "running": bool(state.get("Running")),
        "restart_count": _as_int(attrs.get("RestartCount")),
        "started_at": str(state.get("StartedAt") or ""),
        "finished_at": str(state.get("FinishedAt") or ""),
        "exit_code": _as_int(state.get("ExitCode")),
        "oom_killed": bool(state.get("OOMKilled")),
    }


def _was_up(record: dict) -> bool:
    return bool(record.get("running")) or record.get("status") in _UP_STATUSES


def _is_crash(record: dict) -> bool:
    if record.get("status") in {"restarting", "dead"}:
        return True
    if record.get("oom_killed") and not record.get("running"):
        return True
    if record.get("running"):
        return False
    if record.get("status") in {"exited", "removing", "dead"}:
        return record.get("exit_code") not in CLEAN_EXIT_CODES
    return False


def _crash_detail(record: dict, restart_delta: int) -> str:
    if record.get("status") == "restarting":
        if restart_delta > 1:
            return f"crash-looping (restarted {restart_delta} times)"
        if restart_delta == 1:
            return "crash-looping (restarted once)"
        return "crash-looping"

    extras: list[str] = []
    if record.get("oom_killed"):
        extras.append("OOM")
    exit_code = record.get("exit_code")
    if exit_code not in CLEAN_EXIT_CODES:
        extras.append(f"exit {exit_code}")
    when = _format_docker_time(str(record.get("finished_at") or ""))
    if when:
        extras.append(when)
    if restart_delta > 1:
        extras.append(f"restarted {restart_delta} times")
    elif restart_delta == 1:
        extras.append("restarted once")
    if extras:
        return "crashed (" + ", ".join(extras) + ")"
    return "crashed"


def _stop_detail(record: dict, restart_delta: int) -> str:
    when = _format_docker_time(str(record.get("finished_at") or ""))
    detail = f"stopped at {when}" if when else "stopped"
    if restart_delta > 1:
        return f"{detail} (restarted {restart_delta} times)"
    if restart_delta == 1:
        return f"{detail} (restarted once)"
    return detail


def _restart_detail(restart_delta: int) -> str:
    if restart_delta > 1:
        return f"restarted {restart_delta} times"
    return "restarted"


def _event_for_change(prev: dict, curr: dict) -> ContainerEvent | None:
    restart_delta = max(0, _as_int(curr.get("restart_count")) - _as_int(prev.get("restart_count")))
    started_changed = (
        _real_timestamp(str(curr.get("started_at") or ""))
        and curr.get("started_at") != prev.get("started_at")
    )
    status_changed = curr.get("status") != prev.get("status")
    finish_changed = curr.get("finished_at") != prev.get("finished_at") and _real_timestamp(
        str(curr.get("finished_at") or "")
    )
    oom_new = bool(curr.get("oom_killed")) and not bool(prev.get("oom_killed"))
    if not (restart_delta or started_changed or status_changed or finish_changed or oom_new):
        return None

    name = str(curr.get("name") or "unknown")
    if _is_crash(curr):
        return ContainerEvent(name, "crashed", _crash_detail(curr, restart_delta))

    if (
        _was_up(prev)
        and not curr.get("running")
        and curr.get("status") in {"exited", "dead", "removing"}
    ):
        return ContainerEvent(name, "stopped", _stop_detail(curr, restart_delta))

    if restart_delta or (started_changed and curr.get("running")):
        return ContainerEvent(name, "restarted", _restart_detail(restart_delta))
    return None


def diff_containers(previous: dict[str, dict], current: dict[str, dict]) -> list[ContainerEvent]:
    events: list[ContainerEvent] = []
    current_names = {str(record.get("name") or "") for record in current.values()}

    for container_id, prev in previous.items():
        curr = current.get(container_id)
        if curr is None:
            name = str(prev.get("name") or container_id[:12])
            if name in current_names:
                continue
            if _was_up(prev):
                events.append(ContainerEvent(name, "removed", "removed"))
            continue
        event = _event_for_change(prev, curr)
        if event is not None:
            events.append(event)

    for container_id, curr in current.items():
        if container_id in previous:
            continue
        # The container did not exist at the last check, so its whole lifetime
        # happened since then. RestartCount > 0 means it already went down.
        name = str(curr.get("name") or container_id[:12])
        restart_delta = _as_int(curr.get("restart_count"))
        if _is_crash(curr):
            events.append(ContainerEvent(name, "crashed", _crash_detail(curr, restart_delta)))
        elif restart_delta > 0 and not curr.get("running"):
            events.append(ContainerEvent(name, "stopped", _stop_detail(curr, restart_delta)))
        elif restart_delta > 0:
            events.append(ContainerEvent(name, "restarted", _restart_detail(restart_delta)))

    kind_order = {"crashed": 0, "stopped": 1, "removed": 2, "restarted": 3}
    events.sort(key=lambda event: (kind_order.get(event.kind, 9), event.name))
    if len(events) > _MAX_EVENTS:
        hidden = len(events) - _MAX_EVENTS
        events = events[:_MAX_EVENTS]
        events.append(ContainerEvent(f"+{hidden} more", "restarted", "not shown"))
    return events


def _snapshot(client, self_id: str | None) -> dict[str, dict]:
    from docker.errors import DockerException

    snapshot: dict[str, dict] = {}
    for container in client.containers.list(all=True):
        container_id = container.id
        if self_id and container_id.startswith(self_id):
            continue
        try:
            attrs = client.api.inspect_container(container_id)
        except DockerException:
            continue
        full_id = str(attrs.get("Id") or container_id)
        if self_id and full_id.startswith(self_id):
            continue
        snapshot[full_id] = _record_from_attrs(attrs)
    return snapshot


def collect_container_watch(enabled: bool, state_path: str) -> ContainerWatch:
    if not enabled:
        return ContainerWatch(status="disabled")

    try:
        import docker
        from docker.errors import DockerException
    except ImportError:
        return ContainerWatch(status="unavailable", message="docker SDK is not installed")

    client = None
    try:
        client = docker.from_env()
        client.ping()
        current = _snapshot(client, _container_id(os.getpid()))
    except DockerException as exc:
        message = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else "cannot connect to Docker"
        return ContainerWatch(status="unavailable", message=message)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    new_state = {"containers": current}
    previous = _load_state(state_path)
    if previous is None:
        return ContainerWatch(status="baseline", new_state=new_state)

    events = diff_containers(previous.get("containers") or {}, current)
    return ContainerWatch(status="ok", events=events, new_state=new_state)
