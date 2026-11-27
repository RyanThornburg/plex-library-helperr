"""new_request_routes.py - Requests → Add: search Seerr, check a title's poster and what's
in Plex already, pick the server, quality profile, root folder, tags, seasons and who
it's for (each set to Seerr's default), and request it. Posters come through
/posters/{size}/{name} (web.posters)"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from config.models import SeerrConfig
from logging_config import failure_text
from web.new_requests import (
    ARR_LABELS,
    ARRS,
    MEDIA_TYPES,
    Choices,
    RequestInvalid,
    Server,
    Title,
    User,
    default_server,
    plan_request,
    search,
    seerr_link,
    send,
    servers,
    title_details,
    users,
)
from web.body import read_body
from web.posters import PosterCache
from web.routes import MAX_FORM_BYTES, error_page, form_lists, loaded_settings, templates
from web.seerr import SeerrDeclined, SeerrRefused
from web.status import size

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_QUERY = 200
# seerr's search has 20 a page; past this many pages, search again
MAX_PAGE = 50


def failure(e: BaseException) -> str:
    """why seerr couldn't be read, safe to show"""
    if isinstance(e, SeerrRefused | SeerrDeclined):
        return str(e)
    return failure_text(e)


def seerr_config(request: Request) -> tuple[SeerrConfig | None, str | None]:
    """the seerr connection, or None and why there isn't one"""
    settings, problem = loaded_settings(request)
    if settings is None:
        return None, f"The settings can't be loaded: {problem}"
    if settings.seerr is None:
        return None, None
    return settings.seerr, None


def check_type(media_type: str) -> None:
    if media_type not in MEDIA_TYPES:
        raise HTTPException(status_code=404)


def parse_page(value: str | None) -> int:
    if not value:
        return 1
    try:
        page = int(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="page must be a whole number") from None
    if not 1 <= page <= MAX_PAGE:
        raise HTTPException(status_code=422, detail=f"page must be from 1 to {MAX_PAGE}")
    return page


def parse_query(value: str | None) -> str:
    query = " ".join((value or "").split())
    if len(query) > MAX_QUERY:
        raise HTTPException(status_code=422, detail=f"search for at most {MAX_QUERY} characters")
    return query


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@router.get("/requests/new", response_class=HTMLResponse)
def search_page(request: Request) -> HTMLResponse:
    query = parse_query(request.query_params.get("query"))
    page = parse_page(request.query_params.get("page"))
    config, problem = seerr_config(request)
    found, error = None, None
    if config is not None and query:
        try:
            found = search(config, query, page)
        except Exception as e:
            error = f"Seerr couldn't search: {failure(e)}"
    return templates.TemplateResponse(
        request,
        "request_search.html",
        {
            "user": request.state.user,
            "query": query,
            "found": found,
            "error": error,
            "problem": problem,
            "connected": config is not None,
            "max_page": MAX_PAGE,
        },
    )


@router.get("/api/requests/search")
def api_search(request: Request) -> JSONResponse:
    query = parse_query(request.query_params.get("query"))
    page = parse_page(request.query_params.get("page"))
    if not query:
        raise HTTPException(status_code=422, detail="query is required")
    config = connected(request)
    try:
        found = search(config, query, page)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Seerr couldn't search: {failure(e)}") from None
    return JSONResponse({"page": found.page, "pages": found.pages, "results": [r.as_dict() for r in found.results]})


def connected(request: Request) -> SeerrConfig:
    config, problem = seerr_config(request)
    if config is None:
        raise HTTPException(status_code=409, detail=problem or "Seerr isn't connected (Settings → Connections)")
    return config


# ---------------------------------------------------------------------------
# a title: what's there, and the request form
# ---------------------------------------------------------------------------


@dataclass
class Loaded:
    """what the request form needs from seerr, read at once"""

    title: Title
    servers: list[Server]
    users: list[User]
    me: int | None
    # couldn't read seerr's servers (no form) or users (no picker)
    servers_error: str | None = None
    users_error: str | None = None


def load(config: SeerrConfig, media_type: str, tmdb_id: int) -> Loaded:
    """the title, its type's servers and seerr's users. A title seerr can't read raises
    HTTPException (404 when TMDB has no such title)"""
    with ThreadPoolExecutor(max_workers=3) as pool:
        title_job = pool.submit(title_details, config, media_type, tmdb_id)
        servers_job = pool.submit(servers, config, media_type, failure)
        users_job = pool.submit(users, config)
    try:
        title = title_job.result()
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            raise HTTPException(status_code=404, detail=f"Seerr has no {MEDIA_TYPES[media_type].lower()} {tmdb_id}") from None
        raise HTTPException(status_code=502, detail=f"Seerr couldn't read it: {failure(e)}") from None
    loaded = Loaded(title, [], [], None)
    try:
        loaded.servers = servers_job.result()
    except Exception as e:
        loaded.servers_error = f"Seerr couldn't list its {ARR_LABELS[ARRS[media_type]]} servers: {failure(e)}"
    try:
        loaded.users, loaded.me = users_job.result()
    except Exception as e:
        loaded.users_error = f"Seerr couldn't list its users, so it's requested as Seerr's own user: {failure(e)}"
    return loaded


