"""Tests for the library page: everyone's plays of every movie, show and season"""

import csv
import io
from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_web import ORIGIN, FakeKeys, auth, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from web.app import create_app
from web.db import LibraryItem, MediaRequest
from web.jobs import utc_now
from web.library_routes import Filters, csv_cell, state

# one now for every row, so items added the same day tie
NOW = utc_now()


def item(title, kind="movie", library="Movies", added=400, played=None, plays=None, viewers=1, season=None, tmdb_id=None, tvdb_id=None, episodes=None):
    """added/played: days ago"""
    now = NOW
    plays = (0 if played is None else 1) if plays is None else plays
    return LibraryItem(
        kind=kind, rating_key=1, season=season, library=library, title=title, year=2020,
        tmdb_id=tmdb_id, tvdb_id=tvdb_id,
        added_at=now - timedelta(days=added) if added is not None else None,
        episodes=episodes, plays=plays, viewers=viewers if plays else 0,
        last_played_at=now - timedelta(days=played) if played is not None else None,
        last_viewer="kev" if plays else None, synced_at=now,
    )


def request_row(id, type="movie", requester_id=7, requester="Kev", tmdb_id=None, tvdb_id=None, seasons=(), status="approved"):
    now = NOW
    return MediaRequest(
        id=id, media_type=type, tmdb_id=tmdb_id, tvdb_id=tvdb_id, title="x", year=2020,
        status=status, media_status="available", is_4k=False, seasons=list(seasons),
        requested_at=now - timedelta(days=500 - id), available_at=None,
        requester_id=requester_id, requester=requester, requester_user_id=10,
        items=1, watched=0, started=0, requester_plays=0, last_played_at=None, other_viewers=0,
        synced_at=now,
    )


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        with Session(c.app.state.engine) as session:
            session.add_all(
                [
                    item("Never", tmdb_id=1),  # never watched, 400 days
                    item("Stale", played=300, tmdb_id=2),
                    item("Recent", played=3, plays=5, viewers=3, tmdb_id=3),
                    item("New", added=10),
                    item("Declined", tmdb_id=4),
                    item("4K", library="4K Movies", tmdb_id=1),
                    item("Survivor", kind="show", library="TV", played=200, tvdb_id=202, episodes=3),
                    item("Survivor", kind="season", library="TV", season=1, played=200, tvdb_id=202, episodes=2),
                    item("Survivor", kind="season", library="TV", season=2, added=250, tvdb_id=202, episodes=1),
                    request_row(1, tmdb_id=1),
                    request_row(2, tmdb_id=1, requester_id=8, requester="Sam"),
                    request_row(3, tmdb_id=4, status="declined"),
                    request_row(4, type="tv", tvdb_id=202, seasons=(1,), requester_id=8, requester="Sam"),
                    request_row(5, type="tv", tvdb_id=202, seasons=(2,)),
                ]
            )
            session.commit()
        yield c


def titles(body):
    return [(i["title"], i["season"]) for i in body["items"]]


class TestState:
    @pytest.mark.parametrize(
        "fields, expected",
        [
            ({"added": 200}, "never"),
            ({"added": 100}, "new"),
            ({"added": None}, "new"),
            ({"played": 181}, "stale"),
            ({"played": 179}, "recent"),
        ],
    )
    def test_state(self, fields, expected):
        assert state(item("x", **fields), Filters(days=180), utc_now()) == expected


