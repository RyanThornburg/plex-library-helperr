"""Tests for the app shell: where you are in the sidebar, and the badges and header pill
that say what needs you from any page"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_web import FakeKeys, auth, fake_run_job, live_config
from tests.test_web import settings  # noqa: F401 (fixture)
from web.app import create_app
from web.db import HealthIncident, JobRun
from web.jobs import utc_now
from web.shell import Needs, build_shell, section_of, sub_of, SECTIONS


def section(shell, key):
    return next(s for s in shell.sections if s.key == key)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/status", "status"),
        ("/status/history", "status"),
        ("/status/sonarr/queue", "status"),
        ("/runs", "activity"),
        ("/runs/7/apply", "activity"),
        ("/jobs/movers/confirm", "activity"),
        ("/schedules/new", "activity"),
        ("/removers", "removers"),
        ("/removers/auto-approve", "removers"),
        # remover rules live under Settings but are edited from Removers
        ("/settings/sonarr/removers/new", "removers"),
        ("/settings/radarr/movers/1", "movers"),
        ("/settings/sonarr/profilers/new", "movers"),
        ("/movers", "movers"),
        ("/settings", "settings"),
        ("/requests", "requests"),
        ("/playback", "playback"),
        ("/statusx", None),
    ],
)
def test_section_of(path, expected):
    assert section_of(path) == expected


def test_the_longest_sub_page_wins():
    settings_subs = next(s for s in SECTIONS if s[0] == "settings")[4]
    assert sub_of("/settings", settings_subs) == "general"
    assert sub_of("/settings/trash", settings_subs) == "trash"
    assert sub_of("/settings/history/4", settings_subs) == "history"


class TestBuildShell:
    def test_only_the_current_section_opens(self):
        shell = build_shell("/settings/connections", Needs())
        settings_section = section(shell, "settings")
        assert settings_section.open and not settings_section.current
        assert [c.key for c in settings_section.children if c.current] == ["connections"]
        assert not section(shell, "status").children

    def test_a_section_page_without_sub_pages_is_current(self):
        assert section(build_shell("/requests", Needs()), "requests").current

    def test_nothing_wrong_is_quiet(self):
        shell = build_shell("/status", Needs())
        assert shell.pill is None
        assert all(s.badge is None for s in shell.sections)

    def test_services_that_need_you(self):
        shell = build_shell("/runs", Needs(services={"sonarr": "warn"}))
        assert shell.pill == "Sonarr needs you" and shell.pill_level == "warn"
        badge = section(shell, "status").badge
        assert (badge.count, badge.level, badge.label) == (1, "warn", "1 service needs you")

        shell = build_shell("/runs", Needs(services={"sonarr": "warn", "radarr": "bad"}))
        assert shell.pill == "2 services need you" and shell.pill_level == "bad"
        assert section(shell, "status").badge.level == "bad"

    def test_stuck_queues_are_sub_pages_of_status(self):
        shell = build_shell("/status", Needs(services={"sonarr": "warn"}, stuck={"sonarr"}))
        assert [c.label for c in section(shell, "status").children] == ["Incident history", "Sonarr queue"]
        # a queue page you're on stays listed after its downloads are removed
        shell = build_shell("/status/radarr/queue", Needs())
        children = section(shell, "status").children
        assert [(c.label, c.current) for c in children] == [("Incident history", False), ("Radarr queue", True)]

    def test_activity_counts_plans_and_failed_runs(self):
        badge = section(build_shell("/status", Needs(plans=1)), "activity").badge
        assert (badge.count, badge.level, badge.label) == (1, "info", "1 plan to review")
        badge = section(build_shell("/status", Needs(plans=1, failed=2)), "activity").badge
        assert (badge.count, badge.level) == (3, "bad")
        assert badge.label == "1 plan to review, 2 failed runs in the last day"


@pytest.fixture
def client(settings):  # noqa: F811
    app = create_app(settings, keys=FakeKeys(), run_job=fake_run_job, load_config=live_config, run_scheduler=False)
    with TestClient(app) as c:
        yield c


class TestPages:
    def test_every_page_has_the_shell(self, client):
        page = client.get("/runs", headers=auth()).text
        assert '<nav class="sidebar" aria-label="Main">' in page
        assert '<a href="/runs" aria-current="page">Runs</a>' in page
        assert 'href="#main">Skip to content' in page and '<main id="main">' in page
        # the page's actions are in the toolbar
        assert '<section class="toolbar" aria-label="Page actions">' in page
        assert 'href="/schedules/new"><svg class="icon"' in page

    def test_settings_tabs_are_sidebar_sub_pages(self, client):
        page = client.get("/settings/trash", headers=auth()).text
        assert 'class="tabs"' not in page
        assert '<a href="/settings/trash" aria-current="page">Empty trash</a>' in page
        assert "<h1>Empty trash</h1>" in page
        # radarr's and sonarr's options and rules are the Movers section now
        assert 'href="/settings/radarr"' not in page and 'href="/movers"' in page

    def test_a_page_without_actions_has_no_toolbar(self, client):
        assert 'class="toolbar"' not in client.get("/settings/connections", headers=auth()).text

    def test_open_incidents_and_failed_runs_show_on_every_page(self, client):
        now = utc_now()
        with Session(client.app.state.engine) as session:
            session.add(HealthIncident(service="sonarr", key="queue:Severance S02E07", level="warn", text="stuck",
                                       started_at=now, last_seen_at=now, ended_at=None))
            session.add(HealthIncident(service="radarr", key="down", level="bad", text="down",
                                       started_at=now - timedelta(days=2), last_seen_at=now - timedelta(days=1),
                                       ended_at=now - timedelta(days=1)))
            session.add(JobRun(job="movers", trigger="manual", status="failed", created_at=now, finished_at=now))
            session.add(JobRun(job="movers", trigger="manual", status="failed", created_at=now - timedelta(days=3),
                               finished_at=now - timedelta(days=3)))
            session.commit()
        page = client.get("/settings", headers=auth()).text
        # only the ongoing incident counts
        assert 'class="tag tag-solid-warn state-pill" href="/status">Sonarr needs you</a>' in page
        assert '<span class="visually-hidden">1 service needs you</span>' in page
        # only the failed run from the last day
        assert '<span class="visually-hidden">1 failed run in the last day</span>' in page
        status = client.get("/status/history", headers=auth()).text
        assert '<a href="/status/sonarr/queue">Sonarr queue</a>' in status
