"""assistant_routes.py - the Assistant page (/assistant: ask a question, a chat's
investigation and the fixes it proposed, confirm or dismiss them), its json api and
Settings → Assistant (the Anthropic API key, model, effort and tool budget)"""

import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import ASSISTANT_EFFORTS, ASSISTANT_MODELS, AssistantConfig
from logging_config import failure_text, redacted
from web.assistant import TOOL_LABELS, AssistantBusy, AssistantNotSetUp, AssistantRunner, api_failure, turns
from web.assistant_actions import ActionInvalid, button, perform
from web.db import AssistantAction, AssistantChat, AssistantMessage
from web.jobs import utc_now
from web.routes import loaded_settings, templates
from web.secret_box import SecretError
from web.settings_common import FormLists, commit, first, page, store

logger = logging.getLogger(__name__)

router = APIRouter()

# chats listed beside the conversation
CHATS_SHOWN = 30
# a proposal older than this has to be asked for again: what it was based on has moved on
ACTION_MAX_AGE = timedelta(hours=24)
SECRET = "assistant.apikey"
EFFORT_HINTS = {
    "low": "low: quickest and cheapest, for simple lookups",
    "medium": "medium",
    "high": "high: thorough, the default",
    "xhigh": "xhigh: more thorough, costs more",
    "max": "max: the most thorough, costs the most",
}

templates.env.globals["action_button"] = button
templates.env.globals["TOOL_LABELS"] = TOOL_LABELS


def assistant(request: Request) -> AssistantRunner:
    return request.app.state.assistant


def _chats(request: Request) -> list[AssistantChat]:
    with Session(request.app.state.engine) as session:
        return list(session.scalars(
            select(AssistantChat).order_by(AssistantChat.updated_at.desc()).limit(CHATS_SHOWN)
        ))


def _chat(request: Request, chat_id: int) -> tuple[AssistantChat, list[AssistantMessage], list[AssistantAction]]:
    with Session(request.app.state.engine) as session:
        chat = session.get(AssistantChat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="There's no such chat: it may have been deleted.")
        messages = list(session.scalars(
            select(AssistantMessage).where(AssistantMessage.chat_id == chat_id).order_by(AssistantMessage.id)
        ))
        actions = list(session.scalars(select(AssistantAction).where(AssistantAction.chat_id == chat_id)))
    return chat, messages, actions


def chat_page(
    request: Request,
    chat_id: int | None,
    error: str | None = None,
    draft: str = "",
    panel: bool = False,
    status_code: int | None = None,
) -> HTMLResponse:
    """the Assistant page, or with panel the chat popup's fragment (assistant_panel.html)"""
    settings, problem = loaded_settings(request)
    chat, shown = None, []
    if chat_id is not None:
        chat, messages, actions = _chat(request, chat_id)
        shown = turns(messages, actions)
    return templates.TemplateResponse(
        request,
        "assistant_panel.html" if panel else "assistant.html",
        {
            "user": request.state.user,
            "chat": chat,
            "turns": shown,
            "chats": _chats(request),
            "set_up": settings is not None and settings.assistant is not None,
            "config": settings.assistant if settings else None,
            "models": ASSISTANT_MODELS,
            "settings_problem": problem,
            "error": error,
            "draft": draft,
            "expired": lambda a: utc_now() - a.created_at > ACTION_MAX_AGE,
        },
        status_code=status_code or (400 if error else 200),
    )


@router.get("/assistant", response_class=HTMLResponse)
def assistant_page(request: Request) -> HTMLResponse:
    return chat_page(request, None)


# the chat popup's fragment: before /assistant/{chat_id}, which would take "panel" as an id
@router.get("/assistant/panel", response_class=HTMLResponse)
def assistant_panel(request: Request) -> HTMLResponse:
    return chat_page(request, None, panel=True)


@router.get("/assistant/panel/{chat_id}", response_class=HTMLResponse)
def assistant_panel_chat(request: Request, chat_id: int) -> HTMLResponse:
    return chat_page(request, chat_id, panel=True)


def is_panel(fields: dict[str, list[str]]) -> bool:
    """a form posted from the chat popup, which wants its fragment back"""
    return first(fields, "panel") == "1"


def back_to(chat_id: int, fields: dict[str, list[str]], anchor: str) -> RedirectResponse:
    if is_panel(fields):
        return RedirectResponse(f"/assistant/panel/{chat_id}", status_code=303)
    return RedirectResponse(f"/assistant/{chat_id}#{anchor}", status_code=303)


@router.get("/assistant/{chat_id}", response_class=HTMLResponse)
def assistant_chat(request: Request, chat_id: int) -> HTMLResponse:
    return chat_page(request, chat_id)


