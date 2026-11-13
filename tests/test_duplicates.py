"""Tests for duplicates: planning which plex versions aren't the arr's file, and applying
a reviewed plan"""

from datetime import datetime, timedelta
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

import pytest
from plexapi import exceptions as plexapi_exceptions

import duplicates
from config import build_config
from config.models import ArrConfig, ConfigSettings, DuplicatesSettings, PlexConfig
from duplicates import PlexItem, Tracked, Version, parse_item, plan_item, run_duplicates
from results import JobResult

NOW = datetime(2026, 11, 13, 12)
OLD = NOW - timedelta(days=30)
SETTINGS = DuplicatesSettings()
MIN = 60 * 1000


def version(media_id, name, duration=44 * MIN, size=2 * 1024**3, folder="/mnt/nfs/tv/Show/Season 01"):
    return Version(media_id, (f"{folder}/{name}",), size, duration, "1080p", name.rsplit(".", 1)[-1])


def episode(*versions, rating_key=100, season=1, number=2):
    return PlexItem(rating_key, "episode", f"Show S{season:02d}E{number:02d}", "TV Shows", list(versions),
                    show_key=7, season=season, episode=number)


def movie(*versions, rating_key=200):
    return PlexItem(rating_key, "movie", "Heat (1995)", "Movies", list(versions), tmdb_id=949, imdb_id="tt0113277")


TRACKED = Tracked("Show - S01E02 - Title [WEBDL-1080p].mkv", OLD, False)
NEW_COPY = version(1, "Show - S01E02 - Title [WEBDL-1080p].mkv")
OLD_COPY = version(2, "Show - S01E02 - Title [HDTV-720p].mkv", folder="/mnt/nfs/disk8/tv/Show/Season 01")


class TestPlanItem:
    def test_keeps_the_arrs_file(self):
        plan = plan_item(episode(OLD_COPY, NEW_COPY), TRACKED, SETTINGS, NOW)
        assert (plan.keep, plan.remove) == (NEW_COPY, [OLD_COPY])
        assert [(f.id, f.path) for f in plan.files] == [(2, OLD_COPY.files[0])]

    @pytest.mark.parametrize("tracked, reason", [
        (None, "not found in Sonarr, or Sonarr has no file for it"),
        (Tracked(TRACKED.name, OLD, True), "a download for it is in Sonarr's queue"),
        (Tracked(TRACKED.name, NOW - timedelta(days=1), False), "Sonarr's file was added in the last 3d"),
        (Tracked("Show - S01E02 - Title [Bluray-1080p].mkv", OLD, False), "Sonarr's file isn't one of Plex's versions (Plex may not have scanned it yet)"),
    ])
    def test_skips(self, tracked, reason):
        assert plan_item(episode(OLD_COPY, NEW_COPY), tracked, SETTINGS, NOW) == reason

    def test_two_versions_with_the_arrs_file_name(self):
        twin = version(3, NEW_COPY.files[0].rsplit("/", 1)[1], folder="/mnt/nfs/disk8/tv/Show/Season 01")
        assert plan_item(episode(NEW_COPY, twin), TRACKED, SETTINGS, NOW) == "more than one version is Sonarr's file"

    def test_a_converter_pair_is_still_a_duplicate(self):
        # the october lesson: same name, other extension, same length is a leftover
        mp4 = version(1, "Show - S01E02 - Title [WEBDL-1080p].mp4")
        mkv = version(2, "Show - S01E02 - Title [WEBDL-1080p].mkv", folder="/mnt/nfs/disk8/tv/Show/Season 01")
        plan = plan_item(episode(mkv, mp4), Tracked(mp4.files[0].rsplit("/", 1)[1], OLD, False), SETTINGS, NOW)
        assert plan.remove == [mkv]

    def test_a_much_longer_version_may_be_another_episode(self):
        other = version(2, "Untold - Swamp Kings - S01E02.mkv", duration=80 * MIN)
        assert plan_item(episode(other, NEW_COPY), TRACKED, SETTINGS, NOW) == (
            "a version is 36 min longer than Sonarr's file: it may be another episode, check it in Plex"
        )

    def test_a_little_longer_is_fine(self):
        # under 10% (and 2 minutes) is the same thing, with different credits or ads cut
        longer = version(2, "Show - S01E02 - Title [HDTV-720p].mkv", duration=45 * MIN)
        assert plan_item(episode(longer, NEW_COPY), TRACKED, SETTINGS, NOW).remove == [longer]

    def test_another_cut_of_a_movie(self):
        theatrical = version(1, "Heat (1995) [Bluray-1080p].mkv", duration=170 * MIN)
        extended = version(2, "Heat (1995) Directors Cut.mkv", duration=200 * MIN)
        tracked = Tracked("Heat (1995) [Bluray-1080p].mkv", OLD, False)
        assert "it may be another cut" in plan_item(movie(theatrical, extended), tracked, SETTINGS, NOW)

    def test_a_multi_episode_file_is_left_alone(self):
        double = version(2, "Show - S01E01-E02 - Title.mkv", duration=44 * MIN)
        assert plan_item(episode(double, NEW_COPY), TRACKED, SETTINGS, NOW) == "a version's file holds more than one episode"

    def test_unknown_lengths_dont_block(self):
        unknown = version(2, "Show - S01E02 - old.mkv", duration=None)
        assert plan_item(episode(unknown, NEW_COPY), TRACKED, SETTINGS, NOW).remove == [unknown]


