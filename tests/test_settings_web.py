"""Tests for the settings pages and config api, against a real store and fake lookups"""

from datetime import timedelta
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from results import JobResult
from tests.test_main import make_movie, make_quality_profile, make_root_folder
from tests.test_web import FORM, ORIGIN, FakeKeys, auth
from tests.test_web import settings  # noqa: F401 (fixture)
from web.app import create_app
from web.db import RemoverRuleSetting
from web.jobs import utc_now
from web.lookups import ArrLibrary, LookupFailed

NOW = utc_now()


def movie(added, **kwargs):
    m = make_movie(**kwargs)
    m.added = added
    return m


class FakeLookups:
    """a radarr library of three movies, and two plex libraries"""

    def __init__(self):
        self.cleared = 0
        self.library = ArrLibrary(
            media=[
                movie(id=1, title="Bluey", genres=["Family", "Animation"], added=NOW, path="/movies/main/Bluey"),
                movie(id=2, title="Frozen", genres=["Family"], added=NOW - timedelta(days=30), path="/movies/main/Frozen"),
                movie(id=3, title="Heat", genres=["Crime"], added=NOW, path="/movies/main/Heat"),
            ],
            root_folders=[make_root_folder("/movies/main"), make_root_folder("/movies/kids")],
            profiles=[make_quality_profile("HD-1080p"), make_quality_profile("NoDownload")],
            tags=["helperr", "kids"],
            fetched_at=NOW,
        )

    def clear(self):
        self.cleared += 1

    def arr_library(self, arr_name, arr, refresh=False):
        if arr is None or not arr.baseurl:
            raise LookupFailed(f"{arr_name.title()} isn't connected")
        return self.library

    def plex_libraries(self, plex):
        return [("Movies", "movie"), ("Kids Movies", "movie"), ("Music", "artist")]


@pytest.fixture
def runs():
    return []


@pytest.fixture
def client(settings, runs):  # noqa: F811
    def run_job(job: str, config: ConfigSettings) -> JobResult:
        runs.append(config)
        return JobResult(job)

    app = create_app(settings, keys=FakeKeys(), run_job=run_job, run_scheduler=False, lookups=FakeLookups())
    with TestClient(app) as c:
        yield c


def post(client, url: str, fields: dict | list | None = None):
    body = urlencode(fields or {}, doseq=True)
    return client.post(url, content=body, headers={**auth(), **FORM}, follow_redirects=False)


def version(client) -> int:
    return client.get("/api/config", headers=auth()).json()["version"]


def document(client) -> dict:
    return client.get("/api/config", headers=auth()).json()["config"]


def connect_radarr(client):
    response = post(
        client,
        "/settings/connections/radarr",
        {"version": version(client), "action": "save", "url": "http://radarr:7878/", "secret": "radarr-key"},
    )
    assert response.status_code == 303


def store(client):
    return client.app.state.store


