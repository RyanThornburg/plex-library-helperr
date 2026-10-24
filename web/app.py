"""app.py - FastAPI app factory, started by `python main.py --serve`"""

import logging
import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import config
from web.auth import AccessMiddleware, AccessVerifier, SigningKeyClient
from web.config_store import ConfigStore
from web.db import make_engine, migrate
from web.jobs import JobRunner, LoadConfig, RunJob
from web.lookups import Lookups
from web.routes import router
from web.webhooks import api_key
from web.webhooks import router as webhook_router
from web.scheduler import Scheduler
from web.settings_routes import router as settings_router
from web.stats import StatsSync
from web.stats_routes import router as stats_router
from web.secret_box import SecretBox
from web.security import SecurityMiddleware
from web.settings import WebSettings, load_web_settings

logger = logging.getLogger(__name__)

WEB_DIR = pathlib.Path(__file__).parent


def create_app(
    settings: WebSettings | None = None,
    keys: SigningKeyClient | None = None,
    run_job: RunJob | None = None,
    load_config: LoadConfig | None = None,
    run_scheduler: bool = True,
    lookups: Lookups | None = None,
) -> FastAPI:
    """keys, run_job, load_config and lookups replace the team's signing keys, the real
    jobs, the stored config jobs run with and the *arr/plex data the settings pages use
    in tests, which also leave the scheduler and stats sync off and call tick()"""
    settings = settings or load_web_settings()
    engine = make_engine(settings.database_path)
    store = ConfigStore(engine, SecretBox(settings.secret_key))
    load_config = load_config or store.load_settings
    runner = JobRunner(
        engine, run_job, load_config, settings.allow_remover_apply, store.web_defaults
    )
    scheduler = Scheduler(engine, runner)
    stats = StatsSync(engine, store.load_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        migrate(engine)
        api_key(engine)  # generated on the first start, like the *arrs
        # config.toml is imported into the first config version, then never read again
        app.state.import_error = store.import_toml(config.config_toml_path())
        runner.start()
        if run_scheduler:
            scheduler.start()
            stats.start()
        logger.info(
            "WEB\t Started, access team %s, %s allowed emails, applying remover plans %s",
            settings.team_domain,
            len(settings.allowed_emails),
            "allowed" if settings.allow_remover_apply else "off",
        )
        yield
        if run_scheduler:
            stats.stop()
            scheduler.stop()
        runner.stop()
        engine.dispose()

    # no interactive api docs, they'd be one more surface to protect
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.stats = stats
    app.state.load_config = load_config
    app.state.store = store
    app.state.lookups = lookups or Lookups()
    app.state.import_error = None
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(router)
    app.include_router(settings_router)
    app.include_router(webhook_router)
    app.include_router(stats_router)
    # added last runs first: security headers wrap the access check, so 403s get them too
    app.add_middleware(AccessMiddleware, verifier=AccessVerifier(settings, keys))
    app.add_middleware(SecurityMiddleware)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
