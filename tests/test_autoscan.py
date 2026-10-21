"""Tests for autoscan.py"""

from unittest.mock import MagicMock, patch

import requests

from autoscan import trigger_autoscan
from config.models import AutoscanConfig


class TestTriggerAutoscan:
    def test_posts_all_dirs_to_manual_trigger(self):
        with patch("autoscan.requests.post") as post:
            assert trigger_autoscan(
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

    def test_http_error_logs_status_without_traceback(self, caplog):
        response = MagicMock()
        response.status_code = 401
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            response=response
        )
        with patch("autoscan.requests.post", return_value=response):
            assert not trigger_autoscan(AutoscanConfig(url="http://autoscan:3030"), {"/a"})
        assert (
            "Failed to trigger scan: HTTP 401 (http://autoscan:3030/triggers/manual)"
            " - check autoscan username/password"
            in caplog.text
        )
        assert "Traceback" not in caplog.text

    def test_connection_error_logs_short_message(self, caplog):
        with patch(
            "autoscan.requests.post",
            side_effect=requests.exceptions.ConnectionError("long urllib3 message"),
        ):
            trigger_autoscan(AutoscanConfig(url="http://localhost:3030"), {"/a"})
        assert (
            "Failed to trigger scan: could not connect (http://localhost:3030/triggers/manual)"
            in caplog.text
        )
        assert "long urllib3 message" not in caplog.text

    def test_timeout_logs_short_message(self, caplog):
        with patch(
            "autoscan.requests.post",
            side_effect=requests.exceptions.ConnectTimeout(),
        ):
            trigger_autoscan(AutoscanConfig(url="http://autoscan:3030"), {"/a"})
        assert "Failed to trigger scan: timed out" in caplog.text
