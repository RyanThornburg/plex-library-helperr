"""Tests for arr_client: library snapshots and the bulk editor"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import arr_client
from arr_client import edit_media, read_media


class Partial:
    """an arrapi object whose every read of an unset field would be a request"""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        raise AssertionError(f"{name} would be loaded from the api")


class TestReadMedia:
    def api(self):
        api = MagicMock()
        api.all_tags.return_value = [SimpleNamespace(id=3, label="kids")]
        api.quality_profile.return_value = [SimpleNamespace(id=2, name="HD-1080p")]
        api.all_movies.return_value = [Partial(id=1, title="Heat", tagsIds=[3], qualityProfileId=2)]
        return api

    def test_three_requests_and_no_reloads(self):
        api = self.api()
        [movie] = read_media(api, "all_movies")
        assert (movie.title, [t.label for t in movie.tags], movie.qualityProfile.name) == ("Heat", ["kids"], "HD-1080p")
        assert api.all_tags.call_count == api.quality_profile.call_count == 1

    def test_reuses_lists_the_caller_has(self):
        api = self.api()
        tags, profiles = api.all_tags(), api.quality_profile()
        read_media(api, "all_movies", tags, profiles)
        assert api.all_tags.call_count == api.quality_profile.call_count == 1


class TestEditMedia:
    @pytest.mark.parametrize(("log_name", "path", "key"), [("RADARR", "movie/editor", "movieIds"), ("SONARR", "series/editor", "seriesIds")])
    def test_puts_the_ids_and_changes(self, log_name, path, key):
        response = MagicMock(content=b"[]")
        response.json.return_value = [{"id": 1, "path": "/new/Heat"}]
        with patch.object(arr_client.TimeoutSession, "put", return_value=response) as put:
            assert edit_media("http://arr/", "k", log_name, [1, 2], {"moveFiles": True}) == [{"id": 1, "path": "/new/Heat"}]
        put.assert_called_once_with(f"http://arr/api/v3/{path}", json={key: [1, 2], "moveFiles": True}, headers={"X-Api-Key": "k"})

    def test_an_empty_answer(self):
        with patch.object(arr_client.TimeoutSession, "put", return_value=MagicMock(content=b"")):
            assert edit_media("http://arr", "k", "RADARR", [1], {}) == []

    def test_a_refusal_raises(self):
        response = MagicMock()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("400")
        with patch.object(arr_client.TimeoutSession, "put", return_value=response), pytest.raises(requests.exceptions.HTTPError):
            edit_media("http://arr", "k", "SONARR", [1], {})
