"""Tests for AUTH_MODE=password: the app's own login, sessions, setup token and reset"""

import logging
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
import web.login
from config.models import ConfigSettings
from results import JobResult, utc_now
from web.app import create_app
from web.db import LoginSession, make_engine, migrate
from web.login import (
    FAILURE_WINDOW,
    MAX_FAILURES,
    SESSION_COOKIE,
    SESSION_IDLE,
    SESSION_MAX,
    SESSION_TOUCH,
    LoginThrottle,
    create_session,
    end_other_sessions,
    hash_password,
    is_set_up,
    new_setup_token,
    password_problems,
    reset_login,
    safe_next,
    session_user,
    set_login,
    username_problems,
    verify_password,
)
from web.settings import WebSettings, load_web_settings

ORIGIN = {"Origin": "http://testserver"}
FORM = {**ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
PASSWORD = "correct horse battery"
SECRET_KEY = "s" * 40


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    """the real cost is ~0.1 s a hash, too slow for a test suite"""
    monkeypatch.setattr(web.login, "SCRYPT_N", 2**8)


@pytest.fixture
def settings(tmp_path) -> WebSettings:
    return WebSettings(
        team_domain="",
        audience="",
        allowed_emails=frozenset(),
        database_path=tmp_path / "helperr.db",
        secret_key=SECRET_KEY,
        auth_mode="password",
    )


def no_config() -> ConfigSettings:
    return ConfigSettings(plex=None, radarr=None, sonarr=None)


def ran(job: str, settings: ConfigSettings) -> JobResult:
    return JobResult(job)


def make_client(settings) -> TestClient:
    app = create_app(settings, run_job=ran, load_config=no_config, run_scheduler=False)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(settings):
    with make_client(settings) as c:
        yield c


def engine_of(client):
    return client.app.state.engine


def post(client, url: str, body: dict[str, str], **kwargs):
    return client.post(url, data=body, headers={**ORIGIN, **kwargs.pop("headers", {})}, **kwargs)


def setup_login(client, username: str = "admin", password: str = PASSWORD):
    token = new_setup_token(engine_of(client))
    return post(client, "/setup", {"token": token, "username": username, "password": password, "confirm": password})


@pytest.fixture
def signed_in(client):
    response = setup_login(client)
    assert response.status_code == 303
    return client


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


class TestAuthMode:
    def test_without_cloudflare_settings_it_is_password(self):
        settings = load_web_settings({"HELPERR_SECRET_KEY": SECRET_KEY})
        assert settings.auth_mode == "password"
        assert settings.allowed_emails == frozenset()

    def test_cloudflare_settings_without_auth_mode_stay_cloudflare(self):
        """installs from before password mode keep working unchanged"""
        settings = load_web_settings({
            "CF_ACCESS_TEAM_DOMAIN": "team.cloudflareaccess.com",
            "CF_ACCESS_AUD": "a" * 64,
            "ALLOWED_EMAILS": "me@example.com",
            "HELPERR_SECRET_KEY": SECRET_KEY,
        })
        assert settings.auth_mode == "cloudflare"

    def test_half_configured_cloudflare_is_refused_not_password(self):
        with pytest.raises(ValueError, match="CF_ACCESS_AUD"):
            load_web_settings({"ALLOWED_EMAILS": "me@example.com", "HELPERR_SECRET_KEY": SECRET_KEY})

    def test_cloudflare_mode_needs_its_settings(self):
        with pytest.raises(ValueError, match="CF_ACCESS_TEAM_DOMAIN"):
            load_web_settings({"AUTH_MODE": "cloudflare", "HELPERR_SECRET_KEY": SECRET_KEY})

    def test_password_mode_ignores_cloudflare_settings(self):
        settings = load_web_settings({
            "AUTH_MODE": "Password",
            "CF_ACCESS_AUD": "not checked",
            "HELPERR_SECRET_KEY": SECRET_KEY,
        })
        assert settings.auth_mode == "password"

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValueError, match="AUTH_MODE must be password or cloudflare"):
            load_web_settings({"AUTH_MODE": "none", "HELPERR_SECRET_KEY": SECRET_KEY})

    def test_password_mode_still_needs_the_secret_key(self):
        with pytest.raises(ValueError, match="HELPERR_SECRET_KEY"):
            load_web_settings({"AUTH_MODE": "password"})


