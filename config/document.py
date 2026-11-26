"""document.py - the config document the web app stores, as json, without secrets (stored encrypted on their own) and with rules in a canonical form"""

import copy
from dataclasses import fields
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

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
    "unraid.apikey": ("unraid", "apikey"),
    "autoscan.password": ("autoscan", "password"),
    "plex_host.password": ("plex_host", "password"),
    "assistant.apikey": ("assistant", "apikey"),
}

# where each secret is sent, (section, url key). A saved secret is only sent to the
# server it was saved for: changing the url to another one needs the secret again
# (moved_secrets()), or a changed url would deliver the saved key to whoever's there.
# assistant.apikey only goes to Anthropic
SECRET_URLS: dict[str, tuple[str, str]] = {
    "radarr.apikey": ("radarr", "baseurl"),
    "sonarr.apikey": ("sonarr", "baseurl"),
    "plex.token": ("plex", "baseurl"),
    "tautulli.apikey": ("tautulli", "url"),
    "seerr.apikey": ("seerr", "url"),
    "sabnzbd.apikey": ("sabnzbd", "url"),
    "bazarr.apikey": ("bazarr", "url"),
    "unraid.apikey": ("unraid", "url"),
    "autoscan.password": ("autoscan", "url"),
    "plex_host.password": ("plex_host", "url"),
}
DEFAULT_PORTS = {"http": 80, "https": 443}

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
    """a rule's fields without defaults/empties, durations as 7d etc, as a rule form saves them"""
    data: dict[str, Any] = {}
    for f in fields(rule):
        value = getattr(rule, f.name)
        if value is None or value == [] or (f.name.endswith("_match") and value == "any"):
            continue
        if f.name in OFF_FLAGS and value is False:
            continue
        data[f.name] = format_duration(value) if isinstance(value, timedelta) else value
    return data


def secrets_in(document: dict[str, Any]) -> list[str]:
    """the SECRET_FIELDS names a document holds, which it never should: they're kept
    encrypted, and every version of the document is kept and shown in plain text"""
    return [
        name
        for name, (section, key) in SECRET_FIELDS.items()
        if isinstance(document.get(section), dict) and key in document[section]
    ]


def url_origin(url: Any) -> tuple[str, str, int | None] | None:
    """(scheme, host, port) of a url, None when it has no scheme or host. A base path
    (/radarr) isn't part of it"""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    scheme, host = parts.scheme.casefold(), (parts.hostname or "").casefold()
    if not scheme or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, host, port or DEFAULT_PORTS.get(scheme)


def secret_url(document: dict[str, Any], name: str) -> Any:
    """the url a secret is sent to in a document, None when its section has none"""
    section, key = SECRET_URLS[name]
    values = document.get(section)
    return values.get(key) if isinstance(values, dict) else None


def moved_secrets(before: dict[str, Any], after: dict[str, Any], names: set[str]) -> list[str]:
    """the secrets of `names` that `after` would send to another server than `before`
    does (a section without a url sends nothing)"""
    moved = []
    for name in sorted(names & SECRET_URLS.keys()):
        new = url_origin(secret_url(after, name))
        if new is not None and new != url_origin(secret_url(before, name)):
            moved.append(name)
    return moved


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