@dataclass
class ServerView:
    """a server's fields on the form: seerr's defaults, or what was posted"""

    server: Server
    usable: bool
    profile: int | None
    root: str | None
    tags: set[int]
    seasons: set[int] = field(default_factory=set[int])


def server_views(loaded: Loaded, posted: Choices | None) -> list[ServerView]:
    views = []
    for server in loaded.servers:
        profile, root, tags = server.defaults(loaded.title.anime)
        view = ServerView(
            server,
            loaded.title.requestable(server.is_4k),
            profile,
            root,
            set(tags),
            set(loaded.title.default_seasons(server.is_4k)),
        )
        if posted is not None and posted.server == server.id:
            view.profile = posted.profile if posted.profile is not None else view.profile
            view.root = posted.root_folder or view.root
            view.tags = set(posted.tags) if posted.tags is not None else view.tags
            view.seasons = set(posted.seasons) if posted.seasons is not None else view.seasons
        views.append(view)
    return views


def form_choices(form: dict[str, list[str]], loaded: Loaded) -> Choices:
    """the posted choices. Each server's fields carry its id (profile_3), only the picked
    server's are read"""

    def number(value: str | None, what: str) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except ValueError:
            raise RequestInvalid(f"{what} must be a number") from None

    def last(name: str) -> str | None:
        values = form.get(name)
        return values[-1] if values else None

    choices = Choices(server=number(last("server"), "server"), user=number(last("user"), "user"))
    server = next((s for s in loaded.servers if s.id == choices.server), None)
    if server is None:
        return choices
    if "options_%d" % server.id in form:
        choices.profile = number(last(f"profile_{server.id}"), "quality profile")
        choices.root_folder = last(f"root_{server.id}") or None
        choices.tags = tuple(n for v in form.get(f"tag_{server.id}", []) if (n := number(v, "tag")) is not None)
    if loaded.title.media_type == "tv":
        choices.seasons = tuple(n for v in form.get(f"season_{server.id}", []) if (n := number(v, "season")) is not None)
    return choices


def render_title(
    request: Request,
    config: SeerrConfig,
    loaded: Loaded,
    posted: Choices | None = None,
    error: str | None = None,
    status_code: int = 200,
    query: str = "",
) -> HTMLResponse:
    views = server_views(loaded, posted)
    picked = posted.server if posted and posted.server is not None else None
    if picked is None or not any(v.server.id == picked and v.usable for v in views):
        default = default_server(loaded.servers, loaded.title)
        picked = default.id if default else None
    requester = posted.user if posted and posted.user is not None else loaded.me
    done = request.query_params.get("done")
    return templates.TemplateResponse(
        request,
        "request_new.html",
        {
            "user": request.state.user,
            "title": loaded.title,
            "loaded": loaded,
            "views": views,
            "picked": picked,
            "requester": requester,
            "error": error,
            "done": done if done in ("approved", "pending") else None,
            "query": query,
            "arr": ARR_LABELS[ARRS[loaded.title.media_type]],
            "has_4k": any(s.is_4k for s in loaded.servers),
            "seerr_url": seerr_link(config, loaded.title.media_type, loaded.title.tmdb_id),
            "size": size,
        },
        status_code=status_code,
    )


@router.get("/requests/new/{media_type}/{tmdb_id}", response_class=HTMLResponse)
def title_page(request: Request, media_type: str, tmdb_id: int) -> HTMLResponse:
    check_type(media_type)
    config, problem = seerr_config(request)
    if config is None:
        return error_page(request, 409, problem or "Connect Seerr on Settings → Connections to add requests.", ("Add a request", "/requests/new"))
    try:
        loaded = load(config, media_type, tmdb_id)
    except HTTPException as e:
        return error_page(request, e.status_code, str(e.detail), ("Add a request", "/requests/new"))
    return render_title(request, config, loaded, query=parse_query(request.query_params.get("query")))


def who_for(planned_user: User | None) -> str:
    return f" for {planned_user.name}" if planned_user else ""


def what(title: Title, seasons: tuple[int, ...]) -> str:
    year = f" ({title.year})" if title.year else ""
    if not seasons:
        return f"{title.title}{year}"
    label = "season" if len(seasons) == 1 else "seasons"
    return f"{title.title}{year} {label} {', '.join(map(str, seasons))}"


@router.post("/requests/new/{media_type}/{tmdb_id}", response_class=HTMLResponse)
async def request_form(request: Request, media_type: str, tmdb_id: int) -> Response:
    check_type(media_type)
    form = await form_lists(request)
    # seerr's answers take a while: in a worker thread, not on the event loop every
    # other request waits on
    return await run_in_threadpool(_request_form, request, media_type, tmdb_id, form)


