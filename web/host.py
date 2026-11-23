"""host.py - the machine Plex runs on, through Prometheus node_exporter's /metrics (the
Plex host connection, [plex_host]). Plex on its own machine reads the media over the
network, so Unraid's checks say nothing about it: this card does.

- network mounts (NFS, SMB): node_exporter gives up on one that doesn't answer within its
  mount timeout and reports it with node_filesystem_device_error, so a hung hard NFS
  mount is an error here instead of hanging the check. Mounts listed in the config are
  also an error when they're gone
- local disks: free space against [web] min_free_gb, and one remounted read-only (what
  Linux does when a disk has errors)
- load, memory, CPU temperature, uptime; CPU use and network throughput need two
  readings, so the last one per host is kept in memory (Sample) and the rate is over the
  time since then

node_exporter needs no setup beyond installing it (Ubuntu's prometheus-node-exporter
package). Read only. The parsers are pure, tests feed them text"""

import math
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from config.models import PlexHostConfig
    from web.status import ServiceStatus

TIMEOUT = 10
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p"}
LOCAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "bcachefs"}
# interfaces whose traffic is counted on another one too, or is the machine talking to itself
VIRTUAL_NETS = ("lo", "docker", "veth", "br-", "virbr", "cni", "flannel", "tailscale", "wg", "tun", "zt")
# the CPU running hot (Intel's limit is around 100°C)
HOT_CPU = 90.0
# load (15 minutes) per core that's more work than the machine keeps up with
LOAD_PER_CORE = 2.0
# memory available under this share of all of it
LOW_MEMORY = 0.05
# readings this close together are too noisy for a rate, this far apart too stale
RATE_MIN = timedelta(seconds=10)
RATE_MAX = timedelta(minutes=30)
RECENT_RESTART = timedelta(hours=1)

Labels = dict[str, str]
Metrics = dict[str, list[tuple[Labels, float]]]


class HostRefused(Exception):
    """node_exporter answered but refused, the message has no secrets in it"""


# ---------------------------------------------------------------------------
# the text format
# ---------------------------------------------------------------------------

LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?\s*$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


def parse_metrics(text: str) -> Metrics:
    """Prometheus' text format: {name: [(labels, value)]}. Lines it can't read are left out"""
    found: Metrics = defaultdict(list)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if (match := LINE.match(line)) is None:
            continue
        name, labels, value = match.groups()
        try:
            number = float(value)
        except ValueError:
            continue
        found[name].append((
            {k: re.sub(r'\\[\\"n]', lambda e: ESCAPES[e[0]], v) for k, v in LABEL.findall(labels or "")},
            number,
        ))
    return dict(found)


def value(metrics: Metrics, name: str, **labels: str) -> float | None:
    """the first value of a metric whose labels include these"""
    for found, number in metrics.get(name, []):
        if all(found.get(k) == v for k, v in labels.items()):
            return number
    return None


def label(metrics: Metrics, name: str, key: str) -> str:
    for found, _ in metrics.get(name, []):
        if found.get(key):
            return found[key]
    return ""


# ---------------------------------------------------------------------------
# rates: CPU use and network throughput between two readings
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    # node_time_seconds: the host's clock, so rates don't depend on how long the request took
    at: float
    cpu_idle: float
    cpu_total: float
    received: float
    sent: float


def counted_net(device: str) -> bool:
    return not device.startswith(VIRTUAL_NETS)


def sample(metrics: Metrics) -> Sample | None:
    at = value(metrics, "node_time_seconds")
    cpu = metrics.get("node_cpu_seconds_total", [])
    if at is None or not cpu:
        return None
    return Sample(
        at,
        sum(n for found, n in cpu if found.get("mode") in ("idle", "iowait")),
        sum(n for _, n in cpu),
        sum(n for found, n in metrics.get("node_network_receive_bytes_total", []) if counted_net(found.get("device", ""))),
        sum(n for found, n in metrics.get("node_network_transmit_bytes_total", []) if counted_net(found.get("device", ""))),
    )


