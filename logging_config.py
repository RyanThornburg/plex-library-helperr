"""logging_config.py"""

import logging
import os
import pathlib
from typing import Any
from urllib.parse import quote, quote_plus

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


class SecretFilter(logging.Filter):
    """redacts the config's secrets from a record's message and traceback"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        record.msg, record.args = redacted(record.getMessage()), None
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

