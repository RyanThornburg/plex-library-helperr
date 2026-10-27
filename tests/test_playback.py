"""Tests for playback quality: the playbacks sync, why plays transcoded, and the page"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from tests.test_stats import DAY, PLEX, TAUTULLI, engine, response, tautulli_body, ts  # noqa: F401 (fixture)
from tests.test_web import ORIGIN, FakeKeys, auth, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from web import stats
from web.app import create_app
from web.db import Playback
from web.jobs import utc_now
from web.playback import (
    BACKFILL,
    KEEP,
    STATUS_SETTING,
    causes,
    detail,
    playback_from,
    report,
    stream_data,
    sync,
    sync_playbacks,
)
from web.stats import SyncStatus, save_status, sync_status

NOW = DAY + timedelta(days=40)


def history_row(id=1, user_id=10, started=NOW - timedelta(hours=1), media_type="movie", **kwargs):
    return {
        "id": id,
        "date": ts(started),
        "started": ts(started),
        "user_id": user_id,
        "user": f"user{user_id}",
        "friendly_name": f"Friend {user_id}",
        "media_type": media_type,
        "rating_key": 100 + (id or 0),
        "full_title": f"Title {id}",
        "transcode_decision": "transcode",
        "platform": "Roku",
        "product": "Plex for Roku",
        "player": "Living Room",
        "location": "wan",
        **kwargs,
    }


def stream_row(**kwargs):
    return {
        "quality_profile": "4 Mbps 720p",
        "container": "mkv",
        "stream_container": "mkv",
        "stream_container_decision": "transcode",
        "video_decision": "transcode",
        "video_codec": "h264",
        "stream_video_codec": "h264",
        "video_full_resolution": "1080p",
        "stream_video_full_resolution": "720p",
        "video_dynamic_range": "SDR",
        "stream_video_dynamic_range": "SDR",
        "audio_decision": "transcode",
        "audio_codec": "eac3",
        "stream_audio_codec": "aac",
        "audio_channels": 6,
        "stream_audio_channels": 6,
        "stream_subtitle_decision": "",
        "subtitle_codec": "",
        "bitrate": 10700,
        "stream_bitrate": 3600,
        "transcode_hw_decoding": 1,
        "transcode_hw_encoding": 1,
        **kwargs,
    }


def play(id=1, user_id=10, started=None, decision="transcode", details=True, **kwargs):
    """a Playback like a sync would store, a quality limited transcode by default"""
    fields = playback_from(
        history_row(id=id, user_id=user_id, started=started or NOW - timedelta(hours=id), transcode_decision=decision),
        stream_row(**kwargs) if details else None,
    )
    return Playback(**fields)


DIRECT = dict(
    quality_profile="Original", video_decision="direct play", audio_decision="direct play",
    stream_video_full_resolution="1080p", stream_audio_codec="eac3", transcode_hw_encoding="",
)
# the video plays as it is, only the audio is converted
AUDIO = dict(quality_profile="Original", video_decision="copy", stream_video_full_resolution="1080p")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


class TestPlaybackFrom:
    def test_reads_the_row_and_its_stream(self):
        p = playback_from(history_row(), stream_row())
        assert p["id"] == 1 and p["user"] == "Friend 10" and p["location"] == "wan"
        assert p["details"] is True and p["quality_profile"] == "4 Mbps 720p"
        assert (p["video_resolution"], p["stream_video_resolution"]) == ("1080p", "720p")
        assert p["subtitle_decision"] is None and p["hw_encoding"] is True

    def test_without_stream_data(self):
        p = playback_from(history_row(), None)
        assert p["details"] is False and p["video_decision"] is None

    @pytest.mark.parametrize("row", [history_row(id=None), history_row(media_type="track"), history_row(user_id=None)])
    def test_skipped(self, row):
        assert playback_from(row, stream_row()) is None


class TestStreamData:
    def test_none_when_tautulli_has_none(self):
        error = requests.HTTPError(response=MagicMock(status_code=400))
        failed = response({})
        failed.raise_for_status.side_effect = error
        with patch.object(stats.requests, "get", return_value=failed):
            assert stream_data(TAUTULLI, 1) is None
        with patch.object(stats.requests, "get", return_value=response({"response": {"result": "error", "message": "no"}})):
            assert stream_data(TAUTULLI, 1) is None

    def test_other_errors_stop_the_sync(self):
        with patch.object(stats.requests, "get", side_effect=requests.exceptions.ConnectionError("down")):
            with pytest.raises(requests.exceptions.ConnectionError):
                stream_data(TAUTULLI, 1)


class FakeTautulli:
    def __init__(self, pages, streams=None):
        self.pages = pages
        self.streams = streams or {}
        self.history_calls = []
        self.stream_calls = []

    def get(self, url, params, timeout):
        if params["cmd"] == "get_history":
            self.history_calls.append(params)
            return response(tautulli_body({"data": self.pages[params["start"] // stats.TAUTULLI_PAGE]}))
        self.stream_calls.append(params["row_id"])
        return response(tautulli_body(self.streams.get(params["row_id"], stream_row())))


class TestSyncPlaybacks:
    def run(self, engine, fake, now=NOW):
        with patch.object(stats.requests, "get", side_effect=fake.get):
            return sync_playbacks(engine, TAUTULLI, now)

    def stored(self, engine):
        with Session(engine) as session:
            return {p.id: p for p in session.scalars(select(Playback))}

    def test_adds_new_plays_with_their_stream(self, engine, monkeypatch):
        monkeypatch.setattr(stats, "TAUTULLI_PAGE", 2)
        fake = FakeTautulli(
            [
                [history_row(id=3), history_row(id=2)],
                # a new play shifted the page, music and plays before the backfill are skipped
                [history_row(id=2), history_row(id=9, media_type="track")],
                [history_row(id=1, started=NOW - BACKFILL - timedelta(hours=1))],
            ],
            {3: stream_row(quality_profile="Original")},
        )
        assert self.run(engine, fake) == 2
        assert fake.history_calls[0]["after"] == (NOW - BACKFILL - timedelta(days=1)).date().isoformat()
        assert fake.history_calls[0]["order_dir"] == "desc"
        assert fake.stream_calls == [3, 2]
        assert self.stored(engine)[3].quality_profile == "Original"

    def test_then_only_new_plays(self, engine):
        newest = NOW - timedelta(hours=1)
        self.run(engine, FakeTautulli([[history_row(id=1, started=newest), history_row(id=2, started=NOW - timedelta(days=20))]]))
        fake = FakeTautulli([[history_row(id=3, started=NOW + timedelta(days=3)), history_row(id=1, started=newest)]])
        assert self.run(engine, fake, now=NOW + timedelta(days=4)) == 1
        # from a day before the newest play, not the backfill again
        assert fake.history_calls[0]["after"] == (newest - timedelta(days=2)).date().isoformat()
        assert fake.stream_calls == [3]
        assert sorted(self.stored(engine)) == [1, 2, 3]

    def test_plays_are_kept_for_a_year(self, engine):
        with Session(engine) as session:
            session.add_all([play(1, started=NOW - KEEP - timedelta(hours=1)), play(2, started=NOW - KEEP + timedelta(hours=1))])
            session.commit()
        self.run(engine, FakeTautulli([[]]))
        assert sorted(self.stored(engine)) == [2]

    def test_a_play_without_stream_data_is_kept_once(self, engine):
        failed = response({})
        failed.raise_for_status.side_effect = requests.HTTPError(response=MagicMock(status_code=400))
        fake = FakeTautulli([[history_row(id=1)]])
        real_get = fake.get
        fake.get = lambda url, params, timeout: failed if params["cmd"] == "get_stream_data" else real_get(url, params, timeout)
        assert self.run(engine, fake) == 1
        assert self.stored(engine)[1].details is False
        assert self.run(engine, fake) == 0


class TestSync:
    def test_needs_tautulli(self, engine):
        status = sync(engine, ConfigSettings(plex=None, radarr=None, sonarr=None))
        assert status.error == "Needs Tautulli on Settings → Connections"
        assert sync_status(engine, STATUS_SETTING).error == status.error
        # the requests page's status is separate
        assert sync_status(engine).error is None

    def test_errors_are_saved_without_secrets(self, engine):
        settings = ConfigSettings(plex=PLEX, radarr=None, sonarr=None, tautulli=TAUTULLI)
        error = requests.exceptions.ConnectionError("http://tautulli:8181/api/v2?apikey=tautulli-key failed")
        with patch.object(stats.requests, "get", side_effect=error):
            status = sync(engine, settings)
        assert "tautulli-key" not in status.error and "***" in status.error

    def test_counts_what_it_added(self, engine):
        settings = ConfigSettings(plex=PLEX, radarr=None, sonarr=None, tautulli=TAUTULLI)
        fake = FakeTautulli([[history_row(id=1, started=utc_now() - timedelta(hours=1))]])
        with patch.object(stats.requests, "get", side_effect=fake.get):
            status = sync(engine, settings)
        assert (status.plays_read, status.error) == (1, None)


# ---------------------------------------------------------------------------
# why it transcoded
# ---------------------------------------------------------------------------


class TestCauses:
    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({}, ["quality"]),
            # a quality limit transcodes any file, so the codec change doesn't count
            ({"video_codec": "hevc"}, ["quality"]),
            ({"stream_subtitle_decision": "burn"}, ["quality", "subtitles"]),
            ({"quality_profile": "Original", "stream_subtitle_decision": "burn"}, ["subtitles"]),
            ({"quality_profile": "Original", "video_dynamic_range": "HDR10", "video_codec": "hevc"}, ["hdr"]),
            ({"quality_profile": "Original", "video_codec": "vc1"}, ["video_codec"]),
            ({"quality_profile": "Original", "video_full_resolution": "4k", "stream_video_full_resolution": "1080p"}, ["resolution"]),
            (AUDIO, ["audio"]),
            ({"quality_profile": "Original", "stream_video_full_resolution": "1080p"}, ["unknown"]),
        ],
    )
    def test_causes(self, fields, expected):
        assert causes(play(**fields)) == expected

    def test_none_when_it_didnt_transcode(self):
        assert causes(play(decision="direct play", **DIRECT)) == []
        assert causes(play(decision="copy", **AUDIO)) == []

    def test_unknown_without_stream_data(self):
        assert causes(play(details=False)) == ["unknown"]


class TestDetail:
    def test_a_quality_limited_transcode(self):
        assert detail(play()) == "1080p H264 → 720p H264 · EAC3 5.1 → AAC 5.1 · 4 Mbps 720p · 10.7 → 3.6 Mbps · hardware"

    def test_audio_only(self):
        assert detail(play(audio_codec="dca", **AUDIO)) == "1080p H264 · DTS 5.1 → AAC 5.1 · 10.7 → 3.6 Mbps"

    def test_re_encoded_and_burned_in(self):
        p = play(quality_profile="Original", stream_video_full_resolution="1080p", stream_subtitle_decision="burn",
                 subtitle_codec="pgs", transcode_hw_encoding=0)
        assert detail(p).startswith("1080p H264 re-encoded · EAC3 5.1 → AAC 5.1 · PGS burned in")
        assert detail(p).endswith("software (CPU)")

    def test_a_direct_play(self):
        assert detail(play(decision="direct play", **DIRECT)) == "1080p H264 · EAC3 5.1"

    def test_without_stream_data(self):
        assert detail(play(details=False)) == "No stream details in Tautulli"


class TestReport:
    def plays(self):
        return [
            play(1, user_id=10),
            play(2, user_id=10, quality_profile="8 Mbps 1080p"),
            play(3, user_id=10),
            play(4, user_id=11, decision="direct play", **DIRECT),
            play(5, user_id=11, decision="copy", **{**DIRECT, "video_decision": "copy"}),
            play(6, user_id=12, **AUDIO),
            play(7, user_id=13, **AUDIO),
            play(8, user_id=12, quality_profile="Original", stream_subtitle_decision="burn", subtitle_codec="pgs"),
        ]

    def test_totals(self):
        r = report(self.plays())
        assert (r["plays"], r["direct_play"], r["direct_stream"], r["transcode"]) == (8, 1, 1, 6)
        assert (r["direct_play_percent"], r["transcode_percent"], r["remote"], r["software"]) == (12, 75, 8, 0)

    def test_causes_most_first(self):
        r = report(self.plays())
        assert [(c["cause"], c["plays"], c["users"]) for c in r["causes"]] == [
            ("quality", 3, 1), ("audio", 2, 2), ("subtitles", 1, 1)
        ]
        assert r["causes"][0]["percent"] == 50

    def test_users_worst_first_with_their_quality_limit(self):
        users = report(self.plays())["users"]
        assert [u["user_id"] for u in users] == [10, 12, 13, 11]
        assert users[0]["main_cause"] == "Quality setting" and users[0]["quality_limit"] == "4 Mbps 720p"
        assert users[0]["latest"]["decision"] == "transcode" and users[0]["latest"]["title"] == "Title 1"
        assert users[-1]["transcode"] == 0 and users[-1]["main_cause"] is None

    def test_how_often_a_format_transcodes(self):
        plays = self.plays()
        plays[6].product = "Plex for LG"
        formats = report(plays)["formats"]
        # every play has EAC3 5.1 audio, but quality limited ones count under quality
        assert [(f["cause"], f["format"], f["transcodes"], f["plays"], f["percent"], f["users"]) for f in formats] == [
            ("Audio format", "EAC3 5.1", 2, 8, 25, 2),
            ("Subtitles burned in", "PGS", 1, 1, 100, 1),
        ]
        assert formats[0]["players"] == [("Plex for Roku", 1), ("Plex for LG", 1)]

    def test_a_users_file_format(self):
        users = {u["user_id"]: u for u in report(self.plays())["users"]}
        assert users[13]["file_format"] == "EAC3 5.1" and users[10]["file_format"] is None

    def test_files_for_several_people(self):
        plays = self.plays()
        r = report(plays)
        # every play is its own file
        assert r["files"] == []
        plays[6].rating_key = plays[5].rating_key
        files = report(plays)["files"]
        assert [(f["title"], f["users"], f["plays"], f["causes"]) for f in files] == [("Title 6", 2, 2, ["Audio format"])]

    def test_recent_transcodes(self):
        recent = report(self.plays())["recent"]
        assert [t["title"] for t in recent] == ["Title 1", "Title 2", "Title 3", "Title 6", "Title 7", "Title 8"]
        assert recent[0]["causes"] == ["Quality setting"] and recent[0]["remote"] is True

    def test_nothing(self):
        r = report([])
        assert r["plays"] == 0 and r["transcode_percent"] is None and r["users"] == []


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        now = utc_now()
        with Session(c.app.state.engine) as session:
            session.add_all(
                [
                    play(1, user_id=10, started=now - timedelta(hours=2)),
                    play(2, user_id=11, started=now - timedelta(hours=3), decision="direct play", **DIRECT),
                    play(3, user_id=12, started=now - timedelta(days=3), **AUDIO),
                    play(4, user_id=12, started=now - timedelta(days=20), stream_subtitle_decision="burn", subtitle_codec="pgs"),
                ]
            )
            session.commit()
        yield c


class TestPlaybackPage:
    def test_shows_the_last_7_days(self, client):
        page = client.get("/playback", headers=auth())
        assert page.status_code == 200
        assert "Friend 10" in page.text and "Friend 12" in page.text
        assert "set to 4 Mbps 720p" in page.text
        assert "EAC3 5.1" in page.text
        # 20 days ago is outside the window
        assert "Subtitles burned in" not in page.text
        assert '<option value="7d" selected>' in page.text

    def test_windows_and_users(self, client):
        page = client.get("/playback?window=24h", headers=auth())
        assert "Friend 10" in page.text and "Friend 12" not in page.text
        page = client.get("/playback?window=30d&user=12", headers=auth())
        assert "Subtitles burned in" in page.text and "Title 1" not in page.text
        # the user select still lists everyone in the window
        assert '<option value="10">Friend 10</option>' in page.text

    def test_nothing_yet(self, client):
        assert "Nothing synced yet." in client.get("/playback?user=99", headers=auth()).text
        save_status(client.app.state.engine, SyncStatus(utc_now(), utc_now()), STATUS_SETTING)
        page = client.get("/playback?window=24h&user=99", headers=auth())
        assert "No plays in the last 24 hours for this user." in page.text

    @pytest.mark.parametrize("query", ["window=1y", "user=kev"])
    def test_bad_filters(self, client, query):
        assert client.get(f"/playback?{query}", headers=auth()).status_code == 422
        assert client.get(f"/api/playback?{query}", headers=auth()).status_code == 422

    def test_api(self, client):
        body = client.get("/api/playback?window=30d", headers=auth()).json()
        assert body["window"] == "30d" and body["plays"] == 4 and body["sync_error"] is None
        assert [c["cause"] for c in body["causes"]] == ["quality", "audio"]
        assert body["users"][0]["latest"]["started_at"].startswith(str(utc_now().year))
        assert client.get("/api/playback?user=11", headers=auth()).json()["transcode"] == 0

    def test_sync_now(self, client):
        with patch.object(client.app.state.playback, "sync_now") as sync_now:
            response = client.post("/playback/sync", headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303 and sync_now.called

    def test_needs_access(self, client):
        assert client.get("/playback").status_code == 403
        assert client.get("/api/playback").status_code == 403
