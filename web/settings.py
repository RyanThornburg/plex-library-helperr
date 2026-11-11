"""settings.py - web app settings from environment variables"""

import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from web.secret_box import MIN_KEY_CHARS

# <team>.cloudflareaccess.com, the issuer of cloudflare access tokens
TEAM_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
# access application audience tags are 64 hex characters
AUDIENCE_RE = re.compile(r"^[0-9a-f]{64}$")
# cloudflare: Cloudflare Access signs people in (web.auth); password: the app's own
# login (web.login)
AUTH_MODES = ("cloudflare", "password")
CLOUDFLARE_VARS = ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "ALLOWED_EMAILS")


@dataclass(frozen=True)
class WebSettings:
    """
    auth_mode: (str) AUTH_MODE, cloudflare or password (AUTH_MODES)
    team_domain: (str) Cloudflare Zero Trust team domain, e.g. rattsnest.cloudflareaccess.com
        (cloudflare mode only, empty in password mode, like audience and allowed_emails)
    audience: (str) Application Audience (AUD) tag of the Access application
    allowed_emails: (frozenset[str]) Access identities allowed in, checked on top of the
        Access policy
    database_path: (pathlib.Path) SQLite database file
    allow_remover_apply: (bool) REMOVERS_ALLOW_APPLY, whether a reviewed removers plan can
        be applied from the ui. An env var so nothing in the app can turn it on
    secret_key: (str) HELPERR_SECRET_KEY, encrypts the config's api keys and tokens
    """

    team_domain: str
    audience: str
    allowed_emails: frozenset[str]
    database_path: pathlib.Path
    allow_remover_apply: bool = False
    secret_key: str = field(default="", repr=False)
    auth_mode: str = "cloudflare"

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"


def load_web_settings(env: Mapping[str, str] = os.environ) -> WebSettings:
    """read and validate settings, raising one error that lists every problem so the app
    never starts with auth half configured"""
    problems: list[str] = []

    auth_mode = auth_mode_from(env, problems)
    team_domain, audience, allowed_emails = "", "", frozenset[str]()
    if auth_mode == "cloudflare":
        team_domain, audience, allowed_emails = cloudflare_settings(env, problems)

    allow_apply = env.get("REMOVERS_ALLOW_APPLY", "").strip().lower()
    if allow_apply not in ("", "true", "false"):
        problems.append("REMOVERS_ALLOW_APPLY must be true or false")

    secret_key = secret_key_problems(env, problems)

    if problems:
        raise ValueError("Web settings invalid: " + "; ".join(problems))

    return WebSettings(
        team_domain=team_domain,
        audience=audience,
        allowed_emails=allowed_emails,
        database_path=database_path(env),
        allow_remover_apply=allow_apply == "true",
        secret_key=secret_key,
        auth_mode=auth_mode,
    )


def auth_mode_from(env: Mapping[str, str], problems: list[str]) -> str:
    """AUTH_MODE, or when it isn't set: cloudflare if any CF_ACCESS_*/ALLOWED_EMAILS is
    (installs from before password mode keep working), else password. Either way the
    mode's own settings must be complete, there's no mode without a sign-in"""
    mode = env.get("AUTH_MODE", "").strip().lower()
    if not mode:
        return "cloudflare" if any(env.get(name, "").strip() for name in CLOUDFLARE_VARS) else "password"
    if mode not in AUTH_MODES:
        problems.append("AUTH_MODE must be password or cloudflare")
    return mode


def cloudflare_settings(env: Mapping[str, str], problems: list[str]) -> tuple[str, str, frozenset[str]]:
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
    return team_domain, audience, allowed_emails


def database_path(env: Mapping[str, str] = os.environ) -> pathlib.Path:
    data_dir = env.get("DATA_DIR") or str(pathlib.Path(__file__).parent.parent)
    return pathlib.Path(data_dir) / "helperr.db"


def secret_key_problems(env: Mapping[str, str], problems: list[str]) -> str:
    """HELPERR_SECRET_KEY, adding to problems when it's missing or too short"""
    secret_key = env.get("HELPERR_SECRET_KEY", "").strip()
    if len(secret_key) < MIN_KEY_CHARS:
        problems.append(
            f"HELPERR_SECRET_KEY must be at least {MIN_KEY_CHARS} random characters "
            "(e.g. from openssl rand -hex 32), it encrypts the api keys and tokens"
        )
    return secret_key
