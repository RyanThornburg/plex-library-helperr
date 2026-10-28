"""__init__.py - builds ConfigSettings from a config document, the web app's stored config.
It's shaped like config.toml, which is only read to import it (config.document)"""

import os
import pathlib
import re
import tomllib
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from config.models import (
    ArrConfig,
    AutoscanConfig,
    BazarrConfig,
    ConfigSettings,
    EmptyTrashSettings,
    LinkConfig,
    MoverRule,
    MovieRemoverRule,
    PlexConfig,
    ProfileRule,
    RemoverRule,
    RemoverSettings,
    SabnzbdConfig,
    SeerrConfig,
    TautulliConfig,
    WebDefaults,
)

# fields only available on one arr type, keyed by the arr that does NOT support it
UNSUPPORTED_FIELDS: dict[str, str] = {"radarr": "networks", "sonarr": "studios"}


def _build[T](cls: type[T], raw: Mapping[str, Any], label: str) -> T:
    try:
        return cls(**raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label}: {e}") from e


def _validate_rules(
    rules: list[MoverRule] | list[ProfileRule], arr_name: str, rule_type: str
) -> None:
    unsupported = UNSUPPORTED_FIELDS[arr_name]
    for i, rule in enumerate(rules, start=1):
        if getattr(rule, unsupported):
            raise ValueError(
                f"[[{arr_name}.{rule_type}]] #{i}: '{unsupported}' is not supported for {arr_name}"
            )


def _build_remover(raw: Mapping[str, Any], label: str) -> RemoverRule:
    data = dict(raw)
    if "min_age" not in data:
        raise ValueError(f"{label}: 'min_age' is required")
    data["min_age"] = parse_duration(data["min_age"], f"{label} min_age")
    if data.get("skip_if_watched_within") is not None:
        data["skip_if_watched_within"] = parse_duration(
            data["skip_if_watched_within"], f"{label} skip_if_watched_within"
        )
    return _build(RemoverRule, data, label)


def _build_movie_remover(raw: Mapping[str, Any], label: str) -> MovieRemoverRule:
    data = dict(raw)
    if "min_age" not in data:
        raise ValueError(f"{label}: 'min_age' is required")
    data["min_age"] = parse_duration(data["min_age"], f"{label} min_age")
    if data.get("skip_if_watched_within") is not None:
        data["skip_if_watched_within"] = parse_duration(
            data["skip_if_watched_within"], f"{label} skip_if_watched_within"
        )
    return _build(MovieRemoverRule, data, label)


def _build_movie_removers(data: Mapping[str, Any]) -> list[MovieRemoverRule]:
    removers = [
        _build_movie_remover(r, f"[[radarr.removers]] #{i}")
        for i, r in enumerate(data.get("removers", []), start=1)
    ]
    # a root folder may only belong to one remover, like sonarr removers' tags
    seen: dict[str, int] = {}
    for i, remover in enumerate(removers, start=1):
        if remover.root in seen:
            raise ValueError(
                f"[[radarr.removers]] #{i}: path '{remover.root}' is already used by remover #{seen[remover.root]}"
            )
        seen[remover.root] = i
    return removers


def _build_removers(
    data: Mapping[str, Any], arr_name: str
) -> list[RemoverRule | MovieRemoverRule]:
    if arr_name == "radarr":
        return list(_build_movie_removers(data))
    raw_removers = data.get("removers", [])

    removers = [
        _build_remover(r, f"[[{arr_name}.removers]] #{i}")
        for i, r in enumerate(raw_removers, start=1)
    ]

    # a tag may only belong to one remover, a series matching two removers is a config error
    seen: dict[str, int] = {}
    for i, remover in enumerate(removers, start=1):
        for tag in {t.casefold() for t in remover.tags}:
            if tag in seen:
                raise ValueError(
                    f"[[{arr_name}.removers]] #{i}: tag '{tag}' is already used by remover #{seen[tag]}"
                )
            seen[tag] = i
    return list(removers)


