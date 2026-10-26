"""Tests for watch_history.py"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

import watch_history
from config.models import PlexConfig, TautulliConfig
from watch_history import WatchEvent, get_tautulli_watches, get_watch_history

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
        page = [history_row()] * watch_history.TAUTULLI_PAGE_SIZE
        pages = iter([page, [history_row()]])

        def fake_get(url, params, timeout):
            if params["cmd"] == "get_history":
                return tautulli_response({"data": next(pages)})
            return tautulli_response({"guids": ["tvdb://1"]})

        with patch.object(watch_history.requests, "get", side_effect=fake_get):
            events = get_tautulli_watches(TAUTULLI, SINCE)
        assert len(events) == watch_history.TAUTULLI_PAGE_SIZE + 1


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
