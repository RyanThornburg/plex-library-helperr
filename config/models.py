"""models.py - the settings a config document builds"""

from dataclasses import dataclass, field, fields
from datetime import timedelta
from types import NoneType, UnionType
from typing import Any, get_args, get_origin


def _is_type(value: Any, expected: Any) -> bool:
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is NoneType:
        return value is None
    origin = get_origin(expected)
    if origin is UnionType:
        return any(_is_type(value, option) for option in get_args(expected))
    if origin is list:
        (item,) = get_args(expected)
        return isinstance(value, list) and all(_is_type(v, item) for v in value)
    if expected in (str, timedelta):
        return isinstance(value, expected)
    # nested settings (rules, RemoverSettings) are built by the loader, not read from json
    return True


def check_types(settings: Any) -> None:
    """ValueError naming the first field that isn't its declared type. The config is json:
    a string where a list belongs would be read letter by letter ("keep" excluding the
    tags k, e and p), and 0 for a dry_run would mean live"""
    for f in fields(settings):
        value = getattr(settings, f.name)
        if not _is_type(value, f.type):
            raise ValueError(f"{f.name} must be {_type_name(f.type)}, got {value!r}")


def _type_name(expected: Any) -> str:
    if get_origin(expected) is UnionType:
        return " or ".join(_type_name(option) for option in get_args(expected))
    if get_origin(expected) is list:
        return f"a list of {_type_name(get_args(expected)[0])}"
    names = {bool: "true or false", int: "a whole number", str: "text", NoneType: "empty", timedelta: "a duration"}
    return names.get(expected, getattr(expected, "__name__", str(expected)))


@dataclass
class ProfileRule:
    """ProfileRule class"""

    profile: str
    studios: list[str] = field(default_factory=list[str])
    networks: list[str] = field(default_factory=list[str])
    titles: list[str] = field(default_factory=list[str])
    monitored: bool | None = None

    def __post_init__(self):
        check_types(self)
        if not any((self.studios, self.networks, self.titles)):
            raise ValueError(
                "ProfileRule requires at least one of [Titles, Networks, Studios]"
            )

    def __repr__(self):
        fields: list[tuple[str, str | None | list[str] | bool]] = [
            ("profile", self.profile),
            ("studios", self.studios or None),
            ("networks", self.networks or None),
            ("titles", self.titles or None),
            ("monitored", self.monitored),
        ]

        return (
            f"ProfileRule({','.join(f'{k}={v}' for k, v in fields if v is not None)})"
        )


@dataclass
class MoverRule:
    """
    plex_library: (str) plex library to refresh after moving
    path: (str) Media path to move matches to
    exclude_paths: (list[str]) Skip matches found at these locations

    certifications: (list[str]) Match based on certification/ratings
    exclude_certifications: (list[str]) Skip these certifications

    genres: (list[str]) Genre to match on
    genre_match: (str) "any" genre must match or "all" genres must match
    exclude_genres: (list[str]) Skip if any of these genres found

    studios: (list[str]) Radarr only - match on studio
    networks: (list[str]) Sonarr only - match on network

    tags: (list[str]) Tag to match on
    tag_match: (str) "any" tag must match or "all" tags must match
    exclude_tags: (list[str]) Skip if tag is found
    """

    path: str
    plex_library: str | None = None
    exclude_paths: list[str] = field(default_factory=list[str])

    certifications: list[str] = field(default_factory=list[str])
    exclude_certifications: list[str] = field(default_factory=list[str])

    genres: list[str] = field(default_factory=list[str])
    genre_match: str = "any"
    exclude_genres: list[str] = field(default_factory=list[str])

    studios: list[str] = field(default_factory=list[str])
    networks: list[str] = field(default_factory=list[str])

    tags: list[str] = field(default_factory=list[str])
    tag_match: str = "any"
    exclude_tags: list[str] = field(default_factory=list[str])

    def __post_init__(self):
        check_types(self)
        if self.genres and self.genre_match not in ("any", "all"):
            raise ValueError(
                f"MoverRule.genre_match must be 'any' or 'all', got '{self.genre_match}'"
            )
        if self.tags and self.tag_match not in ("any", "all"):
            raise ValueError(
                f"MoverRule.tag_match must be 'any' or 'all', got '{self.tag_match}'"
            )

        if not any(
            (self.tags, self.networks, self.studios, self.certifications, self.genres)
        ):
            raise ValueError(
                "MoverRule requires at least one of [Tags, Networks, Studios, Certifications, Genres]"
            )

    def __repr__(self):
        fields: list[tuple[str, str | None | list[str]]] = [
            ("plex_library", self.plex_library or None),
            ("path", self.path),
            ("exclude_paths", self.exclude_paths or None),
            ("certifications", self.certifications or None),
            ("exclude_certifications", self.exclude_certifications or None),
            ("genres", self.genres if self.genres else None),
            ("genre_match", self.genre_match if self.genres else None),
            ("exclude_genres", self.exclude_genres or None),
            ("tags", self.tags or None),
            ("tag_match", self.tag_match if self.tags else None),
            ("exclude_tags", self.exclude_tags or None),
            ("studios", self.studios or None),
            ("networks", self.networks or None),
        ]

        return f"MoverRule({','.join(f'{k}={v}' for k, v in fields if v is not None)})"


