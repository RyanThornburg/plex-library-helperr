"""Tests for Kometa's webhooks and its Status card (web/kometa.py)"""

import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from web import kometa
from web.app import create_app
from web.db import KometaRun, make_engine, migrate
from web.kometa import changes, kometa_answers, record, run_seconds
from web.status import ServiceStatus
from tests.test_web import ORIGIN, FakeKeys, auth, basic, fake_run_job, key_of, live_config, settings  # noqa: F401

NOW = datetime(2026, 11, 14, 18, 0)
RUN_END = {
    "event": "run_end",
    "start_time": "2026-11-14 13:03:39",
    "end_time": "2026-11-14 13:09:59",
    "run_time": "0:06:20",
    "collections_created": 2,
    "collections_modified": 5,
    "collections_deleted": 0,
    "items_added": 14,
    "items_removed": 3,
    "added_to_radarr": 1,
    "added_to_sonarr": 0,
    "names": [{"name": "Oscars", "library": "Movies"}],
}
COLLECTION_ERROR = {
    "event": "error",
    "error": "IMDb Error: List ls091597850 does not exist",
    "critical": False,
    "server_name": "Plex",
    "library_name": "Movies",
    "collection": "Oscars",
}
CRITICAL = {"event": "error", "error": "Config Error: No libraries were found in config", "critical": True}


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


def stored(engine) -> list[KometaRun]:
    return kometa.runs(engine, NOW + timedelta(days=1))


def card(engine, now=NOW) -> ServiceStatus:
    return kometa_answers(ServiceStatus("kometa", "Kometa"), stored(engine), now)


def a_run(engine, start: datetime, minutes: int = 6, errors=()) -> None:
    record(engine, {"event": "run_start"}, start)
    for error in errors:
        record(engine, error, start + timedelta(minutes=1))
    record(engine, {**RUN_END, "run_time": f"0:{minutes:02}:00"}, start + timedelta(minutes=minutes))


class TestRecord:
    def test_a_run_from_start_to_end(self, engine):
        assert record(engine, {"event": "run_start", "start_time": "2026-11-14 13:03:39"}, NOW) == "Kometa run started"
        record(engine, COLLECTION_ERROR, NOW + timedelta(minutes=2))
        record(engine, {"event": "version", "current": "2.2.1", "latest": "2.3.0"}, NOW + timedelta(minutes=1))
        said = record(engine, RUN_END, NOW + timedelta(minutes=6, seconds=20))
        assert said == "Kometa run finished in 6m: collections: 2 created, 5 changed; items: 14 added, 3 removed; 1 sent to Radarr"
        [run] = stored(engine)
        # helperr's own clock, not kometa's zoneless local time
        assert (run.started_at, run.finished_at, run.run_seconds) == (NOW, NOW + timedelta(minutes=6, seconds=20), 380)
        assert run.counts["items_added"] == 14 and run.counts["added_to_sonarr"] == 0
        assert run.errors == [{"text": "IMDb Error: List ls091597850 does not exist", "where": "Movies › Oscars", "critical": False}]
        assert (run.version, run.latest_version) == ("2.2.1", "2.3.0")

    def test_an_end_without_its_start_makes_a_run(self, engine):
        record(engine, RUN_END, NOW)
        [run] = stored(engine)
        assert (run.started_at, run.finished_at) == (NOW - timedelta(minutes=6, seconds=20), NOW)

    def test_an_error_without_a_start_makes_an_open_run(self, engine):
        record(engine, CRITICAL, NOW)
        [run] = stored(engine)
        assert run.finished_at is None and run.errors[0]["critical"] is True and run.errors[0]["where"] == ""

    def test_a_run_left_open_isnt_finished_by_a_later_one(self, engine):
        record(engine, {"event": "run_start"}, NOW - timedelta(days=2))  # kometa restarted mid-run
        a_run(engine, NOW)
        new, old = stored(engine)
        assert old.finished_at is None and new.finished_at is not None

    def test_the_same_error_twice_is_kept_once(self, engine):
        record(engine, {"event": "run_start"}, NOW)
        record(engine, COLLECTION_ERROR, NOW)
        record(engine, COLLECTION_ERROR, NOW)
        record(engine, {**COLLECTION_ERROR, "collection": None, "playlist": "Family"}, NOW)
        assert [e["where"] for e in stored(engine)[0].errors] == ["Movies › Oscars", "Playlist Family"]

    @pytest.mark.parametrize("event", ["changes", "delete", "unknown", None])
    def test_other_events_arent_stored(self, engine, event):
        assert record(engine, {"event": event, "poster": "x" * 100}, NOW) is None
        assert stored(engine) == []

    def test_a_version_outside_a_run_is_dropped(self, engine):
        assert record(engine, {"event": "version", "current": "1", "latest": "2"}, NOW) is None

    def test_old_runs_are_dropped_when_one_starts(self, engine):
        a_run(engine, NOW - kometa.KEEP - timedelta(hours=1))
        a_run(engine, NOW)
        assert [r.started_at for r in stored(engine)] == [NOW]

    def test_bad_values_are_read_safely(self, engine):
        record(engine, {"event": "run_start"}, NOW)
        record(engine, {"event": "error", "error": ["a list"], "critical": "yes", "library_name": {"x": 1}}, NOW)
        record(engine, {**RUN_END, "run_time": 380, "items_added": "lots", "items_removed": -4}, NOW)
        [run] = stored(engine)
        assert run.run_seconds is None and run.counts["items_added"] == 0 and run.counts["items_removed"] == 0
        # only a json true is critical
        assert run.errors == [{"text": "['a list']", "where": "{'x': 1}", "critical": False}]

    def test_forget(self, engine):
        a_run(engine, NOW)
        assert kometa.forget(engine) == 1
        assert stored(engine) == []


