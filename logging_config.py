"""logging_config.py"""

import logging
import os
import pathlib
import re
from typing import Any
from urllib.parse import quote, quote_plus

import requests

# DATA_DIR keeps logs out of the app dir, e.g. a docker volume
LOG_DIR = pathlib.Path(os.environ.get("DATA_DIR") or pathlib.Path(__file__).parent) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# the config's secrets, never written to a log (or a run's stored log and result).
# Libraries put urls with keys in their errors: arrapi sends ?apikey=, tautulli and
# sabnzbd take theirs as a query param too
_secrets: set[str] = set()


def hide_secrets(*secrets: str | None) -> None:
    """add secrets to what's redacted, called whenever the settings load"""
    for secret in secrets:
        if secret and len(secret) >= 4:
            _secrets.update({secret, quote(secret, safe=""), quote_plus(secret)})


def redacted(text: str) -> str:
    # longest first, so a secret containing another is replaced whole
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# a url in error text: scheme and host, the path, then the query (not the punctuation
# after it)
_URL = re.compile(r"\b([a-z][a-z0-9+.-]*://[^/\s'\"<>]+)(/[^?#\s'\"<>]*)?([?#][^\s'\"<>]*[^\s'\"<>.,;:)])?", re.I)


def without_queries(text: str) -> str:
    """text with every url's query string dropped, where keys go (an indexer's in a
    SABnzbd fail message, which isn't one of the config's secrets)"""
    return _URL.sub(lambda m: m.group(1) + (m.group(2) or ""), text)


def failure_text(e: BaseException) -> str:
    """why a request failed, safe to store in a result: libraries put the whole url in
    their errors (arrapi with ?apikey=), so urls are cut to their path and the config's
    secrets redacted. Results are shown on run pages and kept"""
    if isinstance(e, requests.exceptions.Timeout):
        return "no answer in time"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "couldn't connect"
    text = _URL.sub(lambda m: m.group(2) or "", str(e) or type(e).__name__)
    return redacted(text)


class SecretFilter(logging.Filter):
    """redacts the config's secrets from a record's message and traceback"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            # a bad format string (a library's) would otherwise raise in the code that
            # logged it, since filters run inside logger.handle: keep it unformatted
            message = f"{record.msg!r} {record.args!r}"
        record.msg, record.args = redacted(message), None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redacted(record.exc_text)
            # rich renders exc_info itself, the redacted text has to stand in for it
            record.exc_info = None
        return True


def logging_config(log_file: str = "log.log") -> dict[str, Any]:
    """the web app and the cli jobs run in separate containers, each needs its own log_file
    because two processes rotating one file lose lines"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"secrets": {"()": SecretFilter}},
        "formatters": {
            "rich": {"format": "%(message)s"},
            "default": {
                "format": "%(levelname)s %(asctime)s [%(filename)s:%(lineno)d]\t %(message)s"
            },
        },
        "handlers": {
            "rich": {
                "class": "rich.logging.RichHandler",
                "level": "INFO",
                "formatter": "rich",
                "markup": False,
                "rich_tracebacks": True,
                "filters": ["secrets"],
            },
            "logfile": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "default",
                "backupCount": 2,
                "maxBytes": 500000,
                "filename": str(LOG_DIR / log_file),
                "filters": ["secrets"],
            },
        },
        "root": {"level": "DEBUG", "handlers": ["rich", "logfile"]},
        # alembic logs every plugin and migration step at INFO on each start
        "loggers": {"alembic": {"level": "WARNING"}},
    }