def _build_arr_config(data: Mapping[str, Any], dry_run: bool, arr_name: str) -> ArrConfig:
    movers = [
        _build(MoverRule, g, f"[[{arr_name}.movers]] #{i}")
        for i, g in enumerate(data.get("movers", []), start=1)
    ]
    profilers = [
        _build(ProfileRule, p, f"[[{arr_name}.profilers]] #{i}")
        for i, p in enumerate(data.get("profilers", []), start=1)
    ]
    _validate_rules(movers, arr_name, "movers")
    _validate_rules(profilers, arr_name, "profilers")
    removers = _build_removers(data, arr_name)

    return ArrConfig(
        baseurl=data.get("baseurl") or None,
        apikey=data.get("apikey") or None,
        dry_run=data.get("dry_run", dry_run),
        movers=movers,
        profilers=profilers,
        removers=removers,
        remover_settings=_build(
            RemoverSettings,
            data.get("remover_settings", {}),
            f"[{arr_name}.remover_settings]",
        ),
        tag=data.get("tag") or None,
        public_url=data.get("public_url") or None,
    )


def _build_empty_trash(data: Mapping[str, Any], dry_run: bool) -> EmptyTrashSettings:
    return _build(EmptyTrashSettings, {"dry_run": dry_run, **data}, "[plex.empty_trash]")


def _build_web(data: Mapping[str, Any]) -> WebDefaults:
    values = dict(data)
    for name in ("followup_delay", "followup_lookback", "plan_max_age"):
        if name in values:
            values[name] = parse_duration(values[name], f"[web] {name}")
    return _build(WebDefaults, values, "[web]")


DURATION_UNITS: dict[str, str] = {"d": "days", "h": "hours", "m": "minutes"}


def _match_duration(value: object) -> timedelta | None:
    match = (
        re.fullmatch(r"(\d+)([dhm])", value.strip()) if isinstance(value, str) else None
    )
    if not match:
        return None
    try:
        return timedelta(**{DURATION_UNITS[match.group(2)]: int(match.group(1))})
    except OverflowError:
        return None


def parse_duration(value: object, label: str) -> timedelta:
    """a duration like 7d, 12h or 30m. Never falls back on a bad value: a typo should not
    mean delete now, or move the whole library"""
    duration = _match_duration(value)
    if duration is None:
        raise ValueError(
            f"{label}: invalid duration '{value}', expected a format like '180d', '12h' or '30m'"
        )
    return duration


def format_duration(value: timedelta) -> str:
    """a duration the way parse_duration reads it, in the largest whole unit"""
    seconds = int(value.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def config_toml_path() -> pathlib.Path:
    """config.toml, in DATA_DIR (e.g. a docker volume) or next to this package"""
    data_dir = os.environ.get("DATA_DIR")
    return (pathlib.Path(data_dir) if data_dir else pathlib.Path(__file__).parent) / (
        "config.toml"
    )


def read_toml(path: pathlib.Path) -> dict[str, Any]:
    with path.open(mode="rb") as f:
        return tomllib.load(f)


def _build_links(data: Any) -> list[LinkConfig]:
    if not isinstance(data, list):
        raise ValueError("[[links]] must be a list")
    links = []
    for i, item in enumerate(data, start=1):
        label = f"[[links]] #{i}"
        if not isinstance(item, Mapping):
            raise ValueError(f"{label}: must be a table")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"{label}: needs a name")
        urls = {key: str(item.get(key) or "").strip() for key in ("url", "public_url")}
        if not any(urls.values()):
            raise ValueError(f"{label} ({name}): needs a url or a public_url")
        for key, url in urls.items():
            if url and not re.match(r"^https?://[^\s/]+", url):
                raise ValueError(f"{label} ({name}): {key} must start with http:// or https://")
        links.append(LinkConfig(name, urls["url"] or None, urls["public_url"] or None))
    return links


# the document's sections that are tables
TABLES = ("settings", "web", "plex", "tautulli", "seerr", "sabnzbd", "bazarr", "autoscan", "radarr", "sonarr")


def build_config(data: Mapping[str, Any]) -> ConfigSettings:
    """settings from a config document, raises ValueError naming what's invalid"""
    if not isinstance(data, Mapping):
        raise ValueError("the config must be a table")
    for name in TABLES:
        if name in data and not isinstance(data[name], Mapping):
            raise ValueError(f"[{name}] must be a table, got {type(data[name]).__name__}")
    try:
        return _build_config(data)
    except (TypeError, AttributeError) as e:
        # a list or value where a table belongs, deeper than the sections
        raise ValueError(f"invalid config: {e}") from None


