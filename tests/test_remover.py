"""Tests for remover.py"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

import remover
from config.models import ArrConfig, RemoverRule, RemoverSettings
from remover import (
    InvalidSeriesData,
    first_imports,
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


def plan(rule, layout, date_added=None, watches=None, raw=None, imported=None):
    episodes, files = parse_series_data(*(raw or build_raw(layout, date_added)))
    return plan_series_removal(
        rule, make_series(), episodes, files, watches or [], NOW, imported
    )


def history(episode_id: int, date: str, event: str = "downloadFolderImported", reason=None):
    return {
        "episodeId": episode_id,
        "date": date,
        "eventType": event,
        "data": {"reason": reason} if reason else {},
    }


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


class TestFirstImports:
    def test_refound_and_upgraded_files_keep_the_first_import(self):
        imported = first_imports(
            [
                history(1, "2026-09-18T09:28:00Z"),
                history(1, "2026-09-18T10:28:45Z", "episodeFileDeleted", "MissingFromDisk"),
                history(1, "2026-09-19T09:00:00Z", "episodeFileDeleted", "Upgrade"),
                history(1, "2026-09-19T09:00:00Z"),
            ]
        )
        assert imported == {1: datetime(2026, 9, 18, 9, 28)}

    def test_deleting_on_purpose_resets_the_age(self):
        imported = first_imports(
            [
                history(1, "2026-09-20T00:00:00Z"),  # out of order, sorted by date
                history(1, "2026-08-01T00:00:00Z"),
                history(1, "2026-08-02T00:00:00Z", "episodeFileDeleted", "Manual"),
            ]
        )
        assert imported == {1: datetime(2026, 9, 20)}

    def test_other_events_are_ignored(self):
        assert first_imports([{"eventType": "grabbed", "date": None}]) == {}

    def test_bad_import_raises(self):
        with pytest.raises(InvalidSeriesData):
            first_imports([history(1, "yesterday")])


class TestAgeFromFirstImport:
    def test_refound_file_ages_from_first_import(self):
        raw = build_raw({1: 5}, {1: NEW})
        p = plan(episodes_rule(), None, raw=raw, imported={1: datetime(2025, 1, 1)})
        assert p.labels == ["S01E01"]

    def test_newer_first_import_does_not_make_an_old_file_new(self):
        p = plan(episodes_rule(), {1: 5}, imported={1: datetime(2026, 9, 30)})
        assert p.labels == ["S01E01", "S01E02"]

    def test_multi_episode_file_ages_from_its_newest_episode(self):
        eps, files = build_raw({1: 6}, {1: NEW})
        eps[1]["episodeFileId"] = 1  # E01-E02 share file 1
        files = [f for f in files if f["id"] != 2]
        old, new = datetime(2025, 1, 1), datetime(2026, 9, 30)
        assert plan(episodes_rule(), None, raw=(eps, files), imported={1: old, 2: new}) is None
        p = plan(episodes_rule(), None, raw=(eps, files), imported={1: old, 2: old})
        assert p.file_ids == [1]

    def test_episode_without_import_uses_the_file_date(self):
        eps, files = build_raw({1: 6}, {1: NEW})
        eps[1]["episodeFileId"] = 1
        files = [f for f in files if f["id"] != 2]
        imported = {1: datetime(2025, 1, 1)}
        assert plan(episodes_rule(), None, raw=(eps, files), imported=imported) is None


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


class SonarrFakes:
    """sonarr fakes for run_removers: series with S01-S03, one file each (ids 1-3)"""

    def setup_method(self):
        self.result = JobResult("removers")
        self.client = MagicMock()
        self.client.queued_series_ids.return_value = set()
        self.client.episodes.side_effect = lambda _id: build_raw({1: 1, 2: 1, 3: 1})[0]
        self.client.episode_files.side_effect = lambda _id: build_raw({1: 1, 2: 1, 3: 1})[1]
        self.client.history.return_value = []

    def _run(self, series, rule=None, settings=None, watches=None, auto_approve=True):
        """auto_approve: the rule deletes, otherwise it plans for review"""
        arr = MagicMock()
        arr.all_series.return_value = series
        rule = rule or seasons_rule(plex_library="Reality")
        arr_config = ArrConfig(
            baseurl="http://sonarr",
            apikey="key",
            tag=None,
            removers=[rule],
            remover_settings=settings or RemoverSettings(),
        )
        with (
            patch.object(remover, "SonarrAPI", return_value=arr),
            patch.object(remover, "SonarrEpisodes", return_value=self.client),
            patch.object(remover, "get_watch_history", return_value=watches) as wh,
            patch.object(remover, "utc_now", return_value=NOW),
        ):
            run_removers(
                arr_config,
                None,
                None,
                self.result,
                auto_approve=frozenset({rule.key}) if auto_approve else frozenset(),
            )
        return wh


class TestRunRemovers(SonarrFakes):
    def test_unmonitors_then_deletes(self):
        calls = MagicMock()
        self.client.unmonitor_episodes.side_effect = lambda ids: calls("unmonitor", ids)
        self.client.delete_episode_files.side_effect = lambda ids: calls("delete", ids)
        self._run([make_series()])
        assert [c.args for c in calls.call_args_list] == [("unmonitor", [1]), ("delete", [1])]
        assert self.result.scan_dirs == {"/tv/Show/Season 01"}
        assert self.result.plex_libraries == {"Reality"}
        assert self.result.changes == [
            Change("SONARR", "remove", "Survivor", ["S01"], "done", "1 files, auto-approved")
        ]

    def test_unapproved_changes_nothing(self):
        self._run([make_series()], auto_approve=False)
        self.client.unmonitor_episodes.assert_not_called()
        self.client.delete_episode_files.assert_not_called()
        assert self.result.scan_dirs == set()
        assert [c.status for c in self.result.changes] == ["dry_run"]

    def test_max_deletes_blocks_everything(self):
        series = [make_series(id=1), make_series(id=2)]
        self._run(series, settings=RemoverSettings(max_deletes=1))
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

    def test_history_read_only_when_a_candidate_is_too_new(self):
        self._run([make_series()])
        self.client.history.assert_not_called()

    def test_refound_file_removed_by_its_first_import(self):
        self.client.episode_files.side_effect = lambda _id: build_raw(
            {1: 1, 2: 1, 3: 1}, {1: NEW}
        )[1]
        self.client.history.return_value = [
            history(1, OLD),
            history(1, "2026-09-24T00:00:00Z", "episodeFileDeleted", "MissingFromDisk"),
        ]
        self._run([make_series()])
        self.client.history.assert_called_once_with(1)
        self.client.delete_episode_files.assert_called_once_with([1])

    def test_invalid_history_skips_the_series(self):
        self.client.episode_files.side_effect = lambda _id: build_raw(
            {1: 1, 2: 1, 3: 1}, {1: NEW}
        )[1]
        self.client.history.return_value = [history(1, "garbage")]
        self._run([make_series()])
        self.client.delete_episode_files.assert_not_called()


# ---------------------------------------------------------------------------
# applying an approved plan
# ---------------------------------------------------------------------------


class TestApplyPlan(SonarrFakes):
    """run_removers with `approved`. With keep 2, S01 (file 1) is planned"""

    def _apply(self, series, approved, **kwargs):
        arr = MagicMock()
        arr.all_series.return_value = series
        arr_config = ArrConfig(
            baseurl="http://sonarr",
            apikey="key",
            tag=None,
            removers=[kwargs.get("rule") or seasons_rule()],
            remover_settings=kwargs.get("settings") or RemoverSettings(),
        )
        with (
            patch.object(remover, "SonarrAPI", return_value=arr),
            patch.object(remover, "SonarrEpisodes", return_value=self.client),
            patch.object(remover, "get_watch_history", return_value=kwargs.get("watches")),
            patch.object(remover, "utc_now", return_value=NOW),
        ):
            run_removers(arr_config, None, None, self.result, approved)

    def test_removes_approved(self):
        self._apply([make_series()], {1: {1}})
        self.client.unmonitor_episodes.assert_called_once_with([1])
        self.client.delete_episode_files.assert_called_once_with([1])
        assert [c.status for c in self.result.changes] == ["done"]

    def test_records_removed_files(self):
        self._apply([make_series()], {1: {1}})
        [removal] = self.result.removals
        assert removal.media_id == 1
        assert removal.labels == ["S01"]
        assert [(f.id, f.path) for f in removal.files] == [(1, "/tv/Show/Season 01/S01E01.mkv")]
        assert removal.rule == "seasons, keep 2 (rm-seasons-2)"
        assert removal.status == "done"

    def test_only_approved_series_are_touched(self):
        self._apply([make_series(id=1), make_series(id=2, title="Other")], {2: {1}})
        self.client.episodes.assert_called_once_with(2)
        assert [c.target for c in self.result.changes] == ["Other"]

    def test_unapproved_newly_eligible_units_are_kept(self):
        # S01 and S02 are planned now (keep 1), only S01's file was approved
        self._apply([make_series()], {1: {1}}, rule=seasons_rule(keep=1))
        self.client.delete_episode_files.assert_called_once_with([1])

    def test_season_with_a_replaced_file_is_kept_whole(self):
        # file 9 was approved but sonarr now has file 1 for that season (an upgrade)
        self._apply([make_series()], {1: {9}})
        self.client.delete_episode_files.assert_not_called()
        [change] = self.result.changes
        assert change.status == "blocked"
        assert "1 of 1 approved files no longer eligible" in change.detail

    def test_partly_eligible_series_removes_the_rest(self):
        rule = episodes_rule(keep=1, tags=["rm-seasons-2"])
        # files 1 and 2 are still eligible, 7 isn't (e.g. replaced)
        self._apply([make_series()], {1: {1, 2, 7}}, rule=rule)
        self.client.delete_episode_files.assert_called_once_with([1, 2])
        statuses = sorted(c.status for c in self.result.changes)
        assert statuses == ["blocked", "done"]

    def test_watched_since_the_plan_is_not_removed(self):
        rule = seasons_rule(skip_if_watched_within=timedelta(days=14))
        self._apply([make_series()], {1: {1}}, rule=rule, watches=[watch(1, 1)])
        self.client.delete_episode_files.assert_not_called()
        assert self.result.changes[0].status == "blocked"

    @pytest.mark.parametrize(
        "series, queued, reason",
        [
            (make_series(), {1}, "downloads in queue"),
            (make_series(added=NOW - timedelta(days=1)), set(), "added recently"),
            (make_series(tags=["other"]), set(), "no longer tagged"),
        ],
        ids=["queued", "re-added", "untagged"],
    )
    def test_series_level_skips_are_reported(self, series, queued, reason):
        self.client.queued_series_ids.return_value = queued
        self._apply([series], {1: {1}})
        self.client.delete_episode_files.assert_not_called()
        [change] = self.result.changes
        assert (change.target, change.status) == ("Survivor", "blocked")
        assert reason in change.detail

    def test_series_gone_from_sonarr_is_reported(self):
        self._apply([], {5: {1}})
        assert self.result.changes == [
            Change("SONARR", "remove", "series 5", [], "blocked", "no longer in Sonarr")
        ]

    def test_max_deletes_still_applies(self):
        series = [make_series(id=1), make_series(id=2)]
        settings = RemoverSettings(max_deletes=1)
        self._apply(series, {1: {1}, 2: {1}}, settings=settings)
        self.client.delete_episode_files.assert_not_called()
        assert [c.status for c in self.result.changes] == ["blocked", "blocked"]


class TestPlanRecorded(SonarrFakes):
    def test_unapproved_records_the_plan(self):
        self._run([make_series()], auto_approve=False)
        [removal] = self.result.removals
        assert removal.status == "dry_run"
        assert removal.detail == "1 files"
        assert self.result.to_dict()["removals"][0]["files"] == [
            {"id": 1, "path": "/tv/Show/Season 01/S01E01.mkv"}
        ]


class TestAutoApprove(SonarrFakes):
    def test_auto_approved_rule_deletes(self):
        rule = seasons_rule()
        self._apply_auto([make_series()], rule, frozenset({rule.key}))
        self.client.delete_episode_files.assert_called_once_with([1])
        [change] = self.result.changes
        assert (change.status, change.detail) == ("done", "1 files, auto-approved")

    def test_other_rules_wait_for_review(self):
        auto = seasons_rule()
        review = seasons_rule(tags=["rm-review"])
        series = [make_series(id=1), make_series(id=2, title="Other", tags=["rm-review"])]
        self._apply_auto(series, [auto, review], frozenset({auto.key}))
        self.client.episodes.assert_any_call(2)
        self.client.delete_episode_files.assert_called_once_with([1])
        assert {c.target: c.status for c in self.result.changes} == {
            "Survivor": "done",
            "Other": "dry_run",
        }

    def test_max_deletes_counts_only_deleted_files(self):
        auto = seasons_rule()
        review = seasons_rule(tags=["rm-review"])
        series = [make_series(id=1), make_series(id=2, title="Other", tags=["rm-review"])]
        settings = RemoverSettings(max_deletes=1)
        self._apply_auto(series, [auto, review], frozenset({auto.key}), settings)
        self.client.delete_episode_files.assert_called_once_with([1])

    def test_max_deletes_blocks_auto_approved(self):
        rule = seasons_rule()
        series = [make_series(id=1), make_series(id=2)]
        settings = RemoverSettings(max_deletes=1)
        self._apply_auto(series, rule, frozenset({rule.key}), settings)
        self.client.delete_episode_files.assert_not_called()
        assert [c.status for c in self.result.changes] == ["blocked", "blocked"]

    def test_plan_over_max_deletes_is_still_a_plan(self):
        # nothing is deleted, so it can still be reviewed and partly approved
        settings = RemoverSettings(max_deletes=1)
        self._run([make_series(id=1), make_series(id=2)], settings=settings, auto_approve=False)
        assert [c.status for c in self.result.changes] == ["dry_run", "dry_run"]

    def test_rule_key_ignores_order_and_case(self):
        assert seasons_rule(tags=["B", "a"]).key == seasons_rule(tags=["A", "b"]).key == "a,b"

    def _apply_auto(self, series, rules, auto, settings=None):
        arr = MagicMock()
        arr.all_series.return_value = series
        arr_config = ArrConfig(
            baseurl="http://sonarr",
            apikey="key",
            tag=None,
            removers=rules if isinstance(rules, list) else [rules],
            remover_settings=settings or RemoverSettings(),
        )
        with (
            patch.object(remover, "SonarrAPI", return_value=arr),
            patch.object(remover, "SonarrEpisodes", return_value=self.client),
            patch.object(remover, "utc_now", return_value=NOW),
        ):
            run_removers(arr_config, None, None, self.result, auto_approve=auto)
