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


class TestEmptyTrashConfig:
    def _load(self, tmp_path, monkeypatch, toml: str):
        (tmp_path / "config.toml").write_text(toml)
        monkeypatch.setattr(config, "__file__", str(tmp_path / "__init__.py"))
        return config.load_config()

    def test_defaults(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "")
        assert settings.empty_trash.libraries == []
        assert settings.empty_trash.max_items == 100
        assert settings.empty_trash.dry_run is False

    def test_dry_run_defaults_to_settings(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "[settings]\ndry_run = true\n")
        assert settings.empty_trash.dry_run is True

    def test_parsed(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path,
            monkeypatch,
            "[settings]\ndry_run = true\n"
            "[plex.empty_trash]\nlibraries = ['TV Shows']\nmax_items = 20\ndry_run = false\n",
        )
        assert settings.empty_trash.libraries == ["TV Shows"]
        assert settings.empty_trash.max_items == 20
        assert settings.empty_trash.dry_run is False

    @pytest.mark.parametrize(
        "toml",
        ["max_items = 0", "max_items = true", "max_items = '10'", "unknown = 1"],
    )
    def test_invalid_raises(self, tmp_path, monkeypatch, toml):
        with pytest.raises(ValueError, match=r"\[plex.empty_trash\]"):
            self._load(tmp_path, monkeypatch, f"[plex.empty_trash]\n{toml}\n")


# ---------------------------------------------------------------------------
# removers
# ---------------------------------------------------------------------------


def remover(**kwargs):
    return {
        "type": "seasons",
        "keep": 2,
        "tags": ["rm-seasons-2"],
        "min_age": "180d",
        **kwargs,
    }


class TestRemoverConfig:
    def test_valid_remover(self):
        arr = _build_arr_config(
            {"removers": [remover(skip_if_watched_within="14d")]}, False, "sonarr"
        )
        rule = arr.removers[0]
        assert rule.min_age == timedelta(days=180)
        assert rule.skip_if_watched_within == timedelta(days=14)
        assert rule.include_specials is False

    def test_remover_settings_default_to_dry_run(self):
        arr = _build_arr_config({"removers": [remover()]}, False, "sonarr")
        assert arr.remover_settings.dry_run is True
        assert arr.remover_settings.max_deletes is None

    def test_remover_settings_parsed(self):
        arr = _build_arr_config(
            {
                "removers": [remover()],
                "remover_settings": {"dry_run": False, "max_deletes": 50},
            },
            True,
            "sonarr",
        )
        assert arr.remover_settings.dry_run is False
        assert arr.remover_settings.max_deletes == 50

    def test_invalid_max_deletes_raises(self):
        with pytest.raises(ValueError, match="max_deletes"):
            _build_arr_config({"remover_settings": {"max_deletes": 0}}, False, "sonarr")

    def test_removers_on_radarr_raise(self):
        with pytest.raises(ValueError, match="only supported for sonarr"):
            _build_arr_config({"removers": [remover()]}, False, "radarr")

    @pytest.mark.parametrize("min_age", ["180", 180, "", "6 months", "0d"])
    def test_invalid_min_age_raises(self, min_age):
        with pytest.raises(ValueError, match="#1"):
            _build_arr_config({"removers": [remover(min_age=min_age)]}, False, "sonarr")

    def test_missing_min_age_raises(self):
        data = remover()
        del data["min_age"]
        with pytest.raises(ValueError, match="'min_age' is required"):
            _build_arr_config({"removers": [data]}, False, "sonarr")

    def test_invalid_watch_window_raises(self):
        with pytest.raises(ValueError, match="skip_if_watched_within"):
            _build_arr_config(
                {"removers": [remover(skip_if_watched_within="two weeks")]},
                False,
                "sonarr",
            )

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"type": "series"}, "type"),
            ({"keep": 0}, "keep"),
            ({"keep": "2"}, "keep"),
            ({"tags": []}, "at least one tag"),
            ({"tag_match": "all"}, "tag_match"),
        ],
    )
    def test_invalid_fields_raise(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            _build_arr_config({"removers": [remover(**kwargs)]}, False, "sonarr")

    def test_tag_shared_between_removers_raises(self):
        data = {
            "removers": [
                remover(tags=["rm-a"]),
                remover(type="episodes", tags=["rm-b", "RM-A"]),
            ]
        }
        with pytest.raises(
            ValueError, match="#2: tag 'rm-a' is already used by remover #1"
        ):
            _build_arr_config(data, False, "sonarr")


class TestTautulliConfig:
    def _load(self, tmp_path, monkeypatch, toml: str):
        (tmp_path / "config.toml").write_text(toml)
        monkeypatch.setattr(config, "__file__", str(tmp_path / "__init__.py"))
        return config.load_config()

    def test_configured(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path, monkeypatch, "[tautulli]\nurl = 'http://t:8181'\napikey = 'abc'\n"
        )
        assert settings.tautulli.url == "http://t:8181"
        assert "abc" not in repr(settings)

    def test_missing_apikey_is_none(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "[tautulli]\nurl = 'http://t'\n")
        assert settings.tautulli is None


class TestDataDir:
    def test_loads_from_data_dir(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text("[settings]\ndry_run = true\n")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        assert config.load_config().empty_trash.dry_run is True

    def test_copies_example_into_data_dir(self, tmp_path, monkeypatch):
        package_dir = tmp_path / "package"
        data_dir = tmp_path / "data"
        package_dir.mkdir()
        data_dir.mkdir()
        (package_dir / "config.toml.example").write_text("[settings]\ndry_run = true\n")
        monkeypatch.setattr(config, "__file__", str(package_dir / "__init__.py"))
        monkeypatch.setenv("DATA_DIR", str(data_dir))
        assert config.load_config().empty_trash.dry_run is True
        assert (data_dir / "config.toml").exists()
        assert not (package_dir / "config.toml").exists()
