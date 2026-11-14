"""Tests for the TRaSH Guides sync (trash_guides.py, web/guides.py, Settings → TRaSH Guides)"""

import copy
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

import trash_guides
from config.models import ArrConfig, ConfigSettings, TrashProfile, TrashSettings
from results import JobResult
from trash_guides import ArrState, Guides, current_settings, plan_arr, run_trash_sync, snapshot_from_tarball, spec_changes
from tests.test_settings_web import FakeLookups, connect_radarr, document, post, version
from tests.test_web import FakeKeys, auth
from tests.test_web import settings  # noqa: F401 (fixture)
from web import guides as guide_store
from web.app import create_app


def cf(trash_id, name, value, scores=None, **extra):
    return {
        "trash_id": trash_id,
        "name": name,
        "trash_scores": scores or {"default": 0},
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            {"name": name, "implementation": "ReleaseTitleSpecification", "negate": False, "required": True, "fields": {"value": value}}
        ],
        **extra,
    }


GUIDE_DATA = {
    "radarr": {
        "formats": [
            cf("t-amzn", "AMZN", r"\b(amzn)\b", {"default": 70}),
            cf("t-lq", "LQ", r"\b(yify)\b", {"default": -10000, "sqp-1-1080p": -20000}),
            cf("t-dv", "DV", r"\b(dv)\b", {"default": 1500}),
            cf("t-notorig", "Language: Not Original", "x", {"default": -10000}),
            cf("t-german", "German DL", "german", {"default": -10000, "german": 11000}),
        ],
        "profiles": [
            {
                "trash_id": "p-hd",
                "name": "HD Bluray + WEB",
                "upgradeAllowed": True,
                "cutoff": "Bluray-1080p",
                "minFormatScore": 0,
                "cutoffFormatScore": 10000,
                "minUpgradeFormatScore": 1,
                "language": "Original",
                "items": [
                    {"name": "Bluray-1080p", "allowed": True},
                    {"name": "WEB 1080p", "allowed": True, "items": ["WEBRip-1080p", "WEBDL-1080p"]},
                    {"name": "Bluray-720p", "allowed": False},
                ],
                "formatItems": {"AMZN": "t-amzn", "LQ": "t-lq"},
            },
        ],
        "sizes": [{"type": "movie", "qualities": [{"quality": "Bluray-1080p", "min": 50.4, "preferred": 1999, "max": 2000}]}],
    },
    "sonarr": {"formats": [], "profiles": [], "sizes": []},
}


def quality(i, name):
    return {"id": i, "name": name, "source": "x", "resolution": 1080}


def arr_profile(**overrides):
    profile = {
        "id": 6,
        "name": "HD Bluray + WEB",
        "upgradeAllowed": True,
        "cutoff": 7,
        "minFormatScore": 0,
        "cutoffFormatScore": 10000,
        "minUpgradeFormatScore": 1,
        "language": {"id": -2, "name": "Original"},
        "items": [
            {"quality": quality(6, "Bluray-720p"), "items": [], "allowed": False},
            {"id": 1001, "name": "WEB 1080p", "allowed": True, "items": [
                {"quality": quality(3, "WEBDL-1080p"), "items": [], "allowed": True},
                {"quality": quality(15, "WEBRip-1080p"), "items": [], "allowed": True},
            ]},
            {"quality": quality(7, "Bluray-1080p"), "items": [], "allowed": True},
        ],
        "formatItems": [
            {"format": 1, "name": "AMZN", "score": 70},
            {"format": 2, "name": "LQ", "score": -10000},
            {"format": 3, "name": "DV", "score": 0},
        ],
    }
    profile.update(overrides)
    return profile


def arr_format(i, name, value):
    return {
        "id": i,
        "name": name,
        "includeCustomFormatWhenRenaming": False,
        "specifications": [{
            "name": name, "implementation": "ReleaseTitleSpecification", "negate": False, "required": True,
            "fields": [{"name": "value", "value": value}, {"name": "exceptLanguage", "value": False}],
        }],
    }


