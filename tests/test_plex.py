"""Tests for plex.py"""

from unittest.mock import MagicMock, patch
from xml.etree import ElementTree

import pytest
import requests
from plexapi import exceptions as plexapi_exceptions

import plex
from config.models import EmptyTrashSettings, PlexConfig
from results import JobResult

PLEX_CONFIG = PlexConfig(baseurl="http://plex:32400", token="token")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_section(
    title: str, type: str = "show", key: int = 1, refreshing: bool = False
) -> MagicMock:
    section = MagicMock()
    section.title = title
    section.type = type
    section.key = key
    section.refreshing = refreshing
    return section


def trash_item(
    title: str, files: list[str], kept: str | None = None, type: str = "episode"
) -> ElementTree.Element:
    """a trashed plex item: deleted versions of `files`, plus a live `kept` version when
    an upgrade left the old ones behind"""
    attrs = {"type": type, "title": title}
    if type == "episode":
        attrs |= {"grandparentTitle": "Survivor", "parentIndex": "1", "index": "2"}
    else:
        attrs["year"] = "2025"
    item = ElementTree.Element("Video", attrs)
    for i, file in enumerate([kept, *files]):
        if file is None:
            continue
        media = ElementTree.SubElement(item, "Media", {} if i == 0 else {"deletedAt": "1792669302"})
        ElementTree.SubElement(media, "Part", {"file": file})
    return item


def make_server(
    sections: list[MagicMock],
    trash: dict[int, int | None],
    items: dict[int, list[ElementTree.Element]] | None = None,
) -> MagicMock:
    """trash maps section key to the totalSize plex returns for its trash query, items to
    the items listed (by default that many upgrade leftovers)"""
    server = MagicMock()
    server.library.sections.return_value = sections

    def section(title: str) -> MagicMock:
        for s in sections:
            if s.title == title:
                return s
        raise plexapi_exceptions.NotFound(title)

    server.library.section.side_effect = section

    def query(path: str) -> ElementTree.Element:
        key = int(path.split("/")[3])
        size = trash[key]
        if "X-Plex-Container-Size=0" not in path:
            container = ElementTree.Element("MediaContainer")
            listed = (items or {}).get(key)
            if listed is None:
                listed = [
                    trash_item(f"old {i}", [f"/tv/old{i}.mkv"], kept="/tv/new.mkv")
                    for i in range(size or 0)
                ]
            container.extend(listed)
            return container
        return ElementTree.Element(
            "MediaContainer", {} if size is None else {"totalSize": str(size)}
        )

    server.query.side_effect = query
    return server


def run(server: MagicMock, removed_files=None, **kwargs) -> JobResult:
    settings = EmptyTrashSettings(**kwargs)
    result = JobResult("empty_trash")
    with patch.object(plex, "setup_plex_server", return_value=server):
        plex.empty_plex_trash(PLEX_CONFIG, settings, result, removed_files)
    return result


def statuses(result: JobResult) -> dict[str, str]:
    return {c.target: c.status for c in result.changes}


# ---------------------------------------------------------------------------
# empty_plex_trash
# ---------------------------------------------------------------------------


class TestEmptyPlexTrash:
    def test_empties_sections_with_trash(self):
        tv = make_section("TV Shows", key=1)
        movies = make_section("Movies", type="movie", key=2)
        result = run(make_server([tv, movies], {1: 5, 2: 3}))
        tv.emptyTrash.assert_called_once()
        movies.emptyTrash.assert_called_once()
        assert statuses(result) == {"TV Shows": "done", "Movies": "done"}
        assert result.changes[0].detail == "5 old versions after upgrades, 0 fully deleted"

    def test_counts_episodes_in_show_libraries(self):
        tv = make_section("TV Shows", key=1)
        server = make_server([tv], {1: 5})
        run(server)
        path = server.query.call_args_list[0].args[0]
        assert path.startswith("/library/sections/1/all?")
        assert "type=4" in path
        assert "trash=1" in path

    def test_counts_movies_in_movie_libraries(self):
        movies = make_section("Movies", type="movie", key=2)
        server = make_server([movies], {2: 5})
        run(server)
        assert "type=1" in server.query.call_args_list[0].args[0]

    def test_empty_trash_is_not_emptied(self):
        tv = make_section("TV Shows", key=1)
        result = run(make_server([tv], {1: 0}))
        tv.emptyTrash.assert_not_called()
        assert result.changes == []

    def test_over_max_items_is_skipped(self):
        tv = make_section("TV Shows", key=1)
        movies = make_section("Movies", type="movie", key=2)
        result = run(make_server([tv, movies], {1: 11, 2: 10}), max_items=10)
        tv.emptyTrash.assert_not_called()
        movies.emptyTrash.assert_called_once()
        assert statuses(result) == {"TV Shows": "blocked", "Movies": "done"}

    def test_refreshing_section_is_skipped(self):
        tv = make_section("TV Shows", key=1, refreshing=True)
        server = make_server([tv], {1: 5})
        result = run(server)
        tv.emptyTrash.assert_not_called()
        server.query.assert_not_called()
        assert statuses(result) == {"TV Shows": "blocked"}

    def test_missing_count_is_skipped(self):
        tv = make_section("TV Shows", key=1)
        result = run(make_server([tv], {1: None}))
        tv.emptyTrash.assert_not_called()
        assert statuses(result) == {"TV Shows": "failed"}

    def test_dry_run_does_not_empty(self):
        tv = make_section("TV Shows", key=1)
        result = run(make_server([tv], {1: 5}), dry_run=True)
        tv.emptyTrash.assert_not_called()
        assert statuses(result) == {"TV Shows": "dry_run"}

    def test_all_libraries_skips_unsupported_types(self):
        music = make_section("Music", type="artist", key=3)
        tv = make_section("TV Shows", key=1)
        server = make_server([music, tv], {1: 5})
        run(server)
        music.emptyTrash.assert_not_called()
        tv.emptyTrash.assert_called_once()

    def test_configured_libraries_only(self):
        tv = make_section("TV Shows", key=1)
        movies = make_section("Movies", type="movie", key=2)
        run(make_server([tv, movies], {1: 5, 2: 5}), libraries=["TV Shows"])
        tv.emptyTrash.assert_called_once()
        movies.emptyTrash.assert_not_called()

    def test_configured_unsupported_library_is_skipped(self):
        music = make_section("Music", type="artist", key=3)
        server = make_server([music], {})
        run(server, libraries=["Music"])
        music.emptyTrash.assert_not_called()
        server.query.assert_not_called()

    def test_missing_library_does_not_stop_others(self):
        tv = make_section("TV Shows", key=1)
        result = run(make_server([tv], {1: 5}), libraries=["Nope", "TV Shows"])
        tv.emptyTrash.assert_called_once()
        assert result.errors == ["Plex library not found: Nope"]

    @pytest.mark.parametrize(
        "error",
        [
            plexapi_exceptions.BadRequest("bad"),
            requests.exceptions.ConnectionError("down"),
        ],
    )
    def test_section_failure_does_not_stop_others(self, error):
        tv = make_section("TV Shows", key=1)
        tv.emptyTrash.side_effect = error
        movies = make_section("Movies", type="movie", key=2)
        result = run(make_server([tv, movies], {1: 5, 2: 5}))
        movies.emptyTrash.assert_called_once()
        assert statuses(result) == {"TV Shows": "failed", "Movies": "done"}

    def test_failure_loading_sections_is_logged(self):
        server = make_server([], {})
        server.library.sections.side_effect = requests.exceptions.ConnectionError()
        result = run(server)
        assert len(result.errors) == 1

    def test_plex_not_configured(self):
        result = JobResult("empty_trash")
        with patch.object(plex, "setup_plex_server") as setup:
            plex.empty_plex_trash(None, EmptyTrashSettings(), result)
        setup.assert_not_called()
        assert result.errors == ["Plex server not configured"]


