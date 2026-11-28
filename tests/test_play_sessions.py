"""Tests for Tautulli's playback webhook and failed starts (web/play_sessions.py)"""

import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from web import play_sessions
from web.app import create_app
from web.db import PlaySession, make_engine, migrate
from web.jobs import utc_now
from web.play_sessions import failed, record, troubles
from web.status import ServiceStatus
from tests.test_web import FakeKeys, auth, basic, key_of, live_config, settings  # noqa: F401

NOW = datetime(2026, 11, 28, 2, 10)
# what tautulli's webhook sends with play_sessions.TEMPLATE: every value a string
PRIMETIME = {
    "session_key": "4141",
    "user_id": "13892756",
    "user": "majcher33",
    "rating_key": "310750",
    "title": "Primetime",
    "media_type": "movie",
    "player": "TV 2025",
    "product": "Plex for Samsung",
    "platform": "Tizen",
    "location": "wan",
    "quality_profile": "Original",
    "transcode_decision": "Direct Play",
    "video_decision": "Direct Play",
    "audio_decision": "Direct Play",
    "subtitle_decision": "",
    "container": "mkv",
    "video_codec": "h264",
    "video_resolution": "1080p",
    "audio_codec": "eac3",
    "audio_channels": "6",
    "audio_profile": "dolby digital plus + dolby atmos",
}


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


def stored(engine) -> list[PlaySession]:
    with Session(engine) as session:
        return list(session.query(PlaySession).order_by(PlaySession.id))


def attempt(engine, at: datetime, key: str, seconds: int = 27, start: int = 0, end: int = 0, **fields) -> None:
    """a session that starts at `at` and stops `seconds` later at offset `end`"""
    payload = {**PRIMETIME, "session_key": key, **fields}
    record(engine, {**payload, "event": "play", "offset": str(start), "stream_seconds": "0"}, at)
    record(engine, {**payload, "event": "stop", "offset": str(end), "stream_seconds": str(seconds)}, at + timedelta(seconds=seconds))


def session(**kwargs) -> PlaySession:
    values = {
        "id": 1, "session_key": "1", "started_at": NOW - timedelta(minutes=5), "ended_at": NOW - timedelta(minutes=4),
        "user_id": 1, "user": "u", "title": "T", "rating_key": 5, "player": "TV", "start_offset": 0,
        "end_offset": 0, "stream_seconds": 20, "error": False,
    }
    return PlaySession(**{**values, **kwargs})


# ---------------------------------------------------------------------------
# the webhook's events
# ---------------------------------------------------------------------------


