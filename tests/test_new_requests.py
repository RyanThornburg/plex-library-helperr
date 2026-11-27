"""Tests for Requests → Add: Seerr's search, a title's details, servers and users, the
request body, the pages and the poster cache"""

import html
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
import urllib3
from fastapi.testclient import TestClient

from config.models import ArrConfig, ConfigSettings, SeerrConfig
from tests.test_web import ORIGIN, FakeKeys, auth
from tests.test_web import settings  # noqa: F401 (fixture)
from web import new_request_routes, new_requests, posters
from web.app import create_app
from web.new_requests import (
    AVAILABLE,
    PENDING,
    PROCESSING,
    UNKNOWN,
    Choices,
    RequestInvalid,
    Server,
    Title,
    User,
    default_server,
    parse_result,
    parse_search,
    parse_server,
    parse_title,
    parse_user,
    plan_request,
    poster_name,
    search,
)
from web.posters import PosterCache
from web.seerr import SeerrDeclined, seerr_post
from web.shell import Needs, build_shell

SEERR = SeerrConfig(url="http://seerr:5055", apikey="seerr-key")


def movie_raw(**changes):
    return {
        "id": 603,
        "mediaType": "movie",
        "title": "The Matrix",
        "releaseDate": "1999-03-30",
        "posterPath": "/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg",
        "overview": "A hacker learns the truth.",
        **changes,
    }


def show_raw(**changes):
    return {
        "id": 1396,
        "mediaType": "tv",
        "name": "Breaking Bad",
        "firstAirDate": "2008-01-20",
        "posterPath": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
        "externalIds": {"tvdbId": 81189},
        "keywords": [],
        "seasons": [
            {"seasonNumber": 0, "name": "Specials", "episodeCount": 9},
            {"seasonNumber": 1, "name": "Season 1", "episodeCount": 7, "airDate": "2008-01-20"},
            {"seasonNumber": 2, "name": "Season 2", "episodeCount": 13, "airDate": "2009-03-08"},
            {"seasonNumber": 3, "name": "Season 3", "episodeCount": 13, "airDate": "2010-03-21"},
        ],
        **changes,
    }


def radarr(id=1, is4k=False, default=True, problem=None, **summary):
    return parse_server(
        {"id": id, "name": "Radarr 4K" if is4k else "Radarr", "is4k": is4k, "isDefault": default,
         "activeProfileId": 4, "activeDirectory": "/movies", "activeTags": [2], **summary},
        None if problem else {
            "profiles": [{"id": 4, "name": "HD-1080p"}, {"id": 5, "name": "Ultra-HD"}],
            "rootFolders": [{"path": "/movies", "freeSpace": 2 * 1024**4}, {"path": "/movies-kids"}],
            "tags": [{"id": 2, "label": "seerr"}, {"id": 3, "label": "kids"}],
        },
        problem,
    )


