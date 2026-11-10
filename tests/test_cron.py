"""Tests for the cron parser schedules use"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from web import cron


def next_after(expression: str, after: str) -> datetime | None:
    return cron.parse(expression).next_after(datetime.fromisoformat(after))


class TestParse:
    @pytest.mark.parametrize(
        "expression, error",
        [
            ("* * * *", "needs 5 fields"),
            ("60 * * * *", "minute"),
            ("* 24 * * *", "hour"),
            ("* * 0 * *", "day"),
            ("* * * 13 *", "month"),
            ("* * * * 8", "weekday"),
            ("*/0 * * * *", "bad step"),
            ("5-1 * * * *", "outside"),
            ("a * * * *", "isn't a number"),
            ("1-x * * * *", "bad range"),
            ("0 0 30 2 *", "never runs"),
        ],
    )
    def test_bad_expressions_say_what_is_wrong(self, expression, error):
        with pytest.raises(ValueError, match=error):
            cron.parse(expression)

    def test_lists_ranges_and_steps(self):
        parsed = cron.parse("0,30 9-17/4 * * *")
        assert parsed.minutes == (0, 30)
        assert parsed.hours == (9, 13, 17)

    def test_start_with_step_runs_to_the_end(self):
        assert cron.parse("5/20 * * * *").minutes == (5, 25, 45)

    def test_seven_is_sunday(self):
        assert cron.parse("0 0 * * 7").weekdays == {0}

    def test_macros(self):
        assert cron.parse("@daily") == cron.parse("0 0 * * *")
        assert cron.parse(" @Hourly ") == cron.parse("0 * * * *")


class TestNextAfter:
    def test_hourly_at_minute(self):
        assert next_after("47 * * * *", "2026-10-22 13:10") == datetime(2026, 10, 22, 13, 47)
        assert next_after("47 * * * *", "2026-10-22 13:47") == datetime(2026, 10, 22, 14, 47)

    def test_seconds_round_up_to_the_next_minute(self):
        assert next_after("* * * * *", "2026-10-22 13:10:59") == datetime(2026, 10, 22, 13, 11)

    def test_rolls_over_day_month_and_year(self):
        assert next_after("0 4 * * *", "2026-12-31 05:00") == datetime(2027, 1, 1, 4, 0)
        assert next_after("0 0 1 * *", "2026-10-22 00:00") == datetime(2026, 11, 1, 0, 0)

    def test_weekday(self):
        # 2026-10-22 is a thursday
        assert next_after("30 3 * * 1", "2026-10-22 00:00") == datetime(2026, 10, 26, 3, 30)

    def test_day_or_weekday_when_both_restricted(self):
        # the 1st or any monday, like cron
        assert next_after("0 0 1 * 1", "2026-10-22 00:00") == datetime(2026, 10, 26, 0, 0)

    def test_day_and_weekday_when_one_is_a_step(self):
        # a field starting with * counts as unrestricted, so both must match
        assert next_after("0 0 */2 * 1", "2026-10-22 00:00") == datetime(2026, 11, 9, 0, 0)

    def test_leap_day(self):
        assert next_after("0 0 29 2 *", "2026-10-22 00:00") == datetime(2028, 2, 29, 0, 0)


NEW_YORK = ZoneInfo("America/New_York")


class TestTimeZone:
    def test_cron_matches_the_zones_wall_time(self):
        # 04:00 in new york (edt, utc-4) is 08:00 utc
        found = cron.parse("0 4 * * *").next_after_utc(datetime(2026, 10, 22, 0, 0), NEW_YORK)
        assert found == datetime(2026, 10, 22, 8, 0)

    def test_after_dst_ends(self):
        # est, utc-5
        found = cron.parse("0 4 * * *").next_after_utc(datetime(2026, 11, 2, 0, 0), NEW_YORK)
        assert found == datetime(2026, 11, 2, 9, 0)

    # 2026-11-01 in new york: 01:00-01:59 happens twice, edt (05:00-05:59 utc) then
    # est (06:00-06:59 utc)
    @pytest.mark.parametrize(
        "expression, after, expected",
        [
            # first pass runs as usual
            ("*/15 * * * *", datetime(2026, 11, 1, 5, 50), datetime(2026, 11, 1, 7, 0)),
            ("30 1 * * *", datetime(2026, 11, 1, 5, 0), datetime(2026, 11, 1, 5, 30)),
            # from the repeated hour, the next time is after it, never back in the first pass
            ("*/15 * * * *", datetime(2026, 11, 1, 6, 5), datetime(2026, 11, 1, 7, 0)),
            ("30 1 * * *", datetime(2026, 11, 1, 6, 10), datetime(2026, 11, 2, 6, 30)),
            ("30 1 * * *", datetime(2026, 11, 1, 5, 30), datetime(2026, 11, 2, 6, 30)),
        ],
    )
    def test_repeated_hour_runs_once(self, expression, after, expected):
        found = cron.parse(expression).next_after_utc(after, NEW_YORK)
        assert found == expected
        assert found > after

    def test_skipped_hour_runs_at_the_same_offset(self):
        # 2026-03-08 in new york: 02:00-02:59 doesn't exist, 02:30 runs at 03:30 edt
        found = cron.parse("30 2 * * *").next_after_utc(datetime(2026, 3, 8, 6, 0), NEW_YORK)
        assert found == datetime(2026, 3, 8, 7, 30)

    def test_after_is_converted_to_the_zone(self):
        # 03:00 utc is 23:00 the day before in new york, so 04:00 is still to come that day
        found = cron.parse("0 23 * * *").next_after_utc(datetime(2026, 10, 22, 2, 0), NEW_YORK)
        assert found == datetime(2026, 10, 22, 3, 0)

    def test_parse_zone(self):
        assert cron.parse_zone("America/New_York") == NEW_YORK
        for bad in ("Mars/Olympus", "", "../etc/passwd"):
            with pytest.raises(ValueError, match="unknown time zone"):
                cron.parse_zone(bad)


def test_a_zone_name_too_long_for_a_file_is_unknown():
    with pytest.raises(ValueError, match="unknown time zone"):
        cron.parse_zone("a" * 300)
