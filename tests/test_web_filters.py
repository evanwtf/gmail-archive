"""Tests for the web UI's Jinja filters.

Pure presentation logic with no database, so these run in the default suite.
Every case pins `now` explicitly — a relative-date function tested against the
real clock is a test that fails at midnight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gmail_archive.web.filters import relative_date

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


class TestRelativeDate:
    def test_none_renders_empty(self) -> None:
        # ~2.7% of the archive has no parseable Date; templates call this
        # unconditionally, so None must not raise or print "None".
        assert relative_date(None, NOW) == ""

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(seconds=0), "just now"),
            (timedelta(seconds=1), "1 second ago"),
            (timedelta(seconds=45), "45 seconds ago"),
            (timedelta(minutes=1), "1 minute ago"),
            (timedelta(minutes=59), "59 minutes ago"),
            (timedelta(hours=1), "1 hour ago"),
            (timedelta(hours=32), "32 hours ago"),
            (timedelta(hours=47), "47 hours ago"),
            (timedelta(hours=48), "2 days ago"),
            (timedelta(days=59), "59 days ago"),
            (timedelta(days=90), "2 months ago"),
            # The month band runs to 24 months, so a year-plus stays in months
            # while that is still the more precise answer.
            (timedelta(days=400), "13 months ago"),
            (timedelta(days=800), "2 years ago"),
            (timedelta(days=7300), "19 years ago"),
        ],
    )
    def test_ages(self, delta: timedelta, expected: str) -> None:
        assert relative_date(NOW - delta, NOW) == expected

    def test_hours_band_runs_to_48_not_24(self) -> None:
        # The point of the wide hour band: "32 hours ago" beats "1 day ago".
        assert relative_date(NOW - timedelta(hours=32), NOW) == "32 hours ago"

    def test_singular_and_plural(self) -> None:
        assert relative_date(NOW - timedelta(hours=1), NOW) == "1 hour ago"
        assert relative_date(NOW - timedelta(hours=2), NOW) == "2 hours ago"

    def test_future_dates_are_not_clamped(self) -> None:
        # The archive contains them: a Date header is whatever the sender
        # claimed, and the parser keeps implausible values rather than
        # discarding them.
        assert relative_date(NOW + timedelta(hours=2), NOW) == "in 2 hours"
        assert relative_date(NOW + timedelta(days=800), NOW) == "in 2 years"

    def test_never_renders_zero_units(self) -> None:
        # Flooring at a band's edge must not produce "0 minutes ago".
        for seconds in (59, 3599, 172_799, 5_183_999):
            out = relative_date(NOW - timedelta(seconds=seconds), NOW)
            assert not out.startswith("0 "), (seconds, out)

    def test_naive_datetime_is_read_as_utc(self) -> None:
        naive = datetime(2026, 8, 6, 10, 0, 0)
        assert relative_date(naive, NOW) == "2 hours ago"

    def test_naive_reference_is_read_as_utc(self) -> None:
        naive_now = datetime(2026, 8, 6, 12, 0, 0)
        assert relative_date(NOW - timedelta(hours=3), naive_now) == "3 hours ago"

    def test_defaults_to_the_current_clock(self) -> None:
        # No `now` argument: the only assertion that can be made safely is
        # that a recent timestamp reads as recent.
        assert relative_date(datetime.now(UTC) - timedelta(hours=5)) == "5 hours ago"

    def test_every_band_is_reachable(self) -> None:
        units = {
            relative_date(NOW - delta, NOW).split()[1].rstrip("s")
            for delta in (
                timedelta(seconds=5),
                timedelta(minutes=5),
                timedelta(hours=5),
                timedelta(days=5),
                timedelta(days=100),
                timedelta(days=1000),
            )
        }
        assert units == {"second", "minute", "hour", "day", "month", "year"}
