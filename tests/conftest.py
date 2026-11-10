"""shared fixtures"""

import pytest
from sqlalchemy import Engine, event


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """DATA_DIR is each test's tmp dir, so nothing touches the real helperr.db or logs"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def dispose_engines():
    """every engine a test connected with is disposed after it: fixtures and stores make
    their own, and a pooled sqlite connection left open is a ResourceWarning"""
    engines: set[Engine] = set()

    def seen(connection):
        engines.add(connection.engine)

    event.listen(Engine, "engine_connect", seen)
    yield
    event.remove(Engine, "engine_connect", seen)
    for engine in engines:
        engine.dispose()