REMOVER_TYPES = ("seasons", "episodes")


@dataclass
class RemoverRule:
    """
    type: (str) "seasons" removes whole seasons, "episodes" removes episode files
    keep: (int) Number of latest seasons/episodes (with files) to always keep
    tags: (list[str]) Series with any of these tags are checked by this rule
    min_age: (timedelta) Every file in a season (or the episode file) must be older than this
    exclude_tags: (list[str]) Skip series with any of these tags
    include_specials: (bool) Specials (season 0) are eligible for removal, never count toward keep
    skip_if_watched_within: (timedelta) Protect content from the earliest recently watched
        removal candidate onward
    plex_library: (str) Plex library to refresh after removing (if autoscan is not setup)
    """

    type: str
    keep: int
    tags: list[str]
    min_age: timedelta
    exclude_tags: list[str] = field(default_factory=list[str])
    include_specials: bool = False
    skip_if_watched_within: timedelta | None = None
    plex_library: str | None = None

    def __post_init__(self):
        check_types(self)
        if self.type not in REMOVER_TYPES:
            raise ValueError(
                f"RemoverRule.type must be 'seasons' or 'episodes', got '{self.type}'"
            )
        if self.keep < 1:
            raise ValueError(
                f"RemoverRule.keep must be an integer >= 1, got '{self.keep}'"
            )
        if not self.tags:
            raise ValueError("RemoverRule requires at least one tag")
        if self.min_age <= timedelta(0):
            raise ValueError("RemoverRule.min_age must be greater than 0")
        if (
            self.skip_if_watched_within is not None
            and self.skip_if_watched_within <= timedelta(0)
        ):
            raise ValueError(
                "RemoverRule.skip_if_watched_within must be greater than 0"
            )

    @property
    def key(self) -> str:
        """identifies the rule outside the config document, e.g. its auto-approve setting in the web
        app's database. Removers can't share a tag, so their tag sets are unique"""
        return ",".join(sorted({t.casefold() for t in self.tags}))

    @property
    def label(self) -> str:
        """how pages name the rule"""
        return ", ".join(self.tags)

    def __repr__(self):
        fields: list[tuple[str, str | int | bool | None | list[str] | timedelta]] = [
            ("type", self.type),
            ("keep", self.keep),
            ("tags", self.tags),
            ("exclude_tags", self.exclude_tags or None),
            ("min_age", self.min_age),
            ("include_specials", self.include_specials or None),
            ("skip_if_watched_within", self.skip_if_watched_within),
            ("plex_library", self.plex_library or None),
        ]
        return (
            f"RemoverRule({','.join(f'{k}={v}' for k, v in fields if v is not None)})"
        )


