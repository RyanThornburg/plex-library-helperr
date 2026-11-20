"""Tests for the Plex card's own checks: trash, files, settings, CPU transcodes"""

from unittest.mock import patch
from urllib.parse import urlparse

import requests

from config.models import ConfigSettings, EmptyTrashSettings, PlexConfig
from tests.test_status import NOW, SESSION, TAUTULLI, FakeServices, plex_answers, response
from web import plex_health, status
from web.plex_health import Library, file_access, flag, library_problems, settings_problems, software_transcodes
from web.status import ServiceStatus, Stream

PLEX = PlexConfig(baseurl="http://plex:32400", token="plex-token")


def setting(id_, value):
    return {"id": id_, "value": value}


def plex_server(
    prefs=None, movies=(1000, 2), shows=(20000, 0), files=(True, True, True), account=None, hung=False,
    sessions=(), connected=True,
):
    """a fake Plex: (items, trash) per library, whether each newest movie file is
    readable, and Tautulli with its sessions. Records every request"""
    calls = []
    tautulli = FakeServices(plex_answers(connected=connected, sessions=sessions))
    prefs = prefs if prefs is not None else [
        setting("autoEmptyTrash", False), setting("allowMediaDeletion", True),
        setting("PublishServerOnPlexOnlineKey", True), setting("HardwareAcceleratedCodecs", True),
        setting("HardwareAcceleratedEncoders", True),
    ]
    counts = {"1": movies, "2": shows}

    def get(url, timeout, headers=None, params=None):
        params = params or {}
        calls.append((url, headers, params))
        if not url.startswith("http://plex"):
            return tautulli(url, timeout, headers, params)
        assert headers["X-Plex-Token"] == "plex-token"
        assert "plex-token" not in url
        path = urlparse(url).path
        if path == "/:/prefs":
            return response({"MediaContainer": {"Setting": prefs}})
        if path == "/myplex/account":
            return response({"MyPlex": account or {"mappingState": "mapped", "mappingError": ""}})
        if path == "/library/sections":
            return response({"MediaContainer": {"Directory": [
                {"key": "1", "type": "movie", "title": "Movies"},
                {"key": "2", "type": "show", "title": "TV"},
                {"key": "3", "type": "artist", "title": "Music"},
            ]}})
        if path.startswith("/library/sections/"):
            key = path.split("/")[3]
            if params.get("sort"):
                return response({"MediaContainer": {"Metadata": [{"ratingKey": f"{key}0{i}"} for i in range(3)]}})
            items, trash = counts[key]
            return response({"MediaContainer": {"size": 0, "totalSize": trash if params.get("trash") else items}})
        if path.startswith("/library/metadata/"):
            assert params["checkFiles"] == 1
            if hung and path.startswith("/library/metadata/10"):
                raise requests.exceptions.ReadTimeout("read timed out")
            readable = files if path.startswith("/library/metadata/10") else (True,)
            return response({"MediaContainer": {"Metadata": [
                {"Media": [{"Part": [{"accessible": r, "exists": r}]}]} for r in readable
            ]}})
        assert path == "/identity", path
        return response({"MediaContainer": {"version": "1.43.5"}})

    return get, calls


def check(tautulli=TAUTULLI, max_trash=100, **server):
    get, calls = plex_server(**server)
    with patch.object(requests, "get", side_effect=get):
        return status.plex_status(tautulli, PLEX, max_trash), calls


