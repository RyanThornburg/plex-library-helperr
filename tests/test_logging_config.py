"""Tests for the log secret filter"""

import logging

import requests

from logging_config import SecretFilter, failure_text, hide_secrets, without_queries


def test_a_bad_format_string_is_logged_unformatted():
    # a filter that raised would raise in the code that logged
    hide_secrets("filter-test-secret")
    record = logging.LogRecord("lib", logging.INFO, "lib.py", 1, "%s and %s with filter-test-secret", ("one",), None)
    assert SecretFilter().filter(record)
    assert "one" in record.msg and "filter-test-secret" not in record.msg and record.args is None


class TestFailureText:
    """what a result may say about a failed request: no hosts, no queries, no secrets"""

    def test_a_url_is_cut_to_its_path(self):
        e = requests.exceptions.HTTPError("500 Server Error: x for url: http://sonarr:8989/api/v3/episodefile/bulk?apikey=abc")
        assert failure_text(e) == "500 Server Error: x for url: /api/v3/episodefile/bulk"

    def test_plexapi_style_error(self):
        e = Exception("(401) unauthorized; https://10.0.0.5:32400/library/sections/1/emptyTrash?X-Plex-Token=t <html>")
        assert failure_text(e) == "(401) unauthorized; /library/sections/1/emptyTrash <html>"

    def test_a_url_without_a_path(self):
        assert failure_text(Exception("can't reach http://plex:32400 now")) == "can't reach  now"

    def test_connection_and_timeout(self):
        assert failure_text(requests.exceptions.ConnectionError("HTTPConnectionPool(host='sonarr', port=8989)")) == "couldn't connect"
        assert failure_text(requests.exceptions.ConnectTimeout("x")) == "no answer in time"
        assert failure_text(requests.exceptions.ReadTimeout("x")) == "no answer in time"

    def test_secrets_are_redacted(self):
        hide_secrets("failure-text-secret")
        assert failure_text(ValueError("key failure-text-secret refused")) == "key *** refused"

    def test_no_message_names_the_error(self):
        assert failure_text(KeyError()) == "KeyError"


def test_without_queries_keeps_host_and_path():
    text = "URL Fetching failed; https://indexer.example/api?t=get&id=1&apikey=idx-key, retry"
    assert without_queries(text) == "URL Fetching failed; https://indexer.example/api, retry"
