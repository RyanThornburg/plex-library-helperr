"""Tests for maintenance windows: a backup stopping containers isn't recorded as incidents"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from tests.test_health import service, stored
from tests.test_stats import engine  # noqa: F401 (fixture)
from tests.test_status import EVERYTHING, NOW
from tests.test_web import FakeKeys, auth, fake_run_job
from tests.test_web import settings  # noqa: F401 (fixture)
from web import maintenance
from web.app import create_app
from web.health import record
from web.jobs import utc_now
from web.maintenance import MAX_LENGTH, SETTLE, Window, quieted


def stopped_containers():
    """what the health check sees while appdata backup has radarr and tautulli stopped"""
    return [
        service("radarr", error="Couldn't connect"),
        service("plex", error="Couldn't connect"),
        service("unraid", problems=[("warn", "Container radarr isn't running", "container:radarr")]),
        service("bazarr", problems=[("warn", "Not getting live updates from Radarr", "signalr:radarr")]),
    ]


class TestWindow:
    def test_open_until_settled_after_its_end(self):
        window = Window("Appdata Backup", NOW)
        assert window.active(NOW) and window.active(NOW + MAX_LENGTH)
        # a post-run script that never ran can't silence the health check for good
        assert not window.active(NOW + MAX_LENGTH + timedelta(seconds=1))
        window.ended_at = NOW + timedelta(minutes=20)
        assert window.active(NOW + timedelta(minutes=20) + SETTLE)
        assert not window.active(NOW + timedelta(minutes=21) + SETTLE)
        assert not window.active(NOW - timedelta(seconds=1))

    def test_quieted(self):
        assert quieted("radarr", "down") and quieted("unraid", "container:radarr") and quieted("bazarr", "signalr:sonarr")
        assert not quieted("unraid", "disk:Disk 3") and not quieted("radarr", "queue:abc") and not quieted("radarr", "container:x")

    def test_start_and_end(self, engine):  # noqa: F811
        assert maintenance.end(engine, NOW) is None
        maintenance.start(engine, "  Appdata Backup ", NOW)
        assert maintenance.current(engine, NOW + timedelta(minutes=5)).source == "Appdata Backup"
        ended = maintenance.end(engine, NOW + timedelta(minutes=30))
        assert ended.ended_at == NOW + timedelta(minutes=30)
        # ending twice keeps the first end
        assert maintenance.end(engine, NOW + timedelta(minutes=35)) is None
        assert maintenance.current(engine, NOW + timedelta(minutes=30) + SETTLE + timedelta(seconds=1)) is None

    def test_an_unreadable_window_is_none(self, engine):  # noqa: F811
        from sqlalchemy.orm import Session

        from web.db import AppSetting

        with Session(engine) as session:
            session.add(AppSetting(key=maintenance.SETTING, value="{not json", updated_at=NOW))
            session.commit()
        assert maintenance.current(engine, NOW) is None


class TestRecord:
    def test_stopped_containers_open_no_incidents(self, engine):  # noqa: F811
        maintenance.start(engine, "Appdata Backup", NOW)
        record(engine, stopped_containers(), NOW + timedelta(minutes=5))
        assert stored(engine) == []

    def test_other_problems_are_still_recorded(self, engine):  # noqa: F811
        maintenance.start(engine, "Appdata Backup", NOW)
        record(engine, [service("unraid", problems=[("bad", "Disk 3 is disabled", "disk:Disk 3")])], NOW + timedelta(minutes=5))
        assert [i.key for i in stored(engine)] == ["disk:Disk 3"]

    def test_an_incident_already_open_carries_on(self, engine):  # noqa: F811
        record(engine, [service("radarr", error="Couldn't connect")], NOW - timedelta(minutes=30))
        maintenance.start(engine, "Appdata Backup", NOW)
        record(engine, [service("radarr", error="Couldn't connect")], NOW + timedelta(minutes=5))
        [incident] = stored(engine)
        assert (incident.key, incident.last_seen_at, incident.ended_at) == ("down", NOW + timedelta(minutes=5), None)
        record(engine, [service("radarr")], NOW + timedelta(minutes=10))
        assert stored(engine)[0].ended_at == NOW + timedelta(minutes=10)

    def test_still_down_after_the_window_is_recorded(self, engine):  # noqa: F811
        maintenance.start(engine, "Appdata Backup", NOW)
        maintenance.end(engine, NOW + timedelta(minutes=20))
        record(engine, stopped_containers(), NOW + timedelta(minutes=25))
        assert stored(engine) == []
        after = NOW + timedelta(minutes=20) + SETTLE + timedelta(minutes=1)
        record(engine, stopped_containers(), after)
        assert sorted((i.service, i.key) for i in stored(engine)) == [
            ("bazarr", "signalr:radarr"), ("plex", "down"), ("radarr", "down"), ("unraid", "container:radarr"),
        ]


class TestCommand:
    def test_start_and_end_from_the_command_line(self, engine, capsys):  # noqa: F811
        assert main.main(["--maintenance", "start", "Appdata", "Backup"]) is None
        assert "Maintenance window open for Appdata Backup" in capsys.readouterr().out
        window = maintenance.current(engine, utc_now())
        assert window.source == "Appdata Backup" and window.ended_at is None
        assert main.main(["--maintenance", "end"]) is None
        assert "ended" in capsys.readouterr().out
        assert maintenance.current(engine, utc_now()).ended_at is not None
        assert main.main(["--maintenance", "end"]) is None
        assert "No maintenance window was open" in capsys.readouterr().out

    def test_without_a_name(self, engine, capsys):  # noqa: F811
        main.main(["--maintenance", "start"])
        assert maintenance.current(engine, utc_now()).source == "Maintenance"

    def test_only_start_or_end(self):
        with pytest.raises(SystemExit):
            main.main(["--maintenance", "begin"])


class TestStatusPage:
    @pytest.fixture
    def client(self, settings):  # noqa: F811
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=lambda: EVERYTHING, run_scheduler=False)
        with TestClient(app) as c:
            yield c

    def test_says_a_backup_is_running(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: stopped_containers())
        maintenance.start(client.app.state.engine, "Appdata Backup", utc_now() - timedelta(minutes=5))
        page = client.get("/status", headers=auth()).text
        assert "Appdata Backup is running since" in page
        api = client.get("/api/status", headers=auth()).json()
        assert api["maintenance"]["source"] == "Appdata Backup"
        # what the page checked isn't recorded either
        assert stored(client.app.state.engine) == []

    def test_nothing_said_without_one(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: [])
        assert "is running since" not in client.get("/status", headers=auth()).text
        assert client.get("/api/status", headers=auth()).json()["maintenance"] is None