class TestCard:
    def test_healthy(self):
        result, calls = check()
        assert result.state == "ok"
        assert result.problems == []
        assert dict(result.facts)["Trash"] == "Movies 2"
        # music isn't asked about
        assert not any("/sections/3/" in url for url, *_ in calls)
        # the newest three of each library are checked, in one request
        assert any(url.endswith("/library/metadata/100,101,102") for url, *_ in calls)

    def test_empty_trash(self):
        result, _ = check(movies=(1000, 0))
        assert dict(result.facts)["Trash"] == "Empty"

    def test_a_full_trash_is_a_warning(self):
        result, _ = check(movies=(1000, 60), max_trash=50)
        assert result.state == "warn"
        [problem] = result.problems
        assert problem.key == "trash:Movies"
        assert problem.text.startswith("Movies: 60 items in the trash (6% of the library), over empty trash's max_items (50)")

    def test_a_library_gone_to_the_trash_is_bad(self):
        result, _ = check(movies=(1000, 900))
        assert result.state == "bad"
        assert result.problems[0].key == "trash:Movies"

    def test_unreadable_newest_files_are_bad(self):
        result, _ = check(files=(False, False, False))
        assert result.state == "bad"
        [problem] = result.problems
        assert (problem.key, problem.text) == (
            "files:Movies", "Movies: Plex can't read its newest files (3 checked). Is the media mount on the Plex server down?"
        )

    def test_one_unreadable_file_is_left_to_the_next_scan(self):
        result, _ = check(files=(False, True, True))
        assert result.state == "ok"

    def test_a_hung_mount(self):
        result, _ = check(hung=True)
        assert result.state == "bad"
        assert result.problems[0].key == "files:Movies"
        assert "may be hung" in result.problems[0].text

    def test_remote_access_failing(self):
        result, _ = check(account={"mappingState": "failed", "mappingError": "unreachable"})
        assert result.state == "warn"
        assert result.problems[0].key == "remote_access"
        assert "Plex says: unreachable" in result.problems[0].text

    def test_remote_access_off_isnt_asked(self):
        result, calls = check(prefs=[setting("PublishServerOnPlexOnlineKey", False)])
        assert result.state == "ok"
        assert not any(url.endswith("/myplex/account") for url, *_ in calls)

    def test_a_software_transcode_with_hardware_on(self):
        software = {**SESSION, "transcode_hw_encoding": "0"}
        result, _ = check(sessions=[software])
        assert result.state == "warn"
        [problem] = result.problems
        assert problem.key == "software_transcode"
        assert "Justified S01E04 · Ghosts for barry" in problem.text

    def test_a_hardware_transcode_is_fine(self):
        result, _ = check(sessions=[SESSION])
        assert result.state == "ok"

    def test_without_tautulli(self):
        result, calls = check(tautulli=None)
        assert result.state == "ok"
        assert result.version == "1.43.5"
        assert result.streams == []
        assert not any("tautulli" in url for url, *_ in calls)

    def test_plex_failing_with_tautulli_up_is_a_warning(self):
        tautulli = FakeServices(plex_answers())

        def get(url, timeout, headers=None, params=None):
            if url.startswith("http://plex"):
                return response(code=401)
            return tautulli(url, timeout, headers, params)

        with patch.object(requests, "get", side_effect=get):
            result = status.plex_status(TAUTULLI, PLEX)
        assert result.state == "warn"
        assert result.problems[0].text == "Helperr can't check Plex's trash, files and settings: Plex refused the token (HTTP 401)"
        assert dict(result.facts)["Streams"] == "0"

    def test_plex_failing_without_tautulli_downs_the_card(self):
        with patch.object(requests, "get", side_effect=requests.exceptions.ConnectionError("http://plex:32400 refused")):
            [result] = [
                s for s in status.check_all(ConfigSettings(plex=PLEX, radarr=None, sonarr=None), NOW) if s.service == "plex"
            ]
        assert (result.state, result.error) == ("bad", "Couldn't connect")

    def test_tautulli_cant_reach_plex_skips_the_rest(self):
        get, calls = plex_server(connected=False)
        with patch.object(requests, "get", side_effect=get):
            result = status.plex_status(TAUTULLI, PLEX)
        assert result.problems[0].text == "Tautulli can't reach the Plex server"
        assert not any(url.startswith("http://plex") for url, *_ in calls)

    def test_off_without_either(self):
        assert status.plex_status(None, None).state == "off"
        assert status.plex_status(None, PlexConfig(baseurl=None, token=None)).state == "off"

    def test_check_all_uses_empty_trash_max_items(self):
        get, _ = plex_server(movies=(1000, 60))
        settings = ConfigSettings(plex=PLEX, radarr=None, sonarr=None, tautulli=TAUTULLI, empty_trash=EmptyTrashSettings(max_items=50))
        with patch.object(requests, "get", side_effect=get):
            [result] = [s for s in status.check_all(settings, NOW) if s.service == "plex"]
        assert result.problems[0].key == "trash:Movies"


