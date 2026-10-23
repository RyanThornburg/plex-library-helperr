"""app.py - FastAPI app factory, started by `python main.py --serve`"""

import logging
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from web.auth import AccessMiddleware, AccessVerifier, SigningKeyClient
from web.db import make_engine, migrate
from web.jobs import JobRunner, LoadConfig, RunJob
from web.routes import router
from web.webhooks import api_key
from web.webhooks import router as webhook_router
from web.scheduler import Scheduler
from web.security import SecurityMiddleware
from web.settings import WebSettings, load_web_settings

logger = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).parent


def create_app(
    settings: WebSettings | None = None,
    keys: SigningKeyClient | None = None,
    run_job: RunJob | None = None,
    load_config: LoadConfig = config.load_config,
    run_scheduler: bool = True,
) -> FastAPI:
    """keys, run_job and load_config replace the team's signing keys, the real jobs and
    config.toml in tests, which also leave the scheduler off and call its tick()"""
    settings = settings or load_web_settings()
    engine = make_engine(settings.database_path)
    runner = JobRunner(engine, run_job, load_config, settings.allow_remover_apply)
    scheduler = Scheduler(engine, runner)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        migrate(engine)
        api_key(engine)  # generated on the first start, like the *arrs
        runner.start()
        if run_scheduler:
            scheduler.start()
        logger.info(
            "WEB\t Started, access team %s, %s allowed emails, applying remover plans %s",
            settings.team_domain,
            len(settings.allowed_emails),
            "allowed" if settings.allow_remover_apply else "off",
        )
        yield
        if run_scheduler:
            scheduler.stop()
        runner.stop()
        engine.dispose()

    # no interactive api docs, they'd be one more surface to protect
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.load_config = load_config
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(webhook_router)
    # added last runs first: security headers wrap the access check, so 403s get them too
    app.add_middleware(AccessMiddleware, verifier=AccessVerifier(settings, keys))
    app.add_middleware(SecurityMiddleware)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
