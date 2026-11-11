"""Tests for the app's version: the build's from CI, else pyproject's with -dev, and a
changelog entry for every release"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import version
from tests.test_web import FakeKeys, auth, fake_run_job, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from version import app_version, project_version
from web.app import create_app

ROOT = Path(__file__).resolve().parent.parent


class TestAppVersion:
    def test_a_release_build_shows_its_tag_without_the_v(self):
        assert app_version({"HELPERR_VERSION": "v2.0.0"}) == "2.0.0"

    def test_an_edge_build_shows_how_far_past_the_release_it_is(self):
        assert app_version({"HELPERR_VERSION": "v2.0.0-3-gabc1234"}) == "2.0.0-3-gabc1234"

    def test_a_build_without_a_release_tag_shows_its_commit(self):
        assert app_version({"HELPERR_VERSION": "abc1234"}) == "abc1234"

    @pytest.mark.parametrize("built", ["", "  ", "v", "<script>", "2.0.0 extra", "x" * 80])
    def test_from_source_or_a_strange_build_it_is_pyprojects_with_dev(self, built):
        assert app_version({"HELPERR_VERSION": built}) == f"{project_version()}-dev"
        assert app_version({}) == f"{project_version()}-dev"

    def test_pyproject_unreadable(self, tmp_path):
        assert project_version(tmp_path / "missing.toml") == "unknown"
        broken = tmp_path / "pyproject.toml"
        broken.write_text("[project]\nname = 'x'\n")
        assert project_version(broken) == "unknown"
        assert app_version({}, broken) == "unknown-dev"


def test_the_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", project_version())


def test_the_changelog_has_the_version():
    """CI makes the GitHub release from this section, and fails without it"""
    headings = re.findall(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert project_version() in headings


def test_the_lock_file_has_the_version():
    """CI installs with --locked, so a version bump needs `uv lock`"""
    lock = (ROOT / "uv.lock").read_text()
    assert f'name = "plex-library-helperr"\nversion = "{project_version()}"' in lock


def test_the_sidebar_shows_the_version(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
    with TestClient(app) as client:
        page = client.get("/runs", headers=auth()).text
    assert f'<p class="version" title="Helperr version">Helperr {version.VERSION}</p>' in page
