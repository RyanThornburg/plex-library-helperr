"""app.py - FastAPI app factory, started by `python main.py --serve`"""

import logging
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.auth import AccessMiddleware, AccessVerifier, SigningKeyClient
from web.db import JobRun, make_engine, migrate
from web.security import SecurityMiddleware
from web.settings import WebSettings, load_web_settings

logger = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")


def create_app(
    settings: WebSettings | None = None, keys: SigningKeyClient | None = None
) -> FastAPI:
    """keys replaces the team's published signing keys in tests"""
    settings = settings or load_web_settings()
    engine = make_engine(settings.database_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        migrate(engine)
        logger.info(
            "WEB\t Started, access team %s, %s allowed emails",
            settings.team_domain,
            len(settings.allowed_emails),
        )
        yield
        engine.dispose()

    # no interactive api docs, they'd be one more surface to protect
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    # added last runs first: security headers wrap the access check, so 403s get them too
    app.add_middleware(AccessMiddleware, verifier=AccessVerifier(settings, keys))
    app.add_middleware(SecurityMiddleware)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        with Session(engine) as session:
            runs = session.scalars(
                select(JobRun).order_by(JobRun.created_at.desc()).limit(20)
            ).all()
        return templates.TemplateResponse(
            request, "index.html", {"user": request.state.user, "runs": runs}
        )

    return app
