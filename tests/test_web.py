"""Tests for the web app: access auth, security headers, database"""

import dataclasses
import re
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from config.models import ArrConfig, ConfigSettings
from results import Change, JobResult, PlannedFile, PlannedRemoval
from web.app import create_app
from web.db import TrashEvent, make_engine, migrate
from web.settings import WebSettings, load_web_settings

TEAM = "rattsnest.cloudflareaccess.com"
AUD = "a" * 64
EMAIL = "me@example.com"
SECRET_KEY = "s" * 40

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeKeys:
    """stands in for the team's published keys at /cdn-cgi/access/certs"""

    def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
        return SimpleNamespace(key=SIGNING_KEY.public_key())


def make_token(key=SIGNING_KEY, algorithm="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": [AUD],
        "email": EMAIL,
        "iss": f"https://{TEAM}",
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        "type": "app",
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=algorithm)


@pytest.fixture
def settings(tmp_path) -> WebSettings:
    return WebSettings(
        team_domain=TEAM,
        audience=AUD,
        allowed_emails=frozenset({EMAIL}),
        database_path=tmp_path / "helperr.db",
        secret_key=SECRET_KEY,
    )


def live_config() -> ConfigSettings:
    arr = ArrConfig(baseurl="http://arr", apikey="key", tag=None, dry_run=False)
    return ConfigSettings(plex=None, radarr=arr, sonarr=None)


def fake_run_job(job: str, settings: ConfigSettings) -> JobResult:
    return JobResult(job, changes=[Change("RADARR", "move", "/movies/kids", ["Bluey"], "done")])


@pytest.fixture
def client(settings):
    app = create_app(
        settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False
    )
    with TestClient(app) as c:
        yield c


def wait(client) -> None:
    client.app.state.runner.wait_idle()


def auth(token: str | None = None) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": token or make_token()}


# ---------------------------------------------------------------------------
# access auth
# ---------------------------------------------------------------------------


class TestAccessAuth:
    def test_valid_token_is_let_in(self, client):
        response = client.get("/", headers=auth())
        assert response.status_code == 200
        assert EMAIL in response.text

    def test_no_token_is_denied(self, client):
        assert client.get("/").status_code == 403

    @pytest.mark.parametrize(
        "token",
        [
            make_token(key=OTHER_KEY),
            make_token(aud=["b" * 64]),
            make_token(iss="https://other.cloudflareaccess.com"),
            make_token(exp=int(time.time()) - 3600),
            make_token(exp=None),
            make_token(email="stranger@example.com"),
            make_token(email=None),
            "not-a-jwt",
        ],
        ids=[
            "wrong-key",
            "wrong-audience",
            "wrong-issuer",
            "expired",
            "no-expiry",
            "email-not-allowed",
            "no-email",
            "garbage",
        ],
    )
    def test_invalid_token_is_denied(self, client, token):
        response = client.get("/", headers=auth(token))
        assert response.status_code == 403
        assert response.text == "Forbidden"

    def test_only_rs256_is_accepted(self, client):
        # otherwise anyone could sign their own token with a shared-secret algorithm
        token = jwt.encode(
            {"aud": [AUD], "email": EMAIL, "iss": f"https://{TEAM}", "exp": 2**31},
            "an-attacker-chosen-secret-of-32-bytes!",
            algorithm="HS256",
        )
        assert client.get("/", headers=auth(token)).status_code == 403

    def test_email_match_ignores_case(self, client):
        response = client.get("/", headers=auth(make_token(email="Me@Example.com")))
        assert response.status_code == 200

    def test_static_files_need_a_token(self, client):
        assert client.get("/static/app.css").status_code == 403
        assert client.get("/static/app.css", headers=auth()).status_code == 200

    def test_unknown_paths_need_a_token(self, client):
        assert client.get("/nope").status_code == 403
        assert client.get("/nope", headers=auth()).status_code == 404

    def test_healthz_is_public(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_api_docs_are_disabled(self, client):
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path, headers=auth()).status_code == 404


