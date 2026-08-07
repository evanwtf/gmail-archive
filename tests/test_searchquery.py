"""Gmail-style search operator parsing.

No database: this is a pure parser, and everything it produces is a value that
`query.search()` turns into a parameterised condition.
"""

from __future__ import annotations

from datetime import date

import pytest

from gmail_archive.searchquery import ParsedQuery, parse


class TestFreeText:
    def test_plain_words_are_text(self) -> None:
        assert parse("invoice receipt").text == "invoice receipt"

    def test_empty(self) -> None:
        assert parse("").is_empty
        assert parse("   ").is_empty
        assert parse("") == ParsedQuery()

    def test_phrases_and_exclusions_survive_untouched(self) -> None:
        # These are websearch_to_tsquery's own syntax and must reach it intact.
        assert parse('"exact phrase" -spam').text == '"exact phrase" -spam'

    def test_whitespace_left_by_operators_is_collapsed(self) -> None:
        assert parse("from:alice   invoice   to:bob").text == "invoice"


class TestOperators:
    def test_from_and_to(self) -> None:
        parsed = parse("from:amazon to:evan@example.com")
        assert parsed.from_addrs == ("amazon",)
        assert parsed.to_addrs == ("evan@example.com",)
        assert parsed.text == ""

    def test_quoted_values_allow_spaces(self) -> None:
        assert parse('from:"john smith"').from_addrs == ("john smith",)
        assert parse('label:"Bank Alerts"').labels == ("Bank Alerts",)

    def test_dates(self) -> None:
        parsed = parse("before:2026-01-01 after:2020-06-01 on:2026-07-30")
        assert parsed.before == date(2026, 1, 1)
        assert parsed.after == date(2020, 6, 1)
        assert parsed.on == date(2026, 7, 30)

    def test_subject_and_label(self) -> None:
        parsed = parse("subject:invoice label:Starred")
        assert parsed.subjects == ("invoice",)
        assert parsed.labels == ("Starred",)

    def test_has_attachment(self) -> None:
        assert parse("has:attachment").has_attachment is True
        assert parse("has:attachments").has_attachment is True
        assert parse("nothing here").has_attachment is False

    @pytest.mark.parametrize(
        ("token", "expected_label"),
        [
            ("is:unread", "Unread"),
            ("is:read", "Opened"),
            ("is:starred", "Starred"),
            ("is:important", "Important"),
            ("is:sent", "Sent"),
            ("is:draft", "Drafts"),
            ("is:spam", "Spam"),
            ("is:chat", "Chat"),
        ],
    )
    def test_is_maps_to_gmail_labels(self, token: str, expected_label: str) -> None:
        assert parse(token).labels == (expected_label,)

    def test_operators_are_case_insensitive(self) -> None:
        assert parse("FROM:amazon IS:Unread").from_addrs == ("amazon",)
        assert parse("FROM:amazon IS:Unread").labels == ("Unread",)

    def test_repeated_operators_accumulate(self) -> None:
        assert parse("label:Amazon label:Starred").labels == ("Amazon", "Starred")

    def test_operators_combine_with_text(self) -> None:
        parsed = parse("from:amazon has:attachment after:2025-01-01 refund")
        assert parsed.from_addrs == ("amazon",)
        assert parsed.has_attachment is True
        assert parsed.after == date(2025, 1, 1)
        assert parsed.text == "refund"


class TestRejectedAndUnknown:
    def test_bad_date_is_rejected_not_guessed(self) -> None:
        parsed = parse("before:tuesday")
        assert parsed.before is None
        assert parsed.rejected == ("before:tuesday",)

    def test_unknown_is_value_is_rejected(self) -> None:
        parsed = parse("is:banana")
        assert parsed.labels == ()
        assert parsed.rejected == ("is:banana",)

    def test_unknown_operator_stays_in_the_text(self) -> None:
        # `cc:` is not supported. Dropping it would silently lose the term;
        # far more likely it is a URL, a time, or a Message-ID anyway.
        assert parse("cc:bob invoice").text == "cc:bob invoice"

    def test_a_url_is_not_mistaken_for_an_operator(self) -> None:
        assert parse("https://example.com/x").text == "https://example.com/x"

    def test_a_time_is_not_mistaken_for_an_operator(self) -> None:
        assert parse("meeting at 14:30").text == "meeting at 14:30"


class TestQueryShape:
    def test_operators_alone_are_a_valid_search(self) -> None:
        # The whole point: "mail from this person" needs no free text, and
        # before these operators it could not be expressed at all.
        parsed = parse("from:amazon")
        assert not parsed.is_empty
        assert parsed.has_filters
        assert parsed.text == ""

    def test_text_alone_is_a_valid_search(self) -> None:
        parsed = parse("invoice")
        assert not parsed.is_empty
        assert not parsed.has_filters

    def test_a_rejected_operator_alone_is_an_empty_search(self) -> None:
        parsed = parse("before:tuesday")
        assert parsed.is_empty
        assert parsed.rejected
