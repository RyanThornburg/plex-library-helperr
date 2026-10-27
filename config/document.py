"""document.py - the config document the web app stores: config.toml's shape, as json,
without secrets (stored encrypted on their own) and with rules in a canonical form"""

import copy
from dataclasses import fields
from datetime import timedelta
from typing import Any

from config import build_config, format_duration
from config.models import MoverRule, ProfileRule, RemoverRule

# (section, key) of every secret. They're kept out of the document, so history and
# diffs never hold one
SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "radarr.apikey": ("radarr", "apikey"),
    "sonarr.apikey": ("sonarr", "apikey"),
    "plex.token": ("plex", "token"),
    "tautulli.apikey": ("tautulli", "apikey"),
    "seerr.apikey": ("seerr", "apikey"),
    "sabnzbd.apikey": ("sabnzbd", "apikey"),
    "autoscan.password": ("autoscan", "password"),
}

# a new install is dry until it's turned live
DEFAULT_DOCUMENT: dict[str, Any] = {"settings": {"dry_run": True}}

ARRS = ("radarr", "sonarr")
RULE_TYPES: dict[str, type[MoverRule | ProfileRule | RemoverRule]] = {
    "movers": MoverRule,
    "profilers": ProfileRule,
    "removers": RemoverRule,
}


def rule_to_dict(rule: MoverRule | ProfileRule | RemoverRule) -> dict[str, Any]:
    """a rule's fields without defaults/empties, durations as 7d etc, like config.toml"""
    data: dict[str, Any] = {}
    for f in fields(rule):
        value = getattr(rule, f.name)
        if value is None or value == [] or (f.name.endswith("_match") and value == "any"):
            continue
        if f.name == "include_specials" and value is False:
            continue
        data[f.name] = format_duration(value) if isinstance(value, timedelta) else value
    return data


def strip_secrets(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """(the document without secrets, the secrets by SECRET_FIELDS name)"""
    document = copy.deepcopy(document)
    secrets: dict[str, str] = {}
    for name, (section, key) in SECRET_FIELDS.items():
        value = document.get(section, {}).pop(key, None)
        if value:
            secrets[name] = value
    return document, secrets


def with_secrets(document: dict[str, Any], secrets: dict[str, str]) -> dict[str, Any]:
    """the document with its secrets put back, for build_config. A secret's section is
    only added when the document has it, a key alone configures nothing"""
    document = copy.deepcopy(document)
    for name, (section, key) in SECRET_FIELDS.items():
        if name in secrets and section in document:
            document[section][key] = secrets[name]
    return document


def canonical(document: dict[str, Any]) -> dict[str, Any]:
    """validate a document (secrets left out) and rewrite its rules canonically.
    Raises ValueError like build_config"""
    settings = build_config(document)
    document = copy.deepcopy(document)
    for arr_name in ARRS:
        arr = getattr(settings, arr_name)
        if arr is None:
            continue
        section = document[arr_name]
        for rule_type in RULE_TYPES:
            rules = getattr(arr, rule_type)
            if rules:
                section[rule_type] = [rule_to_dict(r) for r in rules]
            else:
                section.pop(rule_type, None)
    return document


def from_toml(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """a parsed config.toml as (document, secrets, notes on what changed). Raises
    ValueError when it's invalid"""
    data = copy.deepcopy(data)
    notes: list[str] = []
    # config.toml without it was live, a document without it is dry
    data.setdefault("settings", {}).setdefault("dry_run", False)
    for arr_name in ARRS:
        settings = data.get(arr_name, {}).get("remover_settings", {})
        if "dry_run" in settings:
            # removers only delete what's approved now, by review or auto-approve
            if settings.pop("dry_run") is False:
                notes.append(
                    f"{arr_name}.remover_settings.dry_run = false was dropped: removers "
                    "plan for review, turn on auto-approve per rule to delete without it"
                )
    document, secrets = strip_secrets(data)
    build_config(with_secrets(document, secrets))
    return canonical(document), secrets, notes