# ---------------------------------------------------------------------------
# security headers / cross-site requests
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_headers_on_pages_and_denials(self, client):
        for response in (client.get("/", headers=auth()), client.get("/")):
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "server" not in response.headers

    def test_cross_site_post_is_rejected(self, client):
        headers = {**auth(), "Origin": "https://evil.example"}
        response = client.post("/", headers=headers)
        assert response.status_code == 403
        assert "Cross-site" in response.text

    def test_post_without_origin_is_rejected(self, client):
        assert client.post("/", headers=auth()).status_code == 403

    def test_same_origin_post_passes_the_check(self, client):
        headers = {**auth(), "Origin": "http://testserver"}
        # no POST routes yet, so it reaches routing and gets 405
        assert client.post("/", headers=headers).status_code == 405

    def test_cross_site_check_runs_before_auth(self, client):
        response = client.post("/", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        assert "Cross-site" in response.text


# ---------------------------------------------------------------------------
# settings / database
# ---------------------------------------------------------------------------


class TestSettings:
    def test_loads_and_normalizes(self, tmp_path):
        settings = load_web_settings(
            {
                "CF_ACCESS_TEAM_DOMAIN": f"https://{TEAM.upper()}/",
                "CF_ACCESS_AUD": AUD.upper(),
                "ALLOWED_EMAILS": " Me@Example.com , other@example.com ",
                "DATA_DIR": str(tmp_path),
                "HELPERR_SECRET_KEY": f" {SECRET_KEY} ",
            }
        )
        assert settings.team_domain == TEAM
        assert settings.audience == AUD
        assert settings.allowed_emails == {EMAIL, "other@example.com"}
        assert settings.database_path == tmp_path / "helperr.db"
        assert settings.secret_key == SECRET_KEY
        assert SECRET_KEY not in repr(settings)

    def test_every_problem_is_reported(self):
        with pytest.raises(ValueError) as e:
            load_web_settings({"CF_ACCESS_TEAM_DOMAIN": "example.com", "REMOVERS_ALLOW_APPLY": "yes"})
        for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "ALLOWED_EMAILS", "REMOVERS_ALLOW_APPLY", "HELPERR_SECRET_KEY"):
            assert name in str(e.value)

    @pytest.mark.parametrize("value, allowed", [(None, False), ("false", False), (" True ", True)])
    def test_remover_apply_is_off_unless_set(self, value, allowed):
        env = {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD, "ALLOWED_EMAILS": EMAIL, "HELPERR_SECRET_KEY": SECRET_KEY}
        if value is not None:
            env["REMOVERS_ALLOW_APPLY"] = value
        assert load_web_settings(env).allow_remover_apply is allowed

    def test_short_secret_key_refused(self):
        env = {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD, "ALLOWED_EMAILS": EMAIL, "HELPERR_SECRET_KEY": "short"}
        with pytest.raises(ValueError, match="HELPERR_SECRET_KEY"):
            load_web_settings(env)


class TestDatabase:
    def test_migrations_create_tables(self, tmp_path):
        engine = make_engine(tmp_path / "helperr.db")
        migrate(engine)
        migrate(engine)  # running again at the next start is a no-op
        assert {"job_runs", "schedules"} <= set(inspect(engine).get_table_names())

    def test_wal_mode(self, tmp_path):
        engine = make_engine(tmp_path / "helperr.db")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"

    def test_temp_files_stay_in_memory(self, tmp_path):
        # 2 = MEMORY: the container has no writable temp dir
        engine = make_engine(tmp_path / "helperr.db")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA temp_store").scalar() == 2


# ---------------------------------------------------------------------------
# jobs pages / api
# ---------------------------------------------------------------------------

ORIGIN = {"Origin": "http://testserver"}


class TestJobRoutes:
    def test_dry_run_button_queues_and_redirects(self, client):
        response = client.post(
            "/jobs/movers?dry_run=true", headers={**auth(), **ORIGIN}, follow_redirects=False
        )
        assert response.status_code == 303
        run_url = response.headers["location"]
        wait(client)
        data = client.get("/api" + run_url, headers=auth()).json()
        assert data["status"] == "succeeded"
        assert data["dry_run"] is True
        assert data["requested_by"] == EMAIL
        page = client.get(run_url, headers=auth()).text
        assert "Bluey" in page and "succeeded" in page

    def test_index_lists_runs_and_config_mode(self, client):
        client.post("/jobs/movers?dry_run=true", headers={**auth(), **ORIGIN})
        wait(client)
        page = client.get("/", headers=auth()).text
        assert 'href="/runs/1"' in page
        assert "live" in page  # live_config has dry_run off

    def test_older_runs_a_page_at_a_time(self, client, monkeypatch):
        monkeypatch.setattr("web.routes.RUNS_PAGE", 2)
        for _ in range(3):
            client.post("/jobs/movers?dry_run=true", headers={**auth(), **ORIGIN})
            wait(client)
        page = client.get("/", headers=auth()).text
        assert 'href="/runs/3"' in page and 'href="/runs/1"' not in page
        assert 'href="/?before=2">Older runs' in page
        page = client.get("/?before=2", headers=auth()).text
        assert 'href="/runs/1"' in page and 'href="/runs/3"' not in page and "Older runs</a>" not in page

    def test_error_pages(self, client):
        html = {**auth(), "Accept": "text/html"}
        page = client.get("/runs/abc", headers=html)
        assert page.status_code == 422 and "That can&#39;t be shown" in page.text and 'href="/status"' in page.text
        page = client.get("/runs/99999999999999999999999", headers=html)
        assert page.status_code == 404 and "Not found" in page.text
        assert client.get("/runs/999", headers=html).status_code == 404
        # the api keeps answering json
        assert client.get("/api/runs/abc", headers=html).json()["detail"]
        assert client.get("/api/runs/99999999999999999999999", headers=auth()).status_code == 404

    def test_confirm_page_warns_when_live(self, client):
        page = client.get("/jobs/movers/confirm", headers=auth()).text
        assert "will make changes" in page
        assert 'action="/jobs/movers?dry_run=false"' in page

    def test_unknown_job_is_404(self, client):
        assert client.post("/jobs/nope", headers={**auth(), **ORIGIN}).status_code == 404
        assert client.get("/jobs/nope/confirm", headers=auth()).status_code == 404
        assert client.post("/api/jobs/nope", json={}, headers={**auth(), **ORIGIN}).status_code == 404

    def test_job_post_needs_same_origin(self, client):
        assert client.post("/jobs/movers?dry_run=true", headers=auth()).status_code == 403

    def test_api_defaults_to_dry_run(self, client):
        response = client.post("/api/jobs/empty_trash", json={}, headers={**auth(), **ORIGIN})
        assert response.status_code == 202
        wait(client)
        run = client.get(f"/api/runs/{response.json()['id']}", headers=auth()).json()
        assert run["dry_run"] is True
        assert run["trigger"] == "api"

    def test_api_conflict_when_already_active(self, settings):
        release = threading.Event()

        def slow_job(job, s):
            release.wait(5)
            return JobResult(job)

        app = create_app(settings, keys=FakeKeys(), run_job=slow_job, load_config=live_config)
        with TestClient(app) as client:
            headers = {**auth(), **ORIGIN}
            first = client.post("/api/jobs/movers", json={}, headers=headers).json()["id"]
            second = client.post("/api/jobs/movers", json={}, headers=headers)
            assert second.status_code == 409
            assert second.json()["id"] == first
            # the page button goes to the active run, which says this one wasn't queued
            page = client.post("/jobs/movers?dry_run=true", headers=headers, follow_redirects=False)
            assert page.headers["location"] == f"/runs/{first}?busy=1"
            assert "so yours wasn't queued" in client.get(page.headers["location"], headers=headers).text
            release.set()
            wait(client)

    def test_missing_run_is_404(self, client):
        assert client.get("/runs/999", headers=auth()).status_code == 404
        assert client.get("/api/runs/999", headers=auth()).status_code == 404

    def test_result_text_is_escaped(self, settings):
        def xss_job(job, s):
            return JobResult(job, changes=[Change("SONARR", "remove", "<script>x</script>", [], "dry_run")])

        app = create_app(settings, keys=FakeKeys(), run_job=xss_job, load_config=live_config)
        with TestClient(app) as client:
            client.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
            wait(client)
            page = client.get("/runs/1", headers=auth()).text
        assert "<script>x</script>" not in page
        assert "&lt;script&gt;x&lt;/script&gt;" in page

    def test_broken_config_shown_on_index(self, settings):
        def broken():
            raise ValueError("bad toml")

        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=broken)
        with TestClient(app) as client:
            page = client.get("/", headers=auth()).text
        assert "bad toml" in page


