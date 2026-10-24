import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

SKIP_FSTYPES = frozenset(
    {
        "tmpfs",
        "devtmpfs",
        "squashfs",
        "overlay",
        "aufs",
        "efivarfs",
        "mqueue",
        "proc",
        "sysfs",
        "devpts",
        "securityfs",
        "cgroup",
        "cgroup2",
        "pstore",
        "bpf",
        "tracefs",
        "debugfs",
        "configfs",
        "fusectl",
        "hugetlbfs",
    }
)

SKIP_MOUNT_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/run/docker",
    "/var/lib/docker",
    "/var/lib/kubelet",
)

SKIP_MOUNT_EXACT = frozenset(
    {
        "/etc/resolv.conf",
        "/etc/hostname",
        "/etc/hosts",
    }
)

HOST_ROOT = Path("/host")
SKIP_NET_PREFIXES = ("veth", "br-")
PHYSICAL_NET_PREFIXES = ("en", "eth", "wlp", "wlan", "wwan", "docker")
TOP_CONSUMERS_MAX = 20
_CONTAINER_ID_RE = re.compile(
    r"(?:docker[-/]|cri-containerd-|crio-|libpod-)([0-9a-f]{64})"
)
_CONTAINER_ID_FALLBACK_RE = re.compile(r"/([0-9a-f]{64})(?:\.scope)?(?:/|$)")


@dataclass
class CpuMetrics:
    percent: float
    load_1: float
    load_5: float
    load_15: float
    cores: int


@dataclass
class MemoryMetrics:
    used_gb: float
    total_gb: float
    percent: float
    available_gb: float


@dataclass
class SwapMetrics:
    used_gb: float
    total_gb: float
    percent: float


@dataclass
class DiskMount:
    mountpoint: str
    used_gb: float
    total_gb: float
    percent: float


@dataclass
class NetworkInterface:
    name: str
    rx_bytes: int
    tx_bytes: int
    rx_mbps: float | None
    tx_mbps: float | None


@dataclass
class SystemMetrics:
    hostname: str
    uptime_seconds: int
    timestamp_utc: str


@dataclass
class ResourceConsumer:
    name: str
    kind: str  # container | process
    cpu_percent: float
    memory_mb: float
    memory_percent: float


@dataclass
class Metrics:
    cpu: CpuMetrics
    memory: MemoryMetrics
    swap: SwapMetrics
    disks: list[DiskMount] = field(default_factory=list)
    network: list[NetworkInterface] = field(default_factory=list)
    system: SystemMetrics | None = None
    network_interval_label: str | None = None
    top_cpu: list[ResourceConsumer] = field(default_factory=list)
    top_memory: list[ResourceConsumer] = field(default_factory=list)


def _bytes_to_gb(value: int | float) -> float:
    return round(value / (1024**3), 1)


def _should_skip_mount(mountpoint: str, fstype: str) -> bool:
    if mountpoint in SKIP_MOUNT_EXACT:
        return True
    if "/docker/overlay" in mountpoint or "/overlay2/" in mountpoint:
        return True
    if fstype in SKIP_FSTYPES:
        return True
    return any(mountpoint.startswith(prefix) for prefix in SKIP_MOUNT_PREFIXES)


def _normalize_mountpoint(mountpoint: str) -> str:
    if mountpoint.startswith("/host"):
        return mountpoint[len("/host"):] or "/"
    return mountpoint


def collect_cpu(processes: list[psutil.Process] | None = None) -> CpuMetrics:
    if processes:
        for proc in processes:
            try:
                proc.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    percent = psutil.cpu_percent(interval=1)
    load_1, load_5, load_15 = os.getloadavg()
    return CpuMetrics(
        percent=round(percent, 1),
        load_1=round(load_1, 2),
        load_5=round(load_5, 2),
        load_15=round(load_15, 2),
        cores=psutil.cpu_count() or 1,
    )


def collect_memory() -> MemoryMetrics:
    mem = psutil.virtual_memory()
    return MemoryMetrics(
        used_gb=_bytes_to_gb(mem.used),
        total_gb=_bytes_to_gb(mem.total),
        percent=round(mem.percent, 1),
        available_gb=_bytes_to_gb(mem.available),
    )


def collect_swap() -> SwapMetrics:
    swap = psutil.swap_memory()
    return SwapMetrics(
        used_gb=_bytes_to_gb(swap.used),
        total_gb=_bytes_to_gb(swap.total),
        percent=round(swap.percent, 1),
    )