@pytest.mark.parametrize(
    "value, seconds",
    [("0:06:20", 380), ("12:00:01", 43201), ("1 day, 2:03:04", 93784), ("3 days, 0:00:00", 259200),
     ("6:20", None), ("x:y:z", None), ("day, 1:00:00", None), (380, None), (None, None)],
)
def test_run_seconds(value, seconds):
    assert run_seconds(value) == seconds


def test_changes():
    assert changes(None) == ""
    assert changes({k: 0 for k in kometa.COUNTS}) == ""
    assert changes({"collections_deleted": 1, "added_to_sonarr": 1200}) == "collections: 1 deleted; 1,200 sent to Sonarr"


class TestCard:
    def test_off_until_kometa_sends_something(self, engine):
        assert card(engine).state == "off"

    def test_a_healthy_run(self, engine):
        a_run(engine, NOW - timedelta(minutes=46))
        status = card(engine)
        assert status.state == "ok" and status.problems == []
        assert status.facts == [
            ("Last run", "40m ago, took 6m"),
            ("Changed", "collections: 2 created, 5 changed; items: 14 added, 3 removed; 1 sent to Radarr"),
        ]
        assert status.summary == "Last run 40m ago, took 6m"

    def test_the_last_finished_runs_errors_are_problems(self, engine):
        a_run(engine, NOW - timedelta(hours=4), errors=[CRITICAL])  # an older run's are gone
        a_run(engine, NOW - timedelta(hours=2), errors=[COLLECTION_ERROR, {**COLLECTION_ERROR, "critical": True, "error": "Library Error: x"}])
        status = card(engine)
        assert status.state == "bad"
        assert [(p.level, p.text) for p in status.problems] == [
            ("bad", "Movies › Oscars: Library Error: x"),
            ("warn", "Movies › Oscars: IMDb Error: List ls091597850 does not exist"),
        ]

    def test_a_running_run_shows_only_its_critical_errors_so_far(self, engine):
        a_run(engine, NOW - timedelta(hours=2), errors=[COLLECTION_ERROR])
        record(engine, {"event": "run_start"}, NOW - timedelta(minutes=3))
        record(engine, {**COLLECTION_ERROR, "error": "IMDb Error: another"}, NOW - timedelta(minutes=2))
        record(engine, CRITICAL, NOW - timedelta(minutes=1))
        status = card(engine)
        assert [p.text for p in status.problems] == [
            "Config Error: No libraries were found in config",
            "Movies › Oscars: IMDb Error: List ls091597850 does not exist",
        ]
        assert ("Running", "started 3m ago") in status.facts
        assert status.summary.endswith(", running now")

    def test_no_finished_run_for_a_day_and_a_half_is_stale_until_its_cadence_is_known(self, engine):
        a_run(engine, NOW - timedelta(hours=36))
        assert card(engine).problems == []
        status = card(engine, NOW + timedelta(hours=2))
        assert [(p.level, p.key, p.text) for p in status.problems] == [("warn", "stale", "No Kometa run has finished in 1d 13h")]

    def test_stale_follows_how_often_kometa_runs(self, engine):
        # every 2 hours, 4 at night, for a day
        starts = [NOW - timedelta(hours=h) for h in (32, 30, 28, 24, 22, 20, 18, 16, 14, 12, 10, 8)]
        for start in starts:
            a_run(engine, start)
        status = card(engine)
        assert ("Runs", "at least every 4h") in status.facts
        # the last finished 7h 54m ago: over 4h x 1.5 + 1h
        assert [p.text for p in status.problems] == ["No Kometa run has finished in 7h 54m; it usually starts one at least every 4h"]
        assert card(engine, NOW - timedelta(hours=1)).problems == []

    def test_a_first_run_that_never_finishes(self, engine):
        record(engine, {"event": "run_start"}, NOW - timedelta(minutes=5))
        status = card(engine)
        assert status.state == "ok" and status.summary == "Running its first run"
        status = card(engine, NOW + timedelta(days=2))
        assert status.problems[0].text == "No Kometa run has finished since the first one started 2d ago"
        assert status.summary == "No run has finished yet"

    def test_a_newer_version_is_a_note(self, engine):
        record(engine, {"event": "run_start"}, NOW - timedelta(minutes=10))
        record(engine, {"event": "version", "current": "2.2.1", "latest": "2.3.0"}, NOW - timedelta(minutes=10))
        record(engine, RUN_END, NOW - timedelta(minutes=4))
        status = card(engine)
        assert status.state == "ok" and status.version == "2.2.1"
        assert [(p.level, p.text) for p in status.problems] == [("info", "Kometa 2.3.0 is out")]

    def test_a_database_problem_is_the_cards_error(self, tmp_path):
        engine = make_engine(tmp_path / "helperr.db")  # not migrated: no table
        status = kometa.kometa_status(engine, NOW)
        assert status.state == "bad" and "database" in status.error