def _request_form(request: Request, media_type: str, tmdb_id: int, form: dict[str, list[str]]) -> Response:
    config, problem = seerr_config(request)
    if config is None:
        return error_page(request, 409, problem or "Connect Seerr on Settings → Connections to add requests.", ("Add a request", "/requests/new"))
    try:
        loaded = load(config, media_type, tmdb_id)
    except HTTPException as e:
        return error_page(request, e.status_code, str(e.detail), ("Add a request", "/requests/new"))
    query = parse_query(form.get("query", [""])[-1])
    posted = None
    try:
        posted = form_choices(form, loaded)
        if loaded.servers_error:
            raise RequestInvalid(loaded.servers_error)
        planned = plan_request(loaded.title, loaded.servers, loaded.users, posted)
        made = send(config, planned)
    except RequestInvalid as e:
        return render_title(request, config, loaded, posted, str(e), 400, query)
    except Exception as e:
        logger.warning("WEB\t Requesting %s through Seerr failed: %s", loaded.title.title, failure(e))
        return render_title(request, config, loaded, posted, failure(e), 502, query)
    status = "approved" if made.get("status") == 2 else "pending"
    logger.info(
        "WEB\t %s requested %s through Seerr%s (%s, %s)",
        request.state.user,
        what(loaded.title, planned.seasons),
        who_for(planned.user),
        planned.server.label,
        status,
    )
    params = {"done": status}
    if query:
        params["query"] = query
    return RedirectResponse(f"/requests/new/{media_type}/{tmdb_id}?{urlencode(params)}", status_code=303)


@router.get("/api/requests/new/{media_type}/{tmdb_id}")
def api_title(request: Request, media_type: str, tmdb_id: int) -> JSONResponse:
    check_type(media_type)
    config = connected(request)
    loaded = load(config, media_type, tmdb_id)
    return JSONResponse(
        {
            "title": loaded.title.as_dict(),
            "servers": [
                {**s.as_dict(loaded.title.anime), "requestable": loaded.title.requestable(s.is_4k)} for s in loaded.servers
            ],
            "users": [{"id": u.id, "name": u.name} for u in loaded.users],
            "requested_as": loaded.me,
            "errors": [e for e in (loaded.servers_error, loaded.users_error) if e],
        }
    )


def json_ints(body: dict[str, Any], key: str) -> tuple[int, ...] | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        raise RequestInvalid(f"{key} must be a list of numbers")
    return tuple(value)


def json_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise RequestInvalid(f"{key} must be a number")
    return value


@router.post("/api/requests/new/{media_type}/{tmdb_id}")
async def api_request(request: Request, media_type: str, tmdb_id: int) -> JSONResponse:
    """{server, profile, root_folder, tags, seasons, user}, each optional (Seerr's
    default). 201 with seerr's request id and status"""
    check_type(media_type)
    try:
        body = json.loads(await read_body(request, MAX_FORM_BYTES))
    except ValueError:
        raise HTTPException(status_code=422, detail="the body must be json") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="the body must be a json object")
    return await run_in_threadpool(_api_request, request, media_type, tmdb_id, body)


def _api_request(request: Request, media_type: str, tmdb_id: int, body: dict[str, Any]) -> JSONResponse:
    config = connected(request)
    loaded = load(config, media_type, tmdb_id)
    try:
        root = body.get("root_folder")
        if root is not None and not isinstance(root, str):
            raise RequestInvalid("root_folder must be a path")
        choices = Choices(
            server=json_int(body, "server"),
            profile=json_int(body, "profile"),
            root_folder=root,
            tags=json_ints(body, "tags"),
            seasons=json_ints(body, "seasons"),
            user=json_int(body, "user"),
        )
        if loaded.servers_error:
            raise HTTPException(status_code=502, detail=loaded.servers_error)
        planned = plan_request(loaded.title, loaded.servers, loaded.users, choices)
    except RequestInvalid as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        made = send(config, planned)
    except SeerrDeclined as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except Exception as e:
        raise HTTPException(status_code=502, detail=failure(e)) from None
    logger.info(
        "WEB\t %s requested %s through Seerr%s (%s, API)",
        request.state.user,
        what(loaded.title, planned.seasons),
        who_for(planned.user),
        planned.server.label,
    )
    return JSONResponse(
        {"request_id": made.get("id"), "status": "approved" if made.get("status") == 2 else "pending", "sent": planned.body},
        status_code=201,
    )


# ---------------------------------------------------------------------------
# posters
# ---------------------------------------------------------------------------


@router.get("/posters/{size}/{name}")
def poster(request: Request, size: str, name: str) -> Response:
    cache: PosterCache = request.app.state.posters
    found = cache.get(size, name)
    if found is None:
        raise HTTPException(status_code=404)
    data, content_type = found
    # the same file name is always the same picture
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=2592000, immutable"})
