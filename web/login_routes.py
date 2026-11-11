"""login_routes.py - AUTH_MODE=password's pages: /setup (the first login, with the setup
token from the log), /login, /logout and Settings → Login (change the password, sign
other browsers out). In cloudflare mode Access signs people in: /login, /setup and
/logout are 404s and Settings → Login says so."""

import logging
from functools import partial
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import Engine

from web.login import (
    MIN_PASSWORD,
    SESSION_COOKIE,
    SESSION_MAX,
    LoginThrottle,
    change_password,
    check_login,
    clean_username,
    create_session,
    end_other_sessions,
    end_session,
    is_set_up,
    login_username,
    password_problems,
    safe_next,
    session_count,
    session_user,
    set_login,
    setup_token_matches,
    username_problems,
)
from web.routes import form_fields, templates
from web.settings_common import page

logger = logging.getLogger(__name__)

router = APIRouter()

RESET_COMMAND = "docker exec plex-library-helperr python main.py --reset-login"


def password_mode(request: Request) -> bool:
    return getattr(request.app.state, "auth_mode", "cloudflare") == "password"


def require_password_mode(request: Request) -> Engine:
    if not password_mode(request):
        raise HTTPException(status_code=404)
    return request.app.state.engine


def throttle(request: Request) -> LoginThrottle:
    return request.app.state.login_throttle


def client_key(request: Request) -> str:
    """the connection's address. Never X-Forwarded-For: anyone could make one up"""
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    """https to the browser, also behind a proxy that says so (cloudflared, nginx)"""
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def with_session(response: Response, request: Request, token: str) -> Response:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_MAX.total_seconds()),
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    return response


