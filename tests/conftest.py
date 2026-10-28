"""shared fixtures"""

import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """DATA_DIR is each test's tmp dir, so nothing touches the real helperr.db or logs"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path
