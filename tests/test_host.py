"""Tests for the Plex host card: node_exporter's metrics"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from config import build_config
from config.models import ConfigSettings, PlexHostConfig
from tests.test_status import NOW
from web import host, status
from web.host import HostRefused, Rates, Sample, host_answers, parse_metrics, rates, sample
from web.status import ServiceStatus

GB = 1024**3
HOST = PlexHostConfig(url="http://plexbox:9100", username="helperr", password="host-secret")


def metrics_text(
    data_error=0, backup_error=0, root_free=621 * GB, root_ro=0, load15=0.52, available=20 * GB, temp=54.0,
    time=1_795_400_000.0, boot=1_794_000_000.0, idle=1000.0, received=5e9, sent=2e9, without_data=False,
):
    """what node_exporter on an Ubuntu box with two NFS mounts from Unraid answers, trimmed"""
    data = [] if without_data else [
        f'node_filesystem_avail_bytes{{device="192.168.1.15:/mnt/user/data",fstype="nfs4",mountpoint="/mnt/unraid/data"}} {40e12}',
        f'node_filesystem_size_bytes{{device="192.168.1.15:/mnt/user/data",fstype="nfs4",mountpoint="/mnt/unraid/data"}} {200e12}',
        f'node_filesystem_device_error{{device="192.168.1.15:/mnt/user/data",fstype="nfs4",mountpoint="/mnt/unraid/data"}} {data_error}',
    ]
    return "\n".join([
        "# HELP node_exporter_build_info A metric with a constant '1' value.",
        "# TYPE node_exporter_build_info gauge",
        'node_exporter_build_info{branch="debian/sid",goversion="go1.24",revision="",tags="",version="1.9.1"} 1',
        'node_uname_info{domainname="(none)",machine="x86_64",nodename="rattplex",release="7.0.0-42-generic",sysname="Linux",version="#1"} 1',
        'node_os_info{id="ubuntu",name="Ubuntu",pretty_name="Ubuntu 26.04.1 LTS",version_id="26.04"} 1',
        *data,
        'node_filesystem_device_error{device="192.168.1.15:/mnt/user/backup",fstype="nfs4",mountpoint="/mnt/unraid/backup"} '
        f"{backup_error}",
        f'node_filesystem_avail_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {root_free}',
        f'node_filesystem_size_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {916 * GB}',
        f'node_filesystem_readonly{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {root_ro}',
        f'node_filesystem_avail_bytes{{device="/dev/nvme0n1p1",fstype="vfat",mountpoint="/boot/efi"}} {505e6}',
        f'node_filesystem_size_bytes{{device="/dev/nvme0n1p1",fstype="vfat",mountpoint="/boot/efi"}} {511e6}',
        f'node_filesystem_size_bytes{{device="tmpfs",fstype="tmpfs",mountpoint="/run"}} {3e9}',
        *(f'node_cpu_seconds_total{{cpu="{c}",mode="{m}"}} {idle if m == "idle" else 100.0}'
          for c in range(4) for m in ("idle", "iowait", "system", "user")),
        f"node_load15 {load15}",
        f"node_memory_MemAvailable_bytes {available}",
        f"node_memory_MemTotal_bytes {32 * GB}",
        f'node_thermal_zone_temp{{type="x86_pkg_temp",zone="1"}} {temp}',
        f'node_thermal_zone_temp{{type="acpitz",zone="0"}} 27.8',
        f'node_network_receive_bytes_total{{device="enp2s0"}} {received}',
        f'node_network_transmit_bytes_total{{device="enp2s0"}} {sent}',
        'node_network_receive_bytes_total{device="lo"} 9e12',
        'node_network_transmit_bytes_total{device="docker0"} 9e12',
        f"node_time_seconds {time}",
        f"node_boot_time_seconds {boot}",
    ])


def card(text=None, mounts=(), min_free_gb=100, found_rates=None):
    s = ServiceStatus("plex_host", "Plex host")
    host_answers(s, parse_metrics(text or metrics_text()), list(mounts), min_free_gb, found_rates)
    return s


class TestParse:
    def test_labels_values_and_escapes(self):
        found = parse_metrics(
            '# comment\nup 1\nx{a="b",c="say \\"hi\\"\\\\"} 2.5e3\nbad line here\ny{a="1"} NaN\nz 3 1700000000000\n'
        )
        assert found["up"] == [({}, 1.0)]
        assert found["x"] == [({"a": "b", "c": 'say "hi"\\'}, 2500.0)]
        assert "bad" not in found
        assert found["z"] == [({}, 3.0)]


class TestCard:
    def test_healthy(self):
        result = card(found_rates=Rates(12.4, 45.1, 3.25))
        assert result.state == "ok"
        assert result.problems == []
        # the machine's OS, not node_exporter's version
        assert result.version == "Ubuntu 26.04.1 LTS"
        facts = dict(result.facts)
        assert facts["Host"] == "rattplex"
        assert (facts["Kernel"], facts["node_exporter"]) == ("7.0.0-42-generic", "1.9.1")
        assert facts["Mounts"] == "/mnt/unraid/backup, /mnt/unraid/data"
        # only local disks, never /boot or tmpfs
        assert facts["Disks"] == "/ 621.0 GB free of 916.0 GB"
        assert facts["CPU"] == "12%, load 0.52 (4 cores)"
        assert facts["Memory"] == "12.0 GB used of 32.0 GB"
        assert facts["CPU temperature"] == "54°C"
        assert facts["Network"] == "45.1 Mbps in, 3.2 Mbps out"
        assert facts["Up"] == "16d 4h"
        assert result.summary == "2 mounts OK · / 621.0 GB free of 916.0 GB · 12%"

    def test_a_hung_mount(self):
        # node_exporter stops reading a stuck mount's sizes, its device_error says so
        result = card(metrics_text(data_error=1))
        assert result.state == "bad"
        [problem] = result.problems
        assert problem.key == "mount:/mnt/unraid/data"
        assert problem.text.startswith("/mnt/unraid/data (192.168.1.15:/mnt/user/data) isn't answering")

    def test_a_stuck_mount_only_in_device_error(self):
        result = card(metrics_text(backup_error=1))
        assert [p.key for p in result.problems] == ["mount:/mnt/unraid/backup"]

    def test_a_listed_mount_thats_gone(self):
        result = card(metrics_text(without_data=True), mounts=["/mnt/unraid/data/", "/mnt/unraid/backup"])
        assert [(p.level, p.key) for p in result.problems] == [("bad", "mount:/mnt/unraid/data/")]
        assert "isn't mounted" in result.problems[0].text

    def test_no_network_mounts_at_all_is_likely_left_out(self):
        # what ubuntu's node_exporter answers without the mount-points-exclude change: no /mnt
        text = "\n".join(line for line in metrics_text().splitlines() if "/mnt/" not in line)
        result = card(text, mounts=["/mnt/unraid/data"])
        assert [(p.level, p.key) for p in result.problems] == [("warn", "mount:/mnt/unraid/data")]
        assert "--collector.filesystem.mount-points-exclude" in result.problems[0].text
        assert dict(result.facts)["Mounts"] == "none"

    def test_a_bind_mount_of_a_disk_already_listed(self):
        snap = "/var/snap/firefox/common/host-hunspell"
        text = metrics_text() + "\n" + "\n".join([
            f'node_filesystem_avail_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="{snap}"}} {621 * GB}',
            f'node_filesystem_size_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="{snap}"}} {916 * GB}',
        ])
        assert dict(card(text).facts)["Disks"] == "/ 621.0 GB free of 916.0 GB"

    def test_read_only_root(self):
        result = card(metrics_text(root_ro=1))
        assert [(p.level, p.key) for p in result.problems] == [("bad", "readonly:/")]

    def test_low_space_uses_min_free_gb(self):
        result = card(metrics_text(root_free=50 * GB), min_free_gb=100)
        assert [(p.level, p.key) for p in result.problems] == [("warn", "disk:/")]
        assert card(metrics_text(root_free=50 * GB), min_free_gb=0).problems == []

    @pytest.mark.parametrize("kwargs, key", [
        ({"load15": 9.0}, "load"),
        ({"available": 1 * GB}, "memory"),
        ({"temp": 95.0}, "temp"),
    ])
    def test_warnings(self, kwargs, key):
        result = card(metrics_text(**kwargs))
        assert [(p.level, p.key) for p in result.problems] == [("warn", key)]

    def test_a_recent_restart_is_a_note(self):
        result = card(metrics_text(time=1_795_400_000.0, boot=1_795_400_000.0 - 600))
        assert result.state == "ok"
        assert [(p.level, p.text) for p in result.problems] == [("info", "The Plex host restarted 10m ago")]

    def test_without_os_info_the_kernel(self):
        text = "\n".join(line for line in metrics_text().splitlines() if not line.startswith("node_os_info"))
        result = card(text)
        assert result.version == "Linux 7.0.0-42-generic" and "Kernel" not in dict(result.facts)

    def test_not_node_exporter(self):
        with pytest.raises(HostRefused):
            card("up 1\nsomething_else 2")


class TestRates:
    def test_between_two_readings(self):
        first = sample(parse_metrics(metrics_text(time=1000.0, idle=1000.0, received=0.0, sent=0.0)))
        # 60s later each core's idle went up 45s and nothing else did: not busy at all
        later = sample(parse_metrics(metrics_text(time=1060.0, idle=1045.0, received=75e6, sent=15e6)))
        found = rates(first, later)
        assert found.cpu_percent == 0.0
        assert (round(found.received_mbps, 1), round(found.sent_mbps, 1)) == (10.0, 2.0)

    def test_too_close_too_far_or_restarted(self):
        a = Sample(1000.0, 10.0, 20.0, 100.0, 100.0)
        assert rates(a, Sample(1005.0, 11.0, 22.0, 200.0, 200.0)) is None
        assert rates(a, Sample(1000.0 + 3600, 11.0, 22.0, 200.0, 200.0)) is None
        assert rates(a, Sample(1060.0, 1.0, 2.0, 5.0, 5.0)) is None
        assert rates(None, a) is None

    def test_cpu_use(self):
        found = rates(Sample(0.0, 100.0, 200.0, 0.0, 0.0), Sample(60.0, 130.0, 300.0, 0.0, 0.0))
        assert found.cpu_percent == 70.0

    def test_kept_per_host_and_not_replaced_by_a_quick_reload(self):
        host._samples.clear()
        assert host.rates_since_last("h", Sample(0.0, 100.0, 200.0, 0.0, 0.0)) is None
        # a reload 5s later has no rate yet and keeps the older reading
        assert host.rates_since_last("h", Sample(5.0, 101.0, 202.0, 0.0, 0.0)) is None
        assert host.rates_since_last("h", Sample(60.0, 130.0, 300.0, 0.0, 0.0)).cpu_percent == 70.0
        host._samples.clear()


def response(text="", code=200):
    r = MagicMock()
    r.status_code = code
    r.text = text
    r.raise_for_status.side_effect = requests.exceptions.HTTPError(f"{code} Error") if code >= 400 else None
    return r


class TestStatus:
    def test_reads_metrics_with_basic_auth(self):
        host._samples.clear()
        with patch.object(requests, "get", return_value=response(metrics_text())) as get:
            result = host.plex_host_status(HOST)
        assert result.state == "ok"
        url, kwargs = get.call_args.args[0], get.call_args.kwargs
        assert url == "http://plexbox:9100/metrics"
        assert kwargs["auth"] == ("helperr", "host-secret")
        host._samples.clear()

    def test_without_auth_and_a_metrics_url(self):
        with patch.object(requests, "get", return_value=response(metrics_text())) as get:
            host.plex_host_status(PlexHostConfig(url="http://plexbox:9100/metrics/"))
        assert get.call_args.args[0] == "http://plexbox:9100/metrics"
        assert get.call_args.kwargs["auth"] is None

    def test_off_without_the_connection(self):
        assert host.plex_host_status(None).state == "off"

    def test_refused_and_down_in_check_all(self):
        settings = ConfigSettings(plex=None, radarr=None, sonarr=None, plex_host=HOST)
        with patch.object(requests, "get", return_value=response(code=401)):
            [result] = [s for s in status.check_all(settings, NOW) if s.service == "plex_host"]
        assert (result.state, result.error) == ("bad", "node_exporter refused the username/password (HTTP 401)")
        with patch.object(requests, "get", side_effect=requests.exceptions.ConnectionError("http://plexbox:9100 host-secret")):
            [result] = [s for s in status.check_all(settings, NOW) if s.service == "plex_host"]
        assert result.error == "Couldn't connect"


class TestConfig:
    def test_built_from_the_document(self):
        settings = build_config({"plex_host": {"url": "http://plexbox:9100", "mounts": ["/mnt/unraid/data"]}})
        assert settings.plex_host.mounts == ["/mnt/unraid/data"]
        assert build_config({}).plex_host is None

    def test_its_own_low_space_threshold(self):
        """the machine holds Plex's data, not the media: [web] min_free_gb (750 GB for the
        library's disks) would always be under"""
        assert PlexHostConfig(url="http://plexbox:9100").min_free_gb == 50
        settings = build_config({"web": {"min_free_gb": 750}, "plex_host": {"url": "http://plexbox:9100"}})
        with patch.object(requests, "get", return_value=response(metrics_text(root_free=621 * GB))):
            [result] = [s for s in status.check_all(settings, NOW) if s.service == "plex_host"]
        assert result.problems == []
        with pytest.raises(ValueError, match="min_free_gb"):
            build_config({"plex_host": {"url": "http://plexbox:9100", "min_free_gb": -1}})

    def test_mounts_are_absolute_paths(self):
        with pytest.raises(ValueError, match="absolute"):
            build_config({"plex_host": {"url": "http://plexbox:9100", "mounts": ["mnt/data"]}})

    def test_the_password_is_masked(self):
        assert "host-secret" not in repr(HOST)
        assert "host-secret" not in repr(ConfigSettings(plex=None, radarr=None, sonarr=None, plex_host=HOST))

    def test_the_password_is_a_secret(self):
        from config.document import SECRET_FIELDS

        assert SECRET_FIELDS["plex_host.password"] == ("plex_host", "password")