class TestParsers:
    def test_flag(self):
        assert [flag(v) for v in (True, False, 1, 0, "1", "0", "true", "", None, "x")] == [
            True, False, True, False, True, False, True, False, None, None,
        ]

    def test_file_access_skips_trashed_versions_and_unchecked_parts(self):
        body = {"MediaContainer": {"Metadata": [
            {"Media": [
                {"deletedAt": 1700000000, "Part": [{"accessible": False}]},
                {"Part": [{"accessible": True}, {"file": "/x.mkv"}]},
            ]},
            {"Media": [{"Part": [{"accessible": "0"}]}]},
        ]}}
        assert file_access(body) == (2, 1)

    def test_an_uncounted_trash_isnt_called_empty(self):
        card = ServiceStatus("plex", "Plex")
        library_problems(card, [Library("Movies")], 100)
        assert (card.problems, card.facts) == ([], [])

    def test_nothing_checked_is_no_problem(self):
        card = ServiceStatus("plex", "Plex")
        library_problems(card, [Library("Movies", items=0, trash=0)], 100)
        assert card.problems == []

    def test_auto_empty_trash_on(self):
        card = ServiceStatus("plex", "Plex")
        settings_problems(card, {"autoEmptyTrash": True}, None)
        assert card.state == "warn"
        assert card.problems[0].key == "pref:autoEmptyTrash"

    def test_notes_dont_raise_the_card(self):
        card = ServiceStatus("plex", "Plex")
        settings_problems(card, {"HardwareAcceleratedCodecs": False, "allowMediaDeletion": False}, None)
        assert card.state == "ok"
        assert [p.level for p in card.problems] == ["info", "info"]
        assert "Hardware transcoding is off" in card.problems[0].text

    def test_hardware_encoding_off_is_a_note_and_cpu_transcodes_expected(self):
        card = ServiceStatus("plex", "Plex")
        settings = {"HardwareAcceleratedCodecs": True, "HardwareAcceleratedEncoders": False}
        settings_problems(card, settings, None)
        software_transcodes(card, [Stream("barry", "Heat", "Transcode", hardware=False)], settings)
        assert card.state == "ok"
        assert "encoding is off" in card.problems[0].text

    def test_cpu_transcodes_without_plex_pass_settings(self):
        card = ServiceStatus("plex", "Plex")
        software_transcodes(card, [Stream("barry", "Heat", "Transcode", hardware=False)], {})
        assert card.problems == []

    def test_a_stream_not_transcoding_video_isnt_counted(self):
        card = ServiceStatus("plex", "Plex")
        software_transcodes(card, [Stream("barry", "Heat", "Direct stream", hardware=None)], {"HardwareAcceleratedCodecs": True})
        assert card.problems == []

    def test_errors_never_hold_the_token(self):
        def get(url, timeout, headers=None, params=None):
            raise requests.exceptions.HTTPError(f"500 Server Error for url {url}?X-Plex-Token=plex-token")

        with patch.object(plex_health.requests, "get", side_effect=get):
            [result] = [
                s for s in status.check_all(ConfigSettings(plex=PLEX, radarr=None, sonarr=None), NOW) if s.service == "plex"
            ]
        assert result.error and "plex-token" not in result.error