class TestGeneral:
    def test_page(self, client):
        page = client.get("/settings", headers=auth()).text
        assert "Follow-up empty trash" in page
        assert 'value="15m"' in page

    def test_save(self, client):
        response = post(
            client,
            "/settings",
            {"version": 0, "dry_run": "on", "filter_time": "2d", "followup_delay": "30m",
             "followup_attempts": "0", "followup_lookback": "1d", "plan_max_age": "3d", "min_free_gb": "250"},
        )
        assert (response.status_code, response.headers["location"]) == (303, "/settings?saved=1")
        assert document(client) == {
            "settings": {"dry_run": True, "filter_time": "2d"},
            "web": {"followup_delay": "30m", "followup_attempts": 0, "followup_lookback": "1d", "plan_max_age": "3d", "min_free_gb": 250},
        }
        assert client.app.state.runner.defaults().plan_max_age == timedelta(days=3)

    def test_invalid_shows_errors_and_saves_nothing(self, client):
        response = post(
            client,
            "/settings",
            {"version": 0, "dry_run": "on", "filter_time": "1 day", "followup_delay": "15m",
             "followup_attempts": "x", "followup_lookback": "2d", "plan_max_age": "7d", "min_free_gb": "-1"},
        )
        assert response.status_code == 400
        assert "invalid duration &#39;1 day&#39;" in response.text
        assert "Follow-up attempts must be a whole number" in response.text
        assert "Low disk space must be at least 0" in response.text
        assert version(client) == 0

    def test_going_live_needs_confirming(self, client):
        fields = {"version": 0, "filter_time": "", "followup_delay": "15m", "followup_attempts": "3",
                  "followup_lookback": "2d", "plan_max_age": "7d", "min_free_gb": "100"}
        response = post(client, "/settings", fields)
        assert response.status_code == 200
        assert "Save and go live" in response.text
        assert "Empty Plex trash" in response.text
        assert version(client) == 0
        response = post(client, "/settings", {**fields, "confirm_live": "1"})
        assert response.status_code == 303
        assert document(client)["settings"]["dry_run"] is False

    def test_stale_page_conflicts(self, client):
        fields = {"dry_run": "on", "filter_time": "", "followup_delay": "15m", "followup_attempts": "3",
                  "followup_lookback": "2d", "plan_max_age": "7d", "min_free_gb": "100"}
        assert post(client, "/settings", {**fields, "version": 0}).status_code == 303
        response = post(client, "/settings", {**fields, "version": 0})
        assert response.status_code == 400
        assert "changed since this page was loaded" in response.text

    def test_imported_toml_is_shown(self, settings, tmp_path, monkeypatch):  # noqa: F811
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "config.toml").write_text("[settings]\ndry_run = true\n[radarr]\nbaseurl = 'http://r'\napikey = 'radarr-key'\n")
        app = create_app(settings, keys=FakeKeys(), run_job=lambda job, s: JobResult(job), run_scheduler=False, lookups=FakeLookups())
        with TestClient(app) as c:
            page = c.get("/settings", headers=auth()).text
            assert "Imported from config.toml" in page
            assert "delete" in page and "config.toml" in page
            assert c.app.state.store.load_settings().radarr.apikey == "radarr-key"

    def test_broken_toml_is_shown(self, settings, tmp_path, monkeypatch):  # noqa: F811
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "config.toml").write_text("[settings]\nfilter_time = 'soon'\n")
        app = create_app(settings, keys=FakeKeys(), run_job=lambda job, s: JobResult(job), run_scheduler=False, lookups=FakeLookups())
        with TestClient(app) as c:
            assert "config.toml couldn't be imported" in c.get("/settings", headers=auth()).text