def arr_state():
    return ArrState(
        formats=[arr_format(1, "AMZN", r"\b(amzn)\b"), arr_format(2, "LQ", r"\b(yify)\b"), arr_format(3, "DV", r"\b(dv)\b"), arr_format(9, "My own", "x")],
        profiles=[arr_profile()],
        definitions=[{"id": 9, "quality": {"id": 7, "name": "Bluray-1080p"}, "minSize": 50.4, "preferredSize": 1999, "maxSize": 2000}],
        languages=[{"id": -2, "name": "Original"}, {"id": 1, "name": "English"}],
    )


GUIDES = Guides.from_data("c0ffee" * 6 + "abcd", GUIDE_DATA)


def guide():
    return Guides.from_data(GUIDES.commit, copy.deepcopy(GUIDE_DATA)).radarr


def keys(steps):
    return [(s.change.kind, s.change.title) for s in steps]


class TestPlan:
    def test_in_line_plans_nothing(self):
        # radarr's own exceptLanguage default isn't a difference
        steps, problems = plan_arr("RADARR", guide(), arr_state(), TrashSettings(formats=True, quality_sizes="movie", profiles=[TrashProfile("HD Bluray + WEB", "p-hd", settings=True)]))
        assert (steps, problems) == ([], [])

    def test_a_fixed_regex_is_a_format_update(self):
        state = arr_state()
        state.formats[1] = arr_format(2, "LQ", r"\b(yts)\b")
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(formats=True))
        [step] = steps
        assert (step.change.key, step.change.kind, step.change.lines) == (
            "radarr:format:t-lq", "format_update", [r"Condition LQ: value \b(yts)\b → \b(yify)\b"],
        )

    def test_formats_off_leaves_definitions_alone(self):
        state = arr_state()
        state.formats[1] = arr_format(2, "LQ", "old")
        assert plan_arr("RADARR", guide(), state, TrashSettings())[0] == []

    def test_spec_changes_in_words(self):
        have = arr_format(1, "X", "a")
        want = {"includeCustomFormatWhenRenaming": True, "specifications": [
            {"name": "Y", "implementation": "ReleaseTitleSpecification", "negate": True, "required": True, "fields": {"value": "b"}},
        ]}
        assert spec_changes(have, want) == ["Include in renaming: True", "Adds condition Y: b", "Removes condition X"]

    def test_profile_scores_follow_the_guide_and_its_score_set(self):
        state = arr_state()
        state.profiles = [arr_profile(formatItems=[
            {"format": 1, "name": "AMZN", "score": 0},
            {"format": 2, "name": "LQ", "score": -10000},
            {"format": 3, "name": "DV", "score": 100},  # scored, so kept in line too
        ])]
        link = TrashProfile("HD Bluray + WEB", "p-hd", score_set="sqp-1-1080p")
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(profiles=[link]))
        [step] = steps
        assert step.change.key == "radarr:profile:6"
        assert step.change.lines == ["AMZN: 0 → 70", "DV: 100 → 1500", "LQ: -10000 → -20000"]
        assert step.change.detail == "follows HD Bluray + WEB, sqp-1-1080p scores"

    def test_overrides_win_and_unscored_formats_are_left_alone(self):
        state = arr_state()
        state.profiles = [arr_profile(formatItems=[
            {"format": 1, "name": "AMZN", "score": 70}, {"format": 2, "name": "LQ", "score": -30000},
            {"format": 3, "name": "DV", "score": 0}, {"format": 9, "name": "My own", "score": 5},
        ])]
        link = TrashProfile("hd bluray + web", "p-hd", scores=["lq = -30000", "My own = 50"])
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(profiles=[link]))
        assert steps[0].change.lines == ["My own: 5 → 50"]

    def test_an_override_for_an_unknown_format_is_a_problem(self):
        _, problems = plan_arr("RADARR", guide(), arr_state(), TrashSettings(profiles=[TrashProfile("HD Bluray + WEB", "p-hd", scores=["Nope = 1"])]))
        assert problems == ["HD Bluray + WEB: Score override for nope: no custom format by that name"]

    def test_a_missing_format_is_added_and_the_profile_waits_for_it(self):
        g = guide()
        g.profiles["p-hd"]["formatItems"]["DV HDR10"] = "t-new"
        g.formats["t-new"] = cf("t-new", "DV HDR10", "hdr", {"default": 500})
        steps, _ = plan_arr("RADARR", g, arr_state(), TrashSettings(profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]))
        assert keys(steps) == [("format_add", "DV HDR10"), ("profile", "HD Bluray + WEB")]
        assert steps[1].needs == ["DV HDR10"]
        assert steps[1].change.lines == ["DV HDR10: new custom format, scored 500"]

    def test_settings_only_when_linked_with_them(self):
        state = arr_state()
        state.profiles = [arr_profile(minFormatScore=-5000, cutoff=1001)]
        assert plan_arr("RADARR", guide(), state, TrashSettings(profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]))[0] == []
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(profiles=[TrashProfile("HD Bluray + WEB", "p-hd", settings=True)]))
        assert steps[0].change.lines == ["Upgrade until: WEB 1080p → Bluray-1080p", "Minimum custom format score: -5000 → 0"]

    def test_allowed_qualities_in_order_are_compared(self):
        state = arr_state()
        items = arr_profile()["items"]
        state.profiles = [arr_profile(items=[items[0], items[2], items[1]])]  # bluray below web
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(profiles=[TrashProfile("HD Bluray + WEB", "p-hd", settings=True)]))
        assert steps[0].change.lines == [
            "Qualities (best first): WEB 1080p (WEBRip-1080p, WEBDL-1080p), Bluray-1080p → Bluray-1080p, WEB 1080p (WEBRip-1080p, WEBDL-1080p)"
        ]
        assert current_settings(arr_profile())["items"][0]["name"] == "Bluray-1080p"

    def test_quality_sizes(self):
        state = arr_state()
        state.definitions[0]["minSize"] = 20
        steps, _ = plan_arr("RADARR", guide(), state, TrashSettings(quality_sizes="movie"))
        assert steps[0].change.lines == ["Bluray-1080p: 20–1999–2000 → 50.4–1999–2000 MB/min"]
        _, problems = plan_arr("RADARR", guide(), state, TrashSettings(quality_sizes="anime"))
        assert problems == ["The guides have no anime quality sizes"]

    def test_a_linked_profile_that_is_gone(self):
        _, problems = plan_arr("RADARR", guide(), arr_state(), TrashSettings(profiles=[TrashProfile("Gone", "p-hd")]))
        assert problems == ["Radarr has no quality profile Gone"]


