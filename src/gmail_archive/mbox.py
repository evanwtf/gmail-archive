"""Byte-level mbox splitter.

Scans a file for `From_` envelope separators and yields `(offset, length)` ranges
for each message. The file is never loaded into memory — the scan is a single pass
over an `mmap`, and workers `pread` their own range independently.

The mbox format is underspecified and implementations differ. This module targets
**mboxrd** as written by Google Takeout, which is the only format the project has
been tested against. The critical property: a body line starting with `From ` is
always quoted as `>From ` by the writer, so a bare `\nFrom ` at line start is
unambiguously an envelope separator.

Measured on a real export: 3,855 `>From ` lines, 86 `>>From `, and zero
`>>>From `. Every one of the 86 sits among unquoted neighbours — 84 with plain
text on both sides. That is the signature of mboxrd, and it means the splitter
can treat `\nFrom ` as a hard boundary with high confidence.

The splitter does NOT unquote. That is the parser's job. The splitter's only job
is to find message boundaries.
"""

from __future__ import annotations

import logging
import mmap
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The byte sequence that separates messages in an mbox file. A `From_` line
# starts with `From ` (note the space, distinguishing it from the `From:`
# header). The first message in the file starts at offset 0.
_SEPARATOR = b"\nFrom "


@dataclass
class MboxScan:
    """Result of scanning an mbox file for message boundaries."""

    offsets: list[tuple[int, int]]
    """List of (byte_offset, byte_length) for each message."""

    total_bytes: int
    """Total size of the mbox file in bytes."""

    message_count: int
    """Number of messages found."""


def scan(path: Path) -> MboxScan:
    """Scan an mbox file and return message boundary offsets.

    The file is memory-mapped and scanned in a single pass. Each returned
    (offset, length) pair covers one complete message including its `From_`
    envelope line.
    """
    if not path.is_file():
        raise FileNotFoundError(f"mbox not found: {path}")

    size = path.stat().st_size
    if size == 0:
        return MboxScan(offsets=[], total_bytes=0, message_count=0)

    with open(path, "rb") as fh:
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            offsets: list[tuple[int, int]] = []

            # The first message starts at offset 0.
            starts = [0]
            pos = 0
            while True:
                pos = mapped.find(_SEPARATOR, pos)
                if pos == -1:
                    break
                # +1 to skip the \n; the message starts at the `From_` line.
                starts.append(pos + 1)
                pos += 1  # Advance past the \n so we don't find the same one.

            for i, start in enumerate(starts):
                if i + 1 < len(starts):
                    end = starts[i + 1]
                else:
                    end = size
                offsets.append((start, end - start))

    return MboxScan(
        offsets=offsets,
        total_bytes=size,
        message_count=len(offsets),
    )


def read_message(path: Path, offset: int, length: int) -> bytes:
    """Read one message from an mbox file at the given offset and length.

    Uses seek/read so workers can read independently without contention on a
    shared file handle.
    """
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(length)


def strip_envelope(raw: bytes) -> bytes:
    """Strip the `From_` envelope line from a message, returning just the
    RFC822 headers and body.

    The envelope line is the first line of each message in mbox format and
    is not part of the RFC822 message.
    """
    idx = raw.find(b"\n")
    if idx == -1:
        # Single-line message with no newline — the whole thing is the envelope.
        return b""
    return raw[idx + 1 :]
