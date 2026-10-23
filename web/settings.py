"""settings.py - web app settings from environment variables"""

import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

# <team>.cloudflareaccess.com, the issuer of cloudflare access tokens
TEAM_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
# access application audience tags are 64 hex characters
AUDIENCE_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WebSettings:
    """
    team_domain: (str) Cloudflare Zero Trust team domain, e.g. rattsnest.cloudflareaccess.com
    audience: (str) Application Audience (AUD) tag of the Access application
    allowed_emails: (frozenset[str]) Access identities allowed in, checked on top of the
        Access policy
    database_path: (pathlib.Path) SQLite database file
    allow_remover_apply: (bool) REMOVERS_ALLOW_APPLY, whether a reviewed removers plan can
        be applied from the ui. An env var so nothing in the app can turn it on
    """

    team_domain: str
    audience: str
    allowed_emails: frozenset[str]
    database_path: pathlib.Path
    allow_remover_apply: bool = False

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"


def load_web_settings(env: Mapping[str, str] = os.environ) -> WebSettings:
    """read and validate settings, raising one error that lists every problem so the app
    never starts with auth half configured"""
    problems: list[str] = []

    team_domain = env.get("CF_ACCESS_TEAM_DOMAIN", "").strip().lower()
    team_domain = team_domain.removeprefix("https://").rstrip("/")
    if not TEAM_DOMAIN_RE.match(team_domain):
        problems.append(
            "CF_ACCESS_TEAM_DOMAIN must be <team>.cloudflareaccess.com"
        )

    audience = env.get("CF_ACCESS_AUD", "").strip().lower()
    if not AUDIENCE_RE.match(audience):
        problems.append(
            "CF_ACCESS_AUD must be the Access application's 64 character AUD tag"
        )

    allowed_emails = frozenset(
        e.strip().casefold() for e in env.get("ALLOWED_EMAILS", "").split(",") if e.strip()
    )
    if not allowed_emails:
        problems.append("ALLOWED_EMAILS must list at least one email")

    allow_apply = env.get("REMOVERS_ALLOW_APPLY", "").strip().lower()
    if allow_apply not in ("", "true", "false"):
        problems.append("REMOVERS_ALLOW_APPLY must be true or false")

    if problems:
        raise ValueError("Web settings invalid: " + "; ".join(problems))

    data_dir = env.get("DATA_DIR") or str(pathlib.Path(__file__).parent.parent)
    return WebSettings(
        team_domain=team_domain,
        audience=audience,
        allowed_emails=allowed_emails,
        database_path=pathlib.Path(data_dir) / "helperr.db",
        allow_remover_apply=allow_apply == "true",
    )