class TestConnections:
    def test_key_is_saved_encrypted_and_never_shown(self, client, runs):
        connect_radarr(client)
        page = client.get("/settings/connections", headers=auth()).text
        assert "radarr-key" not in page
        assert "saved, leave empty to keep" in page
        assert document(client)["radarr"] == {"baseurl": "http://radarr:7878"}
        assert "radarr-key" not in client.get("/api/config", headers=auth()).text
        assert client.app.state.lookups.cleared == 1
        # jobs run with it
        client.post("/api/jobs/movers", json={"dry_run": True}, headers={**auth(), **ORIGIN})
        client.app.state.runner.wait_idle()
        assert runs[0].radarr.apikey == "radarr-key"

    def test_empty_key_keeps_the_saved_one(self, client):
        connect_radarr(client)
        post(client, "/settings/connections/radarr", {"version": version(client), "action": "save", "url": "http://radarr2:7878", "secret": ""})
        settings = store(client).load_settings()
        assert (settings.radarr.baseurl, settings.radarr.apikey) == ("http://radarr2:7878", "radarr-key")

    def test_key_required_at_first(self, client):
        response = post(client, "/settings/connections/sonarr", {"version": 0, "action": "save", "url": "http://sonarr:8989"})
        assert response.status_code == 400
        assert "API key is required" in response.text

    def test_bad_url(self, client):
        response = post(client, "/settings/connections/plex", {"version": 0, "action": "save", "url": "plex:32400", "secret": "t"})
        assert "URL must start with http" in response.text

    def test_test_uses_the_saved_key(self, client, monkeypatch):
        connect_radarr(client)
        seen = []

        def check(service, values):
            seen.append((service, values))
            return "Radarr 6.4.4"

        monkeypatch.setattr("web.settings_routes.check_connection", check)
        response = post(client, "/settings/connections/radarr", {"version": version(client), "action": "test", "url": "http://radarr:7878"})
        assert "Connected to Radarr 6.4.4" in response.text
        assert seen == [("radarr", {"url": "http://radarr:7878", "public_url": "", "username": "", "secret": "radarr-key"})]
        assert "radarr-key" not in response.text

    def test_failed_test_is_shown_without_the_key(self, client, monkeypatch):
        def check(service, values):
            raise LookupFailed("ConnectionError: http://radarr:7878 refused")

        monkeypatch.setattr("web.settings_routes.check_connection", check)
        response = post(client, "/settings/connections/radarr", {"version": 0, "action": "test", "url": "http://radarr:7878", "secret": "new-key"})
        assert "Test failed: ConnectionError" in response.text
        assert version(client) == 0

    def test_disconnect_forgets_url_and_key_keeps_rules(self, client):
        connect_radarr(client)
        post(client, "/settings/radarr/movers/new", {"version": version(client), "action": "save", "path": "/movies/kids", "genres": "Family"})
        post(client, "/settings/connections/radarr", {"version": version(client), "action": "disconnect"})
        assert document(client)["radarr"] == {"movers": [{"path": "/movies/kids", "genres": ["Family"]}]}
        assert store(client).secret_info() == {}

    def test_public_url(self, client):
        fields = {"version": 0, "action": "save", "url": "http://sab:8080", "public_url": "https://sab.example.com/", "secret": "k"}
        post(client, "/settings/connections/sabnzbd", fields)
        assert store(client).load_settings().sabnzbd.public_url == "https://sab.example.com"
        response = post(client, "/settings/connections/sabnzbd", {**fields, "version": version(client), "public_url": "sab.example.com"})
        assert "Public URL must start with http" in response.text
        post(client, "/settings/connections/sabnzbd", {**fields, "version": version(client), "public_url": "", "secret": ""})
        assert store(client).load_settings().sabnzbd.public_url is None

    def test_disconnect_forgets_the_public_url(self, client):
        connect_radarr(client)
        post(client, "/settings/connections/radarr", {"version": version(client), "action": "save", "url": "http://radarr:7878", "public_url": "https://radarr.example.com"})
        assert document(client)["radarr"]["public_url"] == "https://radarr.example.com"
        post(client, "/settings/connections/radarr", {"version": version(client), "action": "disconnect"})
        assert "public_url" not in document(client).get("radarr", {})

    def test_autoscan_without_password(self, client):
        response = post(client, "/settings/connections/autoscan", {"version": 0, "action": "save", "url": "http://autoscan:3030", "username": ""})
        assert response.status_code == 303
        assert store(client).load_settings().autoscan.url == "http://autoscan:3030"


