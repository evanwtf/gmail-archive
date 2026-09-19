#!/usr/bin/env python3
"""Pre-commit guard: refuse to stage mail data.

This repository is public and its history is permanent. A commit containing
real mail cannot be walked back with a revert — it needs a history rewrite, and
by then the objects have been fetched. There is no CI on this repository yet,
so this hook is the only thing standing between a careless `git add .` and a
permanent public disclosure of twenty years of mail.

Four checks, in the order they catch real mistakes:

1. Path patterns (`*.mbox`, `blobs/`, `data/`, `backups/`, `Takeout/`). Mirrors
   .gitignore, which does not help at all once someone reaches for `git add -f`.
2. Content sniffing, because the first thing anyone does with a Takeout export
   is rename it. A file whose first line is an mbox `From_` separator is mail
   regardless of what it is called.
3. Archive containers, by extension and by magic bytes. A Takeout export does
   not arrive as an .mbox — it arrives as a .tgz with the mbox inside, and
   gzip's magic number is not a `From_` line, so check 2 waves it straight
   through. The real export was two files: 7.0 GB (caught by size, which is
   luck rather than design) and 56 KB (caught by nothing at all).
4. Size, because the fixtures this project generates are large and belong on
   disk rather than in git.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024

BLOCKED_SUFFIXES = (".mbox", ".mbx", ".eml", ".pst", ".ost")
BLOCKED_DIRS = ("blobs", "data", "backups")

# Google names the export directory `Takeout`, whatever the tarball was called.
BLOCKED_DIR_NAMES_CI = ("takeout",)

# Nothing in this repository is legitimately a compressed archive: the plan is
# explicit that large fixtures are generated at runtime rather than committed.
# So the whole class is refused, rather than trying to tell a harmless tarball
# from one with twenty years of mail in it.
ARCHIVE_SUFFIXES = (".tgz", ".tar", ".gz", ".bz2", ".xz", ".zip", ".7z", ".zst")

# (offset, magic, label). Extensions are a courtesy; these are the fact.
ARCHIVE_MAGIC: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x1f\x8b", "gzip"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"(\xb5/\xfd", "zstd"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (257, b"ustar", "tar"),
)

# The mbox separator: "From " then an address-ish token then an asctime date.
# Anchored to the very start of the file so a quoted ">From " line in prose or
# a Python file that happens to start with the word "From" cannot trip it.
FROM_LINE = re.compile(rb"^From \S+ (Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} ")


def _reasons(path: Path) -> list[str]:
    found: list[str] = []
    parts = path.parts

    suffix = path.suffix.lower()

    if suffix in BLOCKED_SUFFIXES:
        found.append(f"blocked extension {suffix!r} — mail data never goes in git")
    if suffix in ARCHIVE_SUFFIXES:
        found.append(
            f"blocked archive extension {suffix!r} — a Takeout export is a tarball "
            "with the mbox inside; generate fixtures at runtime instead"
        )
    if any(part in BLOCKED_DIRS for part in parts):
        found.append(
            "lives under a data directory (blobs/, data/, backups/) — these hold "
            "the archive itself, not source"
        )
    if any(part.lower() in BLOCKED_DIR_NAMES_CI for part in parts):
        found.append(
            "lives under a Takeout/ directory — this is an unpacked Google export"
        )

    try:
        size = path.stat().st_size
        # 512 bytes, not 256: the tar magic sits at offset 257.
        with path.open("rb") as fh:
            head = fh.read(512)
    except OSError:
        return found

    for offset, magic, label in ARCHIVE_MAGIC:
        if head[offset : offset + len(magic)] == magic:
            found.append(
                f"{label} archive by magic bytes, whatever the extension says — "
                "unpack it outside the working tree"
            )
            break

    if size > MAX_BYTES:
        found.append(
            f"{size / 1024 / 1024:.1f} MB exceeds the {MAX_BYTES // 1024 // 1024} MB "
            "limit — generate large fixtures at runtime instead of committing them"
        )
    if FROM_LINE.match(head):
        found.append(
            "starts with an mbox 'From_' separator — this is a mail spool, "
            "whatever it has been renamed to"
        )
    return found


def main(argv: list[str]) -> int:
    failures: list[tuple[str, list[str]]] = []
    for name in argv:
        path = Path(name)
        if not path.is_file():
            continue
        reasons = _reasons(path)
        if reasons:
            failures.append((name, reasons))

    if not failures:
        return 0

    print("Refusing to stage mail data in a public repository:\n", file=sys.stderr)
    for name, reasons in failures:
        print(f"  {name}", file=sys.stderr)
        for reason in reasons:
            print(f"      - {reason}", file=sys.stderr)
    print(
        "\nIf this is a false positive, move the file out of the working tree "
        "rather than passing --no-verify.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
