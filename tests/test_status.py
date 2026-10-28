"""Tests for the status page: what radarr, sonarr, sabnzbd, seerr and plex report"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
import requests
from fastapi.testclient import TestClient

from config.models import ArrConfig, ConfigSettings, SabnzbdConfig, SeerrConfig, TautulliConfig
from tests.test_web import ORIGIN, FakeKeys, auth, fake_run_job
from tests.test_web import settings  # noqa: F401 (fixture)
from web import status
from web.app import create_app
from web.status import Item, ServiceStatus, check_all, size

NOW = datetime(2026, 10, 25, 12)
RADARR = ArrConfig(baseurl="http://radarr:7878/", apikey="radarr-key", tag=None)
SONARR = ArrConfig(baseurl="http://sonarr:8989", apikey="sonarr-key", tag=None)
SAB = SabnzbdConfig(url="http://sab:8080", apikey="sab-key")
SEERR = SeerrConfig(url="http://seerr:5055", apikey="seerr-key")
TAUTULLI = TautulliConfig(url="http://tautulli:8181", apikey="tautulli-key")


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
        assert [(p.text, p.key) for p in result.problems] == [("Low disk space: 50.0 GB free on /a, /b", "disk:/a, /b")]
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
            Item("Fatherland (2026)", "Not a Custom Format upgrade", level="warn"),
            Item("Survivor S49E03", "Download failed", level="bad"),
        ]
        assert result.state == "bad"
        assert dict(result.facts)["Queue"] == "3, 1 downloading, 2 need attention"

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
            ("Low disk space: 500.5 GB free on the incomplete folder", "disk:the incomplete folder")
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
        assert dict(result.facts) == {"Pending approval": "0", "Processing": "433", "Available": "1585", "Open issues": "0"}
        assert fake.calls[0][1] == {"X-Api-Key": "seerr-key"}

    def test_pending_requests_and_issues_warn(self):
        result, _ = run(lambda: status.seerr_status(SEERR), seerr_answers(pending=2, issues=1))
        assert result.state == "warn"
        assert [p.text for p in result.problems] == ["2 requests waiting for approval", "1 open issue"]

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


EVERYTHING = ConfigSettings(plex=None, radarr=RADARR, sonarr=SONARR, tautulli=TAUTULLI, seerr=SEERR, sabnzbd=SAB)


class TestCheckAll:
    def test_one_service_down_marks_only_its_card(self):
        def fake_get(url, timeout, headers=None, params=None):
            if url.startswith("http://sonarr"):
                raise requests.exceptions.ConnectionError("http://sonarr:8989 refused")
            host = urlparse(url).hostname
            answers = {"radarr": arr_answers(), "sab": sab_answers(), "seerr": seerr_answers(), "tautulli": plex_answers()}[host]
            return FakeServices(answers)(url, timeout, headers, params)

        with patch.object(status.requests, "get", side_effect=fake_get):
            results = check_all(EVERYTHING, NOW)
        assert [(s.service, s.state) for s in results] == [
            ("plex", "ok"), ("radarr", "ok"), ("sonarr", "bad"), ("sabnzbd", "ok"), ("seerr", "ok"),
        ]
        assert results[2].error == "Couldn't connect"

    def test_refused_key(self):
        with patch.object(status.requests, "get", return_value=response(code=401)):
            [result] = [s for s in check_all(EVERYTHING, NOW) if s.service == "radarr"]
        assert result.error == "Radarr refused the API key (HTTP 401)"

    def test_errors_never_hold_a_key(self):
        # sabnzbd's key is a query param, requests puts the url in its errors
        with patch.object(status.requests, "get", return_value=response(code=500)):
            results = check_all(EVERYTHING, NOW)
        for result in results:
            assert result.state == "bad"
            for key in ("sab-key", "radarr-key", "sonarr-key", "seerr-key", "tautulli-key"):
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
    def test_status_page(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        page = client.get("/status", headers=auth())
        assert page.status_code == 200
        assert "Heat (1995) &lt;script&gt;" in page.text
        assert "Couldn&#39;t connect" in page.text
        assert 'href="/settings/connections#sabnzbd"' in page.text
        assert '<meta http-equiv="refresh" content="60">' in page.text

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
            "Unable to parse; Sample", "bad",
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
