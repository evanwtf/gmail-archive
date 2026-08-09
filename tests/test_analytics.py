"""Sender classification and corpus analytics.

The classifier is SQL, so most of this needs a database and is marked
`integration`. The pure logic that can be tested without one is tested without
one.
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from gmail_archive.analytics import (
    _BULK_DOMAIN_RATIO,
    _HIGH_VOLUME_BULK,
    _HIGH_VOLUME_DESPITE_PERSONAL,
    _ROLE_PATTERN,
    _UNREPLYABLE_PATTERN,
    BULK_CATEGORIES,
    YearActivity,
)

DSN = os.environ.get("GMAIL_ARCHIVE_TEST_DATABASE_URL")


class TestYearActivity:
    def test_sent_share(self) -> None:
        y = YearActivity(
            year=2013,
            sent=1103,
            received=11561,
            human_received=0,
            bulk_received=0,
            people_mailed=0,
        )
        assert round(y.sent_share, 1) == 8.7

    def test_sent_share_of_an_empty_year_is_zero_not_an_error(self) -> None:
        y = YearActivity(
            year=1999,
            sent=0,
            received=0,
            human_received=0,
            bulk_received=0,
            people_mailed=0,
        )
        assert y.sent_share == 0.0


class TestThresholds:
    """The thresholds encode a deliberate asymmetry, so pin it."""

    def test_personal_labelled_senders_need_a_higher_bar(self) -> None:
        # Marking a real person "bulk" hides their mail; the reverse only
        # leaves noise in a list. So overruling Gmail's Personal label costs
        # more evidence than overruling nothing.
        assert _HIGH_VOLUME_DESPITE_PERSONAL > _HIGH_VOLUME_BULK

    def test_domain_inheritance_needs_a_strong_majority(self) -> None:
        # Low enough and a mixed domain like gmail.com gets swept up.
        assert _BULK_DOMAIN_RATIO >= 0.9

    def test_role_addresses_are_not_treated_as_unreplyable(self) -> None:
        # #44: these were one list, so a `hello@` you correspond with was
        # filed as marketing — 59 real correspondents on the reference
        # archive. Only a structurally unreplyable address may outrank the
        # fact that you have written to it.
        for addr in (
            "hello@studio.example",
            "info@lawyer.example",
            "team@startup.example",
            "support@shop.example",
            "billing@vendor.example",
            "contact@person.example",
        ):
            assert not re.search(_UNREPLYABLE_PATTERN, addr), addr
            assert re.search(_ROLE_PATTERN, addr), addr

    def test_addresses_that_cannot_receive_mail_are_recognised(self) -> None:
        for addr in (
            "no-reply@shop.example",
            "noreply@shop.example",
            "do-not-reply@bank.example",
            "mailer-daemon@host.example",
            "bounces+x@list.example",
            "notifications@social.example",
        ):
            assert re.search(_UNREPLYABLE_PATTERN, addr), addr

    def test_an_ordinary_address_matches_neither(self) -> None:
        for addr in ("alice@example.com", "j.smith@company.example"):
            assert not re.search(_UNREPLYABLE_PATTERN, addr), addr
            assert not re.search(_ROLE_PATTERN, addr), addr

    def test_bulk_categories_are_gmails_own(self) -> None:
        assert set(BULK_CATEGORIES) == {
            "Category Promotions",
            "Category Updates",
            "Category Social",
            "Category Forums",
        }


@pytest.mark.integration
@pytest.mark.skipif(not DSN, reason="needs GMAIL_ARCHIVE_TEST_DATABASE_URL")
class TestClassification:
    """End-to-end classification against a real database."""

    def _message(
        self,
        conn: object,
        *,
        key: str,
        sender: str,
        labels: tuple[str, ...] = (),
        to: tuple[str, ...] = (),
    ) -> str:
        sha = hashlib.sha256(key.encode()).hexdigest()
        conn.execute(  # type: ignore[attr-defined]
            "insert into blobs (sha256, size_bytes, kind)"
            " values (%s, 1, 'message') on conflict do nothing",
            (sha,),
        )
        conn.execute(  # type: ignore[attr-defined]
            "insert into messages (raw_sha256, size_bytes, from_addr, to_addrs,"
            " internal_date) values (%s, 1, %s, %s, now())"
            " on conflict do nothing",
            (sha, sender, list(to)),
        )
        for label in labels:
            conn.execute(  # type: ignore[attr-defined]
                "insert into labels (raw_sha256, label, account_id)"
                " values (%s, %s, (select min(id) from accounts))"
                " on conflict do nothing",
                (sha, label),
            )
        return sha

    def test_replied_to_senders_are_human(self) -> None:
        import psycopg

        from gmail_archive.analytics import correspondent, rebuild_sender_profiles

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            shas = [
                self._message(conn, key="anx-friend-in", sender="friend@test.invalid"),
                self._message(
                    conn,
                    key="anx-friend-out",
                    sender="me@test.invalid",
                    labels=("Sent",),
                    to=("friend@test.invalid",),
                ),
            ]
            rebuild_sender_profiles(conn)
            profile = correspondent(conn, "friend@test.invalid")
            assert profile is not None
            assert profile.kind == "human"
            assert "replied-to" in profile.evidence

            for sha in shas:
                conn.execute("delete from labels where raw_sha256 = %s", (sha,))
                conn.execute("delete from messages where raw_sha256 = %s", (sha,))
                conn.execute("delete from blobs where sha256 = %s", (sha,))
            conn.execute(
                "delete from sender_profiles where address like %s",
                ("%@test.invalid",),
            )

    def test_noreply_addresses_are_bulk_even_if_replied_to(self) -> None:
        import psycopg

        from gmail_archive.analytics import correspondent, rebuild_sender_profiles

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha = self._message(
                conn, key="anx-noreply", sender="no-reply@shop.test.invalid"
            )
            out = self._message(
                conn,
                key="anx-noreply-out",
                sender="me@test.invalid",
                labels=("Sent",),
                to=("no-reply@shop.test.invalid",),
            )
            rebuild_sender_profiles(conn)
            profile = correspondent(conn, "no-reply@shop.test.invalid")
            assert profile is not None
            # An address that cannot receive mail is not a correspondent, even
            # if something was once sent to it.
            assert profile.kind == "bulk"

            for s in (sha, out):
                conn.execute("delete from labels where raw_sha256 = %s", (s,))
                conn.execute("delete from messages where raw_sha256 = %s", (s,))
                conn.execute("delete from blobs where sha256 = %s", (s,))
            conn.execute(
                "delete from sender_profiles where address like %s",
                ("%test.invalid",),
            )

    def test_manual_override_survives_a_rebuild(self) -> None:
        import psycopg

        from gmail_archive.analytics import correspondent, rebuild_sender_profiles

        with psycopg.connect(DSN) as conn:  # type: ignore[arg-type]
            sha = self._message(
                conn, key="anx-override", sender="no-reply@keepme.test.invalid"
            )
            rebuild_sender_profiles(conn)
            conn.execute(
                "update sender_profiles set override = 'human', kind = 'human'"
                " where address = %s",
                ("no-reply@keepme.test.invalid",),
            )
            rebuild_sender_profiles(conn)

            profile = correspondent(conn, "no-reply@keepme.test.invalid")
            assert profile is not None
            # The whole point of the column: the classifier does not get to
            # overrule a human decision on every run.
            assert profile.kind == "human"

            conn.execute("delete from messages where raw_sha256 = %s", (sha,))
            conn.execute("delete from blobs where sha256 = %s", (sha,))
            conn.execute(
                "delete from sender_profiles where address like %s",
                ("%test.invalid",),
            )
