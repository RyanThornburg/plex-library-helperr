"""version.py - which Helperr this is.

A release's version is pyproject.toml's (`2.0.0`), and a release is the git tag `v2.0.0`.
CI bakes `git describe` into the image as HELPERR_VERSION: `v2.0.0` for a release,
`v2.0.0-3-gabc1234` for an edge build 3 commits after it (commit abc1234). Run from
source without it, it's pyproject's version with `-dev`."""

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

PYPROJECT = Path(__file__).with_name("pyproject.toml")
# what a build's version may look like; anything else is ignored rather than shown
BUILT = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")


def project_version(path: Path = PYPROJECT) -> str:
    try:
        with path.open("rb") as file:
            return str(tomllib.load(file)["project"]["version"])
    except OSError, KeyError, tomllib.TOMLDecodeError:
        return "unknown"


def app_version(env: Mapping[str, str] = os.environ, path: Path = PYPROJECT) -> str:
    built = env.get("HELPERR_VERSION", "").strip().removeprefix("v")
    if BUILT.fullmatch(built):
        return built
    return f"{project_version(path)}-dev"


VERSION = app_version()
