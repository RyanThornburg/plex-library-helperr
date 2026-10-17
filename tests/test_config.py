"""Tests for config module"""

from datetime import timedelta

import pytest

import config
from config import _build_arr_config, parse_duration
from config.models import MoverRule, ProfileRule


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_days(self):
        assert parse_duration("7d") == timedelta(days=7)

    def test_hours(self):
        assert parse_duration("24h") == timedelta(hours=24)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_single_day(self):
        assert parse_duration("1d") == timedelta(days=1)

    def test_empty_string_returns_none(self):
        assert parse_duration("") is None

    def test_none_returns_none(self):
        assert parse_duration(None) is None

    def test_invalid_unit_returns_none(self):
        assert parse_duration("7x") is None

    def test_invalid_format_returns_none(self):
        assert parse_duration("abc") is None

    def test_non_string_returns_none(self):
        assert parse_duration(7) is None

    def test_whitespace_is_stripped(self):
        assert parse_duration(" 5d ") == timedelta(days=5)


# ---------------------------------------------------------------------------
# MoverRule validation
# ---------------------------------------------------------------------------


class TestMoverRuleValidation:
    def test_invalid_genre_match_raises(self):
        with pytest.raises(ValueError, match="genre_match"):
            MoverRule(path="/foo", genres=["Action"], genre_match="maybe")

    def test_invalid_tag_match_raises(self):
        with pytest.raises(ValueError, match="tag_match"):
            MoverRule(path="/foo", tags=["kids"], tag_match="maybe")

    def test_no_criteria_raises(self):
        with pytest.raises(ValueError, match="requires at least one"):
            MoverRule(path="/foo")

    def test_valid_genre_match_any_does_not_raise(self):
        MoverRule(path="/foo", genres=["Action"], genre_match="any")

    def test_valid_genre_match_all_does_not_raise(self):
        MoverRule(path="/foo", genres=["Action"], genre_match="all")

    def test_valid_tag_match_any_does_not_raise(self):
        MoverRule(path="/foo", tags=["kids"], tag_match="any")

    def test_valid_tag_match_all_does_not_raise(self):
        MoverRule(path="/foo", tags=["kids"], tag_match="all")


# ---------------------------------------------------------------------------
# ProfileRule validation
# ---------------------------------------------------------------------------


class TestProfileRuleValidation:
    def test_no_criteria_raises(self):
        with pytest.raises(ValueError, match="requires at least one"):
            ProfileRule(profile="HD-1080p")

    def test_titles_only_does_not_raise(self):
        ProfileRule(profile="HD-1080p", titles=["Inception"])

    def test_networks_only_does_not_raise(self):
        ProfileRule(profile="HD-1080p", networks=["HBO"])

    def test_studios_only_does_not_raise(self):
        ProfileRule(profile="HD-1080p", studios=["A24"])


# ---------------------------------------------------------------------------
# arr-specific field validation
# ---------------------------------------------------------------------------


class TestArrFieldValidation:
    def test_networks_on_radarr_mover_raises(self):
        data = {"movers": [{"path": "/movies/kids", "networks": ["HBO"]}]}
        with pytest.raises(ValueError, match=r"radarr\.movers\]\] #1: 'networks'"):
            _build_arr_config(data, False, "radarr")

    def test_studios_on_sonarr_profiler_raises(self):
        data = {"profilers": [{"profile": "HD", "studios": ["A24"]}]}
        with pytest.raises(ValueError, match=r"sonarr\.profilers\]\] #1: 'studios'"):
            _build_arr_config(data, False, "sonarr")

    def test_reports_correct_rule_number(self):
        data = {
            "movers": [
                {"path": "/tv/kids", "genres": ["family"]},
                {"path": "/tv/kids", "studios": ["Pixar"]},
            ]
        }
        with pytest.raises(ValueError, match="#2"):
            _build_arr_config(data, False, "sonarr")

    def test_supported_fields_do_not_raise(self):
        _build_arr_config(
            {"movers": [{"path": "/movies/kids", "studios": ["Pixar"]}]},
            False,
            "radarr",
        )
        _build_arr_config(
            {"movers": [{"path": "/tv/kids", "networks": ["HBO"]}]}, False, "sonarr"
        )


# ---------------------------------------------------------------------------
# plex config
# ---------------------------------------------------------------------------


class TestPlexConfig:
    def _load(self, tmp_path, monkeypatch, plex_toml: str):
        (tmp_path / "config.toml").write_text(plex_toml)
        monkeypatch.setattr(config, "__file__", str(tmp_path / "__init__.py"))
        return config.load_config()

    def test_empty_plex_values_is_none(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path, monkeypatch, "[plex]\nbaseurl = ''\ntoken = ''\n"
        )
        assert settings.plex is None

    def test_missing_token_is_none(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path, monkeypatch, "[plex]\nbaseurl = 'http://plex:32400'\n"
        )
        assert settings.plex is None

    def test_configured_plex(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path,
            monkeypatch,
            "[plex]\nbaseurl = 'http://plex:32400'\ntoken = 'abc'\n",
        )
        assert settings.plex is not None
        assert settings.plex.baseurl == "http://plex:32400"