@dataclass
class MovieRemoverRule:
    """Radarr removers: delete old movies (and their files) from a root folder
    path: (str) Radarr root folder the rule checks, e.g. /data/media/movies/movies-hallmark
    min_age: (timedelta) Added to Radarr and released longer ago than this
    exclude_tags: (list[str]) Skip movies with any of these tags
    skip_if_ever_watched: (bool) Keep movies anyone has ever played
    skip_if_watched_within: (timedelta) Keep movies played within this
    plex_library: (str) Plex library to refresh after removing (if autoscan is not setup)
    """

    path: str
    min_age: timedelta
    exclude_tags: list[str] = field(default_factory=list[str])
    skip_if_ever_watched: bool = False
    skip_if_watched_within: timedelta | None = None
    plex_library: str | None = None

    def __post_init__(self):
        check_types(self)
        if not self.path.strip("/ "):
            raise ValueError("MovieRemoverRule requires a root folder path")
        if self.min_age <= timedelta(0):
            raise ValueError("MovieRemoverRule.min_age must be greater than 0")
        if (
            self.skip_if_watched_within is not None
            and self.skip_if_watched_within <= timedelta(0)
        ):
            raise ValueError(
                "MovieRemoverRule.skip_if_watched_within must be greater than 0"
            )
        if self.skip_if_ever_watched and self.skip_if_watched_within is not None:
            raise ValueError(
                "MovieRemoverRule: use skip_if_ever_watched or skip_if_watched_within, not both"
            )

    @property
    def root(self) -> str:
        return self.path.rstrip("/")

    @property
    def watch_check(self) -> bool:
        return self.skip_if_ever_watched or self.skip_if_watched_within is not None

    @property
    def key(self) -> str:
        """like RemoverRule.key. Removers can't share a root folder, and sonarr tags
        can't hold a ':', so this never matches a sonarr remover's key"""
        return f"radarr:{self.root}"

    @property
    def label(self) -> str:
        """how pages name the rule"""
        return self.root

    def __repr__(self):
        fields: list[tuple[str, str | bool | None | list[str] | timedelta]] = [
            ("path", self.path),
            ("min_age", self.min_age),
            ("exclude_tags", self.exclude_tags or None),
            ("skip_if_ever_watched", self.skip_if_ever_watched or None),
            ("skip_if_watched_within", self.skip_if_watched_within),
            ("plex_library", self.plex_library or None),
        ]
        return (
            f"MovieRemoverRule({','.join(f'{k}={v}' for k, v in fields if v is not None)})"
        )


@dataclass
class RemoverSettings:
    """
    max_deletes: (int) Skip all removals if a run would delete more files than this.
    There's no dry_run: removers only delete what was approved, by reviewing a plan or
    auto-approve
    """

    max_deletes: int | None = None

    def __post_init__(self):
        check_types(self)
        if self.max_deletes is not None and self.max_deletes < 1:
            raise ValueError(
                f"remover_settings.max_deletes must be an integer >= 1, got '{self.max_deletes}'"
            )


# which stuck downloads queue cleanup may remove, each the arrs' trackedDownloadStates
# it covers. A record that's only flagged warning/error (a download client hiccup can
# flag the whole queue) is never one of them
QUEUE_STATES: dict[str, tuple[str, ...]] = {
    "failed": ("failed", "failedPending"),
    "import_blocked": ("importBlocked",),
    "import_pending": ("importPending",),
}
# like the arrs' Remove: remove from the download client too, or the queue only
QUEUE_METHODS = ("client", "ignore")
# don't blocklist, blocklist and search for a replacement, blocklist only
QUEUE_BLOCKLIST = ("none", "search", "only")
# shorter than this and a download the arr is about to sort out itself would be removed
QUEUE_MIN_STUCK_FOR = timedelta(hours=1)


@dataclass
class QueueCleanup:
    """
    [radarr.queue_cleanup] / [sonarr.queue_cleanup]: remove downloads stuck in the
    queue, after the health checks have seen them stuck for stuck_for.
    enabled: (bool) Off unless turned on
    stuck_for: (timedelta) How long a download has to be stuck, at least an hour
    states: (list[str]) QUEUE_STATES keys it may remove
    method: (str) QUEUE_METHODS: remove from the download client too, or the queue only
    blocklist: (str) QUEUE_BLOCKLIST
    max_per_run: (int) Remove none when more than this are due at once (a missing
        mount blocks every import), 0 = no limit
    dry_run: (bool) Starts on, whatever [settings].dry_run says
    """

    enabled: bool = False
    stuck_for: timedelta = timedelta(hours=6)
    states: list[str] = field(default_factory=lambda: ["failed", "import_blocked"])
    method: str = "client"
    blocklist: str = "search"
    max_per_run: int = 10
    dry_run: bool = True

    def __post_init__(self):
        check_types(self)
        if self.stuck_for < QUEUE_MIN_STUCK_FOR:
            raise ValueError("stuck_for must be at least 1h")
        if not self.states:
            raise ValueError("states needs at least one of " + ", ".join(QUEUE_STATES))
        if unknown := [s for s in self.states if s not in QUEUE_STATES]:
            raise ValueError(f"unknown states {unknown}, expected {', '.join(QUEUE_STATES)}")
        if self.method not in QUEUE_METHODS:
            raise ValueError(f"method must be one of {', '.join(QUEUE_METHODS)}, got '{self.method}'")
        if self.blocklist not in QUEUE_BLOCKLIST:
            raise ValueError(
                f"blocklist must be one of {', '.join(QUEUE_BLOCKLIST)}, got '{self.blocklist}'"
            )
        if self.max_per_run < 0:
            raise ValueError(f"max_per_run must be an integer >= 0, got '{self.max_per_run}'")

    def tracked_states(self) -> set[str]:
        """the arrs' trackedDownloadStates it removes"""
        return {state for key in self.states for state in QUEUE_STATES[key]}


