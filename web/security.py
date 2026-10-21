"""security.py - browser hardening headers and cross-site request protection"""

import logging

from starlette.datastructures import MutableHeaders
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# everything is served from this app: no inline scripts/styles, no third party content
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}


class SecurityMiddleware:
    """adds SECURITY_HEADERS to every response and rejects state changing requests whose
    Origin is not this site, so another site can't submit forms with the access cookie"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        if scope["method"] not in SAFE_METHODS and not same_origin(scope):
            logger.warning("WEB\t Rejected cross-site %s %s", scope["method"], scope["path"])
            response = PlainTextResponse("Cross-site request rejected", status_code=403)
            await response(scope, receive, send_with_headers)
            return

        await self.app(scope, receive, send_with_headers)


def same_origin(scope: Scope) -> bool:
    """Origin must match Host; browsers always send Origin on cross-site POSTs"""
    headers = dict(scope["headers"])
    origin = headers.get(b"origin", b"").decode("latin-1")
    host = headers.get(b"host", b"").decode("latin-1")
    return bool(origin and host) and origin in (f"https://{host}", f"http://{host}")