class TestParse:
    def test_movie(self):
        element = fromstring("""
            <Video type="movie" ratingKey="200" title="Heat" year="1995">
              <Media id="11" duration="10200000" videoResolution="1080" container="mkv">
                <Part file="/mnt/nfs/movies/Heat (1995)/Heat.mkv" size="1000"/>
              </Media>
              <Media id="12" duration="10200000" videoResolution="4k" container="mp4">
                <Part file="/mnt/nfs/movies/Heat (1995)/cd1.mp4" size="400"/>
                <Part file="/mnt/nfs/movies/Heat (1995)/cd2.mp4" size="600"/>
              </Media>
              <Guid id="imdb://tt0113277"/><Guid id="tmdb://949"/><Guid id="tvdb://1"/>
            </Video>""")
        item = parse_item(element, "Movies")
        assert (item.title, item.tmdb_id, item.imdb_id, item.kind) == ("Heat (1995)", 949, "tt0113277", "movie")
        assert [(v.media_id, v.size, v.label) for v in item.versions] == [
            (11, 1000, "1080p mkv, 1000 B"), (12, 1000, "4K mp4, 1000 B"),
        ]
        assert item.versions[1].names == {"cd1.mp4", "cd2.mp4"}

    def test_episode(self):
        element = fromstring("""
            <Video type="episode" ratingKey="100" grandparentRatingKey="7" grandparentTitle="Show"
                   parentIndex="1" index="2" title="Title">
              <Media id="1"><Part file="/a.mkv" size="1"/></Media>
            </Video>""")
        item = parse_item(element, "TV Shows")
        assert (item.title, item.show_key, item.season, item.episode) == ("Show S01E02", 7, 1, 2)


# ---------------------------------------------------------------------------
# a run against fake plex and arrs
# ---------------------------------------------------------------------------


class FakePlex:
    def __init__(self, items, fail=None):
        self.items = {i.rating_key: i for i in items}
        self.deleted: list[tuple[int, int]] = []
        self.fail = fail

    def sections(self, libraries):
        return [("1", "movie", "Movies"), ("2", "show", "TV Shows")]

    def duplicates(self, key, kind, title):
        return [i for i in self.items.values() if i.library == title]

    def item(self, rating_key):
        return self.items.get(rating_key)

    def show_tvdb(self, show_key):
        return 121361

    def delete(self, rating_key, media_id):
        if self.fail:
            raise self.fail
        self.deleted.append((rating_key, media_id))


