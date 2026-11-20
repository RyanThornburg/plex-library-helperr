"""plex_health.py - what Plex itself says about its health, asked directly with the Plex
token (the Plex card's streams come through Tautulli):

- each movie/show library's trash: Plex trashes whatever it can't find when it scans, so
  a media mount that went away fills a library's trash, and empty trash skips a library
  over its max_items
- whether Plex can read each library's newest files right now (checkFiles): a missing or
  hung media mount on the Plex server, before any scan trashes anything
- the Plex settings Helperr relies on: empty trash automatically off, remote access
  working (else remote viewers go through Plex's relay, about 2 Mbps), hardware
  transcoding, and Allow media deletion for the duplicates job
- a video transcode encoding on the CPU while hardware transcoding is on: Plex lost its
  GPU (Quick Sync, NVENC), usually after a kernel, driver or Plex update

Read only. The parsers are pure, tests feed them dicts"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from web.status import ServiceStatus, Stream

TIMEOUT = 10
# the items holding files, per library type: trash and files are counted on those
LIBRARY_TYPES = {"movie": 1, "show": 4}
# each library's newest items whose files are checked
FILES_CHECKED = 3
# a trash over max_items holding at least this share of the library is a mount gone
TRASH_BAD_SHARE = 0.1
# Plex's settings (/:/prefs ids)
AUTO_EMPTY_TRASH = "autoEmptyTrash"
MEDIA_DELETION = "allowMediaDeletion"
REMOTE_ACCESS = "PublishServerOnPlexOnlineKey"
HW_TRANSCODING = "HardwareAcceleratedCodecs"
HW_ENCODING = "HardwareAcceleratedEncoders"
# /myplex/account's mappingState when remote access isn't working
MAPPING_FAILED = {"failed", "unmapped"}


class PlexRefused(Exception):
    """Plex answered but refused the token, the message has no secrets in it"""


def plex_get(base: str, token: str, path: str, **params: object) -> dict[str, Any]:
    """Plex's json answer. The token goes in a header, never the url"""
    response = requests.get(
        f"{base.rstrip('/')}{path}",
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        params=params,
        timeout=TIMEOUT,
    )
    if response.status_code in (401, 403):
        raise PlexRefused(f"Plex refused the token (HTTP {response.status_code})")
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def container(body: dict[str, Any]) -> dict[str, Any]:
    found = body.get("MediaContainer")
    return found if isinstance(found, dict) else {}


def _list(value: object) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _count(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def flag(value: object) -> bool | None:
    """Plex's json gives booleans as true/false, its xml-shaped values as 1/0"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("1", "true"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("0", "false", ""):
        return False
    return None


# ---------------------------------------------------------------------------
# libraries: trash and files
# ---------------------------------------------------------------------------


@dataclass
class Library:
    title: str
    # items holding files: movies, episodes
    items: int | None = None
    trash: int | None = None
    # files of the newest items checked, and those Plex couldn't read
    checked: int = 0
    unreadable: int = 0
    # Plex took longer than TIMEOUT to check them
    hung: bool = False


def sections(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(key, type, title) of each movie/show library"""
    return [
        (str(d.get("key")), str(d.get("type")), str(d.get("title") or d.get("key")))
        for d in _list(container(body).get("Directory"))
        if d.get("type") in LIBRARY_TYPES and d.get("key") is not None
    ]


def file_access(body: dict[str, Any]) -> tuple[int, int]:
    """(files checked, files Plex can't read) of a /library/metadata/...?checkFiles=1
    answer. Versions in the trash (deletedAt) are gone on purpose, and a part without
    `accessible` wasn't checked"""
    checked = unreadable = 0
    for item in _list(container(body).get("Metadata")):
        for media in _list(item.get("Media")):
            if media.get("deletedAt"):
                continue
            for part in _list(media.get("Part")):
                if (readable := flag(part.get("accessible"))) is None:
                    continue
                checked += 1
                unreadable += not readable
    return checked, unreadable


def read_library(base: str, token: str, key: str, kind: str, title: str) -> Library:
    def total(**params: object) -> int | None:
        found = container(plex_get(
            base, token, f"/library/sections/{key}/all",
            type=LIBRARY_TYPES[kind], **params, **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0},
        ))
        # never `size`, that's the page's (0): unknown stays unknown
        return _count(found.get("totalSize"))

    library = Library(title, items=total(), trash=total(trash=1))
    newest = container(plex_get(
        base, token, f"/library/sections/{key}/all",
        type=LIBRARY_TYPES[kind], sort="addedAt:desc",
        **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": FILES_CHECKED},
    ))
    keys = [str(m["ratingKey"]) for m in _list(newest.get("Metadata")) if m.get("ratingKey") is not None]
    if keys:
        try:
            library.checked, library.unreadable = file_access(
                plex_get(base, token, f"/library/metadata/{','.join(keys)}", checkFiles=1)
            )
        except requests.exceptions.Timeout:
            # Plex answered the listings, so it's up: what it waits on is the files
            library.hung = True
    return library


