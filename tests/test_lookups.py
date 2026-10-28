"""Tests for the settings pages' lookups: snapshots, previews, connection checks"""

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from config.models import MoverRule, ProfileRule
from tests.test_main import make_movie, make_quality_profile, make_root_folder
from web.jobs import utc_now
from web.lookups import (
    ArrLibrary,
    LookupFailed,
    check_connection,
    preview_mover,
    preview_profiler,
    quiet,
    redact,
    snapshot,
)

NOW = utc_now()


class Partial:
    """like an arrapi object: reading an empty field would reload it from the api"""

    def __init__(self, **fields):
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if value is None:
            raise AssertionError(f"{name} read through arrapi, that's a request")
        return value


class TestSnapshot:
    def test_reads_fields_as_loaded(self):
        movie = Partial(
            id=1, title="Heat", path="/m/Heat", added=NOW, genres=["Crime"], certification=None,
            studio="Warner", tagsIds=[3, 9], qualityProfileId=2,
        )
        media = snapshot(movie, {3: "kids"}, {2: "HD-1080p"})
        assert media.certification is None
        # movies have no network
        assert media.network is None
        assert [t.label for t in media.tags] == ["kids", "9"]
        assert media.qualityProfile.name == "HD-1080p"


def movie(added, **kwargs):
    m = make_movie(**kwargs)
    m.added = added
    return m


def library() -> ArrLibrary:
    return ArrLibrary(
        media=[
            movie(NOW, id=1, title="Bluey", genres=["Family", "Animation"], path="/movies/main/Bluey"),
            movie(NOW - timedelta(days=9), id=2, title="Frozen", genres=["Family"], path="/movies/main/Frozen"),
            movie(NOW, id=3, title="Already", genres=["Family"], path="/movies/kids/Already"),
        ],
        root_folders=[make_root_folder("/movies/main"), make_root_folder("/movies/kids")],
        profiles=[make_quality_profile("HD-1080p"), make_quality_profile("NoDownload")],
        tags=[],
        fetched_at=NOW,
    )


class TestPreview:
    def test_mover_skips_media_already_there(self):
        preview = preview_mover(library(), [], MoverRule(path="/movies/kids", genres=["family"]), None, "RADARR")
        assert [m.title for m in preview.matches] == ["Bluey", "Frozen"]
        assert (preview.total, preview.recent, preview.claimed) == (2, 2, 0)

    def test_earlier_movers_and_filter_time(self):
        earlier = [MoverRule(path="/movies/kids", genres=["animation"])]
        preview = preview_mover(library(), earlier, MoverRule(path="/movies/kids", genres=["family"]), timedelta(days=1), "RADARR")
        assert [m.title for m in preview.matches] == ["Frozen"]
        assert (preview.claimed, preview.recent) == (1, 0)

    def test_unknown_root_folder(self):
        preview = preview_mover(library(), [], MoverRule(path="/movies/nope", genres=["family"]), None, "RADARR")
        assert preview.problem == "/movies/nope isn't a root folder in Radarr"

    def test_profiler(self):
        preview = preview_profiler(library(), ProfileRule(profile="NoDownload", titles=["frozen"]), None, "RADARR")
        assert [m.title for m in preview.matches] == ["Frozen"]
        assert preview_profiler(library(), ProfileRule(profile="4K", titles=["x"]), None, "RADARR").problem

    def test_previews_dont_log_each_match(self, caplog):
        caplog.set_level(logging.INFO)
        preview_mover(library(), [], MoverRule(path="/movies/kids", genres=["family"]), None, "RADARR")
        assert "Adding Bluey" not in caplog.text


def test_quiet_only_drops_this_thread():
    target = logging.getLogger("tests.quiet")
    seen = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())
    target.addHandler(handler)
    target.setLevel(logging.INFO)
    with quiet("tests.quiet"):
        target.info("hidden")
    target.info("shown")
    target.removeHandler(handler)
    assert seen == ["shown"]


def test_redact():
    assert redact("http://t/api?apikey=abc123&cmd=x failed", "abc123", None, "") == "http://t/api?apikey=***&cmd=x failed"