def arr_answers(movie_file="Heat (1995) [Bluray-1080p].mkv", episode_file=TRACKED.name, queued=()):
    def answer(self, method, path, **kwargs):
        if path == "movie":
            return [{"id": 5, "tmdbId": 949, "imdbId": "tt0113277", "hasFile": True,
                     "movieFile": {"path": f"/data/media/movies/Heat (1995)/{movie_file}", "dateAdded": "2026-10-11T21:30:00Z"}}]
        if path == "series":
            return [{"id": 9, "tvdbId": 121361}]
        if path == "queue/details":
            return [{"movieId": m} for m in queued]
        if path == "episodefile":
            return [{"id": 40, "path": f"/data/media/tv/Show/Season 01/{episode_file}", "dateAdded": "2026-10-11T21:30:00Z"},
                    # a stale record sonarr kept: no episode links to it
                    {"id": 41, "path": f"/data/media/tv/Show/Season 01/{OLD_COPY.files[0].rsplit('/', 1)[1]}", "dateAdded": "2026-01-01T00:00:00Z"}]
        if path == "episode":
            return [{"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 40}]
        raise AssertionError(path)
    return answer


HEAT_ARR = version(21, "Heat (1995) [Bluray-1080p].mkv", duration=170 * MIN, folder="/mnt/nfs/movies/Heat (1995)")
HEAT_OLD = version(22, "Heat (1995) {imdb-tt0113277} [Bluray-1080p].mkv", duration=170 * MIN, folder="/mnt/nfs/disk8/movies/Heat (1995)")
CONFIG = ConfigSettings(
    plex=PlexConfig(baseurl="http://plex:32400", token="t"),
    radarr=ArrConfig(baseurl="http://radarr:7878", apikey="k", tag=None),
    sonarr=ArrConfig(baseurl="http://sonarr:8989", apikey="k", tag=None),
)


def run(plex, config=CONFIG, approved=None, answers=None):
    result = JobResult("duplicates")
    with patch.object(duplicates, "setup_plex_server", return_value=object()), \
            patch.object(duplicates, "PlexVersions", return_value=plex), \
            patch.object(duplicates, "utc_now", return_value=NOW), \
            patch.object(duplicates.ArrV3, "_request", answers or arr_answers()):
        run_duplicates(config, result, approved)
    return result


class TestRun:
    def test_plans_for_review_and_deletes_nothing(self):
        plex = FakePlex([movie(HEAT_OLD, HEAT_ARR), episode(OLD_COPY, NEW_COPY)])
        result = run(plex)
        assert plex.deleted == []
        assert [(r.service, r.media_id, r.title, r.status, [f.id for f in r.files]) for r in result.removals] == [
            ("PLEX", 200, "Heat (1995)", "dry_run", [22]),
            ("PLEX", 100, "Show S01E02", "dry_run", [2]),
        ]
        assert result.removals[0].rule == "Movies: keeps Radarr's file (1080p mkv, 2.0 GB)"
        assert not result.errors

    def test_stale_episodefile_records_dont_count(self):
        # sonarr still has a record for the old copy, but no episode links to it
        plex = FakePlex([episode(OLD_COPY, NEW_COPY)])
        [removal] = run(plex).removals
        assert [f.path for f in removal.files] == [OLD_COPY.files[0]]

    def test_an_unconnected_arr_is_an_error(self):
        config = ConfigSettings(plex=CONFIG.plex, radarr=None, sonarr=CONFIG.sonarr)
        result = run(FakePlex([movie(HEAT_OLD, HEAT_ARR)]), config)
        assert result.removals == []
        assert result.errors == ["Radarr isn't connected, so 1 items with more than one version weren't checked"]

    def test_applying_deletes_only_approved_versions_still_duplicates(self):
        third = version(23, "Heat (1995) [DVD].mkv", duration=170 * MIN, folder="/mnt/nfs/movies/Heat (1995)")
        plex = FakePlex([movie(HEAT_OLD, HEAT_ARR, third)])
        result = run(plex, approved={200: {22}})
        assert plex.deleted == [(200, 22)]
        [removal] = result.removals
        assert (removal.status, [f.id for f in removal.files]) == ("done", [22])

    @pytest.mark.parametrize("items, approved, reason", [
        ([], {200: {22}}, "no longer in Plex"),
        ([movie(HEAT_ARR)], {200: {22}}, "it has one version now"),
        ([movie(HEAT_OLD, HEAT_ARR)], {200: {21}}, "the approved versions aren't duplicates any more"),
    ])
    def test_applying_skips_what_changed(self, items, approved, reason):
        plex = FakePlex(items)
        result = run(plex, approved=approved)
        assert plex.deleted == []
        assert [(c.status, c.detail) for c in result.changes] == [("blocked", reason)]

    def test_applying_when_the_arr_replaced_its_file(self):
        plex = FakePlex([movie(HEAT_OLD, HEAT_ARR)])
        result = run(plex, approved={200: {22}}, answers=arr_answers(movie_file="Heat (1995) [Remux-2160p].mkv"))
        assert plex.deleted == []
        assert result.changes[0].detail == "Radarr's file isn't one of Plex's versions (Plex may not have scanned it yet)"

    def test_max_deletes(self):
        config = ConfigSettings(plex=CONFIG.plex, radarr=CONFIG.radarr, sonarr=CONFIG.sonarr,
                                duplicates=DuplicatesSettings(max_deletes=1))
        plex = FakePlex([movie(HEAT_OLD, HEAT_ARR), episode(OLD_COPY, NEW_COPY)])
        result = run(plex, config, approved={200: {22}, 100: {2}})
        assert plex.deleted == []
        assert {r.status for r in result.removals} == {"blocked"}

    def test_plex_without_media_deletion(self):
        plex = FakePlex([movie(HEAT_OLD, HEAT_ARR)], fail=plexapi_exceptions.Unauthorized("(401) unauthorized"))
        [removal] = run(plex, approved={200: {22}}).removals
        assert (removal.status, removal.detail) == ("failed", "Plex refused: turn on Settings → Library → Allow media deletion in Plex")

    def test_without_plex(self):
        result = JobResult("duplicates")
        run_duplicates(ConfigSettings(plex=None, radarr=None, sonarr=None), result)
        assert result.errors == ["Plex isn't connected"]


class TestConfig:
    def test_defaults(self):
        assert build_config({}).duplicates == DuplicatesSettings(libraries=[], min_age=timedelta(days=3), max_deletes=100)

    def test_set(self):
        settings = build_config({"plex": {"duplicates": {"libraries": ["TV Shows"], "min_age": "12h", "max_deletes": 5}}})
        assert settings.duplicates == DuplicatesSettings(["TV Shows"], timedelta(hours=12), 5)

    @pytest.mark.parametrize("section, error", [
        ({"min_age": "soon"}, "invalid duration"),
        ({"min_age": "30m"}, "at least 1h"),
        ({"max_deletes": 0}, ">= 1"),
    ])
    def test_invalid(self, section, error):
        with pytest.raises(ValueError, match=error):
            build_config({"plex": {"duplicates": section}})