def library_problems(status: "ServiceStatus", libraries: list[Library], max_trash: int) -> None:
    """a library whose newest files Plex can't read (bad), and a trash over empty trash's
    max_items (warn; bad when it holds TRASH_BAD_SHARE of the library)"""
    for library in libraries:
        if library.hung:
            status.problem(
                "bad",
                f"{library.title}: Plex took over {TIMEOUT}s to check its newest files. "
                "The media mount on the Plex server may be hung",
                f"files:{library.title}",
            )
        elif library.checked and library.unreadable == library.checked:
            status.problem(
                "bad",
                f"{library.title}: Plex can't read its newest files ({library.checked} checked). "
                "Is the media mount on the Plex server down?",
                f"files:{library.title}",
            )
        if library.trash is not None and library.trash > max_trash:
            share = library.trash / library.items if library.items else 0
            status.problem(
                "bad" if share >= TRASH_BAD_SHARE else "warn",
                f"{library.title}: {library.trash:,} items in the trash"
                + (f" ({share:.0%} of the library)" if library.items else "")
                + f", over empty trash's max_items ({max_trash:,}), so empty trash skips it. "
                "A missing media mount trashes a whole library: check Plex can read its files before emptying it",
                f"trash:{library.title}",
            )
    counted = [lib for lib in libraries if lib.trash is not None]
    trash = [(lib.title, lib.trash) for lib in counted if lib.trash]
    if counted:
        status.facts.append(("Trash", ", ".join(f"{t} {n:,}" for t, n in trash) if trash else "Empty"))


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def prefs(body: dict[str, Any]) -> dict[str, Any]:
    """/:/prefs as {id: value}"""
    return {str(s["id"]): s.get("value") for s in _list(container(body).get("Setting")) if s.get("id") is not None}


def mapping(body: dict[str, Any]) -> tuple[str, str]:
    """/myplex/account's (mappingState, mappingError)"""
    account = body.get("MyPlex") if isinstance(body.get("MyPlex"), dict) else container(body)
    return str(account.get("mappingState") or "").lower(), str(account.get("mappingError") or "")


def hardware_expected(settings: dict[str, Any]) -> bool:
    """hardware transcoding and hardware encoding both on. A server without Plex Pass
    has neither setting"""
    return flag(settings.get(HW_TRANSCODING)) is True and flag(settings.get(HW_ENCODING)) is not False


def settings_problems(status: "ServiceStatus", settings: dict[str, Any], remote: tuple[str, str] | None) -> None:
    """the Plex settings Helperr relies on. remote is /myplex/account's mapping, None when
    remote access is off or it couldn't be read"""
    if flag(settings.get(AUTO_EMPTY_TRASH)) is True:
        status.problem(
            "warn",
            "Plex empties the trash after every scan (Settings → Library → Empty trash automatically "
            "after every scan). With it on, a scan while a media mount is missing removes the whole library, "
            "before empty trash's checks can stop it: turn it off",
            f"pref:{AUTO_EMPTY_TRASH}",
        )
    if remote is not None:
        state, error = remote
        if state in MAPPING_FAILED or error:
            status.problem(
                "warn",
                f"Remote access isn't working (Plex says: {error or state}). Viewers away from home go "
                "through Plex's relay, about 2 Mbps, so most of their streams transcode",
                "remote_access",
            )
    if flag(settings.get(HW_TRANSCODING)) is False:
        status.problem(
            "info",
            "Hardware transcoding is off (Settings → Transcoder → Use hardware acceleration when available): "
            "every transcode runs on the CPU",
        )
    elif flag(settings.get(HW_TRANSCODING)) is True and flag(settings.get(HW_ENCODING)) is False:
        status.problem(
            "info",
            "Hardware-accelerated video encoding is off (Settings → Transcoder): transcodes encode on the CPU",
        )
    if flag(settings.get(MEDIA_DELETION)) is False:
        status.problem(
            "info",
            "Allow media deletion is off (Settings → Library): the duplicates job can't delete versions",
        )


def software_transcodes(status: "ServiceStatus", streams: Iterable["Stream"], settings: dict[str, Any]) -> None:
    """a video transcode encoding on the CPU while hardware transcoding is on (warn)"""
    if not hardware_expected(settings):
        return
    software = [s for s in streams if s.hardware is False]
    if not software:
        return
    which = ", ".join(f"{s.title} for {s.user}" for s in software[:3])
    status.problem(
        "warn",
        f"Transcoding on the CPU although hardware transcoding is on: {which}. Plex can't use the GPU "
        "(Quick Sync, NVENC): on the Plex server check its drivers and that Plex's user can open /dev/dri "
        "(the render group), often changed by a kernel, driver or Plex update",
        "software_transcode",
    )


# ---------------------------------------------------------------------------


def check_plex(status: "ServiceStatus", base: str, token: str, max_trash: int) -> None:
    """everything above, onto the Plex card. Raises requests' errors or PlexRefused when
    Plex can't be asked"""
    settings = prefs(plex_get(base, token, "/:/prefs"))
    remote = None
    if flag(settings.get(REMOTE_ACCESS)) is True:
        try:
            remote = mapping(plex_get(base, token, "/myplex/account"))
        except requests.exceptions.RequestException, ValueError:
            # plex.tv's side of it, not worth failing the card for
            remote = None
    libraries = [
        read_library(base, token, key, kind, title)
        for key, kind, title in sections(plex_get(base, token, "/library/sections"))
    ]
    if status.version is None:
        status.version = container(plex_get(base, token, "/identity")).get("version")
    library_problems(status, libraries, max_trash)
    settings_problems(status, settings, remote)
    software_transcodes(status, status.streams, settings)
