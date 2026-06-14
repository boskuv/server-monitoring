import json
import os
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
class Metrics:
    cpu: CpuMetrics
    memory: MemoryMetrics
    swap: SwapMetrics
    disks: list[DiskMount] = field(default_factory=list)
    network: list[NetworkInterface] = field(default_factory=list)
    system: SystemMetrics | None = None
    network_interval_label: str | None = None


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


def collect_cpu() -> CpuMetrics:
    psutil.cpu_percent(interval=1)
    load_1, load_5, load_15 = os.getloadavg()
    return CpuMetrics(
        percent=round(psutil.cpu_percent(interval=None), 1),
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
) -> Metrics:
    network, interval_label = collect_network(state_file)
    return Metrics(
        cpu=collect_cpu(),
        memory=collect_memory(),
        swap=collect_swap(),
        disks=collect_disks(),
        network=network,
        system=collect_system(hostname_override),
        network_interval_label=interval_label,
    )