class TestRecord:
    def test_a_start_and_its_stop_are_one_session(self, engine):
        stored_, said = record(engine, {**PRIMETIME, "event": "play", "offset": "0"}, NOW)
        assert stored_ and said is None
        stored_, said = record(engine, {**PRIMETIME, "event": "stop", "offset": "0", "stream_seconds": "27"}, NOW + timedelta(seconds=27))
        [s] = stored(engine)
        assert (s.user_id, s.user, s.rating_key, s.title, s.player, s.audio_channels) == (13892756, "majcher33", 310750, "Primetime", "TV 2025", 6)
        assert (s.started_at, s.ended_at, s.start_offset, s.end_offset, s.stream_seconds) == (NOW, NOW + timedelta(seconds=27), 0, 0, 27)
        assert said == "majcher33 stopped Primetime on TV 2025 after 27s at 0:00 (direct play of H264 1080p, EAC3 Atmos 5.1)"

    def test_decisions_are_read_as_the_api_says_them(self, engine):
        record(engine, {**PRIMETIME, "event": "play", "transcode_decision": "Direct Stream", "audio_decision": "Transcode"}, NOW)
        [s] = stored(engine)
        assert (s.transcode_decision, s.video_decision, s.audio_decision) == ("copy", "direct play", "transcode")
        assert play_sessions.stream(s).startswith("direct stream of H264 1080p")

    def test_a_stop_whose_start_wasnt_seen(self, engine):
        record(engine, {**PRIMETIME, "event": "stop", "offset": "0", "stream_seconds": "27"}, NOW)
        [s] = stored(engine)
        assert s.started_at == NOW - timedelta(seconds=27) and s.start_offset is None and s.ended_at == NOW

    def test_a_start_sent_twice_is_one_session(self, engine):
        record(engine, {**PRIMETIME, "event": "play"}, NOW)
        record(engine, {**PRIMETIME, "event": "play"}, NOW + timedelta(seconds=5))
        assert len(stored(engine)) == 1

    def test_a_new_item_on_an_open_session_key_closes_the_old_one(self, engine):
        record(engine, {**PRIMETIME, "event": "play"}, NOW)
        record(engine, {**PRIMETIME, "event": "play", "rating_key": "298098", "title": "Backrooms"}, NOW + timedelta(minutes=1))
        old, new = stored(engine)
        assert old.ended_at == NOW + timedelta(minutes=1) and old.stream_seconds is None and not failed(old)
        assert new.title == "Backrooms" and new.ended_at is None

    def test_an_error_marks_the_session_its_stop_closes(self, engine):
        record(engine, {**PRIMETIME, "event": "play"}, NOW)
        record(engine, {**PRIMETIME, "event": "error"}, NOW + timedelta(seconds=3))
        record(engine, {**PRIMETIME, "event": "stop", "stream_seconds": "300", "offset": "290"}, NOW + timedelta(minutes=5))
        [s] = stored(engine)
        assert s.error and s.ended_at is not None and failed(s)

    def test_other_actions_and_missing_ids_are_not_stored(self, engine):
        assert record(engine, {**PRIMETIME, "event": "pause"}, NOW) == (False, None)
        assert record(engine, {**PRIMETIME, "event": "play", "user_id": ""}, NOW) == (False, None)
        assert record(engine, {**PRIMETIME, "event": "play", "session_key": ""}, NOW) == (False, None)
        assert stored(engine) == []

    def test_old_sessions_are_pruned(self, engine):
        attempt(engine, NOW - play_sessions.KEEP - timedelta(days=1), "1")
        record(engine, {**PRIMETIME, "session_key": "2", "event": "play"}, NOW)
        assert [s.session_key for s in stored(engine)] == ["2"]


class TestFailed:
    def test_stopped_quickly_without_playing(self):
        assert failed(session(stream_seconds=27, start_offset=0, end_offset=0))

    def test_a_resumed_session_that_never_moved(self):
        assert failed(session(stream_seconds=40, start_offset=3000, end_offset=3000))

    def test_watched_a_bit(self):
        assert not failed(session(stream_seconds=90, start_offset=0, end_offset=85))

    def test_streamed_a_while(self):
        assert not failed(session(stream_seconds=600, start_offset=0, end_offset=0))

    def test_still_open(self):
        assert not failed(session(ended_at=None, stream_seconds=None))

    def test_start_not_seen_counts_from_the_items_start(self):
        assert failed(session(start_offset=None, end_offset=5))
        assert not failed(session(start_offset=None, end_offset=3000))

    def test_an_error(self):
        assert failed(session(error=True, stream_seconds=900, end_offset=900))


# ---------------------------------------------------------------------------
# what's worth a look
# ---------------------------------------------------------------------------


