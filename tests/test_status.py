"""Tests for the status page: what radarr, sonarr, sabnzbd, seerr and plex report"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import requests
from fastapi.testclient import TestClient

from config.models import ArrConfig, BazarrConfig, ConfigSettings, LinkConfig, SabnzbdConfig, SeerrConfig, TautulliConfig, UnraidConfig
from tests.test_web import ORIGIN, FakeKeys, auth, fake_run_job
from tests.test_web import settings  # noqa: F401 (fixture)
from web import health, status
from web.app import create_app
from web.status import Item, ServiceStatus, check_all, size

NOW = datetime(2026, 10, 25, 12)
RADARR = ArrConfig(baseurl="http://radarr:7878/", apikey="radarr-key", tag=None)
SONARR = ArrConfig(baseurl="http://sonarr:8989", apikey="sonarr-key", tag=None)
SAB = SabnzbdConfig(url="http://sab:8080", apikey="sab-key")
SEERR = SeerrConfig(url="http://seerr:5055", apikey="seerr-key")
TAUTULLI = TautulliConfig(url="http://tautulli:8181", apikey="tautulli-key")
BAZARR = BazarrConfig(url="http://bazarr:6767/subs", apikey="bazarr-key")
UNRAID = UnraidConfig(url="http://unraid", apikey="unraid-key")


def response(body=None, code=200):
    r = MagicMock()
    r.status_code = code
    r.json.return_value = body
    r.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(f"{code} Error for url http://x?apikey=sab-key") if code >= 400 else None
    )
    return r


class FakeServices:
    """answers by path (and sabnzbd's mode, tautulli's cmd), records every call"""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, timeout, headers=None, params=None):
        self.calls.append((url, headers, params))
        path = urlparse(url).path
        key = params.get("mode") or params.get("cmd") if path in ("/api", "/api/v2") else path
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, MagicMock) else response(answer)


def arr_answers(**overrides):
    return {
        "/api/v3/system/status": {"version": "6.4.4"},
        "/api/v3/health": [],
        "/api/v3/queue": {"totalRecords": 0, "records": []},
        "/api/v3/wanted/missing": {"totalRecords": 132},
        "/api/v3/wanted/cutoff": {"totalRecords": 867},
        "/api/v3/rootfolder": [
            {"path": "/data/movies", "accessible": True, "freeSpace": 37 * 1024**4},
            {"path": "/data/kids", "accessible": True, "freeSpace": 37 * 1024**4},
        ],
        **overrides,
    }


def run(check, answers):
    fake = FakeServices(answers)
    with patch.object(status.requests, "get", side_effect=fake):
        return check(), fake


def test_size():
    assert size(512) == "512 B"
    assert size(1536) == "1.5 KB"
    assert size(37 * 1024**4) == "37.0 TB"
    assert size(None) == "-"


# ---------------------------------------------------------------------------
# radarr / sonarr
# ---------------------------------------------------------------------------


class TestArr:
    def test_healthy(self):
        result, fake = run(lambda: status.arr_status("radarr", "Radarr", RADARR), arr_answers())
        assert result.state == "ok"
        assert result.version == "6.4.4"
        assert dict(result.facts) == {"Queue": "0", "Missing": "132", "Cutoff unmet": "867", "Free": "37.0 TB"}
        url, headers, params = fake.calls[2]
        assert url == "http://radarr:7878/api/v3/queue"
        assert headers == {"X-Api-Key": "radarr-key"}
        assert params["includeMovie"] == "true"

    def test_sonarr_includes_series_and_episode(self):
        _, fake = run(lambda: status.arr_status("sonarr", "Sonarr", SONARR), arr_answers())
        params = fake.calls[2][2]
        assert params["includeSeries"] == params["includeEpisode"] == "true"

    def test_not_connected(self):
        assert status.arr_status("radarr", "Radarr", None).state == "off"
        assert status.arr_status("radarr", "Radarr", ArrConfig(baseurl=None, apikey=None, tag=None)).state == "off"

    def test_health_checks_set_the_state(self):
        health = [
            {"source": "IndexerCheck", "type": "warning", "message": "All indexers are unavailable"},
            {"source": "UpdateCheck", "type": "notice", "message": "New update"},
        ]
        result, _ = run(lambda: status.arr_status("radarr", "Radarr", RADARR), arr_answers(**{"/api/v3/health": health}))
        assert result.state == "warn"
        assert [(p.level, p.text) for p in result.problems] == [
            ("warn", "All indexers are unavailable"),
            ("info", "New update"),
        ]

    def test_inaccessible_root_folder_is_bad(self):
        roots = [{"path": "/data/movies", "accessible": False}]
        result, _ = run(lambda: status.arr_status("radarr", "Radarr", RADARR), arr_answers(**{"/api/v3/rootfolder": roots}))
        assert result.state == "bad"
        assert result.problems[0].text == "Root folder /data/movies isn't accessible"

    def test_free_space_per_disk_when_they_differ(self):
        roots = [
            {"path": "/a", "accessible": True, "freeSpace": 1024**4},
            {"path": "/b", "accessible": True, "freeSpace": 2 * 1024**4},
        ]
        result, _ = run(lambda: status.arr_status("radarr", "Radarr", RADARR), arr_answers(**{"/api/v3/rootfolder": roots}))
        assert [v for k, v in result.facts if k == "Free"] == ["1.0 TB (/a)", "2.0 TB (/b)"]

    def test_low_disk_space(self):
        roots = [
            {"path": "/a", "accessible": True, "freeSpace": 50 * 1024**3},
            {"path": "/b", "accessible": True, "freeSpace": 50 * 1024**3},
            {"path": "/c", "accessible": True, "freeSpace": 2 * 1024**4},
        ]
        answers = arr_answers(**{"/api/v3/rootfolder": roots})
        result, _ = run(lambda: status.arr_status("radarr", "Radarr", RADARR, 100), answers)
        assert result.state == "warn"
        assert [(p.text, p.key) for p in result.problems] == [
            ("Low disk space: 50.0 GB free on /a, /b, under the 100 GB minimum", "disk:/a, /b")
        ]
        # 0 is off
        result, _ = run(lambda: status.arr_status("radarr", "Radarr", RADARR, 0), answers)
        assert result.problems == []


class TestArrQueue:
    def test_stuck_imports_need_attention(self):
        records = [
            {"trackedDownloadState": "downloading", "trackedDownloadStatus": "ok", "movie": {"title": "Heat", "year": 1995}},
            {
                "trackedDownloadState": "importPending",
                "trackedDownloadStatus": "warning",
                "movie": {"title": "Fatherland", "year": 2026},
                "statusMessages": [{"title": "Fatherland.mkv", "messages": ["Not a Custom Format upgrade"]}],
            },
            {
                "trackedDownloadState": "failedPending",
                "trackedDownloadStatus": "error",
                "series": {"title": "Survivor"},
                "episode": {"seasonNumber": 49, "episodeNumber": 3},
                "errorMessage": "Download failed",
            },
        ]
        result = ServiceStatus("radarr", "Radarr")
        status.arr_queue(result, records, 3)
        assert result.items == [
            Item("Fatherland (2026)", "Not a Custom Format upgrade", level="warn", states=["importPending"]),
            Item("Survivor S49E03", "Download failed", level="bad", states=["failedPending"]),
        ]
        assert result.state == "bad"
        assert dict(result.facts)["Queue"] == "3, 1 downloading, 2 need attention"

    def test_a_season_pack_is_one_download(self):
        # sonarr lists a season pack as a record per episode: one row, one incident
        pack = [
            {
                "id": n,
                "downloadId": "SABnzbd_nzo_pack",
                "trackedDownloadState": "importPending",
                "trackedDownloadStatus": "warning",
                "series": {"title": "Big Brother (US)"},
                "episode": {"seasonNumber": 3, "episodeNumber": n},
                "statusMessages": [{"title": "pack", "messages": ["Episode file already imported"]}],
            }
            for n in (18, 19, 20)
        ]
        downloading = [{"id": 30 + n, "downloadId": "nzo_other", "trackedDownloadState": "downloading"} for n in range(4)]
        result = ServiceStatus("sonarr", "Sonarr")
        status.arr_queue(result, pack + downloading, 7)
        assert result.items == [
            Item(
                "Big Brother (US) S03E18 and 2 more", "Episode file already imported", level="warn",
                key="SABnzbd_nzo_pack", states=["importPending"],
            )
        ]
        assert dict(result.facts)["Queue"] == "7, 1 downloading, 1 need attention"
        assert health.item_key(result.items[0]) == "queue:SABnzbd_nzo_pack"

    def test_title_falls_back_to_the_release(self):
        assert status.arr_title({"title": "Some.Release.1080p"}) == "Some.Release.1080p"
        assert status.arr_title({"series": {"title": "Bluey"}}) == "Bluey"

    def test_a_warning_without_messages_says_its_state(self):
        result = ServiceStatus("sonarr", "Sonarr")
        status.arr_queue(result, [{"trackedDownloadState": "importBlocked", "title": "x"}], 1)
        assert result.items[0].detail == "importBlocked"


# ---------------------------------------------------------------------------
# sabnzbd
# ---------------------------------------------------------------------------


def sab_answers(queue=None, failed=(), warnings=()):
    return {
        "queue": {
            "queue": {
                "version": "5.1.3",
                "status": "Downloading",
                "paused": False,
                "noofslots_total": 1,
                "kbpersec": "10240",
                "mbleft": "1024",
                "timeleft": "0:01:40",
                "diskspace1": "500.5",
                "diskspace2": "33000",
                "slots": [{"filename": "Heat.1995", "status": "Downloading", "percentage": "42", "cat": "movies", "timeleft": "0:01:40"}],
                **(queue or {}),
            }
        },
        "history": {"history": {"slots": list(failed)}},
        "warnings": {"warnings": list(warnings)},
    }


def ts(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


class TestSabnzbd:
    def test_low_disk_space(self):
        result, _ = run(lambda: status.sabnzbd_status(SAB, NOW, 1000), sab_answers())
        assert [(p.text, p.key) for p in result.problems] == [
            ("Low disk space: 500.5 GB free on the incomplete folder, under the 1,000 GB minimum", "disk:the incomplete folder")
        ]

    def test_downloading(self):
        result, fake = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers())
        assert result.state == "ok"
        assert result.version == "5.1.3"
        assert dict(result.facts) == {
            "Status": "Downloading",
            "Queue": "1",
            "Speed": "10.0 MB/s",
            "Left": "1.0 GB, 0:01:40",
            "Free (incomplete)": "500.5 GB",
            "Free (complete)": "32.2 TB",
            "Failed, 7 days": "0",
        }
        assert result.items == [Item("Heat.1995", "Downloading · movies · 0:01:40", progress=42)]
        assert fake.calls[0][2]["apikey"] == "sab-key"

    def test_paused_is_a_warning(self):
        result, _ = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers({"paused": True, "status": "Paused", "pause_int": "10:00"}))
        assert result.state == "warn"
        assert result.problems[0].text == "Downloads are paused (resumes in 10:00)"

    def test_recent_failures_only(self):
        failed = [
            {"name": "Recent", "completed": ts(NOW - timedelta(days=1)), "fail_message": "Out of retention"},
            {"name": "Old", "completed": ts(NOW - timedelta(days=30))},
        ]
        result, fake = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers(failed=failed))
        assert [p.text for p in result.problems] == ["Failed: Recent (Out of retention)"]
        assert dict(result.facts)["Failed, 7 days"] == "1"
        assert fake.calls[1][2]["failed_only"] == 1

    def test_failure_reason_drops_the_indexers_key(self):
        failed = [{"name": "Grab", "completed": ts(NOW), "fail_message": "URL Fetching failed; https://idx.example/api?t=get&apikey=idx-key"}]
        result, _ = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers(failed=failed))
        assert [p.text for p in result.problems] == ["Failed: Grab (URL Fetching failed; https://idx.example)"]

    def test_warnings_old_and_new_format(self):
        warnings = ["Server a is down", {"text": "Disk almost full", "type": "WARNING", "time": 1}]
        result, _ = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers(warnings=warnings))
        assert [p.text for p in result.problems] == ["Server a is down", "Disk almost full"]

    def test_idle_queue_has_no_speed(self):
        result, _ = run(lambda: status.sabnzbd_status(SAB, NOW), sab_answers({"noofslots_total": 0, "slots": [], "status": "Idle"}))
        assert "Speed" not in dict(result.facts)
        assert result.items == []

    def test_refused_in_the_body(self):
        answers = sab_answers()
        answers["queue"] = {"status": False, "error": "API Key Incorrect"}
        with patch.object(status.requests, "get", side_effect=FakeServices(answers)):
            [result] = [s for s in check_all(ConfigSettings(plex=None, radarr=None, sonarr=None, sabnzbd=SAB), NOW) if s.service == "sabnzbd"]
        assert result.state == "bad"
        assert result.error == "SABnzbd refused: API Key Incorrect"


# ---------------------------------------------------------------------------
# bazarr, answers as bazarr 1.6.2 gives them
# ---------------------------------------------------------------------------


def bazarr_answers(health=(), providers=None, radarr_signalr="", radarr_version="6.4.4.10685"):
    good = [{"name": n, "status": "Good", "retry": "-"} for n in ("gestdown", "opensubtitlescom", "subf2m")]
    return {
        "/subs/api/system/status": {"data": {"bazarr_version": "1.6.2", "sonarr_version": "4.0.20.3014", "radarr_version": radarr_version}},
        "/subs/api/badges": {"episodes": 1702, "movies": 129, "providers": 0, "status": 0, "sonarr_signalr": "", "radarr_signalr": radarr_signalr},
        "/subs/api/system/health": {"data": list(health)},
        "/subs/api/providers": {"data": good if providers is None else providers},
    }


class TestBazarr:
    def test_counts(self):
        result, fake = run(lambda: status.bazarr_status(BAZARR), bazarr_answers(radarr_version=""))
        assert result.state == "ok"
        assert result.version == "1.6.2"
        assert dict(result.facts) == {
            "Wanted": "1,702 episode, 129 movie subtitles", "Providers": "3", "Live updates": "Sonarr",
        }
        assert fake.calls[0][1] == {"X-API-KEY": "bazarr-key"}

    def test_problems(self):
        answers = bazarr_answers(
            health=[{"object": "/tv", "issue": "Not accessible"}],
            providers=[{"name": "subf2m", "status": "TooManyRequests", "retry": "2026-10-28 14:00"}, {"name": "gestdown", "status": "Good", "retry": "-"}],
            radarr_signalr="DOWN",
        )
        result, _ = run(lambda: status.bazarr_status(BAZARR), answers)
        assert result.state == "warn"
        assert [(p.text, p.key) for p in result.problems] == [
            ("/tv: Not accessible", "health:/tv"),
            ("Provider subf2m is throttled: TooManyRequests (retries 2026-10-28 14:00)", "provider:subf2m"),
            ("Not getting live updates from Radarr, new media waits for the next sync", "signalr:radarr"),
        ]
        assert dict(result.facts)["Providers"] == "2, 1 throttled"

    def test_without_its_base_url(self):
        page = MagicMock(status_code=200)
        page.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        result, _ = run(lambda: next(s for s in status.check_all(ConfigSettings(plex=None, radarr=None, sonarr=None, bazarr=BAZARR), NOW) if s.service == "bazarr"), {
            "/subs/api/system/status": page,
        })
        assert result.state == "bad"
        assert "add its base URL" in result.error


# ---------------------------------------------------------------------------
# unraid, answers shaped like unraid 7.4's api (4.37) gives them
# ---------------------------------------------------------------------------


def disk(name, kind="DATA", status="DISK_OK", temp=35, errors=0, size_tb=10, free_tb=4.0, warning=None, critical=None, rotational=True):
    kb = 1024**3  # sizes are in KB
    return {
        "name": name, "status": status, "type": kind, "rotational": rotational, "temp": temp, "numErrors": str(errors),
        # parity has no file system, a pool's second device has no sizes of its own
        "fsSize": None if kind == "PARITY" or size_tb is None else str(int(size_tb * kb)),
        "fsFree": None if kind == "PARITY" or size_tb is None else str(int(free_tb * kb)),
        "warning": warning, "critical": critical,
    }


def unraid_data(**overrides):
    array = {
        "state": "STARTED",
        "capacity": {"kilobytes": {"free": str(9 * 1024**3), "total": str(120 * 1024**3)}},
        "parityCheckStatus": {
            "status": "COMPLETED", "date": "2026-11-02T06:12:00.000Z", "duration": 50_580, "speed": "75.1 MB/s",
            "errors": 0, "progress": 0, "running": False, "paused": False, "correcting": False,
        },
        "parities": [disk("parity", "PARITY"), disk("parity2", "PARITY", temp=None)],
        "disks": [disk("disk1", warning=95, critical=99), disk("disk2", temp=38), {"name": "disk3", "status": "DISK_NP", "type": "DATA"}],
        "caches": [disk("cache_nvme", "CACHE", temp=43, size_tb=1, free_tb=0.7, rotational=False), disk("cache_nvme2", "CACHE", size_tb=None, rotational=False)],
    }
    data = {
        "vars": {"version": "7.4.0", "name": "Tower"},
        "info": {"display": {"unit": "CELSIUS", "warning": 70, "critical": 90, "hot": 50, "max": 55}},
        "array": array,
        "notifications": {"alerts": [], "warnings": []},
        "docker": {"containers": [
            {"names": ["/sonarr"], "state": "RUNNING", "status": "Up 2 days", "autoStart": True},
            {"names": ["/old-thing"], "state": "EXITED", "status": "Exited (0) 3 weeks ago", "autoStart": False},
        ]},
    }
    for key, value in overrides.items():
        (array if key in array else data)[key] = value
    return data


def unraid_run(data=None, errors=None, answer=None):
    body = {"data": unraid_data() if data is None else data}
    if errors:
        body["errors"] = errors
    with patch.object(status.requests, "post", return_value=answer or response(body)) as post:
        return status.unraid_status(UNRAID), post


def texts(result):
    return [(p.level, p.text, p.key) for p in result.problems]


class TestUnraid:
    def test_healthy(self):
        result, post = unraid_run()
        assert (result.state, result.version, result.problems) == ("ok", "7.4.0", [])
        assert dict(result.facts) == {
            "Server": "Tower",
            "Array": "Started, 9.0 TB free of 120.0 TB",
            "Disks": "2 data, 2 parity",
            "Hottest disk": "cache_nvme, 43°C",
            "Pools": "cache_nvme 716.8 GB free",
            "Last parity check": "2026-11-02, no errors, took 14h 3m",
            "Containers": "1 running, 1 stopped",
        }
        assert result.summary == "9.0 TB free of 120.0 TB"
        assert post.call_args.args[0] == "http://unraid/graphql"
        assert post.call_args.kwargs["headers"] == {"x-api-key": "unraid-key"}
        assert post.call_args.kwargs["allow_redirects"] is False

    def test_a_disk_dropping_out_is_the_worst_news(self):
        result, _ = unraid_run(unraid_data(disks=[
            disk("disk1", temp=51),
            disk("disk8", status="DISK_DSBL", errors=49_012),
            disk("disk9", status="DISK_NP_MISSING", temp=None),
        ], parities=[disk("parity", "PARITY", status="DISK_DSBL")]))
        assert result.state == "bad"
        assert texts(result) == [
            ("bad", "Parity is disabled: the array has less protection", "disk:parity"),
            ("bad", "Disk 8 is disabled: Unraid is emulating it from parity", "disk:disk8"),
            ("bad", "Disk 9 is missing: Unraid is emulating it from parity", "disk:disk9"),
            ("warn", "Disk 1 is running hot: 51°C, Unraid warns at 50°C", "temp:disk1"),
            ("warn", "Disk 8 has 49,012 read/write errors", "errors:disk8"),
        ]

    def test_temperatures(self):
        result, _ = unraid_run(unraid_data(disks=[disk("disk1", temp=55), disk("disk2", temp=float("nan"))], caches=[
            disk("cache_ssd", "CACHE", temp=65, rotational=False),  # ssds use unraid's ssd defaults, 60/70
            disk("cache_nvme", "CACHE", temp=70, rotational=False),
        ]))
        assert texts(result) == [
            ("bad", "Disk 1 is 55°C, over Unraid's 55°C limit", "temp:disk1"),
            ("bad", "cache_nvme is 70°C, over Unraid's 70°C limit", "temp:cache_nvme"),
            ("warn", "cache_ssd is running hot: 65°C, Unraid warns at 60°C", "temp:cache_ssd"),
        ]

    def test_space_uses_each_disks_thresholds_else_the_servers(self):
        result, _ = unraid_run(unraid_data(disks=[
            disk("disk1", free_tb=0.8, warning=95, critical=99),  # 92% full: under its own 95
            disk("disk2", free_tb=0.4, warning=95, critical=99),  # 96%
            disk("disk3", free_tb=2.5),  # 75%: over the server's 70
            disk("disk4", free_tb=0.5, warning=0, critical=0),  # 0 turns them off
        ], caches=[disk("cache_ssd", "CACHE", size_tb=1, free_tb=0.05, warning=90, critical=95)]))
        assert texts(result) == [
            ("bad", "cache_ssd is 95% full, 51.2 GB free (critical at 95%)", "space:cache_ssd"),
            ("warn", "Disk 2 is 96% full, 409.6 GB free (Unraid warns at 95%)", "space:disk2"),
            ("warn", "Disk 3 is 75% full, 2.5 TB free (Unraid warns at 70%)", "space:disk3"),
        ]

    def test_array_stopped(self):
        result, _ = unraid_run(unraid_data(state="STOPPED", docker=None), errors=[{"message": "Docker is not running", "path": ["docker"]}])
        assert texts(result) == [("bad", "The array is stopped", "array")]
        assert dict(result.facts)["Array"].startswith("Stopped")

    def test_parity_check_running(self):
        result, _ = unraid_run(unraid_data(parityCheckStatus={
            "status": "RUNNING", "running": True, "paused": False, "progress": 45, "speed": "150 MB/s", "errors": 3, "correcting": False,
        }))
        assert dict(result.facts)["Parity check"] == "45%, 150 MB/s"
        assert result.summary == "9.0 TB free of 120.0 TB · Parity check 45%, 150 MB/s"
        assert texts(result) == [("warn", "The parity check running now has found 3 errors so far", "parity")]
        paused, _ = unraid_run(unraid_data(parityCheckStatus={"status": "PAUSED", "running": True, "paused": True, "progress": 60}))
        assert (dict(paused.facts)["Parity check"], paused.state) == ("60%, paused", "ok")

    @pytest.mark.parametrize("check, fact, problem", [
        ({"status": "COMPLETED", "errors": 1, "date": "2026-11-02T06:12:00Z"}, "2026-11-02, 1 error", "The last parity check found 1 error"),
        ({"status": "FAILED", "date": "2026-11-02T06:12:00Z"}, "2026-11-02, failed", "The last parity check failed"),
        ({"status": "CANCELLED", "date": "2026-11-02T06:12:00Z", "duration": 600}, "2026-11-02, cancelled, took 10m", None),
        ({"status": "NEVER_RUN"}, "never run", None),
    ])
    def test_last_parity_check(self, check, fact, problem):
        result, _ = unraid_run(unraid_data(parityCheckStatus=check))
        assert dict(result.facts)["Last parity check"] == fact
        assert [p.text for p in result.problems] == ([problem] if problem else [])

    def test_containers_that_should_be_running(self):
        result, _ = unraid_run(unraid_data(docker={"containers": [
            {"names": ["/radarr"], "state": "EXITED", "status": "Exited (1) 2 hours ago", "autoStart": True},
            {"names": ["/cloudflared"], "state": "PAUSED", "status": "", "autoStart": True},
            {"names": ["/sonarr"], "state": "RUNNING", "status": "Up", "autoStart": True},
        ]}))
        assert texts(result) == [
            ("warn", "Container radarr isn't running (Exited (1) 2 hours ago), but it's set to autostart", "container:radarr"),
            ("warn", "Container cloudflared isn't running (paused), but it's set to autostart", "container:cloudflared"),
        ]
        assert dict(result.facts)["Containers"] == "1 running, 2 stopped"

    def test_notifications(self):
        result, _ = unraid_run(unraid_data(notifications={
            "alerts": [{"subject": "Alert [TOWER] - Disk 8 in error state (disk dsbl)", "description": "ST8000 (sdk)"}],
            "warnings": [{"subject": "Warning [TOWER] - reallocated sector ct is 8", "description": "ST10000 (sdf) see http://x/y?key=abc"}],
        }))
        assert texts(result) == [
            ("bad", "Notification: Alert [TOWER] - Disk 8 in error state (disk dsbl): ST8000 (sdk)", "notice:Alert [TOWER] - Disk 8 in error state (disk dsbl)|ST8000 (sdk)"),
            ("warn", "Notification: Warning [TOWER] - reallocated sector ct is 8: ST10000 (sdf) see http://x", "notice:Warning [TOWER] - reallocated sector ct is 8|ST10000 (sdf) see http://x/y?key=abc"),
        ]

    def test_what_the_key_cant_read_is_a_warning(self):
        forbidden = {"extensions": {"code": "FORBIDDEN"}}
        result, _ = unraid_run(unraid_data(notifications=None, docker=None), errors=[
            {"message": "Forbidden resource", "path": ["notifications"], **forbidden},
            {"message": "Forbidden resource", "path": ["docker"], **forbidden},
        ])
        assert texts(result) == [
            ("warn", "Unraid's notifications can't be read: Forbidden resource", "notifications"),
            ("warn", "Docker containers can't be read: Forbidden resource", "docker"),
        ]

    def test_without_the_array_the_card_is_down(self):
        data = {**unraid_data(), "array": None}
        with patch.object(status.requests, "post", return_value=response({"data": data, "errors": [{"message": "Forbidden resource", "path": ["array"]}]})):
            result = status.check_all(ConfigSettings(plex=None, radarr=None, sonarr=None, unraid=UNRAID), NOW)[0]
        assert (result.service, result.state, result.error) == ("unraid", "bad", "Unraid didn't say how the array is: Forbidden resource")

    def test_refused_key(self):
        refused = response({"data": None, "errors": [{"message": "Unauthorized", "extensions": {"code": "UNAUTHENTICATED"}}]})
        with patch.object(status.requests, "post", return_value=refused):
            result = status.check_all(ConfigSettings(plex=None, radarr=None, sonarr=None, unraid=UNRAID), NOW)[0]
        assert result.error == "Unraid refused the API key: Unauthorized"
        with patch.object(status.requests, "post", return_value=response(code=401)):
            result = status.check_all(ConfigSettings(plex=None, radarr=None, sonarr=None, unraid=UNRAID), NOW)[0]
        assert result.error == "Unraid refused the API key (HTTP 401)"

    def test_incidents_per_disk(self):
        result, _ = unraid_run(unraid_data(disks=[disk("disk8", status="DISK_DSBL", errors=5), disk("disk9", errors=2)]))
        assert {i.key: i.level for i in health.issues(result)} == {"disk:disk8": "bad", "errors:disk8": "warn", "errors:disk9": "warn"}

    def test_needs_a_connection(self):
        assert status.unraid_status(None).state == "off"


# ---------------------------------------------------------------------------
# seerr
# ---------------------------------------------------------------------------


def seerr_answers(pending=0, issues=0, **about):
    return {
        "/api/v1/status": {"version": "3.5.1", "updateAvailable": False, **about},
        "/api/v1/request/count": {"pending": pending, "processing": 433, "available": 1585},
        "/api/v1/issue/count": {"open": issues},
    }


class TestSeerr:
    def test_counts(self):
        result, fake = run(lambda: status.seerr_status(SEERR), seerr_answers())
        assert result.state == "ok"
        assert result.version == "3.5.1"
        assert dict(result.facts) == {"Pending approval": "0", "Processing": "433", "Available": "1,585", "Open issues": "0"}
        assert fake.calls[0][1] == {"X-Api-Key": "seerr-key"}

    def test_open_issues_warn_and_pending_requests_are_a_note(self):
        result, _ = run(lambda: status.seerr_status(SEERR), seerr_answers(pending=2, issues=1))
        assert result.state == "warn"
        assert [(p.level, p.text) for p in result.problems] == [
            ("info", "2 requests waiting for approval"),
            ("warn", "1 open issue"),
        ]
        result, _ = run(lambda: status.seerr_status(SEERR), seerr_answers(pending=2))
        assert result.state == "ok"

    def test_an_update_is_only_info(self):
        result, _ = run(lambda: status.seerr_status(SEERR), seerr_answers(updateAvailable=True))
        assert result.state == "ok"
        assert result.problems[0].level == "info"


# ---------------------------------------------------------------------------
# plex
# ---------------------------------------------------------------------------


def tautulli(data, result="success"):
    return {"response": {"result": result, "message": None if result == "success" else "Invalid apikey", "data": data}}


SESSION = {
    "friendly_name": "barry",
    "full_title": "Justified - Ghosts",
    "transcode_decision": "transcode",
    "video_full_resolution": "720p",
    "stream_video_full_resolution": "SD",
    "player": "LG OLED",
    "product": "Plex for LG",
    "state": "playing",
    "location": "wan",
    "progress_percent": "51",
}


def plex_answers(connected=True, sessions=()):
    return {
        "server_status": tautulli({"connected": connected}),
        "get_server_info": tautulli({"pms_version": "1.43.5"}),
        "get_activity": tautulli(
            {
                "stream_count": str(len(sessions)),
                "stream_count_direct_play": 0,
                "stream_count_direct_stream": 0,
                "stream_count_transcode": len(sessions),
                "total_bandwidth": 41920,
                "wan_bandwidth": 41920,
                "sessions": list(sessions),
            }
        ),
    }


class TestPlex:
    def test_now_playing(self):
        result, _ = run(lambda: status.plex_status(TAUTULLI), plex_answers(sessions=[SESSION]))
        # a transcode is marked, it isn't a problem with plex
        assert result.state == "ok"
        assert result.version == "1.43.5"
        assert dict(result.facts) == {"Streams": "1: 1 transcode", "Bandwidth": "41.9 Mbps, 41.9 remote"}
        assert result.items == [
            Item("barry: Justified - Ghosts", "Transcode · 720p → SD · LG OLED (Plex for LG) · remote", progress=51, level="warn")
        ]

    def test_nothing_playing(self):
        result, _ = run(lambda: status.plex_status(TAUTULLI), plex_answers())
        assert dict(result.facts) == {"Streams": "0"}
        assert result.items_label == ""

    def test_plex_unreachable(self):
        result, _ = run(lambda: status.plex_status(TAUTULLI), plex_answers(connected=False))
        assert result.state == "bad"
        assert result.problems[0].text == "Tautulli can't reach the Plex server"

    def test_needs_tautulli(self):
        assert status.plex_status(None).state == "off"


# ---------------------------------------------------------------------------
# all at once
# ---------------------------------------------------------------------------


EVERYTHING = ConfigSettings(plex=None, radarr=RADARR, sonarr=SONARR, tautulli=TAUTULLI, seerr=SEERR, sabnzbd=SAB, bazarr=BAZARR, unraid=UNRAID)


class TestCheckAll:
    def test_one_service_down_marks_only_its_card(self):
        def fake_get(url, timeout, headers=None, params=None):
            if url.startswith("http://sonarr"):
                raise requests.exceptions.ConnectionError("http://sonarr:8989 refused")
            host = urlparse(url).hostname
            answers = {
                "radarr": arr_answers(), "sab": sab_answers(), "seerr": seerr_answers(), "tautulli": plex_answers(),
                "bazarr": bazarr_answers(),
            }[host]
            return FakeServices(answers)(url, timeout, headers, params)

        with patch.object(status.requests, "get", side_effect=fake_get), \
                patch.object(status.requests, "post", return_value=response({"data": unraid_data()})):
            results = check_all(EVERYTHING, NOW)
        assert [(s.service, s.state) for s in results] == [
            ("unraid", "ok"), ("plex", "ok"), ("radarr", "ok"), ("sonarr", "bad"), ("bazarr", "ok"), ("sabnzbd", "ok"), ("seerr", "ok"),
        ]
        assert results[3].error == "Couldn't connect"

    def test_refused_key(self):
        with patch.object(status.requests, "get", return_value=response(code=401)):
            [result] = [s for s in check_all(EVERYTHING, NOW) if s.service == "radarr"]
        assert result.error == "Radarr refused the API key (HTTP 401)"

    def test_errors_never_hold_a_key(self):
        # sabnzbd's key is a query param, requests puts the url in its errors
        with patch.object(status.requests, "get", return_value=response(code=500)), \
                patch.object(status.requests, "post", return_value=response(code=500)):
            results = check_all(EVERYTHING, NOW)
        for result in results:
            assert result.state == "bad"
            for key in ("sab-key", "radarr-key", "sonarr-key", "seerr-key", "tautulli-key", "bazarr-key", "unraid-key"):
                assert key not in result.error

    def test_nothing_connected(self):
        results = check_all(ConfigSettings(plex=None, radarr=None, sonarr=None), NOW)
        assert {s.state for s in results} == {"off"}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(
        settings,
        keys=FakeKeys(),
        run_job=fake_run_job,
        load_config=lambda: EVERYTHING,
        run_scheduler=False,
    )
    with TestClient(app) as c:
        yield c


def fake_check_all(settings, now):
    radarr = ServiceStatus("radarr", "Radarr", version="6.4.4", facts=[("Queue", "1, 1 need attention")])
    radarr.items_label = "Needs attention in the queue"
    radarr.items = [Item("Heat (1995) <script>", "Not an upgrade", level="warn")]
    radarr.raise_state("warn")
    sonarr = ServiceStatus("sonarr", "Sonarr", state="bad", error="Couldn't connect")
    return [radarr, sonarr, ServiceStatus("sabnzbd", "SABnzbd", state="off")]


class TestPages:
    def test_arr_cards_say_whether_auto_remove_is_on(self, settings, monkeypatch):  # noqa: F811
        from dataclasses import replace

        from config.models import QueueCleanup

        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        radarr = replace(RADARR, queue_cleanup=QueueCleanup(enabled=True, stuck_for=timedelta(hours=12)))
        everything = replace(EVERYTHING, radarr=radarr)
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: everything, run_scheduler=False)
        with TestClient(app) as c:
            page = c.get("/status", headers=auth()).text
        assert '<dt>Auto-remove</dt><dd>after 12h stuck, dry run · <a href="/settings/queue">Settings</a></dd>' in page
        # sonarr's card is down, without facts: still says it's off
        assert '<dt>Auto-remove</dt><dd>off · <a href="/settings/queue">Settings</a></dd>' in page

    def test_status_page(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        page = client.get("/status", headers=auth())
        assert page.status_code == 200
        assert "Heat (1995) &lt;script&gt;" in page.text
        assert "Couldn&#39;t connect" in page.text
        assert 'href="/settings/connections#sabnzbd"' in page.text
        assert '<meta http-equiv="refresh" content="60">' in page.text
        # the services that need you come first, named in one line
        assert "2 services need you:" in page.text
        assert page.text.index('id="radarr"') < page.text.index('id="sabnzbd"')
        assert 'href="/settings/connections#sonarr">Check the Sonarr connection' in page.text

    def test_links_to_each_service(self, settings, monkeypatch):  # noqa: F811
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        both = ArrConfig(baseurl="http://radarr:7878", apikey="k", tag=None, public_url="https://radarr.example.com")
        config = ConfigSettings(plex=None, radarr=both, sonarr=SONARR, sabnzbd=SAB)
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: config, run_scheduler=False)
        with TestClient(app) as c:
            page = c.get("/status", headers=auth()).text
        # public by default, app.js switches to local when this browser chose it
        assert 'href="https://radarr.example.com" target="_blank" rel="noopener" data-local="http://radarr:7878" data-public="https://radarr.example.com">Open Radarr' in page
        assert 'href="http://sonarr:8989" target="_blank" rel="noopener" data-local="http://sonarr:8989">Open Sonarr' in page
        assert "data-link-choice" in page
        assert "Also open" not in page
        # radarr's warning can be muted, sonarr's outage can't
        assert 'data-unmutable' in page and 'id="sonarr" data-service="sonarr" data-unmutable' in page

    def test_links_to_other_tools(self, settings, monkeypatch):  # noqa: F811
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        config = ConfigSettings(plex=None, radarr=None, sonarr=None, links=[LinkConfig("Tracearr", "http://unraid:3000", "https://tracearr.example.com")])
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: config, run_scheduler=False)
        with TestClient(app) as c:
            page = c.get("/status", headers=auth()).text
        assert 'href="https://tracearr.example.com" target="_blank" rel="noopener" data-local="http://unraid:3000" data-public="https://tracearr.example.com">Tracearr</a>' in page
        assert "data-link-choice" in page

    def test_a_link_shown_with_its_service(self, settings, monkeypatch):  # noqa: F811
        monkeypatch.setattr("web.status_routes.check_all", lambda s, n: [ServiceStatus("plex", "Plex", facts=[("Streams", "0")])])
        config = ConfigSettings(
            plex=None, radarr=None, sonarr=None, tautulli=TAUTULLI,
            links=[LinkConfig("Tracearr", "http://unraid:3000", service="plex"), LinkConfig("Unraid", "http://unraid")],
        )
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: config, run_scheduler=False)
        with TestClient(app) as c:
            page = c.get("/status", headers=auth()).text
        line = page[page.index('<li id="plex">'):page.index("</li>", page.index('<li id="plex">'))]
        assert ">Open Tautulli</a> <a" in line and ">Tracearr</a>" in line
        also = page[page.index("Also open"):]
        assert ">Unraid</a>" in also and "Tracearr" not in also

    def test_unraid_line_is_its_summary(self, client, monkeypatch):
        def found(_settings, _now):
            with patch.object(status.requests, "post", return_value=response({"data": unraid_data()})):
                return [status.unraid_status(UNRAID)]

        monkeypatch.setattr("web.status_routes.check_all", found)
        page = client.get("/status", headers=auth()).text
        assert '<span class="line-facts">9.0 TB free of 120.0 TB</span>' in page
        # every fact once it's opened
        assert "<dt>Containers</dt><dd>1 running, 1 stopped</dd>" in page

    def test_unraid_card(self, settings, monkeypatch):  # noqa: F811
        def found(_settings, _now):
            answer = response({"data": unraid_data(disks=[disk("disk8", status="DISK_DSBL")])})
            with patch.object(status.requests, "post", return_value=answer):
                return [status.unraid_status(UNRAID), ServiceStatus("radarr", "Radarr")]

        monkeypatch.setattr("web.status_routes.check_all", found)
        unraid = UnraidConfig(url="http://192.168.1.15", apikey="k", public_url="https://unraid.example.com")
        config = ConfigSettings(plex=None, radarr=None, sonarr=None, unraid=unraid)
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: config, run_scheduler=False)
        with TestClient(app) as c:
            page = c.get("/status", headers=auth()).text
        assert "Unraid: 1 error" in page
        assert 'data-mute="unraid|disk:disk8" data-level="bad"' in page
        assert "Disk 8 is disabled: Unraid is emulating it from parity" in page
        assert 'data-local="http://192.168.1.15" data-public="https://unraid.example.com">Open Unraid</a>' in page

    def test_problems_can_be_muted(self, client, monkeypatch):
        def warn(settings, now):
            sonarr = ServiceStatus("sonarr", "Sonarr")
            sonarr.problem("warn", "Low disk space", "disk:/tv")
            return [sonarr]

        monkeypatch.setattr("web.status_routes.check_all", warn)
        page = client.get("/status", headers=auth()).text
        assert 'data-mute="sonarr|disk:/tv" data-level="warn"' in page
        assert 'id="sonarr" data-service="sonarr">' in page
        assert "data-link-choice" not in page

    def test_refresh_can_be_paused(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        page = client.get("/status?refresh=0", headers=auth()).text
        assert 'http-equiv="refresh"' not in page
        assert 'href="/status">resume</a>' in page

    def test_all_ok_is_one_line(self, client, monkeypatch):
        def healthy(settings, now):
            return [ServiceStatus("radarr", "Radarr", facts=[("Missing", "3")]), ServiceStatus("seerr", "Seerr", state="off")]

        monkeypatch.setattr("web.status_routes.check_all", healthy)
        page = client.get("/status", headers=auth()).text
        assert "All 1 connected services are OK." in page
        assert "No health checks yet." in page

    def test_nothing_connected(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda s, n: [ServiceStatus("radarr", "Radarr", state="off")])
        assert "Nothing is connected yet." in client.get("/status", headers=auth()).text

    def test_api(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        body = client.get("/api/status", headers=auth()).json()
        radarr = body["services"][0]
        assert radarr["state"] == "warn"
        assert radarr["facts"] == {"Queue": "1, 1 need attention"}
        assert radarr["items"][0]["title"] == "Heat (1995) <script>"

    def test_needs_access(self, client):
        assert client.get("/status").status_code == 403
        assert client.get("/api/status").status_code == 403

    def test_settings_that_cant_load(self, settings):  # noqa: F811
        def broken():
            raise ValueError("[radarr] bad")

        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=broken, run_scheduler=False)
        with TestClient(app) as c:
            assert "[radarr] bad" in c.get("/status", headers=auth()).text
            assert c.get("/api/status", headers=auth()).status_code == 503


# ---------------------------------------------------------------------------
# stuck downloads
# ---------------------------------------------------------------------------


def stuck_record(id, download="SABnzbd_nzo_1", episode=None, **fields):
    record = {
        "id": id,
        "downloadId": download,
        "title": "Show.S01.1080p",
        "downloadClient": "SABnzbd",
        "trackedDownloadState": "importBlocked",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"title": "x", "messages": ["Unable to parse"]}],
        "series": {"title": "Show"},
        "episode": {"seasonNumber": 1, "episodeNumber": episode or id},
    }
    return record | fields


class TestStuckDownloads:
    def test_a_season_pack_is_one_download(self):
        records = [
            stuck_record(1),
            stuck_record(2, trackedDownloadStatus="error", errorMessage="Sample"),
            stuck_record(3, download="SABnzbd_nzo_2", title="Other.Release", series={"title": "Other"}),
            stuck_record(4, download="SABnzbd_nzo_3", trackedDownloadState="downloading", trackedDownloadStatus="ok"),
        ]
        first, second = status.stuck_downloads(records)
        assert first == status.StuckDownload(
            "SABnzbd_nzo_1", [1, 2], ["Show S01E01", "Show S01E02"], "Show.S01.1080p", "SABnzbd",
            "Unable to parse; Sample", "bad", ["importBlocked"],
        )
        assert (second.key, second.ids, second.titles) == ("SABnzbd_nzo_2", [3], ["Other S01E03"])

    def test_without_a_download_id_the_record_is_its_own(self):
        [download] = status.stuck_downloads([stuck_record(7, download=None)])
        assert download.key == "queue:7"

    def test_records_without_an_id_are_skipped(self):
        assert status.stuck_downloads([stuck_record(None)]) == []


class FakeQueue:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def get(self, url, timeout, headers=None, params=None):
        self.calls.append(("GET", url, params))
        return response({"totalRecords": len(self.records), "records": self.records})

    def delete(self, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append(("DELETE", url, params, json))
        ids = {r["id"] for r in self.records}
        self.records = [r for r in self.records if r["id"] not in json["ids"]]
        return response(code=200 if set(json["ids"]) <= ids else 404)


@pytest.fixture
def fake_queue():
    fake = FakeQueue([stuck_record(1), stuck_record(2), stuck_record(3, download="SABnzbd_nzo_2")])
    with patch.object(status.requests, "get", side_effect=fake.get), patch.object(
        status.requests, "delete", side_effect=fake.delete
    ):
        yield fake


class TestArrQueueRemoval:
    def test_reads_the_whole_queue(self, fake_queue):
        status.ArrQueue("sonarr", "Sonarr", SONARR).stuck()
        _, url, params = fake_queue.calls[0]
        assert url == "http://sonarr:8989/api/v3/queue"
        assert params["pageSize"] == status.ARR_QUEUE_ALL
        assert params["includeEpisode"] == "true"

    @pytest.mark.parametrize(
        "method, blocklist, expected",
        [
            ("client", "none", {"removeFromClient": "true", "blocklist": "false", "skipRedownload": "false"}),
            ("ignore", "search", {"removeFromClient": "false", "blocklist": "true", "skipRedownload": "false"}),
            ("client", "only", {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}),
        ],
    )
    def test_removal_options(self, fake_queue, method, blocklist, expected):
        queue = status.ArrQueue("sonarr", "Sonarr", SONARR)
        removed, gone = status.clear_stuck(queue, ["SABnzbd_nzo_1"], method, blocklist)
        assert [d.key for d in removed] == ["SABnzbd_nzo_1"] and gone == 0
        verb, url, params, body = fake_queue.calls[-1]
        assert (verb, url, params, body) == ("DELETE", "http://sonarr:8989/api/v3/queue/bulk", expected, {"ids": [1, 2]})

    def test_only_whats_still_stuck_is_removed(self, fake_queue):
        queue = status.ArrQueue("sonarr", "Sonarr", SONARR)
        # nzo_2 imported since the page was loaded
        fake_queue.records[2]["trackedDownloadState"] = "importing"
        fake_queue.records[2]["trackedDownloadStatus"] = "ok"
        removed, gone = status.clear_stuck(queue, ["SABnzbd_nzo_1", "SABnzbd_nzo_2", "SABnzbd_nzo_1", "gone"], "client", "none")
        assert [d.key for d in removed] == ["SABnzbd_nzo_1"]
        assert gone == 2
        assert fake_queue.calls[-1][3] == {"ids": [1, 2]}

    def test_nothing_still_stuck_removes_nothing(self, fake_queue):
        removed, gone = status.clear_stuck(status.ArrQueue("sonarr", "Sonarr", SONARR), ["gone"], "client", "none")
        assert (removed, gone) == ([], 1)
        assert [c[0] for c in fake_queue.calls] == ["GET"]

    def test_unknown_options_raise(self, fake_queue):
        with pytest.raises(ValueError):
            status.clear_stuck(status.ArrQueue("sonarr", "Sonarr", SONARR), ["SABnzbd_nzo_1"], "delete", "none")
        assert fake_queue.calls == []

    def test_a_record_gone_meanwhile_removes_nothing(self):
        queue = status.ArrQueue("radarr", "Radarr", RADARR)
        with patch.object(status.requests, "delete", return_value=response(code=404)):
            with pytest.raises(status.Refused, match="nothing was removed"):
                queue.remove([1], "client", "none")


@pytest.fixture
def queue_client(settings, fake_queue):  # noqa: F811
    app = create_app(
        settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: EVERYTHING, run_scheduler=False
    )
    with TestClient(app) as c:
        c.app.state.health.sync_now = MagicMock()
        yield c


class TestQueuePages:
    def test_status_card_links_to_it(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        page = client.get("/status", headers=auth()).text
        assert 'href="/status/radarr/queue"' in page
        assert 'href="/status/sabnzbd/queue"' not in page

    def test_lists_stuck_downloads(self, queue_client):
        page = queue_client.get("/status/sonarr/queue", headers=auth())
        assert page.status_code == 200
        assert 'data-select-all="download"' in page.text
        assert 'value="SABnzbd_nzo_1"' in page.text
        assert "Show S01E01, Show S01E02" in page.text

    def test_says_whether_auto_remove_is_on(self, queue_client):
        page = queue_client.get("/status/sonarr/queue", headers=auth()).text
        assert "Auto-remove is off." in page and 'href="/settings/queue"' in page

    def test_says_when_each_is_auto_removed(self, settings, fake_queue):  # noqa: F811
        from dataclasses import replace

        from config.models import QueueCleanup
        from tests.test_queue_cleanup import incident
        from web.jobs import utc_now

        sonarr = replace(SONARR, queue_cleanup=QueueCleanup(enabled=True, dry_run=True))
        everything = replace(EVERYTHING, sonarr=sonarr)
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: everything, run_scheduler=False)
        with TestClient(app) as c:
            incident(app.state.engine, "SABnzbd_nzo_1", utc_now() - timedelta(hours=2), service="sonarr")
            page = c.get("/status/sonarr/queue", headers=auth()).text
        assert "Auto-remove is on: after 6h 0m stuck, remove from the download client, blocklist and search, at most 10 a run (dry run" in page
        assert "Listed by a dry run in 3h 59m" in page or "Listed by a dry run in 4h 0m" in page
        assert "Listed by a dry run 6h 0m after the health check first sees it" in page

    def test_remove_selected(self, queue_client, fake_queue):
        response = queue_client.post(
            "/status/sonarr/queue/remove",
            content="download=SABnzbd_nzo_1&download=SABnzbd_nzo_2&download=gone&method=ignore&blocklist=only",
            headers={**auth(), **ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/status/sonarr/queue?removed=2&gone=1"
        assert fake_queue.calls[-1][2:] == (
            {"removeFromClient": "false", "blocklist": "true", "skipRedownload": "true"},
            {"ids": [1, 2, 3]},
        )
        queue_client.app.state.health.sync_now.assert_called_once()
        page = queue_client.get(response.headers["location"], headers=auth()).text
        assert "Removed 2 downloads" in page
        assert "1 selected wasn&#39;t stuck any more" in page
        assert "Nothing is stuck" in page

    def test_nothing_selected(self, queue_client, fake_queue):
        response = queue_client.post(
            "/status/sonarr/queue/remove",
            content="method=client&blocklist=none",
            headers={**auth(), **ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 422
        assert "Select at least one download." in response.text
        assert "DELETE" not in [c[0] for c in fake_queue.calls]

    def test_unknown_service_or_option(self, queue_client):
        assert queue_client.get("/status/sabnzbd/queue", headers=auth()).status_code == 404
        assert queue_client.get("/api/status/plex/queue", headers=auth()).status_code == 404
        response = queue_client.post(
            "/status/sonarr/queue/remove",
            content="download=SABnzbd_nzo_1&method=delete",
            headers={**auth(), **ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 422

    def test_not_connected(self, settings):  # noqa: F811
        app = create_app(
            settings, keys=FakeKeys(), run_job=fake_run_job,
            load_config=lambda: ConfigSettings(plex=None, radarr=None, sonarr=None), run_scheduler=False,
        )
        with TestClient(app) as c:
            page = c.get("/status/radarr/queue", headers=auth()).text
            assert "Radarr isn&#39;t connected." in page
            assert 'href="/settings/connections#radarr"' in page
            assert c.get("/api/status/radarr/queue", headers=auth()).status_code == 409

    def test_errors_never_hold_a_key(self, queue_client):
        error = requests.exceptions.HTTPError("500 Error for url http://sonarr?apikey=sonarr-key")
        with patch.object(status.requests, "get", side_effect=error):
            page = queue_client.get("/status/sonarr/queue", headers=auth())
            body = queue_client.get("/api/status/sonarr/queue", headers=auth())
        assert "HTTPError" in page.text and "sonarr-key" not in page.text
        assert body.status_code == 502 and "sonarr-key" not in body.text

    def test_a_failed_removal_never_holds_a_key(self, queue_client, fake_queue):
        error = requests.exceptions.HTTPError("500 Error for url http://sonarr:8989/api/v3/queue/bulk?apikey=sonarr-key")
        form = {**auth(), **ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
        with patch.object(status.requests, "delete", side_effect=error):
            page = queue_client.post("/status/sonarr/queue/remove", content="download=SABnzbd_nzo_1&method=client&blocklist=none", headers=form)
            body = queue_client.post(
                "/api/status/sonarr/queue/remove",
                json={"downloads": ["SABnzbd_nzo_1"], "method": "client", "blocklist": "none"},
                headers={**auth(), **ORIGIN},
            )
        assert page.status_code == 502 and "HTTPError" in page.text and "sonarr-key" not in page.text
        assert body.status_code == 502 and "sonarr-key" not in body.text
        queue_client.app.state.health.sync_now.assert_not_called()

    def test_api(self, queue_client, fake_queue):
        listed = queue_client.get("/api/status/sonarr/queue", headers=auth()).json()
        assert [d["key"] for d in listed["downloads"]] == ["SABnzbd_nzo_1", "SABnzbd_nzo_2"]
        assert listed["downloads"][0]["ids"] == [1, 2]
        response = queue_client.post(
            "/api/status/sonarr/queue/remove", json={"downloads": ["SABnzbd_nzo_2"]}, headers={**auth(), **ORIGIN}
        )
        assert response.status_code == 200
        assert [d["key"] for d in response.json()["removed"]] == ["SABnzbd_nzo_2"]
        assert fake_queue.calls[-1][2]["removeFromClient"] == "true"
        assert queue_client.post(
            "/api/status/sonarr/queue/remove", json={"downloads": ["x"], "blocklist": "always"}, headers={**auth(), **ORIGIN}
        ).status_code == 422
        assert queue_client.post("/api/status/sonarr/queue/remove", json={"downloads": []}, headers={**auth(), **ORIGIN}).status_code == 422

    def test_needs_access(self, queue_client):
        assert queue_client.get("/status/sonarr/queue").status_code == 403
        assert queue_client.post("/api/status/sonarr/queue/remove", json={"downloads": ["x"]}, headers=ORIGIN).status_code == 403
