"""Tests for the events page (the app's log) and the log files"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import logging_config
from logging_config import hide_secrets
from tests.test_web import FakeKeys, auth, fake_run_job, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from web import logs
from web.app import create_app

LOG = """INFO 2026-11-09 23:51:01,728 [health.py:121]\t HEALTH\t Bazarr: Bazarr can't be checked: No answer within 10s
INFO 2026-11-09 23:54:03,566 [h11_impl.py:477]\t 172.18.0.3:49908 - "POST /settings/connections/bazarr HTTP/1.1" 200
ERROR 2026-11-09 23:55:00,001 [jobs.py:405]\t WEB\t movers run 4 failed with log-test-secret
Traceback (most recent call last):
  File "jobs.py", line 1
RuntimeError: boom
WARNING 2026-11-09 23:56:00,001 [plex.py:200]\t PLEX TV Shows: scan in progress, skipping
"""


def utc(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logdir"
    directory.mkdir()
    monkeypatch.setattr(logging_config, "LOG_DIR", directory)
    return directory


class TestParse:
    def test_events_with_their_details(self):
        events = logs.parse(LOG)
        assert [(e.level, e.component, e.request) for e in events] == [
            ("INFO", "HEALTH", False),
            ("INFO", "", True),
            ("ERROR", "WEB", False),
            ("WARNING", "", False),
        ]
        assert events[0].message == "Bazarr: Bazarr can't be checked: No answer within 10s"
        # the traceback belongs to the error
        assert events[2].details == ["Traceback (most recent call last):", '  File "jobs.py", line 1', "RuntimeError: boom"]
        # the file has the container's local time, the pages want utc
        assert events[0].time == utc("2026-11-09 23:51:01")

    def test_lines_before_the_first_event_are_dropped(self):
        assert logs.parse("  a stray traceback line\n") == []


class TestReading:
    def test_newest_first_across_rotated_files(self, log_dir):
        (log_dir / "web.log.1").write_text("INFO 2026-11-08 10:00:00,000 [a.py:1]\t WEB\t older\n")
        (log_dir / "web.log").write_text("INFO 2026-11-09 10:00:00,000 [a.py:1]\t WEB\t newer\n")
        assert [e.message for e in logs.read_events()] == ["newer", "older"]

    def test_filters_and_pages(self, log_dir, monkeypatch):
        (log_dir / "web.log").write_text(LOG)
        page = logs.event_page(logs.EventFilters())
        # http requests are left out unless asked for
        assert [e.level for e in page.events] == ["WARNING", "ERROR", "INFO"]
        assert len(logs.event_page(logs.EventFilters(requests=True)).events) == 4
        assert [e.level for e in logs.event_page(logs.EventFilters("error")).events] == ["ERROR"]
        monkeypatch.setattr(logs, "PAGE", 2)
        second = logs.event_page(logs.EventFilters(page=2))
        assert (second.pages, [e.level for e in second.events]) == (2, ["INFO"])
        # past the last page shows the last
        assert logs.event_page(logs.EventFilters(page=9)).filters.page == 2

    def test_secrets_are_redacted_when_read(self, log_dir):
        # a line written before the secret was known still doesn't show it
        hide_secrets("log-test-secret")
        (log_dir / "web.log").write_text(LOG)
        assert "log-test-secret" not in str([e.to_dict() for e in logs.read_events()])
        assert "log-test-secret" not in logs.log_file("web.log")


class TestFiles:
    def test_lists_log_files_only(self, log_dir):
        for name in ("web.log", "web.log.1", "log.log", "helperr.db", "notes.txt"):
            (log_dir / name).write_text("x")
        assert sorted(f.name for f in logs.log_files()) == ["log.log", "web.log", "web.log.1"]

    @pytest.mark.parametrize("name", ["../helperr.db", "..%2Fhelperr.db", "web.log/../x", "notes.txt", "missing.log"])
    def test_only_log_files_can_be_read(self, log_dir, name):
        (log_dir / "notes.txt").write_text("x")
        assert logs.log_file(name) is None


@pytest.fixture
def client(settings, log_dir):  # noqa: F811
    (log_dir / "web.log").write_text(LOG)
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        yield c


class TestPages:
    def test_events_page(self, client):
        page = client.get("/events", headers=auth()).text
        assert "<h1>Events</h1>" in page
        assert "Bazarr can&#39;t be checked" in page and "POST /settings" not in page
        assert '<span class="event-level state-word state-bad">error</span>' in page
        assert "3 more lines</summary>" in page
        # under Activity in the sidebar
        assert '<a href="/events" aria-current="page">Events</a>' in page
        page = client.get("/events?level=error&requests=1", headers=auth()).text
        assert "Bazarr" not in page and "movers run 4 failed" in page

    def test_bad_filters(self, client):
        assert client.get("/events?level=debug", headers=auth()).status_code == 422
        assert client.get("/events?page=0", headers=auth()).status_code == 422
        assert client.get("/api/events?page=x", headers=auth()).status_code == 422

    def test_api(self, client):
        body = client.get("/api/events?level=warning", headers=auth()).json()
        assert body["total"] == 2 and body["pages"] == 1
        assert body["events"][0]["level"] == "warning" and body["events"][1]["details"].endswith("RuntimeError: boom")

    def test_log_files(self, client):
        page = client.get("/logs", headers=auth()).text
        assert 'href="/logs/files/web.log" download>Download</a>' in page
        response = client.get("/logs/files/web.log", headers=auth())
        assert response.status_code == 200 and "Bazarr" in response.text
        assert response.headers["content-disposition"] == 'attachment; filename="web.log"'
        assert client.get("/logs/files/helperr.db", headers=auth()).status_code == 404
        assert client.get("/logs/files/..%2Fhelperr.db", headers=auth()).status_code == 404

    def test_needs_access(self, client):
        assert client.get("/events").status_code == 403
        assert client.get("/logs/files/web.log").status_code == 403