class TestTroubles:
    def test_tonights_primetime(self, engine):
        # 13 tries in 20 minutes on majcher33's samsung, nothing else played there since
        for i in range(13):
            attempt(engine, NOW - timedelta(minutes=25) + timedelta(minutes=i * 1.5), str(4141 + i), seconds=5 + i * 3)
        [t] = troubles(stored(engine), NOW)
        assert (t.kind, t.user, t.player, len(t.failures), t.key) == ("title", "majcher33", "TV 2025", 13, "start:13892756:310750")
        assert t.text(NOW).startswith("majcher33 couldn't start Primetime on TV 2025: 13 tries in 18m, the last 6m ago (direct play of H264 1080p, EAC3 Atmos 5.1)")

    def test_two_tries_are_someone_changing_their_mind(self, engine):
        attempt(engine, NOW - timedelta(minutes=10), "1")
        attempt(engine, NOW - timedelta(minutes=5), "2")
        assert troubles(stored(engine), NOW) == []

    def test_cleared_once_it_plays_for_them(self, engine):
        for i in range(3):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i))
        attempt(engine, NOW - timedelta(minutes=20), "9", seconds=1800, end=1790)
        assert troubles(stored(engine), NOW) == []

    def test_cleared_while_it_plays(self, engine):
        # watched to the end, tautulli may send no stop: still open after SHORT is playing
        for i in range(3):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i))
        record(engine, {**PRIMETIME, "session_key": "9", "event": "play"}, NOW - play_sessions.SHORT - timedelta(seconds=1))
        assert troubles(stored(engine), NOW) == []

    def test_a_play_before_the_failures_doesnt_clear_them(self, engine):
        attempt(engine, NOW - timedelta(hours=5), "0", seconds=1800, end=1790)
        for i in range(3):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i + 1))
        assert len(troubles(stored(engine), NOW)) == 1

    def test_gone_after_alert_for(self, engine):
        for i in range(3):
            attempt(engine, NOW - play_sessions.ALERT_FOR - timedelta(minutes=30 - i), str(i))
        assert troubles(stored(engine), NOW) == []

    def test_other_people_playing_it_doesnt_clear_it(self, engine):
        for i in range(3):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i))
        attempt(engine, NOW - timedelta(minutes=10), "9", seconds=1800, end=1790, user_id="1", user="edmajcher")
        [t] = troubles(stored(engine), NOW)
        assert t.user == "majcher33"

    def test_a_device_that_starts_nothing(self, engine):
        for i, (key, title) in enumerate([("1", "A"), ("1", "A"), ("1", "A"), ("2", "B")]):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i), rating_key=key, title=title)
        # the title's three tries are part of the device's trouble, not another
        [t] = troubles(stored(engine), NOW)
        assert (t.kind, t.titles, t.key) == ("device", ["A", "B"], "start:13892756:device:TV 2025")
        assert t.text(NOW).startswith("majcher33 couldn't start 2 titles on TV 2025: 4 tries in 3m")

    def test_one_title_on_a_device_is_the_titles_trouble(self, engine):
        for i in range(4):
            attempt(engine, NOW - timedelta(minutes=30 - i), str(i))
        assert [t.kind for t in troubles(stored(engine), NOW)] == ["title"]

    def test_the_plex_cards_problems(self, engine):
        for i in range(3):
            attempt(engine, utc_now() - timedelta(minutes=30 - i), str(i))
        [p] = play_sessions.problems(engine, utc_now())
        assert (p.level, p.key) == ("warn", "start:13892756:310750")


class TestReport:
    def test_lists_failed_starts_and_whether_they_played_since(self, engine):
        for i in range(2):
            attempt(engine, NOW - timedelta(hours=3, minutes=-i), str(i), rating_key="1", title="A")
        attempt(engine, NOW - timedelta(hours=2), "5", seconds=1800, end=1790, rating_key="1", title="A")
        attempt(engine, NOW - timedelta(hours=1), "6", rating_key="2", title="B")
        attempt(engine, NOW - timedelta(minutes=30), "7", seconds=600, end=590, rating_key="3", title="C")
        sessions = play_sessions.window_sessions(engine, NOW - timedelta(days=1))
        r = play_sessions.report(sessions, sessions, NOW)
        assert (r["sessions"], r["failed"], r["troubles"]) == (5, 3, [])
        assert [(row["title"], row["tries"], row["played_since"] is not None) for row in r["rows"]] == [("B", 1, False), ("A", 2, True)]


# ---------------------------------------------------------------------------
# through the app
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        yield c


def fail_three_times(client) -> None:
    key = basic(key_of(client))
    for i in range(3):
        session_key = {"session_key": str(i)}
        client.post("/webhooks/tautulli", json={**PRIMETIME, **session_key, "event": "play", "offset": "0"}, headers=key)
        client.post("/webhooks/tautulli", json={**PRIMETIME, **session_key, "event": "stop", "offset": "0", "stream_seconds": "9"}, headers=key)