def sonarr(id=1, is4k=False, default=True):
    return parse_server(
        {"id": id, "name": "Sonarr", "is4k": is4k, "isDefault": default, "activeProfileId": 6,
         "activeDirectory": "/tv", "activeTags": [], "activeAnimeProfileId": 7,
         "activeAnimeDirectory": "/anime", "activeAnimeTags": [9]},
        {"profiles": [{"id": 6, "name": "HD"}, {"id": 7, "name": "Anime"}],
         "rootFolders": [{"path": "/tv"}, {"path": "/anime"}],
         "tags": [{"id": 9, "label": "anime"}]},
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


class TestParse:
    def test_poster_name_only_takes_a_file_name(self):
        assert poster_name("/abc_D-1.jpg") == "abc_D-1.jpg"
        assert poster_name("abc.png") == "abc.png"
        for bad in ("/../etc/passwd", "/a/b.jpg", "/a.gif", "", None, 5, "/a.jpg?x=1", "/a.jpg\n"):
            assert poster_name(bad) is None

    def test_search_keeps_movies_and_shows(self):
        body = {
            "page": 1,
            "totalPages": 3,
            "results": [
                movie_raw(mediaInfo={"status": 5, "status4k": 1}),
                show_raw(),
                {"id": 31, "mediaType": "person", "name": "Tom Hanks"},
                {"mediaType": "movie", "title": "no id"},
                "junk",
            ],
        }
        found = parse_search(body, 1)
        assert [(r.media_type, r.tmdb_id, r.title, r.year) for r in found.results] == [
            ("movie", 603, "The Matrix", 1999),
            ("tv", 1396, "Breaking Bad", 2008),
        ]
        assert found.pages == 3
        assert found.results[0].status == AVAILABLE and found.results[0].status_4k == UNKNOWN
        assert found.results[0].state == ("In Plex", "")
        assert found.results[1].state is None

    def test_users_stop_when_seerr_ignores_skip(self, monkeypatch):
        """the same full page every time would page forever"""
        page = [{"id": i, "displayName": f"User {i}"} for i in range(new_requests.USERS_PAGE)]
        calls = []

        def get(url, key, path, timeout=None, **params):
            calls.append(path)
            return {"id": 1} if path == "auth/me" else {"results": page}

        monkeypatch.setattr("web.new_requests.seerr_get", get)
        found, me = new_requests.users(SEERR)
        assert len(found) == new_requests.USERS_PAGE and me == 1
        assert calls == ["user", "user", "auth/me"]

    def test_a_result_without_dates_or_poster(self):
        result = parse_result({"id": 5, "mediaType": "movie", "title": "Untitled"})
        assert result.year is None and result.poster is None

    def test_search_encodes_the_query_in_the_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "web.new_requests.seerr_get",
            lambda url, key, path, timeout=None, **params: calls.append((path, params)) or {"results": []},
        )
        search(SEERR, "Don't Look Up! (2021) & more", 2)
        assert calls == [("search?query=Don%27t%20Look%20Up%21%20%282021%29%20%26%20more&page=2", {})]

    def test_movie_details(self):
        title = parse_title(
            "movie",
            movie_raw(
                runtime=136,
                genres=[{"name": "Action"}, {"name": "Science Fiction"}],
                mediaInfo={"status": 3, "status4k": 1, "plexUrl": "https://app.plex.tv/desktop#!/server/x"},
            ),
        )
        assert (title.title, title.year, title.runtime, title.genres) == ("The Matrix", 1999, 136, ("Action", "Science Fiction"))
        assert title.status == PROCESSING
        assert not title.requestable(False) and title.requestable(True)
        assert title.plex_url == "https://app.plex.tv/desktop#!/server/x"

    def test_a_link_that_isnt_http_is_dropped(self):
        title = parse_title("movie", movie_raw(mediaInfo={"plexUrl": "javascript:alert(1)"}))
        assert title.plex_url is None

    def test_deleted_media_can_be_requested_again(self):
        assert parse_title("movie", movie_raw(mediaInfo={"status": 7})).requestable(False)

    def test_show_seasons_from_plex_and_requests(self):
        title = parse_title(
            "tv",
            show_raw(
                mediaInfo={
                    "status": 4,
                    "seasons": [{"seasonNumber": 1, "status": 5, "status4k": 1}],
                    "requests": [
                        {"status": 1, "is4k": False, "seasons": [{"seasonNumber": 2}]},
                        # declined: season 3 is open again
                        {"status": 3, "is4k": False, "seasons": [{"seasonNumber": 3}]},
                        {"status": 2, "is4k": True, "seasons": [{"seasonNumber": 3}]},
                    ],
                }
            ),
        )
        assert [s.number for s in title.seasons] == [1, 2, 3]  # no specials
        assert [s.status for s in title.seasons] == [AVAILABLE, PENDING, UNKNOWN]
        assert [s.status_4k for s in title.seasons] == [UNKNOWN, UNKNOWN, PROCESSING]
        assert title.open_seasons(False) == [3]
        assert title.open_seasons(True) == [1, 2]
        assert title.tvdb_id == 81189
        assert title.seasons[0].state(False) == ("In Plex", "")

    def test_anime_keyword(self):
        assert parse_title("tv", show_raw(keywords=[{"id": 210024, "name": "anime"}])).anime
        assert not parse_title("tv", show_raw()).anime

    def test_a_title_without_an_id(self):
        with pytest.raises(ValueError):
            parse_title("movie", {"title": "x"})

    def test_server_with_options(self):
        server = radarr()
        assert server.profiles == ((4, "HD-1080p"), (5, "Ultra-HD"))
        assert [f.path for f in server.root_folders] == ["/movies", "/movies-kids"]
        assert server.root_folders[0].free_bytes == 2 * 1024**4
        assert server.tags == ((3, "kids"), (2, "seerr"))  # by label
        assert server.defaults(False) == (4, "/movies", (2,))

    def test_sonarr_anime_defaults(self):
        assert sonarr().defaults(True) == (7, "/anime", (9,))
        assert sonarr().defaults(False) == (6, "/tv", ())

    def test_sonarr_defaults_come_from_its_details(self):
        # seerr's list gives sonarr's tags as [] and no anime tags, the details have them
        server = parse_server(
            {"id": 2, "name": "Sonarr", "activeTags": [], "activeProfileId": 6, "activeDirectory": "/tv"},
            {"server": {"id": 2, "name": "Sonarr", "activeTags": [4], "activeAnimeTags": [9],
                        "activeProfileId": 6, "activeDirectory": "/tv"},
             "profiles": [], "rootFolders": [], "tags": []},
        )
        assert server.default_tags == (4,) and server.anime_tags == (9,)

    def test_completed_requests_dont_hold_seasons_back(self):
        title = parse_title(
            "tv",
            show_raw(mediaInfo={"requests": [{"status": 5, "is4k": False, "seasons": [{"seasonNumber": 2}]}]}),
        )
        assert title.open_seasons(False) == [1, 2, 3]

    def test_seasons_without_episodes_arent_ticked(self):
        raw = show_raw()
        raw["seasons"].append({"seasonNumber": 4, "name": "Season 4", "episodeCount": 0})
        title = parse_title("tv", raw)
        assert title.open_seasons(False) == [1, 2, 3, 4]
        assert title.default_seasons(False) == [1, 2, 3]
        assert plan_request(title, [sonarr()], USERS, Choices()).body["seasons"] == [1, 2, 3]
        assert plan_request(title, [sonarr()], USERS, Choices(server=1, seasons=(4,))).body["seasons"] == [4]

    def test_user_names(self):
        assert parse_user({"id": 3, "displayName": "Kev", "plexUsername": "kev99"}) == User(3, "Kev", "kev99")
        assert parse_user({"id": 4, "email": "a@b.c"}) == User(4, "a@b.c", None)
        assert parse_user({"displayName": "no id"}) is None


