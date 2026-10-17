"""Tests for autoscan.py"""

from unittest.mock import MagicMock, patch

import requests

from autoscan import trigger_autoscan
from config.models import AutoscanConfig


class TestTriggerAutoscan:
    def test_posts_all_dirs_to_manual_trigger(self):
        with patch("autoscan.requests.post") as post:
            trigger_autoscan(
                AutoscanConfig(url="http://autoscan:3030/"), {"/b", "/a"}
            )
        post.assert_called_once_with(
            "http://autoscan:3030/triggers/manual",
            params=[("dir", "/a"), ("dir", "/b")],
            auth=None,
            timeout=30,
        )

    def test_uses_basic_auth_when_configured(self):
        with patch("autoscan.requests.post") as post:
            trigger_autoscan(
                AutoscanConfig(url="http://autoscan:3030", username="u", password="p"),
                {"/a"},
            )
        assert post.call_args.kwargs["auth"] == ("u", "p")

    def test_request_error_is_logged_not_raised(self, caplog):
        response = MagicMock()
        response.raise_for_status.side_effect = requests.exceptions.HTTPError("401")
        with patch("autoscan.requests.post", return_value=response):
            trigger_autoscan(AutoscanConfig(url="http://autoscan:3030"), {"/a"})
        assert "Failed to trigger scan" in caplog.text