class FakeArr:
    def __init__(self, state):
        self.current = state
        self.sent = []

    def state(self):
        return copy.deepcopy(self.current)

    def add_format(self, body):
        self.sent.append(("add", body["name"]))
        self.current.formats.append({**body, "id": 50 + len(self.current.formats)})

    def update_format(self, format_id, body):
        self.sent.append(("format", format_id, body["specifications"][0]["fields"]))
        self.current.formats = [body if f["id"] == format_id else f for f in self.current.formats]

    def update_profile(self, profile_id, body):
        self.sent.append(("profile", profile_id, {i["name"]: i["score"] for i in body["formatItems"]}))
        self.current.profiles = [body if p["id"] == profile_id else p for p in self.current.profiles]

    def update_definitions(self, body):
        self.sent.append(("sizes", body))


def config(**trash):
    arr = ArrConfig(baseurl="http://radarr", apikey="k", tag=None, trash=TrashSettings(**trash))
    return ConfigSettings(plex=None, radarr=arr, sonarr=None)


def drifted():
    state = arr_state()
    state.formats[1] = arr_format(2, "LQ", "old")
    state.profiles = [arr_profile(formatItems=[{"format": 1, "name": "AMZN", "score": 0}, {"format": 2, "name": "LQ", "score": -10000}])]
    return state


