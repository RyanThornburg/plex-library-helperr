"""auth.py - every request needs a sign-in: a valid Cloudflare Access token
(AccessMiddleware, AUTH_MODE=cloudflare) or a session from the app's own login
(PasswordMiddleware, AUTH_MODE=password, web.login)"""

import logging
from typing import Any, Protocol

from urllib.parse import quote

import anyio.to_thread
import jwt
from sqlalchemy import Engine
from starlette.requests import cookie_parser
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from web.login import SESSION_COOKIE, session_user
from web.settings import WebSettings

logger = logging.getLogger(__name__)

TOKEN_HEADER = b"cf-access-jwt-assertion"
# reachable without a token, they reveal nothing beyond the app being up
PUBLIC_PATHS = frozenset({"/healthz"})
# called by radarr/sonarr over the docker network, they check the app's api key instead
# (web/webhooks.py). Exact paths, so nothing else slips past the access check
API_KEY_PATHS = frozenset({"/webhooks/arr"})
# password mode's sign-in pages, and the css/js/icons they need: the app's own code,
# nothing about your setup
LOGIN_PATHS = frozenset({"/login", "/setup"})
STATIC_PREFIX = "/static/"


class SigningKeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class AccessDenied(Exception):
    """the reason is logged, never sent back to the client"""


class AccessVerifier:
    """checks a cloudflare access jwt: signature from the team's published keys, issuer,
    audience, expiry, then the email against the app's own allowlist"""

    def __init__(self, settings: WebSettings, keys: SigningKeyClient | None = None):
        self.settings = settings
        self.keys = keys or jwt.PyJWKClient(
            f"{settings.issuer}/cdn-cgi/access/certs",
            cache_keys=True,
            lifespan=3600,
            timeout=10,
        )

    def verify(self, token: str) -> str:
        """the verified email, or AccessDenied"""
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
                leeway=30,
            )
        except jwt.PyJWTError as e:
            raise AccessDenied(f"invalid token ({type(e).__name__})") from e

        email = claims.get("email")
        if not isinstance(email, str) or email.casefold() not in self.settings.allowed_emails:
            raise AccessDenied("identity not in ALLOWED_EMAILS")
        return email.casefold()


class AccessMiddleware:
    """pure asgi so it wraps every route, static files and 404s alike, failing closed"""

    def __init__(self, app: ASGIApp, verifier: AccessVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if await passed_through(self.app, scope, receive, send):
            return

        token = dict(scope["headers"]).get(TOKEN_HEADER, b"").decode("latin-1")
        try:
            if not token:
                raise AccessDenied("no access token")
            # fetching the team's keys is blocking network io, keep it off the event loop
            email = await anyio.to_thread.run_sync(self.verifier.verify, token)
        except AccessDenied as e:
            # the path is decoded and anyone's to choose: %r, so a %0A can't forge a log line
            logger.warning("WEB\t Denied %s %r: %s", scope["method"], scope["path"], e)
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = email
        await self.app(scope, receive, send)


async def passed_through(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> bool:
    """lifespan and the public/api key paths go straight to the app, anything that isn't
    http is refused. True when it was handled here"""
    if scope["type"] == "lifespan" or (
        scope["type"] == "http" and scope["path"] in PUBLIC_PATHS | API_KEY_PATHS
    ):
        await app(scope, receive, send)
        return True
    if scope["type"] != "http":
        # the app has no websockets: refuse one rather than pass it by unchecked
        logger.warning("WEB\t Denied a %s connection to %r", scope["type"], scope.get("path"))
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008)(scope, receive, send)
        return True
    return False


class PasswordMiddleware:
    """AUTH_MODE=password: a page needs a signed in session (web.login). A browser going
    to a page is sent to /login and back after; the JSON API and form posts get a 401"""

    def __init__(self, app: ASGIApp, engine: Engine):
        self.app = app
        self.engine = engine

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if await passed_through(self.app, scope, receive, send):
            return
        path = scope["path"]
        if path in LOGIN_PATHS or path.startswith(STATIC_PREFIX):
            await self.app(scope, receive, send)
            return

        token = session_token(scope)
        user = await anyio.to_thread.run_sync(session_user, self.engine, token) if token else None
        if user is None:
            if scope["method"] in ("GET", "HEAD") and not path.startswith("/api/"):
                query = scope.get("query_string", b"").decode("latin-1")
                target = path + (f"?{query}" if query else "")
                login = "/login" if target == "/" else f"/login?next={quote(target, safe='')}"
                await RedirectResponse(login, status_code=303)(scope, receive, send)
                return
            logger.warning("WEB\t Denied %s %r: not signed in", scope["method"], path)
            await PlainTextResponse("Sign in required", status_code=401)(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


def session_token(scope: Scope) -> str:
    """the session cookie's token, empty when there isn't one"""
    for name, value in scope["headers"]:
        if name == b"cookie":
            token = cookie_parser(value.decode("latin-1")).get(SESSION_COOKIE)
            if token:
                return token
    return ""