# ---------------------------------------------------------------------------
# passwords and the pure helpers
# ---------------------------------------------------------------------------


class TestPasswords:
    def test_round_trip(self):
        stored = hash_password(PASSWORD)
        assert stored.startswith("scrypt$")
        assert PASSWORD not in stored
        assert verify_password(PASSWORD, stored)
        assert not verify_password(PASSWORD + "x", stored)

    def test_each_hash_has_its_own_salt(self):
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    @pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$aa$bb", "scrypt$8$8$1$!!$!!"])
    def test_unreadable_hash_never_matches(self, stored):
        assert not verify_password(PASSWORD, stored)

    def test_password_rules(self):
        assert password_problems(PASSWORD, PASSWORD) == []
        assert password_problems("short", "short") == ["The password needs at least 12 characters."]
        assert password_problems(PASSWORD, PASSWORD + "!") == ["The passwords don't match."]

    @pytest.mark.parametrize(
        ("username", "ok"),
        [("admin", True), ("Ryan T", True), ("", False), ("a" * 65, False), ("ad\nmin", False), ("ad‮min", False)],
    )
    def test_username_rules(self, username, ok):
        assert (username_problems(username) == []) is ok

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("/runs?before=3", "/runs?before=3"),
            ("/status", "/status"),
            (None, "/"),
            ("", "/"),
            ("https://evil.example", "/"),
            ("//evil.example", "/"),
            ("/\\evil.example", "/"),
            ("evil", "/"),
            ("/runs\r\nSet-Cookie: x=1", "/"),
        ],
    )
    def test_next_stays_on_this_site(self, value, expected):
        assert safe_next(value) == expected


class TestThrottle:
    def test_blocks_after_too_many_failures_then_lets_up(self):
        throttle, now = LoginThrottle(), utc_now()
        for i in range(MAX_FAILURES):
            assert throttle.wait("1.2.3.4", now) is None
            throttle.failed("1.2.3.4", now + timedelta(seconds=i))
        assert throttle.wait("1.2.3.4", now + timedelta(seconds=10)) is not None
        assert throttle.wait("5.6.7.8", now) is None
        assert throttle.wait("1.2.3.4", now + FAILURE_WINDOW + timedelta(seconds=MAX_FAILURES)) is None

    def test_success_clears_the_count(self):
        throttle, now = LoginThrottle(), utc_now()
        for _ in range(MAX_FAILURES - 1):
            throttle.failed("1.2.3.4", now)
        throttle.succeeded("1.2.3.4")
        throttle.failed("1.2.3.4", now)
        assert throttle.wait("1.2.3.4", now) is None


# ---------------------------------------------------------------------------
# sessions in the database
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    yield engine
    engine.dispose()


