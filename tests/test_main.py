"""Tests for main.py"""

from unittest.mock import ANY, MagicMock, patch

import pytest
from arrapi import ConnectionFailure

import main
from config.models import (
    ArrConfig,
    AutoscanConfig,
    ConfigSettings,
    MoverRule,
    ProfileRule,
)
from main import (
    get_matches_based_on_mover_rules,
    get_media_matching_profile_rules,
    get_moved_dirs,
    is_path_valid,
    is_profile_valid,
)
from results import Change, JobResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tag(label: str) -> MagicMock:
    t = MagicMock()
    t.label = label
    return t


def make_root_folder(path: str) -> MagicMock:
    rf = MagicMock()
    rf.path = path
    return rf


def make_quality_profile(name: str) -> MagicMock:
    qp = MagicMock()
    qp.name = name
    return qp


def make_series(**kwargs) -> MagicMock:
    """Build a mock Series resembling the Justice League Sonarr response."""
    s = MagicMock()
    s.id = kwargs.get("id", 738)
    s.tmdbId = kwargs.get("tmdbId", None)
    s.imdbId = kwargs.get("imdbId", None)
    s.tvdbId = kwargs.get("tvdbId", 76320)
    s.title = kwargs.get("title", "Justice League")
    # Default path is NOT under the typical target so it appears in filtered_media
    s.path = kwargs.get(
        "path", "/data/media/tv/other/Justice League (2001) [imdb-tt0275137]"
    )
    s.genres = kwargs.get(
        "genres",
        ["Action", "Adventure", "Animation", "Children", "Crime", "Drama", "Family"],
    )
    s.tags = [make_tag(t) for t in kwargs.get("tags", [])]
    s.certification = kwargs.get("certification", "TV-Y7")
    s.network = kwargs.get("network", "Cartoon Network")
    s.studio = kwargs.get("studio", None)
    s.qualityProfile = make_quality_profile(kwargs.get("quality_profile", "HD-1080p"))
    return s


def make_movie(**kwargs) -> MagicMock:
    """Build a mock Movie resembling the Jurassic Park Radarr response."""
    m = MagicMock()
    m.id = kwargs.get("id", 485)
    m.tvdbId = kwargs.get("tvdbId", None)
    m.tmdbId = kwargs.get("tmdbId", None)
    m.imdbId = kwargs.get("imdbId", None)
    m.title = kwargs.get("title", "Jurassic Park")
    # Default path is NOT under the typical target so it appears in filtered_media
    m.path = kwargs.get("path", "/data/media/movies/other/Jurassic Park (1993)")
    m.genres = kwargs.get("genres", ["Action", "Adventure", "Science Fiction"])
    m.tags = [make_tag(t) for t in kwargs.get("tags", [])]
    m.certification = kwargs.get("certification", "PG")
    m.network = kwargs.get("network", None)
    m.studio = kwargs.get("studio", "Universal Pictures")
    m.qualityProfile = make_quality_profile(kwargs.get("quality_profile", "HD-1080p"))
    return m


# ---------------------------------------------------------------------------
# is_profile_valid
# ---------------------------------------------------------------------------


class TestIsProfileValid:
    def test_returns_true_when_profile_exists(self):
        profiles = [make_quality_profile("HD-1080p"), make_quality_profile("4K")]
        assert is_profile_valid("HD-1080p", profiles) is True

    def test_returns_false_when_profile_missing(self):
        profiles = [make_quality_profile("HD-1080p")]
        assert is_profile_valid("4K", profiles) is False

    def test_returns_false_for_empty_list(self):
        assert is_profile_valid("HD-1080p", []) is False


# ---------------------------------------------------------------------------
# is_path_valid
# ---------------------------------------------------------------------------


class TestIsPathValid:
    def test_returns_true_when_path_exists(self):
        folders = [
            make_root_folder("/data/media/tv/tv"),
            make_root_folder("/data/media/tv/kids"),
        ]
        assert is_path_valid("/data/media/tv/kids", folders) is True

    def test_returns_false_when_path_missing(self):
        folders = [make_root_folder("/data/media/tv/tv")]
        assert is_path_valid("/data/media/tv/kids", folders) is False

    def test_returns_false_for_empty_list(self):
        assert is_path_valid("/data/media/tv/tv", []) is False


