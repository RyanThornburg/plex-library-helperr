"""__init__.py"""

import logging
import pathlib
import re
import shutil
import tomllib
from datetime import timedelta
from typing import Any

from config.models import ArrConfig, ConfigSettings, MoverRule, PlexConfig, ProfileRule

logger = logging.getLogger(__name__)


def parse_duration(value: str) -> timedelta | None:
    """parses duration or returns None on error/missing"""
    if not value:
        return None

    match = re.fullmatch(r"(\d+)([dhm])", value.strip())
    if not match:
        logger.warning("Invalid time filter. Not filtering by time")
        return None

    duration, unit = int(match.group(1)), match.group(2)
    return {
        "d": timedelta(days=duration),
        "h": timedelta(hours=duration),
        "m": timedelta(minutes=duration),
    }[unit]


def _build_arr_config(data: dict[str, Any], dry_run: bool) -> ArrConfig:
    return ArrConfig(
        baseurl=data.get("baseurl"),
        apikey=data.get("apikey"),
        dry_run=data.get("dry_run", dry_run),
        movers=[MoverRule(**g) for g in data.get("movers", [])],
        profilers=[ProfileRule(**p) for p in data.get("profilers", [])],
    )


def load_config() -> ConfigSettings:
    """load user config"""
    path = pathlib.Path(__file__).parent / "config.toml"
    if not path.exists():
        example = path.parent / "config.toml.example"
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
        radarr_config = data.get("radarr", {})
        sonarr_config = data.get("sonarr", {})
        dry_run: bool = raw_settings.get("dry_run", False)
        filter_time: timedelta | None = parse_duration(
            raw_settings.get("filter_time", None)
        )
    plex = PlexConfig(
        baseurl=plex_config.get("baseurl"), token=plex_config.get("token")
    )

    radarr = _build_arr_config(radarr_config, dry_run) if radarr_config else None
    sonarr = _build_arr_config(sonarr_config, dry_run) if sonarr_config else None

    return ConfigSettings(
        plex=plex,
        radarr=radarr,
        sonarr=sonarr,
        filter_time=filter_time,
    )