def _disk_partitions() -> list[tuple[str, str]]:
    if HOST_ROOT.is_dir():
        mounts_file = HOST_ROOT / "proc" / "mounts"
        if mounts_file.is_file():
            partitions: list[tuple[str, str]] = []
            for line in mounts_file.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) < 3:
                    continue
                device, mountpoint, fstype = parts[0], parts[1], parts[2]
                if not device.startswith("/dev"):
                    continue
                if not mountpoint.startswith("/host"):
                    continue
                partitions.append((mountpoint, fstype))
            return partitions

    return [
        (partition.mountpoint, partition.fstype)
        for partition in psutil.disk_partitions(all=False)
    ]


def _disk_usage_path(mountpoint: str) -> str:
    if HOST_ROOT.is_dir() and mountpoint.startswith("/host"):
        return mountpoint
    if HOST_ROOT.is_dir():
        if mountpoint == "/":
            return str(HOST_ROOT)
        return str(HOST_ROOT / mountpoint.lstrip("/"))
    return mountpoint


def collect_disks() -> list[DiskMount]:
    seen: set[str] = set()
    mounts: list[DiskMount] = []

    for mountpoint, fstype in _disk_partitions():
        display_mount = _normalize_mountpoint(mountpoint)
        if _should_skip_mount(display_mount, fstype):
            continue
        if display_mount in seen:
            continue

        try:
            usage = psutil.disk_usage(_disk_usage_path(mountpoint))
        except (OSError, PermissionError):
            continue

        seen.add(display_mount)
        mounts.append(
            DiskMount(
                mountpoint=display_mount,
                used_gb=_bytes_to_gb(usage.used),
                total_gb=_bytes_to_gb(usage.total),
                percent=round(usage.percent, 1),
            )
        )

    mounts.sort(key=lambda m: m.mountpoint)
    return mounts


