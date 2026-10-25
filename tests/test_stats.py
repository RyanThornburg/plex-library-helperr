"""Tests for request stats: the plays/requests sync and the requests page"""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import ConfigSettings, PlexConfig, SeerrConfig, TautulliConfig
from tests.test_web import ORIGIN, FakeKeys, auth, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from web import stats
from web.app import create_app
from web.db import MediaRequest, Play, make_engine, migrate
from web.jobs import utc_now
from web.stats import (
    PlayFact,
    find_plays,
    Requester,
    match_user,
    parse_request,
    play_from_row,
    summarize,
    wanted,
    sync,
    sync_plays,
    sync_requests,
    sync_status,
)

TAUTULLI = TautulliConfig(url="http://tautulli:8181/", apikey="tautulli-key")
SEERR = SeerrConfig(url="http://seerr:5055", apikey="seerr-key")
PLEX = PlexConfig(baseurl="http://plex:32400", token="plex-token")
DAY = datetime(2026, 10, 1)


def ts(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


def history_row(id=1, user_id=10, guid="plex://movie/a", watched_status=1, started=DAY, **kwargs):
    return {
        "id": id,
        "date": ts(started),
        "started": ts(started),
        "stopped": ts(started + timedelta(hours=2)),
        "user_id": user_id,
        "user": f"user{user_id}",
        "media_type": "movie",
        "rating_key": 100,
        "grandparent_rating_key": "",
        "guid": guid,
        "full_title": "Heat",
        "percent_complete": 100 if watched_status == 1 else 40,
        "watched_status": watched_status,
        "play_duration": 7200,
        "transcode_decision": "direct play",
        "platform": "Roku",
        "product": "Plex for Roku",
        "player": "Living Room",
        **kwargs,
    }


def response(body):
    r = MagicMock()
    r.json.return_value = body
    return r


def tautulli_body(data):
    return {"response": {"result": "success", "data": data}}


# ---------------------------------------------------------------------------
# plays
# ---------------------------------------------------------------------------


class TestPlays:
    def test_reads_a_history_row(self):
        play = play_from_row(history_row())
        assert play["id"] == 1 and play["user_id"] == 10 and play["guid"] == "plex://movie/a"
        assert play["started_at"] == DAY and play["watched"] is True
        assert play["grandparent_rating_key"] is None
        assert play["transcode_decision"] == "direct play"

    @pytest.mark.parametrize("status", [0, 0.25, 0.75])
    def test_only_tautullis_watched_status_counts_as_watched(self, status):
        assert play_from_row(history_row(watched_status=status))["watched"] is False

    def test_legacy_guids_lose_their_language(self):
        play = play_from_row(history_row(guid="com.plexapp.agents.thetvdb://121361/6/1?lang=en"))
        assert play["guid"] == "com.plexapp.agents.thetvdb://121361/6/1"
        assert play_from_row(history_row(guid="plex://movie/a?x"))["guid"] == "plex://movie/a?x"

    def test_a_play_in_progress_has_no_id_yet(self):
        assert play_from_row(history_row(id=None)) is None

    def run_sync(self, engine, pages):
        calls = []

        def fake_get(url, params, timeout):
            calls.append(params)
            return response(tautulli_body({"data": pages[params["start"] // stats.TAUTULLI_PAGE]}))

        with patch.object(stats.requests, "get", side_effect=fake_get):
            return sync_plays(engine, TAUTULLI), calls

    def test_copies_every_page_then_only_new_plays(self, engine, monkeypatch):
        monkeypatch.setattr(stats, "TAUTULLI_PAGE", 2)
        first = [[history_row(id=1), history_row(id=2)], [history_row(id=3, started=DAY + timedelta(days=5))]]
        read, calls = self.run_sync(engine, first)
        assert read == 3
        assert "after" not in calls[0] and calls[0]["order_dir"] == "asc"
        # again: from a day before the newest play, existing plays are updated not repeated
        read, calls = self.run_sync(engine, [[history_row(id=3, started=DAY + timedelta(days=5), watched_status=0)]])
        assert calls[0]["after"] == "2026-10-05"
        with Session(engine) as session:
            plays = {p.id: p for p in session.scalars(select(Play))}
        assert sorted(plays) == [1, 2, 3]
        assert plays[3].watched is False

    def test_a_tautulli_error_stops_it(self, engine):
        with patch.object(stats.requests, "get", return_value=response({"response": {"result": "error", "message": "bad key"}})):
            with pytest.raises(stats.StatsError, match="bad key"):
                sync_plays(engine, TAUTULLI)


# ---------------------------------------------------------------------------
# seerr requests
# ---------------------------------------------------------------------------


def seerr_request(id=1, type="movie", user=None, seasons=(), **media):
    return {
        "id": id,
        "status": 2,
        "type": type,
        "is4k": False,
        "createdAt": "2026-09-01T12:00:00.000Z",
        "media": {
            "mediaType": type,
            "tmdbId": 100 + id,
            "tvdbId": 200 + id if type == "tv" else None,
            "status": 5,
            "status4k": 1,
            "mediaAddedAt": "2026-09-03T08:30:00.000Z",
            **media,
        },
        "seasons": [{"seasonNumber": n} for n in seasons],
        "requestedBy": user or {"id": 7, "displayName": "Kev", "plexId": 10, "plexUsername": "kev", "email": "kev@example.com"},
    }


class TestParseRequest:
    def test_a_movie(self):
        r = parse_request(seerr_request())
        assert (r.id, r.media_type, r.tmdb_id, r.tvdb_id) == (1, "movie", 101, None)
        assert (r.status, r.media_status) == ("approved", "available")
        assert r.requested_at == datetime(2026, 9, 1, 12)
        assert r.available_at == datetime(2026, 9, 3, 8, 30)
        assert r.requester == Requester(7, "Kev", 10, "kev", "kev@example.com")

    def test_tv_seasons_sorted(self):
        r = parse_request(seerr_request(type="tv", seasons=(3, 1)))
        assert r.seasons == (1, 3) and r.tvdb_id == 201

    def test_4k_uses_the_4k_status(self):
        r = parse_request({**seerr_request(), "is4k": True})
        assert r.is_4k and r.media_status == "unknown"

    def test_name_falls_back(self):
        r = parse_request(seerr_request(user={"id": 8, "plexUsername": "sam"}))
        assert r.requester.name == "sam" and r.requester.plex_id is None

    @pytest.mark.parametrize(
        "change",
        [{"id": None}, {"createdAt": "nope"}, {"type": "music", "media": {}}, {"requestedBy": None}],
        ids=["id", "time", "type", "user"],
    )
    def test_unreadable_requests_are_skipped(self, change):
        assert parse_request({**seerr_request(), **change}) is None


class TestMatchUser:
    USERS = [
        {"user_id": 10, "username": "kev", "email": "kev@example.com"},
        {"user_id": 11, "username": "Sam", "email": "sam@example.com"},
    ]

    def test_by_plex_id(self):
        assert match_user(Requester(1, "x", 10, None, None), self.USERS) == 10

    def test_by_username_then_email(self):
        assert match_user(Requester(1, "x", 99, "sam", None), self.USERS) == 11
        assert match_user(Requester(1, "x", None, None, "SAM@example.com"), self.USERS) == 11

    def test_no_match(self):
        assert match_user(Requester(1, "x", 99, "nobody", None), self.USERS) is None


# ---------------------------------------------------------------------------
# plex guids and summaries
# ---------------------------------------------------------------------------


def episode(season, number, guid=None):
    return SimpleNamespace(parentIndex=season, index=number, guid=guid or f"plex://episode/{season}-{number}")


def ids(*values):
    return [SimpleNamespace(id=v) for v in values]


def show(key, episodes, guids=()):
    return SimpleNamespace(ratingKey=key, title="Survivor", year=2000, guid=f"plex://show/{key}", guids=ids(*guids), episodes=lambda: episodes)


class FakeIndex:
    def episodes(self, item):
        return item.episodes()


class TestWanted:
    def test_a_movie_with_legacy_agent_guids_from_its_ids(self):
        movie = SimpleNamespace(guid="plex://movie/a", guids=ids("tmdb://101", "imdb://tt1", "tvdb://9"))
        want = wanted(parse_request(seerr_request()), [movie, movie], FakeIndex())
        assert want.guids == {
            "plex://movie/a": "movie",
            "com.plexapp.agents.themoviedb://101": "movie",
            "com.plexapp.agents.imdb://tt1": "movie",
        }
        assert want.items == {"movie"} and not want.show_keys

    def test_only_the_requested_seasons(self):
        s = show(1, [episode(1, 1), episode(2, 1), episode(2, 2)], guids=("tvdb://202",))
        want = wanted(parse_request(seerr_request(type="tv", seasons=(2,))), [s], FakeIndex())
        assert want.items == {(2, 1), (2, 2)}
        assert want.guids["com.plexapp.agents.thetvdb://202/2/1"] == (2, 1)
        assert (want.show_keys, want.tvdb_ids) == ({1}, {"202"})

    def test_no_seasons_means_every_numbered_season(self):
        s = show(1, [episode(0, 1), episode(1, 1)])
        want = wanted(parse_request(seerr_request(type="tv")), [s], FakeIndex())
        assert want.items == {(1, 1)}
        assert want.counts(3) and not want.counts(0)


class TestSummarize:
    ITEMS = {(1, 1), (1, 2), (1, 3)}

    def test_the_requesters_progress_and_others(self):
        plays = [
            PlayFact(10, True, DAY, (1, 1)),
            PlayFact(11, True, DAY, (1, 1)),
            PlayFact(10, False, DAY + timedelta(days=1), (1, 2)),
            PlayFact(12, False, DAY, (1, 3)),
        ]
        s = summarize(self.ITEMS, plays, 10)
        assert (s.items, s.watched, s.started, s.requester_plays, s.last_played_at, s.other_viewers) == (
            3, 1, 2, 2, DAY + timedelta(days=1), 2
        )

    def test_an_episode_counts_once(self):
        plays = [PlayFact(10, True, DAY, (1, 3)), PlayFact(10, True, DAY, (1, 3))]
        s = summarize(self.ITEMS, plays, 10)
        assert (s.watched, s.requester_plays) == (1, 2)

    def test_plays_of_episodes_plex_no_longer_has(self):
        s = summarize(self.ITEMS, [PlayFact(10, True, DAY, (1, 9))], 10)
        assert (s.watched, s.started, s.requester_plays, s.last_played_at) == (0, 0, 1, DAY)

    def test_without_a_matched_requester_everyone_is_another_viewer(self):
        s = summarize(self.ITEMS, [PlayFact(10, True, DAY, (1, 1))], None)
        assert (s.watched, s.requester_plays, s.other_viewers) == (0, 0, 1)

    def test_not_in_plex(self):
        assert summarize(set(), [], 10).items == 0


class TestFindPlays:
    def add(self, engine, *rows):
        with Session(engine) as session:
            for row in rows:
                session.add(Play(**play_from_row(row)))
            session.commit()

    def test_a_shows_plays_by_guid_rating_key_and_legacy_guid(self, engine):
        ep = {"media_type": "episode"}
        self.add(
            engine,
            # moved: another rating key, same episode guid
            history_row(id=1, guid="plex://episode/2-1", grandparent_rating_key=99, parent_media_index=2, media_index=1, **ep),
            # removed from plex since, found by the show's rating key
            history_row(id=2, guid="plex://episode/gone", grandparent_rating_key=5, parent_media_index=2, media_index=7, **ep),
            # before plex's new agents
            history_row(id=3, guid="com.plexapp.agents.thetvdb://202/2/8?lang=en", grandparent_rating_key=40, parent_media_index=2, media_index=8, **ep),
            # a season that wasn't requested
            history_row(id=4, guid="plex://episode/1-1", grandparent_rating_key=5, parent_media_index=1, media_index=1, **ep),
            # another show
            history_row(id=5, guid="plex://episode/x", grandparent_rating_key=6, parent_media_index=2, media_index=1, **ep),
        )
        s = show(5, [episode(2, 1)], guids=("tvdb://202",))
        want = wanted(parse_request(seerr_request(type="tv", seasons=(2,))), [s], FakeIndex())
        with Session(engine) as session:
            keys = sorted(p.key for p in find_plays(session, want))
        assert keys == [(2, 1), (2, 7), (2, 8)]

    def test_a_movie(self, engine):
        self.add(engine, history_row(id=1), history_row(id=2, guid="com.plexapp.agents.themoviedb://101?lang=en"), history_row(id=3, guid="plex://movie/b"))
        movie = SimpleNamespace(guid="plex://movie/a", guids=ids("tmdb://101"))
        want = wanted(parse_request(seerr_request()), [movie], FakeIndex())
        with Session(engine) as session:
            assert [p.key for p in find_plays(session, want)] == ["movie", "movie"]


# ---------------------------------------------------------------------------
# the whole sync
# ---------------------------------------------------------------------------


class FakeSection:
    def __init__(self, type, items):
        self.type = type
        self._items = items

    def search(self, includeGuids):
        assert includeGuids
        return self._items


def plex_item(title, guid, ids, **kwargs):
    return SimpleNamespace(title=title, year=2020, guid=guid, guids=[SimpleNamespace(id=i) for i in ids], **kwargs)


def fake_plex(*sections):
    return SimpleNamespace(library=SimpleNamespace(sections=lambda: list(sections)))


class TestSyncRequests:
    def fake_get(self, requests_page, details=None):
        def get(url, params=None, headers=None, timeout=None):
            if url.endswith("/api/v2"):
                return response(tautulli_body([{"user_id": 10, "username": "kev", "email": "kev@example.com"}]))
            if url.endswith("/api/v1/request"):
                return response({"results": requests_page if params["skip"] == 0 else []})
            if details is not None and "/api/v1/movie/" in url:
                return response(details)
            raise requests.exceptions.ConnectionError(f"unexpected {url}")

        return get

    def run(self, engine, requests_page, plex, details=None):
        with (
            patch.object(stats.requests, "get", side_effect=self.fake_get(requests_page, details)),
            patch.object(stats, "PlexServer", return_value=plex),
        ):
            return sync_requests(engine, SEERR, TAUTULLI, PLEX)

    def add_plays(self, engine, *rows):
        with Session(engine) as session:
            for row in rows:
                session.add(Play(**play_from_row(row)))
            session.commit()

    def test_movies_and_shows_with_the_requesters_plays(self, engine):
        movie = plex_item("Heat", "plex://movie/a", ["tmdb://101", "imdb://tt1"])
        series = plex_item("Survivor", "plex://show/s", ["tvdb://202"], ratingKey=5, episodes=lambda: [episode(1, 1), episode(1, 2)])
        plex = fake_plex(FakeSection("movie", [movie]), FakeSection("show", [series]))
        self.add_plays(
            engine,
            history_row(id=1, guid="plex://movie/a"),
            history_row(id=2, guid="plex://episode/1-1", user_id=11),
            history_row(id=3, guid="plex://episode/1-2", watched_status=0.5),
            # watched before plex's new agents
            history_row(id=4, guid="com.plexapp.agents.thetvdb://202/1/1?lang=en", started=datetime(2021, 1, 1)),
        )
        assert self.run(engine, [seerr_request(1), seerr_request(2, type="tv", seasons=(1,))], plex) == 2
        with Session(engine) as session:
            rows = {r.id: r for r in session.scalars(select(MediaRequest))}
        assert (rows[1].title, rows[1].items, rows[1].watched, rows[1].requester_user_id) == ("Heat", 1, 1, 10)
        assert (rows[2].title, rows[2].items, rows[2].watched, rows[2].started, rows[2].other_viewers) == ("Survivor", 2, 1, 2, 1)

    def test_not_in_plex_gets_its_title_from_seerr_once(self, engine):
        plex = fake_plex(FakeSection("movie", []))
        self.run(engine, [seerr_request(1)], plex, details={"title": "Gone", "releaseDate": "2019-05-01"})
        # a later sync keeps it without asking seerr again
        self.run(engine, [seerr_request(1)], plex)
        with Session(engine) as session:
            row = session.get(MediaRequest, 1)
        assert (row.title, row.year, row.items) == ("Gone", 2019, 0)

    def test_requests_seerr_dropped_are_dropped(self, engine):
        plex = fake_plex(FakeSection("movie", []))
        self.run(engine, [seerr_request(1), seerr_request(2)], plex, details={"title": "X"})
        self.run(engine, [seerr_request(2)], plex)
        with Session(engine) as session:
            assert [r.id for r in session.scalars(select(MediaRequest))] == [2]


class TestSync:
    def test_needs_the_connections(self, engine):
        status = sync(engine, ConfigSettings(plex=None, radarr=None, sonarr=None))
        assert status.error == "Needs Seerr, Tautulli, Plex on Settings → Connections"
        assert sync_status(engine).error == status.error

    def test_copies_plays_before_seerr_is_connected(self, engine):
        settings = ConfigSettings(plex=PLEX, radarr=None, sonarr=None, tautulli=TAUTULLI)
        with patch.object(stats.requests, "get", return_value=response(tautulli_body({"data": [history_row()]}))):
            status = sync(engine, settings)
        assert (status.plays_read, status.error) == (1, "Needs Seerr on Settings → Connections")

    def test_errors_are_saved_without_secrets(self, engine):
        settings = ConfigSettings(plex=PLEX, radarr=None, sonarr=None, tautulli=TAUTULLI, seerr=SEERR)
        error = requests.exceptions.ConnectionError("http://tautulli:8181/api/v2?apikey=tautulli-key failed")
        with patch.object(stats.requests, "get", side_effect=error):
            status = sync(engine, settings)
        assert "tautulli-key" not in status.error and "***" in status.error
        assert sync_status(engine).finished_at is not None


# ---------------------------------------------------------------------------
# the requests page
# ---------------------------------------------------------------------------


def stored_request(id, requester_id=7, requester="Kev", user_id=10, items=1, watched=0, started=0, days=40, type="movie", status="approved", plays=None):
    now = utc_now()
    return MediaRequest(
        id=id, media_type=type, tmdb_id=id, tvdb_id=None, title=f"Title {id}", year=2020,
        status=status, media_status="available", is_4k=False, seasons=[1] if type == "tv" else [],
        requested_at=now - timedelta(days=days + 1), available_at=now - timedelta(days=days),
        requester_id=requester_id, requester=requester, requester_user_id=user_id,
        items=items, watched=watched, started=started, requester_plays=started if plays is None else plays,
        last_played_at=None, other_viewers=0,
        synced_at=now,
    )


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        with Session(c.app.state.engine) as session:
            session.add_all(
                [
                    stored_request(1),  # not started, 40 days
                    stored_request(2, watched=1, started=1),
                    stored_request(3, type="tv", items=10, watched=3, started=4, days=5),
                    stored_request(4, requester_id=8, requester="Sam", user_id=None),
                    stored_request(5, items=0),
                    stored_request(6, status="declined"),
                ]
            )
            session.commit()
        yield c


class TestProgress:
    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({"items": 0}, "unavailable"),
            ({"user_id": None}, "unmatched"),
            ({"watched": 1, "started": 1}, "finished"),
            ({"type": "tv", "items": 5, "started": 2}, "started"),
            # only played episodes plex no longer has
            ({"type": "tv", "items": 5, "plays": 3}, "started"),
            ({}, "not_started"),
        ],
    )
    def test_progress(self, fields, expected):
        from web.stats_routes import progress

        assert progress(stored_request(1, **fields)) == expected


class TestPeriodRange:
    NOW = datetime(2026, 10, 25, 12)

    @pytest.mark.parametrize(
        "period, since, until, expected",
        [
            # the start of the year, not 365 days
            ("year", None, None, (datetime(2026, 1, 1), None)),
            ("last_year", None, None, (datetime(2025, 1, 1), datetime(2026, 1, 1))),
            ("30d", None, None, (datetime(2026, 9, 25, 12), None)),
            ("365d", None, None, (datetime(2025, 10, 25, 12), None)),
            ("all", None, None, (None, None)),
            # until is inclusive
            ("custom", date(2026, 3, 1), date(2026, 3, 31), (datetime(2026, 3, 1), datetime(2026, 4, 1))),
            ("custom", None, None, (None, None)),
        ],
    )
    def test_period_range(self, period, since, until, expected):
        from web.stats_routes import period_range

        assert period_range(period, since, until, self.NOW) == expected


class TestRequestsPage:
    def test_lists_requests_and_requesters(self, client):
        page = client.get("/requests?period=all", headers=auth())
        assert page.status_code == 200
        assert "Title 3" in page.text and "3 of 10 episodes, 1 part watched" in page.text
        assert "no Plex user matched" in page.text

    def test_filters(self, client):
        body = client.get("/api/requests?period=all&progress=not_started&min_days=30", headers=auth()).json()
        # 6 is declined but still a request that wasn't started
        assert sorted(r["id"] for r in body["requests"]) == [1, 6]
        body = client.get("/api/requests?period=all&user=8", headers=auth()).json()
        assert [r["progress"] for r in body["requests"]] == ["unmatched"]
        body = client.get("/api/requests?period=all&type=tv", headers=auth()).json()
        assert [r["id"] for r in body["requests"]] == [3]

    @pytest.mark.parametrize(
        "query",
        ["user=x", "min_days=-1", "type=book", "progress=done", "period=decade", "since=x", "period=custom&until=2026-13-01", "since=2026-02-01&until=2026-01-01"],
    )
    def test_bad_filters(self, client, query):
        assert client.get(f"/api/requests?{query}", headers=auth()).status_code == 422

    def test_users(self, client):
        users = {u["name"]: u for u in client.get("/api/requests/users?period=all", headers=auth()).json()["users"]}
        kev = users["Kev"]
        assert (kev["requests"], kev["available"], kev["finished"], kev["started"], kev["not_started"], kev["stale"]) == (4, 3, 1, 1, 1, 1)
        assert kev["finished_percent"] == 33
        assert users["Sam"]["finished_percent"] is None

    def test_period(self, client):
        # 3 was requested 6 days ago, the rest 41
        body = client.get("/api/requests?period=30d", headers=auth()).json()
        assert [r["id"] for r in body["requests"]] == [3]
        today = utc_now().date()
        since = (today - timedelta(days=10)).isoformat()
        body = client.get(f"/api/requests?since={since}&until={today}", headers=auth()).json()
        assert [r["id"] for r in body["requests"]] == [3]
        # a custom period's dates are ignored by the others
        body = client.get(f"/api/requests?period=all&since={since}", headers=auth()).json()
        assert len(body["requests"]) == 6
        body = client.get("/api/requests/users?period=30d", headers=auth()).json()
        assert [(u["name"], u["requests"]) for u in body["users"]] == [("Kev", 1)]
        assert body["period"] == "30d" and body["to"] is None

    def test_the_page_defaults_to_this_year(self, client):
        page = client.get("/requests", headers=auth())
        assert '<option value="year" selected>' in page.text
        assert "made this year" in page.text

    def test_sync_now(self, client):
        with patch.object(client.app.state.stats, "sync_now") as sync_now:
            response = client.post("/requests/sync", headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303 and sync_now.called

    def test_needs_access(self, client):
        assert client.get("/requests").status_code == 403
        assert client.get("/api/requests").status_code == 403
