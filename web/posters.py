"""posters.py - TMDB posters for the add request pages, fetched by the app and kept on
disk: the CSP keeps the browser to this site, and a poster is how you tell two films of
the same name apart. Only the poster's file name (from seerr) goes to TMDB"""

import logging
import os
import pathlib
import re
import tempfile
import threading

import requests

from logging_config import failure_text

logger = logging.getLogger(__name__)

TMDB_IMAGES = "https://image.tmdb.org/t/p"
# a search result's card, the request page's
SIZES = ("w185", "w342")
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png)$")
TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}
TIMEOUT = 10
# a w342 poster is ~30 KB
MAX_BYTES = 2_000_000
# past this many files the oldest tenth goes (~60 MB at most)
MAX_FILES = 3000


class PosterCache:
    def __init__(self, directory: pathlib.Path):
        self.directory = directory
        self._lock = threading.Lock()

    def path(self, size: str, name: str) -> pathlib.Path | None:
        """where a poster is kept, None for a size or name that isn't one"""
        if size not in SIZES or not NAME.match(name):
            return None
        return self.directory / size / name

    def get(self, size: str, name: str) -> tuple[bytes, str] | None:
        """the poster and its content type, fetched once; None when it isn't one or TMDB
        doesn't have it"""
        path = self.path(size, name)
        if path is None:
            return None
        content_type = TYPES[name.rsplit(".", 1)[1].lower()]
        try:
            return path.read_bytes(), content_type
        except OSError:
            pass
        try:
            response = requests.get(f"{TMDB_IMAGES}/{size}/{name}", timeout=TIMEOUT, stream=True)
            if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
                response.close()
                return None
            data = response.raw.read(MAX_BYTES + 1, decode_content=True)
            response.close()
        except requests.RequestException as e:
            logger.warning("WEB\t Couldn't fetch a poster from TMDB: %s", failure_text(e))
            return None
        if len(data) > MAX_BYTES:
            return None
        self._store(path, data)
        return data, content_type

    def _store(self, path: pathlib.Path, data: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
                temp.write(data)
            os.replace(temp.name, path)
            self._prune()
        except OSError as e:
            logger.warning("WEB\t Couldn't keep a poster: %s", e)

    def _prune(self) -> None:
        with self._lock:
            files = [f for f in self.directory.glob("*/*") if f.is_file()]
            if len(files) <= MAX_FILES:
                return
            files.sort(key=lambda f: f.stat().st_mtime)
            for old in files[: len(files) // 10 or 1]:
                old.unlink(missing_ok=True)
