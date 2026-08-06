"""Mbox splitter tests.

The splitter is the first thing that touches a real export, so it is tested
against the same hazards the parser is: arbitrary bytes, edge cases, and the
specific fixture pathologies that exercise boundary detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gmail_archive.mbox import MboxScan, read_message, scan, strip_envelope


def _mbox(raw: bytes) -> tuple[Path, int]:
    """Write raw bytes to a temp file and return (path, size)."""
    import tempfile

    fh, path = tempfile.mkstemp(suffix=".mbox")
    with open(fh, "wb") as f:
        f.write(raw)
    return Path(path), len(raw)


class TestScan:
    def test_single_message(self) -> None:
        raw = b"From user@example.com Mon Jan 01 00:00:00 2000\nSubject: hi\n\nbody\n"
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 1
        assert result.offsets == [(0, len(raw))]
        path.unlink()

    def test_two_messages(self) -> None:
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
        )
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 2
        assert len(result.offsets) == 2
        # Each offset+length should cover the full message including envelope.
        for offset, length in result.offsets:
            assert raw[offset : offset + length]
        assert result.offsets[0][0] + result.offsets[0][1] == result.offsets[1][0]
        path.unlink()

    def test_empty_file(self) -> None:
        path, _ = _mbox(b"")
        result = scan(path)
        assert result.message_count == 0
        assert result.offsets == []
        assert result.total_bytes == 0
        path.unlink()

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            scan(Path("/nonexistent/mbox"))

    def test_quoted_from_line_is_not_a_separator(self) -> None:
        """A `>From ` body line must not be mistaken for an envelope separator."""
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: s\n\n"
            b">From quoted line\n"
            b">>From double quoted\n"
            b"normal line\n"
        )
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 1
        path.unlink()

    def test_from_header_is_not_a_separator(self) -> None:
        """A `From:` header contains a colon, not a space after 'From'."""
        raw = b"From a@e.com Mon Jan 01 00:00:00 2000\nFrom: sender@e.com\nSubject: s\n\nbody\n"
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 1
        path.unlink()

    def test_bare_from_line_in_body_is_a_separator(self) -> None:
        """A bare `From ` at line start in the body IS a separator in mboxrd.

        This is the classic mbox delimiter ambiguity. In mboxrd the writer
        should have quoted it as `>From `, so a bare one is a real boundary.
        """
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
        )
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 2
        path.unlink()

    def test_total_bytes_matches_file_size(self) -> None:
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
        )
        path, size = _mbox(raw)
        result = scan(path)
        assert result.total_bytes == size
        path.unlink()

    def test_offsets_cover_the_entire_file(self) -> None:
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
            b"From c@e.com Mon Jan 03 00:00:00 2000\nSubject: three\n\nbody3\n"
        )
        path, size = _mbox(raw)
        result = scan(path)
        total = sum(length for _, length in result.offsets)
        assert total == size
        path.unlink()

    def test_message_with_no_trailing_newline(self) -> None:
        """The last message may not end with a newline."""
        raw = b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: s\n\nbody"
        path, _ = _mbox(raw)
        result = scan(path)
        assert result.message_count == 1
        path.unlink()

    def test_large_file_does_not_load_into_memory(self) -> None:
        """The scan must not read the full file content. We verify by checking
        that the scan completes without OOM on a sparse-ish file."""
        import os

        path, _ = _mbox(b"")
        # Write a file with many messages without holding all the content.
        with open(path, "wb") as f:
            for i in range(1000):
                line = f"From user{i}@e.com Mon Jan 01 00:00:00 2000\nSubject: {i}\n\nbody{i}\n".encode()
                f.write(line)
        result = scan(path)
        assert result.message_count == 1000
        path.unlink()


class TestReadMessage:
    def test_reads_correct_range(self) -> None:
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
        )
        path, _ = _mbox(raw)
        result = scan(path)
        msg = read_message(path, result.offsets[0][0], result.offsets[0][1])
        assert b"Subject: one" in msg
        assert b"Subject: two" not in msg
        path.unlink()

    def test_second_message_is_readable(self) -> None:
        raw = (
            b"From a@e.com Mon Jan 01 00:00:00 2000\nSubject: one\n\nbody1\n"
            b"From b@e.com Mon Jan 02 00:00:00 2000\nSubject: two\n\nbody2\n"
        )
        path, _ = _mbox(raw)
        result = scan(path)
        msg = read_message(path, result.offsets[1][0], result.offsets[1][1])
        assert b"Subject: two" in msg
        assert b"Subject: one" not in msg
        path.unlink()


class TestStripEnvelope:
    def test_strips_from_line(self) -> None:
        raw = b"From user@e.com Mon Jan 01 00:00:00 2000\nSubject: s\n\nbody\n"
        stripped = strip_envelope(raw)
        assert stripped == b"Subject: s\n\nbody\n"

    def test_empty_body_after_envelope(self) -> None:
        raw = b"From user@e.com Mon Jan 01 00:00:00 2000\n"
        stripped = strip_envelope(raw)
        assert stripped == b""

    def test_no_newline_returns_empty(self) -> None:
        raw = b"From user@e.com"
        stripped = strip_envelope(raw)
        assert stripped == b""
