"""Tests for movie_remover.py (radarr removers)"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

import main
import movie_remover
from config.models import (
    ArrConfig,
    ConfigSettings,
    MovieRemoverRule,
    RemoverSettings,
)
from movie_remover import (
    InvalidMovieData,
    MovieFile,
    age_skip_reason,
    parse_movie,
    run_movie_removers,
    watch_skip_reason,
)
from results import JobResult

NOW = datetime(2026, 10, 27, 12, 0)
HALLMARK = "/data/media/movies/movies-hallmark"
TAGS = {1: "keep-forever", 2: "hallmark"}


def raw_movie(id=1, title="Falling Like Snowflakes", root=HALLMARK, added="2024-07-04T16:45:49Z",
              released="2024-06-29T00:00:00Z", file_id=None, tags=(), **kwargs):
    raw = {
        "id": id,
        "title": title,
        "year": 2024,
        "path": f"{root}/{title} (2024)",
        "rootFolderPath": root,
        "added": added,
        "releaseDate": released,
        "tmdbId": 1000 + id if id else None,
        "imdbId": f"tt{id}",
        "tags": list(tags),
        "hasFile": True,
        "movieFile": {"id": file_id or (id or 0) * 10, "path": f"{root}/{title} (2024)/{title}.mkv"},
    }
    raw.update(kwargs)
    return raw


def rule(**kwargs) -> MovieRemoverRule:
    return MovieRemoverRule(**{"path": HALLMARK, "min_age": timedelta(days=730), **kwargs})


def movie(**kwargs):
    return parse_movie(raw_movie(**kwargs), TAGS)


# ---------------------------------------------------------------------------
# parsing radarr's data
# ---------------------------------------------------------------------------


class TestParseMovie:
    def test_valid(self):
        m = movie(tags=[2])
        assert (m.id, m.label, m.root) == (1, "Falling Like Snowflakes (2024)", HALLMARK)
        assert m.added == datetime(2024, 7, 4, 16, 45, 49)
        assert m.released == datetime(2024, 6, 29)
        assert m.tags == {"hallmark"}
        assert m.file == MovieFile(10, f"{HALLMARK}/Falling Like Snowflakes (2024)/Falling Like Snowflakes.mkv")
        assert (m.tmdb_id, m.imdb_id) == (1001, "tt1")

    def test_root_without_trailing_slash_and_from_the_path(self):
        assert movie(root=HALLMARK + "/").root == HALLMARK
        raw = raw_movie()
        del raw["rootFolderPath"]
        assert parse_movie(raw, TAGS).root == HALLMARK

    def test_no_file(self):
        assert movie(hasFile=False, movieFile=None).file is None

    @pytest.mark.parametrize(
        "changes",
        [
            {"id": None},
            {"title": ""},
            {"path": None},
            {"added": None},
            # radarr's "no date", must never read as very old
            {"added": "0001-01-01T00:00:00Z"},
            {"movieFile": None},
            {"movieFile": {"id": 5}},
        ],
        ids=["id", "title", "path", "added", "added-0001", "no-movie-file", "file-path"],
    )
    def test_invalid_raises(self, changes):
        with pytest.raises(InvalidMovieData):
            parse_movie(raw_movie(**changes), TAGS)

    @pytest.mark.parametrize("released", [None, "", "0001-01-01T00:00:00Z", "soon"])
    def test_unknown_release_date_is_none(self, released):
        assert movie(released=released).released is None


# ---------------------------------------------------------------------------
# what's removed
# ---------------------------------------------------------------------------


class TestAgeSkipReason:
    def test_old_enough(self):
        assert age_skip_reason(movie(), rule(min_age=timedelta(days=700)), set(), NOW) is None

    @pytest.mark.parametrize(
        "kwargs, reason",
        [
            ({"hasFile": False}, "no file"),
            ({"tags": [1]}, "excluded tag"),
            ({"added": "2026-01-01T00:00:00Z"}, "added recently"),
            # an old movie added lately stays: both dates must be old
            ({"released": "2026-01-01T00:00:00Z"}, "released recently"),
            ({"released": None}, "no release date"),
        ],
    )
    def test_skipped(self, kwargs, reason):
        r = rule(min_age=timedelta(days=700), exclude_tags=["Keep-Forever"])
        assert age_skip_reason(movie(**kwargs), r, set(), NOW) == reason

    def test_old_movie_added_lately_is_skipped(self):
        m = movie(added="2026-09-01T00:00:00Z", released="1990-01-01T00:00:00Z")
        assert age_skip_reason(m, rule(), set(), NOW) == "added recently"

    def test_queued(self):
        assert age_skip_reason(movie(), rule(min_age=timedelta(days=700)), {1}, NOW) == "download in queue"


class TestWatchSkipReason:
    def test_always_removes_watched(self):
        assert watch_skip_reason(rule(), [NOW], NOW) is None
        assert watch_skip_reason(rule(), None, NOW) is None

    def test_ever_watched(self):
        r = rule(skip_if_ever_watched=True)
        assert watch_skip_reason(r, [datetime(2020, 1, 1)], NOW) == "watched"
        assert watch_skip_reason(r, [], NOW) is None
        assert watch_skip_reason(r, None, NOW) == "plays unknown"

    def test_watched_within(self):
        r = rule(skip_if_watched_within=timedelta(days=180))
        assert watch_skip_reason(r, [NOW - timedelta(days=10)], NOW) == "watched recently"
        assert watch_skip_reason(r, [NOW - timedelta(days=200)], NOW) is None
        assert watch_skip_reason(r, None, NOW) == "plays unknown"


# ---------------------------------------------------------------------------
# run_movie_removers
# ---------------------------------------------------------------------------


class RadarrFakes:
    """two old hallmark movies (ids 1 and 2, files 10 and 20) and one new one (3)"""

    def setup_method(self):
        self.result = JobResult("removers")
        self.client = MagicMock()
        self.client.tags.return_value = TAGS
        self.client.queued_movie_ids.return_value = set()
        self.client.movies.return_value = [
            raw_movie(1, "Old One"),
            raw_movie(2, "Old Two"),
            raw_movie(3, "New", added="2026-09-01T00:00:00Z", released="2026-08-01T00:00:00Z"),
            raw_movie(4, "Elsewhere", root="/data/media/movies/movies"),
        ]

    def _run(self, rules=None, auto_approve=frozenset(), approved=None, plays=None, max_deletes=None):
        arr_config = ArrConfig(
            baseurl="http://radarr",
            apikey="key",
            tag=None,
            removers=rules or [rule(plex_library="Hallmark")],
            remover_settings=RemoverSettings(max_deletes=max_deletes),
        )
        with (
            patch.object(movie_remover, "RadarrMovies", return_value=self.client),
            patch.object(movie_remover, "utc_now", return_value=NOW),
        ):
            run_movie_removers(arr_config, self.result, approved, auto_approve, plays)
        return self.result

    def statuses(self):
        return {(r.title, r.status) for r in self.result.removals}


class TestRunMovieRemovers(RadarrFakes):
    def test_plans_for_review(self):
        result = self._run()
        assert self.statuses() == {("Old One (2024)", "dry_run"), ("Old Two (2024)", "dry_run")}
        self.client.delete_movie.assert_not_called()
        removal = result.removals[0]
        assert removal.service == "RADARR" and removal.media_id == 1
        assert [f.id for f in removal.files] == [10]
        assert removal.labels == ["released 2024-06-29", "added 2024-07-04"]
        assert removal.rule == f"older than 730d in {HALLMARK}"

    def test_auto_approved_rule_deletes(self):
        result = self._run(auto_approve=frozenset({rule().key}))
        assert self.statuses() == {("Old One (2024)", "done"), ("Old Two (2024)", "done")}
        assert [c.args for c in self.client.delete_movie.call_args_list] == [(1,), (2,)]
        assert result.scan_dirs == {f"{HALLMARK}/Old One (2024)", f"{HALLMARK}/Old Two (2024)"}
        assert result.plex_libraries == {"Hallmark"}

    def test_other_keys_dont_auto_approve(self):
        self._run(auto_approve=frozenset({"rm-seasons-2"}))
        self.client.delete_movie.assert_not_called()

    def test_max_deletes_blocks_everything(self):
        self._run(auto_approve=frozenset({rule().key}), max_deletes=1)
        assert {s for _, s in self.statuses()} == {"blocked"}
        self.client.delete_movie.assert_not_called()

    def test_delete_failure_is_recorded(self):
        self.client.delete_movie.side_effect = [requests.exceptions.HTTPError("500"), None]
        result = self._run(auto_approve=frozenset({rule().key}))
        assert self.statuses() == {("Old One (2024)", "failed"), ("Old Two (2024)", "done")}
        assert result.failed

    def test_queued_and_invalid_movies_skipped(self):
        self.client.queued_movie_ids.return_value = {1}
        self.client.movies.return_value.append({"id": 9, "title": "Broken"})
        self._run()
        assert self.statuses() == {("Old Two (2024)", "dry_run")}

    def test_nothing_matched_touches_no_more_of_radarr(self):
        self._run(rules=[rule(path="/data/media/movies/movies-standup")])
        self.client.queued_movie_ids.assert_not_called()
        assert self.result.removals == []

    def test_not_configured(self):
        result = JobResult("removers")
        run_movie_removers(ArrConfig(baseurl=None, apikey=None, tag=None, removers=[rule()]), result)
        assert result.errors == ["RADARR not configured"]


class TestWatchedMovies(RadarrFakes):
    def test_ever_watched_kept_and_unknown_kept(self):
        plays = MagicMock(return_value={1: [datetime(2025, 1, 2)], 3: []})
        self._run(rules=[rule(skip_if_ever_watched=True)], plays=plays)
        # 1 was played, 2 isn't in plex so its plays are unknown
        assert self.result.removals == []
        # only candidates are looked up
        assert [m.id for m in plays.call_args.args[0]] == [1, 2]

    def test_never_played_is_removed_and_labelled(self):
        plays = MagicMock(return_value={1: [datetime(2025, 1, 2)], 2: []})
        self._run(rules=[rule(skip_if_ever_watched=True)], plays=plays)
        assert self.statuses() == {("Old Two (2024)", "dry_run")}
        assert self.result.removals[0].labels[-1] == "never played"

    def test_played_long_ago_is_removed_with_a_window(self):
        plays = MagicMock(return_value={1: [NOW - timedelta(days=10)], 2: [datetime(2025, 1, 2)]})
        self._run(rules=[rule(skip_if_watched_within=timedelta(days=180))], plays=plays)
        assert self.statuses() == {("Old Two (2024)", "dry_run")}
        assert self.result.removals[0].labels[-1] == "last played 2025-01-02"

    def test_without_play_history_nothing_is_removed(self):
        self._run(rules=[rule(skip_if_ever_watched=True)], plays=None)
        assert self.result.removals == []
        assert "No play history" in self.result.errors[0]

    def test_failing_lookup_keeps_everything(self):
        plays = MagicMock(side_effect=RuntimeError("tautulli down"))
        self._run(rules=[rule(skip_if_ever_watched=True)], plays=plays)
        assert self.result.removals == []
        assert "tautulli down" in self.result.errors[0]

    def test_always_rules_dont_look_up_plays(self):
        plays = MagicMock()
        self._run(plays=plays)
        plays.assert_not_called()
        assert len(self.result.removals) == 2


class TestApplyMoviePlan(RadarrFakes):
    def test_removes_only_approved(self):
        self._run(approved={2: {20}})
        assert self.statuses() == {("Old Two (2024)", "done")}
        self.client.delete_movie.assert_called_once_with(2)

    def test_replaced_file_is_not_removed(self):
        self._run(approved={2: {99}})
        self.client.delete_movie.assert_not_called()
        [change] = self.result.changes
        assert (change.status, change.detail) == ("blocked", "its file was replaced since the plan")

    @pytest.mark.parametrize(
        "approved, detail",
        [
            ({3: {30}}, "added recently"),
            ({4: {40}}, "no longer in a remover's root folder"),
            ({8: {80}}, "no longer in Radarr"),
        ],
    )
    def test_no_longer_eligible_is_reported(self, approved, detail):
        self._run(approved=approved)
        self.client.delete_movie.assert_not_called()
        assert [(c.status, c.detail) for c in self.result.changes] == [("blocked", detail)]

    def test_watched_since_the_plan_is_not_removed(self):
        plays = MagicMock(return_value={2: [NOW - timedelta(hours=1)]})
        self._run(rules=[rule(skip_if_ever_watched=True)], approved={2: {20}}, plays=plays)
        self.client.delete_movie.assert_not_called()
        assert [c.detail for c in self.result.changes] == ["watched"]

    def test_max_deletes_still_applies(self):
        self._run(approved={1: {10}, 2: {20}}, max_deletes=1)
        self.client.delete_movie.assert_not_called()
        assert {s for _, s in self.statuses()} == {"blocked"}


# ---------------------------------------------------------------------------
# the removers job runs both arrs
# ---------------------------------------------------------------------------


def settings_with(sonarr_removers=False, radarr_removers=True) -> ConfigSettings:
    sonarr = ArrConfig(baseurl="http://sonarr", apikey="key", tag=None,
                       removers=[MagicMock()] if sonarr_removers else [])
    radarr = ArrConfig(baseurl="http://radarr", apikey="key", tag=None,
                       removers=[rule()] if radarr_removers else [])
    return ConfigSettings(plex=None, radarr=radarr, sonarr=sonarr)


class TestRunAllRemovers:
    def run(self, settings, approved=None):
        with (
            patch.object(main, "run_removers") as sonarr,
            patch.object(main, "run_movie_removers") as radarr,
        ):
            main.run_job("removers", settings, approved=approved, movie_plays="plays")
        return sonarr, radarr

    def test_plans_both(self):
        sonarr, radarr = self.run(settings_with(sonarr_removers=True))
        sonarr.assert_called_once()
        assert radarr.call_args.args[2:] == (None, frozenset(), "plays")

    def test_radarr_without_removers_is_skipped(self):
        sonarr, radarr = self.run(settings_with(radarr_removers=False))
        sonarr.assert_called_once()
        radarr.assert_not_called()

    def test_applying_only_touches_the_approved_arr(self):
        sonarr, radarr = self.run(settings_with(sonarr_removers=True), approved={"RADARR": {2: {20}}})
        sonarr.assert_not_called()
        assert radarr.call_args.args[2] == {2: {20}}

    def test_applying_to_a_missing_arr_is_an_error(self):
        settings = settings_with()
        settings.radarr = None
        with patch.object(main, "run_movie_removers") as radarr:
            result = main.run_job("removers", settings, approved={"RADARR": {2: {20}}})
        radarr.assert_not_called()
        assert result.errors == ["RADARR not configured"]

    def test_radarr_failure_is_an_error(self):
        with (
            patch.object(main, "run_removers"),
            patch.object(main, "run_movie_removers", side_effect=requests.exceptions.ConnectionError("down")),
        ):
            result = main.run_job("removers", settings_with())
        assert result.errors == ["RADARR removers failed: down"]
