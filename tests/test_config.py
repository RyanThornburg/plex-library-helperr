import re
"""Tests for config module"""

import tomllib
from datetime import timedelta

import pytest

import config
from config import _build_arr_config, format_duration, parse_duration
from config.models import MoverRule, MovieRemoverRule, ProfileRule, WebDefaults


def load(toml: str):
    return config.build_config(tomllib.loads(toml))

# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("7d", timedelta(days=7)),
            ("24h", timedelta(hours=24)),
            ("30m", timedelta(minutes=30)),
            (" 5d ", timedelta(days=5)),
        ],
    )
    def test_valid(self, value, expected):
        assert parse_duration(value, "x") == expected

    @pytest.mark.parametrize("value", ["", None, "7x", "abc", 7, "1.5d"])
    def test_invalid_raises(self, value):
        with pytest.raises(ValueError, match="filter_time: invalid duration"):
            parse_duration(value, "filter_time")

    @pytest.mark.parametrize(
        "value, text",
        [(timedelta(days=7), "7d"), (timedelta(hours=36), "36h"), (timedelta(minutes=90), "90m")],
    )
    def test_format_round_trips(self, value, text):
        assert format_duration(value) == text
        assert parse_duration(text, "x") == value


class TestSettings:
    def test_filter_time(self):
        assert load("[settings]\nfilter_time = '1d'\n").filter_time == timedelta(days=1)
        assert load("[settings]\nfilter_time = ''\n").filter_time is None
        assert load("").filter_time is None

    def test_bad_filter_time_raises(self):
        # a typo would otherwise move the whole library
        with pytest.raises(ValueError, match="filter_time"):
            load("[settings]\nfilter_time = '1 day'\n")

    def test_dry_run_must_be_a_bool(self):
        with pytest.raises(ValueError, match="dry_run"):
            load("[settings]\ndry_run = 'false'\n")

    def test_web_defaults(self):
        assert load("").web == WebDefaults()
        web = load("[web]\nfollowup_delay = '1h'\nfollowup_attempts = 0\nplan_max_age = '3d'\nmin_free_gb = 0\n").web
        assert web.followup_delay == timedelta(hours=1)
        assert web.followup_attempts == 0
        assert web.plan_max_age == timedelta(days=3)
        assert web.min_free_gb == 0 and WebDefaults().min_free_gb == 100

    @pytest.mark.parametrize(
        "toml",
        ["followup_delay = '0m'", "followup_attempts = -1", "followup_attempts = 11", "plan_max_age = '7'", "min_free_gb = -1", "min_free_gb = '100GB'", "other = 1"],
    )
    def test_bad_web_defaults_raise(self, toml):
        with pytest.raises(ValueError, match=r"\[web\]"):
            load(f"[web]\n{toml}\n")


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
        return load(plex_toml)

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
        return load(toml)

    def test_defaults(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "")
        assert settings.empty_trash.libraries == []
        assert settings.empty_trash.max_items == 100
        # a document without dry_run is dry
        assert settings.empty_trash.dry_run is True

    def test_dry_run_defaults_to_settings(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "[settings]\ndry_run = false\n")
        assert settings.empty_trash.dry_run is False

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

    def test_remover_settings_default(self):
        arr = _build_arr_config({"removers": [remover()]}, False, "sonarr")
        assert arr.remover_settings.max_deletes is None

    def test_remover_settings_parsed(self):
        arr = _build_arr_config(
            {"removers": [remover()], "remover_settings": {"max_deletes": 50}},
            True,
            "sonarr",
        )
        assert arr.remover_settings.max_deletes == 50

    def test_remover_dry_run_is_gone(self):
        # removers only delete approvals
        with pytest.raises(ValueError, match="remover_settings"):
            _build_arr_config({"remover_settings": {"dry_run": False}}, False, "sonarr")

    def test_invalid_max_deletes_raises(self):
        with pytest.raises(ValueError, match="max_deletes"):
            _build_arr_config({"remover_settings": {"max_deletes": 0}}, False, "sonarr")

    def test_sonarr_removers_on_radarr_raise(self):
        with pytest.raises(ValueError, match=r"\[\[radarr.removers\]\] #1.*'type'"):
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
        return load(toml)

    def test_configured(self, tmp_path, monkeypatch):
        settings = self._load(
            tmp_path, monkeypatch, "[tautulli]\nurl = 'http://t:8181'\napikey = 'abc'\n"
        )
        assert settings.tautulli.url == "http://t:8181"
        assert "abc" not in repr(settings)

    def test_missing_apikey_is_none(self, tmp_path, monkeypatch):
        settings = self._load(tmp_path, monkeypatch, "[tautulli]\nurl = 'http://t'\n")
        assert settings.tautulli is None


class TestSabnzbdConfig:
    def test_configured(self):
        settings = load("[sabnzbd]\nurl = 'http://sab:8080'\napikey = 'abc123'\n")
        assert settings.sabnzbd.url == "http://sab:8080"
        assert "abc123" not in repr(settings)

    def test_missing_apikey_is_none(self):
        assert load("[sabnzbd]\nurl = 'http://sab:8080'\n").sabnzbd is None


