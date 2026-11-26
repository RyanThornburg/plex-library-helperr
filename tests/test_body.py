import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from web.body import read_body


def request_of(*messages):
    """a request whose body arrives as these asgi messages"""
    queue = list(messages)

    async def receive():
        return queue.pop(0)

    scope = {"type": "http", "method": "POST", "path": "/login", "headers": []}
    return Request(scope, receive)


def body(more_body=False, data=b"ab"):
    return {"type": "http.request", "body": data, "more_body": more_body}


def test_reads_every_chunk():
    request = request_of(body(True, b"a=1"), body(False, b"&b=2"))
    assert asyncio.run(read_body(request, 100)) == b"a=1&b=2"


def test_too_large_is_413():
    request = request_of(body(True, b"x" * 60), body(False, b"x" * 60))
    with pytest.raises(HTTPException) as e:
        asyncio.run(read_body(request, 100, "too big"))
    assert (e.value.status_code, e.value.detail) == (413, "too big")


def test_client_gone_mid_body_is_400_not_a_traceback():
    """a half-sent post used to raise ClientDisconnect out of the handler, logging a
    traceback on the event loop for every one"""
    request = request_of(body(True), {"type": "http.disconnect"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(read_body(request, 100))
    assert e.value.status_code == 400
