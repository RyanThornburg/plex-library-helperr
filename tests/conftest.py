"""shared fixtures"""

import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """DATA_DIR is each test's tmp dir, so nothing reads the real config/config.toml (the
    web app imports it at startup)"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path