class TestLibraryApi:
    def test_unwatched_by_default_longest_idle_first(self, client):
        body = client.get("/api/library", headers=auth()).json()
        assert body["days"] == 180
        assert titles(body) == [("4K", None), ("Declined", None), ("Never", None), ("Stale", None), ("Survivor", None)]
        never = next(i for i in body["items"] if i["title"] == "Never")
        assert never["state"] == "never" and never["days_since_added"] == 400
        assert [r["name"] for r in never["requesters"]] == ["Kev", "Sam"]
        # a declined request isn't a requester
        assert next(i for i in body["items"] if i["title"] == "Declined")["requesters"] == []

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("watch=never", ["4K", "Declined", "Never"]),
            ("watch=stale", ["Stale", "Survivor"]),
            ("watch=recent", ["Recent"]),
            ("watch=all&type=movie&sort=title", ["4K", "Declined", "Never", "New", "Recent", "Stale"]),
            ("watch=all&type=movie&sort=plays", ["Recent", "Stale", "4K", "Declined", "Never", "New"]),
            ("watch=all&library=4K+Movies", ["4K"]),
            ("type=show&days=100", ["Survivor"]),
            ("days=500", []),
            ("requester=none", ["Declined", "Stale"]),
            ("requester=8&watch=all", ["4K", "Never", "Survivor"]),
            ("sort=title&reverse=1", ["Survivor", "Stale", "Never", "Declined", "4K"]),
        ],
    )
    def test_filters(self, client, query, expected):
        body = client.get(f"/api/library?{query}", headers=auth()).json()
        assert [t for t, _ in titles(body)] == expected

    def test_seasons(self, client):
        body = client.get("/api/library?type=show&seasons=1&watch=all", headers=auth()).json()
        # season 2 has been idle longest: added 250 days ago, season 1 played 200 days ago
        assert titles(body) == [("Survivor", 2), ("Survivor", 1)]
        # each season's requester is whoever asked for it
        assert [[r["name"] for r in i["requesters"]] for i in body["items"]] == [["Kev"], ["Sam"]]
        assert [i["state"] for i in body["items"]] == ["never", "stale"]

    def test_summaries(self, client):
        body = client.get("/api/library?watch=recent", headers=auth()).json()
        # the summaries count everything in scope, whatever the watch filter
        by_requester = {g["name"]: (g["never"], g["stale"], g["items"]) for g in body["by_requester"]}
        assert by_requester == {"Kev": (2, 1, 3), "Sam": (2, 1, 3), "No request": (1, 1, 4)}
        assert body["by_requester"][-1]["key"] == "none"
        by_library = {g["name"]: (g["never"], g["stale"], g["items"]) for g in body["by_library"]}
        assert by_library == {"4K Movies": (1, 0, 1), "Movies": (2, 1, 5), "TV": (0, 1, 1)}

    @pytest.mark.parametrize("query", ["watch=old", "type=book", "sort=size", "days=-1", "days=x", "requester=x"])
    def test_bad_filters(self, client, query):
        assert client.get(f"/api/library?{query}", headers=auth()).status_code == 422


class TestLibraryPage:
    def test_lists_what_nobody_watches(self, client):
        page = client.get("/library", headers=auth())
        assert page.status_code == 200
        assert "Never watched or stale" in page.text and "By requester" in page.text
        assert "Survivor (2020)" in page.text and "TV, 3 episodes" in page.text
        assert "Recent (2020)" not in page.text
        # the summaries sort client side, the list is capped so it doesn't
        assert page.text.count("data-sortable") == 2

    def test_summary_links_keep_the_filters(self, client):
        page = client.get("/library?days=90", headers=auth()).text
        assert "/library?watch=never&amp;days=90&amp;requester=8" in page
        assert "/library/export.csv?days=90" in page

    def test_a_season_is_named(self, client):
        page = client.get("/library?seasons=1&type=show&watch=all", headers=auth()).text
        assert "Survivor (2020) – Season 2" in page

    def test_a_long_list_shows_the_first_ones(self, client, monkeypatch):
        monkeypatch.setattr("web.library_routes.SHOWN", 2)
        page = client.get("/library", headers=auth()).text
        assert "the first 2 shown" in page and "all=1" in page
        assert "the first 2 shown" not in client.get("/library?all=1", headers=auth()).text

    def test_says_what_isnt_connected(self, client):
        assert "Connect Tautulli and Plex on" in client.get("/library", headers=auth()).text

    def test_sync_now(self, client):
        with patch.object(client.app.state.stats, "sync_now") as sync_now:
            response = client.post("/library/sync", headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/library" and sync_now.called

    def test_needs_access(self, client):
        for path in ("/library", "/api/library", "/library/export.csv"):
            assert client.get(path).status_code == 403


class TestCsv:
    def test_every_matching_row(self, client, monkeypatch):
        monkeypatch.setattr("web.library_routes.SHOWN", 1)
        response = client.get("/library/export.csv?watch=never&sort=title", headers=auth())
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [r["title"] for r in rows] == ["4K", "Declined", "Never"]
        assert rows[2]["requested_by"] == "Kev; Sam" and rows[2]["state"] == "Never watched"
        assert rows[2]["days_since_added"] == "400" and rows[2]["last_played"] == ""

    def test_formulas_are_quoted(self):
        assert csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
        assert csv_cell("-ish") == "'-ish"
        assert csv_cell("Heat") == "Heat" and csv_cell(5) == 5
