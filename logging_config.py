"""logging_config.py"""

import os
import pathlib
from typing import Any

# DATA_DIR keeps logs out of the app dir, e.g. a docker volume
LOG_DIR = pathlib.Path(os.environ.get("DATA_DIR") or pathlib.Path(__file__).parent) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)



def logging_config(log_file: str = "log.log") -> dict[str, Any]:
    """the web app and the cli jobs run in separate containers, each needs its own log_file
    because two processes rotating one file lose lines"""
    return {
        "version": 1,
        "disable_existing_loggers": False,
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
            },
            "logfile": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "default",
                "backupCount": 2,
                "maxBytes": 500000,
                "filename": str(LOG_DIR / log_file),
            },
        },
        "root": {"level": "DEBUG", "handlers": ["rich", "logfile"]},
        # alembic logs every plugin and migration step at INFO on each start
        "loggers": {"alembic": {"level": "WARNING"}},
    }

