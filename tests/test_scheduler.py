"""Tests for the scheduler and schedule validation"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from results import JobResult
from web.db import JobRun, Schedule, make_engine, migrate
from web.jobs import JobRunner
from web.scheduler import (
    TRASH_FOLLOWUP_ATTEMPTS,
    TRASH_FOLLOWUP_DELAY,
    Scheduler,
    next_run_at,
    schedule_errors,
    trash_followup_due,
)

START = datetime(2026, 10, 22, 13, 0)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


@pytest.fixture
def runner(engine):
    calls = []

    def run_job(job: str, settings: ConfigSettings) -> JobResult:
        calls.append(job)
        return JobResult(job)

    runner = JobRunner(engine, run_job, lambda: ConfigSettings(plex=None, radarr=None, sonarr=None))
    runner.calls = calls
    runner.start()
    yield runner
    runner.stop()


@pytest.fixture
def scheduler(engine, runner):
    scheduler = Scheduler(engine, runner)
    scheduler.started_at = START
    return scheduler


def add_schedule(engine, cron="47 * * * *", job="movers", **values) -> int:
    with Session(engine) as session:
        schedule = Schedule(
            name=values.pop("name", "hourly movers"),
            job=job,
            cron=cron,
            timezone=values.pop("timezone", "UTC"),
            dry_run=values.pop("dry_run", True),
            enabled=values.pop("enabled", True),
            created_at=START,
            updated_at=values.pop("updated_at", START),
            **values,
        )
        session.add(schedule)
        session.commit()
        return schedule.id


def runs(engine) -> list[JobRun]:
    with Session(engine) as session:
        return list(session.scalars(select(JobRun).order_by(JobRun.id)))


class TestTick:
    def test_fires_when_due_and_links_the_run(self, engine, runner, scheduler):
        schedule_id = add_schedule(engine, dry_run=False)
        assert scheduler.tick(START + timedelta(minutes=46)) == []
        [run_id] = scheduler.tick(START + timedelta(minutes=47, seconds=1))
        runner.wait_idle()
        [run] = runs(engine)
        assert run.id == run_id
        assert run.trigger == "schedule"
        assert run.schedule_id == schedule_id
        assert run.dry_run is False
        assert run.status == "succeeded"
        assert runner.calls == ["movers"]

    def test_fires_once_per_match(self, engine, runner, scheduler):
        add_schedule(engine)
        due = START + timedelta(minutes=47, seconds=1)
        assert len(scheduler.tick(due)) == 1
        runner.wait_idle()
        assert scheduler.tick(due + timedelta(seconds=30)) == []
        assert len(scheduler.tick(due + timedelta(hours=1))) == 1

    def test_missed_times_fire_once(self, engine, runner, scheduler):
        add_schedule(engine, "* * * * *")
        assert len(scheduler.tick(START + timedelta(hours=3))) == 1

    def test_nothing_from_before_the_app_started(self, engine, scheduler):
        # last fired long ago, the app was down for the times in between
        add_schedule(engine, last_fired_at=START - timedelta(days=2), updated_at=START - timedelta(days=3))
        assert scheduler.tick(START + timedelta(minutes=30)) == []

    def test_nothing_from_before_an_edit(self, engine, scheduler):
        add_schedule(engine, updated_at=START + timedelta(minutes=50))
        assert scheduler.tick(START + timedelta(minutes=55)) == []
        assert len(scheduler.tick(START + timedelta(hours=1, minutes=47))) == 1

    def test_uses_the_schedules_time_zone(self, engine, scheduler):
        # 09:00 in new york is 13:00 utc, so 10:00 there is 14:00 utc
        add_schedule(engine, "0 10 * * *", timezone="America/New_York")
        assert scheduler.tick(START + timedelta(minutes=59)) == []
        assert len(scheduler.tick(START + timedelta(hours=1))) == 1

    def test_disabled_never_fires(self, engine, scheduler):
        add_schedule(engine, "* * * * *", enabled=False)
        assert scheduler.tick(START + timedelta(hours=1)) == []

    def test_active_job_skips_but_counts_as_fired(self, engine, runner, scheduler):
        runner.submit("movers", True, "manual", "me@example.com")
        runner.wait_idle()
        with Session(engine) as session:
            session.get_one(JobRun, 1).status = "running"
            session.commit()
        schedule_id = add_schedule(engine)
        assert scheduler.tick(START + timedelta(minutes=47)) == []
        with Session(engine) as session:
            assert session.get_one(Schedule, schedule_id).last_fired_at is not None

    def test_one_bad_schedule_doesnt_stop_the_others(self, engine, scheduler):
        add_schedule(engine, "not cron", name="broken")
        add_schedule(engine, "* * * * *", name="bad zone", timezone="Mars/Olympus")
        add_schedule(engine, "* * * * *", job="removers", name="ok")
        assert len(scheduler.tick(START + timedelta(minutes=5))) == 1


class TestValidation:
    def test_valid(self):
        assert schedule_errors("hourly", "movers", "47 * * * *", "America/New_York") == []

    def test_every_problem_is_reported(self):
        errors = schedule_errors(" ", "nope", "61 * * * *", "Mars/Olympus")
        assert len(errors) == 4
        assert "Time zone: unknown time zone 'Mars/Olympus'" in errors
        assert "Name is required" in errors
        assert any("nope" in e for e in errors)
        assert any(e.startswith("Schedule: minute") for e in errors)

    def test_next_run_at(self):
        schedule = Schedule(cron="47 * * * *", timezone="UTC", enabled=True)
        assert next_run_at(schedule, START) == START + timedelta(minutes=47)
        schedule.enabled = False
        assert next_run_at(schedule, START) is None


# ---------------------------------------------------------------------------
# follow-up empty trash
# ---------------------------------------------------------------------------


def add_run(engine, job, finished, changes=(), dry_run=False, trigger="schedule", status="succeeded"):
    with Session(engine) as session:
        run = JobRun(
            job=job,
            trigger=trigger,
            status=status,
            dry_run=dry_run,
            created_at=finished - timedelta(minutes=1),
            started_at=finished - timedelta(minutes=1),
            finished_at=None if status in ("queued", "running") else finished,
            result={"job": job, "changes": [dict(c) for c in changes]},
        )
        session.add(run)
        session.commit()
        return run.id


MOVED = [{"action": "move", "status": "done"}]
REMOVED = [{"action": "remove", "status": "done"}]
EMPTIED = [{"action": "empty_trash", "status": "done"}]
BLOCKED = [{"action": "empty_trash", "status": "blocked"}]


def due(engine, now):
    with Session(engine) as session:
        return trash_followup_due(session, now)


class TestTrashFollowup:
    def test_due_a_while_after_files_moved_or_removed(self, engine):
        add_run(engine, "movers", START, MOVED)
        assert due(engine, START) == START + TRASH_FOLLOWUP_DELAY
        add_run(engine, "removers", START + timedelta(minutes=5), REMOVED)
        assert due(engine, START) == START + timedelta(minutes=5) + TRASH_FOLLOWUP_DELAY

    @pytest.mark.parametrize(
        "changes, dry_run",
        [
            ([{"action": "profile", "status": "done"}], False),
            ([{"action": "move", "status": "dry_run"}], False),
            (MOVED, True),
            ([], False),
        ],
        ids=["profile-only", "config-dry", "forced-dry", "nothing"],
    )
    def test_not_needed(self, engine, changes, dry_run):
        add_run(engine, "movers", START, changes, dry_run=dry_run)
        assert due(engine, START) is None

    def test_not_needed_once_emptied(self, engine):
        add_run(engine, "removers", START, REMOVED)
        add_run(engine, "empty_trash", START + timedelta(minutes=20), EMPTIED, trigger="manual")
        assert due(engine, START + timedelta(hours=1)) is None

    def test_a_forced_dry_empty_doesnt_count(self, engine):
        add_run(engine, "removers", START, REMOVED)
        add_run(engine, "empty_trash", START + timedelta(minutes=20), [{"action": "empty_trash", "status": "dry_run"}], dry_run=True)
        assert due(engine, START) is not None

    def test_config_dry_empty_counts(self, engine):
        # config.toml has empty trash dry, nothing more to do until that changes
        add_run(engine, "removers", START, REMOVED)
        add_run(engine, "empty_trash", START + timedelta(minutes=20), [{"action": "empty_trash", "status": "dry_run"}])
        assert due(engine, START + timedelta(hours=1)) is None

    def test_blocked_follow_up_is_retried_then_given_up(self, engine):
        add_run(engine, "removers", START, REMOVED)
        last = START
        for attempt in range(TRASH_FOLLOWUP_ATTEMPTS):
            assert due(engine, last) == last + TRASH_FOLLOWUP_DELAY
            last = last + TRASH_FOLLOWUP_DELAY + timedelta(minutes=1)
            add_run(engine, "empty_trash", last, BLOCKED, trigger="followup")
        assert due(engine, last) is None
        # new work starts over
        add_run(engine, "movers", last + timedelta(hours=1), MOVED)
        assert due(engine, last) == last + timedelta(hours=1) + TRASH_FOLLOWUP_DELAY

    def test_waits_for_an_active_empty_trash(self, engine):
        add_run(engine, "removers", START, REMOVED)
        add_run(engine, "empty_trash", START + timedelta(minutes=2), status="running", trigger="manual")
        assert due(engine, START + timedelta(hours=1)) is None

    def test_old_work_isnt_followed_up(self, engine):
        add_run(engine, "movers", START - timedelta(days=3), MOVED)
        assert due(engine, START) is None

    def test_tick_queues_it(self, engine, runner, scheduler):
        add_run(engine, "movers", START, MOVED)
        assert scheduler.tick(START + timedelta(minutes=14)) == []
        [run_id] = scheduler.tick(START + TRASH_FOLLOWUP_DELAY)
        runner.wait_idle()
        run = next(r for r in runs(engine) if r.id == run_id)
        assert (run.job, run.trigger, run.dry_run) == ("empty_trash", "followup", False)
