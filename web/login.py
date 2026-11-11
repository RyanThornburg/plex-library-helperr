"""login.py - the app's own sign-in, for AUTH_MODE=password (web.settings).

One admin account, like the *arrs: a username and a scrypt hash of its password, kept in
app_settings. Signing in makes a LoginSession; the browser's cookie holds a random token
and the table only its sha256. Until a password is set the app wants a setup token,
written to the log at startup, so whoever can reach the page first can't claim it:
only someone who can read the server's logs. `main.py --reset-login`, run in the
container, is the way back in after a forgotten password: it deletes the password and
every session and prints a new setup token. Nothing on the web can reset it."""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from functools import cache
from urllib.parse import urlsplit

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from results import utc_now
from web.db import AppSetting, LoginSession

logger = logging.getLogger(__name__)

SESSION_COOKIE = "helperr_session"
# a session ends after this long unused, and this long after signing in whatever happens
SESSION_IDLE = timedelta(days=30)
SESSION_MAX = timedelta(days=90)
# last_seen_at is written at most this often, not on every request
SESSION_TOUCH = timedelta(hours=1)

USERNAME_SETTING = "login_username"
PASSWORD_SETTING = "login_password"
SETUP_TOKEN_SETTING = "login_setup_token"

MIN_PASSWORD = 12
MAX_PASSWORD = 1024
MAX_USERNAME = 64

# scrypt with n=2**15, r=8 needs 32 MiB; about 0.1 s per hash
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

# failed sign-ins (or setup tokens) from one address before it has to wait
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)


# ---------------------------------------------------------------------------
# passwords
# ---------------------------------------------------------------------------


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32
    )


def hash_password(password: str) -> str:
    """scrypt$<log2 n>$<r>$<p>$<salt>$<hash>, base64"""
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    b64 = base64.b64encode
    return f"scrypt${SCRYPT_N.bit_length() - 1}${SCRYPT_R}${SCRYPT_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    """False for a wrong password or a hash it can't read"""
    try:
        kind, log_n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), 2 ** int(log_n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


@cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_hex(16))


def password_problems(password: str, confirm: str) -> list[str]:
    problems = []
    if len(password) < MIN_PASSWORD:
        problems.append(f"The password needs at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        problems.append(f"The password can't be longer than {MAX_PASSWORD} characters.")
    if password != confirm:
        problems.append("The passwords don't match.")
    return problems


def clean_username(username: str) -> str:
    return unicodedata.normalize("NFC", username.strip())


def username_problems(username: str) -> list[str]:
    if not username:
        return ["Enter a username."]
    if len(username) > MAX_USERNAME:
        return [f"The username can't be longer than {MAX_USERNAME} characters."]
    if any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in username):
        return ["The username can't contain control characters or line breaks."]
    return []


# ---------------------------------------------------------------------------
# the account and the setup token, in app_settings
# ---------------------------------------------------------------------------


def _get(session: Session, key: str) -> str | None:
    setting = session.get(AppSetting, key)
    return setting.value if setting else None


def _set(session: Session, key: str, value: str) -> None:
    setting = session.get(AppSetting, key) or AppSetting(key=key)
    setting.value = value
    setting.updated_at = utc_now()
    session.add(setting)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def login_username(engine: Engine) -> str | None:
    """the account's username, None until a password is set"""
    with Session(engine) as session:
        if _get(session, PASSWORD_SETTING) is None:
            return None
        return _get(session, USERNAME_SETTING)


def is_set_up(engine: Engine) -> bool:
    return login_username(engine) is not None


def new_setup_token(engine: Engine) -> str:
    """a new setup token, replacing any earlier one. Only its hash is stored"""
    token = secrets.token_urlsafe(18)
    with Session(engine) as session:
        _set(session, SETUP_TOKEN_SETTING, _sha256(token))
        session.commit()
    return token


def setup_token_matches(engine: Engine, token: str) -> bool:
    with Session(engine) as session:
        stored = _get(session, SETUP_TOKEN_SETTING)
    return bool(stored and token) and hmac.compare_digest(_sha256(token.strip()), stored)


def announce_setup_token(engine: Engine) -> None:
    """at startup with no password yet: a new setup token, in the log. Only someone who
    can read the server's logs can set the password"""
    if is_set_up(engine):
        return
    token = new_setup_token(engine)
    logger.warning(
        "WEB\t No password is set yet. Open the app and enter this setup token to create "
        "the login: %s",
        token,
    )


def set_login(engine: Engine, username: str, password: str) -> None:
    """sets the account, uses up the setup token and signs every browser out"""
    password_hash = hash_password(password)
    with Session(engine) as session:
        _set(session, USERNAME_SETTING, username)
        _set(session, PASSWORD_SETTING, password_hash)
        session.execute(delete(AppSetting).where(AppSetting.key == SETUP_TOKEN_SETTING))
        session.execute(delete(LoginSession))
        session.commit()