class TestSessions:
    def test_only_the_tokens_hash_is_stored(self, engine):
        set_login(engine, "admin", PASSWORD)
        token = create_session(engine, "admin")
        with Session(engine) as session:
            (row,) = session.query(LoginSession).all()
        assert row.id != token and token not in row.id
        assert session_user(engine, token) == "admin"
        assert session_user(engine, token + "x") is None
        assert session_user(engine, "") is None

    def test_idle_and_old_sessions_end(self, engine):
        set_login(engine, "admin", PASSWORD)
        now = utc_now()
        token = create_session(engine, "admin", now)
        assert session_user(engine, token, now + SESSION_IDLE + timedelta(minutes=1)) is None
        # it was deleted, using it again doesn't bring it back
        assert session_user(engine, token, now) is None

        token = create_session(engine, "admin", now)
        # used every day, it still ends SESSION_MAX after signing in
        day = now
        while day < now + SESSION_MAX - timedelta(days=1):
            day += timedelta(days=1)
            assert session_user(engine, token, day) == "admin"
        assert session_user(engine, token, now + SESSION_MAX + timedelta(minutes=1)) is None

    def test_use_is_recorded_at_most_hourly(self, engine):
        set_login(engine, "admin", PASSWORD)
        now = utc_now()
        token = create_session(engine, "admin", now)
        session_user(engine, token, now + SESSION_TOUCH / 2)
        with Session(engine) as session:
            assert session.query(LoginSession).one().last_seen_at == now
        session_user(engine, token, now + SESSION_TOUCH * 2)
        with Session(engine) as session:
            assert session.query(LoginSession).one().last_seen_at == now + SESSION_TOUCH * 2

    def test_a_new_login_ends_every_session(self, engine):
        set_login(engine, "admin", PASSWORD)
        token = create_session(engine, "admin")
        set_login(engine, "someone", PASSWORD)
        assert session_user(engine, token) is None

    def test_signing_out_others_keeps_this_one(self, engine):
        set_login(engine, "admin", PASSWORD)
        mine, other = create_session(engine, "admin"), create_session(engine, "admin")
        assert end_other_sessions(engine, mine) == 1
        assert session_user(engine, mine) == "admin"
        assert session_user(engine, other) is None

    def test_reset_forgets_the_login(self, engine):
        set_login(engine, "admin", PASSWORD)
        token = create_session(engine, "admin")
        setup_token = reset_login(engine)
        assert not is_set_up(engine)
        assert session_user(engine, token) is None
        assert setup_token


# ---------------------------------------------------------------------------
# the web app in password mode
# ---------------------------------------------------------------------------


class TestSignedOut:
    def test_startup_logs_a_setup_token(self, settings, caplog):
        with caplog.at_level(logging.WARNING, logger="web.login"), make_client(settings):
            pass
        (record,) = [r for r in caplog.records if "setup token" in r.getMessage()]
        token = record.getMessage().rsplit(" ", 1)[-1]
        assert len(token) >= 20

    def test_no_setup_token_logged_once_there_is_a_login(self, settings, caplog):
        with make_client(settings) as c:
            setup_login(c)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="web.login"), make_client(settings):
            pass
        assert not [r for r in caplog.records if "setup token" in r.getMessage()]

    def test_a_page_sends_you_to_sign_in_and_back(self, client):
        response = client.get("/runs?before=4")
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Fruns%3Fbefore%3D4"
        assert client.get("/").headers["location"] == "/login"

    def test_without_a_login_sign_in_is_setup(self, client):
        assert client.get("/login").headers["location"] == "/setup"
        assert client.get("/setup").status_code == 200

    def test_api_and_posts_get_401(self, client):
        assert client.get("/api/runs").status_code == 401
        assert post(client, "/jobs/movers?dry_run=true", {}).status_code == 401
        assert post(client, "/logout", {}).status_code == 401

    def test_public_paths(self, client):
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/app.css").status_code == 200
        # the webhook checks the app's api key itself
        assert client.post("/webhooks/arr", json={}).status_code == 401

    def test_sign_in_pages_show_nothing_about_the_setup(self, client):
        page = client.get("/setup").text
        assert 'class="sidebar' not in page
        assert "/status/" not in page and "Sign out" not in page

    def test_a_forged_cookie_is_refused(self, signed_in):
        signed_in.cookies.clear()
        signed_in.cookies.set(SESSION_COOKIE, "made-up")
        assert signed_in.get("/runs").status_code == 303