# ---------------------------------------------------------------------------
# the webhook, Status and Settings → Webhook
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def kometa_runs(client) -> list[KometaRun]:
    with Session(client.app.state.engine) as session:
        return list(session.query(KometaRun))


class TestWebhook:
    def test_kometas_url_password_is_the_key(self, client):
        # kometa's requests turns http://helperr:<key>@host/... into basic auth
        response = client.post("/webhooks/kometa", json={"event": "run_start", "start_time": "x"}, headers=basic(key_of(client)))
        assert response.status_code == 200 and response.json() == {"event": "run_start", "recorded": True}
        assert len(kometa_runs(client)) == 1

    def test_needs_the_key(self, client):
        assert client.post("/webhooks/kometa", json={"event": "run_start"}).status_code == 401
        assert client.post("/webhooks/kometa", json={"event": "run_start"}, headers=basic("wrong")).status_code == 401
        assert client.post(f"/webhooks/kometa?apikey={key_of(client)}", json={"event": "run_start"}).status_code == 401
        assert kometa_runs(client) == []

    def test_unused_events_are_accepted(self, client):
        response = client.post("/webhooks/kometa", json={"event": "changes", "additions": []}, headers=basic(key_of(client)))
        assert response.json() == {"event": "changes", "recorded": False}

    def test_kometas_text_stays_on_one_log_line(self, client, caplog):
        caplog.set_level(logging.INFO, logger="web.webhooks")
        key = basic(key_of(client))
        client.post("/webhooks/kometa", json={"event": "run_start"}, headers=key)
        client.post("/webhooks/kometa", json={**CRITICAL, "error": "bad\nERROR 2026-01-01 forged"}, headers=key)
        [line] = [r.getMessage() for r in caplog.records if "forged" in r.getMessage()]
        assert "\n" not in line


class TestPages:
    def test_status_says_how_to_set_kometa_up(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: [])
        page = client.get("/status", headers=auth()).text
        assert '<a href="/settings/webhook#kometa">Send Kometa&#39;s webhooks here</a>' in page

    def test_status_shows_kometas_errors(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: [])
        key = basic(key_of(client))
        client.post("/webhooks/kometa", json={"event": "run_start"}, headers=key)
        client.post("/webhooks/kometa", json={**COLLECTION_ERROR, "collection": "<b>Oscars</b>"}, headers=key)
        client.post("/webhooks/kometa", json=RUN_END, headers=key)
        page = client.get("/status", headers=auth()).text
        assert "1 service needs you" in page
        assert "Movies › &lt;b&gt;Oscars&lt;/b&gt;: IMDb Error: List ls091597850 does not exist" in page
        data = client.get("/api/status", headers=auth()).json()
        [card] = [s for s in data["services"] if s["service"] == "kometa"]
        assert card["state"] == "warn"

    def test_the_health_check_records_kometas_errors(self, client, monkeypatch):
        from web import health

        client.post("/webhooks/kometa", json=CRITICAL, headers=basic(key_of(client)))
        monkeypatch.setattr(health, "check_all", lambda settings, now: [])
        health.check(client.app.state.engine, live_config())
        history = client.get("/api/status/history?service=kometa", headers=auth()).json()
        assert [i["text"] for i in history["incidents"]] == ["Config Error: No libraries were found in config"]

    def test_settings_gives_the_yaml_and_forgets(self, client):
        page = client.get("/settings/webhook", headers=auth()).text
        url = f"http://helperr:{key_of(client)}@plex-library-helperr:"
        assert f"webhooks:\n  run_start: {url}" in page and "  version: " in page
        assert "Nothing received from Kometa yet" in page
        client.post("/webhooks/kometa", json={"event": "run_start"}, headers=basic(key_of(client)))
        assert "Forget Kometa" in client.get("/settings/webhook", headers=auth()).text
        response = client.post("/settings/kometa/forget", headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303 and kometa_runs(client) == []

    def test_forget_needs_the_same_origin(self, client):
        client.post("/webhooks/kometa", json={"event": "run_start"}, headers=basic(key_of(client)))
        response = client.post("/settings/kometa/forget", headers={**auth(), "Origin": "https://evil.example"})
        assert response.status_code == 403 and len(kometa_runs(client)) == 1
