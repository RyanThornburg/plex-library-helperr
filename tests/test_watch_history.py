"""Tests for watch_history.py"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from plexapi import exceptions as plexapi_exceptions

import watch_history
from config.models import PlexConfig, TautulliConfig
from tautulli import history_rows
from watch_history import WatchEvent, get_plex_watches, get_tautulli_watches, get_watch_history

TAUTULLI = TautulliConfig(url="http://tautulli:8181/", apikey="key")
SINCE = datetime(2026, 9, 1)


def ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def tautulli_response(data):
    r = MagicMock()
    r.json.return_value = {"response": {"result": "success", "data": data}}
    return r


def history_row(**kwargs):
    return {
        "grandparent_rating_key": 10,
        "grandparent_title": "Survivor",
        "parent_media_index": 3,
        "media_index": 4,
        "date": ts(datetime(2026, 9, 10)),
        "stopped": ts(datetime(2026, 9, 10, 1)),
        **kwargs,
    }


class TestTautulliWatches:
    def _run(self, rows, metadata=None):
        def fake_get(url, params, timeout):
            if params["cmd"] == "get_history":
                return tautulli_response({"data": rows})
            return tautulli_response(
                metadata if metadata is not None else {"guids": ["tvdb://555"]}
            )

        with patch.object(watch_history.requests, "get", side_effect=fake_get) as get:
            return get_tautulli_watches(TAUTULLI, SINCE), get

    def test_parses_rows_and_maps_tvdb(self):
        events, get = self._run([history_row()])
        assert events == [WatchEvent(555, "Survivor", 3, 4, datetime(2026, 9, 10, 1))]
        assert get.call_args_list[0].args[0] == "http://tautulli:8181/api/v2"

    def test_metadata_fetched_once_per_show(self):
        _, get = self._run([history_row(), history_row(media_index=5)])
        cmds = [c.kwargs["params"]["cmd"] for c in get.call_args_list]
        assert cmds.count("get_metadata") == 1

    def test_legacy_guid(self):
        events, _ = self._run(
            [history_row()],
            metadata={"guid": "com.plexapp.agents.thetvdb://777?lang=en"},
        )
        assert events[0].tvdb_id == 777

    def test_missing_metadata_falls_back_to_title(self):
        events, _ = self._run([history_row()], metadata={})
        assert events[0].tvdb_id is None
        assert events[0].title == "Survivor"

    def test_show_gone_from_plex_falls_back_to_title(self):
        # a moved show has a new rating key, tautulli answers 400 for the old one
        def fake_get(url, params, timeout):
            if params["cmd"] == "get_history":
                return tautulli_response({"data": [history_row()]})
            r = MagicMock(status_code=400)
            r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
            return r

        with patch.object(watch_history.requests, "get", side_effect=fake_get):
            events = get_tautulli_watches(TAUTULLI, SINCE)
        assert events == [WatchEvent(None, "Survivor", 3, 4, datetime(2026, 9, 10, 1))]

    def test_metadata_server_error_returns_none(self):
        def fake_get(url, params, timeout):
            if params["cmd"] == "get_history":
                return tautulli_response({"data": [history_row()]})
            r = MagicMock(status_code=500)
            r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
            return r

        with patch.object(watch_history.requests, "get", side_effect=fake_get):
            assert get_tautulli_watches(TAUTULLI, SINCE) is None

    def test_rows_before_since_dropped(self):
        old = ts(datetime(2026, 8, 31, 23))
        events, _ = self._run([history_row(date=old, stopped=old)])
        assert events == []

    def test_asks_from_the_day_before_since(self):
        # tautulli compares `after` with each play's local date: a server at utc-5 dates a
        # play at 01:00 utc on the 1st as the 31st, and asking from the 1st would miss it
        _, get = self._run([])
        assert get.call_args.kwargs["params"]["after"] == "2026-08-31"

    def test_rows_without_episode_numbers_skipped(self):
        events, _ = self._run([history_row(media_index=None)])
        assert events == []

    def test_request_failure_returns_none(self):
        with patch.object(
            watch_history.requests,
            "get",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            assert get_tautulli_watches(TAUTULLI, SINCE) is None

    def test_error_log_redacts_apikey(self, caplog):
        config = TautulliConfig(url="http://t", apikey="s3cr3t0123")
        error = requests.exceptions.HTTPError(
            "500 Server Error for url: http://t/api/v2?apikey=s3cr3t0123&cmd=get_history"
        )
        with patch.object(watch_history.requests, "get", side_effect=error):
            assert get_tautulli_watches(config, SINCE) is None
        assert "Failed to read watch history" in caplog.text
        assert "s3cr3t0123" not in caplog.text
        assert "apikey=***" in caplog.text

    def test_api_error_returns_none(self):
        r = MagicMock()
        r.json.return_value = {"response": {"result": "error", "message": "bad key"}}
        with patch.object(watch_history.requests, "get", return_value=r):
            assert get_tautulli_watches(TAUTULLI, SINCE) is None

    def test_paginates(self):
        page = [history_row()] * watch_history.HISTORY_PAGE
        pages = iter([page, [history_row()]])

        def fake_get(url, params, timeout):
            if params["cmd"] == "get_history":
                return tautulli_response({"data": next(pages)})
            return tautulli_response({"guids": ["tvdb://1"]})

        with patch.object(watch_history.requests, "get", side_effect=fake_get):
            events = get_tautulli_watches(TAUTULLI, SINCE)
        assert len(events) == watch_history.HISTORY_PAGE + 1


PLEX = PlexConfig("http://plex:32400", "plex-token")
WATCHED = datetime(2026, 9, 10, 20, 30, tzinfo=timezone.utc)


def plex_play(show_key=10, season=3, episode=4, viewed=WATCHED, kind="episode", title="Survivor"):
    return SimpleNamespace(type=kind, grandparentRatingKey=show_key, grandparentTitle=title,
                           parentIndex=season, index=episode, viewedAt=viewed)


def fake_plex(history, shows=None):
    plex = MagicMock()
    plex.history.return_value = history
    shows = {10: SimpleNamespace(guids=[SimpleNamespace(id="tvdb://555")], guid="plex://show/10")} if shows is None else shows

    def fetch(key):
        if key not in shows:
            raise plexapi_exceptions.NotFound("gone")
        return shows[key]

    plex.fetchItem.side_effect = fetch
    return plex


class TestPlexWatches:
    """the fallback without Tautulli: plex's own history, completed plays only"""

    def _run(self, plex):
        with patch.object(watch_history, "setup_plex_server", return_value=plex):
            return get_plex_watches(PLEX, SINCE)

    def test_episode_plays_with_their_shows_tvdb_id(self):
        plex = fake_plex([plex_play(), plex_play(episode=5)])
        assert self._run(plex) == [
            WatchEvent(555, "Survivor", 3, 4, datetime(2026, 9, 10, 20, 30)),
            WatchEvent(555, "Survivor", 3, 5, datetime(2026, 9, 10, 20, 30)),
        ]
        # a show is looked up once, however many of its episodes were played
        plex.fetchItem.assert_called_once_with(10)

    def test_asks_from_since_in_plexs_local_time(self):
        plex = fake_plex([])
        self._run(plex)
        local = SINCE.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        plex.history.assert_called_once_with(mindate=local)

    def test_a_legacy_agent_guid(self):
        shows = {10: SimpleNamespace(guids=[], guid="com.plexapp.agents.thetvdb://777?lang=en")}
        assert self._run(fake_plex([plex_play()], shows))[0].tvdb_id == 777

    def test_a_show_plex_no_longer_has_is_matched_by_title(self):
        [event] = self._run(fake_plex([plex_play(show_key=99, title="Gone")], shows={}))
        assert (event.tvdb_id, event.title) == (None, "Gone")

    def test_movies_and_unnumbered_plays_are_skipped(self):
        plays = [plex_play(kind="movie"), plex_play(season=None), plex_play(episode=None), plex_play(viewed=None)]
        assert self._run(fake_plex(plays)) == []

    def test_a_plex_failure_is_no_history(self):
        plex = fake_plex([])
        plex.history.side_effect = plexapi_exceptions.BadRequest("(500) error")
        assert self._run(plex) is None

    def test_no_server_is_no_history(self):
        assert self._run(None) is None


class TestGetWatchHistory:
    def test_prefers_tautulli(self):
        with (
            patch.object(watch_history, "get_tautulli_watches", return_value=[]) as t,
            patch.object(watch_history, "get_plex_watches") as p,
        ):
            get_watch_history(TAUTULLI, PlexConfig("http://plex", "tok"), SINCE)
        t.assert_called_once()
        p.assert_not_called()

    def test_falls_back_to_plex(self):
        with patch.object(watch_history, "get_plex_watches", return_value=[]) as p:
            get_watch_history(None, PlexConfig("http://plex", "tok"), SINCE)
        p.assert_called_once()

    def test_none_without_sources(self):
        assert get_watch_history(None, None, SINCE) is None


class TestHistoryRows:
    @pytest.mark.parametrize("data", [[], None, {"data": None}, {"data": "x"}, {"data": [1]}])
    def test_an_unexpected_answer_is_no_history_not_no_plays(self, data):
        with pytest.raises(ValueError):
            history_rows(data)

    def test_rows(self):
        assert history_rows({"data": [{"id": 1}]}) == [{"id": 1}]
