"""Tests for health checks: problems kept as incidents, and the history pages"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from tests.test_stats import engine  # noqa: F401 (fixture)
from tests.test_status import EVERYTHING, NOW, fake_check_all
from tests.test_web import FakeKeys, auth, fake_run_job
from tests.test_web import settings  # noqa: F401 (fixture)
from web import health
from web.app import create_app
from web.db import HealthIncident
from web.health import KEEP, REOPEN_WITHIN, STATUS_SETTING, check, issues, record, span, summary
from web.jobs import utc_now
from web.stats import SyncStatus, save_status, sync_status
from web.status import Item, ServiceStatus


def service(name="radarr", problems=(), items=(), error=None, state="ok"):
    s = ServiceStatus(name, name.title(), state=state, error=error)
    for problem in problems:
        s.problem(*problem)
    s.items = list(items)
    return s


def stored(engine):  # noqa: F811
    with Session(engine) as session:
        return list(session.scalars(select(HealthIncident).order_by(HealthIncident.id)))


class TestIssues:
    def test_problems_by_key(self):
        s = service(problems=[("warn", "Indexers unavailable"), ("bad", "Low disk space: 5 GB free on /a", "disk:/a"), ("info", "Update")])
        assert [(i.key, i.level) for i in issues(s)] == [("Indexers unavailable", "warn"), ("disk:/a", "bad")]

    def test_stuck_queue_records(self):
        s = service(items=[Item("Heat (1995)", "Not an upgrade", level="warn"), Item("Fine", level="ok")])
        assert [(i.key, i.text) for i in issues(s)] == [("queue:Heat (1995)", "Stuck in the queue: Heat (1995): Not an upgrade")]
        # plex's items are streams, a transcode isn't a problem
        plex = service("plex", items=[Item("Kev: Heat", "Transcode", level="warn")])
        assert issues(plex) == []

    def test_down(self):
        s = service(error="Couldn't connect", problems=[("warn", "ignored")])
        assert [(i.key, i.level, i.text) for i in issues(s)] == [("down", "bad", "Radarr can't be checked: Couldn't connect")]

    def test_seerrs_waiting_work_isnt_recorded(self):
        s = service("seerr", problems=[("warn", "3 requests waiting for approval", "pending"), ("warn", "2 open issues", "issues"), ("warn", "Seerr needs a restart")])
        assert [i.key for i in issues(s)] == ["Seerr needs a restart"]

    def test_one_per_key_the_worst(self):
        s = service(problems=[("warn", "a", "disk"), ("bad", "b", "disk")])
        assert [(i.key, i.level, i.text) for i in issues(s)] == [("disk", "bad", "b")]


class TestRecord:
    def test_opens_updates_and_clears(self, engine):  # noqa: F811
        assert record(engine, [service(problems=[("warn", "SAB paused (resumes in 10:00)", "paused")])], NOW) == 1
        five = NOW + timedelta(minutes=5)
        record(engine, [service(problems=[("bad", "SAB paused (resumes in 5:00)", "paused")])], five)
        [incident] = stored(engine)
        assert (incident.started_at, incident.last_seen_at, incident.ended_at) == (NOW, five, None)
        assert (incident.level, incident.text) == ("bad", "SAB paused (resumes in 5:00)")
        # still bad after it eases: the worst it was
        record(engine, [service(problems=[("warn", "SAB paused", "paused")])], NOW + timedelta(minutes=10))
        assert stored(engine)[0].level == "bad"
        assert record(engine, [service()], NOW + timedelta(minutes=15)) == 0
        assert stored(engine)[0].ended_at == NOW + timedelta(minutes=15)

    def test_coming_back_soon_continues_the_incident(self, engine):  # noqa: F811
        problem = [("warn", "Indexers unavailable")]
        record(engine, [service(problems=problem)], NOW)
        record(engine, [service()], NOW + timedelta(minutes=5))
        record(engine, [service(problems=problem)], NOW + timedelta(minutes=10))
        [incident] = stored(engine)
        assert incident.ended_at is None and incident.started_at == NOW
        # later than REOPEN_WITHIN is a new one
        cleared = NOW + timedelta(minutes=15)
        record(engine, [service()], cleared)
        record(engine, [service(problems=problem)], cleared + REOPEN_WITHIN + timedelta(minutes=1))
        assert [(i.started_at, i.ended_at) for i in stored(engine)] == [
            (NOW, cleared),
            (cleared + REOPEN_WITHIN + timedelta(minutes=1), None),
        ]

    def test_a_service_that_cant_be_checked_keeps_its_problems_open(self, engine):  # noqa: F811
        record(engine, [service(problems=[("warn", "Indexers unavailable")]), service("sonarr", problems=[("warn", "x")])], NOW)
        record(engine, [service(error="Couldn't connect"), service("sonarr")], NOW + timedelta(minutes=5))
        by_key = {(i.service, i.key): i for i in stored(engine)}
        assert by_key["radarr", "Indexers unavailable"].ended_at is None
        assert by_key["radarr", "down"].ended_at is None
        assert by_key["sonarr", "x"].ended_at == NOW + timedelta(minutes=5)
        # back up: down clears, and so does what's gone
        record(engine, [service(), service("sonarr")], NOW + timedelta(minutes=10))
        assert all(i.ended_at == NOW + timedelta(minutes=10) for i in stored(engine) if i.service == "radarr")

    def test_disconnecting_clears(self, engine):  # noqa: F811
        record(engine, [service(problems=[("warn", "x")])], NOW)
        record(engine, [service(state="off")], NOW + timedelta(minutes=5))
        assert stored(engine)[0].ended_at is not None

    def test_old_incidents_are_deleted(self, engine):  # noqa: F811
        record(engine, [service(problems=[("warn", "x")])], NOW)
        record(engine, [service()], NOW + timedelta(minutes=5))
        record(engine, [service()], NOW + KEEP + timedelta(minutes=10))
        assert stored(engine) == []


class TestCheck:
    def test_records_and_saves_its_status(self, engine, monkeypatch):  # noqa: F811
        monkeypatch.setattr(health, "check_all", fake_check_all)
        status = check(engine, ConfigSettings(plex=None, radarr=None, sonarr=None))
        assert status.error is None and sync_status(engine, STATUS_SETTING).finished_at is not None
        assert sorted((i.service, i.key) for i in stored(engine)) == [("radarr", "queue:Heat (1995) <script>"), ("sonarr", "down")]


@pytest.mark.parametrize(
    "delta, expected",
    [(timedelta(minutes=3), "under 5m"), (timedelta(minutes=25), "25m"), (timedelta(hours=3, minutes=12), "3h 12m"), (timedelta(days=2, hours=4, minutes=9), "2d 4h")],
)
def test_span(delta, expected):
    assert span(delta) == expected


def test_summary_counts_down_time_within_the_period():
    since = NOW - timedelta(hours=1)
    rows = [
        HealthIncident(service="sonarr", key="down", level="bad", text="", started_at=NOW - timedelta(hours=3), last_seen_at=NOW, ended_at=NOW),
        HealthIncident(service="sonarr", key="x", level="warn", text="", started_at=NOW, last_seen_at=NOW, ended_at=None),
    ]
    assert summary(rows, since, NOW + timedelta(minutes=30)) == [
        {"service": "sonarr", "label": "Sonarr", "incidents": 2, "ongoing": 1, "down_seconds": 3600, "down": "1h 0m"}
    ]


# ---------------------------------------------------------------------------
# the pages
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: EVERYTHING, run_scheduler=False)
    with TestClient(app) as c:
        now = utc_now()
        record(c.app.state.engine, [service(problems=[("warn", "Indexers unavailable")])], now - timedelta(days=10))
        record(c.app.state.engine, [service()], now - timedelta(days=9))
        record(c.app.state.engine, [service("sonarr", error="Couldn't connect")], now - timedelta(hours=2))
        save_status(c.app.state.engine, SyncStatus(now, now), STATUS_SETTING)
        yield c


class TestPages:
    def test_status_page_shows_the_last_7_days(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        page = client.get("/status", headers=auth())
        assert "Recent problems" in page.text
        assert "Sonarr can&#39;t be checked: Couldn&#39;t connect" in page.text and "ongoing" in page.text
        assert "Indexers unavailable" not in page.text
        # how long the open problem has lasted, on its card and in the summary line
        assert "Sonarr down, for 2h 0m" in page.text

    def test_ongoing_incidents_come_first(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", fake_check_all)
        now = utc_now()
        for n in range(12):
            record(client.app.state.engine, [service("plex", problems=[("warn", f"cleared {n}")])], now - timedelta(minutes=60 - n))
            record(client.app.state.engine, [service("plex")], now - timedelta(minutes=59 - n))
        page = client.get("/status", headers=auth()).text
        assert "Sonarr can&#39;t be checked" in page
        assert page.index("Sonarr can&#39;t be checked") < page.index("cleared 11")

    def test_history(self, client):
        page = client.get("/status/history", headers=auth())
        assert page.status_code == 200
        assert "Indexers unavailable" in page.text and "Sonarr can&#39;t be checked" in page.text
        page = client.get("/status/history?days=7&service=radarr", headers=auth())
        assert "No problems in the last 7 days." in page.text

    def test_api(self, client):
        body = client.get("/api/status/history?days=30", headers=auth()).json()
        assert [i["key"] for i in body["incidents"]] == ["down", "Indexers unavailable"]
        assert body["incidents"][0]["ongoing"] is True and body["incidents"][1]["lasted"] == "1d 0h"
        assert [s["service"] for s in body["summary"]] == ["radarr", "sonarr"]
        assert body["summary"][1]["down_seconds"] >= 7200

    @pytest.mark.parametrize("query", ["days=2", "days=x", "service=plexx"])
    def test_bad_filters(self, client, query):
        assert client.get(f"/status/history?{query}", headers=auth()).status_code == 422
        assert client.get(f"/api/status/history?{query}", headers=auth()).status_code == 422

    def test_needs_access(self, client):
        assert client.get("/status/history").status_code == 403
        assert client.get("/api/status/history").status_code == 403