# ---------------------------------------------------------------------------
# the request body
# ---------------------------------------------------------------------------


MATRIX = parse_title("movie", movie_raw())
SHOW = parse_title("tv", show_raw(mediaInfo={"seasons": [{"seasonNumber": 1, "status": 5}]}))
USERS = [User(1, "Me"), User(3, "Kev")]


class TestPlanRequest:
    def test_defaults(self):
        planned = plan_request(MATRIX, [radarr()], USERS, Choices())
        assert planned.body == {
            "mediaType": "movie", "mediaId": 603, "is4k": False, "serverId": 1,
            "profileId": 4, "rootFolder": "/movies", "tags": [2],
        }
        assert planned.user is None

    def test_choices(self):
        planned = plan_request(
            MATRIX, [radarr(), radarr(2, is4k=True, default=False)], USERS,
            Choices(server=2, profile=5, root_folder="/movies-kids", tags=(3, 2, 3), user=3),
        )
        assert planned.body == {
            "mediaType": "movie", "mediaId": 603, "is4k": True, "serverId": 2,
            "profileId": 5, "rootFolder": "/movies-kids", "tags": [2, 3], "userId": 3,
        }
        assert planned.user == User(3, "Kev")

    def test_no_tags(self):
        assert plan_request(MATRIX, [radarr()], USERS, Choices(server=1, tags=())).body["tags"] == []

    @pytest.mark.parametrize(
        "choices, message",
        [
            (Choices(server=9), "no Radarr server 9"),
            (Choices(server=1, profile=99), "no quality profile 99"),
            (Choices(server=1, root_folder="/etc"), "/etc isn't a root folder"),
            (Choices(server=1, tags=(77,)), "no tag 77"),
            (Choices(user=42), "no user 42"),
        ],
    )
    def test_choices_seerr_doesnt_offer_are_refused(self, choices, message):
        with pytest.raises(RequestInvalid, match=message):
            plan_request(MATRIX, [radarr()], USERS, choices)

    def test_no_servers(self):
        with pytest.raises(RequestInvalid, match="no Radarr server set up"):
            plan_request(MATRIX, [], USERS, Choices())

    def test_already_in_plex(self):
        there = parse_title("movie", movie_raw(mediaInfo={"status": 5}))
        with pytest.raises(RequestInvalid, match="in Plex or requested"):
            plan_request(there, [radarr()], USERS, Choices())
        with pytest.raises(RequestInvalid, match="in Plex or requested"):
            plan_request(there, [radarr()], USERS, Choices(server=1))

    def test_in_plex_goes_to_the_4k_server(self):
        there = parse_title("movie", movie_raw(mediaInfo={"status": 5, "status4k": 1}))
        found = [radarr(), radarr(2, is4k=True, default=True)]
        assert default_server(found, there).id == 2
        assert plan_request(there, found, USERS, Choices()).body["is4k"] is True

    def test_a_server_whose_options_failed_uses_seerrs_defaults(self):
        broken = radarr(problem="Seerr couldn't read its options: couldn't connect")
        planned = plan_request(MATRIX, [broken], USERS, Choices(server=1))
        assert planned.body == {"mediaType": "movie", "mediaId": 603, "is4k": False, "serverId": 1}
        with pytest.raises(RequestInvalid, match="only Seerr's defaults"):
            plan_request(MATRIX, [broken], USERS, Choices(server=1, profile=4))

    def test_show_takes_the_open_seasons(self):
        planned = plan_request(SHOW, [sonarr()], USERS, Choices())
        assert planned.body["seasons"] == [2, 3]
        assert planned.body["tvdbId"] == 81189
        assert planned.seasons == (2, 3)

    def test_show_picked_seasons(self):
        assert plan_request(SHOW, [sonarr()], USERS, Choices(server=1, seasons=(3,))).body["seasons"] == [3]

    def test_show_season_in_plex_is_refused(self):
        with pytest.raises(RequestInvalid, match="Season 1 is in Plex"):
            plan_request(SHOW, [sonarr()], USERS, Choices(server=1, seasons=(1, 2)))
        with pytest.raises(RequestInvalid, match="at least one season"):
            plan_request(SHOW, [sonarr()], USERS, Choices(server=1, seasons=()))

    def test_anime_show_uses_anime_defaults(self):
        anime = parse_title("tv", show_raw(keywords=[{"id": 210024}]))
        body = plan_request(anime, [sonarr()], USERS, Choices()).body
        assert (body["profileId"], body["rootFolder"], body["tags"]) == (7, "/anime", [9])


