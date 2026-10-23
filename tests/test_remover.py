"""Tests for remover.py"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

import remover
from config.models import ArrConfig, RemoverRule, RemoverSettings
from remover import (
    InvalidSeriesData,
    match_remover,
    parse_series_data,
    plan_series_removal,
    run_removers,
    series_watches,
)
from results import Change, JobResult
from watch_history import WatchEvent

NOW = datetime(2026, 10, 1)
OLD = "2025-01-01T00:00:00Z"
NEW = "2026-09-25T00:00:00Z"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tag(label: str) -> MagicMock:
    t = MagicMock()
    t.label = label
    return t


def make_series(**kwargs) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", 1)
    s.tvdbId = kwargs.get("tvdbId", 100)
    s.title = kwargs.get("title", "Survivor")
    s.added = kwargs.get("added", datetime(2020, 1, 1))
    s.tags = [make_tag(t) for t in kwargs.get("tags", ["rm-seasons-2"])]
    return s


def seasons_rule(**kwargs) -> RemoverRule:
    return RemoverRule(
        **{
            "type": "seasons",
            "keep": 2,
            "tags": ["rm-seasons-2"],
            "min_age": timedelta(days=180),
            **kwargs,
        }
    )


def episodes_rule(**kwargs) -> RemoverRule:
    return RemoverRule(
        **{
            "type": "episodes",
            "keep": 3,
            "tags": ["rm-keep-3"],
            "min_age": timedelta(days=30),
            **kwargs,
        }
    )


def build_raw(layout: dict[int, int], date_added: dict[int, str] | None = None):
    """layout {season: episode_count}, every episode has its own file"""
    episodes, files = [], []
    next_id = 1
    for season, count in layout.items():
        for ep in range(1, count + 1):
            files.append(
                {
                    "id": next_id,
                    "seasonNumber": season,
                    "dateAdded": (date_added or {}).get(season, OLD),
                    "path": f"/tv/Show/Season {season:02d}/S{season:02d}E{ep:02d}.mkv",
                }
            )
            episodes.append(
                {
                    "id": next_id,
                    "seasonNumber": season,
                    "episodeNumber": ep,
                    "hasFile": True,
                    "episodeFileId": next_id,
                }
            )
            next_id += 1
    return episodes, files


def plan(rule, layout, date_added=None, watches=None, raw=None):
    episodes, files = parse_series_data(*(raw or build_raw(layout, date_added)))
    return plan_series_removal(rule, make_series(), episodes, files, watches or [], NOW)


def watch(season: int, episode: int, days_ago: int = 1) -> WatchEvent:
    return WatchEvent(100, "Survivor", season, episode, NOW - timedelta(days=days_ago))


# ---------------------------------------------------------------------------
# parse_series_data
# ---------------------------------------------------------------------------


class TestParseSeriesData:
    def test_valid_data(self):
        episodes, files = parse_series_data(*build_raw({1: 2}))
        assert len(episodes) == 2
        assert files[1].date_added == datetime(2025, 1, 1)

    def test_fractional_seconds_parse(self):
        eps, raw_files = build_raw({1: 1})
        raw_files[0]["dateAdded"] = "2025-01-01T12:00:00.1234567Z"
        _, files = parse_series_data(eps, raw_files)
        assert files[1].date_added.hour == 12

    def test_missing_date_added_raises(self):
        eps, raw_files = build_raw({1: 1})
        del raw_files[0]["dateAdded"]
        with pytest.raises(InvalidSeriesData):
            parse_series_data(eps, raw_files)

    def test_bad_date_added_raises(self):
        eps, raw_files = build_raw({1: 1})
        raw_files[0]["dateAdded"] = "yesterday"
        with pytest.raises(InvalidSeriesData):
            parse_series_data(eps, raw_files)

    def test_episode_referencing_missing_file_raises(self):
        eps, raw_files = build_raw({1: 2})
        with pytest.raises(InvalidSeriesData, match="missing file"):
            parse_series_data(eps, raw_files[:1])

    def test_missing_episode_number_raises(self):
        eps, raw_files = build_raw({1: 1})
        eps[0]["episodeNumber"] = None
        with pytest.raises(InvalidSeriesData):
            parse_series_data(eps, raw_files)

    def test_episode_without_file_is_allowed(self):
        eps, raw_files = build_raw({1: 1})
        eps.append({"id": 9, "seasonNumber": 2, "episodeNumber": 1, "hasFile": False})
        episodes, _ = parse_series_data(eps, raw_files)
        assert episodes[-1].file_id is None


# ---------------------------------------------------------------------------
# seasons
# ---------------------------------------------------------------------------


class TestPlanSeasons:
    def test_keeps_latest_seasons(self):
        p = plan(seasons_rule(), {1: 2, 2: 2, 3: 2, 4: 2})
        assert p.labels == ["S01", "S02"]
        assert p.file_ids == [1, 2, 3, 4]
        assert p.episode_ids == [1, 2, 3, 4]
        assert p.dirs == {"/tv/Show/Season 01", "/tv/Show/Season 02"}

    def test_nothing_when_at_or_under_keep(self):
        assert plan(seasons_rule(), {1: 2, 2: 2}) is None

    def test_season_with_any_new_file_is_kept(self):
        raw = build_raw({1: 2, 2: 2, 3: 2, 4: 2})
        raw[1][0]["dateAdded"] = NEW  # one recent file in S01
        p = plan(seasons_rule(), None, raw=raw)
        assert p.labels == ["S02"]

    def test_seasons_without_files_do_not_count_toward_keep(self):
        eps, files = build_raw({1: 2, 2: 2, 3: 2})
        # announced season 4 with no files yet
        eps.append({"id": 50, "seasonNumber": 4, "episodeNumber": 1, "hasFile": False})
        p = plan(seasons_rule(), None, raw=(eps, files))
        assert p.labels == ["S01"]

    def test_specials_ignored_by_default(self):
        p = plan(seasons_rule(), {0: 2, 1: 2, 2: 2, 3: 2})
        assert p.labels == ["S01"]

    def test_specials_removable_when_included(self):
        p = plan(seasons_rule(include_specials=True), {0: 2, 1: 2, 2: 2})
        assert p.labels == ["S00"]

    def test_specials_never_count_toward_keep(self):
        # specials included, 2 numbered seasons: both numbered seasons are kept
        p = plan(seasons_rule(include_specials=True), {0: 1, 1: 1, 2: 1})
        assert p.labels == ["S00"]


class TestSeasonWatchProtection:
    rule = seasons_rule(skip_if_watched_within=timedelta(days=14))
    layout = {s: 2 for s in range(1, 9)}

    def test_binge_protects_watched_season_and_later(self):
        p = plan(self.rule, self.layout, watches=[watch(3, 1)])
        assert p.labels == ["S01", "S02"]

    def test_earliest_viewer_wins(self):
        p = plan(self.rule, self.layout, watches=[watch(5, 1), watch(2, 2)])
        assert p.labels == ["S01"]

    def test_watching_kept_seasons_does_not_block(self):
        p = plan(self.rule, self.layout, watches=[watch(8, 1), watch(7, 2)])
        assert p.labels == ["S01", "S02", "S03", "S04", "S05", "S06"]

    def test_old_watch_does_not_protect(self):
        p = plan(self.rule, self.layout, watches=[watch(3, 1, days_ago=20)])
        assert len(p.labels) == 6

    def test_watch_on_new_season_protects_older_seasons_after_it(self):
        # S03 was just re-downloaded and is being watched, S04-S06 are old
        p = plan(self.rule, self.layout, date_added={3: NEW}, watches=[watch(3, 1)])
        assert p.labels == ["S01", "S02"]

    def test_no_window_ignores_watches(self):
        p = plan(seasons_rule(), self.layout, watches=[watch(1, 1)])
        assert p.labels[0] == "S01"


# ---------------------------------------------------------------------------
# episodes
# ---------------------------------------------------------------------------


class TestPlanEpisodes:
    def test_keeps_latest_episodes_across_seasons(self):
        p = plan(episodes_rule(), {1: 3, 2: 2})
        assert p.labels == ["S01E01", "S01E02"]
        assert p.file_ids == [1, 2]

    def test_new_files_are_kept(self):
        raw = build_raw({1: 5})
        raw[1][0]["dateAdded"] = NEW
        p = plan(episodes_rule(), None, raw=raw)
        assert p.labels == ["S01E02"]

    def test_multi_episode_file_in_keep_window_is_kept(self):
        eps, files = build_raw({1: 4})
        # E02-E03 share file 2, keep=3 keeps E02..E04, so only E01 goes
        eps[2]["episodeFileId"] = 2
        files = [f for f in files if f["id"] != 3]
        p = plan(episodes_rule(), None, raw=(eps, files))
        assert p.file_ids == [1]

    def test_multi_episode_file_straddling_window_is_kept(self):
        eps, files = build_raw({1: 5})
        # E02-E03 share file 2, keep=3 keeps E03..E05, file 2 holds kept E03
        eps[2]["episodeFileId"] = 2
        files = [f for f in files if f["id"] != 3]
        p = plan(episodes_rule(), None, raw=(eps, files))
        assert p.file_ids == [1]

    def test_multi_episode_file_removed_unmonitors_both(self):
        eps, files = build_raw({1: 5})
        eps[1]["episodeFileId"] = 1
        files = [f for f in files if f["id"] != 2]
        p = plan(episodes_rule(), None, raw=(eps, files))
        assert p.file_ids == [1]
        assert p.episode_ids == [1, 2]

    def test_specials_ignored_by_default(self):
        p = plan(episodes_rule(), {0: 2, 1: 4})
        assert p.labels == ["S01E01"]

    def test_watch_protects_from_watched_episode_on(self):
        rule = episodes_rule(skip_if_watched_within=timedelta(days=7))
        p = plan(rule, {1: 10}, watches=[watch(1, 4)])
        assert p.labels == ["S01E01", "S01E02", "S01E03"]

    def test_watching_latest_episodes_does_not_block(self):
        rule = episodes_rule(skip_if_watched_within=timedelta(days=7))
        p = plan(rule, {1: 10}, watches=[watch(1, 9), watch(1, 10)])
        assert len(p.labels) == 7

    def test_watched_special_only_protects_specials(self):
        rule = episodes_rule(include_specials=True, skip_if_watched_within=timedelta(days=7))
        p = plan(rule, {0: 2, 1: 5}, watches=[watch(0, 1)])
        assert p.labels == ["S01E01", "S01E02"]


# ---------------------------------------------------------------------------
# tag matching / watch matching
# ---------------------------------------------------------------------------


class TestMatchRemover:
    removers = [seasons_rule(exclude_tags=["keep-forever"]), episodes_rule()]

    def test_matches_by_tag_case_insensitive(self):
        assert match_remover(make_series(tags=["RM-Seasons-2"]), self.removers) is self.removers[0]

    def test_no_tag_no_match(self):
        assert match_remover(make_series(tags=["other"]), self.removers) is None

    def test_exclude_tag_skips(self):
        series = make_series(tags=["rm-seasons-2", "keep-forever"])
        assert match_remover(series, self.removers) is None

    def test_two_removers_is_error_and_skipped(self, caplog):
        series = make_series(tags=["rm-seasons-2", "rm-keep-3"])
        assert match_remover(series, self.removers) is None
        assert "2 removers" in caplog.text


class TestSeriesWatches:
    def test_matches_tvdb_then_title_fallback(self):
        watches = [
            WatchEvent(100, "Different Title", 1, 1, NOW),
            WatchEvent(None, "survivor", 1, 2, NOW),
            WatchEvent(200, "Survivor", 1, 3, NOW),
            WatchEvent(None, "Other", 1, 4, NOW),
        ]
        assert [w.episode for w in series_watches(make_series(), watches)] == [1, 2]

    def test_series_without_tvdb_only_matches_title(self):
        watches = [WatchEvent(None, "Other", 1, 1, NOW)]
        assert series_watches(make_series(tvdbId=None), watches) == []


# ---------------------------------------------------------------------------
# run_removers
# ---------------------------------------------------------------------------


class TestRunRemovers:
    def setup_method(self):
        self.result = JobResult("removers")
        self.client = MagicMock()
        self.client.queued_series_ids.return_value = set()
        self.client.episodes.side_effect = lambda _id: build_raw({1: 1, 2: 1, 3: 1})[0]
        self.client.episode_files.side_effect = lambda _id: build_raw({1: 1, 2: 1, 3: 1})[1]

    def _run(self, series, rule=None, settings=None, watches=None):
        arr = MagicMock()
        arr.all_series.return_value = series
        arr_config = ArrConfig(
            baseurl="http://sonarr",
            apikey="key",
            tag=None,
            removers=[rule or seasons_rule(plex_library="Reality")],
            remover_settings=settings or RemoverSettings(dry_run=False),
        )
        with (
            patch.object(remover, "SonarrAPI", return_value=arr),
            patch.object(remover, "SonarrEpisodes", return_value=self.client),
            patch.object(remover, "get_watch_history", return_value=watches) as wh,
            patch.object(remover, "utc_now", return_value=NOW),
        ):
            run_removers(arr_config, None, None, self.result)
        return wh

    def test_unmonitors_then_deletes(self):
        calls = MagicMock()
        self.client.unmonitor_episodes.side_effect = lambda ids: calls("unmonitor", ids)
        self.client.delete_episode_files.side_effect = lambda ids: calls("delete", ids)
        self._run([make_series()])
        assert [c.args for c in calls.call_args_list] == [("unmonitor", [1]), ("delete", [1])]
        assert self.result.scan_dirs == {"/tv/Show/Season 01"}
        assert self.result.plex_libraries == {"Reality"}
        assert self.result.changes == [
            Change("SONARR", "remove", "Survivor", ["S01"], "done", "1 files")
        ]

    def test_dry_run_changes_nothing(self):
        self._run([make_series()], settings=RemoverSettings(dry_run=True))
        self.client.unmonitor_episodes.assert_not_called()
        self.client.delete_episode_files.assert_not_called()
        assert self.result.scan_dirs == set()
        assert [c.status for c in self.result.changes] == ["dry_run"]

    def test_max_deletes_blocks_everything(self):
        series = [make_series(id=1), make_series(id=2)]
        self._run(series, settings=RemoverSettings(dry_run=False, max_deletes=1))
        self.client.delete_episode_files.assert_not_called()
        assert [c.status for c in self.result.changes] == ["blocked", "blocked"]
        assert "exceeds max_deletes (1)" in self.result.changes[0].detail

    def test_recently_added_series_skipped(self):
        self._run([make_series(added=NOW - timedelta(days=10))])
        self.client.episodes.assert_not_called()

    def test_missing_added_date_skipped(self):
        self._run([make_series(added=None)])
        self.client.episodes.assert_not_called()

    def test_queued_series_skipped(self):
        self.client.queued_series_ids.return_value = {1}
        self._run([make_series()])
        self.client.episodes.assert_not_called()

    def test_invalid_data_skips_only_that_series(self):
        good = build_raw({1: 1, 2: 1, 3: 1})
        bad_files = [{**f, "dateAdded": None} for f in good[1]]
        self.client.episode_files.side_effect = lambda sid: bad_files if sid == 1 else good[1]
        self._run([make_series(id=1), make_series(id=2)])
        self.client.delete_episode_files.assert_called_once()

    def test_untagged_series_ignored(self):
        self._run([make_series(tags=["other"])])
        self.client.queued_series_ids.assert_not_called()

    def test_unmonitor_failure_skips_delete(self):
        self.client.unmonitor_episodes.side_effect = requests.exceptions.HTTPError("500")
        self._run([make_series()])
        self.client.delete_episode_files.assert_not_called()
        assert self.result.scan_dirs == set()
        assert self.result.changes[0].status == "failed"
        assert "unmonitor failed" in self.result.changes[0].detail

    def test_delete_failure_records_unmonitored(self):
        self.client.delete_episode_files.side_effect = requests.exceptions.HTTPError("500")
        self._run([make_series()])
        assert self.result.scan_dirs == set()
        assert self.result.changes[0].status == "failed"
        assert "unmonitored, but delete failed" in self.result.changes[0].detail

    def test_watch_rule_skipped_when_history_unavailable(self):
        rule = seasons_rule(skip_if_watched_within=timedelta(days=14))
        wh = self._run([make_series()], rule=rule, watches=None)
        wh.assert_called_once()
        self.client.episodes.assert_not_called()
        assert len(self.result.errors) == 1

    def test_watch_history_fetched_for_longest_window(self):
        rule = seasons_rule(skip_if_watched_within=timedelta(days=14))
        wh = self._run([make_series()], rule=rule, watches=[watch(1, 1)])
        assert wh.call_args.args[2] == NOW - timedelta(days=14)
        # S01 watched recently, so S01 and everything after it is protected
        self.client.delete_episode_files.assert_not_called()

    def test_no_watch_history_needed_without_window(self):
        wh = self._run([make_series()])
        wh.assert_not_called()