class TestSetup:
    def test_wrong_token_creates_nothing(self, client):
        new_setup_token(engine_of(client))
        response = post(client, "/setup", {"token": "guess", "username": "admin", "password": PASSWORD, "confirm": PASSWORD})
        assert response.status_code == 401
        assert "setup token isn&#39;t right" in response.text
        assert not is_set_up(engine_of(client))

    def test_creates_the_login_and_signs_in(self, client):
        response = setup_login(client)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert f"{SESSION_COOKIE}=" in cookie
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        # plain http on a LAN: a Secure cookie would never come back
        assert "Secure" not in cookie
        assert client.get("/runs").status_code == 200

    def test_cookie_is_secure_behind_https(self, client):
        token = new_setup_token(engine_of(client))
        response = post(
            client,
            "/setup",
            {"token": token, "username": "admin", "password": PASSWORD, "confirm": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "Secure" in response.headers["set-cookie"]

    def test_bad_password_keeps_the_token(self, client):
        token = new_setup_token(engine_of(client))
        response = post(client, "/setup", {"token": token, "username": "admin", "password": "short", "confirm": "short"})
        assert response.status_code == 400
        assert "at least 12 characters" in response.text
        assert 'value="admin"' in response.text
        response = post(client, "/setup", {"token": token, "username": "admin", "password": PASSWORD, "confirm": PASSWORD})
        assert response.status_code == 303

    def test_the_token_works_once(self, client):
        token = new_setup_token(engine_of(client))
        body = {"token": token, "username": "admin", "password": PASSWORD, "confirm": PASSWORD}
        assert post(client, "/setup", body).status_code == 303
        client.cookies.clear()
        body["username"] = "intruder"
        assert post(client, "/setup", body).headers["location"] == "/login"
        assert client.get("/setup").headers["location"] == "/login"

    def test_guessing_the_token_is_throttled(self, client):
        new_setup_token(engine_of(client))
        body = {"token": "guess", "username": "admin", "password": PASSWORD, "confirm": PASSWORD}
        for _ in range(MAX_FAILURES):
            assert post(client, "/setup", body).status_code == 401
        assert post(client, "/setup", body).status_code == 429

    def test_needs_same_origin(self, client):
        token = new_setup_token(engine_of(client))
        body = {"token": token, "username": "admin", "password": PASSWORD, "confirm": PASSWORD}
        response = client.post("/setup", data=body, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
        assert not is_set_up(engine_of(client))


class TestSignIn:
    @pytest.fixture
    def signed_out(self, signed_in):
        signed_in.cookies.clear()
        return signed_in

    def test_wrong_password(self, signed_out):
        response = post(signed_out, "/login", {"username": "admin", "password": "nope", "next": "/runs"})
        assert response.status_code == 401
        assert "Wrong username or password." in response.text
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")

    def test_signs_in_and_goes_back(self, signed_out):
        response = post(signed_out, "/login", {"username": "ADMIN", "password": PASSWORD, "next": "/runs?before=2"})
        assert response.status_code == 303
        assert response.headers["location"] == "/runs?before=2"
        page = signed_out.get("/runs")
        assert page.status_code == 200
        assert "admin" in page.text and 'action="/logout"' in page.text

    def test_next_cant_leave_the_site(self, signed_out):
        response = post(signed_out, "/login", {"username": "admin", "password": PASSWORD, "next": "//evil.example/x"})
        assert response.headers["location"] == "/"

    def test_login_page_keeps_next(self, signed_out):
        page = signed_out.get("/login?next=%2Fremovers")
        assert 'name="next" value="/removers"' in page.text

    def test_already_signed_in_skips_the_form(self, signed_in):
        assert signed_in.get("/login?next=/runs").headers["location"] == "/runs"

    def test_guessing_is_throttled_even_with_the_right_password(self, signed_out):
        for _ in range(MAX_FAILURES):
            post(signed_out, "/login", {"username": "admin", "password": "nope"})
        response = post(signed_out, "/login", {"username": "admin", "password": PASSWORD})
        assert response.status_code == 429
        assert "Try again in" in response.text

    def test_sign_out_ends_the_session(self, signed_in):
        token = signed_in.cookies.get(SESSION_COOKIE)
        response = post(signed_in, "/logout", {})
        assert response.headers["location"] == "/login?signed_out=1"
        assert session_user(engine_of(signed_in), token) is None
        signed_in.cookies.set(SESSION_COOKIE, token)
        assert signed_in.get("/runs").status_code == 303

    def test_runs_are_requested_by_the_username(self, signed_in):
        response = signed_in.post("/api/jobs/movers", json={"dry_run": True}, headers=ORIGIN)
        assert response.status_code == 202
        signed_in.app.state.runner.wait_idle()
        (run,) = signed_in.get("/api/runs").json()
        assert run["requested_by"] == "admin"


class TestLoginSettings:
    def change(self, client, current: str, new: str, confirm: str | None = None):
        return post(client, "/settings/login/password", {"current": current, "password": new, "confirm": confirm or new})

    def test_page(self, signed_in):
        page = signed_in.get("/settings/login").text
        assert "Change password" in page
        assert "--reset-login" in page

    def test_wrong_current_password(self, signed_in):
        response = self.change(signed_in, "nope", "a whole new password")
        assert response.status_code == 400
        assert "current password isn" in response.text

    def test_change_signs_out_other_browsers(self, signed_in):
        engine = engine_of(signed_in)
        other = create_session(engine, "admin")
        response = self.change(signed_in, PASSWORD, "a whole new password")
        assert response.headers["location"] == "/settings/login?changed=1"
        assert session_user(engine, other) is None
        assert signed_in.get("/settings/login").status_code == 200
        signed_in.cookies.clear()
        assert post(signed_in, "/login", {"username": "admin", "password": PASSWORD}).status_code == 401
        assert post(signed_in, "/login", {"username": "admin", "password": "a whole new password"}).status_code == 303

    def test_new_password_rules(self, signed_in):
        response = self.change(signed_in, PASSWORD, "short")
        assert response.status_code == 400
        assert "at least 12 characters" in response.text

    def test_sign_out_other_browsers(self, signed_in):
        engine = engine_of(signed_in)
        other = create_session(engine, "admin")
        response = post(signed_in, "/settings/login/sign-out-others", {})
        assert response.headers["location"] == "/settings/login?signed_out=1"
        assert session_user(engine, other) is None
        assert signed_in.get("/settings/login").status_code == 200


class TestResetCommand:
    def test_forgets_the_login_and_prints_a_working_token(self, signed_in, capsys):
        assert main.main(["--reset-login"]) is None
        out = capsys.readouterr().out
        token = re.search(r"^\s{4}(\S+)$", out, re.MULTILINE).group(1)
        # every browser is signed out, the app wants a new login
        assert signed_in.get("/runs").status_code == 303
        assert signed_in.get("/login").headers["location"] == "/setup"
        body = {"token": token, "username": "admin2", "password": PASSWORD, "confirm": PASSWORD}
        assert post(signed_in, "/setup", body).status_code == 303
        assert signed_in.get("/runs").status_code == 200

    def test_works_on_a_new_database(self, tmp_path, capsys):
        assert main.main(["--reset-login"]) is None
        assert "setup token" in capsys.readouterr().out
        assert (tmp_path / "helperr.db").exists()


class TestCloudflareMode:
    def test_password_pages_are_404(self, tmp_path):
        from tests.test_web import AUD, EMAIL, TEAM, FakeKeys, auth

        settings = WebSettings(
            team_domain=TEAM, audience=AUD, allowed_emails=frozenset({EMAIL}),
            database_path=tmp_path / "helperr.db", secret_key=SECRET_KEY,
        )
        app = create_app(settings, keys=FakeKeys(), run_job=ran, load_config=no_config, run_scheduler=False)
        with TestClient(app, follow_redirects=False) as c:
            for path in ("/login", "/setup"):
                assert c.get(path, headers=auth()).status_code == 404
            assert c.post("/logout", headers={**auth(), **ORIGIN}).status_code == 404
            page = c.get("/settings/login", headers=auth()).text
            assert "Cloudflare Access signs you in" in page
            assert 'action="/logout"' not in c.get("/runs", headers=auth()).text
            # cloudflare mode still needs a token for static files
            assert c.get("/static/app.css").status_code == 403
