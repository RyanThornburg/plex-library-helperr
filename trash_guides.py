"""trash_guides.py - the trash_sync job: keeps radarr's and sonarr's custom formats,
quality profiles and quality sizes in line with TRaSH Guides (trash-guides.info).

The guides publish their data as json in the TRaSH-Guides/Guides repo (docs/json):
custom formats with a trash_id, a score per score set and their specifications, quality
profiles listing the custom formats they score, and quality sizes. A run reads them at
one commit (a GuideLoader, web.guides caches them), compares them with the arr and plans
each difference as a PlannedChange for review. Applying a reviewed plan reads the arr
again, plans again from the same commit and only sends a change whose fingerprint is the
one approved. Nothing is ever deleted: custom formats and profiles the guides don't name
are left alone"""

import hashlib
import io
import json
import logging
import re
import tarfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any, Protocol

import requests

from arr_client import ArrV3
from config.models import ArrConfig, ConfigSettings, TrashProfile, TrashSettings, parse_score_override
from logging_config import failure_text
from results import JobResult, PlannedChange

logger = logging.getLogger(__name__)

REPO = "TRaSH-Guides/Guides"
JSON_DIR = "docs/json"
# the latest commit that changed the guides' json (unauthenticated: 60 requests an hour)
COMMITS_URL = f"https://api.github.com/repos/{REPO}/commits"
# a snapshot of the repo at a commit, ~25 MB, of which only docs/json is kept
TARBALL_URL = f"https://codeload.github.com/{REPO}/tar.gz/{{commit}}"
TIMEOUT = 120
ARRS = ("radarr", "sonarr")
# what's kept of each arr's json: custom formats, quality profiles and quality sizes
KINDS = {"cf": "formats", "quality-profiles": "profiles", "quality-size": "sizes"}
# the guide's json fields a run never needs, left out of the stored snapshot
DROPPED = ("trash_description", "trash_regex", "$schema")
# a regex shown in a plan is cut to this
SHOWN = 120


# ---------------------------------------------------------------------------
# the guides
# ---------------------------------------------------------------------------


@dataclass
class ArrGuide:
    """one arr's part of the guides, each keyed by trash_id (sizes by their type)"""

    formats: dict[str, dict[str, Any]] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    sizes: dict[str, dict[str, Any]] = field(default_factory=dict)
    _names: dict[str, dict[str, Any]] | None = field(default=None, init=False, repr=False)

    def format_named(self, name: str) -> dict[str, Any] | None:
        if self._names is None:
            self._names = {}
            for cf in self.formats.values():
                self._names.setdefault(cf["name"].casefold(), cf)
        return self._names.get(name.casefold())

    def score_sets(self) -> list[str]:
        return sorted({s for cf in self.formats.values() for s in cf.get("trash_scores", {})})


@dataclass
class Guides:
    commit: str
    radarr: ArrGuide
    sonarr: ArrGuide

    @classmethod
    def from_data(cls, commit: str, data: Mapping[str, Any]) -> "Guides":
        """from a snapshot: {"radarr": {"formats": [...], "profiles": [...], "sizes": [...]}, ...}"""
        arrs = {}
        for arr in ARRS:
            part = data.get(arr, {})
            arrs[arr] = ArrGuide(
                formats={cf["trash_id"]: cf for cf in part.get("formats", [])},
                profiles={p["trash_id"]: p for p in part.get("profiles", [])},
                sizes={s["type"]: s for s in part.get("sizes", [])},
            )
        return cls(commit, arrs["radarr"], arrs["sonarr"])

    def arr(self, arr_name: str) -> ArrGuide:
        return getattr(self, arr_name)


# guides at a commit, or the latest when None (web.guides.load_guides)
GuideLoader = Callable[[str | None], Guides]