# ---------------------------------------------------------------------------
# seerr's answers to a request
# ---------------------------------------------------------------------------


def answer(status, body):
    response = MagicMock(status_code=status)
    response.json.return_value = body
    response.raise_for_status.side_effect = requests.HTTPError(f"{status} Server Error")
    return response


class TestSeerrPost:
    def test_created(self, monkeypatch):
        sent = {}

        def post(url, json=None, headers=None, timeout=None):
            sent.update(url=url, json=json, headers=headers)
            return answer(201, {"id": 12, "status": 2})

        monkeypatch.setattr("web.seerr.requests.post", post)
        assert seerr_post("http://seerr/", "k", "request", {"mediaId": 1}) == {"id": 12, "status": 2}
        assert sent["url"] == "http://seerr/api/v1/request"
        assert sent["headers"] == {"X-Api-Key": "k"}

    @pytest.mark.parametrize("status", [202, 403, 409])
    def test_seerr_says_why(self, monkeypatch, status):
        monkeypatch.setattr("web.seerr.requests.post", lambda *a, **k: answer(status, {"message": "Request for this media already exists."}))
        with pytest.raises(SeerrDeclined, match="already exists"):
            seerr_post("http://seerr", "k", "request", {})

    def test_an_error_without_a_message(self, monkeypatch):
        monkeypatch.setattr("web.seerr.requests.post", lambda *a, **k: answer(502, None))
        with pytest.raises(requests.HTTPError):
            seerr_post("http://seerr", "k", "request", {})