@dataclass
class EmptyTrashSettings:
    """
    libraries: (list[str]) Plex libraries to empty the trash of, empty means every movie/show library
    max_items: (int) Skip a library if its trash holds more items than this (a missing mount trashes everything)
    max_deleted_items: (int) Skip a library if more items than this are fully deleted (no
        version left) and weren't removed by the web app's removers
    dry_run: (bool) Defaults to the [settings] dry_run
    """

    libraries: list[str] = field(default_factory=list[str])
    max_items: int = 100
    max_deleted_items: int = 50
    dry_run: bool = False

    def __post_init__(self):
        check_types(self)
        if self.max_items < 1:
            raise ValueError(
                f"plex.empty_trash.max_items must be an integer >= 1, got '{self.max_items}'"
            )
        if self.max_deleted_items < 0:
            raise ValueError(
                "plex.empty_trash.max_deleted_items must be an integer >= 0, "
                f"got '{self.max_deleted_items}'"
            )


def _positive_span(value: timedelta, name: str) -> None:
    if value <= timedelta(0):
        raise ValueError(f"web.{name} must be greater than 0")


@dataclass
class WebDefaults:
    """
    followup_delay: (timedelta) Empty trash this long after a live run moved/removed files or an upgrade
    followup_attempts: (int) Follow-ups tried in all when blocked (e.g. a scan running), 0 is off
    followup_lookback: (timedelta) Runs and upgrades older than this aren't followed up
    plan_max_age: (timedelta) A removers plan older than this can't be applied
    min_free_gb: (int) Radarr/Sonarr root folders and SABnzbd's folders with less free space (GB) are a problem, 0 is off
    """

    followup_delay: timedelta = timedelta(minutes=15)
    followup_attempts: int = 3
    followup_lookback: timedelta = timedelta(days=2)
    plan_max_age: timedelta = timedelta(days=7)
    min_free_gb: int = 100

    def __post_init__(self):
        check_types(self)
        _positive_span(self.followup_delay, "followup_delay")
        _positive_span(self.followup_lookback, "followup_lookback")
        _positive_span(self.plan_max_age, "plan_max_age")
        if not 0 <= self.followup_attempts <= 10:
            raise ValueError(
                f"web.followup_attempts must be an integer from 0 to 10, got '{self.followup_attempts}'"
            )
        if self.min_free_gb < 0:
            raise ValueError(f"web.min_free_gb must be a whole number of GB, 0 or more, got '{self.min_free_gb}'")


@dataclass
class TautulliConfig:
    """Tautulli watch history for remover watch protection"""

    url: str
    apikey: str
    # where the status page links to away from home, url is the local one
    public_url: str | None = None

    def __repr__(self):
        public = f",public_url={self.public_url}" if self.public_url else ""
        return f"TautulliConfig(url={self.url}{public},apikey={len(self.apikey) * '*'})"


@dataclass
class SeerrConfig:
    """Seerr (Overseerr) requests, for request stats"""

    url: str
    apikey: str
    # where the status page links to away from home, url is the local one
    public_url: str | None = None

    def __repr__(self):
        public = f",public_url={self.public_url}" if self.public_url else ""
        return f"SeerrConfig(url={self.url}{public},apikey={len(self.apikey) * '*'})"


@dataclass
class SabnzbdConfig:
    """SABnzbd's queue and history, for the status page"""

    url: str
    apikey: str
    # where the status page links to away from home, url is the local one
    public_url: str | None = None

    def __repr__(self):
        public = f",public_url={self.public_url}" if self.public_url else ""
        return f"SabnzbdConfig(url={self.url}{public},apikey={len(self.apikey) * '*'})"


@dataclass
class BazarrConfig:
    """Bazarr's health, providers and wanted subtitles, for the status page"""

    # with its base url when it has one, e.g. http://192.168.1.15:6767/subs
    url: str
    apikey: str
    # where the status page links to away from home, url is the local one
    public_url: str | None = None

    def __repr__(self):
        public = f",public_url={self.public_url}" if self.public_url else ""
        return f"BazarrConfig(url={self.url}{public},apikey={len(self.apikey) * '*'})"


@dataclass
class UnraidConfig:
    """Unraid's array, disks, parity checks, containers and notifications, for the status
    page, through its GraphQL api with a read-only (Viewer) key"""

    # the server's web ui, e.g. http://192.168.1.15 (the api is its /graphql)
    url: str
    apikey: str
    # where the status page links to away from home, url is the local one
    public_url: str | None = None

    def __repr__(self):
        public = f",public_url={self.public_url}" if self.public_url else ""
        return f"UnraidConfig(url={self.url}{public},apikey={len(self.apikey) * '*'})"