class TestBazarrConfig:
    def test_configured(self):
        settings = load("[bazarr]\nurl = 'http://bazarr:6767/subs'\npublic_url = 'https://bazarr.example.com'\napikey = 'abc123'\n")
        assert (settings.bazarr.url, settings.bazarr.public_url) == ("http://bazarr:6767/subs", "https://bazarr.example.com")
        assert "abc123" not in repr(settings)

    def test_missing_apikey_is_none(self):
        assert load("[bazarr]\nurl = 'http://bazarr:6767'\n").bazarr is None


class TestLinks:
    def test_links(self):
        settings = load("[[links]]\nname = 'Tracearr'\nurl = 'http://unraid:3000'\n\n[[links]]\nname = 'Unraid'\npublic_url = 'https://unraid.example.com'\n")
        assert [(l.name, l.url, l.public_url) for l in settings.links] == [
            ("Tracearr", "http://unraid:3000", None), ("Unraid", None, "https://unraid.example.com"),
        ]

    @pytest.mark.parametrize("toml, error", [
        ("[[links]]\nurl = 'http://x'\n", "#1: needs a name"),
        ("[[links]]\nname = 'X'\n", "#1 (X): needs a url or a public_url"),
        ("[[links]]\nname = 'X'\nurl = 'x:3000'\n", "#1 (X): url must start with http"),
        ("[[links]]\nname = 'X'\nurl = 'http://x'\nservice = 'kometa'\n", "#1 (X): service must be one of plex,"),
    ])
    def test_invalid(self, toml, error):
        with pytest.raises(ValueError, match=re.escape(error)):
            load(toml)


class TestBadShapes:
    @pytest.mark.parametrize("document, error", [
        ({"settings": "x"}, "[settings] must be a table, got str"),
        ({"radarr": 5}, "[radarr] must be a table, got int"),
        ({"sonarr": {"removers": [5]}}, "invalid config"),
        ({"settings": {"filter_time": "99999999999d"}}, "invalid duration '99999999999d'"),
    ])
    def test_value_errors(self, document, error):
        with pytest.raises(ValueError, match=re.escape(error)):
            config.build_config(document)


def movie_remover(**kwargs):
    return {"path": "/data/media/movies/movies-hallmark", "min_age": "730d", **kwargs}


class TestMovieRemoverConfig:
    def test_valid(self):
        arr = _build_arr_config(
            {"removers": [movie_remover(skip_if_watched_within="180d", exclude_tags=["keep"])]},
            False,
            "radarr",
        )
        rule = arr.removers[0]
        assert isinstance(rule, MovieRemoverRule)
        assert rule.min_age == timedelta(days=730)
        assert rule.skip_if_watched_within == timedelta(days=180)
        assert rule.watch_check and not rule.skip_if_ever_watched
        assert rule.key == "radarr:/data/media/movies/movies-hallmark"
        assert rule.label == "/data/media/movies/movies-hallmark"

    def test_trailing_slash_is_the_same_folder(self):
        rule = _build_arr_config({"removers": [movie_remover(path="/movies/hallmark/")]}, False, "radarr").removers[0]
        assert rule.key == "radarr:/movies/hallmark"

    def test_folder_used_twice_raises(self):
        removers = [movie_remover(path="/movies/hallmark"), movie_remover(path="/movies/hallmark/")]
        with pytest.raises(ValueError, match="#2: path '/movies/hallmark' is already used by remover #1"):
            _build_arr_config({"removers": removers}, False, "radarr")

    @pytest.mark.parametrize(
        "changes, message",
        [
            ({"min_age": "2 years"}, "min_age: invalid duration"),
            ({"min_age": None}, "min_age"),
            ({"path": ""}, "root folder path"),
            ({"path": "/"}, "root folder path"),
            ({"skip_if_watched_within": "0d"}, "greater than 0"),
            ({"skip_if_ever_watched": True, "skip_if_watched_within": "7d"}, "not both"),
            ({"skip_if_ever_watched": "yes"}, "true or false"),
        ],
    )
    def test_invalid_raises(self, changes, message):
        with pytest.raises(ValueError, match=message):
            _build_arr_config({"removers": [movie_remover(**changes)]}, False, "radarr")

    def test_missing_min_age_raises(self):
        data = movie_remover()
        del data["min_age"]
        with pytest.raises(ValueError, match="'min_age' is required"):
            _build_arr_config({"removers": [data]}, False, "radarr")

    def test_repr(self):
        rule = _build_arr_config({"removers": [movie_remover(skip_if_ever_watched=True)]}, False, "radarr").removers[0]
        assert repr(rule) == (
            "MovieRemoverRule(path=/data/media/movies/movies-hallmark,min_age=730 days, 0:00:00,"
            "skip_if_ever_watched=True)"
        )