class TestJob:
    def plan(self, fake, **trash):
        result = JobResult("trash_sync")
        run_trash_sync(config(**trash), result, lambda commit: GUIDES, apis={"radarr": fake})
        return result

    def test_plans_without_sending_anything(self):
        fake = FakeArr(drifted())
        result = self.plan(fake, formats=True, profiles=[TrashProfile("HD Bluray + WEB", "p-hd")])
        assert fake.sent == []
        assert [(p.key, p.status) for p in result.plan] == [("radarr:format:t-lq", "dry_run"), ("radarr:profile:6", "dry_run")]
        assert result.guide_commit == GUIDES.commit

    def test_auto_formats_applies_format_fixes_only(self):
        fake = FakeArr(drifted())
        result = self.plan(fake, formats=True, auto_formats=True, profiles=[TrashProfile("HD Bluray + WEB", "p-hd")])
        assert [s[0] for s in fake.sent] == ["format"]
        assert [(p.kind, p.status) for p in result.plan] == [("format_update", "done"), ("profile", "dry_run")]

    def test_a_forced_dry_run_applies_nothing(self):
        fake = FakeArr(drifted())
        result = JobResult("trash_sync")
        run_trash_sync(config(formats=True, auto_formats=True), result, lambda c: GUIDES, auto=False, apis={"radarr": fake})
        assert fake.sent == [] and result.plan[0].status == "dry_run"

    def test_applies_only_what_was_approved_and_unchanged(self):
        fake = FakeArr(drifted())
        planned = self.plan(fake, formats=True, profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]).plan
        approved = {p.key: p.fingerprint for p in planned if p.kind == "profile"}
        seen = []
        result = JobResult("trash_sync")
        run_trash_sync(config(formats=True, profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]), result,
                       lambda commit: seen.append(commit) or GUIDES, approved, GUIDES.commit, apis={"radarr": fake})
        assert seen == [GUIDES.commit]
        assert fake.sent == [("profile", 6, {"AMZN": 70, "LQ": -10000})]
        assert [(p.key, p.status) for p in result.plan] == [("radarr:profile:6", "done")]

    def test_a_change_since_the_plan_is_blocked(self):
        fake = FakeArr(drifted())
        planned = self.plan(fake, profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]).plan
        fake.current.profiles[0]["formatItems"][0]["score"] = 5  # edited by hand meanwhile
        result = JobResult("trash_sync")
        run_trash_sync(config(profiles=[TrashProfile("HD Bluray + WEB", "p-hd")]), result, lambda c: GUIDES,
                       {p.key: p.fingerprint for p in planned}, GUIDES.commit, apis={"radarr": fake})
        assert fake.sent == [] and result.plan[0].status == "blocked"

    def test_already_in_line_sends_nothing(self):
        fake = FakeArr(arr_state())
        result = JobResult("trash_sync")
        run_trash_sync(config(formats=True), result, lambda c: GUIDES, {"radarr:format:t-lq": "x"}, GUIDES.commit, apis={"radarr": fake})
        assert fake.sent == [] and (result.plan[0].status, result.plan[0].detail) == ("done", "Already in line, nothing sent")

    def test_a_new_format_is_sent_before_the_profile_that_scores_it(self):
        data = copy.deepcopy(GUIDE_DATA)
        data["radarr"]["formats"].append(cf("t-new", "DV HDR10", "hdr", {"default": 500}))
        data["radarr"]["profiles"][0]["formatItems"]["DV HDR10"] = "t-new"
        g = Guides.from_data(GUIDES.commit, data)
        fake = FakeArr(arr_state())
        trash = {"profiles": [TrashProfile("HD Bluray + WEB", "p-hd")]}
        result = JobResult("trash_sync")
        run_trash_sync(config(**trash), result, lambda c: g, apis={"radarr": fake})
        approved = {p.key: p.fingerprint for p in result.plan}
        # the profile alone waits for the format
        only = JobResult("trash_sync")
        run_trash_sync(config(**trash), only, lambda c: g, {"radarr:profile:6": approved["radarr:profile:6"]}, g.commit, apis={"radarr": fake})
        assert only.plan[0].status == "blocked" and "approve that too" in only.plan[0].detail
        both = JobResult("trash_sync")
        run_trash_sync(config(**trash), both, lambda c: g, dict(reversed(list(approved.items()))), g.commit, apis={"radarr": fake})
        assert [s[0] for s in fake.sent] == ["add", "profile"]
        assert fake.sent[1][2]["DV HDR10"] == 500

    def test_nothing_configured(self):
        result = JobResult("trash_sync")
        run_trash_sync(ConfigSettings(plex=None, radarr=None, sonarr=None), result, lambda c: pytest.fail("no guides needed"))
        assert result.plan == [] and result.errors == []


