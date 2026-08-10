"""Tests for the web UI's Jinja filters.

Pure presentation logic with no database, so these run in the default suite.
Every case pins `now` explicitly — a relative-date function tested against the
real clock is a test that fails at midnight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gmail_archive.web.filters import gmail_date, relative_date, sender_name

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


class TestGmailDate:
    """Gmail shows a time today, a month/day this year, a numeric date before."""

    @pytest.mark.parametrize(
        ("when", "expected"),
        [
            (datetime(2026, 8, 6, 11, 42, tzinfo=UTC), "11:42 AM"),
            (datetime(2026, 8, 6, 0, 5, tzinfo=UTC), "12:05 AM"),
            (datetime(2026, 8, 6, 13, 7, tzinfo=UTC), "1:07 PM"),
            (datetime(2026, 3, 4, 9, 0, tzinfo=UTC), "Mar 4"),
            (datetime(2026, 1, 1, 9, 0, tzinfo=UTC), "Jan 1"),
            (datetime(2009, 3, 4, 9, 0, tzinfo=UTC), "3/4/09"),
            (datetime(2025, 12, 31, 23, 59, tzinfo=UTC), "12/31/25"),
        ],
    )
    def test_formats(self, when: datetime, expected: str) -> None:
        assert gmail_date(when, NOW) == expected

    def test_none_renders_empty(self) -> None:
        assert gmail_date(None, NOW) == ""

    def test_naive_is_read_as_utc(self) -> None:
        assert gmail_date(datetime(2026, 8, 6, 11, 42), NOW) == "11:42 AM"

    def test_no_leading_zero_on_the_hour(self) -> None:
        # Gmail writes "9:05 AM", not "09:05 AM".
        assert gmail_date(datetime(2026, 8, 6, 9, 5, tzinfo=UTC), NOW) == "9:05 AM"


class TestSenderName:
    def test_derives_a_readable_name_from_the_local_part(self) -> None:
        assert sender_name("order-update@amazon.com") == "Order Update"
        assert sender_name("no_reply@example.com") == "No Reply"
        assert sender_name("first.last@example.com") == "First Last"

    def test_missing_sender(self) -> None:
        assert sender_name(None) == "(unknown sender)"
        assert sender_name("") == "(unknown sender)"

    def test_garbage_is_returned_rather_than_dropped(self) -> None:
        # Not every From header in twenty years of mail is a valid address.
        assert sender_name("not-an-address") == "Not An Address"
        assert sender_name("@example.com") == "@example.com"


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


class TestFilesize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0 B"),
            (1, "1 B"),
            (999, "999 B"),
            (1000, "1.0 kB"),
            (47_000, "47.0 kB"),
            (999_999, "1000 kB"),
            (1_400_000_000, "1.4 GB"),
            (412_000_000, "412 MB"),
            (2_500_000_000_000, "2.5 TB"),
        ],
    )
    def test_formats(self, value: int, expected: str) -> None:
        from gmail_archive.web.filters import filesize

        assert filesize(value) == expected

    def test_none_is_a_dash_not_zero(self) -> None:
        # "0 B" would be a claim; "—" is an absence.
        from gmail_archive.web.filters import filesize

        assert filesize(None) == "—"

    def test_sub_kilobyte_keeps_exact_bytes(self) -> None:
        from gmail_archive.web.filters import filesize

        assert filesize(512) == "512 B"

    def test_large_values_drop_the_decimal(self) -> None:
        # "412 MB" reads better than "412.3 MB" and is no less useful.
        from gmail_archive.web.filters import filesize

        assert "." not in filesize(412_345_678)


class TestDuration:
    """Import wall-clock, shown on /imports."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0s"),
            (18, "18s"),
            (59.9, "59s"),
            (60, "1m 00s"),
            (2585, "43m 05s"),
            (3600, "1h 00m"),
            (7860, "2h 11m"),
        ],
    )
    def test_spans_read_at_a_glance(self, seconds: float, expected: str) -> None:
        from gmail_archive.web.filters import duration

        assert duration(seconds) == expected

    def test_an_unfinished_run_is_a_dash(self) -> None:
        # Matches `filesize`: "0s" would claim the run took no time.
        from gmail_archive.web.filters import duration

        assert duration(None) == "—"
