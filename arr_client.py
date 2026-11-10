"""arr_client.py - arrapi clients whose requests time out. arrapi sets no timeout, so a
radarr/sonarr that accepts the connection and never answers would hold the job runner
(and every job after it) forever"""

from types import SimpleNamespace
from typing import Any

import requests
from arrapi import Movie, QualityProfile, RadarrAPI, Series, SonarrAPI

# seconds to connect, and to wait for an answer: a big library's full list is slow
TIMEOUT = (10, 300)


class TimeoutSession(requests.Session):
    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(*args, **kwargs)


def arr_api[T: (RadarrAPI, SonarrAPI)](cls: type[T], url: str, apikey: str) -> T:
    return cls(url, apikey, session=TimeoutSession())


class ArrV3:
    """an arr's /api/v3, for the endpoints arrapi doesn't cover (removers)"""

    def __init__(self, baseurl: str, apikey: str):
        self.url = f"{baseurl.rstrip('/')}/api/v3"
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = apikey

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.url}/{path}", timeout=TIMEOUT, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None


# what rules read from a movie/series; tags and quality profile come from their ids
MEDIA_FIELDS = (
    "id", "title", "path", "added", "genres", "certification", "studio", "network", "tvdbId",
)


def _loaded(media: Movie | Series, name: str) -> Any:
    try:
        return object.__getattribute__(media, name)
    except AttributeError:  # movies have no network, series no studio
        return None


def snapshot(
    media: Movie | Series, tags: dict[int, str], profiles: dict[int, str]
) -> SimpleNamespace:
    """the fields rules match on, read as loaded. arrapi reloads a movie/series from the
    api when a field it read is empty (e.g. no certification), and its tags/profile only
    hold an id until read, one request each. Those come from the arr's lists instead"""
    return SimpleNamespace(
        **{name: _loaded(media, name) for name in MEDIA_FIELDS},
        tags=[
            SimpleNamespace(label=tags.get(i, str(i))) for i in _loaded(media, "tagsIds") or []
        ],
        qualityProfile=SimpleNamespace(
            name=profiles.get(_loaded(media, "qualityProfileId"), "")
        ),
    )


def read_media(
    api: RadarrAPI | SonarrAPI,
    all_fn: str,
    tags: list[Any] | None = None,
    profiles: list[QualityProfile] | None = None,
) -> list[SimpleNamespace]:
    """every movie/series as a snapshot(): three requests however big the library, one
    when the caller already has the arr's tags and quality profiles"""
    tags = api.all_tags() if tags is None else tags
    profiles = api.quality_profile() if profiles is None else profiles
    labels = {t.id: t.label for t in tags}
    names = {p.id: p.name for p in profiles}
    return [snapshot(m, labels, names) for m in getattr(api, all_fn)()]


# each arr's bulk editor: its path and the key holding the ids
EDITORS = {"RADARR": ("movie/editor", "movieIds"), "SONARR": ("series/editor", "seriesIds")}


def edit_media(
    baseurl: str, apikey: str, log_name: str, ids: list[int], changes: dict[str, Any]
) -> list[dict[str, Any]]:
    """PUT the arr's bulk editor for these ids, returning the edited media as the arr
    answers them. arrapi's edit_multiple_* first reads the whole library again to check
    the ids (and reloads every movie without an imdb id); these ids come from it"""
    path, key = EDITORS[log_name]
    response = TimeoutSession().put(
        f"{baseurl.rstrip('/')}/api/v3/{path}",
        json={key: ids, **changes},
        headers={"X-Api-Key": apikey},
    )
    response.raise_for_status()
    return response.json() if response.content else []