@dataclass
class Rates:
    cpu_percent: float
    received_mbps: float
    sent_mbps: float


def rates(previous: Sample | None, current: Sample | None) -> Rates | None:
    """None without two readings a usable time apart, or when the counters went back (a restart)"""
    if previous is None or current is None:
        return None
    seconds = current.at - previous.at
    if not RATE_MIN.total_seconds() <= seconds <= RATE_MAX.total_seconds():
        return None
    total = current.cpu_total - previous.cpu_total
    idle = current.cpu_idle - previous.cpu_idle
    received, sent = current.received - previous.received, current.sent - previous.sent
    if total <= 0 or idle < 0 or received < 0 or sent < 0:
        return None
    return Rates(
        max(0.0, min(100.0, 100 * (1 - idle / total))),
        received * 8 / seconds / 1_000_000,
        sent * 8 / seconds / 1_000_000,
    )


_samples: dict[str, Sample] = {}
_samples_lock = threading.Lock()


def rates_since_last(key: str, current: Sample | None) -> Rates | None:
    """the rate since the last reading of this host, keeping a reading at least RATE_MIN
    old so a page reloaded every few seconds still gets one"""
    if current is None:
        return None
    with _samples_lock:
        previous = _samples.get(key)
        found = rates(previous, current)
        if previous is None or found is not None or not (0 <= current.at - previous.at < RATE_MIN.total_seconds()):
            _samples[key] = current
    return found


# ---------------------------------------------------------------------------
# what the card says
# ---------------------------------------------------------------------------