def _load_network_state(state_file: str) -> dict | None:
    path = Path(state_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_network_state(state_file: str, counters: dict[str, dict[str, int]]) -> None:
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": time.time(),
        "interfaces": counters,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def _bytes_to_mbps(delta_bytes: int, elapsed_seconds: float) -> float:
    if elapsed_seconds <= 0:
        return 0.0
    bits_per_second = (delta_bytes * 8) / elapsed_seconds
    return round(bits_per_second / 1_000_000, 1)


def collect_network(state_file: str) -> tuple[list[NetworkInterface], str | None]:
    current = psutil.net_io_counters(pernic=True)
    previous = _load_network_state(state_file)
    now = time.time()

    current_counters: dict[str, dict[str, int]] = {}
    interfaces: list[NetworkInterface] = []
    interval_label: str | None = None

    if previous and "interfaces" in previous and "timestamp" in previous:
        elapsed = now - previous["timestamp"]
        if elapsed > 0:
            interval_label = _format_interval(elapsed)

    for name, stats in sorted(current.items()):
        if name == "lo":
            continue
        if name.startswith(SKIP_NET_PREFIXES):
            continue
        if not name.startswith(PHYSICAL_NET_PREFIXES):
            continue

        current_counters[name] = {
            "rx_bytes": stats.bytes_recv,
            "tx_bytes": stats.bytes_sent,
        }

        rx_mbps: float | None = None
        tx_mbps: float | None = None

        if previous and interval_label:
            prev_iface = previous.get("interfaces", {}).get(name)
            if prev_iface:
                elapsed = now - previous["timestamp"]
                rx_delta = stats.bytes_recv - prev_iface.get("rx_bytes", 0)
                tx_delta = stats.bytes_sent - prev_iface.get("tx_bytes", 0)
                if rx_delta >= 0 and tx_delta >= 0:
                    rx_mbps = _bytes_to_mbps(rx_delta, elapsed)
                    tx_mbps = _bytes_to_mbps(tx_delta, elapsed)

        interfaces.append(
            NetworkInterface(
                name=name,
                rx_bytes=stats.bytes_recv,
                tx_bytes=stats.bytes_sent,
                rx_mbps=rx_mbps,
                tx_mbps=tx_mbps,
            )
        )

    _save_network_state(state_file, current_counters)
    return interfaces, interval_label


def _process_label(proc: psutil.Process) -> str:
    try:
        cmdline = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        cmdline = []

    if cmdline:
        base = Path(cmdline[0]).name or proc.name()
        if len(cmdline) > 1 and base in {"python", "python3", "java", "node", "ruby", "perl"}:
            arg = cmdline[1]
            if arg and not arg.startswith("-"):
                return f"{base} {Path(arg).name}"[:40]
        return base[:40]

    try:
        return proc.name()[:40]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return "unknown"


def _container_id(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _CONTAINER_ID_RE.search(text)
    if match:
        return match.group(1)
    match = _CONTAINER_ID_FALLBACK_RE.search(text)
    if match:
        return match.group(1)
    return None


def _container_names() -> dict[str, str]:
    try:
        import docker
        from docker.errors import DockerException
    except ImportError:
        return {}

    try:
        client = docker.from_env()
    except DockerException:
        return {}

    names: dict[str, str] = {}
    try:
        for container in client.containers.list():
            names[container.id] = container.name.lstrip("/")
    except DockerException:
        return {}
    finally:
        try:
            client.close()
        except DockerException:
            pass
    return names


def _prime_processes() -> list[psutil.Process]:
    return list(psutil.process_iter())


def collect_top_consumers(
    processes: list[psutil.Process],
    cores: int,
    total_memory_bytes: int,
    limit: int,
) -> tuple[list[ResourceConsumer], list[ResourceConsumer]]:
    if limit <= 0 or cores <= 0 or total_memory_bytes <= 0:
        return [], []

    names = _container_names()
    grouped: dict[str, dict] = {}

    for proc in processes:
        try:
            cpu_raw = proc.cpu_percent(interval=None)
            rss = proc.memory_info().rss
            pid = proc.pid
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        container_id = _container_id(pid)
        if container_id:
            key = f"c:{container_id}"
            kind = "container"
            name = names.get(container_id, container_id[:12])
        else:
            key = f"p:{pid}"
            kind = "process"
            suffix = f" [{pid}]"
            label = _process_label(proc)
            name = f"{label[: 40 - len(suffix)]}{suffix}"

        bucket = grouped.get(key)
        if bucket is None:
            grouped[key] = {
                "name": name,
                "kind": kind,
                "cpu_raw": cpu_raw,
                "rss": rss,
            }
        else:
            bucket["cpu_raw"] += cpu_raw
            bucket["rss"] += rss

    consumers: list[ResourceConsumer] = []
    for bucket in grouped.values():
        consumers.append(
            ResourceConsumer(
                name=bucket["name"],
                kind=bucket["kind"],
                cpu_percent=round(bucket["cpu_raw"] / cores, 1),
                memory_mb=round(bucket["rss"] / (1024**2), 1),
                memory_percent=round(bucket["rss"] / total_memory_bytes * 100, 1),
            )
        )

    top_cpu = [item for item in consumers if item.cpu_percent >= 0.1]
    top_cpu.sort(key=lambda item: item.cpu_percent, reverse=True)
    top_memory = [item for item in consumers if item.memory_mb >= 1]
    top_memory.sort(key=lambda item: item.memory_mb, reverse=True)
    return top_cpu[:limit], top_memory[:limit]


def collect_system(hostname_override: str | None = None) -> SystemMetrics:
    from datetime import datetime, timezone

    hostname = hostname_override or socket.gethostname()
    uptime_seconds = int(time.time() - psutil.boot_time())
    timestamp_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return SystemMetrics(
        hostname=hostname,
        uptime_seconds=uptime_seconds,
        timestamp_utc=timestamp_utc,
    )


def collect_all(
    state_file: str,
    hostname_override: str | None = None,
    top_consumers: int = 5,
) -> Metrics:
    limit = max(0, min(top_consumers, TOP_CONSUMERS_MAX))
    processes = _prime_processes() if limit else []
    cpu = collect_cpu(processes or None)
    memory = collect_memory()
    top_cpu: list[ResourceConsumer] = []
    top_memory: list[ResourceConsumer] = []
    if limit:
        top_cpu, top_memory = collect_top_consumers(
            processes,
            cores=cpu.cores,
            total_memory_bytes=psutil.virtual_memory().total or 1,
            limit=limit,
        )

    network, interval_label = collect_network(state_file)
    return Metrics(
        cpu=cpu,
        memory=memory,
        swap=collect_swap(),
        disks=collect_disks(),
        network=network,
        system=collect_system(hostname_override),
        network_interval_label=interval_label,
        top_cpu=top_cpu,
        top_memory=top_memory,
    )