# ---------------------------------------------------------------------------
# schedules pages / api
# ---------------------------------------------------------------------------

FORM = {**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}


def post_form(client, url: str, body: str):
    return client.post(url, content=body, headers={**auth(), **FORM}, follow_redirects=False)


class TestScheduleRoutes:
    def test_create_from_form(self, client):
        response = post_form(
            client, "/schedules", "name=Hourly+movers&job=movers&cron=47+*+*+*+*&timezone=America%2FNew_York&enabled=on"
        )
        assert response.status_code == 303
        [schedule] = client.get("/api/schedules", headers=auth()).json()
        assert schedule["name"] == "Hourly movers"
        assert schedule["cron"] == "47 * * * *"
        assert schedule["timezone"] == "America/New_York"
        # unchecked box: config.toml's settings
        assert schedule["dry_run"] is False
        assert schedule["enabled"] is True
        assert schedule["next_run_at"]
        page = client.get("/schedules", headers=auth()).text
        assert "Hourly movers" in page and "47 * * * *" in page
        # the job card shows when it runs next
        assert "Next:" in client.get("/", headers=auth()).text

    def test_invalid_form_shows_errors_and_keeps_values(self, client):
        response = post_form(client, "/schedules", "name=Nightly&job=removers&timezone=UTC&cron=99+*+*+*+*&dry_run=on")
        assert response.status_code == 400
        assert "Schedule: minute" in response.text
        assert 'value="Nightly"' in response.text
        assert client.get("/api/schedules", headers=auth()).json() == []

    def test_new_form_defaults_to_dry_run(self, client):
        page = client.get("/schedules/new", headers=auth()).text
        assert 'name="dry_run" checked' in page
        # static/app.js swaps in the browser's zone
        assert "data-browser-zone" in page
        assert "<option selected>UTC</option>" in page
        # shows what an unchecked box would mean
        assert "live" in page

    def test_edit_toggle_and_delete(self, client):
        post_form(client, "/schedules", "name=a&job=movers&timezone=UTC&cron=%40hourly&enabled=on&dry_run=on")
        assert "@hourly" in client.get("/schedules/1", headers=auth()).text
        post_form(client, "/schedules/1", "name=b&job=empty_trash&timezone=UTC&cron=0+4+*+*+*&enabled=on&dry_run=on")
        schedule = client.get("/api/schedules/1", headers=auth()).json()
        assert (schedule["name"], schedule["job"], schedule["cron"]) == ("b", "empty_trash", "0 4 * * *")
        post_form(client, "/schedules/1/toggle", "")
        assert client.get("/api/schedules/1", headers=auth()).json()["enabled"] is False
        assert "paused" in client.get("/schedules", headers=auth()).text
        assert post_form(client, "/schedules/1/delete", "").status_code == 303
        assert client.get("/schedules/1", headers=auth()).status_code == 404

    def test_scheduled_run_page_links_the_schedule(self, client):
        post_form(client, "/schedules", "name=Nightly&job=movers&timezone=UTC&cron=0+4+*+*+*&enabled=on&dry_run=on")
        client.app.state.runner.submit("movers", True, "schedule", None, 1)
        wait(client)
        assert 'href="/schedules/1">Nightly' in client.get("/runs/1", headers=auth()).text
        assert "schedule: Nightly" in client.get("/", headers=auth()).text
        assert "#1" in client.get("/schedules", headers=auth()).text
        # deleting the schedule keeps its runs
        post_form(client, "/schedules/1/delete", "")
        assert client.get("/api/runs/1", headers=auth()).json()["schedule_id"] is None

    def test_unknown_time_zone_is_rejected(self, client):
        response = post_form(client, "/schedules", "name=a&job=movers&cron=%40daily&timezone=Mars%2FOlympus")
        assert response.status_code == 400
        assert "unknown time zone" in response.text

    def test_times_are_utc_for_the_browser_to_convert(self, client):
        post_form(client, "/schedules", "name=a&job=movers&cron=%40daily&timezone=UTC&enabled=on")
        page = client.get("/schedules", headers=auth()).text
        assert re.search(r'<time datetime="\d{4}-\d\d-\d\dT00:00:00Z">[^<]+ 00:00:00 UTC</time>', page)
        assert '<script src="/static/app.js" defer>' in page

    def test_form_posts_need_same_origin(self, client):
        response = client.post("/schedules", content="name=x", headers={**auth(), "Content-Type": FORM["Content-Type"]})
        assert response.status_code == 403

    def test_oversized_form_is_rejected(self, client):
        assert post_form(client, "/schedules", "name=" + "x" * 20_000).status_code == 413

    def test_missing_schedule_is_404(self, client):
        assert client.get("/schedules/9", headers=auth()).status_code == 404
        assert post_form(client, "/schedules/9", "name=x").status_code == 404
        assert post_form(client, "/schedules/9/toggle", "").status_code == 404
        assert client.get("/api/schedules/9", headers=auth()).status_code == 404
        assert client.delete("/api/schedules/9", headers={**auth(), **ORIGIN}).status_code == 404

    def test_names_are_escaped(self, client):
        post_form(client, "/schedules", "name=%3Cscript%3Ex%3C%2Fscript%3E&job=movers&timezone=UTC&cron=%40daily")
        page = client.get("/schedules", headers=auth()).text
        assert "<script>x</script>" not in page
        assert "&lt;script&gt;" in page