def _size(n: float) -> str:
    from web.status import size

    return size(n)


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def host_answers(
    status: "ServiceStatus", metrics: Metrics, mounts: list[str], min_free_gb: int, found_rates: Rates | None = None
) -> None:
    """node_exporter's metrics on the card"""
    if not metrics.get("node_filesystem_avail_bytes") and not metrics.get("node_cpu_seconds_total"):
        raise HostRefused("That URL doesn't answer with node_exporter's metrics: check it ends in :9100 (or its /metrics)")
    status.version = label(metrics, "node_exporter_build_info", "version") or None
    if name := label(metrics, "node_uname_info", "nodename"):
        status.facts.append(("Host", name))

    filesystems: dict[str, Labels] = {}
    for found, _ in metrics.get("node_filesystem_size_bytes", []):
        if (mountpoint := found.get("mountpoint")) and found.get("fstype") in NETWORK_FS | LOCAL_FS:
            filesystems.setdefault(mountpoint, found)
    # a stuck mount may only be in device_error: node_exporter stops reading its sizes
    for found, error in metrics.get("node_filesystem_device_error", []):
        if (mountpoint := found.get("mountpoint")) and found.get("fstype") in NETWORK_FS:
            filesystems.setdefault(mountpoint, found)

    network = sorted(m for m, f in filesystems.items() if f.get("fstype") in NETWORK_FS)
    for mountpoint in network:
        device = filesystems[mountpoint].get("device", "")
        if value(metrics, "node_filesystem_device_error", mountpoint=mountpoint) == 1:
            status.problem(
                "bad",
                f"{mountpoint}{f' ({device})' if device else ''} isn't answering: the mount is hung or its "
                "server can't be reached, so Plex can't read the media on it",
                f"mount:{mountpoint}",
            )
    for mountpoint in mounts:
        if mountpoint.rstrip("/") not in filesystems and mountpoint not in filesystems:
            status.problem("bad", f"{mountpoint} isn't mounted, so Plex can't read the media on it", f"mount:{mountpoint}")
    if network or mounts:
        status.facts.append(("Mounts", ", ".join(network) or "none"))

    disks = []
    for mountpoint, found in sorted(filesystems.items()):
        if found.get("fstype") not in LOCAL_FS or mountpoint.startswith("/boot"):
            continue
        if value(metrics, "node_filesystem_readonly", mountpoint=mountpoint) == 1:
            status.problem(
                "bad", f"{mountpoint} has been remounted read-only: the disk may have errors", f"readonly:{mountpoint}"
            )
        free = value(metrics, "node_filesystem_avail_bytes", mountpoint=mountpoint)
        total = value(metrics, "node_filesystem_size_bytes", mountpoint=mountpoint)
        if free is None or not total:
            continue
        status.low_space(int(free), mountpoint, min_free_gb)
        disks.append(f"{mountpoint} {_size(free)} free of {_size(total)}")
    if disks:
        status.facts.append(("Disks", ", ".join(disks)))

    cores = len([1 for found, _ in metrics.get("node_cpu_seconds_total", []) if found.get("mode") == "idle"])
    load = value(metrics, "node_load15")
    cpu = []
    if found_rates is not None:
        cpu.append(f"{found_rates.cpu_percent:.0f}%")
    if load is not None:
        cpu.append(f"load {load:.2f}" + (f" ({cores} cores)" if cores else ""))
        if cores and load >= LOAD_PER_CORE * cores:
            status.problem(
                "warn",
                f"The Plex host is overloaded: load {load:.1f} over 15 minutes on {cores} cores. "
                "CPU transcodes are the usual cause; a hung mount makes it climb too",
                "load",
            )
    if cpu:
        status.facts.append(("CPU", ", ".join(cpu)))

    available, memory = value(metrics, "node_memory_MemAvailable_bytes"), value(metrics, "node_memory_MemTotal_bytes")
    if available is not None and memory:
        status.facts.append(("Memory", f"{_size(memory - available)} used of {_size(memory)}"))
        if available < LOW_MEMORY * memory:
            status.problem("warn", f"The Plex host is almost out of memory: {_size(available)} available", "memory")

    temps = [n for found, n in metrics.get("node_thermal_zone_temp", []) if found.get("type") == "x86_pkg_temp"]
    if temps and not math.isnan(hottest := max(temps)):
        status.facts.append(("CPU temperature", f"{hottest:.0f}°C"))
        if hottest >= HOT_CPU:
            status.problem("warn", f"The Plex host's CPU is at {hottest:.0f}°C", "temp")

    if found_rates is not None:
        status.facts.append(("Network", f"{found_rates.received_mbps:.1f} Mbps in, {found_rates.sent_mbps:.1f} Mbps out"))

    now, booted = value(metrics, "node_time_seconds"), value(metrics, "node_boot_time_seconds")
    if now is not None and booted is not None and now >= booted:
        status.facts.append(("Up", _span(now - booted)))
        if now - booted < RECENT_RESTART.total_seconds():
            status.problem("info", f"The Plex host restarted {_span(now - booted)} ago")

    status.summary = " · ".join(
        part for part in (
            f"{len(network)} {'mount' if len(network) == 1 else 'mounts'} OK" if network and not status.problems else "",
            disks[0] if disks else "",
            cpu[0] if cpu else "",
        ) if part
    )


def read_metrics(url: str, username: str | None, password: str | None) -> Metrics:
    """node_exporter's metrics. Raises HostRefused or requests' errors"""
    url = url.rstrip("/")
    response = requests.get(
        url if url.endswith("/metrics") else f"{url}/metrics",
        auth=(username, password or "") if username else None,
        headers={"Accept": "text/plain"},
        timeout=TIMEOUT,
    )
    if response.status_code in (401, 403):
        raise HostRefused(f"node_exporter refused the username/password (HTTP {response.status_code})")
    response.raise_for_status()
    return parse_metrics(response.text)


def plex_host_status(config: "PlexHostConfig | None", min_free_gb: int) -> "ServiceStatus":
    from web.status import ServiceStatus

    status = ServiceStatus("plex_host", "Plex host")
    if config is None:
        status.state = "off"
        return status
    metrics = read_metrics(config.url, config.username, config.password)
    host_answers(status, metrics, config.mounts, min_free_gb, rates_since_last(config.url, sample(metrics)))
    status.problems.sort(key=lambda p: -{"bad": 2, "warn": 1}.get(p.level, 0))
    return status