class TestRules:
    def test_add_edit_move_delete_movers(self, client):
        connect_radarr(client)
        page = client.get("/settings/radarr/movers/new", headers=auth()).text
        # choices come from radarr and plex
        assert '<option value="/movies/kids">' in page
        assert '<option value="Kids Movies">' in page
        assert '<option value="Animation">' in page

        response = post(
            client,
            "/settings/radarr/movers/new",
            {"version": version(client), "action": "save", "path": "/movies/kids", "plex_library": "Kids Movies",
             "genres": "Family\nAnimation\nfamily", "genre_match": "all", "tag_match": "any"},
        )
        assert (response.status_code, response.headers["location"]) == (303, "/settings/radarr?saved=1")
        post(client, "/settings/radarr/movers/new", {"version": version(client), "action": "save", "path": "/movies/main", "certifications": "R"})
        assert document(client)["radarr"]["movers"] == [
            {"path": "/movies/kids", "plex_library": "Kids Movies", "genres": ["Family", "Animation"], "genre_match": "all"},
            {"path": "/movies/main", "certifications": ["R"]},
        ]
        page = client.get("/settings/radarr", headers=auth()).text
        assert "genres all of Family, Animation" in page

        post(client, "/settings/radarr/movers/2/move?direction=up", {"version": version(client)})
        assert [m["path"] for m in document(client)["radarr"]["movers"]] == ["/movies/main", "/movies/kids"]
        post(client, "/settings/radarr/movers/1", {"version": version(client), "action": "save", "path": "/movies/main", "certifications": "R\nNC-17"})
        assert document(client)["radarr"]["movers"][0]["certifications"] == ["R", "NC-17"]
        post(client, "/settings/radarr/movers/1/delete", {"version": version(client)})
        assert [m["path"] for m in document(client)["radarr"]["movers"]] == ["/movies/kids"]
        notes = [v["note"] for v in client.get("/api/config/versions", headers=auth()).json()]
        assert notes[:3] == ["Radarr mover 1 deleted", "Radarr mover 1 edited", "Radarr mover 2 moved up"]

    def test_invalid_rule_is_refused(self, client):
        response = post(client, "/settings/radarr/movers/new", {"version": 0, "action": "save", "path": "/movies/kids"})
        assert response.status_code == 400
        assert "requires at least one of" in response.text
        assert version(client) == 0

    def test_unknown_rule(self, client):
        assert client.get("/settings/radarr/movers/1", headers=auth()).status_code == 404
        assert client.get("/settings/radarr/removers/1", headers=auth()).status_code == 404
        assert client.get("/settings/radarr/pruners/new", headers=auth()).status_code == 404
        assert client.get("/settings/lidarr", headers=auth()).status_code == 404

    def test_preview_saves_nothing(self, client):
        connect_radarr(client)
        response = post(
            client,
            "/settings/radarr/movers/new",
            {"version": version(client), "action": "preview", "path": "/movies/kids", "genres": "Family"},
        )
        assert response.status_code == 200
        assert "Matches <strong>2</strong>" in response.text
        assert "Bluey" in response.text and "Frozen" in response.text and "Heat" not in response.text
        assert "radarr" not in document(client) or "movers" not in document(client)["radarr"]

    def test_preview_counts_earlier_movers_and_filter_time(self, client):
        connect_radarr(client)
        post(client, "/settings/radarr/movers/new", {"version": version(client), "action": "save", "path": "/movies/kids", "genres": "Animation"})
        store(client).save({**document(client), "settings": {"dry_run": True, "filter_time": "1d"}}, None, "x", version(client))
        response = post(client, "/settings/radarr/movers/new", {"version": version(client), "action": "preview", "path": "/movies/kids", "genres": "Family"})
        # Bluey is the first mover's
        assert "Matches <strong>1</strong>" in response.text
        assert "1 more that an earlier mover matches first" in response.text
        assert "added within 1d: 0 of these" in response.text

    def test_preview_profiler(self, client):
        connect_radarr(client)
        response = post(client, "/settings/radarr/profilers/new", {"version": version(client), "action": "preview", "profile": "NoDownload", "titles": "Heat"})
        assert "Would change <strong>1</strong>" in response.text

    def test_choices_failing_still_allows_typing(self, client):
        page = client.get("/settings/radarr/movers/new", headers=auth()).text
        assert "Radarr isn&#39;t connected" in page
        assert '<input id="path" name="path"' in page

    def test_profiler(self, client):
        post(client, "/settings/sonarr/profilers/new", {"version": 0, "action": "save", "profile": "NoDownload", "networks": "HBO", "monitored": "false"})
        assert document(client)["sonarr"]["profilers"] == [{"profile": "NoDownload", "networks": ["HBO"], "monitored": False}]

    def test_arr_options(self, client):
        post(client, "/settings/sonarr", {"version": 0, "dry_run": "true", "tag": "helperr"})
        assert document(client)["sonarr"] == {"dry_run": True, "tag": "helperr"}
        response = post(client, "/settings/sonarr", {"version": version(client), "dry_run": "inherit", "tag": ""})
        assert response.status_code == 303
        assert document(client)["sonarr"] == {}