class TestScheduleApi:
    def test_create_update_delete(self, client):
        headers = {**auth(), **ORIGIN}
        created = client.post(
            "/api/schedules", json={"name": " hourly ", "job": "movers", "cron": "47 * * * *"}, headers=headers
        )
        assert created.status_code == 201
        schedule = created.json()
        # dry and enabled unless asked otherwise
        assert schedule["name"] == "hourly"
        assert schedule["dry_run"] is True and schedule["enabled"] is True
        assert schedule["timezone"] == "UTC"
        updated = client.put(
            f"/api/schedules/{schedule['id']}",
            json={"name": "hourly", "job": "movers", "cron": "15 * * * *", "dry_run": False},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["cron"] == "15 * * * *"
        assert updated.json()["dry_run"] is False
        assert client.delete(f"/api/schedules/{schedule['id']}", headers=headers).status_code == 204
        assert client.get("/api/schedules", headers=auth()).json() == []

    def test_invalid_schedule_is_422_with_errors(self, client):
        response = client.post(
            "/api/schedules", json={"name": "", "job": "movers", "cron": "* *"}, headers={**auth(), **ORIGIN}
        )
        assert response.status_code == 422
        assert len(response.json()["errors"]) == 2


# ---------------------------------------------------------------------------
# reviewing and applying a removers plan
# ---------------------------------------------------------------------------


def planning_job(job: str, settings: ConfigSettings, approved=None) -> JobResult:
    """plans two series, or removes the approved ones"""
    removals = [
        PlannedRemoval(1, "Survivor", "seasons, keep 2 (rm)", ["S01"], [PlannedFile(11, "/tv/Survivor/S01E01.mkv")], "dry_run", "1 files"),
        PlannedRemoval(2, "Big Brother", "seasons, keep 2 (rm)", ["S01", "S02"], [PlannedFile(21, "/tv/BB/a.mkv"), PlannedFile(22, "/tv/BB/b.mkv")], "dry_run", "2 files"),
    ]
    if approved is not None:
        removals = [dataclasses.replace(r, status="done") for r in removals if r.media_id in approved.get("SONARR", {})]
    changes = [Change("SONARR", "remove", r.title, r.labels, r.status, r.detail) for r in removals]
    return JobResult(job, changes=changes, removals=removals)


@pytest.fixture
def plan_client(settings):
    app = create_app(
        dataclasses.replace(settings, allow_remover_apply=True),
        keys=FakeKeys(),
        run_job=planning_job,
        load_config=live_config,
        run_scheduler=False,
    )
    with TestClient(app) as c:
        c.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
        wait(c)
        yield c


class TestPlanReview:
    def test_plan_page_lists_series_to_select(self, plan_client):
        page = plan_client.get("/runs/1", headers=auth()).text
        assert 'action="/runs/1/apply"' in page
        assert 'name="item" value="sonarr:1"' in page and 'name="item" value="sonarr:2"' in page
        assert "/tv/BB/b.mkv" in page
        # removals aren't listed again as changes
        assert "Other changes" not in page and "Nothing to change" not in page

    def test_off_without_the_env_var(self, client):
        client.app.state.runner.run_job = planning_job
        client.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
        wait(client)
        page = client.get("/runs/1", headers=auth()).text
        assert "REMOVERS_ALLOW_APPLY=true" in page
        assert 'name="item"' not in page
        response = client.post("/api/runs/1/apply", json={"series_ids": [1]}, headers={**auth(), **ORIGIN})
        assert response.status_code == 422

    def test_confirm_page_totals_the_selection(self, plan_client):
        page = plan_client.get("/runs/1/apply?item=sonarr:2", headers=auth()).text
        assert "deletes up to 2 episode files" in page
        assert "Big Brother" in page and "Survivor" not in page
        assert 'action="/runs/1/apply?item=sonarr%3A2"' in page

    def test_confirm_page_needs_a_selection(self, plan_client):
        page = plan_client.get("/runs/1/apply", headers=auth()).text
        assert "Select at least one series" in page
        assert "Delete" not in page

    def test_confirm_page_warns_over_max_deletes(self, settings):
        def config_with_max():
            from config.models import RemoverSettings

            arr = ArrConfig(baseurl="http://arr", apikey="key", tag=None, dry_run=False, remover_settings=RemoverSettings(max_deletes=2))
            return ConfigSettings(plex=None, radarr=None, sonarr=arr)

        app = create_app(
            dataclasses.replace(settings, allow_remover_apply=True),
            keys=FakeKeys(), run_job=planning_job, load_config=config_with_max, run_scheduler=False,
        )
        with TestClient(app) as c:
            c.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
            wait(c)
            page = c.get("/runs/1/apply?item=sonarr:1&item=sonarr:2", headers=auth()).text
        assert "3 Sonarr files is more than its max_deletes (2)" in page
        assert "Delete 3 files" not in page

    def test_apply_runs_the_approved_series(self, plan_client):
        response = plan_client.post(
            "/runs/1/apply?item=sonarr:2", headers={**auth(), **ORIGIN}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/runs/2"
        wait(plan_client)
        run = plan_client.get("/api/runs/2", headers=auth()).json()
        assert run["plan_run_id"] == 1
        assert run["approved"] == [{"service": "SONARR", "media_id": 2, "title": "Big Brother", "file_ids": [21, 22]}]
        assert run["requested_by"] == EMAIL
        assert [r["title"] for r in run["result"]["removals"]] == ["Big Brother"]
        page = plan_client.get("/runs/2", headers=auth()).text
        assert 'href="/runs/1">plan #1' in page
        # an apply run isn't a plan to approve
        assert 'name="item"' not in page
        # the plan says it was applied and can't be applied again
        plan_page = plan_client.get("/runs/1", headers=auth()).text
        assert 'Applied by run <a href="/runs/2">#2</a>' in plan_page
        assert 'name="item"' not in plan_page
        again = plan_client.post("/runs/1/apply?item=sonarr:1", headers={**auth(), **ORIGIN})
        assert again.status_code == 400
        assert "already applied by run 2" in again.text

    def test_apply_needs_same_origin(self, plan_client):
        assert plan_client.post("/runs/1/apply?item=sonarr:1", headers=auth()).status_code == 403

    def test_api_apply(self, plan_client):
        headers = {**auth(), **ORIGIN}
        response = plan_client.post("/api/runs/1/apply", json={"series_ids": [1, 2]}, headers=headers)
        assert response.status_code == 202
        wait(plan_client)
        run = plan_client.get(f"/api/runs/{response.json()['id']}", headers=auth()).json()
        assert run["status"] == "succeeded"
        assert len(run["approved"]) == 2
        assert plan_client.post("/api/runs/99/apply", json={"series_ids": [1]}, headers=headers).status_code == 404
        refused = plan_client.post("/api/runs/1/apply", json={"series_ids": [1]}, headers=headers)
        assert refused.status_code == 422
        assert "already applied" in refused.json()["error"]

    def test_movies_in_a_plan(self, settings):
        def movie_plan(job, s, approved=None, movie_plays=None):
            movie = PlannedRemoval(
                7, "Old (2024)", "older than 730d in /movies/hallmark", ["released 2024-01-01", "added 2024-02-01", "never played"],
                [PlannedFile(70, "/movies/hallmark/Old (2024)/Old.mkv")], "done" if approved else "dry_run", service="RADARR",
            )
            if approved is not None and 7 not in approved.get("RADARR", {}):
                return JobResult(job)
            return JobResult(job, removals=[movie])

        app = create_app(
            dataclasses.replace(settings, allow_remover_apply=True),
            keys=FakeKeys(), run_job=movie_plan, load_config=live_config, run_scheduler=False,
        )
        with TestClient(app) as c:
            c.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
            wait(c)
            page = c.get("/runs/1", headers=auth()).text
            assert 'name="item" value="radarr:7"' in page and "never played" in page
            confirm = c.get("/runs/1/apply?item=radarr:7", headers=auth()).text
            assert "deletes 1 movie and its file" in confirm and "episode files" not in confirm
            c.post("/api/runs/1/apply", json={"items": ["radarr:7"]}, headers={**auth(), **ORIGIN})
            wait(c)
            run = c.get("/api/runs/2", headers=auth()).json()
        assert run["approved"] == [{"service": "RADARR", "media_id": 7, "title": "Old (2024)", "file_ids": [70]}]
        assert run["result"]["removals"][0]["status"] == "done"

    def test_paths_are_escaped(self, settings):
        def xss_plan(job, s, approved=None):
            r = PlannedRemoval(1, "<b>x</b>", "r", ["S01"], [PlannedFile(1, "<script>y</script>")], "dry_run")
            return JobResult(job, removals=[r])

        app = create_app(
            dataclasses.replace(settings, allow_remover_apply=True),
            keys=FakeKeys(), run_job=xss_plan, load_config=live_config, run_scheduler=False,
        )
        with TestClient(app) as c:
            c.post("/jobs/removers?dry_run=true", headers={**auth(), **ORIGIN})
            wait(c)
            pages = [c.get(url, headers=auth()).text for url in ("/runs/1", "/runs/1/apply?item=sonarr:1")]
        for page in pages:
            assert "<script>y</script>" not in page and "<b>x</b>" not in page
            assert "&lt;script&gt;y&lt;/script&gt;" in page


# ---------------------------------------------------------------------------
# removers page / auto-approve
# ---------------------------------------------------------------------------


def remover_config() -> ConfigSettings:
    from datetime import timedelta

    from config.models import RemoverRule, RemoverSettings

    rules = [
        RemoverRule(type="seasons", keep=1, tags=["rm-Housewives"], min_age=timedelta(days=30)),
        RemoverRule(type="episodes", keep=5, tags=["rm-keep-5"], min_age=timedelta(hours=12)),
    ]
    sonarr = ArrConfig(
        baseurl="http://arr", apikey="key", tag=None, removers=rules, remover_settings=RemoverSettings()
    )
    return ConfigSettings(plex=None, radarr=None, sonarr=sonarr)


def removers_app(settings, allow_apply=True):
    return create_app(
        dataclasses.replace(settings, allow_remover_apply=allow_apply),
        keys=FakeKeys(), run_job=fake_run_job, load_config=remover_config, run_scheduler=False,
    )


class TestRemoversPage:
    def test_lists_rules_off_by_default(self, settings):
        with TestClient(removers_app(settings)) as c:
            page = c.get("/removers", headers=auth()).text
            assert "rm-Housewives" in page and "30d" in page and "12h" in page
            assert 'href="/removers/auto-approve?rule=rm-housewives"' in page
            assert [r["auto_approve"] for r in c.get("/api/removers", headers=auth()).json()] == [False, False]

    def test_turn_on_through_the_confirm_page_then_off(self, settings):
        with TestClient(removers_app(settings)) as c:
            confirm = c.get("/removers/auto-approve?rule=rm-housewives", headers=auth()).text
            assert "without review" in confirm
            assert 'action="/removers/auto-approve?rule=rm-housewives&amp;enabled=true"' in confirm
            response = c.post(
                "/removers/auto-approve?rule=rm-housewives&enabled=true",
                headers={**auth(), **ORIGIN},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert c.app.state.runner.auto_approved_rules() == {"rm-housewives"}
            # every place that says what a config run does says so
            assert "auto-approve on for 1" in c.get("/", headers=auth()).text
            assert "will be deleted" in c.get("/jobs/removers/confirm", headers=auth()).text
            assert "auto-approved removers delete" in c.get("/schedules/new", headers=auth()).text
            assert "Turn off" in c.get("/removers", headers=auth()).text
            c.post("/removers/auto-approve?rule=rm-housewives&enabled=false", headers={**auth(), **ORIGIN})
            assert c.app.state.runner.auto_approved_rules() == frozenset()
            assert "nothing is deleted until you approve" in c.get("/jobs/removers/confirm", headers=auth()).text

    def test_unknown_rule_cant_be_turned_on(self, settings):
        with TestClient(removers_app(settings)) as c:
            assert c.get("/removers/auto-approve?rule=nope", headers=auth()).status_code == 404
            headers = {**auth(), **ORIGIN}
            assert c.post("/removers/auto-approve?rule=nope&enabled=true", headers=headers).status_code == 404
            response = c.post("/api/removers/auto-approve", json={"rule": "nope", "enabled": True}, headers=headers)
            assert response.status_code == 404

    def test_api_toggle(self, settings):
        with TestClient(removers_app(settings)) as c:
            headers = {**auth(), **ORIGIN}
            response = c.post("/api/removers/auto-approve", json={"rule": "rm-keep-5", "enabled": True}, headers=headers)
            assert response.json() == {"rule": "rm-keep-5", "auto_approve": True}
            assert [r["auto_approve"] for r in c.get("/api/removers", headers=auth()).json()] == [False, True]

    def test_no_effect_while_apply_is_off(self, settings):
        with TestClient(removers_app(settings, allow_apply=False)) as c:
            c.post("/removers/auto-approve?rule=rm-housewives&enabled=true", headers={**auth(), **ORIGIN})
            assert "REMOVERS_ALLOW_APPLY=true" in c.get("/removers", headers=auth()).text
            assert c.app.state.runner.auto_approved_rules() == frozenset()
            assert "nothing is deleted until you approve" in c.get("/jobs/removers/confirm", headers=auth()).text

    def test_toggle_needs_same_origin(self, settings):
        with TestClient(removers_app(settings)) as c:
            assert c.post("/removers/auto-approve?rule=rm-housewives&enabled=true", headers=auth()).status_code == 403


class TestTrashPage:
    def test_fully_deleted_items_are_listed(self, settings):
        from results import TrashedItem

        def trash_job(job, s):
            return JobResult(
                job,
                changes=[Change("PLEX", "empty_trash", "TV Shows", ["Survivor - S01E01 - Pilot"], "dry_run", "121 old versions after upgrades, 2 fully deleted (1 by removers)")],
                trash=[
                    TrashedItem("TV Shows", "Survivor - S01E01 - Pilot", ["/mnt/nfs/tv/S01E01.mkv"], 4),
                    TrashedItem("TV Shows", "<b>Lost</b>", ["/mnt/nfs/tv/lost.mkv"]),
                ],
            )

        app = create_app(settings, keys=FakeKeys(), run_job=trash_job, load_config=live_config, run_scheduler=False)
        with TestClient(app) as c:
            c.post("/jobs/empty_trash?dry_run=true", headers={**auth(), **ORIGIN})
            wait(c)
            page = c.get("/runs/1", headers=auth()).text
        assert "Fully deleted" in page
        assert 'href="/runs/4">removers #4' in page
        assert "not by removers" in page
        assert "/mnt/nfs/tv/lost.mkv" in page
        assert "&lt;b&gt;Lost&lt;/b&gt;" in page
        assert "121 old versions after upgrades" in page


# ---------------------------------------------------------------------------
# api key / webhooks
# ---------------------------------------------------------------------------


def key_of(client) -> str:
    from web.webhooks import api_key

    return api_key(client.app.state.engine)


def basic(password: str, user: str = "helperr") -> dict[str, str]:
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


MOVIE_ADDED = {"eventType": "MovieAdded", "instanceName": "Radarr", "movie": {"title": "Frankenstein", "year": 2025}}
MOVIE_UPGRADED = {"eventType": "Download", "isUpgrade": True, "movie": {"title": "Frankenstein", "year": 2025}}
EPISODES_UPGRADED = {
    "eventType": "Download",
    "isUpgrade": True,
    "instanceName": "Sonarr 4K",
    "series": {"title": "Survivor", "year": 2000},
    "episodes": [{"seasonNumber": 1, "episodeNumber": 2}, {"seasonNumber": 1, "episodeNumber": 3}],
}


def trash_events(client) -> list[tuple[str, str]]:
    with Session(client.app.state.engine) as session:
        return [(e.event, e.source) for e in session.scalars(select(TrashEvent).order_by(TrashEvent.id))]


class TestWebhooks:
    def test_movie_added_queues_movers_without_access_or_origin(self, client):
        response = client.post("/webhooks/arr", json=MOVIE_ADDED, headers=basic(key_of(client)))
        assert response.status_code == 200
        run_id = response.json()["queued"]
        wait(client)
        run = client.get(f"/api/runs/{run_id}", headers=auth()).json()
        assert (run["job"], run["trigger"], run["dry_run"]) == ("movers", "webhook", False)
        assert run["requested_by"] == "Radarr: Frankenstein (2025)"

    def test_series_add_with_header_key(self, client):
        body = {"eventType": "SeriesAdd", "series": {"title": "Survivor"}}
        response = client.post("/webhooks/arr", json=body, headers={"X-Api-Key": key_of(client)})
        wait(client)
        run = client.get(f"/api/runs/{response.json()['queued']}", headers=auth()).json()
        assert run["requested_by"] == "Sonarr: Survivor"

    @pytest.mark.parametrize(
        "body",
        [{"eventType": "Test"}, {"eventType": "Download"}, {**MOVIE_UPGRADED, "isUpgrade": False}, {"eventType": "Grab"}, {"eventType": ""}],
        ids=["test", "download", "import", "grab", "empty"],
    )
    def test_other_events_are_ignored(self, client, body):
        response = client.post("/webhooks/arr", json=body, headers=basic(key_of(client)))
        assert response.status_code == 200
        assert response.json()["queued"] is None
        assert client.get("/api/runs", headers=auth()).json() == []
        assert trash_events(client) == []

    @pytest.mark.parametrize(
        "body, source",
        [(MOVIE_UPGRADED, "Radarr: Frankenstein (2025)"), (EPISODES_UPGRADED, "Sonarr 4K: Survivor (2000) S01E02,S01E03")],
        ids=["radarr", "sonarr"],
    )
    def test_an_upgrade_is_recorded_for_a_follow_up_empty_trash(self, client, body, source):
        response = client.post("/webhooks/arr", json=body, headers={"X-Api-Key": key_of(client)})
        assert response.json() == {"event": "Download", "queued": None}
        # plex hasn't scanned the upgrade yet, the scheduler empties trash later
        assert client.get("/api/runs", headers=auth()).json() == []
        assert trash_events(client) == [("Download", source)]
        assert "Follow-up:" in client.get("/", headers=auth()).text

    def test_old_upgrades_are_dropped(self, client):
        with Session(client.app.state.engine) as session:
            session.add(TrashEvent(received_at=datetime(2026, 1, 1), event="Download", source="old"))
            session.commit()
        client.post("/webhooks/arr", json=MOVIE_UPGRADED, headers={"X-Api-Key": key_of(client)})
        assert trash_events(client) == [("Download", "Radarr: Frankenstein (2025)")]

    @pytest.mark.parametrize(
        "headers",
        [{}, basic("wrong"), {"X-Api-Key": "wrong"}, {"Authorization": "Basic !!!"}, {"Authorization": "Bearer x"}],
        ids=["none", "wrong-basic", "wrong-header", "garbled", "bearer"],
    )
    def test_needs_the_api_key(self, client, headers):
        response = client.post("/webhooks/arr", json=MOVIE_ADDED, headers=headers)
        assert response.status_code == 401
        assert client.get("/api/runs", headers=auth()).json() == []

    def test_key_in_the_url_isnt_accepted(self, client):
        assert client.post(f"/webhooks/arr?apikey={key_of(client)}", json=MOVIE_ADDED).status_code == 401

    def test_an_access_token_isnt_enough(self, client):
        assert client.post("/webhooks/arr", json=MOVIE_ADDED, headers=auth()).status_code == 401

    def test_only_the_exact_path_skips_access(self, client):
        key = basic(key_of(client))
        assert client.post("/webhooks/arr/", json=MOVIE_ADDED, headers=key).status_code == 403
        assert client.post("/webhooks/other", json=MOVIE_ADDED, headers=key).status_code == 403
        # the key doesn't open the rest of the api
        assert client.post("/api/jobs/removers", json={}, headers={**key, **ORIGIN}).status_code == 403

    def test_bad_bodies(self, client):
        key = basic(key_of(client))
        assert client.post("/webhooks/arr", content=b"not json", headers=key).status_code == 400
        assert client.post("/webhooks/arr", json=[1], headers=key).status_code == 400
        big = b'{"x": "' + b"a" * 1_100_000 + b'"}'
        assert client.post("/webhooks/arr", content=big, headers=key).status_code == 413

    def test_adds_close_together_share_a_queued_run(self, settings):
        release = threading.Event()
        started = threading.Event()
        calls = []

        def slow(job, s):
            calls.append(job)
            started.set()
            release.wait(5)
            return JobResult(job)

        app = create_app(settings, keys=FakeKeys(), run_job=slow, load_config=live_config, run_scheduler=False)
        with TestClient(app) as c:
            key = basic(key_of(c))
            first = c.post("/webhooks/arr", json=MOVIE_ADDED, headers=key).json()["queued"]
            started.wait(5)
            # running: the next add is remembered, not queued beside it
            assert c.post("/webhooks/arr", json=MOVIE_ADDED, headers=key).json()["queued"] is None
            assert c.post("/webhooks/arr", json=MOVIE_ADDED, headers=key).json()["queued"] is None
            release.set()
            wait(c)
            wait(c)
            runs = c.get("/api/runs", headers=auth()).json()
        # the first run, then one more for the adds during it
        assert len(calls) == 2
        assert [r["id"] for r in runs][-1] == first

    def test_regenerate_replaces_the_key(self, client):
        old = key_of(client)
        page = client.get("/settings/webhook", headers=auth()).text
        assert f'value="{old}"' in page
        assert "/webhooks/arr" in page
        response = client.post("/settings/api-key", headers={**auth(), **ORIGIN}, follow_redirects=False)
        assert response.status_code == 303
        new = key_of(client)
        assert new != old and len(new) == 32
        assert client.post("/webhooks/arr", json=MOVIE_ADDED, headers=basic(old)).status_code == 401
        assert client.post("/webhooks/arr", json=MOVIE_ADDED, headers=basic(new)).status_code == 200

    def test_settings_need_access(self, client):
        assert client.get("/settings").status_code == 403
        assert client.post("/settings/api-key", headers=ORIGIN).status_code == 403
        assert client.post("/settings/api-key", headers=auth()).status_code == 403  # no origin

    def test_key_is_generated_once_and_kept(self, settings):
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
        with TestClient(app) as c:
            first = key_of(c)
        app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
        with TestClient(app) as c:
            assert key_of(c) == first
