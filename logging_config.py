"""logging_config.py"""

import pathlib
from typing import Any

LOG_DIR = pathlib.Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING_CONFIG: dict[str, Any] = {
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
            "markup": True,
            "rich_tracebacks": True,
        },
        "logfile": {
            "class": "logging.handlers.RotatingFileHandler",
            "level": "INFO",
            "formatter": "default",
            "backupCount": 2,
            "maxBytes": 500000,
            "filename": str(LOG_DIR / "log.log"),
        },
    },
    "root": {"level": "DEBUG", "handlers": ["rich", "logfile"]},
}