def _ask(request: Request, chat_id: int | None, question: str) -> int:
    return assistant(request).ask(chat_id, question, request.state.user)


@router.post("/assistant")
def ask_new(request: Request, fields: FormLists) -> Response:
    question = first(fields, "question")
    try:
        chat_id = _ask(request, None, question)
    except (ValueError, AssistantNotSetUp, SecretError) as e:
        return chat_page(request, None, str(e), question, is_panel(fields))
    return back_to(chat_id, fields, "latest")


@router.post("/assistant/{chat_id}")
def ask_more(request: Request, chat_id: int, fields: FormLists) -> Response:
    question = first(fields, "question")
    try:
        _ask(request, chat_id, question)
    except LookupError:
        raise HTTPException(status_code=404, detail="There's no such chat: it may have been deleted.") from None
    except AssistantBusy as e:
        if is_panel(fields):
            return chat_page(request, chat_id, str(e), question, True, 409)
        raise HTTPException(status_code=409, detail=str(e)) from None
    except (ValueError, AssistantNotSetUp, SecretError) as e:
        return chat_page(request, chat_id, str(e), question, is_panel(fields))
    return back_to(chat_id, fields, "latest")


@router.post("/assistant/{chat_id}/delete")
def delete_chat(request: Request, chat_id: int) -> RedirectResponse:
    with Session(request.app.state.engine) as session:
        chat = session.get(AssistantChat, chat_id)
        if chat is None:
            raise HTTPException(status_code=404)
        if chat.status == "running":
            raise HTTPException(status_code=409, detail="This chat is answering a question, delete it once it's done.")
        session.delete(chat)
        session.commit()
    logger.info("WEB\t Assistant chat %s deleted by %s", chat_id, request.state.user)
    return RedirectResponse("/assistant", status_code=303)


def _proposed(session: Session, action_id: int) -> AssistantAction:
    action = session.get(AssistantAction, action_id)
    if action is None:
        raise HTTPException(status_code=404)
    if action.status != "proposed":
        raise HTTPException(status_code=409, detail=f"This fix was already {action.status}.")
    return action


@router.post("/assistant/actions/{action_id}/confirm")
def confirm_action(request: Request, action_id: int, fields: FormLists) -> RedirectResponse:
    """does a proposed fix: the card's button, which says what it does"""
    settings, problem = loaded_settings(request)
    if settings is None:
        raise HTTPException(status_code=409, detail=f"The settings can't be loaded: {problem}")
    with Session(request.app.state.engine) as session:
        action = _proposed(session, action_id)
        if utc_now() - action.created_at > ACTION_MAX_AGE:
            raise HTTPException(status_code=409, detail="This fix was proposed over a day ago: ask again for a fresh look.")
        try:
            action.result = redacted(perform(settings, action.kind, action.params))
            action.status = "done"
        except ActionInvalid as e:
            action.result, action.status = redacted(str(e)), "failed"
        except Exception as e:
            logger.warning("WEB\t Assistant action %s failed", action_id, exc_info=True)
            action.result, action.status = redacted(f"{type(e).__name__}: {failure_text(e)}"), "failed"
        action.acted_at, action.acted_by = utc_now(), request.state.user
        chat_id = action.chat_id
        logger.info(
            "WEB\t Assistant action %s (%s) confirmed by %s: %s, %s",
            action_id, action.kind, request.state.user, action.status, action.result,
        )
        session.commit()
    return back_to(chat_id, fields, f"action-{action_id}")


@router.post("/assistant/actions/{action_id}/dismiss")
def dismiss_action(request: Request, action_id: int, fields: FormLists) -> RedirectResponse:
    with Session(request.app.state.engine) as session:
        action = _proposed(session, action_id)
        action.status, action.acted_at, action.acted_by = "dismissed", utc_now(), request.state.user
        chat_id = action.chat_id
        session.commit()
    return back_to(chat_id, fields, f"action-{action_id}")


# ---------------------------------------------------------------------------
# json api: ask, and read what came of it. Fixes are confirmed on the page only
# ---------------------------------------------------------------------------


def chat_dict(chat: AssistantChat, messages: list[AssistantMessage], actions: list[AssistantAction]) -> dict[str, Any]:
    return {
        "id": chat.id,
        "title": chat.title,
        "status": chat.status,
        "error": chat.error,
        "created_at": chat.created_at.isoformat() + "Z",
        "updated_at": chat.updated_at.isoformat() + "Z",
        "turns": [
            {
                "question": t.question,
                "asked_at": t.asked_at.isoformat() + "Z",
                "answer_html": t.html,
                "steps": [{"tool": s.name, "label": s.label, "error": s.error} for s in t.steps],
                "actions": [
                    {"id": a.id, "kind": a.kind, "description": a.description, "reason": a.reason,
                     "status": a.status, "result": a.result}
                    for a in t.actions
                ],
            }
            for t in turns(messages, actions)
        ],
        "usage": {
            "input_tokens": chat.input_tokens, "output_tokens": chat.output_tokens,
            "cache_read_tokens": chat.cache_read_tokens, "cache_write_tokens": chat.cache_write_tokens,
            "tool_calls": chat.tool_calls,
        },
    }


