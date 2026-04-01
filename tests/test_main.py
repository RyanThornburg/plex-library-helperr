"""Tests for main.py"""

from unittest.mock import MagicMock

from config.models import MoverRule, ProfileRule
from main import (
    get_matches_based_on_mover_rules,
    get_media_id,
    get_media_matching_profile_rules,
    is_path_valid,
    is_profile_valid,
)

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
# get_media_id
# ---------------------------------------------------------------------------


def make_media(tvdb_id=None, tmdb_id=None, imdb_id=None, media_id=1):
    m = MagicMock()
    m.id = media_id
    m.tvdbId = tvdb_id
    m.tmdbId = tmdb_id
    m.imdbId = imdb_id
    return m


class TestGetMediaId:
    def test_sonarr_returns_tvdb_id(self):
        media = make_media(tvdb_id=76320)
        assert get_media_id(media, "sonarr") == 76320

    def test_sonarr_falls_back_to_id_when_no_tvdb(self):
        media = make_media(tvdb_id=None, media_id=738)
        assert get_media_id(media, "SONARR") == 738

    def test_radarr_returns_tmdb_id(self):
        media = make_media(tmdb_id=329, media_id=485)
        assert get_media_id(media, "radarr") == 329

    def test_radarr_falls_back_to_imdb_when_no_tmdb(self):
        media = make_media(tmdb_id=None, imdb_id="tt0107290", media_id=485)
        assert get_media_id(media, "RADARR") == "tt0107290"

    def test_radarr_falls_back_to_id_when_no_tmdb_or_imdb(self):
        media = make_media(tmdb_id=None, imdb_id=None, media_id=485)
        assert get_media_id(media, "radarr") == 485


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
        assert result == [series.tvdbId]

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
        assert result == [series.tvdbId]

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
        assert result == [series.tvdbId]

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
        assert result == [series.tvdbId]

    def test_certification_match_returns_tvdb_id(self):
        mover = MoverRule(path=self.TARGET, certifications=["TV-Y7"])
        series = make_series(certification="TV-Y7")
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series.tvdbId]

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
        assert result == [series.tvdbId]

    def test_tag_match_all_returns_tvdb_id_when_all_present(self):
        mover = MoverRule(
            path=self.TARGET, tags=["animated", "superhero"], tag_match="all"
        )
        series = make_series(tags=["animated", "superhero", "dc"])
        result = get_matches_based_on_mover_rules(
            mover, [series], self._root_folders(), self.LOG
        )
        assert result == [series.tvdbId]

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
        assert result == [jl.tvdbId]

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
        assert result == [movie.id]

    def test_studio_match_returns_movie_id(self):
        mover = MoverRule(path=self.TARGET, studios=["Universal Pictures"])
        movie = make_movie()
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie.id]

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
        assert result == [movie.id]

    def test_certification_match_returns_movie_id(self):
        mover = MoverRule(path=self.TARGET, certifications=["PG"])
        movie = make_movie(certification="PG")
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [movie.id]

    def test_radarr_uses_movie_id_not_tvdb(self):
        """RADARR log_name means we always use fm.id, not tvdbId."""
        mover = MoverRule(path=self.TARGET, genres=["Action"])
        movie = make_movie(id=485, tvdbId=None)
        result = get_matches_based_on_mover_rules(
            mover, [movie], self._root_folders(), self.LOG
        )
        assert result == [485]

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
        assert result == [series.tvdbId]

    def test_title_match_returns_tvdb_id(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Justice League"])
        series = make_series()
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series.tvdbId]

    def test_title_match_is_case_insensitive(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["justice league"])
        series = make_series(title="Justice League")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series.tvdbId]

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
        assert result == [jl.tvdbId]


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
        assert result == [movie.id]

    def test_studio_match_is_case_insensitive(self):
        rule = ProfileRule(profile=self.PROFILE, studios=["universal pictures"])
        movie = make_movie(studio="Universal Pictures")
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie.id]

    def test_title_match_returns_movie_id(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Jurassic Park"])
        movie = make_movie()
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [movie.id]

    def test_radarr_uses_movie_id(self):
        rule = ProfileRule(profile=self.PROFILE, titles=["Jurassic Park"])
        movie = make_movie(id=485, tvdbId=None)
        result = get_media_matching_profile_rules(
            rule, [movie], self._quality_profiles(), self.LOG
        )
        assert result == [485]

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
        assert result == [movie.id]

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
        assert result == [series.tvdbId]

    def test_studio_match_returns_tvdb_id(self):
        rule = ProfileRule(profile=self.PROFILE, studios=["Warner Bros"])
        series = make_series(studio="Warner Bros")
        result = get_media_matching_profile_rules(
            rule, [series], self._quality_profiles(), self.LOG
        )
        assert result == [series.tvdbId]

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
        assert result == [series.tvdbId]

    def test_empty_media_list_returns_empty(self):
        rule = ProfileRule(profile=self.PROFILE, networks=["Cartoon Network"])
        result = get_media_matching_profile_rules(
            rule, [], self._quality_profiles(), self.LOG
        )
        assert result == []