def tarball(files: dict[str, object]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, content in files.items():
            data = content if isinstance(content, bytes) else json.dumps(content).encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_snapshot_keeps_only_the_json_runs_use():
    data = tarball({
        "Guides-abc/docs/json/radarr/cf/amzn.json": {**GUIDE_DATA["radarr"]["formats"][0], "trash_description": "long"},
        "Guides-abc/docs/json/radarr/quality-profiles/hd.json": GUIDE_DATA["radarr"]["profiles"][0],
        "Guides-abc/docs/json/radarr/quality-size/movie.json": GUIDE_DATA["radarr"]["sizes"][0],
        "Guides-abc/docs/json/radarr/cf-groups/x.json": {"trash_id": "g"},
        "Guides-abc/docs/json/radarr/cf/broken.json": b"{not json",
        "Guides-abc/docs/images/x.png": b"png",
    })
    snapshot = snapshot_from_tarball(data)
    assert [c["name"] for c in snapshot["radarr"]["formats"]] == ["AMZN"]
    assert "trash_description" not in snapshot["radarr"]["formats"][0]
    assert len(snapshot["radarr"]["profiles"]) == 1 and snapshot["radarr"]["sizes"][0]["type"] == "movie"
    with pytest.raises(ValueError):
        snapshot_from_tarball(tarball({"Guides-abc/README.md": b"x"}))


class TestGuideStore:
    @pytest.fixture
    def engine(self, tmp_path):
        from web.db import make_engine, migrate

        engine = make_engine(tmp_path / "helperr.db")
        migrate(engine)
        return engine

    def test_downloads_only_when_the_guides_changed(self, engine, monkeypatch):
        downloads = []
        monkeypatch.setattr(trash_guides, "latest_commit", lambda: "a" * 40)
        monkeypatch.setattr(trash_guides, "download", lambda commit: downloads.append(commit) or GUIDE_DATA)
        assert guide_store.load_guides(engine).commit == "a" * 40
        assert guide_store.load_guides(engine).radarr.profiles["p-hd"]["name"] == "HD Bluray + WEB"
        assert downloads == ["a" * 40]
        assert guide_store.status(engine)["commit"] == "a" * 40

    def test_github_down_uses_the_stored_guides(self, engine, monkeypatch):
        monkeypatch.setattr(trash_guides, "download", lambda commit: GUIDE_DATA)
        guide_store.load_guides(engine, "b" * 40)

        def down():
            raise ConnectionError("no route")

        monkeypatch.setattr(trash_guides, "latest_commit", down)
        assert guide_store.load_guides(engine).commit == "b" * 40
        assert "Couldn't ask GitHub" in guide_store.status(engine)["error"]

    def test_github_down_without_stored_guides_raises(self, engine, monkeypatch):
        monkeypatch.setattr(trash_guides, "latest_commit", lambda: (_ for _ in ()).throw(ConnectionError("x")))
        with pytest.raises(RuntimeError):
            guide_store.load_guides(engine)

    def test_keeps_a_few_snapshots(self, engine, monkeypatch):
        monkeypatch.setattr(trash_guides, "download", lambda commit: GUIDE_DATA)
        for i in range(guide_store.KEEP + 2):
            guide_store.load_guides(engine, f"{i:040d}")
        from sqlalchemy.orm import Session

        from web.db import GuideSnapshot

        with Session(engine) as session:
            assert session.query(GuideSnapshot).count() == guide_store.KEEP


# ---------------------------------------------------------------------------
# the web app
# ---------------------------------------------------------------------------


@pytest.fixture
def client(settings, monkeypatch):  # noqa: F811
    from main import run_job

    monkeypatch.setattr(trash_guides, "latest_commit", lambda: GUIDES.commit)
    monkeypatch.setattr(trash_guides, "download", lambda commit: GUIDE_DATA)
    fake = FakeArr(drifted())
    monkeypatch.setattr(trash_guides, "ArrSettings", lambda url, key, log_name: fake)
    monkeypatch.setattr("web.guides_routes.arr_profiles", lambda request, arr: (fake.state().profiles, None))
    app = create_app(settings, keys=FakeKeys(), run_job=run_job, run_scheduler=False, lookups=FakeLookups())
    with TestClient(app) as c:
        c.fake = fake
        yield c


def wait(client):
    client.app.state.runner.wait_idle()


def link_hd(client, **extra):
    connect_radarr(client)
    fields = {"version": version(client), "formats": "1", "profile": ["HD Bluray + WEB"], "guide_0": "p-hd", **extra}
    return post(client, "/settings/trash-guides/radarr", fields)


class TestPages:
    def test_settings_suggest_links_once_the_guides_are_stored(self, client):
        connect_radarr(client)
        page = client.get("/settings/trash-guides", headers=auth()).text
        assert "Download the guides" in page
        assert post(client, "/settings/trash-guides/check", {}).status_code == 303
        page = client.get("/settings/trash-guides", headers=auth()).text
        assert '<option value="p-hd" selected>HD Bluray + WEB</option>' in page and "suggested" in page
        assert '<option value="sqp-1-1080p">sqp-1-1080p</option>' in page

    def test_save(self, client):
        response = link_hd(client, score_set_0="german", settings_0="1", scores_0="LQ = -30000\n\n", quality_sizes="movie")
        assert response.status_code == 303
        assert document(client)["radarr"]["trash"] == {
            "formats": True, "quality_sizes": "movie",
            "profiles": [{"profile": "HD Bluray + WEB", "guide": "p-hd", "score_set": "german", "settings": True, "scores": ["LQ = -30000"]}],
        }

    def test_bad_override_saves_nothing(self, client):
        response = link_hd(client, scores_0="LQ is bad")
        assert response.status_code == 400 and "must be &#39;custom format name = score&#39;" in response.text
        assert "trash" not in document(client)["radarr"]

    def test_auto_formats_asks_first(self, client):
        response = link_hd(client, auto_formats="1")
        assert response.status_code == 200 and "TRaSH Guides sync" in response.text and "confirm_live" in response.text
        assert "trash" not in document(client)["radarr"]

    def test_plan_review_and_apply(self, client):
        link_hd(client)
        run = client.post("/api/jobs/trash_sync", json={"dry_run": False}, headers={**auth(), "Origin": "http://testserver"}).json()
        wait(client)
        page = client.get(f"/runs/{run['id']}", headers=auth()).text
        assert "Planned changes" in page and 'value="radarr:profile:6"' in page and "AMZN: 0 → 70" in page
        confirm = client.get(f"/runs/{run['id']}/apply?item=radarr:profile:6", headers=auth()).text
        assert "Apply 1 change" in confirm and "Nothing is deleted" in confirm
        response = client.post(f"/runs/{run['id']}/apply?item=radarr:profile:6", headers={**auth(), "Origin": "http://testserver"}, follow_redirects=False)
        assert response.status_code == 303
        wait(client)
        assert client.fake.sent == [("profile", 6, {"AMZN": 70, "LQ": -10000})]
        applied = client.get(response.headers["location"], headers=auth()).text
        assert "Applied" in applied and "status-done" in applied

    def test_applying_a_plan_doesnt_need_the_removers_switch(self, client):
        assert client.app.state.runner.allow_apply is False
        link_hd(client)
        run = client.post("/api/jobs/trash_sync", json={"dry_run": False}, headers={**auth(), "Origin": "http://testserver"}).json()
        wait(client)
        response = client.post(f"/api/runs/{run['id']}/apply", json={"items": ["radarr:format:t-lq"]}, headers={**auth(), "Origin": "http://testserver"})
        assert response.status_code == 202
        wait(client)
        assert [s[0] for s in client.fake.sent] == ["format"]