@router.get("/api/assistant")
def api_chats(request: Request) -> list[dict[str, Any]]:
    return [
        {"id": c.id, "title": c.title, "status": c.status, "updated_at": c.updated_at.isoformat() + "Z"}
        for c in _chats(request)
    ]


@router.get("/api/assistant/{chat_id}")
def api_chat(request: Request, chat_id: int) -> dict[str, Any]:
    return chat_dict(*_chat(request, chat_id))


@router.post("/api/assistant")
def api_ask(request: Request, body: dict[str, Any] = Body(...)) -> JSONResponse:
    """{"question", "chat_id"?}: queues it, then poll GET /api/assistant/{chat_id}"""
    chat_id = body.get("chat_id")
    if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
        return JSONResponse({"error": "chat_id must be a chat's id"}, status_code=422)
    try:
        chat_id = _ask(request, chat_id, str(body.get("question") or ""))
    except LookupError:
        raise HTTPException(status_code=404) from None
    except AssistantBusy as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except (ValueError, AssistantNotSetUp, SecretError) as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return JSONResponse({"chat_id": chat_id}, status_code=202)


# ---------------------------------------------------------------------------
# Settings → Assistant
# ---------------------------------------------------------------------------


def settings_values(document: dict[str, Any]) -> dict[str, Any]:
    section = document.get("assistant", {})
    return {
        "model": section.get("model", AssistantConfig.model),
        "effort": section.get("effort", AssistantConfig.effort),
        "max_tool_calls": str(section.get("max_tool_calls", AssistantConfig.max_tool_calls)),
    }


def settings_page(
    request: Request, values: dict[str, Any] | None = None, errors: list[str] | None = None, tested: str | None = None
) -> HTMLResponse:
    _, document = store(request).document()
    return page(
        request,
        "assistant",
        {
            "values": values or settings_values(document),
            "secret": store(request).secret_info().get(SECRET),
            "configured": "assistant" in document,
            "models": ASSISTANT_MODELS,
            "efforts": EFFORT_HINTS,
            "tested": tested,
        },
        errors,
    )


@router.get("/settings/assistant", response_class=HTMLResponse)
def assistant_settings(request: Request) -> HTMLResponse:
    return settings_page(request)


@router.post("/settings/assistant")
def save_assistant_settings(request: Request, fields: FormLists) -> Response:
    action = first(fields, "action")
    stored = store(request)
    values = {key: first(fields, key) for key in ("model", "effort", "max_tool_calls")}
    new_secret = fields.get("secret", [""])[-1].strip()
    _, document = stored.document()

    def failed(message: str) -> HTMLResponse:
        return settings_page(request, values, [message])

    if action == "disconnect":
        document.pop("assistant", None)
        return commit(
            request, fields, document, "Assistant turned off", failed, "/settings/assistant",
            {SECRET: None}, check_live=False,
        )

    errors: list[str] = []
    if values["model"] not in ASSISTANT_MODELS:
        errors.append("Pick a model")
    if values["effort"] not in ASSISTANT_EFFORTS:
        errors.append("Pick an effort")
    try:
        max_calls = int(values["max_tool_calls"])
        if not 1 <= max_calls <= 100:
            raise ValueError
    except ValueError:
        errors.append("Tool calls per question must be a whole number from 1 to 100")
        max_calls = AssistantConfig.max_tool_calls
    if not new_secret and SECRET not in stored.secret_info():
        errors.append("API key is required")
    if errors:
        return settings_page(request, values, errors)

    if action == "test":
        try:
            key = new_secret or stored.secret(SECRET)
            model = assistant(request).client_factory(key).models.retrieve(values["model"])
        except SecretError as e:
            return settings_page(request, values, [f"Test failed: {e}"])
        except Exception as e:
            return settings_page(request, values, [f"Test failed: {api_failure(e)}"])
        return settings_page(request, values, tested=f"Anthropic's API answered: {getattr(model, 'display_name', values['model'])} is available to this key")

    document["assistant"] = {"model": values["model"], "effort": values["effort"], "max_tool_calls": max_calls}
    return commit(
        request, fields, document, "Assistant settings", failed, "/settings/assistant",
        {SECRET: new_secret} if new_secret else None, check_live=False,
    )

