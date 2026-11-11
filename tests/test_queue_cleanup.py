"""Tests for queue cleanup: removing downloads stuck in radarr's/sonarr's queue"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

import config
from config.models import QUEUE_BLOCKLIST, QUEUE_METHODS, ArrConfig, ConfigSettings, QueueCleanup
from results import JobResult
from tests.test_stats import engine  # noqa: F401 (fixture)
from web import health
from web.db import HealthIncident
from web.health import HealthMonitor
from web.jobs import utc_now
from web.queue_cleanup import (
    IncidentClock,
    auto_note,
    due,
    due_services,
    incident_key,
    removable,
    run_queue_cleanup,
)
from web.settings_common import turns_live
from web.status import BLOCKLIST, REMOVAL_METHODS, Item, ServiceStatus, StuckDownload

NOW = utc_now()
HOUR = timedelta(hours=1)


def download(key="nzo_1", states=("importBlocked",), ids=(1,), title="Heat (1995)"):
    return StuckDownload(key, list(ids), [title], "Heat.1995.1080p", "SABnzbd", "Not an upgrade", "warn", list(states))


def cleanup(**kwargs) -> QueueCleanup:
    return QueueCleanup(**{"enabled": True, "dry_run": False, **kwargs})


def arr(**kwargs) -> ArrConfig:
    return ArrConfig(baseurl="http://radarr", apikey="key", tag=None, queue_cleanup=cleanup(**kwargs))


class FakeQueue:
    def __init__(self, downloads, health=()):
        self.downloads = downloads
        self.health_checks = list(health)
        self.removed: list[tuple[list[int], str, str]] = []
        self.fail = False

    def stuck(self):
        return self.downloads

    def health(self):
        return self.health_checks

    def remove(self, ids, method, blocklist):
        if self.fail:
            raise RuntimeError("Radarr said no")
        self.removed.append((ids, method, blocklist))


class FakeClock:
    def __init__(self, since):
        self.stuck_since = since
        self.marks: list[tuple[str, list[str], str]] = []

    def since(self, service, keys):
        return {k: v for k, v in self.stuck_since.items() if k in keys}

    def mark(self, service, keys, status, now):
        self.marks.append((service, keys, status))


def run(radarr: ArrConfig | None, queue: FakeQueue, clock, sonarr=None) -> JobResult:
    result = JobResult("clear_stuck")
    settings = ConfigSettings(plex=None, radarr=radarr, sonarr=sonarr)
    run_queue_cleanup(settings, result, clock, NOW, queue_for=lambda service, label, a: queue)
    return result


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestConfig:
    def test_off_and_dry_by_default(self):
        settings = config.build_config({"settings": {"dry_run": False}, "radarr": {}})
        assert settings.radarr.queue_cleanup == QueueCleanup()
        assert not settings.radarr.queue_cleanup.enabled
        assert settings.radarr.queue_cleanup.dry_run
        assert settings.radarr.queue_cleanup.states == ["failed", "import_blocked"]

    def test_built_from_the_document(self):
        settings = config.build_config(
            {"sonarr": {"queue_cleanup": {"enabled": True, "stuck_for": "12h", "states": ["failed"],
                                          "method": "ignore", "blocklist": "only", "max_per_run": 0, "dry_run": False}}}
        )
        assert settings.sonarr.queue_cleanup == QueueCleanup(True, 12 * HOUR, ["failed"], "ignore", "only", 0, False)
        assert settings.sonarr.queue_cleanup.tracked_states() == {"failed", "failedPending"}

    @pytest.mark.parametrize(
        "section, message",
        [
            ({"stuck_for": "30m"}, "at least 1h"),
            ({"stuck_for": "6"}, "invalid duration"),
            ({"states": []}, "at least one"),
            ({"states": "failed"}, "list"),
            ({"states": ["downloading"]}, "unknown states"),
            ({"method": "delete"}, "method"),
            ({"blocklist": "yes"}, "blocklist"),
            ({"max_per_run": -1}, "max_per_run"),
            ({"dry_run": 0}, "dry_run"),
            ([], "must be a table"),
        ],
    )
    def test_invalid(self, section, message):
        with pytest.raises(ValueError, match=message) as error:
            config.build_config({"radarr": {"queue_cleanup": section}})
        assert "[radarr.queue_cleanup]" in str(error.value)

    def test_options_match_the_queue_pages(self):
        assert tuple(REMOVAL_METHODS) == QUEUE_METHODS
        assert tuple(BLOCKLIST) == QUEUE_BLOCKLIST

    def test_config_dry_run(self):
        def settings(radarr, sonarr):
            return ConfigSettings(plex=None, radarr=radarr, sonarr=sonarr)

        live, dry = arr(), arr(dry_run=True)
        off = ArrConfig(baseurl="http://sonarr", apikey="key", tag=None, dry_run=False)
        assert config.config_dry_run("clear_stuck", settings(live, off)) is False
        assert config.config_dry_run("clear_stuck", settings(dry, off)) is True
        assert config.config_dry_run("clear_stuck", settings(live, dry)) is None
        assert config.config_dry_run("clear_stuck", settings(None, off)) is True
        # movers' own dry run is separate
        assert config.config_dry_run("movers", settings(off, None)) is False

    def test_going_live_needs_confirming(self):
        on = {"radarr": {"queue_cleanup": {"enabled": True, "dry_run": True}}}
        live = {"radarr": {"queue_cleanup": {"enabled": True, "dry_run": False}}}
        assert turns_live(on, live) == ["clear_stuck"]
        assert turns_live(live, live) == []
        # turned off, a live dry_run doesn't count
        assert turns_live({}, {"radarr": {"queue_cleanup": {"enabled": False, "dry_run": False}}}) == []


# ---------------------------------------------------------------------------
# what's due
# ---------------------------------------------------------------------------


class TestDue:
    def test_only_the_states_it_removes(self):
        c = cleanup(states=["failed"])
        assert removable(["failedPending"], "nzo", c)
        assert not removable(["importBlocked"], "nzo", c)
        # a season pack with one record in another state stays
        assert not removable(["failed", "importBlocked"], "nzo", c)
        assert not removable([], "nzo", c)

    def test_needs_a_download_id(self):
        # one without is keyed by title in the health history, its age isn't reliable
        assert not removable(["importBlocked"], "queue:7", cleanup())
        assert not removable(["importBlocked"], "", cleanup())

    def test_stuck_long_enough(self):
        c = cleanup(stuck_for=6 * HOUR)
        old, new, unseen = download("old"), download("new"), download("unseen")
        since = {"old": NOW - 6 * HOUR, "new": NOW - 5 * HOUR}
        assert due([old, new, unseen], c, since, NOW) == [(old, NOW - 6 * HOUR)]

    def test_incident_key_matches_the_health_history(self):
        assert incident_key("SABnzbd_nzo_1") == health.item_key(Item("Heat", key="SABnzbd_nzo_1"))

    def test_notes(self):
        c = cleanup(stuck_for=6 * HOUR)
        assert auto_note(download(), c, NOW - 2 * HOUR, NOW) == "Removed automatically in 4h 0m"
        assert auto_note(download(), c, NOW - 7 * HOUR, NOW) == "Due: stuck 7h 0m"
        assert auto_note(download(), c, None, NOW).startswith("Removed automatically 6h 0m after")
        assert auto_note(download(states=["importPending"]), c, NOW, NOW) == "Not removed automatically: import pending"
        assert auto_note(download(), cleanup(dry_run=True), NOW, NOW).startswith("Listed by a dry run in")


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------


class TestRun:
    def test_removes_whats_due(self):
        queue = FakeQueue([download("a", ids=[1, 2]), download("b", ids=[3]), download("c", ids=[4])])
        clock = FakeClock({"a": NOW - 7 * HOUR, "b": NOW - 6 * HOUR, "c": NOW - HOUR})
        result = run(arr(method="ignore", blocklist="only"), queue, clock)
        assert queue.removed == [([1, 2, 3], "ignore", "only")]
        [change] = result.changes
        assert (change.service, change.action, change.status) == ("RADARR", "remove_download", "done")
        assert change.target == "remove from the queue only, blocklist only"
        assert change.items[0] == "Heat (1995), stuck 7h 0m: Not an upgrade"
        assert clock.marks == [("radarr", ["a", "b"], "done")]
        assert not result.failed

    def test_dry_run_removes_nothing(self):
        queue = FakeQueue([download("a")])
        clock = FakeClock({"a": NOW - 7 * HOUR})
        result = run(arr(dry_run=True), queue, clock)
        assert queue.removed == []
        assert [c.status for c in result.changes] == ["dry_run"]
        assert clock.marks == [("radarr", ["a"], "dry_run")]

    def test_forced_dry_run_removes_nothing(self):
        settings = ConfigSettings(plex=None, radarr=arr(), sonarr=None)
        config.force_dry_run(settings)
        queue = FakeQueue([download("a")])
        result = JobResult("clear_stuck")
        run_queue_cleanup(settings, result, FakeClock({"a": NOW - 7 * HOUR}), NOW, queue_for=lambda *a: queue)
        assert queue.removed == []
        assert [c.status for c in result.changes] == ["dry_run"]

    def test_over_max_per_run_removes_none(self):
        queue = FakeQueue([download(str(n)) for n in range(3)])
        clock = FakeClock({str(n): NOW - 7 * HOUR for n in range(3)})
        result = run(arr(max_per_run=2), queue, clock)
        assert queue.removed == []
        [change] = result.changes
        assert change.status == "blocked"
        assert "3 downloads are due, over the max per run (2)" in change.detail
        assert clock.marks == [("radarr", ["0", "1", "2"], "blocked")]

    def test_no_limit(self):
        queue = FakeQueue([download(str(n), ids=[n]) for n in range(30)])
        clock = FakeClock({str(n): NOW - 7 * HOUR for n in range(30)})
        run(arr(max_per_run=0), queue, clock)
        assert queue.removed[0][0] == list(range(30))

    @pytest.mark.parametrize("source", ["RootFolderCheck", "RemotePathMappingCheck", "DownloadClientCheck"])
    def test_an_arr_problem_that_blocks_every_import_removes_none(self, source):
        queue = FakeQueue([download("a")], health=[{"source": source, "type": "error", "message": "Missing root folder: /movies"}])
        result = run(arr(), queue, FakeClock({"a": NOW - 7 * HOUR}))
        assert queue.removed == []
        [change] = result.changes
        assert change.status == "blocked"
        assert "Missing root folder: /movies" in change.detail

    def test_other_health_checks_dont_block(self):
        queue = FakeQueue([download("a")], health=[{"source": "IndexerStatusCheck", "type": "warning", "message": "Indexers unavailable"}])
        run(arr(), queue, FakeClock({"a": NOW - 7 * HOUR}))
        assert queue.removed

    def test_only_the_states_set(self):
        queue = FakeQueue([download("a", states=["importPending"]), download("b", states=["failed"], ids=[9])])
        run(arr(), queue, FakeClock({"a": NOW - 7 * HOUR, "b": NOW - 7 * HOUR}))
        assert queue.removed == [([9], "client", "search")]

    def test_nothing_due_changes_nothing(self):
        queue = FakeQueue([download("a")])
        clock = FakeClock({"a": NOW - HOUR})
        result = run(arr(), queue, clock)
        assert (queue.removed, result.changes, clock.marks) == ([], [], [])

    def test_a_failed_removal_is_a_failed_change(self):
        queue = FakeQueue([download("a")])
        queue.fail = True
        clock = FakeClock({"a": NOW - 7 * HOUR})
        result = run(arr(), queue, clock)
        assert [c.status for c in result.changes] == ["failed"]
        assert result.failed
        assert clock.marks == [("radarr", ["a"], "failed")]

    def test_one_arr_failing_doesnt_stop_the_other(self):
        class Broken(FakeQueue):
            def stuck(self):
                raise RuntimeError("down")

        good = FakeQueue([download("a")])
        result = JobResult("clear_stuck")
        settings = ConfigSettings(plex=None, radarr=arr(), sonarr=arr())
        queues = {"radarr": Broken([]), "sonarr": good}
        run_queue_cleanup(settings, result, FakeClock({"a": NOW - 7 * HOUR}), NOW, queue_for=lambda s, label, a: queues[s])
        assert result.errors == ["Radarr queue cleanup failed: down"]
        assert good.removed

    def test_off_or_without_the_web_app_does_nothing(self):
        queue = FakeQueue([download("a")])
        off = ArrConfig(baseurl="http://radarr", apikey="key", tag=None)
        assert run(off, queue, FakeClock({"a": NOW - 7 * HOUR})).changes == []
        # the command line has no health history
        assert run(arr(), queue, None).changes == []
        assert queue.removed == []


# ---------------------------------------------------------------------------
# the health history: when a download got stuck, and the health check's trigger
# ---------------------------------------------------------------------------


def incident(engine, key, started, service="radarr", ended=None, done=None, done_at=None):  # noqa: F811
    with Session(engine) as session:
        session.add(HealthIncident(
            service=service, key=incident_key(key), level="warn", text="Stuck", started_at=started,
            last_seen_at=NOW, ended_at=ended, cleanup=done, cleanup_at=done_at,
        ))
        session.commit()


def checked(*keys, states=("importBlocked",), service="radarr"):
    s = ServiceStatus(service, service.title())
    s.items = [Item(k, key=k, level="warn", states=list(states)) for k in keys]
    return s


class TestIncidentClock:
    def test_since_and_mark(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 7 * HOUR)
        incident(engine, "b", NOW - 9 * HOUR, ended=NOW - HOUR)  # cleared, not stuck now
        incident(engine, "a", NOW - 2 * HOUR, service="sonarr")
        clock = IncidentClock(engine)
        assert clock.since("radarr", ["a", "b", "c"]) == {"a": NOW - 7 * HOUR}
        clock.mark("radarr", ["a"], "done", NOW)
        with Session(engine) as session:
            marks = {(i.service, i.cleanup) for i in session.query(HealthIncident)}
        assert marks == {("radarr", "done"), ("radarr", None), ("sonarr", None)}


class TestDueServices:
    def settings(self, **kwargs):
        return ConfigSettings(plex=None, radarr=arr(**kwargs), sonarr=None)

    def test_queues_once_a_download_comes_due(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 5 * HOUR)
        assert due_services(engine, self.settings(), [checked("a")], NOW) == []
        assert due_services(engine, self.settings(), [checked("a")], NOW + HOUR) == ["Radarr"]

    def test_not_again_once_acted_on(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 7 * HOUR, done="dry_run", done_at=NOW - HOUR)
        incident(engine, "b", NOW - 7 * HOUR, done="done", done_at=NOW - HOUR)
        assert due_services(engine, self.settings(dry_run=True), [checked("a", "b")], NOW) == []
        # a dry run's download is removed once it's live
        assert due_services(engine, self.settings(), [checked("a", "b")], NOW) == ["Radarr"]

    def test_blocked_is_tried_again_after_stuck_for(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 9 * HOUR, done="blocked", done_at=NOW - 5 * HOUR)
        assert due_services(engine, self.settings(), [checked("a")], NOW) == []
        assert due_services(engine, self.settings(), [checked("a")], NOW + HOUR) == ["Radarr"]

    def test_only_states_it_removes_and_only_when_on(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 7 * HOUR)
        assert due_services(engine, self.settings(), [checked("a", states=["importPending"])], NOW) == []
        off = ConfigSettings(plex=None, radarr=ArrConfig(baseurl="x", apikey="k", tag=None), sonarr=None)
        assert due_services(engine, off, [checked("a")], NOW) == []

    def test_the_health_monitor_queues_a_run(self, engine):  # noqa: F811
        incident(engine, "a", NOW - 7 * HOUR)
        runner = MagicMock()
        monitor = HealthMonitor(engine, lambda: self.settings(), runner)
        monitor.queue_cleanup(self.settings(), [checked("a")], NOW)
        runner.submit_soon.assert_called_once_with("clear_stuck", "health", "health check: stuck in Radarr")
        runner.reset_mock()
        monitor.queue_cleanup(self.settings(), [checked()], NOW)
        runner.submit_soon.assert_not_called()
