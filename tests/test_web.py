"""Tests for the web app: access auth, security headers, database"""

import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from config.models import ArrConfig, ConfigSettings
from results import Change, JobResult
from web.app import create_app
from web.db import make_engine, migrate
from web.settings import WebSettings, load_web_settings

TEAM = "rattsnest.cloudflareaccess.com"
AUD = "a" * 64
EMAIL = "me@example.com"

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
    )


def live_config() -> ConfigSettings:
    arr = ArrConfig(baseurl="http://arr", apikey="key", tag=None, dry_run=False)
    return ConfigSettings(plex=None, radarr=arr, sonarr=None)


def fake_run_job(job: str, settings: ConfigSettings) -> JobResult:
    return JobResult(job, changes=[Change("RADARR", "move", "/movies/kids", ["Bluey"], "done")])


@pytest.fixture
def client(settings):
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config)
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
            }
        )
        assert settings.team_domain == TEAM
        assert settings.audience == AUD
        assert settings.allowed_emails == {EMAIL, "other@example.com"}
        assert settings.database_path == tmp_path / "helperr.db"

    def test_every_problem_is_reported(self):
        with pytest.raises(ValueError) as e:
            load_web_settings({"CF_ACCESS_TEAM_DOMAIN": "example.com"})
        for name in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "ALLOWED_EMAILS"):
            assert name in str(e.value)


class TestDatabase:
    def test_migrations_create_job_runs(self, tmp_path):
        engine = make_engine(tmp_path / "helperr.db")
        migrate(engine)
        migrate(engine)  # running again at the next start is a no-op
        assert "job_runs" in inspect(engine).get_table_names()

    def test_wal_mode(self, tmp_path):
        engine = make_engine(tmp_path / "helperr.db")
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


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
            # the page button goes to the active run instead of failing
            page = client.post("/jobs/movers?dry_run=true", headers=headers, follow_redirects=False)
            assert page.headers["location"] == f"/runs/{first}"
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