# the status page's services a link can sit next to
LINK_SERVICES = {
    "unraid": "Unraid", "plex": "Plex", "radarr": "Radarr", "sonarr": "Sonarr",
    "bazarr": "Bazarr", "sabnzbd": "SABnzbd", "seerr": "Seerr",
}


@dataclass
class LinkConfig:
    """another tool the status page links to, e.g. Tracearr"""

    name: str
    url: str | None = None
    public_url: str | None = None
    # shown next to this service's own link (LINK_SERVICES), else under the services
    service: str | None = None


@dataclass
class PlexConfig:
    """Plex class for refreshing library"""

    baseurl: str | None
    token: str | None

    def __repr__(self):
        fields: list[tuple[str, str | None]] = [
            ("baseurl", self.baseurl or None),
            ("token", len(self.token) * "*" if self.token else None),
        ]
        return f"PlexConfig({','.join(f'{k}={v}' for k, v in fields if v is not None)})"


@dataclass
class AutoscanConfig:
    """Autoscan manual trigger for scanning moved folders"""

    url: str
    username: str | None = None
    password: str | None = None

    def __repr__(self):
        fields: list[tuple[str, str | None]] = [
            ("url", self.url),
            ("username", self.username or None),
            ("password", len(self.password) * "*" if self.password else None),
        ]
        return f"AutoscanConfig({','.join(f'{k}={v}' for k, v in fields if v is not None)})"


@dataclass
class ArrConfig:
    """shared arr config"""

    baseurl: str | None
    apikey: str | None
    tag: str | None
    movers: list[MoverRule] = field(default_factory=list[MoverRule])
    profilers: list[ProfileRule] = field(default_factory=list[ProfileRule])
    # sonarr's are RemoverRules, radarr's MovieRemoverRules
    removers: list[RemoverRule | MovieRemoverRule] = field(
        default_factory=list[RemoverRule | MovieRemoverRule]
    )
    remover_settings: RemoverSettings = field(default_factory=RemoverSettings)
    queue_cleanup: QueueCleanup = field(default_factory=QueueCleanup)
    dry_run: bool = False
    # where the status page links to away from home, baseurl is the local one
    public_url: str | None = None

    def __post_init__(self):
        check_types(self)

    def __repr__(self):
        fields: list[
            tuple[
                str,
                str
                | bool
                | None
                | list[MoverRule]
                | list[ProfileRule]
                | list[RemoverRule | MovieRemoverRule]
                | RemoverSettings
                | QueueCleanup,
            ]
        ] = [
            ("baseurl", self.baseurl or None),
            ("public_url", self.public_url or None),
            ("apikey", len(self.apikey) * "*" if self.apikey else None),
            ("movers", self.movers or None),
            ("profilers", self.profilers or None),
            ("removers", self.removers or None),
            ("remover_settings", self.remover_settings if self.removers else None),
            ("queue_cleanup", self.queue_cleanup if self.queue_cleanup.enabled else None),
            ("dry_run", self.dry_run),
            ("tag", self.tag or None),
        ]
        return f"ArrConfig({','.join(f'{k}={v}' for k, v in fields if v is not None)})"


@dataclass
class ConfigSettings:
    """ConfigSettings"""

    plex: PlexConfig | None
    radarr: ArrConfig | None
    sonarr: ArrConfig | None
    filter_time: timedelta | None = None
    autoscan: AutoscanConfig | None = None
    tautulli: TautulliConfig | None = None
    seerr: SeerrConfig | None = None
    sabnzbd: SabnzbdConfig | None = None
    bazarr: BazarrConfig | None = None
    unraid: UnraidConfig | None = None
    links: list[LinkConfig] = field(default_factory=list[LinkConfig])
    empty_trash: EmptyTrashSettings = field(default_factory=EmptyTrashSettings)
    web: WebDefaults = field(default_factory=WebDefaults)

    def __repr__(self):
        return f"ConfigSettings(filter_time={self.filter_time}, web={self.web}, plex={self.plex}, empty_trash={self.empty_trash}, tautulli={self.tautulli}, seerr={self.seerr}, sabnzbd={self.sabnzbd}, bazarr={self.bazarr}, unraid={self.unraid}, links={self.links}, autoscan={self.autoscan}, sonarr={self.sonarr}, radarr={self.radarr})"
