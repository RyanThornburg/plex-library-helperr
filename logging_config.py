"""logging_config.py"""

import os
import pathlib
from typing import Any

# DATA_DIR keeps logs out of the app dir, e.g. a docker volume
LOG_DIR = pathlib.Path(os.environ.get("DATA_DIR") or pathlib.Path(__file__).parent) / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

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
            "markup": False,
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
