"""config_api.py - the config as json: read it, replace it (PUT /api/config), its
versions and restoring one"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config.document import secrets_in
from web.config_store import ConfigConflict
from web.db import ConfigVersion
from web.settings_common import (
    LiveUnconfirmed,
    save_checked,
    store,
)


logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# json api
# ---------------------------------------------------------------------------


def version_to_dict(version: ConfigVersion, include_document: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": version.id,
        "note": version.note,
        "created_at": version.created_at.isoformat() + "Z",
        "created_by": version.created_by,
    }
    if include_document:
        data["config"] = version.document
    return data


@router.get("/api/config")
def api_config(request: Request) -> dict[str, Any]:
    """the config without its secrets, which are only listed"""
    version, document = store(request).document()
    return {
        "version": version,
        "config": document,
        "secrets": {
            name: {"updated_at": info.updated_at.isoformat() + "Z", "readable": info.readable}
            for name, info in store(request).secret_info().items()
        },
    }


class ConfigRequest(BaseModel):
    # the version this change is based on, 0 before any
    version: int
    config: dict[str, Any]
    # turning a job live has to be asked for
    confirm_live: bool = False


def api_commit(request: Request, body: ConfigRequest, note: str) -> JSONResponse:
    """save_checked(), its refusals as json"""
    try:
        version = save_checked(request, body.config, note, body.version, confirmed=body.confirm_live)
    except LiveUnconfirmed as e:
        return JSONResponse({"error": f"{e}, send confirm_live: true", "live": e.jobs}, status_code=422)
    except ConfigConflict as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return JSONResponse({"version": version})


@router.put("/api/config")
def api_save_config(request: Request, body: ConfigRequest) -> JSONResponse:
    """replace the config, secrets aren't part of it and stay as they are"""
    if secrets := secrets_in(body.config):
        return JSONResponse(
            {"error": f"secrets can only be set on Settings → Connections: {secrets}"},
            status_code=422,
        )
    return api_commit(request, body, "Saved through the api")


@router.get("/api/config/versions")
def api_versions(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return [version_to_dict(v) for v in store(request).history(max(1, min(limit, 500)))]


@router.get("/api/config/versions/{version_id}")
def api_version(request: Request, version_id: int) -> dict[str, Any]:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return version_to_dict(version, include_document=True)


class RestoreRequest(BaseModel):
    version: int
    confirm_live: bool = False


@router.post("/api/config/versions/{version_id}/restore")
def api_restore(request: Request, version_id: int, body: RestoreRequest) -> JSONResponse:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return api_commit(
        request,
        ConfigRequest(version=body.version, config=version.document, confirm_live=body.confirm_live),
        f"Restored version {version_id}",
    )
