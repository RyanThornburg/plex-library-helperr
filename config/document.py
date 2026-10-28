"""document.py - the config document the web app stores, as json, without secrets (stored encrypted on their own) and with rules in a canonical form"""

import copy
from dataclasses import fields
from datetime import timedelta
from typing import Any

from config import build_config, format_duration
from config.models import MoverRule, MovieRemoverRule, ProfileRule, RemoverRule

# (section, key) of every secret. They're kept out of the document, so history and
# diffs never hold one
SECRET_FIELDS: dict[str, tuple[str, str]] = {
    "radarr.apikey": ("radarr", "apikey"),
    "sonarr.apikey": ("sonarr", "apikey"),
    "plex.token": ("plex", "token"),
    "tautulli.apikey": ("tautulli", "apikey"),
    "seerr.apikey": ("seerr", "apikey"),
    "sabnzbd.apikey": ("sabnzbd", "apikey"),
    "bazarr.apikey": ("bazarr", "apikey"),
    "autoscan.password": ("autoscan", "password"),
}

# a new install is dry until it's turned live
DEFAULT_DOCUMENT: dict[str, Any] = {"settings": {"dry_run": True}}

ARRS = ("radarr", "sonarr")
RULE_TYPES = ("movers", "profilers", "removers")
# flags that are off unless set, left out of the document when off
OFF_FLAGS = ("include_specials", "skip_if_ever_watched")

Rule = MoverRule | ProfileRule | RemoverRule | MovieRemoverRule


def rule_class(arr_name: str, rule_type: str) -> type[Rule]:
    """radarr removers remove movies by root folder, sonarr's seasons/episodes by tag"""
    if rule_type == "removers":
        return MovieRemoverRule if arr_name == "radarr" else RemoverRule
    return {"movers": MoverRule, "profilers": ProfileRule}[rule_type]


def rule_to_dict(rule: Rule) -> dict[str, Any]:
    """a rule's fields without defaults/empties, durations as 7d etc, like config.toml"""
    data: dict[str, Any] = {}
    for f in fields(rule):
        value = getattr(rule, f.name)
        if value is None or value == [] or (f.name.endswith("_match") and value == "any"):
            continue
        if f.name in OFF_FLAGS and value is False:
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

