#!/usr/bin/env python3
"""Pre-commit guard: refuse to stage mail data.

This repository is public and its history is permanent. A commit containing
real mail cannot be walked back with a revert — it needs a history rewrite, and
by then the objects have been fetched. There is no CI on this repository yet,
so this hook is the only thing standing between a careless `git add .` and a
permanent public disclosure of twenty years of mail.

Three checks, in the order they catch real mistakes:

1. Path patterns (`*.mbox`, `blobs/`, `data/`, `backups/`). Mirrors .gitignore,
   which does not help at all once someone reaches for `git add -f`.
2. Content sniffing, because the first thing anyone does with a Takeout export
   is rename it. A file whose first line is an mbox `From_` separator is mail
   regardless of what it is called.
3. Size, because the fixtures this project generates are large and belong on
   disk rather than in git.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024

BLOCKED_SUFFIXES = (".mbox", ".mbx", ".eml", ".pst", ".ost")
BLOCKED_DIRS = ("blobs", "data", "backups")

# The mbox separator: "From " then an address-ish token then an asctime date.
# Anchored to the very start of the file so a quoted ">From " line in prose or
# a Python file that happens to start with the word "From" cannot trip it.
FROM_LINE = re.compile(rb"^From \S+ (Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} ")


def _reasons(path: Path) -> list[str]:
    found: list[str] = []
    parts = path.parts

    if path.suffix.lower() in BLOCKED_SUFFIXES:
        found.append(f"blocked extension {path.suffix!r} — mail data never goes in git")
    if any(part in BLOCKED_DIRS for part in parts):
        found.append(
            "lives under a data directory (blobs/, data/, backups/) — these hold "
            "the archive itself, not source"
        )

    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(256)
    except OSError:
        return found

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
