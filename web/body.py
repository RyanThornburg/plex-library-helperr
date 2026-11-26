"""body.py - reading a request body with a size cap, for the forms and json the app
parses itself"""

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect


async def read_body(request: Request, limit: int, too_large: str | None = None) -> bytes:
    """the whole body, 413 past limit. A client that goes away before sending it all is
    a 400 rather than an uncaught ClientDisconnect: that logged a traceback on the event
    loop, so anyone could stall the app and fill its log with half-sent posts to /login"""
    body = b""
    try:
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(status_code=413, detail=too_large)
    except ClientDisconnect:
        raise HTTPException(status_code=400, detail="The request ended before its body did.") from None
    return body