class TestWebhook:
    def test_the_urls_password_is_the_key(self, client):
        response = client.post("/webhooks/tautulli", json={**PRIMETIME, "event": "play"}, headers=basic(key_of(client)))
        assert response.status_code == 200 and response.json() == {"event": "play", "recorded": True}

    def test_needs_the_key(self, client):
        assert client.post("/webhooks/tautulli", json={**PRIMETIME, "event": "play"}).status_code == 401
        assert client.post("/webhooks/tautulli", json={**PRIMETIME, "event": "play"}, headers=basic("wrong")).status_code == 401
        assert stored(client.app.state.engine) == []

    def test_other_actions_are_accepted(self, client):
        response = client.post("/webhooks/tautulli", json={**PRIMETIME, "event": "pause"}, headers=basic(key_of(client)))
        assert response.json() == {"event": "pause", "recorded": False}

    def test_a_failed_start_is_one_log_line(self, client, caplog):
        caplog.set_level(logging.INFO, logger="web.webhooks")
        key = basic(key_of(client))
        title = {"title": "Bad\nERROR 2026-01-01 forged"}
        client.post("/webhooks/tautulli", json={**PRIMETIME, **title, "event": "play"}, headers=key)
        client.post("/webhooks/tautulli", json={**PRIMETIME, **title, "event": "stop", "stream_seconds": "9", "offset": "0"}, headers=key)
        [line] = [r.getMessage() for r in caplog.records if "forged" in r.getMessage()]
        assert "\n" not in line and line.startswith("WEB\t Failed start: majcher33 stopped Bad ERROR")


class TestPages:
    def test_status_warns_on_plexs_card(self, client, monkeypatch):
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: [ServiceStatus("plex", "Plex")])
        fail_three_times(client)
        data = client.get("/api/status", headers=auth()).json()
        [card] = [s for s in data["services"] if s["service"] == "plex"]
        assert card["state"] == "warn"
        assert card["problems"][0]["text"].startswith("majcher33 couldn't start Primetime on TV 2025: 3 tries")

    def test_not_on_a_plex_card_that_cant_be_checked(self, client, monkeypatch):
        down = ServiceStatus("plex", "Plex", error="Tautulli timed out")
        monkeypatch.setattr("web.status_routes.check_all", lambda settings, now: [down])
        fail_three_times(client)
        data = client.get("/api/status", headers=auth()).json()
        [card] = [s for s in data["services"] if s["service"] == "plex"]
        assert card["problems"] == []

    def test_the_health_check_records_it(self, client, monkeypatch):
        from web import health

        fail_three_times(client)
        monkeypatch.setattr(health, "check_all", lambda settings, now: [ServiceStatus("plex", "Plex")])
        health.check(client.app.state.engine, live_config())
        history = client.get("/api/status/history?service=plex", headers=auth()).json()
        [incident] = history["incidents"]
        assert incident["key"] == "start:13892756:310750" and incident["ongoing"]

    def test_playback_lists_them(self, client):
        fail_three_times(client)
        page = client.get("/playback", headers=auth()).text
        assert "Couldn't start" in page and "Primetime" in page and "TV 2025" in page
        data = client.get("/api/playback", headers=auth()).json()
        [t] = data["failed_starts"]["troubles"]
        assert (t["user"], t["titles"], t["tries"]) == ("majcher33", ["Primetime"], 3)

    def test_playback_says_how_to_set_it_up(self, client):
        page = client.get("/playback", headers=auth()).text
        assert '<a href="/settings/webhook#tautulli">Settings → Webhook</a>' in page

    def test_settings_gives_the_url_and_json(self, client):
        page = client.get("/settings/webhook", headers=auth()).text
        assert f"http://helperr:{key_of(client)}@plex-library-helperr:" in page and "/webhooks/tautulli" in page
        assert "Nothing received from Tautulli yet" in page
        for trigger in play_sessions.TRIGGERS:
            assert trigger in page
        assert "&#34;session_key&#34;: &#34;{session_key}&#34;" in page
        client.post("/webhooks/tautulli", json={**PRIMETIME, "event": "play"}, headers=basic(key_of(client)))
        assert "Last heard from Tautulli" in client.get("/settings/webhook", headers=auth()).text
