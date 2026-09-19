"""The pre-commit guard is the only thing protecting a public history.

There is no CI on this repository, so this hook has no backstop. Test it like
it matters, including the case it exists for: an mbox that has been renamed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reject_mail_data import MAX_BYTES, main

MBOX_SAMPLE = (
    b"From fixture@example.invalid Mon Jan  5 09:12:33 2009\n"
    b"From: Fixture Sender <fixture@example.invalid>\n"
    b"Subject: hello\n\n"
    b"body\n"
)


def _run(path: Path) -> int:
    return main([str(path)])


class TestRejects:
    def test_mbox_extension(self, tmp_path: Path) -> None:
        target = tmp_path / "archive.mbox"
        target.write_bytes(b"anything")
        assert _run(target) == 1

    def test_renamed_mbox_by_content(self, tmp_path: Path) -> None:
        # The whole point: the first thing anyone does with a Takeout export is
        # rename it to something that looks harmless.
        target = tmp_path / "notes.txt"
        target.write_bytes(MBOX_SAMPLE)
        assert _run(target) == 1

    def test_file_under_a_data_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "blobs" / "ab" / "cd" / "deadbeef"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert _run(target) == 1

    def test_oversized_file(self, tmp_path: Path) -> None:
        target = tmp_path / "big.bin"
        target.write_bytes(b"\0" * (MAX_BYTES + 1))
        assert _run(target) == 1

    def test_archive_by_extension(self, tmp_path: Path) -> None:
        target = tmp_path / "takeout-20260805T095328Z-1-001.tgz"
        target.write_bytes(b"not really gzip")
        assert _run(target) == 1

    def test_small_archive_by_magic_bytes_under_a_harmless_name(
        self, tmp_path: Path
    ) -> None:
        # The gap this closes. The real export came as two tarballs; the 56 KB
        # one was under the size limit, had no blocked extension, and gzip magic
        # is not a From_ line — so every existing check waved it through.
        target = tmp_path / "notes.bin"
        target.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 64)
        assert _run(target) == 1

    def test_tar_magic_at_offset_257(self, tmp_path: Path) -> None:
        # Uncompressed tar hides its magic past the first 256 bytes, which is
        # exactly how much the guard used to read.
        target = tmp_path / "bundle.bin"
        target.write_bytes(b"\x00" * 257 + b"ustar\x0000" + b"\x00" * 64)
        assert _run(target) == 1

    def test_unpacked_takeout_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "Takeout" / "Mail" / "User Settings" / "Filters.json"
        target.parent.mkdir(parents=True)
        target.write_text("{}")
        assert _run(target) == 1

    def test_reports_every_offending_file_not_just_the_first(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        first = tmp_path / "a.mbox"
        second = tmp_path / "b.mbox"
        first.write_bytes(b"x")
        second.write_bytes(b"x")
        assert main([str(first), str(second)]) == 1
        err = capsys.readouterr().err
        assert "a.mbox" in err and "b.mbox" in err


class TestAllows:
    def test_ordinary_source_file(self, tmp_path: Path) -> None:
        target = tmp_path / "module.py"
        target.write_text("def f() -> None:\n    return None\n")
        assert _run(target) == 0

    def test_prose_mentioning_a_from_line(self, tmp_path: Path) -> None:
        # This very repository's documentation and tests discuss mbox From_
        # lines constantly. A guard that blocks them is a guard that gets
        # bypassed with --no-verify, which is worse than no guard.
        target = tmp_path / "notes.md"
        target.write_text(
            "The mbox separator looks like:\n\n"
            "    From someone@example.invalid Mon Jan  5 09:12:33 2009\n"
        )
        assert _run(target) == 0

    def test_python_file_starting_with_the_word_from(self, tmp_path: Path) -> None:
        target = tmp_path / "imports.py"
        target.write_text("from __future__ import annotations\n")
        assert _run(target) == 0

    def test_missing_path_is_not_an_error(self, tmp_path: Path) -> None:
        # pre-commit can pass a path that a previous hook has just deleted.
        assert main([str(tmp_path / "gone.txt")]) == 0
