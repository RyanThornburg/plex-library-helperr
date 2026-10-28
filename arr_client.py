"""arr_client.py - arrapi clients whose requests time out. arrapi sets no timeout, so a
radarr/sonarr that accepts the connection and never answers would hold the job runner
(and every job after it) forever"""

from typing import Any

import requests
from arrapi import RadarrAPI, SonarrAPI

# seconds to connect, and to wait for an answer: a big library's full list is slow
TIMEOUT = (10, 300)


class TimeoutSession(requests.Session):
    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(*args, **kwargs)


def arr_api[T: (RadarrAPI, SonarrAPI)](cls: type[T], url: str, apikey: str) -> T:
    return cls(url, apikey, session=TimeoutSession())
