"""__init__.py"""

import logging
import os
import pathlib
import re
import shutil
import tomllib
from datetime import timedelta
from typing import Any

from config.models import (
    ArrConfig,
    AutoscanConfig,
    ConfigSettings,
    EmptyTrashSettings,
    MoverRule,
    PlexConfig,
    ProfileRule,
    RemoverRule,
    RemoverSettings,
    TautulliConfig,
)

logger = logging.getLogger(__name__)

# fields only available on one arr type, keyed by the arr that does NOT support it
UNSUPPORTED_FIELDS: dict[str, str] = {"radarr": "networks", "sonarr": "studios"}


def _validate_rules(
    rules: list[MoverRule] | list[ProfileRule], arr_name: str, rule_type: str
) -> None:
    unsupported = UNSUPPORTED_FIELDS[arr_name]
    for i, rule in enumerate(rules, start=1):
        if getattr(rule, unsupported):
            raise ValueError(
                f"[[{arr_name}.{rule_type}]] #{i}: '{unsupported}' is not supported for {arr_name}"
            )


def _build_remover(raw: dict[str, Any], label: str) -> RemoverRule:
    data = dict(raw)
    if "min_age" not in data:
        raise ValueError(f"{label}: 'min_age' is required")
    data["min_age"] = _parse_required_duration(data["min_age"], f"{label} min_age")
    if "skip_if_watched_within" in data:
        data["skip_if_watched_within"] = _parse_required_duration(
            data["skip_if_watched_within"], f"{label} skip_if_watched_within"
        )
    try:
        return RemoverRule(**data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label}: {e}") from e


def _build_removers(data: dict[str, Any], arr_name: str) -> list[RemoverRule]:
    raw_removers = data.get("removers", [])
    if raw_removers and arr_name != "sonarr":
        raise ValueError(f"[[{arr_name}.removers]] is only supported for sonarr")

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
    return removers


def _build_arr_config(data: dict[str, Any], dry_run: bool, arr_name: str) -> ArrConfig:
    movers = [MoverRule(**g) for g in data.get("movers", [])]
    profilers = [ProfileRule(**p) for p in data.get("profilers", [])]
    _validate_rules(movers, arr_name, "movers")
    _validate_rules(profilers, arr_name, "profilers")
    removers = _build_removers(data, arr_name)

    return ArrConfig(
        baseurl=data.get("baseurl"),
        apikey=data.get("apikey"),
        dry_run=data.get("dry_run", dry_run),
        movers=movers,
        profilers=profilers,
        removers=removers,
        remover_settings=RemoverSettings(**data.get("remover_settings", {})),
        tag=data.get("tag"),
    )


def _build_empty_trash(data: dict[str, Any], dry_run: bool) -> EmptyTrashSettings:
    try:
        return EmptyTrashSettings(**{"dry_run": dry_run, **data})
    except (TypeError, ValueError) as e:
        raise ValueError(f"[plex.empty_trash]: {e}") from e


DURATION_UNITS: dict[str, str] ={"d": "days", "h": "hours", "m": "minutes"}


def _match_duration(value: object) -> timedelta | None:
    match = (
        re.fullmatch(r"(\d+)([dhm])", value.strip()) if isinstance(value, str) else None
    )
    if not match:
        return None
    return timedelta(**{DURATION_UNITS[match.group(2)]: int(match.group(1))})


def _parse_required_duration(value: object, label: str) -> timedelta:
    """removers never fall back on a bad duration, a typo should not mean delete now"""
    duration = _match_duration(value)
    if duration is None:
        raise ValueError(
            f"{label}: invalid duration '{value}', expected a format like '180d', '12h' or '30m'"
        )
    return duration


def parse_duration(value: object) -> timedelta | None:
    """parses duration or returns None on error/missing"""
    if value is None or value == "":
        return None

    duration = _match_duration(value)
    if duration is None:
        logger.warning("Invalid time filter. Not filtering by time")
    return duration


def load_config() -> ConfigSettings:
    """load user config"""
    package_dir = pathlib.Path(__file__).parent
    # DATA_DIR keeps config out of the package dir, e.g. a docker volume
    data_dir = os.environ.get("DATA_DIR")
    path = (pathlib.Path(data_dir) if data_dir else package_dir) / "config.toml"
    if not path.exists():
        example = package_dir / "config.toml.example"
        if not example.exists():
            raise FileNotFoundError(
                f"Config file not found and no example to copy from: {example}"
            )
        shutil.copy(example, path)
        logger.info(
            "Created config.toml from config.toml.example — please update it with your settings."
        )
    with path.open(mode="rb") as f:
        data = tomllib.load(f)
        raw_settings = data.get("settings", {})
        plex_config = data.get("plex", {})
        tautulli_config = data.get("tautulli", {})
        autoscan_config = data.get("autoscan", {})
        radarr_config = data.get("radarr", {})
        sonarr_config = data.get("sonarr", {})
        dry_run: bool = raw_settings.get("dry_run", False)
        filter_time: timedelta | None = parse_duration(
            raw_settings.get("filter_time", None)
        )
    plex = (
        PlexConfig(baseurl=plex_config["baseurl"], token=plex_config["token"])
        if plex_config.get("baseurl") and plex_config.get("token")
        else None
    )
    empty_trash = _build_empty_trash(plex_config.get("empty_trash", {}), dry_run)
    tautulli = (
        TautulliConfig(url=tautulli_config["url"], apikey=tautulli_config["apikey"])
        if tautulli_config.get("url") and tautulli_config.get("apikey")
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
        _build_arr_config(radarr_config, dry_run, "radarr") if radarr_config else None
    )
    sonarr = (
        _build_arr_config(sonarr_config, dry_run, "sonarr") if sonarr_config else None
    )

    return ConfigSettings(
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        filter_time=filter_time,
        autoscan=autoscan,
        tautulli=tautulli,
        empty_trash=empty_trash,
    )


def force_dry_run(settings: ConfigSettings) -> None:
    """turn on every dry_run, for runs the user asked to be dry. a new dry_run setting
    must be added here (tests check none is missed)"""
    for arr in (settings.radarr, settings.sonarr):
        if arr:
            arr.dry_run = True
            arr.remover_settings.dry_run = True
    settings.empty_trash.dry_run = True


def config_dry_run(job: str, settings: ConfigSettings) -> bool | None:
    """whether a job run as configured would be a dry run, None when the arrs disagree"""
    if job == "removers":
        return settings.sonarr.remover_settings.dry_run if settings.sonarr else True
    if job == "empty_trash":
        return settings.empty_trash.dry_run
    values = {arr.dry_run for arr in (settings.radarr, settings.sonarr) if arr}
    return values.pop() if len(values) == 1 else (None if values else True)