def _build_config(data: Mapping[str, Any]) -> ConfigSettings:
    raw_settings = data.get("settings", {})
    plex_config = data.get("plex", {})
    tautulli_config = data.get("tautulli", {})
    seerr_config = data.get("seerr", {})
    sabnzbd_config = data.get("sabnzbd", {})
    bazarr_config = data.get("bazarr", {})
    autoscan_config = data.get("autoscan", {})
    radarr_config = data.get("radarr")
    sonarr_config = data.get("sonarr")
    # dry unless it's turned off, config.document writes config.toml's default (false)
    dry_run = raw_settings.get("dry_run", True)
    if not isinstance(dry_run, bool):
        raise ValueError(f"[settings] dry_run must be true or false, got '{dry_run}'")
    raw_filter = raw_settings.get("filter_time")
    filter_time = (
        None
        if raw_filter in (None, "")
        else parse_duration(raw_filter, "[settings] filter_time")
    )

    plex = (
        PlexConfig(baseurl=plex_config["baseurl"], token=plex_config["token"])
        if plex_config.get("baseurl") and plex_config.get("token")
        else None
    )
    empty_trash = _build_empty_trash(plex_config.get("empty_trash", {}), dry_run)
    tautulli = (
        TautulliConfig(
            url=tautulli_config["url"],
            apikey=tautulli_config["apikey"],
            public_url=tautulli_config.get("public_url") or None,
        )
        if tautulli_config.get("url") and tautulli_config.get("apikey")
        else None
    )
    seerr = (
        SeerrConfig(
            url=seerr_config["url"],
            apikey=seerr_config["apikey"],
            public_url=seerr_config.get("public_url") or None,
        )
        if seerr_config.get("url") and seerr_config.get("apikey")
        else None
    )
    sabnzbd = (
        SabnzbdConfig(
            url=sabnzbd_config["url"],
            apikey=sabnzbd_config["apikey"],
            public_url=sabnzbd_config.get("public_url") or None,
        )
        if sabnzbd_config.get("url") and sabnzbd_config.get("apikey")
        else None
    )
    bazarr = (
        BazarrConfig(
            url=bazarr_config["url"],
            apikey=bazarr_config["apikey"],
            public_url=bazarr_config.get("public_url") or None,
        )
        if bazarr_config.get("url") and bazarr_config.get("apikey")
        else None
    )
    autoscan = (
        AutoscanConfig(
            url=autoscan_config["url"],
            username=autoscan_config.get("username") or None,
            password=autoscan_config.get("password") or None,
        )
        if autoscan_config.get("url")
        else None
    )

    radarr = (
        _build_arr_config(radarr_config, dry_run, "radarr")
        if radarr_config is not None
        else None
    )
    sonarr = (
        _build_arr_config(sonarr_config, dry_run, "sonarr")
        if sonarr_config is not None
        else None
    )

    return ConfigSettings(
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        filter_time=filter_time,
        autoscan=autoscan,
        tautulli=tautulli,
        seerr=seerr,
        sabnzbd=sabnzbd,
        bazarr=bazarr,
        links=_build_links(data.get("links", [])),
        empty_trash=empty_trash,
        web=_build_web(data.get("web", {})),
    )


def force_dry_run(settings: ConfigSettings) -> None:
    """turn on every dry_run, for runs the user asked to be dry. a new dry_run setting
    must be added here (tests check none is missed). Removers have none: a forced dry
    run gets no approvals, so it can't delete"""
    for arr in (settings.radarr, settings.sonarr):
        if arr:
            arr.dry_run = True
    settings.empty_trash.dry_run = True


def config_dry_run(job: str, settings: ConfigSettings) -> bool | None:
    """whether a job run as configured would be a dry run, None when the arrs disagree.
    Removers only delete approved removals, the web app reports auto-approved ones"""
    if job == "removers":
        return True
    if job == "empty_trash":
        return settings.empty_trash.dry_run
    values = {arr.dry_run for arr in (settings.radarr, settings.sonarr) if arr}
    return values.pop() if len(values) == 1 else (None if values else True)