def bare_page(request: Request, name: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    """a sign-in page: no sidebar, nothing about the setup until you're in"""
    return templates.TemplateResponse(
        request, name, {"bare": True, "user": None, "errors": [], **context}, status_code=status_code
    )


def wait_text(request: Request) -> str | None:
    wait = throttle(request).wait(client_key(request))
    if wait is None:
        return None
    minutes = max(1, round(wait.total_seconds() / 60))
    return f"Too many wrong tries. Try again in {minutes} minute{'s' if minutes != 1 else ''}."


# ---------------------------------------------------------------------------
# setup: the first login
# ---------------------------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request) -> Response:
    engine = require_password_mode(request)
    if is_set_up(engine):
        return RedirectResponse("/login", status_code=303)
    return bare_page(request, "setup.html", {"username": "", "min_password": MIN_PASSWORD})


@router.post("/setup", response_class=HTMLResponse)
async def setup(request: Request) -> Response:
    engine = require_password_mode(request)
    fields = await form_fields(request)
    return await anyio.to_thread.run_sync(partial(_setup, request, engine, fields))


def _setup(request: Request, engine: Engine, fields: dict[str, str]) -> Response:
    if is_set_up(engine):
        return RedirectResponse("/login", status_code=303)
    username = clean_username(fields.get("username", ""))
    context = {"username": username, "min_password": MIN_PASSWORD}
    key = client_key(request)
    if waiting := wait_text(request):
        logger.warning("WEB\t Refused a setup attempt from %s: too many wrong tries", key)
        return bare_page(request, "setup.html", {**context, "errors": [waiting]}, 429)
    if not setup_token_matches(engine, fields.get("token", "")):
        throttle(request).failed(key)
        logger.warning("WEB\t Wrong setup token from %s", key)
        error = "That setup token isn't right. Copy it from the container's log; restarting the app writes a new one."
        return bare_page(request, "setup.html", {**context, "errors": [error]}, 401)
    problems = username_problems(username) + password_problems(
        fields.get("password", ""), fields.get("confirm", "")
    )
    if problems:
        return bare_page(request, "setup.html", {**context, "errors": problems}, 400)
    throttle(request).succeeded(key)
    set_login(engine, username, fields["password"])
    logger.info("WEB\t Login created for %r from %s", username, key)
    return with_session(RedirectResponse("/", status_code=303), request, create_session(engine, username))


# ---------------------------------------------------------------------------
# sign in and out
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    engine = require_password_mode(request)
    if not is_set_up(engine):
        return RedirectResponse("/setup", status_code=303)
    target = safe_next(request.query_params.get("next"))
    if session_user(engine, request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse(target, status_code=303)
    return bare_page(
        request,
        "login.html",
        {"next": target, "username": "", "signed_out": request.query_params.get("signed_out") == "1", "reset_command": RESET_COMMAND},
    )


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request) -> Response:
    engine = require_password_mode(request)
    fields = await form_fields(request)
    return await anyio.to_thread.run_sync(partial(_login, request, engine, fields))


def _login(request: Request, engine: Engine, fields: dict[str, str]) -> Response:
    if not is_set_up(engine):
        return RedirectResponse("/setup", status_code=303)
    target = safe_next(fields.get("next"))
    typed = clean_username(fields.get("username", ""))
    context = {"next": target, "username": typed, "reset_command": RESET_COMMAND}
    key = client_key(request)
    if waiting := wait_text(request):
        logger.warning("WEB\t Refused a sign-in from %s: too many wrong tries", key)
        return bare_page(request, "login.html", {**context, "errors": [waiting]}, 429)
    if not check_login(engine, typed, fields.get("password", "")):
        throttle(request).failed(key)
        logger.warning("WEB\t Failed sign-in as %r from %s", typed, key)
        return bare_page(request, "login.html", {**context, "errors": ["Wrong username or password."]}, 401)
    throttle(request).succeeded(key)
    username = login_username(engine) or typed
    logger.info("WEB\t %s signed in from %s", username, key)
    return with_session(RedirectResponse(target, status_code=303), request, create_session(engine, username))


@router.post("/logout")
def logout(request: Request) -> Response:
    engine = require_password_mode(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        end_session(engine, token)
    logger.info("WEB\t %s signed out", getattr(request.state, "user", None))
    response = RedirectResponse("/login?signed_out=1", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=is_https(request))
    return response


# ---------------------------------------------------------------------------
# Settings → Login
# ---------------------------------------------------------------------------


def login_settings(request: Request, errors: list[str] | None = None, **context: Any) -> HTMLResponse:
    password = password_mode(request)
    return page(
        request,
        "login",
        {
            "password_mode": password,
            "sessions": session_count(request.app.state.engine) if password else 0,
            "min_password": MIN_PASSWORD,
            "reset_command": RESET_COMMAND,
            "changed": request.query_params.get("changed") == "1",
            "signed_out": request.query_params.get("signed_out"),
            **context,
        },
        errors,
    )


@router.get("/settings/login", response_class=HTMLResponse)
def login_settings_page(request: Request) -> HTMLResponse:
    return login_settings(request)


@router.post("/settings/login/password", response_class=HTMLResponse)
async def password_change(request: Request) -> Response:
    engine = require_password_mode(request)
    fields = await form_fields(request)
    return await anyio.to_thread.run_sync(partial(_password_change, request, engine, fields))


def _password_change(request: Request, engine: Engine, fields: dict[str, str]) -> Response:
    key = client_key(request)
    if waiting := wait_text(request):
        return login_settings(request, [waiting])
    user = request.state.user
    if not check_login(engine, user, fields.get("current", "")):
        throttle(request).failed(key)
        logger.warning("WEB\t Wrong current password from %s changing %s's password", key, user)
        return login_settings(request, ["The current password isn't right."])
    throttle(request).succeeded(key)
    problems = password_problems(fields.get("password", ""), fields.get("confirm", ""))
    if problems:
        return login_settings(request, problems)
    change_password(engine, fields["password"], request.cookies.get(SESSION_COOKIE))
    logger.info("WEB\t %s changed the password, other sessions signed out", user)
    return RedirectResponse("/settings/login?changed=1", status_code=303)


@router.post("/settings/login/sign-out-others")
def sign_out_others(request: Request) -> Response:
    engine = require_password_mode(request)
    ended = end_other_sessions(engine, request.cookies.get(SESSION_COOKIE))
    logger.info("WEB\t %s signed out %s other sessions", request.state.user, ended)
    return RedirectResponse(f"/settings/login?signed_out={ended}", status_code=303)