class TestRemovers:
    REMOVER = {"action": "save", "type": "seasons", "keep": "2", "tags": "rm-2", "min_age": "90d"}

    def test_add_and_list(self, client):
        response = post(client, "/settings/sonarr/removers/new", {**self.REMOVER, "version": 0})
        assert response.headers["location"] == "/removers?saved=1"
        assert document(client)["sonarr"]["removers"] == [{"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d"}]
        page = client.get("/removers", headers=auth()).text
        assert "rm-2" in page and "/settings/sonarr/removers/1" in page

    @pytest.mark.parametrize(
        "fields, error",
        [({"min_age": "90"}, "invalid duration"), ({"keep": "0"}, "Keep must be at least 1"), ({"tags": ""}, "at least one tag")],
    )
    def test_invalid(self, client, fields, error):
        response = post(client, "/settings/sonarr/removers/new", {**self.REMOVER, "version": 0, **fields})
        assert response.status_code == 400
        assert error in response.text

    def test_shared_tag_refused(self, client):
        post(client, "/settings/sonarr/removers/new", {**self.REMOVER, "version": 0})
        response = post(client, "/settings/sonarr/removers/new", {**self.REMOVER, "version": version(client), "type": "episodes"})
        assert "already used by remover #1" in response.text

    def test_editing_an_auto_approved_rule_turns_it_off(self, client):
        post(client, "/settings/sonarr/removers/new", {**self.REMOVER, "version": 0})
        with Session(client.app.state.engine) as session:
            session.add(RemoverRuleSetting(rule_key="rm-2", auto_approve=True, updated_at=utc_now()))
            session.commit()
        post(client, "/settings/sonarr/removers/1", {**self.REMOVER, "version": version(client), "keep": "1"})
        with Session(client.app.state.engine) as session:
            assert session.get_one(RemoverRuleSetting, "rm-2").auto_approve is False

    def test_max_deletes(self, client):
        post(client, "/settings/sonarr/remover-settings", {"version": 0, "max_deletes": "50"})
        assert document(client)["sonarr"]["remover_settings"] == {"max_deletes": 50}
        post(client, "/settings/sonarr/remover-settings", {"version": version(client), "max_deletes": ""})
        assert document(client)["sonarr"] == {}