def response(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.raise_for_status.side_effect = (
        requests.exceptions.HTTPError(f"{status} Client Error for url http://x?apikey=secret")
        if status >= 400
        else None
    )
    return r


class TestCheckConnection:
    def test_arr(self):
        with patch("web.lookups.requests.get", return_value=response(body={"appName": "Radarr", "version": "6.4.4"})) as get:
            assert check_connection("radarr", {"url": "http://radarr:7878/", "secret": "key"}) == "Radarr 6.4.4"
        get.assert_called_once_with("http://radarr:7878/api/v3/system/status", headers={"X-Api-Key": "key"}, timeout=15)

    def test_tautulli_refusal_is_redacted(self):
        body = {"response": {"result": "error", "message": "Invalid apikey secret"}}
        with patch("web.lookups.requests.get", return_value=response(body=body)):
            with pytest.raises(LookupFailed, match=r"Tautulli refused: Invalid apikey \*\*\*"):
                check_connection("tautulli", {"url": "http://t:8181", "secret": "secret"})

    def test_http_error_is_redacted(self):
        with patch("web.lookups.requests.get", return_value=response(500)):
            with pytest.raises(LookupFailed) as e:
                check_connection("tautulli", {"url": "http://t:8181", "secret": "secret"})
        assert "secret" not in str(e.value)

    def test_autoscan_bad_password(self):
        with patch("web.lookups.requests.get", return_value=response(401)) as get:
            with pytest.raises(LookupFailed, match="username/password"):
                check_connection("autoscan", {"url": "http://a:3030", "username": "me", "secret": "pw"})
        assert get.call_args.kwargs["auth"] == ("me", "pw")

    def test_seerr(self):
        responses = [response(body={"results": []}), response(body={"version": "3.0.1"})]
        with patch("web.lookups.requests.get", side_effect=responses) as get:
            assert check_connection("seerr", {"url": "http://seerr:5055/", "secret": "key"}) == "Seerr 3.0.1"
        assert get.call_args_list[0].args[0] == "http://seerr:5055/api/v1/request"
        assert get.call_args_list[0].kwargs["headers"] == {"X-Api-Key": "key"}

    def test_seerr_bad_key(self):
        with patch("web.lookups.requests.get", return_value=response(403)):
            with pytest.raises(LookupFailed, match="refused the API key"):
                check_connection("seerr", {"url": "http://seerr:5055", "secret": "s3cret"})

    def test_sabnzbd(self):
        with patch("web.lookups.requests.get", return_value=response(body={"queue": {"version": "5.1.3"}})) as get:
            assert check_connection("sabnzbd", {"url": "http://sab:8080/", "secret": "key"}) == "SABnzbd 5.1.3"
        assert get.call_args.args[0] == "http://sab:8080/api"
        assert get.call_args.kwargs["params"]["apikey"] == "key"

    def test_sabnzbd_bad_key(self):
        with patch("web.lookups.requests.get", return_value=response(403)):
            with pytest.raises(LookupFailed, match="SABnzbd refused the API key"):
                check_connection("sabnzbd", {"url": "http://sab:8080", "secret": "s3cret"})

    def test_sabnzbd_error_is_redacted(self):
        with patch("web.lookups.requests.get", return_value=response(500)):
            with pytest.raises(LookupFailed) as e:
                check_connection("sabnzbd", {"url": "http://sab:8080", "secret": "secret"})
        assert "secret" not in str(e.value)

    def test_bazarr(self):
        answer = response(body={"data": {"bazarr_version": "1.6.2"}})
        with patch("web.lookups.requests.get", return_value=answer) as get:
            assert check_connection("bazarr", {"url": "http://bazarr:6767/subs/", "secret": "key"}) == "Bazarr 1.6.2"
        assert get.call_args.args[0] == "http://bazarr:6767/subs/api/system/status"
        assert get.call_args.kwargs["headers"] == {"X-API-KEY": "key"}

    def test_bazarr_without_its_base_url(self):
        page = response(body=None)
        page.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
        with patch("web.lookups.requests.get", return_value=page):
            with pytest.raises(LookupFailed, match="add its base URL"):
                check_connection("bazarr", {"url": "http://bazarr:6767", "secret": "key"})

    def test_plex(self):
        server = SimpleNamespace(friendlyName="rattplex", version="1.42")
        with patch("web.lookups.PlexServer", return_value=server):
            assert check_connection("plex", {"url": "http://plex:32400", "secret": "t"}) == "Plex rattplex 1.42"
