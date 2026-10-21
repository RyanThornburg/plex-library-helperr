"""Tests for the web app: access auth, security headers, database"""

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import inspect

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


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, keys=FakeKeys())) as c:
        yield c


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
