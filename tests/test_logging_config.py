"""Tests for the log secret filter"""

import logging

from logging_config import SecretFilter, hide_secrets


def test_a_bad_format_string_is_logged_unformatted():
    # a filter that raised would raise in the code that logged
    hide_secrets("filter-test-secret")
    record = logging.LogRecord("lib", logging.INFO, "lib.py", 1, "%s and %s with filter-test-secret", ("one",), None)
    assert SecretFilter().filter(record)
    assert "one" in record.msg and "filter-test-secret" not in record.msg and record.args is None
