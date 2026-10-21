"""Tests for the job runner and dry run helpers"""

import dataclasses
import logging
import threading

import pytest
from sqlalchemy.orm import Session

import config
from config.models import ArrConfig, ConfigSettings, EmptyTrashSettings, RemoverSettings
from results import Change, JobResult
from web.db import JobRun, make_engine, migrate
from web.jobs import AlreadyActive, JobRunner, utc_now

logger = logging.getLogger("tests.jobs")


def arr(dry_run: bool = False, remover_dry_run: bool = False) -> ArrConfig:
    return ArrConfig(
        baseurl="http://arr",
        apikey="key",
        tag=None,
        dry_run=dry_run,
        remover_settings=RemoverSettings(dry_run=remover_dry_run),
    )


def live_settings() -> ConfigSettings:
    return ConfigSettings(
        plex=None,
        radarr=arr(),
        sonarr=arr(),
        empty_trash=EmptyTrashSettings(dry_run=False),
    )


def dry_run_fields(obj: object) -> list[bool]:
    """every dry_run anywhere in the settings tree"""
    found: list[bool] = []
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            value = getattr(obj, f.name)
            if f.name == "dry_run":
                found.append(value)
            else:
                found.extend(dry_run_fields(value))
    return found


# ---------------------------------------------------------------------------
# dry run helpers
# ---------------------------------------------------------------------------


class TestForceDryRun:
    def test_turns_on_every_dry_run(self):
        settings = live_settings()
        assert not any(dry_run_fields(settings))
        config.force_dry_run(settings)
        fields = dry_run_fields(settings)
        # radarr, sonarr, both remover_settings and empty_trash
        assert len(fields) == 5
        assert all(fields)


class TestConfigDryRun:
    def test_movers_live_when_all_arrs_live(self):
        assert config.config_dry_run("movers", live_settings()) is False

    def test_movers_mixed(self):
        settings = ConfigSettings(plex=None, radarr=arr(True), sonarr=arr(False))
        assert config.config_dry_run("movers", settings) is None

    def test_movers_without_arrs_does_nothing(self):
        settings = ConfigSettings(plex=None, radarr=None, sonarr=None)
        assert config.config_dry_run("movers", settings) is True

    def test_removers_use_remover_settings(self):
        settings = ConfigSettings(plex=None, radarr=None, sonarr=arr(False, True))
        assert config.config_dry_run("removers", settings) is True

    def test_empty_trash(self):
        assert config.config_dry_run("empty_trash", live_settings()) is False


# ---------------------------------------------------------------------------
# job runner
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


@pytest.fixture(autouse=True)
def info_logs(caplog):
    # the app configures the root logger at DEBUG, pytest leaves it at WARNING
    caplog.set_level(logging.INFO)


def make_runner(engine, run_job, load_config=live_settings) -> JobRunner:
    runner = JobRunner(engine, run_job, load_config)
    runner.start()
    return runner


def get(engine, run_id: int) -> JobRun:
    with Session(engine) as session:
        return session.get_one(JobRun, run_id)


class TestJobRunner:
    def test_run_is_stored(self, engine):
        def run_job(job, settings):
            logger.info("moving things")
            return JobResult(job, changes=[Change("RADARR", "move", "/m", ["A"], "done")])

        runner = make_runner(engine, run_job)
        run_id = runner.submit("movers", False, "manual", "me@example.com")
        runner.wait_idle()
        run = get(engine, run_id)
        assert run.status == "succeeded"
        assert run.requested_by == "me@example.com"
        assert run.started_at and run.finished_at
        assert run.result["changes"][0]["items"] == ["A"]
        assert "moving things" in run.log
        assert "SUMMARY movers: 1 changes" in run.log
        runner.stop()

    def test_forced_dry_run_reaches_the_job(self, engine):
        seen: list[bool] = []

        def run_job(job, settings):
            seen.extend(dry_run_fields(settings))
            return JobResult(job)

        runner = make_runner(engine, run_job)
        runner.submit("movers", True, "manual", None)
        runner.submit("removers", False, "manual", None)
        runner.wait_idle()
        assert all(seen[:5])
        assert not any(seen[5:])
        runner.stop()

    def test_failed_result_is_failed(self, engine):
        runner = make_runner(engine, lambda job, s: JobResult(job, errors=["SONARR down"]))
        run_id = runner.submit("removers", True, "manual", None)
        runner.wait_idle()
        assert get(engine, run_id).status == "failed"
        runner.stop()

    def test_exception_is_failed_with_traceback_in_log(self, engine):
        def run_job(job, settings):
            raise RuntimeError("boom")

        runner = make_runner(engine, run_job)
        run_id = runner.submit("movers", True, "manual", None)
        runner.wait_idle()
        run = get(engine, run_id)
        assert run.status == "failed"
        assert run.result is None
        assert "RuntimeError: boom" in run.log
        runner.stop()

    def test_bad_config_is_failed(self, engine):
        def load_config():
            raise ValueError("RemoverRule.keep must be an integer >= 1")

        runner = make_runner(engine, lambda job, s: JobResult(job), load_config)
        run_id = runner.submit("removers", True, "manual", None)
        runner.wait_idle()
        run = get(engine, run_id)
        assert run.status == "failed"
        assert "keep must be an integer" in run.log
        runner.stop()

    def test_active_job_cant_be_queued_twice(self, engine):
        release = threading.Event()

        def run_job(job, settings):
            release.wait(5)
            return JobResult(job)

        runner = make_runner(engine, run_job)
        first = runner.submit("movers", True, "manual", None)
        with pytest.raises(AlreadyActive) as e:
            runner.submit("movers", False, "manual", None)
        assert e.value.run_id == first
        # a different job queues behind it
        runner.submit("empty_trash", True, "manual", None)
        release.set()
        runner.wait_idle()
        # finished, so it can run again
        runner.submit("movers", True, "manual", None)
        runner.wait_idle()
        runner.stop()

    def test_unknown_job_rejected(self, engine):
        runner = make_runner(engine, lambda job, s: JobResult(job))
        with pytest.raises(ValueError):
            runner.submit("rm -rf", True, "manual", None)
        runner.stop()

    def test_other_threads_logs_not_captured(self, engine):
        def run_job(job, settings):
            t = threading.Thread(target=lambda: logger.info("request log from elsewhere"))
            t.start()
            t.join()
            logger.info("job log")
            return JobResult(job)

        runner = make_runner(engine, run_job)
        run_id = runner.submit("movers", True, "manual", None)
        runner.wait_idle()
        log = get(engine, run_id).log
        assert "job log" in log
        assert "request log from elsewhere" not in log
        runner.stop()

    def test_interrupted_runs_marked_failed_and_not_resumed(self, engine):
        with Session(engine) as session:
            for status in ("queued", "running", "succeeded"):
                session.add(
                    JobRun(job="removers", trigger="manual", status=status, created_at=utc_now())
                )
            session.commit()
        calls: list[str] = []
        runner = make_runner(engine, lambda job, s: calls.append(job) or JobResult(job))
        runner.wait_idle()
        with Session(engine) as session:
            runs = session.query(JobRun).order_by(JobRun.id).all()
        assert [r.status for r in runs] == ["failed", "failed", "succeeded"]
        assert "Interrupted" in runs[0].log
        assert calls == []
        runner.stop()
