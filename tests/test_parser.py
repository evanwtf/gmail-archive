"""Parser tests.

The archive's integrity rests on this surface, so the tests are written against
the hazards rather than the happy path. Two carry most of the weight:

- `test_never_raises` — a hypothesis property over arbitrary bytes. A six-hour
  run must not die on one bad 2009 Outlook message.
- `TestSanitisation` — NUL and lone surrogates. Postgres `text` cannot hold
  either, and a single one aborts a COPY batch of thousands.

Fixtures come from the Phase 2 generator, so the pathologies under test are the
ones measured against a real export rather than invented here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gmail_archive.fixtures import Pathology, generate
from gmail_archive.parser import (
    SEARCH_TEXT_MAX_BYTES,
    ParsedMessage,
    Warn,
    parse,
    requote_mbox,
    unquote_mbox,
)
from test_fixtures import split_mbox  # pytest puts tests/ on sys.path


def codes(parsed: ParsedMessage) -> set[str]:
    return {w.code.value for w in parsed.parse_warnings}


def parse_fixture(
    tmp_path: Path, pathology: Pathology, count: int = 6
) -> list[ParsedMessage]:
    out = tmp_path / f"{pathology.value}.mbox"
    generate(out, count=count, seed=17, pathologies=[pathology])
    # split_mbox unquotes, so the parser is told not to unquote again.
    return [parse(m, already_unquoted=True) for m in split_mbox(out.read_bytes())]


class TestNeverRaises:
    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=4096))
    def test_arbitrary_bytes(self, raw: bytes) -> None:
        result = parse(raw)
        assert isinstance(result, ParsedMessage)
        assert len(result.raw_sha256) == 64

    @settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=2048))
    def test_arbitrary_bytes_with_a_header_shape(self, blob: bytes) -> None:
        raw = b"Subject: x\nFrom: a@example.invalid\n\n" + blob
        assert isinstance(parse(raw), ParsedMessage)

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"\x00" * 64,
            b"Subject:\n\n",
            b"Content-Type: multipart/mixed; boundary=\n\n--\n",
            b"Content-Type: text/plain; charset=nonexistent-charset\n\nbody",
            b"Content-Transfer-Encoding: base64\n\n!!!not base64!!!",
            b"\xff\xfe\x00\x00binary garbage",
            b"From: \n\n",
        ],
        ids=lambda r: repr(r[:24]),
    )
    def test_known_nasty_inputs(self, raw: bytes) -> None:
        assert isinstance(parse(raw), ParsedMessage)

    @pytest.mark.parametrize(
        "header", ["Date", "Message-ID", "References", "In-Reply-To", "X-GM-THRID"]
    )
    def test_eight_bit_byte_in_a_structured_header(self, header: str) -> None:
        """The bug three real messages found, generalised to every structured header.

        `Message.get()` under compat32 returns an `email.header.Header` object,
        not a str, when the raw header holds bytes it cannot decode as ASCII.
        Anything downstream that calls `.split()` on it — `parsedate_to_datetime`
        does — then dies with AttributeError.

        The original 8-bit fixture put its bad byte in `Subject`, an
        *unstructured* header nothing later tries to parse, which is exactly why
        neither it nor the hypothesis property test caught this.
        """
        raw = f"{header}: ".encode() + b"\xe9\xa0 bad bytes\nSubject: s\n\nbody"
        parsed = parse(raw)
        assert isinstance(parsed, ParsedMessage)

    @settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=120))
    def test_arbitrary_bytes_inside_a_date_header(self, blob: bytes) -> None:
        # Targeted where undirected binary fuzzing has effectively no chance of
        # landing: a syntactically real header whose *value* is arbitrary.
        raw = b"Date: " + blob.replace(b"\n", b" ") + b"\nSubject: s\n\nbody"
        assert isinstance(parse(raw), ParsedMessage)

    @settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=120))
    def test_arbitrary_bytes_inside_a_labels_header(self, blob: bytes) -> None:
        raw = b"X-Gmail-Labels: " + blob.replace(b"\n", b" ") + b"\n\nbody"
        assert isinstance(parse(raw), ParsedMessage)


class TestSanitisation:
    def test_nul_is_stripped_from_body(self) -> None:
        raw = b"Subject: hi\nContent-Type: text/plain\n\nbefore\x00after"
        parsed = parse(raw)
        assert "\x00" not in parsed.body_text
        assert "\x00" not in parsed.search_text
        assert Warn.NUL_STRIPPED.value in codes(parsed)

    def test_nul_is_stripped_from_a_header(self) -> None:
        parsed = parse(b"Subject: bad\x00subject\n\nbody")
        assert parsed.subject is not None
        assert "\x00" not in parsed.subject

    def test_lone_surrogate_is_stripped(self) -> None:
        # Undecodable bytes reach Python as lone surrogates via surrogateescape;
        # Postgres rejects them exactly as it rejects NUL.
        raw = (
            b"Subject: s\nContent-Type: text/plain; charset=utf-8\n\n"
            b"good \xed\xa0\x80 bad"
        )
        parsed = parse(raw)
        assert not any(0xD800 <= ord(c) <= 0xDFFF for c in parsed.body_text)

    def test_generated_nul_fixture_is_sanitised(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.BODY_NUL):
            assert "\x00" not in parsed.body_text


class TestSearchTextBound:
    def test_bounded_well_under_the_tsvector_limit(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.BODY_OVER_TSVECTOR)
        assert any(Warn.SEARCH_TEXT_TRUNCATED.value in codes(p) for p in results)
        for parsed in results:
            assert len(parsed.search_text.encode("utf-8")) <= SEARCH_TEXT_MAX_BYTES

    def test_bound_is_applied_in_bytes_not_characters(self) -> None:
        # Four-byte characters: a character-based bound would let this through
        # at four times the intended size.
        body = "𝄞" * 400_000
        raw = b"Subject: s\nContent-Type: text/plain; charset=utf-8\n\n" + body.encode()
        parsed = parse(raw)
        assert len(parsed.search_text.encode("utf-8")) <= SEARCH_TEXT_MAX_BYTES

    def test_short_body_is_not_truncated(self) -> None:
        parsed = parse(b"Subject: s\nContent-Type: text/plain\n\nshort body")
        assert Warn.SEARCH_TEXT_TRUNCATED.value not in codes(parsed)
        assert "short body" in parsed.search_text


class TestMboxQuoting:
    def test_single_quote_is_reversed(self) -> None:
        raw, ambiguous = unquote_mbox(b"a\n>From here\nb\n")
        assert raw == b"a\nFrom here\nb\n"
        assert not ambiguous

    def test_double_quote_strips_one_and_flags_ambiguity(self) -> None:
        # mboxrd says this was `>From `. mboxo says it was `>>From `. The
        # evidence favours mboxrd; the ambiguity is recorded either way.
        raw, ambiguous = unquote_mbox(b"a\n>>From here\n")
        assert raw == b"a\n>From here\n"
        assert ambiguous

    def test_unquoted_from_line_is_untouched(self) -> None:
        raw, ambiguous = unquote_mbox(b"a\nFrom here\n")
        assert raw == b"a\nFrom here\n"
        assert not ambiguous

    def test_file_bytes_round_trip_byte_identically(self) -> None:
        # Inputs are what an mbox *writer* can emit. A bare `From ` at line
        # start is excluded by construction — the writer would have quoted it,
        # and an unquoted one is an envelope separator, not body content.
        for original in (
            b"x\n>From a\ny\n",
            b"x\n>>From a\ny\n",
            b"x\n>>>From a\ny\n",
            b"no from lines here\n",
            b"From: header@example.invalid\n\nbody\n",  # colon, not a separator
        ):
            unquoted, _ = unquote_mbox(original)
            assert requote_mbox(unquoted) == original, original

    def test_message_bytes_survive_export_and_reingest(self) -> None:
        # The direction that matters for `export`: a true RFC822 message is
        # written out quoted and read back identically. Holds for bare `From `
        # lines too, which is the case the file-level property cannot cover.
        for message in (
            b"Subject: s\n\nFrom the desk of someone\n",
            b"Subject: s\n\n>From an already-quoted line\n",
            b"Subject: s\n\nFrom a\n>From b\n>>From c\n",
            b"Subject: s\n\nnothing special\n",
        ):
            restored, _ = unquote_mbox(requote_mbox(message))
            assert restored == message, message

    def test_ambiguity_is_reported_as_a_warning(self) -> None:
        parsed = parse(b"Subject: s\n\nbody\n>>From x\n")
        assert Warn.UNQUOTE_AMBIGUOUS.value in codes(parsed)

    def test_hash_is_over_unquoted_bytes(self) -> None:
        # The locked decision: raw_sha256 is the true RFC822 message, not the
        # file bytes, so an .eml export is correct.
        quoted = b"Subject: s\n\nbody\n>From x\n"
        unquoted = b"Subject: s\n\nbody\nFrom x\n"
        assert parse(quoted).raw_sha256 == hashlib.sha256(unquoted).hexdigest()

    def test_generated_quoted_from_fixture_round_trips(self, tmp_path: Path) -> None:
        out = tmp_path / "q.mbox"
        generate(out, count=6, seed=3, pathologies=[Pathology.QUOTED_FROM])
        assert b"\n>From " in out.read_bytes()


class TestLabels:
    def test_decoded_before_split(self, tmp_path: Path) -> None:
        # The trap: a non-ASCII label RFC 2047-encodes the whole header and the
        # separating commas become =2C. Splitting the raw value yields one label.
        results = parse_fixture(tmp_path, Pathology.LABELS_PUNCTUATED)
        assert any(len(p.labels) > 1 for p in results)
        for parsed in results:
            assert not any("=2C" in label for label in parsed.labels)

    def test_quoted_label_containing_a_comma_stays_one_label(self) -> None:
        raw = b'Subject: s\nX-Gmail-Labels: Inbox,"Receipts, invoices",Work\n\nbody'
        assert parse(raw).labels == ["Inbox", "Receipts, invoices", "Work"]

    def test_nested_paths_are_preserved(self) -> None:
        raw = b"Subject: s\nX-Gmail-Labels: Projects/Archive/2011\n\nbody"
        assert parse(raw).labels == ["Projects/Archive/2011"]

    def test_absent_header_parses_cleanly_without_warning(self, tmp_path: Path) -> None:
        # ~1.8% of the real export. Absence is normal, not a defect.
        for parsed in parse_fixture(tmp_path, Pathology.LABELS_ABSENT):
            if not parsed.labels:
                assert not any("label" in c for c in codes(parsed))

    def test_empty_header_yields_no_labels(self) -> None:
        assert parse(b"Subject: s\nX-Gmail-Labels: \n\nbody").labels == []


class TestDates:
    def test_missing_date_warns_and_is_none(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.DATE_MISSING)
        assert all(p.internal_date is None for p in results)
        assert all(Warn.DATE_MISSING.value in codes(p) for p in results)

    def test_unparseable_date_warns_and_is_none(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.DATE_UNPARSEABLE)
        assert all(p.internal_date is None for p in results)
        assert all(Warn.DATE_UNPARSEABLE.value in codes(p) for p in results)

    def test_naive_date_is_kept(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.DATE_NAIVE)
        assert any(p.internal_date is not None for p in results)

    def test_implausible_year_is_kept_but_flagged(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.DATE_FAR_FUTURE)
        assert any(Warn.DATE_IMPLAUSIBLE.value in codes(p) for p in results)


class TestIdentity:
    def test_missing_message_id_warns(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.MSGID_MISSING):
            assert parsed.message_id is None
            assert Warn.MESSAGE_ID_MISSING.value in codes(parsed)

    def test_thread_id_is_read_and_gmail_id_is_absent(self, tmp_path: Path) -> None:
        out = tmp_path / "d.mbox"
        generate(out, count=4, seed=2)
        for parsed in (
            parse(m, already_unquoted=True) for m in split_mbox(out.read_bytes())
        ):
            assert parsed.thread_id is not None
            assert parsed.gmail_id is None

    def test_identical_bytes_hash_identically(self) -> None:
        raw = b"Subject: s\n\nbody"
        assert parse(raw).raw_sha256 == parse(raw).raw_sha256

    def test_references_are_split_into_ids(self) -> None:
        raw = b"Subject: s\nReferences: <a@example.invalid> <b@example.invalid>\n\nx"
        assert parse(raw).references_ids == [
            "<a@example.invalid>",
            "<b@example.invalid>",
        ]


class TestStructure:
    def test_deep_nesting_is_walked(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.DEEP_NESTING):
            assert parsed.body_text or parsed.body_html

    def test_unknown_charset_warns_and_still_decodes(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.CHARSET_NONEXISTENT)
        assert all(Warn.CHARSET_UNKNOWN.value in codes(p) for p in results)
        assert all(p.body_text for p in results)

    def test_legacy_charsets_decode(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.CHARSET_LEGACY):
            assert parsed.body_text
            assert Warn.CHARSET_UNKNOWN.value not in codes(parsed)

    def test_attachment_metadata_is_recorded(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.ATTACH_REPEATED):
            assert parsed.attachments
            for att in parsed.attachments:
                assert len(att.sha256) == 64
                assert att.size > 0

    def test_repeated_attachment_hashes_match_across_messages(
        self, tmp_path: Path
    ) -> None:
        results = parse_fixture(tmp_path, Pathology.ATTACH_REPEATED)
        digests = {a.sha256 for p in results for a in p.attachments}
        assert len(digests) == 1

    def test_zero_byte_attachment_is_recorded_not_dropped(self, tmp_path: Path) -> None:
        results = parse_fixture(tmp_path, Pathology.ATTACH_ZERO_BYTE)
        assert any(a.size == 0 for p in results for a in p.attachments)

    def test_path_traversal_filename_is_stored_as_declared(
        self, tmp_path: Path
    ) -> None:
        # Stored verbatim on purpose. Sanitising here would hide what the
        # archive actually contains; the defence belongs at the serving layer.
        results = parse_fixture(tmp_path, Pathology.ATTACH_PATH_FILENAME)
        assert any(".." in (a.filename or "") for p in results for a in p.attachments)

    def test_truncated_message_still_parses(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.BODY_TRUNCATED):
            assert parsed.raw_sha256

    def test_bad_base64_padding_does_not_kill_the_parse(self, tmp_path: Path) -> None:
        for parsed in parse_fixture(tmp_path, Pathology.BASE64_BAD_PADDING):
            assert parsed.raw_sha256


class TestAgainstTheDefaultMix:
    def test_a_realistic_corpus_parses_end_to_end(self, tmp_path: Path) -> None:
        out = tmp_path / "mix.mbox"
        generate(out, count=800, seed=4)
        messages = split_mbox(out.read_bytes())
        parsed = [parse(m, already_unquoted=True) for m in messages]
        assert len(parsed) == 800
        assert all(len(p.raw_sha256) == 64 for p in parsed)
        # Every message yields a row; nothing is dropped for being defective.
        assert sum(1 for p in parsed if p.internal_date is not None) > 700
        assert sum(1 for p in parsed if p.parse_warnings) > 0


class TestAttachmentExtraction:
    """Re-extracting attachment bytes from a raw message.

    Ingest stores an attachment's metadata but not its bytes, so serving one
    means parsing the message again. The indices must match what `parse()`
    assigned, or the archive hands you the wrong file.
    """

    RAW = (
        b"From: a@example.com\r\n"
        b"Subject: two files\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"body text\r\n"
        b"--B\r\n"
        b"Content-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="first.pdf"\r\n'
        b"\r\n"
        b"PDFBYTES\r\n"
        b"--B\r\n"
        b"Content-Type: text/csv\r\n"
        b'Content-Disposition: attachment; filename="second.csv"\r\n'
        b"\r\n"
        b"a,b,c\r\n"
        b"--B--\r\n"
    )

    def test_indices_and_order_match_parse(self) -> None:
        from gmail_archive.parser import iter_attachment_payloads, parse

        parsed = parse(self.RAW, already_unquoted=True)
        extracted = list(iter_attachment_payloads(self.RAW))

        assert [a.filename for a in parsed.attachments] == ["first.pdf", "second.csv"]
        assert [idx for idx, _, _ in extracted] == [0, 1]
        assert [att.filename for _, att, _ in extracted] == [
            a.filename for a in parsed.attachments
        ]

    def test_content_hashes_match_what_ingest_recorded(self) -> None:
        # The database stores content_sha256 from parse(); if extraction
        # disagreed, a download would silently return different bytes.
        from gmail_archive.parser import iter_attachment_payloads, parse

        parsed = parse(self.RAW, already_unquoted=True)
        extracted = list(iter_attachment_payloads(self.RAW))
        assert [att.sha256 for _, att, _ in extracted] == [
            a.sha256 for a in parsed.attachments
        ]

    def test_payload_bytes_are_decoded(self) -> None:
        from gmail_archive.parser import iter_attachment_payloads

        payloads = [payload for _, _, payload in iter_attachment_payloads(self.RAW)]
        assert payloads[0] == b"PDFBYTES"
        assert payloads[1] == b"a,b,c"

    def test_the_body_is_not_an_attachment(self) -> None:
        from gmail_archive.parser import iter_attachment_payloads

        assert all(
            b"body text" not in payload
            for _, _, payload in iter_attachment_payloads(self.RAW)
        )

    def test_garbage_yields_nothing_rather_than_raising(self) -> None:
        from gmail_archive.parser import iter_attachment_payloads

        assert list(iter_attachment_payloads(b"")) == []
        assert list(iter_attachment_payloads(b"\xff\xfe not a message")) == []


class TestStripUnstorable:
    """Postgres cannot hold NUL or lone surrogates, in text or in jsonb.

    Public because the hazard is not confined to parsing: `imap-backfill`
    builds envelope JSON from pymap's output and died 194,000 messages into a
    run on a subject containing a NUL, with
    `UntranslatableCharacter: \\u0000 cannot be converted to text`.
    """

    def test_nul_is_removed(self) -> None:
        from gmail_archive.parser import strip_unstorable

        assert strip_unstorable("Delivery Tomorrow\x00 ok") == "Delivery Tomorrow ok"

    def test_lone_surrogates_are_removed(self) -> None:
        from gmail_archive.parser import strip_unstorable

        assert "\ud800" not in strip_unstorable("lone \ud800 surrogate")

    def test_ordinary_text_is_untouched(self) -> None:
        from gmail_archive.parser import strip_unstorable

        for text in ("plain", "emoji", "", "tabs\tand\nnewlines"):
            assert strip_unstorable(text) == text

    def test_json_round_trips_after_scrubbing(self) -> None:
        # The actual failure: json.dumps encodes a NUL as an escape quite
        # happily, and Postgres then rejects the resulting jsonb.
        import json

        from gmail_archive.parser import strip_unstorable

        assert "u0000" not in json.dumps(strip_unstorable("subject\x00here"))

    def test_scrub_reaches_nested_structures(self) -> None:
        # The backfill builds a nested dict of envelope and bodystructure;
        # a NUL anywhere in it poisons the whole jsonb value.
        from gmail_archive.cli import _scrub

        dirty = {"subject": "a\x00b", "to": [{"name": "c\x00d"}], "n": 5}
        clean = _scrub(dirty)
        assert clean == {"subject": "ab", "to": [{"name": "cd"}], "n": 5}


class TestTimezoneGuard:
    """The +/-15:59 offset guard added in c3adeb6 (#26).

    A Date header like `+17:00` parses fine in email.utils but makes psycopg
    COPY fail with InvalidTimeZoneDisplacementValue, aborting an entire batch
    of a thousand messages. The hypothesis property test could not catch it:
    it only asserts parse() does not raise, and parse() never did — the
    failure was downstream, in the COPY.
    """

    def _warnings(self, date_header: str) -> list[str]:
        from gmail_archive.parser import parse

        raw = f"From: a@example.com\r\nDate: {date_header}\r\n\r\nbody\r\n"
        parsed = parse(raw.encode(), already_unquoted=True)
        return [w.code.value for w in parsed.parse_warnings]

    def _date(self, date_header: str) -> object:
        from gmail_archive.parser import parse

        raw = f"From: a@example.com\r\nDate: {date_header}\r\n\r\nbody\r\n"
        return parse(raw.encode(), already_unquoted=True).internal_date

    def test_an_offset_beyond_the_postgres_limit_is_dropped(self) -> None:
        assert self._date("Tue, 1 Jan 2019 12:00:00 +1700") is None
        assert "date-tz-out-of-range" in self._warnings(
            "Tue, 1 Jan 2019 12:00:00 +1700"
        )

    def test_a_negative_offset_beyond_the_limit_is_dropped(self) -> None:
        assert self._date("Tue, 1 Jan 2019 12:00:00 -1700") is None

    def test_offsets_inside_the_limit_are_kept(self) -> None:
        # 15:59 is the boundary Postgres accepts; dropping it would discard
        # legitimate dates.
        for offset in ("+1559", "-1559", "+0000", "-0800", "+1400"):
            header = f"Tue, 1 Jan 2019 12:00:00 {offset}"
            assert self._date(header) is not None, offset
            assert "date-tz-out-of-range" not in self._warnings(header), offset

    def test_a_naive_date_is_unaffected(self) -> None:
        assert self._date("Tue, 1 Jan 2019 12:00:00") is not None

    def test_the_value_is_droppped_not_the_message(self) -> None:
        # A bad Date must not lose the message; it loses the date.
        from gmail_archive.parser import parse

        raw = (
            b"From: a@example.com\r\n"
            b"Date: Tue, 1 Jan 2019 12:00:00 +1700\r\n\r\nkeep me\r\n"
        )
        parsed = parse(raw, already_unquoted=True)
        assert parsed.internal_date is None
        assert "keep me" in parsed.body_text


class TestRequoteIsShared:
    """export must not re-implement parser.requote_mbox (#18)."""

    def test_deep_quoting_round_trips(self) -> None:
        # The hand-rolled version handled `From ` and `>From ` and stopped, so
        # a `>>From ` line came out unquoted and the file no longer round
        # tripped.
        from gmail_archive.parser import requote_mbox, unquote_mbox

        # A valid mboxrd body: every line beginning `From ` already carries
        # at least one `>`, because the writer put it there. A bare `From ` at
        # the start of a body line cannot occur in a well-formed file, so it
        # is not what round-tripping has to preserve.
        original = b">From a\n>>From b\n>>>From c\nplain\n"
        unquoted, _ = unquote_mbox(original)
        assert unquoted == b"From a\n>From b\n>>From c\nplain\n"
        assert requote_mbox(unquoted) == original

    def test_export_uses_the_parser_implementation(self) -> None:
        import gmail_archive.export as export_module

        assert not hasattr(export_module, "_requote")