class TestTrashListing:
    def test_lists_fully_deleted_and_counts_upgrades(self):
        tv = make_section("TV Shows", key=1)
        items = [
            trash_item("Pilot", ["/mnt/nfs/tv/S01E02.mkv"]),
            trash_item(
                "Upgraded",
                ["/mnt/nfs/tv/a 720p.mkv", "/mnt/nfs/tv/a SD.mkv"],
                kept="/mnt/nfs/tv/a 1080p.mkv",
            ),
        ]
        result = run(make_server([tv], {1: 2}, {1: items}))
        [change] = result.changes
        assert change.detail == "1 old versions after upgrades, 1 fully deleted"
        assert change.items == ["Survivor - S01E02 - Pilot"]
        [trashed] = result.trash
        assert trashed.library == "TV Shows"
        assert trashed.files == ["/mnt/nfs/tv/S01E02.mkv"]
        assert trashed.removed_by_run is None

    def test_movie_titles_have_the_year(self):
        movies = make_section("Movies", type="movie", key=2)
        items = [trash_item("Frankenstein", ["/mnt/nfs/movies/f.mkv"], type="movie")]
        result = run(make_server([movies], {2: 1}, {2: items}))
        assert result.trash[0].title == "Frankenstein (2025)"

    def test_files_the_removers_deleted_are_marked_by_name(self):
        tv = make_section("TV Shows", key=1)
        items = [trash_item("Pilot", ["/mnt/nfs/tv/Show/S01E02.mkv"])]
        # sonarr's path differs, the name matches
        result = run(make_server([tv], {1: 1}, {1: items}), removed_files={"S01E02.mkv": 7})
        assert result.trash[0].removed_by_run == 7
        assert result.changes[0].detail == (
            "0 old versions after upgrades, 1 fully deleted (1 by removers)"
        )

    def test_too_many_unexplained_deletions_block(self):
        tv = make_section("TV Shows", key=1)
        items = [trash_item(f"Ep {i}", [f"/mnt/nfs/tv/{i}.mkv"]) for i in range(3)]
        result = run(make_server([tv], {1: 3}, {1: items}), max_deleted_items=2)
        tv.emptyTrash.assert_not_called()
        assert statuses(result) == {"TV Shows": "blocked"}
        assert "3 not by removers is over max_deleted_items (2)" in result.changes[0].detail
        # still listed, so they can be checked
        assert len(result.trash) == 3

    def test_removers_deletions_dont_count_toward_the_limit(self):
        tv = make_section("TV Shows", key=1)
        items = [trash_item(f"Ep {i}", [f"/mnt/nfs/tv/{i}.mkv"]) for i in range(3)]
        removed = {f"{i}.mkv": 4 for i in range(3)}
        run(make_server([tv], {1: 3}, {1: items}), removed_files=removed, max_deleted_items=0)
        tv.emptyTrash.assert_called_once()

    def test_upgrades_never_count_toward_the_limit(self):
        tv = make_section("TV Shows", key=1)
        run(make_server([tv], {1: 40}), max_deleted_items=0)
        tv.emptyTrash.assert_called_once()

    def test_over_max_items_is_blocked_before_listing(self):
        tv = make_section("TV Shows", key=1)
        server = make_server([tv], {1: 11})
        run(server, max_items=10)
        # only the count, a missing mount's whole library isn't fetched
        assert server.query.call_count == 1
