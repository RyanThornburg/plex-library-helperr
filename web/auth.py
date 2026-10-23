"""auth.py - require a valid cloudflare access token on every request"""

import logging
from typing import Any, Protocol

import anyio.to_thread
import jwt
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from web.settings import WebSettings

logger = logging.getLogger(__name__)

TOKEN_HEADER = b"cf-access-jwt-assertion"
# reachable without a token, they reveal nothing beyond the app being up
PUBLIC_PATHS = frozenset({"/healthz"})
# called by radarr/sonarr over the docker network, they check the app's api key instead
# (web/webhooks.py). Exact paths, so nothing else slips past the access check
API_KEY_PATHS = frozenset({"/webhooks/arr"})


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
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS | API_KEY_PATHS:
            await self.app(scope, receive, send)
            return

        token = dict(scope["headers"]).get(TOKEN_HEADER, b"").decode("latin-1")
        try:
            if not token:
                raise AccessDenied("no access token")
            # fetching the team's keys is blocking network io, keep it off the event loop
            email = await anyio.to_thread.run_sync(self.verifier.verify, token)
        except AccessDenied as e:
            logger.warning("WEB\t Denied %s %s: %s", scope["method"], scope["path"], e)
            await PlainTextResponse("Forbidden", status_code=403)(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = email
        await self.app(scope, receive, send)