def check_login(engine: Engine, username: str, password: str) -> bool:
    """the same work for a wrong username as a wrong password, so timing doesn't say
    which one was wrong"""
    with Session(engine) as session:
        stored_user = _get(session, USERNAME_SETTING)
        stored_hash = _get(session, PASSWORD_SETTING)
    if stored_hash is None or stored_user is None:
        verify_password(password, _dummy_hash())
        return False
    user_ok = hmac.compare_digest(
        clean_username(username).casefold().encode("utf-8"), stored_user.casefold().encode("utf-8")
    )
    password_ok = verify_password(password, stored_hash)
    return user_ok and password_ok


def change_password(engine: Engine, password: str, keep_token: str | None) -> None:
    """a new password, every other session signed out"""
    password_hash = hash_password(password)
    with Session(engine) as session:
        _set(session, PASSWORD_SETTING, password_hash)
        _end_sessions(session, keep_token)
        session.commit()


def reset_login(engine: Engine) -> str:
    """forgets the password, signs everyone out, returns a new setup token"""
    with Session(engine) as session:
        session.execute(
            delete(AppSetting).where(AppSetting.key.in_((USERNAME_SETTING, PASSWORD_SETTING)))
        )
        session.execute(delete(LoginSession))
        session.commit()
    return new_setup_token(engine)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def create_session(engine: Engine, username: str, now: datetime | None = None) -> str:
    """a new session, its token for the cookie. Old expired sessions are deleted"""
    now = now or utc_now()
    token = secrets.token_urlsafe(32)
    with Session(engine) as session:
        session.execute(
            delete(LoginSession).where(
                (LoginSession.last_seen_at < now - SESSION_IDLE)
                | (LoginSession.created_at < now - SESSION_MAX)
            )
        )
        session.add(LoginSession(id=_sha256(token), username=username, created_at=now, last_seen_at=now))
        session.commit()
    return token


def session_user(engine: Engine, token: str, now: datetime | None = None) -> str | None:
    """the signed in username, None when the session doesn't exist, ended, or belongs to
    a login that has since changed"""
    if not token:
        return None
    now = now or utc_now()
    with Session(engine) as session:
        row = session.get(LoginSession, _sha256(token))
        if row is None:
            return None
        if row.last_seen_at < now - SESSION_IDLE or row.created_at < now - SESSION_MAX:
            session.delete(row)
            session.commit()
            return None
        if _get(session, PASSWORD_SETTING) is None or _get(session, USERNAME_SETTING) != row.username:
            return None
        if now - row.last_seen_at > SESSION_TOUCH:
            row.last_seen_at = now
            session.commit()
        return row.username


def end_session(engine: Engine, token: str) -> None:
    with Session(engine) as session:
        session.execute(delete(LoginSession).where(LoginSession.id == _sha256(token)))
        session.commit()


def _end_sessions(session: Session, keep_token: str | None) -> int:
    query = delete(LoginSession)
    if keep_token:
        query = query.where(LoginSession.id != _sha256(keep_token))
    return session.execute(query).rowcount  # type: ignore[attr-defined]


def end_other_sessions(engine: Engine, keep_token: str | None) -> int:
    """signs out every session but keep_token's, returns how many"""
    with Session(engine) as session:
        ended = _end_sessions(session, keep_token)
        session.commit()
    return ended


def session_count(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count(LoginSession.id))) or 0


# ---------------------------------------------------------------------------
# guessing
# ---------------------------------------------------------------------------


class LoginThrottle:
    """refuses an address after MAX_FAILURES failed attempts within FAILURE_WINDOW. Keyed
    by the connection's address, never a header a client could make up, so behind a
    proxy every visitor shares one count: a wrong guess storm locks sign-in for a while,
    browsers already signed in carry on"""

    def __init__(self) -> None:
        self._failures: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: datetime) -> deque[datetime]:
        failures = self._failures.get(key, deque())
        while failures and failures[0] <= now - FAILURE_WINDOW:
            failures.popleft()
        return failures

    def wait(self, key: str, now: datetime | None = None) -> timedelta | None:
        """how long the address must wait, None when it may try"""
        now = now or utc_now()
        with self._lock:
            failures = self._recent(key, now)
            if len(failures) < MAX_FAILURES:
                return None
            return failures[0] + FAILURE_WINDOW - now

    def failed(self, key: str, now: datetime | None = None) -> None:
        now = now or utc_now()
        with self._lock:
            failures = self._recent(key, now)
            failures.append(now)
            self._failures[key] = failures

    def succeeded(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def safe_next(value: str | None) -> str:
    """where to go after signing in: a path on this site, never another site"""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value
