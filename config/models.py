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
class ArrConfig:
    """shared arr config"""

    baseurl: str | None
    apikey: str | None
    tag: str | None
    movers: list[MoverRule] = field(default_factory=list[MoverRule])
    profilers: list[ProfileRule] = field(default_factory=list[ProfileRule])
    dry_run: bool = False

    def __repr__(self):
        fields: list[
            tuple[str, str | bool | None | list[MoverRule] | list[ProfileRule]]
        ] = [
            ("baseurl", self.baseurl or None),
            ("apikey", len(self.apikey) * "*" if self.apikey else None),
            ("movers", self.movers or None),
            ("profilers", self.profilers or None),
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

    def __repr__(self):
        return f"ConfigSettings(filter_time={self.filter_time}, plex={self.plex}, sonarr={self.sonarr}, radarr={self.radarr})"