# ---------------------------------------------------------------------------
# get_matches_based_on_mover_rules — Sonarr / Series
# ---------------------------------------------------------------------------


class TestMoverRulesSonarr:
    TARGET = "/data/media/tv/kids"
    LOG = "SONARR"

    def _root_folders(self):
        return [make_root_folder(self.TARGET)]

    def test_invalid_path_returns_empty(self):
        mover = MoverRule(path=self.TARGET, genres=["Animation"])
        result = get_matches_based_on_mover_rules(mover, [make_series()], [], self.LOG)
        assert result == []

    def test_media_already_in_target_path_is_skipped(self):
        mover = MoverRule(path=self.TARGET, genres=["Animation"])
        # Place the series directly under the target path
        series = make_series(path=f"{self.TARGET}/Justice League (2001)")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_media_in_exclude_path_is_skipped(self):
        exclude = "/data/media/tv/excluded"
        mover = MoverRule(
            path=self.TARGET,
            exclude_paths=[exclude],
            genres=["Animation"],
        )
        series = make_series(path=f"{exclude}/Justice League (2001)")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_genre_match_any_returns_tvdb_id(self):
        mover = MoverRule(path=self.TARGET, genres=["Animation"], genre_match="any")
        series = make_series()
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_genre_match_all_returns_tvdb_id_when_all_present(self):
        mover = MoverRule(
            path=self.TARGET,
            genres=["Animation", "Action"],
            genre_match="all",
        )
        series = make_series(genres=["Action", "Adventure", "Animation"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_genre_match_all_returns_empty_when_not_all_present(self):
        mover = MoverRule(
            path=self.TARGET,
            genres=["Animation", "Horror"],
            genre_match="all",
        )
        series = make_series(genres=["Animation", "Action"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_network_match_returns_tvdb_id(self):
        mover = MoverRule(path=self.TARGET, networks=["Cartoon Network"])
        series = make_series()
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_network_no_match_returns_empty(self):
        mover = MoverRule(path=self.TARGET, networks=["HBO"])
        series = make_series(network="Cartoon Network")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_network_match_is_case_insensitive(self):
        mover = MoverRule(path=self.TARGET, networks=["cartoon network"])
        series = make_series(network="Cartoon Network")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_certification_match_returns_tvdb_id(self):
        mover = MoverRule(path=self.TARGET, certifications=["TV-Y7"])
        series = make_series(certification="TV-Y7")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_certification_no_match_returns_empty(self):
        mover = MoverRule(path=self.TARGET, certifications=["TV-MA"])
        series = make_series(certification="TV-Y7")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_exclude_genre_skips_media(self):
        mover = MoverRule(
            path=self.TARGET,
            genres=["Action"],
            exclude_genres=["Animation"],
        )
        series = make_series(genres=["Action", "Animation"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_exclude_certification_skips_media(self):
        mover = MoverRule(
            path=self.TARGET,
            genres=["Animation"],
            exclude_certifications=["TV-Y7"],
        )
        series = make_series(certification="TV-Y7")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_exclude_tag_skips_media(self):
        mover = MoverRule(
            path=self.TARGET,
            genres=["Animation"],
            exclude_tags=["no-kids"],
        )
        series = make_series(tags=["no-kids", "animated"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_tag_match_any_returns_tvdb_id(self):
        mover = MoverRule(path=self.TARGET, tags=["animated"], tag_match="any")
        series = make_series(tags=["animated", "superhero"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_tag_match_all_returns_tvdb_id_when_all_present(self):
        mover = MoverRule(
            path=self.TARGET, tags=["animated", "superhero"], tag_match="all"
        )
        series = make_series(tags=["animated", "superhero", "dc"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series]

    def test_tag_match_all_returns_empty_when_not_all_present(self):
        mover = MoverRule(
            path=self.TARGET, tags=["animated", "superhero"], tag_match="all"
        )
        series = make_series(tags=["animated"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == []

    def test_multiple_series_mixed_results(self):
        mover = MoverRule(path=self.TARGET, genres=["Animation"], genre_match="any")
        jl = make_series(id=738, tvdbId=76320, genres=["Animation", "Action"])
        non_animated = make_series(
            id=999, tvdbId=99999, title="Breaking Bad", genres=["Drama", "Crime"]
        )
        result = get_matches_based_on_mover_rules(
            mover, [jl, non_animated], self._root_folders(), self.LOG
        )
        assert result == [jl]

    def test_empty_media_list_returns_empty(self):
        mover = MoverRule(path=self.TARGET, genres=["Animation"])
        result = get_matches_based_on_mover_rules(
            mover, [], self._root_folders(), self.LOG
        )
        assert result == []


# ---------------------------------------------------------------------------
# get_matches_based_on_mover_rules — Radarr / Movie
# ---------------------------------------------------------------------------


class TestMoverRulesRadarr:
    TARGET = "/data/media/movies/kids"
    LOG = "RADARR"

    def _root_folders(self):
        return [make_root_folder(self.TARGET)]

    def test_genre_match_returns_movie_id(self):
        mover = MoverRule(
            path=self.TARGET, genres=["Science Fiction"], genre_match="any"
        )
        movie = make_movie()
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie]

    def test_studio_match_returns_movie_id(self):
        mover = MoverRule(path=self.TARGET, studios=["Universal Pictures"])
        movie = make_movie()
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie]

    def test_studio_no_match_returns_empty(self):
        mover = MoverRule(path=self.TARGET, studios=["Warner Bros"])
        movie = make_movie(studio="Universal Pictures")
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == []

    def test_studio_match_is_case_insensitive(self):
        mover = MoverRule(path=self.TARGET, studios=["universal pictures"])
        movie = make_movie(studio="Universal Pictures")
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie]

    def test_certification_match_returns_movie_id(self):
        mover = MoverRule(path=self.TARGET, certifications=["PG"])
        movie = make_movie(certification="PG")
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie]

    def test_media_already_in_target_is_skipped(self):
        mover = MoverRule(path=self.TARGET, genres=["Action"])
        movie = make_movie(path=f"{self.TARGET}/Jurassic Park (1993)")
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == []


# ---------------------------------------------------------------------------
# get_media_matching_profile_rules
# ---------------------------------------------------------------------------


class TestProfileRulesSonarr:
    PROFILE = "Kids-HD"
    LOG = "SONARR"

    def _quality_profiles(self):
        return [make_quality_profile(self.PROFILE), make_quality_profile("HD-1080p")]

    def test_invalid_profile_returns_empty(self):
        rule = ProfileRule(profile="NonExistent", networks=["Cartoon Network"])
        result = get_media_matching_profile_rules(
            rule, [make_series()], self._quality_profiles(), self.LOG
        )
        assert result == []

    def test_media_already_on_correct_profile_is_skipped(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["Cartoon Network"])
        series = make_series(quality_profile=self.PROFILE)
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == []

    def test_network_match_returns_tvdb_id(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["Cartoon Network"])
        series = (
            make_series()
        )  # default quality_profile="HD-1080p", network="Cartoon Network"
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_title_match_returns_tvdb_id(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Justice League"])
        series = make_series()
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_title_match_is_case_insensitive(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["justice league"])
        series = make_series(title="Justice League")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_no_match_returns_empty(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["HBO"])
        series = make_series(network="Cartoon Network")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == []

    def test_missing_profile_and_criteria_returns_empty(self):
        rule = ProfileRule(profile="", networks=["Cartoon Network"])
        result = get_media_matching_profile_rules(
            rule, [make_series()], self._quality_profiles(), self.LOG
        )
        assert result == []

    def test_multiple_series_only_matching_returned(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["Cartoon Network"])
        jl = make_series(id=738, tvdbId=76320, network="Cartoon Network")
        other = make_series(id=999, tvdbId=99999, title="The Wire", network="HBO")
        result = get_media_matching_profile_rules(
            rule, [jl, other], self._quality_profiles(), self.LOG
        )
        assert result == [jl]


class TestProfileRulesRadarr:
    PROFILE = "Remux-1080p"
    LOG = "RADARR"

    def _quality_profiles(self):
        return [make_quality_profile(self.PROFILE), make_quality_profile("HD-1080p")]

    def test_studio_match_returns_movie_id(self):
        rule = ProfileRule(profile=self.PROFILE, studios=["Universal Pictures"])
        movie = make_movie()
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie]

    def test_studio_match_is_case_insensitive(self):
        rule = ProfileRule(profile=self.PROFILE, studios=["universal pictures"])
        movie = make_movie(studio="Universal Pictures")
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie]

    def test_title_match_returns_movie_id(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Jurassic Park"])
        movie = make_movie()
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie]

    def test_or_logic_title_match_overrides_missing_studio(self):
        """A title match alone is sufficient even when studio does not match."""
        rule = ProfileRule(
            profile=self.PROFILE,
            titles=["Jurassic Park"],
            studios=["Warner Bros"],
        )
        movie = make_movie(studio="Universal Pictures")
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie]

    def test_empty_media_list_returns_empty(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Jurassic Park"])
        result = get_media_matching_profile_rules(
            rule, [], self._quality_profiles(), self.LOG
        )
        assert result == []


class TestProfileRulesSonarrExtended:
    """Additional profiler coverage for Sonarr."""

    PROFILE = "Kids-HD"
    LOG = "SONARR"

    def _quality_profiles(self):
        return [make_quality_profile(self.PROFILE), make_quality_profile("HD-1080p")]

    def test_network_match_is_case_insensitive(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["cartoon network"])
        series = make_series(network="Cartoon Network")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_studio_match_returns_tvdb_id(self):
        rule = ProfileRule(profile=self.PROFILE, studios=["Warner Bros"])
        series = make_series(studio="Warner Bros")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_or_logic_network_match_overrides_missing_title(self):
        """A network match alone is sufficient even when title does not match."""
        rule = ProfileRule(
            profile=self.PROFILE,
            titles=["The Wire"],
            networks=["Cartoon Network"],
        )
        series = make_series(title="Justice League", network="Cartoon Network")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series]

    def test_empty_media_list_returns_empty(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["Cartoon Network"])
        result = get_media_matching_profile_rules(
            rule, [], self._quality_profiles(), self.LOG
        )
        assert result == []


# ---------------------------------------------------------------------------
# run_arr / main
# ---------------------------------------------------------------------------


def make_arr(media, root_paths):
    arr = MagicMock()
    arr.all_movies.return_value = media
    arr.root_folder.return_value = [make_root_folder(p) for p in root_paths]
    arr.quality_profile.return_value = []
    arr.edit_multiple_movies.return_value = ([], [])
    return arr


class TestRunArr:
    def setup_method(self):
        self.result = JobResult("movers")

    def _run(self, arr, arr_config):
        main.run_arr(
            arr_config,
            MagicMock(return_value=arr),
            "all_movies",
            "edit_multiple_movies",
            "RADARR",
            None,
            self.result,
        )

    def test_first_matching_mover_wins(self):
        movie = make_movie(genres=["Documentary", "Family"])
        arr = make_arr([movie], ["/movies/docs", "/movies/kids"])
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            movers=[
                MoverRule(path="/movies/docs", genres=["documentary"]),
                MoverRule(path="/movies/kids", genres=["family"]),
            ],
        )
        self._run(arr, arr_config)
        arr.edit_multiple_movies.assert_called_once_with(
            ids=[movie], root_folder="/movies/docs", move_files=True
        )

    def test_unclaimed_media_still_matches_later_mover(self):
        doc = make_movie(id=1, genres=["Documentary"])
        kids = make_movie(id=2, genres=["Family"])
        arr = make_arr([doc, kids], ["/movies/docs", "/movies/kids"])
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            movers=[
                MoverRule(path="/movies/docs", genres=["documentary"]),
                MoverRule(path="/movies/kids", genres=["family"]),
            ],
        )
        self._run(arr, arr_config)
        assert arr.edit_multiple_movies.call_count == 2
        assert arr.edit_multiple_movies.call_args.kwargs["ids"] == [kids]

    def test_moved_folders_added_to_scan_dirs(self):
        movie = make_movie(
            id=1,
            genres=["Documentary"],
            path="/data/media/movies/movies/Free Solo (2018)",
        )
        arr = make_arr([movie], ["/data/media/movies/docs"])
        updated = make_movie(id=1, path="/data/media/movies/docs/Free Solo (2018)")
        arr.edit_multiple_movies.return_value = ([updated], [])
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            movers=[MoverRule(path="/data/media/movies/docs", genres=["documentary"])],
        )
        self._run(arr, arr_config)
        assert self.result.scan_dirs == {
            "/data/media/movies/movies/Free Solo (2018)",
            "/data/media/movies/docs/Free Solo (2018)",
        }
        assert self.result.changes == [
            Change("RADARR", "move", "/data/media/movies/docs", ["Jurassic Park"], "done")
        ]

    def test_dry_run_adds_no_scan_dirs(self):
        movie = make_movie(genres=["Documentary"])
        arr = make_arr([movie], ["/movies/docs"])
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            dry_run=True,
            movers=[MoverRule(path="/movies/docs", genres=["documentary"])],
        )
        self._run(arr, arr_config)
        arr.edit_multiple_movies.assert_not_called()
        assert self.result.scan_dirs == set()
        assert [c.status for c in self.result.changes] == ["dry_run"]

    def test_profiler_change_recorded(self):
        movie = make_movie(studio="Pixar")
        arr = make_arr([movie], [])
        arr.quality_profile.return_value = [make_quality_profile("Kids")]
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            profilers=[ProfileRule(profile="Kids", studios=["pixar"])],
        )
        self._run(arr, arr_config)
        assert self.result.changes == [
            Change("RADARR", "profile", "Kids", ["Jurassic Park"], "done")
        ]


class TestMainErrorHandling:
    def test_arr_failure_does_not_stop_other_arrs(self):
        arr_config = ArrConfig(baseurl="http://arr", apikey="key", tag=None)
        settings = ConfigSettings(plex=None, radarr=arr_config, sonarr=arr_config)
        with (
            patch.object(main.config, "load_config", return_value=settings),
            patch.object(main.logging.config, "dictConfig"),
            patch.object(
                main, "run_arr", side_effect=[ConnectionFailure("down"), None]
            ) as run_arr,
        ):
            result = main.main([])
        assert run_arr.call_count == 2
        assert len(result.errors) == 1
        assert result.errors[0].startswith("RADARR failed")


class TestGetMovedDirs:
    def test_uses_updated_path_from_arr(self):
        old = make_series(id=5, path="/data/media/tv/tv/Bluey (2018)")
        new = make_series(id=5, path="/data/media/tv/tv-kids/Bluey")
        assert get_moved_dirs([old], [new], "/data/media/tv/tv-kids") == {
            "/data/media/tv/tv/Bluey (2018)",
            "/data/media/tv/tv-kids/Bluey",
        }

    def test_falls_back_to_target_plus_folder_name(self):
        old = make_series(id=5, path="/data/media/tv/tv/Bluey (2018)")
        assert get_moved_dirs([old], [], "/data/media/tv/tv-kids") == {
            "/data/media/tv/tv/Bluey (2018)",
            "/data/media/tv/tv-kids/Bluey (2018)",
        }


class TestMainScanDispatch:
    def _main(self, settings, scan_dirs, plex_libraries):
        def fake_run_arr(*args):
            args[-1].plex_libraries.update(plex_libraries)
            args[-1].scan_dirs.update(scan_dirs)

        with (
            patch.object(main.config, "load_config", return_value=settings),
            patch.object(main.logging.config, "dictConfig"),
            patch.object(main, "run_arr", side_effect=fake_run_arr),
            patch.object(main, "trigger_autoscan", return_value=True) as autoscan,
            patch.object(main, "refresh_plex_libraries", return_value=set()) as plex,
        ):
            self.result = main.main([])
        return autoscan, plex

    def _settings(self, autoscan):
        arr_config = ArrConfig(baseurl="http://arr", apikey="key", tag=None)
        return ConfigSettings(
            plex=None, radarr=arr_config, sonarr=None, autoscan=autoscan
        )

    def test_autoscan_used_instead_of_plex_when_configured(self):
        autoscan_config = AutoscanConfig(url="http://autoscan:3030")
        autoscan, plex = self._main(
            self._settings(autoscan_config), {"/a", "/b"}, {"Movies"}
        )
        autoscan.assert_called_once_with(autoscan_config, {"/a", "/b"})
        plex.assert_not_called()
        assert self.result.changes == [
            Change("AUTOSCAN", "scan", "autoscan", ["/a", "/b"], "done")
        ]

    def test_failed_autoscan_recorded(self):
        autoscan_config = AutoscanConfig(url="http://autoscan:3030")
        with patch.object(main, "trigger_autoscan", return_value=False):
            result = JobResult("movers", scan_dirs={"/a"})
            main.scan_changed(self._settings(autoscan_config), result)
        assert [c.status for c in result.changes] == ["failed"]

    def test_plex_refresh_when_autoscan_not_configured(self):
        autoscan, plex = self._main(self._settings(None), {"/a"}, {"Movies"})
        autoscan.assert_not_called()
        plex.assert_called_once()
        # the mock refreshed nothing, so the library is recorded as failed
        assert self.result.changes == [Change("PLEX", "refresh", "Movies", [], "failed")]


class TestMainRemove:
    def _main(self, argv):
        arr_config = ArrConfig(baseurl="http://arr", apikey="key", tag=None)
        settings = ConfigSettings(plex=None, radarr=arr_config, sonarr=arr_config)
        with (
            patch.object(main.config, "load_config", return_value=settings),
            patch.object(main.logging.config, "dictConfig"),
            patch.object(main, "run_arr") as run_arr,
            patch.object(main, "run_removers") as run_removers,
        ):
            main.main(argv)
        return run_arr, run_removers

    def test_remove_flag_runs_only_removers(self):
        run_arr, run_removers = self._main(["--remove"])
        run_arr.assert_not_called()
        run_removers.assert_called_once()

    def test_default_does_not_run_removers(self):
        run_arr, run_removers = self._main([])
        assert run_arr.call_count == 2
        run_removers.assert_not_called()


class TestMainEmptyTrash:
    def _main(self, argv):
        arr_config = ArrConfig(baseurl="http://arr", apikey="key", tag=None)
        settings = ConfigSettings(plex=None, radarr=arr_config, sonarr=arr_config)
        with (
            patch.object(main.config, "load_config", return_value=settings),
            patch.object(main.logging.config, "dictConfig"),
            patch.object(main, "run_arr") as run_arr,
            patch.object(main, "run_removers") as run_removers,
            patch.object(main, "empty_plex_trash") as empty_trash,
        ):
            main.main(argv)
        return run_arr, run_removers, empty_trash, settings

    def test_empty_trash_flag_runs_only_empty_trash(self):
        run_arr, run_removers, empty_trash, settings = self._main(["--empty-trash"])
        run_arr.assert_not_called()
        run_removers.assert_not_called()
        empty_trash.assert_called_once_with(settings.plex, settings.empty_trash, ANY, None)

    def test_default_does_not_empty_trash(self):
        _, _, empty_trash, _ = self._main([])
        empty_trash.assert_not_called()

    def test_remove_and_empty_trash_are_exclusive(self):
        with pytest.raises(SystemExit):
            main.parse_args(["--remove", "--empty-trash"])


class TestRunJob:
    def test_unknown_job_raises(self):
        with pytest.raises(ValueError, match="unknown job"):
            main.run_job("nope", ConfigSettings(plex=None, radarr=None, sonarr=None))

    def test_removers_without_sonarr_is_an_error(self):
        result = main.run_job(
            "removers", ConfigSettings(plex=None, radarr=None, sonarr=None)
        )
        assert result.errors == ["SONARR not configured"]

    def test_movers_without_arrs_is_not_an_error(self):
        result = main.run_job("movers", ConfigSettings(plex=None, radarr=None, sonarr=None))
        assert result == JobResult("movers")