# ---------------------------------------------------------------------------
# the pages
# ---------------------------------------------------------------------------


def seerr_config():
    return ConfigSettings(plex=None, radarr=ArrConfig(baseurl="http://arr", apikey="key", tag=None), sonarr=None, seerr=SEERR)


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), load_config=seerr_config, run_scheduler=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_seerr(monkeypatch):
    """seerr with the matrix, one radarr and two users; sent requests are recorded"""
    seerr = SimpleNamespace(title=MATRIX, servers=[radarr()], users=(USERS, 1), sent=[], made={"id": 40, "status": 2})

    def send(config, planned):
        seerr.sent.append(planned.body)
        if isinstance(seerr.made, Exception):
            raise seerr.made
        return seerr.made

    monkeypatch.setattr(new_request_routes, "title_details", lambda config, media_type, tmdb_id: seerr.title)
    monkeypatch.setattr(new_request_routes, "servers", lambda config, media_type, failure: seerr.servers)
    monkeypatch.setattr(new_request_routes, "users", lambda config: seerr.users)
    monkeypatch.setattr(new_request_routes, "send", send)
    return seerr


def post(client, path, data):
    return client.post(path, data=data, headers={**auth(), **ORIGIN}, follow_redirects=False)


class TestPages:
    def test_search_page(self, client, monkeypatch):
        found = parse_search({"page": 1, "totalPages": 2, "results": [movie_raw(mediaInfo={"status": 5})]}, 1)
        monkeypatch.setattr(new_request_routes, "search", lambda config, query, page: found)
        page = client.get("/requests/new?query=matrix", headers=auth())
        assert page.status_code == 200
        assert 'href="/requests/new/movie/603?query=matrix"' in page.text
        assert 'src="/posters/w185/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg"' in page.text
        assert "In Plex" in page.text
        assert "page=2" in page.text

    def test_search_failure_is_shown_without_the_key(self, client, monkeypatch):
        def fail(config, query, page):
            raise requests.ConnectionError("http://seerr:5055/api/v1/search?apikey=seerr-key")

        monkeypatch.setattr(new_request_routes, "search", fail)
        page = client.get("/requests/new?query=matrix", headers=auth())
        assert "Seerr couldn't search: couldn't connect" in html.unescape(page.text)
        assert "seerr-key" not in page.text

    def test_without_seerr(self, settings):  # noqa: F811
        app = create_app(
            settings, keys=FakeKeys(), run_scheduler=False,
            load_config=lambda: ConfigSettings(plex=None, radarr=None, sonarr=None),
        )
        with TestClient(app) as c:
            page = c.get("/requests/new", headers=auth())
            assert "Connect Seerr" in page.text
            assert c.get("/requests/new/movie/603", headers=auth()).status_code == 409
            assert c.get("/api/requests/search?query=x", headers=auth()).status_code == 409

    def test_bad_params(self, client):
        assert client.get("/requests/new?query=x&page=0", headers=auth()).status_code == 422
        assert client.get("/requests/new?query=" + "x" * 201, headers=auth()).status_code == 422
        assert client.get("/requests/new/person/31", headers=auth()).status_code == 404

    def test_sidebar(self):
        shell = build_shell("/requests/new/movie/3", Needs())
        requests_section = next(s for s in shell.sections if s.key == "requests")
        assert [(c.label, c.current) for c in requests_section.children] == [("Who watches", False), ("Add a request", True)]

    def test_title_form_has_seerrs_defaults(self, client, fake_seerr):
        page = client.get("/requests/new/movie/603?query=matrix", headers=auth()).text
        assert "The Matrix" in page and "(1999)" in page
        assert 'src="/posters/w342/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg"' in page
        assert '<option value="4" selected>HD-1080p</option>' in page
        assert '<option value="/movies" selected>/movies (2.0 TB free)</option>' in page
        assert 'name="tag_1" value="2" checked' in page
        assert 'name="tag_1" value="3">' in page
        assert '<option value="1" selected>Me</option>' in page
        assert 'href="http://seerr:5055/movie/603"' in page

    def test_title_in_plex_has_no_form(self, client, fake_seerr):
        fake_seerr.title = parse_title("movie", movie_raw(mediaInfo={"status": 5}))
        page = client.get("/requests/new/movie/603", headers=auth()).text
        assert "in Plex or requested already" in page
        assert "<form method=\"post\"" not in page

    def test_request(self, client, fake_seerr):
        response = post(
            client, "/requests/new/movie/603",
            {"query": "matrix", "server": "1", "options_1": "1", "profile_1": "5", "root_1": "/movies-kids", "tag_1": "3", "user": "3"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/requests/new/movie/603?done=approved&query=matrix"
        assert fake_seerr.sent == [
            {"mediaType": "movie", "mediaId": 603, "is4k": False, "serverId": 1,
             "profileId": 5, "rootFolder": "/movies-kids", "tags": [3], "userId": 3},
        ]
        assert "Seerr approved it" in client.get(response.headers["location"], headers=auth()).text  # no quote to escape

    def test_unticked_tags_send_none(self, client, fake_seerr):
        post(client, "/requests/new/movie/603", {"server": "1", "options_1": "1", "profile_1": "4", "root_1": "/movies", "user": "1"})
        assert fake_seerr.sent[0]["tags"] == []

    def test_show_request_reads_the_servers_seasons(self, client, fake_seerr):
        fake_seerr.title = SHOW
        fake_seerr.servers = [sonarr()]
        page = client.get("/requests/new/tv/1396", headers=auth()).text
        assert 'name="season_1" value="2" checked' in page
        assert "Season 1" in page and 'disabled checked' in page
        post(client, "/requests/new/tv/1396", {"server": "1", "options_1": "1", "profile_1": "6", "root_1": "/tv", "season_1": "3"})
        assert fake_seerr.sent[0]["seasons"] == [3]

    def test_invalid_choice_rerenders_with_the_reason(self, client, fake_seerr):
        response = post(client, "/requests/new/movie/603", {"server": "1", "options_1": "1", "profile_1": "99", "root_1": "/movies"})
        assert response.status_code == 400
        assert "has no quality profile 99" in response.text
        assert fake_seerr.sent == []

    def test_seerr_declining_is_shown(self, client, fake_seerr):
        fake_seerr.made = SeerrDeclined("Seerr didn't request it: Request for this media already exists.")
        response = post(client, "/requests/new/movie/603", {"server": "1", "options_1": "1", "profile_1": "4", "root_1": "/movies"})
        assert response.status_code == 502
        assert "Request for this media already exists." in response.text

    def test_cross_site_post_is_rejected(self, client, fake_seerr):
        response = client.post("/requests/new/movie/603", data={"server": "1"}, headers={**auth(), "Origin": "https://evil.example"})
        assert response.status_code == 403
        assert fake_seerr.sent == []

    def test_api(self, client, fake_seerr):
        body = client.get("/api/requests/new/movie/603", headers=auth()).json()
        assert body["title"]["title"] == "The Matrix"
        assert body["servers"][0]["defaults"] == {"profile": 4, "root_folder": "/movies", "tags": [2]}
        assert body["servers"][0]["requestable"] is True
        assert body["requested_as"] == 1
        response = client.post("/api/requests/new/movie/603", json={"user": 3}, headers={**auth(), **ORIGIN})
        assert response.status_code == 201
        assert response.json()["request_id"] == 40
        assert fake_seerr.sent[0]["userId"] == 3
        bad = client.post("/api/requests/new/movie/603", json={"tags": "2"}, headers={**auth(), **ORIGIN})
        assert bad.status_code == 422

    @pytest.mark.parametrize("path, kwargs", [
        ("/requests/new/movie/603", {"data": {"server": "1", "options_1": "1", "profile_1": "4", "root_1": "/movies"}}),
        ("/api/requests/new/movie/603", {"json": {}}),
    ], ids=["form", "api"])
    def test_a_slow_seerr_doesnt_block_other_requests(self, client, fake_seerr, monkeypatch, path, kwargs):
        """seerr is asked in a worker thread, the app keeps answering meanwhile"""
        started, release = threading.Event(), threading.Event()

        def slow_send(config, planned):
            started.set()
            release.wait(5)
            return fake_seerr.made

        monkeypatch.setattr(new_request_routes, "send", slow_send)
        sending = threading.Thread(target=lambda: client.post(path, headers={**auth(), **ORIGIN}, follow_redirects=False, **kwargs))
        sending.start()
        try:
            assert started.wait(5)
            began = time.monotonic()
            assert client.get("/healthz").status_code == 200
            assert time.monotonic() - began < 2 and not release.is_set()
        finally:
            release.set()
            sending.join(5)


# ---------------------------------------------------------------------------
# posters
# ---------------------------------------------------------------------------


class FakeImage:
    def __init__(self, data=b"\xff\xd8jpeg", status=200, content_type="image/jpeg"):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.raw = SimpleNamespace(read=lambda n, decode_content=True: data[:n])

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TestPosters:
    def test_fetched_once_then_kept(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: calls.append(url) or FakeImage())
        cache = PosterCache(tmp_path / "posters")
        assert cache.get("w185", "abc.jpg") == (b"\xff\xd8jpeg", "image/jpeg")
        assert cache.get("w185", "abc.jpg") == (b"\xff\xd8jpeg", "image/jpeg")
        assert calls == ["https://image.tmdb.org/t/p/w185/abc.jpg"]
        assert (tmp_path / "posters" / "w185" / "abc.jpg").exists()

    @pytest.mark.parametrize("size, name", [
        ("original", "abc.jpg"), ("w185", "../x.jpg"), ("w185", "x.svg"), ("w185", "a/b.jpg"), ("w185", "abc.jpg\n"),
    ])
    def test_only_tmdb_posters(self, tmp_path, monkeypatch, size, name):
        monkeypatch.setattr(posters.requests, "get", MagicMock(side_effect=AssertionError("not fetched")))
        assert PosterCache(tmp_path).get(size, name) is None

    def test_not_an_image(self, tmp_path, monkeypatch):
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: FakeImage(b"<html>", content_type="text/html"))
        assert PosterCache(tmp_path).get("w185", "abc.jpg") is None
        assert not (tmp_path / "w185" / "abc.jpg").exists()

    def test_too_large(self, tmp_path, monkeypatch):
        monkeypatch.setattr(posters, "MAX_BYTES", 3)
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: FakeImage(b"12345"))
        assert PosterCache(tmp_path).get("w185", "abc.jpg") is None

    def test_tmdb_down(self, tmp_path, monkeypatch):
        monkeypatch.setattr(posters.requests, "get", MagicMock(side_effect=requests.ConnectionError("down")))
        assert PosterCache(tmp_path).get("w185", "abc.jpg") is None

    def test_a_connection_dropped_mid_read(self, tmp_path, monkeypatch):
        """reading the raw body raises urllib3's errors, not requests'"""
        image = FakeImage()
        image.raw = SimpleNamespace(read=MagicMock(side_effect=urllib3.exceptions.ProtocolError("reset")))
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: image)
        assert PosterCache(tmp_path).get("w185", "abc.jpg") is None

    def test_a_failed_write_leaves_no_half_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: FakeImage())
        monkeypatch.setattr(posters.os, "replace", MagicMock(side_effect=OSError("disk full")))
        assert PosterCache(tmp_path).get("w185", "abc.jpg") == (b"\xff\xd8jpeg", "image/jpeg")
        assert list(tmp_path.glob("*/*")) == []

    def test_route_answers_404_for_a_name_with_a_newline(self, client):
        assert client.get("/posters/w185/abc.jpg%0A", headers=auth()).status_code == 404

    def test_oldest_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(posters, "MAX_FILES", 3)
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: FakeImage())
        cache = PosterCache(tmp_path)
        for n in range(4):
            cache.get("w185", f"p{n}.jpg")
        assert len(list(tmp_path.glob("*/*"))) == 3

    def test_route(self, client, monkeypatch):
        monkeypatch.setattr(posters.requests, "get", lambda url, **k: FakeImage())
        response = client.get("/posters/w185/abc.jpg", headers=auth())
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert "max-age" in response.headers["cache-control"]
        assert client.get("/posters/w185/abc.gif", headers=auth()).status_code == 404

    def test_route_needs_sign_in(self, client):
        assert client.get("/posters/w185/abc.jpg").status_code in (401, 403)