class TestMovieRemovers:
    REMOVER = {"path": "/movies/kids", "min_age": "730d", "watched": "always", "plex_library": "Kids Movies"}

    def test_form_offers_root_folders(self, client):
        connect_radarr(client)
        page = client.get("/settings/radarr/removers/new", headers=auth()).text
        assert 'value="/movies/kids"' in page and "Keep if anyone ever played it" in page

    def test_create(self, client):
        response = post(client, "/settings/radarr/removers/new", {**self.REMOVER, "version": 0})
        assert response.status_code == 303 and response.headers["location"] == "/removers?saved=1"
        assert document(client)["radarr"]["removers"] == [
            {"path": "/movies/kids", "min_age": "730d", "plex_library": "Kids Movies"}
        ]
        page = client.get("/removers", headers=auth()).text
        assert "/movies/kids" in page and "/settings/radarr/removers/1" in page and "removed too" in page

    @pytest.mark.parametrize(
        "watched, within, saved",
        [("ever", "", {"skip_if_ever_watched": True}), ("within", "180d", {"skip_if_watched_within": "180d"}),
         # a window typed in but not chosen isn't saved
         ("always", "180d", {})],
    )
    def test_watched_choice(self, client, watched, within, saved):
        fields = {**self.REMOVER, "version": 0, "watched": watched, "skip_if_watched_within": within}
        assert post(client, "/settings/radarr/removers/new", fields).status_code == 303
        rule = document(client)["radarr"]["removers"][0]
        assert {k: v for k, v in rule.items() if k.startswith("skip_")} == saved
        # the edit form shows the choice again
        page = client.get("/settings/radarr/removers/1", headers=auth()).text
        assert f'<option value="{watched}" selected' in page

    @pytest.mark.parametrize(
        "fields, error",
        [({"path": ""}, "Root folder is required"), ({"min_age": "2y"}, "invalid duration"),
         ({"watched": "within"}, "Played within is required"), ({"watched": "within", "skip_if_watched_within": "x"}, "invalid duration")],
    )
    def test_invalid(self, client, fields, error):
        response = post(client, "/settings/radarr/removers/new", {**self.REMOVER, "version": 0, **fields})
        assert response.status_code == 400
        assert error in response.text
        # the choice survives the error
        if fields.get("watched") == "within":
            assert '<option value="within" selected' in response.text

    def test_folder_used_twice_refused(self, client):
        post(client, "/settings/radarr/removers/new", {**self.REMOVER, "version": 0})
        response = post(client, "/settings/radarr/removers/new", {**self.REMOVER, "version": version(client), "path": "/movies/kids/"})
        assert "already used by remover #1" in response.text

    def test_auto_approve_and_editing_turns_it_off(self, client):
        post(client, "/settings/radarr/removers/new", {**self.REMOVER, "version": 0})
        key = "radarr:/movies/kids"
        page = client.get("/removers/auto-approve?" + urlencode({"rule": key}), headers=auth()).text
        assert "delete movies and their files" in page and "watched or not" in page
        response = client.post("/removers/auto-approve?" + urlencode({"rule": key, "enabled": "true"}), headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303
        assert {"rule": key, "auto_approve": True, "service": "RADARR", "path": "/movies/kids"} in client.get("/api/removers", headers=auth()).json()
        post(client, "/settings/radarr/removers/1", {**self.REMOVER, "version": version(client), "min_age": "365d"})
        with Session(client.app.state.engine) as session:
            assert session.get_one(RemoverRuleSetting, key).auto_approve is False

    def test_max_deletes(self, client):
        post(client, "/settings/radarr/remover-settings", {"version": 0, "max_deletes": "5"})
        assert document(client)["radarr"]["remover_settings"] == {"max_deletes": 5}
        assert 'id="radarr_max_deletes" name="max_deletes" type="number" min="1" value="5"' in client.get("/removers", headers=auth()).text


class TestTrash:
    def test_libraries_from_plex(self, client):
        page = client.get("/settings/trash", headers=auth()).text
        # needs a plex connection, so typed in
        assert "Plex" in page
        post(client, "/settings/connections/plex", {"version": 0, "action": "save", "url": "http://plex:32400", "secret": "plex-token"})
        page = client.get("/settings/trash", headers=auth()).text
        assert 'value="Kids Movies"' in page and 'value="Music"' not in page

    def test_save(self, client):
        response = post(
            client,
            "/settings/trash",
            [("version", 0), ("libraries", "Movies"), ("libraries", "Kids Movies"), ("max_items", "500"),
             ("max_deleted_items", "0"), ("dry_run", "true")],
        )
        assert response.status_code == 303
        assert document(client)["plex"]["empty_trash"] == {
            "libraries": ["Movies", "Kids Movies"], "max_items": 500, "max_deleted_items": 0, "dry_run": True,
        }

    def test_going_live_needs_confirming(self, client):
        response = post(client, "/settings/trash", {"version": 0, "max_items": "100", "max_deleted_items": "50", "dry_run": "false"})
        assert "Save and go live" in response.text
        assert version(client) == 0


class TestHistory:
    def test_diff_and_restore(self, client):
        post(client, "/settings/sonarr", {"version": 0, "dry_run": "true", "tag": "one"})
        first = version(client)
        post(client, "/settings/sonarr", {"version": first, "dry_run": "true", "tag": "two"})
        page = client.get(f"/settings/history/{version(client)}", headers=auth()).text
        assert '<span class="diff-del">-    &#34;tag&#34;: &#34;one&#34;</span>' in page
        assert '<span class="diff-add">+    &#34;tag&#34;: &#34;two&#34;</span>' in page
        assert "History" in client.get("/settings/history", headers=auth()).text

        response = post(client, f"/settings/history/{first}/restore", {"version": version(client)})
        assert response.status_code == 303
        assert document(client)["sonarr"]["tag"] == "one"
        assert client.get("/api/config/versions", headers=auth()).json()[0]["note"] == f"Restored version {first}"

    def test_restoring_a_live_version_needs_confirming(self, client):
        store(client).save({"settings": {"dry_run": False}}, None, "live", 0)
        live = version(client)
        store(client).save({"settings": {"dry_run": True}}, None, "dry", live)
        response = post(client, f"/settings/history/{live}/restore", {"version": version(client)})
        assert "Save and go live" in response.text

    def test_missing_version(self, client):
        assert client.get("/settings/history/9", headers=auth()).status_code == 404


class TestConfigApi:
    def test_put_and_get(self, client):
        body = {"version": 0, "config": {"settings": {"dry_run": True}, "radarr": {"movers": [{"path": "/m", "genres": ["a"], "genre_match": "any"}]}}}
        response = client.put("/api/config", json=body, headers={**auth(), **ORIGIN})
        assert response.status_code == 200
        data = client.get("/api/config", headers=auth()).json()
        assert data["version"] == response.json()["version"]
        assert data["config"]["radarr"]["movers"] == [{"path": "/m", "genres": ["a"]}]

    @pytest.mark.parametrize(
        "body, status, error",
        [
            ({"version": 5, "config": {}}, 409, "changed since"),
            ({"version": 0, "config": {"settings": {"filter_time": "x"}}}, 422, "filter_time"),
            ({"version": 0, "config": {"radarr": {"apikey": "k"}}}, 422, "secrets can only be set"),
            ({"version": 0, "config": {"settings": {"dry_run": False}, "radarr": {}}}, 422, "confirm_live"),
        ],
        ids=["conflict", "invalid", "secret", "live"],
    )
    def test_refused(self, client, body, status, error):
        response = client.put("/api/config", json=body, headers={**auth(), **ORIGIN})
        assert response.status_code == status
        assert error in response.json()["error"]

    def test_live_with_confirm(self, client):
        body = {"version": 0, "config": {"settings": {"dry_run": False}}, "confirm_live": True}
        assert client.put("/api/config", json=body, headers={**auth(), **ORIGIN}).status_code == 200

    def test_versions_and_restore(self, client):
        store(client).save({"settings": {"dry_run": True, "filter_time": "1d"}}, "me", "a", 0)
        first = version(client)
        store(client).save({"settings": {"dry_run": True}}, "me", "b", first)
        [latest, older] = client.get("/api/config/versions", headers=auth()).json()
        assert (older["id"], older["note"], older["created_by"]) == (first, "a", "me")
        assert client.get(f"/api/config/versions/{first}", headers=auth()).json()["config"]["settings"]["filter_time"] == "1d"
        response = client.post(f"/api/config/versions/{first}/restore", json={"version": latest["id"]}, headers={**auth(), **ORIGIN})
        assert response.status_code == 200
        assert document(client)["settings"]["filter_time"] == "1d"

    def test_needs_access(self, client):
        assert client.get("/api/config").status_code == 403
        assert client.get("/settings/connections").status_code == 403


def test_every_settings_page_renders(client):
    connect_radarr(client)
    post(client, "/settings/radarr/movers/new", {"version": version(client), "action": "save", "path": "/movies/kids", "genres": "Family"})
    for url in ("/settings", "/settings/connections", "/settings/radarr", "/settings/sonarr", "/settings/trash",
                "/settings/webhook", "/settings/history", f"/settings/history/{version(client)}",
                "/settings/radarr/movers/1", "/settings/radarr/profilers/new", "/settings/sonarr/removers/new", "/removers"):
        response = client.get(url, headers=auth())
        assert response.status_code == 200, url
        assert "Traceback" not in response.text

