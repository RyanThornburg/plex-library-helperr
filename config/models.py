"""models.py"""

from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class ProfileRule:
    """ProfileRule class"""

    profile: str
    studios: list[str] = field(default_factory=list[str])
    networks: list[str] = field(default_factory=list[str])
    titles: list[str] = field(default_factory=list[str])
    monitored: bool | None = None

    def __post_init__(self):
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
        if self.type not in REMOVER_TYPES:
            raise ValueError(
                f"RemoverRule.type must be 'seasons' or 'episodes', got '{self.type}'"
            )
        if (
            not isinstance(self.keep, int)
            or isinstance(self.keep, bool)
            or self.keep < 1
        ):
            raise ValueError(
                f"RemoverRule.keep must be an integer >= 1, got '{self.keep}'"
            )
        if not self.tags:
            raise ValueError("RemoverRule requires at least one tag")
        if not isinstance(self.min_age, timedelta) or self.min_age <= timedelta(0):
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
        """identifies the rule outside config.toml, e.g. its auto-approve setting in the web
        app's database. Removers can't share a tag, so their tag sets are unique"""
        return ",".join(sorted({t.casefold() for t in self.tags}))

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
class RemoverSettings:
    """
    max_deletes: (int) Skip all removals if a run would delete more files than this.
    There's no dry_run: removers only delete what was approved, by reviewing a plan or
    auto-approve
    """

    max_deletes: int | None = None

    def __post_init__(self):
        if self.max_deletes is not None and (
            not isinstance(self.max_deletes, int)
            or isinstance(self.max_deletes, bool)
            or self.max_deletes < 1
        ):
            raise ValueError(
                f"remover_settings.max_deletes must be an integer >= 1, got '{self.max_deletes}'"
            )


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
        if (
            not isinstance(self.max_items, int)
            or isinstance(self.max_items, bool)
            or self.max_items < 1
        ):
            raise ValueError(
                f"plex.empty_trash.max_items must be an integer >= 1, got '{self.max_items}'"
            )
        if (
            not isinstance(self.max_deleted_items, int)
            or isinstance(self.max_deleted_items, bool)
            or self.max_deleted_items < 0
        ):
            raise ValueError(
                "plex.empty_trash.max_deleted_items must be an integer >= 0, "
                f"got '{self.max_deleted_items}'"
            )


def _positive_span(value: object, name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"web.{name} must be greater than 0")


@dataclass
class WebDefaults:
    """
    followup_delay: (timedelta) Empty trash this long after a live run moved/removed files or an upgrade
    followup_attempts: (int) Follow-ups tried in all when blocked (e.g. a scan running), 0 is off
    followup_lookback: (timedelta) Runs and upgrades older than this aren't followed up
    plan_max_age: (timedelta) A removers plan older than this can't be applied
    """

    followup_delay: timedelta = timedelta(minutes=15)
    followup_attempts: int = 3
    followup_lookback: timedelta = timedelta(days=2)
    plan_max_age: timedelta = timedelta(days=7)

    def __post_init__(self):
        _positive_span(self.followup_delay, "followup_delay")
        _positive_span(self.followup_lookback, "followup_lookback")
        _positive_span(self.plan_max_age, "plan_max_age")
        if (
            not isinstance(self.followup_attempts, int)
            or isinstance(self.followup_attempts, bool)
            or not 0 <= self.followup_attempts <= 10
        ):
            raise ValueError(
                f"web.followup_attempts must be an integer from 0 to 10, got '{self.followup_attempts}'"
            )


@dataclass
class TautulliConfig:
    """Tautulli watch history for remover watch protection"""

    url: str
    apikey: str

    def __repr__(self):
        return f"TautulliConfig(url={self.url},apikey={len(self.apikey) * '*'})"


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
    removers: list[RemoverRule] = field(default_factory=list[RemoverRule])
    remover_settings: RemoverSettings = field(default_factory=RemoverSettings)
    dry_run: bool = False

    def __repr__(self):
        fields: list[
            tuple[
                str,
                str
                | bool
                | None
                | list[MoverRule]
                | list[ProfileRule]
                | list[RemoverRule]
                | RemoverSettings,
            ]
        ] = [
            ("baseurl", self.baseurl or None),
            ("apikey", len(self.apikey) * "*" if self.apikey else None),
            ("movers", self.movers or None),
            ("profilers", self.profilers or None),
            ("removers", self.removers or None),
            ("remover_settings", self.remover_settings if self.removers else None),
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
    empty_trash: EmptyTrashSettings = field(default_factory=EmptyTrashSettings)
    web: WebDefaults = field(default_factory=WebDefaults)

    def __repr__(self):
        return f"ConfigSettings(filter_time={self.filter_time}, web={self.web}, plex={self.plex}, empty_trash={self.empty_trash}, tautulli={self.tautulli}, autoscan={self.autoscan}, sonarr={self.sonarr}, radarr={self.radarr})"