def latest_commit(session: requests.Session | None = None) -> str:
    """the newest commit that changed the guides' json"""
    response = (session or requests).get(
        COMMITS_URL,
        params={"path": JSON_DIR, "per_page": 1},
        headers={"Accept": "application/vnd.github+json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    commits = response.json()
    if not isinstance(commits, list) or not commits or not isinstance(commits[0].get("sha"), str):
        raise ValueError("GitHub didn't say which commit the guides are at")
    return commits[0]["sha"]


def snapshot_from_tarball(data: bytes) -> dict[str, Any]:
    """the json the runs use, from the repo's tarball: {arr: {kind: [objects]}}"""
    found: dict[str, dict[str, list[dict[str, Any]]]] = {arr: {k: [] for k in KINDS.values()} for arr in ARRS}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            # <repo>-<commit>/docs/json/<arr>/<kind>/<name>.json
            parts = member.name.split("/")
            if not member.isfile() or len(parts) != 6 or parts[1:3] != JSON_DIR.split("/"):
                continue
            arr, kind, name = parts[3], parts[4], parts[5]
            if arr not in ARRS or kind not in KINDS or not name.endswith(".json"):
                continue
            file = tar.extractfile(member)
            if file is None:
                continue
            try:
                item = json.loads(file.read())
            except ValueError:
                logger.warning("TRASH\t %s isn't json, left out", member.name)
                continue
            if not isinstance(item, dict) or ("trash_id" not in item and kind != "quality-size"):
                continue
            found[arr][KINDS[kind]].append({k: v for k, v in item.items() if k not in DROPPED})
    if not any(found[arr]["formats"] for arr in ARRS):
        raise ValueError("The guides' download has no custom formats in it")
    return found


def download(commit: str, session: requests.Session | None = None) -> dict[str, Any]:
    response = (session or requests).get(TARBALL_URL.format(commit=commit), timeout=TIMEOUT)
    response.raise_for_status()
    return snapshot_from_tarball(response.content)


# ---------------------------------------------------------------------------
# what the arr has
# ---------------------------------------------------------------------------


@dataclass
class ArrState:
    """an arr's custom formats, quality profiles, quality definitions and languages, as
    its api gives them"""

    formats: list[dict[str, Any]]
    profiles: list[dict[str, Any]]
    definitions: list[dict[str, Any]]
    languages: list[dict[str, Any]] = field(default_factory=list)
    # a new quality profile as the arr makes it (every quality and custom format), what
    # an imported guide profile starts from
    schema: dict[str, Any] | None = None

    def format_named(self, name: str) -> dict[str, Any] | None:
        return next((f for f in self.formats if f["name"].casefold() == name.casefold()), None)

    def profile_named(self, name: str) -> dict[str, Any] | None:
        return next((p for p in self.profiles if p["name"].casefold() == name.casefold()), None)


class ArrSettingsApi(Protocol):
    def state(self) -> ArrState: ...
    def add_format(self, body: dict[str, Any]) -> None: ...
    def update_format(self, format_id: int, body: dict[str, Any]) -> None: ...
    def update_profile(self, profile_id: int, body: dict[str, Any]) -> None: ...
    def add_profile(self, body: dict[str, Any]) -> None: ...
    def update_definitions(self, body: list[dict[str, Any]]) -> None: ...


class ArrSettings(ArrV3):
    """the arr's /api/v3 settings endpoints the sync reads and writes"""

    def __init__(self, baseurl: str, apikey: str, log_name: str):
        super().__init__(baseurl, apikey)
        self.log_name = log_name

    def state(self) -> ArrState:
        return ArrState(
            formats=self._request("GET", "customformat"),
            profiles=self._request("GET", "qualityprofile"),
            definitions=self._request("GET", "qualitydefinition"),
            # sonarr v4's profiles have no language
            languages=self._request("GET", "language") if self.log_name == "RADARR" else [],
            schema=self._request("GET", "qualityprofile/schema"),
        )

    def add_format(self, body: dict[str, Any]) -> None:
        self._request("POST", "customformat", json=body)

    def update_format(self, format_id: int, body: dict[str, Any]) -> None:
        self._request("PUT", f"customformat/{format_id}", json=body)

    def update_profile(self, profile_id: int, body: dict[str, Any]) -> None:
        self._request("PUT", f"qualityprofile/{profile_id}", json=body)

    def add_profile(self, body: dict[str, Any]) -> None:
        self._request("POST", "qualityprofile", json=body)

    def update_definitions(self, body: list[dict[str, Any]]) -> None:
        self._request("PUT", "qualitydefinition/update", json=body)


# ---------------------------------------------------------------------------
# planning (pure)
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """a planned change and what applying it sends, given the arr's state just before"""

    change: PlannedChange
    send: Callable[[ArrSettingsApi, ArrState], None]
    # format names a profile change needs to exist in the arr first
    needs: list[str] = field(default_factory=list)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _fields(spec: Mapping[str, Any]) -> dict[str, Any]:
    """a specification's fields as {name: value}; the arr lists them, the guides map them"""
    fields = spec.get("fields") or {}
    if isinstance(fields, list):
        return {f.get("name"): f.get("value") for f in fields if isinstance(f, Mapping)}
    return dict(fields)


def _short(value: Any) -> str:
    text = json.dumps(value) if not isinstance(value, str) else value
    return text if len(text) <= SHOWN else text[: SHOWN - 1] + "…"


# a regex or value compared word by word: escapes (\b), names, numbers and punctuation
# one at a time
TOKENS = re.compile(r"\\.|[\w.'+-]+|\s+|.", re.S)
WORD = re.compile(r"\w[\w.'+-]*")


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def text_diff(old: str, new: str) -> dict[str, Any]:
    """two values as segments ({text, changed}) for showing what differs, and the words
    one has that the other hasn't (a release group moved between tiers, say)"""
    a, b = TOKENS.findall(old), TOKENS.findall(new)
    have: list[dict[str, Any]] = []
    want: list[dict[str, Any]] = []
    added: list[str] = []
    removed: list[str] = []

    def push(segments: list[dict[str, Any]], text: str, changed: bool) -> None:
        if not text:
            return
        if segments and segments[-1]["changed"] == changed:
            segments[-1]["text"] += text
        else:
            segments.append({"text": text, "changed": changed})

    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            push(have, "".join(a[i1:i2]), False)
            push(want, "".join(b[j1:j2]), False)
            continue
        push(have, "".join(a[i1:i2]), True)
        push(want, "".join(b[j1:j2]), True)
        removed += [t for t in a[i1:i2] if WORD.fullmatch(t)]
        added += [t for t in b[j1:j2] if WORD.fullmatch(t)]
    # a word moved within the value is neither
    moved = set(added) & set(removed)
    return {
        "have": have,
        "want": want,
        "added": [w for w in added if w not in moved],
        "removed": [w for w in removed if w not in moved],
    }


def spec_diff(have: Mapping[str, Any], want: Mapping[str, Any]) -> list[dict[str, Any]]:
    """how an arr custom format differs from the guide's, a row per difference:
    condition, what (renaming, added, removed, implementation, negate, required or a
    field's name), have and want. Fields the guide doesn't set aren't compared: the arr
    adds its own defaults (exceptLanguage: false)"""
    rows: list[dict[str, Any]] = []

    def row(condition: str, what: str, old: Any, new: Any) -> None:
        rows.append({"condition": condition, "what": what, "have": old, "want": new})

    if bool(have.get("includeCustomFormatWhenRenaming")) != bool(want.get("includeCustomFormatWhenRenaming")):
        row("", "renaming", bool(have.get("includeCustomFormatWhenRenaming")), bool(want.get("includeCustomFormatWhenRenaming")))
    have_specs = {s.get("name"): s for s in have.get("specifications", [])}
    want_specs = {s.get("name"): s for s in want.get("specifications", [])}
    for name, spec in want_specs.items():
        old = have_specs.get(name)
        if old is None:
            row(name, "added", None, _text(_fields(spec).get("value")))
            continue
        if old.get("implementation") != spec.get("implementation"):
            row(name, "implementation", old.get("implementation"), spec.get("implementation"))
        for key in ("negate", "required"):
            if bool(old.get(key)) != bool(spec.get(key)):
                row(name, key, bool(old.get(key)), bool(spec.get(key)))
        old_fields = _fields(old)
        for key, value in _fields(spec).items():
            if old_fields.get(key) != value:
                row(name, key, _text(old_fields.get(key)), _text(value))
                rows[-1]["text"] = text_diff(_text(old_fields.get(key)), _text(value))
    for name in sorted(have_specs.keys() - want_specs.keys(), key=str):
        row(name, "removed", _text(_fields(have_specs[name]).get("value")), None)
    return rows


def spec_changes(have: dict[str, Any], want: dict[str, Any]) -> list[str]:
    """spec_diff in words"""
    lines = []
    for r in spec_diff(have, want):
        name, what = r["condition"], r["what"]
        if what == "renaming":
            lines.append(f"Include in renaming: {r['want']}")
        elif what == "added":
            lines.append(f"Adds condition {name}: {_short(r['want'])}")
        elif what == "removed":
            lines.append(f"Removes condition {name}")
        elif what == "implementation":
            lines.append(f"Condition {name}: type {r['have']} → {r['want']}")
        else:
            lines.append(f"Condition {name}: {what} {_short(r['have'])} → {_short(r['want'])}")
    return lines


def format_body(guide_cf: Mapping[str, Any], have: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """the arr's custom format json for a guide's"""
    body = dict(have or {})
    body["name"] = guide_cf["name"]
    body["includeCustomFormatWhenRenaming"] = bool(guide_cf.get("includeCustomFormatWhenRenaming"))
    body["specifications"] = [
        {
            "name": s["name"],
            "implementation": s["implementation"],
            "negate": bool(s.get("negate")),
            "required": bool(s.get("required")),
            "fields": [{"name": k, "value": v} for k, v in _fields(s).items()],
        }
        for s in guide_cf.get("specifications", [])
    ]
    return body


def guide_score(cf: Mapping[str, Any], score_set: str) -> int | None:
    scores = cf.get("trash_scores") or {}
    value = scores.get(score_set, scores.get("default"))
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class ProfilePlan:
    """what a linked profile should score, and why some can't be"""

    scores: dict[str, int]  # custom format name -> score
    settings: dict[str, Any] | None
    problems: list[str]


def desired_profile(guide: ArrGuide, state: ArrState, link: TrashProfile, have: Mapping[str, Any]) -> ProfilePlan:
    """the scores (and settings, when linked with them) a profile gets: every custom
    format of the guide profile, every one the profile already scores that the guides
    have, by the guide's score set, then the overrides"""
    profile = guide.profiles.get(link.guide)
    if profile is None:
        return ProfilePlan({}, None, [f"The guides have no quality profile {link.guide} any more"])
    score_set = link.score_set or profile.get("trash_score_set") or "default"
    wanted: dict[str, dict[str, Any]] = {}
    for trash_id in (profile.get("formatItems") or {}).values():
        if cf := guide.formats.get(trash_id):
            wanted[cf["name"].casefold()] = cf
    for item in have.get("formatItems", []):
        if item.get("score") and (cf := guide.format_named(item.get("name", ""))):
            wanted.setdefault(cf["name"].casefold(), cf)
    scores = {}
    for cf in wanted.values():
        score = guide_score(cf, score_set)
        if score is not None:
            scores[cf["name"]] = score
    problems = []
    for name, score in link.overrides().items():
        arr_cf = state.format_named(name)
        if arr_cf is None and guide.format_named(name) is None:
            problems.append(f"Score override for {name}: no custom format by that name")
            continue
        real = (arr_cf or guide.format_named(name) or {}).get("name", name)
        scores = {k: v for k, v in scores.items() if k.casefold() != name}
        scores[real] = score
    settings = profile_settings(profile, state, problems) if link.settings else None
    return ProfilePlan(scores, settings, problems)


def _quality_ids(state: ArrState, have: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """the arr's qualities by name, from a profile's items"""
    found: dict[str, dict[str, Any]] = {}
    for profile in [have, *state.profiles]:
        for item in profile.get("items", []):
            for q in [item, *item.get("items", [])]:
                if q.get("quality"):
                    found.setdefault(q["quality"]["name"], q["quality"])
    return found


def profile_settings(profile: Mapping[str, Any], state: ArrState, problems: list[str]) -> dict[str, Any]:
    """a guide profile's settings, by name: the arr's ids are worked out when applying"""
    settings: dict[str, Any] = {
        "upgradeAllowed": bool(profile.get("upgradeAllowed")),
        "cutoff": profile.get("cutoff"),
        "minFormatScore": profile.get("minFormatScore", 0),
        "cutoffFormatScore": profile.get("cutoffFormatScore", 0),
        "minUpgradeFormatScore": profile.get("minUpgradeFormatScore", 1),
        # the guide lists the best first, the arrs the worst first
        "items": [
            {"name": i["name"], "allowed": bool(i.get("allowed")), "items": list(i.get("items") or [])}
            for i in profile.get("items", [])
        ],
    }
    if state.languages and profile.get("language"):
        if any(l.get("name") == profile["language"] for l in state.languages):
            settings["language"] = profile["language"]
        else:
            problems.append(f"Language {profile['language']} isn't one the arr has")
    return settings


def current_settings(have: Mapping[str, Any]) -> dict[str, Any]:
    """an arr profile's settings in the guide's terms"""
    ids = {}
    for item in have.get("items", []):
        if item.get("quality"):
            ids[item["quality"]["id"]] = item["quality"]["name"]
        elif item.get("id") is not None:
            ids[item["id"]] = item.get("name")
    settings = {
        "upgradeAllowed": bool(have.get("upgradeAllowed")),
        "cutoff": ids.get(have.get("cutoff")),
        "minFormatScore": have.get("minFormatScore", 0),
        "cutoffFormatScore": have.get("cutoffFormatScore", 0),
        "minUpgradeFormatScore": have.get("minUpgradeFormatScore", 1),
        "items": [
            {
                "name": i.get("name") or i["quality"]["name"],
                "allowed": bool(i.get("allowed")),
                "items": [q["quality"]["name"] for q in reversed(i.get("items", []))] if i.get("items") else [],
            }
            for i in reversed(have.get("items", []))
        ],
    }
    if isinstance(have.get("language"), Mapping):
        settings["language"] = have["language"].get("name")
    return settings


SETTING_NAMES = {
    "upgradeAllowed": "Upgrades allowed",
    "cutoff": "Upgrade until",
    "minFormatScore": "Minimum custom format score",
    "cutoffFormatScore": "Upgrade until custom format score",
    "minUpgradeFormatScore": "Minimum score increment",
    "language": "Language",
}


def _items_text(items: list[dict[str, Any]]) -> str:
    allowed = [i["name"] + (f" ({', '.join(i['items'])})" if i["items"] else "") for i in items if i["allowed"]]
    return ", ".join(allowed) or "none"


def allowed_items(settings: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """the allowed qualities, best first, as {name, items}"""
    return [{"name": i["name"], "items": i["items"]} for i in (settings or {}).get("items", []) if i["allowed"]]


def settings_diff(have: Mapping[str, Any] | None, want: Mapping[str, Any]) -> dict[str, Any]:
    """{"settings": [{setting, have, want}], "qualities": {have, want} or None}, the
    settings that differ (all of them for a new profile, have None)"""
    rows = [
        {"setting": label, "have": (have or {}).get(key), "want": want[key]}
        for key, label in SETTING_NAMES.items()
        if key in want and (have is None or have.get(key) != want[key])
    ]
    # only the allowed qualities and their order matter: the arr never grabs the others
    qualities = None
    if have is None or allowed_items(have) != allowed_items(want):
        qualities = {"have": allowed_items(have) if have is not None else None, "want": allowed_items(want)}
    return {"settings": rows, "qualities": qualities}


def settings_changes(have: dict[str, Any], want: dict[str, Any]) -> list[str]:
    diff = settings_diff(have, want)
    lines = [f"{r['setting']}: {r['have']} → {r['want']}" for r in diff["settings"]]
    if diff["qualities"]:
        lines.append(f"Qualities (best first): {_items_text(have['items'])} → {_items_text(want['items'])}")
    return lines


def profile_body(have: Mapping[str, Any], plan: ProfilePlan, state: ArrState) -> dict[str, Any]:
    """the arr's profile json with the planned scores and settings. Raises ValueError
    when it needs something the arr doesn't have"""
    body = json.loads(json.dumps(have))
    ids = {f["name"].casefold(): f["id"] for f in state.formats}
    missing = [name for name in plan.scores if name.casefold() not in ids]
    if missing:
        raise ValueError(f"needs custom format{'s' if len(missing) > 1 else ''} {', '.join(missing)}, which the arr doesn't have")
    by_id = {item["format"]: item for item in body.get("formatItems", [])}
    for name, score in plan.scores.items():
        format_id = ids[name.casefold()]
        if format_id in by_id:
            by_id[format_id]["score"] = score
        else:
            body.setdefault("formatItems", []).append({"format": format_id, "name": name, "score": score})
    if plan.settings is not None:
        apply_settings(body, plan.settings, state)
    return body


def apply_settings(body: dict[str, Any], want: Mapping[str, Any], state: ArrState) -> None:
    qualities = _quality_ids(state, body)
    groups = {i.get("name"): i.get("id") for i in body.get("items", []) if not i.get("quality")}
    next_id = max([1000, *[i for i in groups.values() if isinstance(i, int)]]) + 1
    items = []
    named: dict[str, int] = {}
    listed: set[str] = set()
    for item in want["items"]:
        if item["items"]:
            members = []
            for name in item["items"]:
                if name not in qualities:
                    raise ValueError(f"quality {name} isn't one the arr has")
                listed.add(name)
                members.append({"quality": qualities[name], "items": [], "allowed": item["allowed"]})
            group_id = groups.get(item["name"])
            if group_id is None:
                group_id, next_id = next_id, next_id + 1
            named[item["name"]] = group_id
            items.append({"id": group_id, "name": item["name"], "allowed": item["allowed"], "items": list(reversed(members))})
        else:
            if item["name"] not in qualities:
                raise ValueError(f"quality {item['name']} isn't one the arr has")
            listed.add(item["name"])
            named[item["name"]] = qualities[item["name"]]["id"]
            items.append({"quality": qualities[item["name"]], "items": [], "allowed": item["allowed"]})
    # qualities the guide leaves out stay, not allowed, at the bottom
    rest = [{"quality": q, "items": [], "allowed": False} for name, q in qualities.items() if name not in listed]
    body["items"] = rest + list(reversed(items))
    if want.get("cutoff") not in named:
        raise ValueError(f"the cutoff {want.get('cutoff')} isn't in the profile's qualities")
    body["cutoff"] = named[want["cutoff"]]
    for key in ("upgradeAllowed", "minFormatScore", "cutoffFormatScore", "minUpgradeFormatScore"):
        body[key] = want[key]
    if "language" in want:
        body["language"] = next(l for l in state.languages if l.get("name") == want["language"])


def size_changes(
    definitions: list[dict[str, Any]], guide_sizes: Mapping[str, Any]
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """lines, the definitions to send and the diff's rows ({quality, have, want}: min,
    preferred and max in MB/min), for the qualities the guide sizes"""
    by_name = {d["quality"]["name"]: d for d in definitions}
    lines, changed, rows = [], [], []
    for q in guide_sizes.get("qualities", []):
        have = by_name.get(q.get("quality"))
        if have is None:
            continue
        want = {"minSize": q.get("min"), "preferredSize": q.get("preferred"), "maxSize": q.get("max")}
        if any(v is not None and have.get(k) != v for k, v in want.items()):
            new = {**have, **{k: v for k, v in want.items() if v is not None}}
            changed.append(new)
            rows.append({
                "quality": q["quality"],
                "have": [have.get("minSize"), have.get("preferredSize"), have.get("maxSize")],
                "want": [new["minSize"], new["preferredSize"], new["maxSize"]],
            })
            lines.append(
                f"{q['quality']}: {have.get('minSize')}–{have.get('preferredSize')}–{have.get('maxSize')} → "
                f"{new['minSize']}–{new['preferredSize']}–{new['maxSize']} MB/min"
            )
    return lines, changed, rows


def score_diff(current: Mapping[str, int], plan: ProfilePlan, link: TrashProfile) -> dict[str, Any]:
    """the scores that change ({name, have (None: not scored yet), want, yours}), how
    many already match and the overrides kept"""
    overrides = link.overrides()
    rows, matching = [], 0
    for name, score in sorted(plan.scores.items(), key=lambda kv: kv[0].casefold()):
        old = current.get(name.casefold())
        if old == score:
            matching += 1
        else:
            rows.append({"name": name, "have": old, "want": score, "yours": name.casefold() in overrides})
    yours = [{"name": n, "score": s} for n, s in sorted(plan.scores.items(), key=lambda kv: kv[0].casefold()) if n.casefold() in overrides]
    return {"scores": rows, "matching": matching, "yours": yours}


def new_profile_body(state: ArrState, name: str, plan: ProfilePlan) -> dict[str, Any]:
    """an imported guide profile as the arr's json: its new-profile template with the
    plan's scores and settings. Raises ValueError when it can't be made"""
    if state.schema is None:
        raise ValueError("the arr's new profile template couldn't be read")
    body = json.loads(json.dumps(state.schema))
    body.pop("id", None)
    body["name"] = name
    return profile_body(body, plan, state)


def plan_arr(
    service: str, guide: ArrGuide, state: ArrState, trash: TrashSettings
) -> tuple[list[Step], list[str]]:
    """every change the arr needs to be in line, and problems with the links (a profile
    that's gone, an override for a format nobody has). Pure"""
    arr = service.lower()
    steps: list[Step] = []
    problems: list[str] = []
    links: list[tuple[TrashProfile, dict[str, Any], ProfilePlan]] = []
    imports: list[tuple[TrashProfile, ProfilePlan]] = []
    for link in trash.profiles:
        have = state.profile_named(link.profile)
        if have is None and link.create:
            if link.guide not in guide.profiles:
                problems.append(f"{link.profile}: the guides have no quality profile {link.guide} any more")
                continue
            if state.schema is None:
                problems.append(f"{link.profile}: {service.title()}'s new profile template couldn't be read")
                continue
            plan = desired_profile(guide, state, link, state.schema)
            problems += [f"{link.profile}: {p}" for p in plan.problems]
            try:
                apply_settings(json.loads(json.dumps(state.schema)), plan.settings or {}, state)
            except ValueError as e:
                problems.append(f"{link.profile} can't be made: {e}")
                continue
            imports.append((link, plan))
            continue
        if have is None:
            problems.append(f"{service.title()} has no quality profile {link.profile}")
            continue
        plan = desired_profile(guide, state, link, have)
        problems += [f"{link.profile}: {p}" for p in plan.problems]
        links.append((link, have, plan))

    # custom formats: the ones named as in the guides, and the ones linked profiles score
    if trash.formats:
        for arr_cf in state.formats:
            guide_cf = guide.format_named(arr_cf["name"])
            if guide_cf is None:
                continue
            lines = spec_changes(arr_cf, guide_cf)
            if lines:
                body = format_body(guide_cf, arr_cf)
                steps.append(
                    Step(
                        PlannedChange(
                            service, f"{arr}:format:{guide_cf['trash_id']}", "format_update", guide_cf["name"], lines,
                            fingerprint(format_body(guide_cf)), "dry_run",
                            diff={"conditions": spec_diff(arr_cf, guide_cf)},
                        ),
                        lambda api, _, i=arr_cf["id"], b=body: api.update_format(i, b),
                    )
                )

    # the custom formats linked and imported profiles score that the arr doesn't have yet
    missing: dict[str, dict[str, Any]] = {}
    scored_by: dict[str, list[str]] = {}
    for link, plan in [(link, plan) for link, _, plan in links] + imports:
        for name in plan.scores:
            if state.format_named(name) is None and (cf := guide.format_named(name)):
                missing.setdefault(cf["trash_id"], cf)
                scored_by.setdefault(cf["trash_id"], []).append(link.profile)
    for trash_id, cf in sorted(missing.items(), key=lambda kv: kv[1]["name"].casefold()):
        body = format_body(cf)
        conditions = [f"{s['name']}: {_short(s['fields'][0]['value'] if s['fields'] else '')}" for s in body["specifications"]]
        steps.append(
            Step(
                PlannedChange(
                    service, f"{arr}:format:{trash_id}", "format_add", cf["name"],
                    [f"New custom format, conditions: {'; '.join(conditions) or 'none'}"],
                    fingerprint(body), "dry_run",
                    diff={
                        "conditions": [
                            {
                                "condition": s["name"],
                                "implementation": s["implementation"],
                                "value": _text(_fields(s).get("value")),
                                "negate": s["negate"],
                                "required": s["required"],
                            }
                            for s in body["specifications"]
                        ],
                        "scored_by": scored_by[trash_id],
                    },
                ),
                lambda api, _, b=body: api.add_format(b),
            )
        )

    def follows(link: TrashProfile) -> dict[str, str]:
        profile = guide.profiles[link.guide]
        return {"follows": profile["name"], "score_set": link.score_set or profile.get("trash_score_set") or "default"}

    for link, have, plan in links:
        current = {f["name"].casefold(): f.get("score", 0) for f in have.get("formatItems", [])}
        lines = []
        needs = []
        for name, score in sorted(plan.scores.items(), key=lambda kv: kv[0].casefold()):
            old = current.get(name.casefold())
            if old is None:
                needs.append(name)
                lines.append(f"{name}: new custom format, scored {score}")
            elif old != score:
                lines.append(f"{name}: {old} → {score}")
        if plan.settings is not None:
            lines += settings_changes(current_settings(have), plan.settings)
        if not lines:
            continue
        diff = {**follows(link), **score_diff(current, plan, link), "settings": [], "qualities": None}
        if plan.settings is not None:
            diff.update(settings_diff(current_settings(have), plan.settings))
        guide_name = guide.profiles[link.guide]["name"]
        steps.append(
            Step(
                PlannedChange(
                    service, f"{arr}:profile:{have['id']}", "profile", have["name"], lines,
                    fingerprint({"scores": plan.scores, "settings": plan.settings, "from": [current.get(n.casefold()) for n in plan.scores]}),
                    "dry_run", f"follows {guide_name}" + (f", {link.score_set} scores" if link.score_set else ""),
                    diff=diff,
                ),
                lambda api, st, p=have["id"], pl=plan: api.update_profile(p, profile_body(_profile(st, p), pl, st)),
                needs,
            )
        )

    for link, plan in imports:
        guide_name = guide.profiles[link.guide]["name"]
        settings = plan.settings or {}
        diff = {**follows(link), **score_diff({}, plan, link), **settings_diff(None, settings)}
        lines = [
            f"New quality profile from {guide_name}: {len(plan.scores)} custom formats scored, "
            f"qualities (best first) {_items_text(settings.get('items', []))}, upgrade until {settings.get('cutoff')}"
        ]
        steps.append(
            Step(
                PlannedChange(
                    service, f"{arr}:profile_add:{link.profile.casefold()}", "profile_add", link.profile, lines,
                    fingerprint({"name": link.profile, "scores": plan.scores, "settings": plan.settings}),
                    "dry_run", f"follows {guide_name}" + (f", {link.score_set} scores" if link.score_set else ""),
                    diff=diff,
                ),
                lambda api, st, n=link.profile, pl=plan: api.add_profile(new_profile_body(st, n, pl)),
                [name for name in plan.scores if state.format_named(name) is None],
            )
        )

    if trash.quality_sizes:
        sizes = guide.sizes.get(trash.quality_sizes)
        if sizes is None:
            problems.append(f"The guides have no {trash.quality_sizes} quality sizes")
        else:
            lines, changed, rows = size_changes(state.definitions, sizes)
            if changed:
                steps.append(
                    Step(
                        PlannedChange(
                            service, f"{arr}:sizes", "sizes", f"Quality sizes ({trash.quality_sizes})", lines,
                            fingerprint([(d["quality"]["name"], d["minSize"], d["preferredSize"], d["maxSize"]) for d in changed]),
                            "dry_run", diff={"sizes": rows},
                        ),
                        lambda api, _, c=changed: api.update_definitions(c),
                    )
                )
    return steps, problems


def _profile(state: ArrState, profile_id: int) -> dict[str, Any]:
    profile = next((p for p in state.profiles if p["id"] == profile_id), None)
    if profile is None:
        raise ValueError("the profile is gone")
    return profile


# ---------------------------------------------------------------------------
# the job
# ---------------------------------------------------------------------------


def _send(step: Step, api: ArrSettingsApi, state: ArrState, log_name: str) -> None:
    change = step.change
    try:
        step.send(api, state)
        change.status = "done"
        logger.info("%s\t TRaSH: %s %s", log_name, change.kind.replace("_", " "), change.title)
    except Exception as e:
        change.status = "failed"
        change.detail = (change.detail + "; " if change.detail else "") + failure_text(e)
        logger.error("%s\t TRaSH: %s %s failed: %s", log_name, change.kind.replace("_", " "), change.title, failure_text(e))


def sync_arr(
    log_name: str,
    arr: ArrConfig,
    guide: ArrGuide,
    result: JobResult,
    approved: Mapping[str, str] | None,
    auto: bool,
    api: ArrSettingsApi,
) -> None:
    """plan one arr, then apply what was approved (or, planning, what auto_formats lets
    through). Approved changes go formats first, so a profile's new format exists"""
    state = api.state()
    steps, problems = plan_arr(log_name, guide, state, arr.trash)
    for problem in problems:
        logger.warning("%s\t TRaSH: %s", log_name, problem)
        result.errors.append(f"{log_name.title()}: {problem}")
    if approved is None:
        for step in steps:
            if auto and step.change.kind == "format_update":
                _send(step, api, state, log_name)
            result.plan.append(step.change)
        logger.info("%s\t TRaSH: %s changes planned", log_name, sum(s.change.status == "dry_run" for s in steps))
        return
    planned = {s.change.key: s for s in steps}
    order = {"format_update": 0, "format_add": 1, "sizes": 2, "profile": 3, "profile_add": 3}
    chosen = []
    for key, print_ in approved.items():
        step = planned.get(key)
        if step is None:
            result.plan.append(
                PlannedChange(log_name, key, key.split(":")[1], key, [], print_, "done", "Already in line, nothing sent")
            )
        elif step.change.fingerprint != print_:
            step.change.status = "blocked"
            step.change.detail = "Changed since the plan was made, nothing sent. Run TRaSH Guides sync again for a fresh plan"
            result.plan.append(step.change)
        else:
            chosen.append(step)
    fresh = None
    for step in sorted(chosen, key=lambda s: order[s.change.kind]):
        if step.change.kind in ("profile", "profile_add"):
            # read once after the formats were sent, for their ids
            fresh = fresh or api.state()
            have = {f["name"].casefold() for f in fresh.formats}
            if missing := [n for n in step.needs if n.casefold() not in have]:
                step.change.status = "blocked"
                step.change.detail = f"Needs {', '.join(missing)} added first: approve that too"
                result.plan.append(step.change)
                continue
        _send(step, api, fresh or state, log_name)
        result.plan.append(step.change)


def run_trash_sync(
    settings: ConfigSettings,
    result: JobResult,
    guides: GuideLoader,
    approved: Mapping[str, str] | None = None,
    commit: str | None = None,
    auto: bool = True,
    apis: Mapping[str, ArrSettingsApi] | None = None,
) -> None:
    """plan (approved None) or apply a reviewed plan ({key: fingerprint}, at the plan's
    commit). `auto` lets each arr's auto_formats apply format fixes while planning; a
    forced dry run passes False"""
    arrs = [(name, getattr(settings, name)) for name in ARRS]
    arrs = [(name, arr) for name, arr in arrs if arr is not None and arr.trash.enabled]
    if approved is not None:
        arrs = [(name, arr) for name, arr in arrs if any(k.startswith(f"{name}:") for k in approved)]
    if not arrs:
        logger.info("TRASH\t No Radarr or Sonarr is set to follow TRaSH Guides (Settings → TRaSH Guides)")
        return
    loaded = guides(commit)
    result.guide_commit = loaded.commit
    logger.info("TRASH\t Guides at %s", loaded.commit[:12])
    for name, arr in arrs:
        log_name = name.upper()
        logger.info("-" * 90)
        logger.info("STARTING %s TRASH GUIDES SYNC", log_name)
        if not arr.baseurl or not arr.apikey:
            result.errors.append(f"{log_name.title()} isn't connected")
            continue
        api = (apis or {}).get(name) or ArrSettings(arr.baseurl, arr.apikey, log_name)
        mine = None if approved is None else {k: v for k, v in approved.items() if k.startswith(f"{name}:")}
        try:
            sync_arr(log_name, arr, loaded.arr(name), result, mine, auto and arr.trash.auto_formats, api)
        except Exception as e:
            logger.error("%s\t TRaSH Guides sync failed: %s", log_name, failure_text(e))
            result.errors.append(f"{log_name.title()} TRaSH Guides sync failed: {failure_text(e)}")


def profile_table(guide: ArrGuide, state: ArrState, link: TrashProfile) -> dict[str, Any] | None:
    """every custom format a linked profile scores, for Settings → TRaSH Guides' editor:
    the arr's score now (None: the arr lacks the format, or the profile is still to be
    made), the guide's (None: not a guide format of this profile) and yours (the
    override, None: follow the guide), differing ones first; and the formats it could
    score too (options). None when the guide profile is gone"""
    if link.guide not in guide.profiles:
        return None
    have = state.profile_named(link.profile)
    exists = have is not None
    if have is None:
        if state.schema is None:
            return None
        have = state.schema
    base = desired_profile(guide, state, replace(link, scores=[]), have)
    guide_scores = {name.casefold(): score for name, score in base.scores.items()}
    names = {name.casefold(): name for name in base.scores}
    overrides: dict[str, int] = {}
    for line in link.scores:
        name, score = parse_score_override(line)
        real = (state.format_named(name) or guide.format_named(name) or {}).get("name", name)
        names.setdefault(name.casefold(), real)
        overrides[name.casefold()] = score
    current = {f["name"].casefold(): f.get("score", 0) for f in have.get("formatItems", [])} if exists else {}
    in_arr = {f["name"].casefold() for f in state.formats}
    rows = []
    for key, name in names.items():
        yours = overrides.get(key)
        wanted = yours if yours is not None else guide_scores.get(key)
        now = current.get(key, 0) if exists and key in in_arr else None
        rows.append(
            {
                "name": name,
                "now": now,
                "guide": guide_scores.get(key),
                "yours": yours,
                "in_arr": key in in_arr,
                "known": key in in_arr or guide.format_named(name) is not None,
                "differs": now != wanted,
            }
        )
    rows.sort(key=lambda r: (not r["differs"], r["name"].casefold()))
    known = {f["name"] for f in state.formats} | {cf["name"] for cf in guide.formats.values()}
    # the guide profile's settings, shown whether or not the link follows them
    want = profile_settings(guide.profiles[link.guide], state, [])
    now = current_settings(have) if exists else None
    settings = [
        {"setting": label, "now": (now or {}).get(key), "guide": want[key], "differs": now is None or now.get(key) != want[key]}
        for key, label in SETTING_NAMES.items()
        if key in want
    ]
    qualities = {
        "now": allowed_items(now) if now is not None else None,
        "guide": allowed_items(want),
        "differs": now is None or allowed_items(now) != allowed_items(want),
    }
    return {
        "rows": rows,
        "exists": exists,
        "differ": sum(r["differs"] for r in rows),
        "yours": len(overrides),
        "options": sorted((n for n in known if n.casefold() not in names), key=str.casefold),
        "settings": settings,
        "qualities": qualities,
    }


def score_set_names(guide: ArrGuide) -> list[str]:
    return guide.score_sets()


def suggest_links(guide: ArrGuide, profiles: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """arr profile name -> guide profile trash_id, for profiles named as in the guides"""
    by_name = {p["name"].casefold(): trash_id for trash_id, p in guide.profiles.items()}
    return {p["name"]: by_name[p["name"].casefold()] for p in profiles if p["name"].casefold() in by_name}
