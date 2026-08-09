"""Synthetic mbox fixture generation.

The load-bearing piece of the project. Nothing downstream can be exercised
without input, and the real Takeout export can never be a test fixture in a
public repository — so the project generates its own. See docs/plan.md, Phase 2.

Two-stage construction, because the pathologies split cleanly in two.
*Structural* ones — nesting, charsets, absent headers, attachment shapes — are
expressed through the stdlib email API. *Corruption* ones — an 8-bit byte in a
header, an embedded NUL, a body cut mid-sentence — are applied to the serialized
bytes afterwards, via placeholder tokens planted during stage one. Expressing
corruption through the email API means fighting it (it exists to produce valid
output); expressing structure through byte edits means reimplementing MIME.

Determinism is a hard requirement — `--seed` is asserted byte-reproducible — and
three things quietly break it if you let them:

- `MIMEMultipart` picks a *random* boundary at construction. Every boundary here
  is overwritten with a deterministic one before serialization.
- `email.utils.make_msgid()` mixes in randomness and the host FQDN. See
  `addresses.message_id`.
- Anything reading the clock. Dates are derived from a fixed epoch plus
  seeded offsets.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.encoders import encode_base64
from email.generator import BytesGenerator
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from enum import StrEnum
from io import BytesIO
from pathlib import Path

from gmail_archive.fixtures.addresses import (
    message_id,
    pick_address,
    pick_display_address,
)

# Planted during stage one, substituted for raw bytes during stage two. Chosen
# to be impossible in generated prose and to survive header folding intact.
_TOKEN_8BIT = "X8BITX"
_TOKEN_NUL = "XNULX"

_EPOCH = datetime(2004, 3, 1, 9, 15, 0, tzinfo=UTC)

_WORDS: tuple[str, ...] = (
    "archive",
    "message",
    "thread",
    "label",
    "folder",
    "digest",
    "reply",
    "attachment",
    "header",
    "envelope",
    "mailbox",
    "delivery",
    "transfer",
    "encoding",
    "boundary",
    "multipart",
    "charset",
    "signature",
    "quota",
    "spool",
    "relay",
    "queue",
    "index",
    "snapshot",
    "checksum",
    "offset",
)

_SUBJECT_PREFIXES: tuple[str, ...] = ("", "Re: ", "Fwd: ", "Re: Re: ")

_LEGACY_CHARSETS: tuple[tuple[str, str], ...] = (
    # (declared charset, a byte-safe sample that actually encodes in it)
    ("iso-8859-1", "Café façade naïve"),
    ("windows-1252", "Smart “quotes” and an em—dash"),
    ("koi8-r", "Привет мир"),
    ("iso-8859-15", "Prix: 12€ le kilo"),
    ("ansi_x3.4-1968", "plain ascii only"),
)

# A fixed blob, byte-identical wherever it appears, so dedup has a target.
_REPEATED_ATTACHMENT = bytes(range(256)) * 64


class Pathology(StrEnum):
    """Individually selectable defects. Values are the CLI spelling."""

    LABELS_ABSENT = "labels-absent"
    LABELS_NESTED = "labels-nested"
    LABELS_PUNCTUATED = "labels-punctuated"
    DATE_MISSING = "date-missing"
    DATE_NAIVE = "date-naive"
    DATE_UNPARSEABLE = "date-unparseable"
    DATE_FAR_FUTURE = "date-far-future"
    MSGID_MISSING = "msgid-missing"
    MSGID_DUPLICATE = "msgid-duplicate"
    QUOTED_FROM = "quoted-from"
    BARE_FROM = "bare-from"
    HEADER_8BIT = "header-8bit"
    HEADER_RFC2047_SPLIT = "header-rfc2047-split"
    BODY_OVER_TSVECTOR = "body-over-tsvector"
    BODY_NUL = "body-nul"
    BODY_TRUNCATED = "body-truncated"
    CHARSET_LEGACY = "charset-legacy"
    CHARSET_NONEXISTENT = "charset-nonexistent"
    CHARSET_ABSENT = "charset-absent"
    DEEP_NESTING = "deep-nesting"
    BASE64_BAD_PADDING = "base64-bad-padding"
    ATTACH_REPEATED = "attach-repeated"
    ATTACH_ZERO_BYTE = "attach-zero-byte"
    ATTACH_PATH_FILENAME = "attach-path-filename"
    ATTACH_UNICODE_FILENAME = "attach-unicode-filename"
    ATTACH_OVERSIZED = "attach-oversized"
    HTML_ALTERNATIVE = "html-alternative"


# Rates measured against the real export (docs/progress.md). The default mix
# reproduces these rather than weighting pathologies uniformly: a generator that
# emits 4% truncated messages tests the parser against a corpus nobody has.
#
# Pathologies absent from this table are absent from the real export too. They
# stay generatable — absence in one corpus is not proof of impossibility — but
# they are opt-in, and ATTACH_OVERSIZED is opt-in for the additional reason that
# it costs 25 MB a message.
MEASURED_RATES: dict[Pathology, float] = {
    # Measured on the live archive: 239,773 of 277,017 messages carry an HTML
    # body, so this is the *majority* shape of real mail, not a defect. It is
    # in the pathology list only because that is the mechanism the generator
    # has for varying structure.
    #
    # It was missing entirely until #32 needed it, which meant the synthetic
    # corpus — the thing standing in for the real export in every test —
    # contained no HTML at all. A test asserting something about HTML
    # rendering would have passed by rendering nothing.
    Pathology.HTML_ALTERNATIVE: 0.866,
    Pathology.LABELS_NESTED: 0.11,
    Pathology.CHARSET_LEGACY: 0.11,
    Pathology.DATE_MISSING: 0.027,
    Pathology.DATE_NAIVE: 0.014,
    Pathology.QUOTED_FROM: 0.010,
    Pathology.BODY_OVER_TSVECTOR: 0.004,
    Pathology.HEADER_8BIT: 0.002,
    Pathology.LABELS_ABSENT: 0.018,
    Pathology.MSGID_DUPLICATE: 0.0004,
    Pathology.BODY_NUL: 0.00014,
    Pathology.MSGID_MISSING: 0.0001,
    Pathology.DATE_UNPARSEABLE: 0.0001,
    Pathology.DATE_FAR_FUTURE: 0.00001,
}

# Only one member of each group can apply to a single message; when the random
# draw picks several, the first in enum order wins.
_CONFLICT_GROUPS: tuple[frozenset[Pathology], ...] = (
    frozenset(
        {
            Pathology.DATE_MISSING,
            Pathology.DATE_NAIVE,
            Pathology.DATE_UNPARSEABLE,
            Pathology.DATE_FAR_FUTURE,
        }
    ),
    frozenset({Pathology.MSGID_MISSING, Pathology.MSGID_DUPLICATE}),
    frozenset(
        {
            Pathology.LABELS_ABSENT,
            Pathology.LABELS_NESTED,
            Pathology.LABELS_PUNCTUATED,
        }
    ),
    frozenset(
        {
            Pathology.CHARSET_LEGACY,
            Pathology.CHARSET_NONEXISTENT,
            Pathology.CHARSET_ABSENT,
        }
    ),
    frozenset({Pathology.QUOTED_FROM, Pathology.BARE_FROM}),
    frozenset({Pathology.BODY_OVER_TSVECTOR, Pathology.BODY_TRUNCATED}),
    # A legacy-charset or absent-charset text part is built by hand and the
    # alternative wrapper would obscure which part the assertion is about.
    frozenset(
        {
            Pathology.HTML_ALTERNATIVE,
            Pathology.CHARSET_LEGACY,
            Pathology.CHARSET_NONEXISTENT,
            Pathology.CHARSET_ABSENT,
        }
    ),
)


@dataclass(frozen=True, slots=True)
class GenerationReport:
    path: Path
    count: int
    seed: int
    bytes_written: int
    pathology_counts: dict[str, int] = field(default_factory=dict)


def _resolve_conflicts(active: set[Pathology]) -> set[Pathology]:
    for group in _CONFLICT_GROUPS:
        overlap = active & group
        if len(overlap) > 1:
            keep = sorted(overlap, key=lambda p: list(Pathology).index(p))[0]
            active -= overlap - {keep}
    return active


def _sentence(rng: random.Random, words: int) -> str:
    picked = [rng.choice(_WORDS) for _ in range(words)]
    return picked[0].capitalize() + " " + " ".join(picked[1:]) + "."


def _body_text(rng: random.Random, sentences: int = 6) -> str:
    return "\n".join(_sentence(rng, rng.randint(6, 14)) for _ in range(sentences))


def _labels(rng: random.Random, active: set[Pathology]) -> str | None:
    if Pathology.LABELS_ABSENT in active:
        return None
    base = ["Inbox", "Important", "Archived"][: 1 + rng.randint(0, 2)]
    if Pathology.LABELS_NESTED in active:
        base.append(rng.choice(["Projects/Archive/2011", "Work/Clients/Acme"]))
    if Pathology.LABELS_PUNCTUATED in active:
        # Gmail quotes a label containing a comma, which is exactly why a naive
        # split(",") on this header is wrong.
        base.append('"Receipts, invoices"')
        base.append("Café/Notes")
    return ",".join(base)


def _date_header(rng: random.Random, index: int, active: set[Pathology]) -> str | None:
    if Pathology.DATE_MISSING in active:
        return None
    if Pathology.DATE_UNPARSEABLE in active:
        return "Yesterday afternoon, around tea time"
    when = _EPOCH + timedelta(minutes=index * 37 + rng.randint(0, 30))
    if Pathology.DATE_FAR_FUTURE in active:
        when = when.replace(year=2611)
    if Pathology.DATE_NAIVE in active:
        return when.strftime("%a, %d %b %Y %H:%M:%S")
    return format_datetime(when)


def _attachment(rng: random.Random, active: set[Pathology]) -> MIMEBase | None:
    if Pathology.ATTACH_REPEATED in active:
        data, name = _REPEATED_ATTACHMENT, "quarterly-report.bin"
    elif Pathology.ATTACH_ZERO_BYTE in active:
        data, name = b"", "empty.dat"
    elif Pathology.ATTACH_PATH_FILENAME in active:
        data, name = b"traversal", "../../etc/passwd"
    elif Pathology.ATTACH_UNICODE_FILENAME in active:
        data, name = b"unicode name", "réçu-发票-🧾.pdf"
    elif Pathology.ATTACH_OVERSIZED in active:
        # Above Gmail's own 25 MB limit, which the real export contains.
        data, name = bytes(rng.getrandbits(8) for _ in range(64)) * 420_000, "big.bin"
    else:
        return None

    part = MIMEBase("application", "octet-stream")
    part.set_payload(data)
    encode_base64(part)
    # Stored as declared and never trusted as a path downstream (plan.md, Phase 4).
    part.add_header("Content-Disposition", "attachment", filename=name)
    return part


def _text_part(rng: random.Random, active: set[Pathology], body: str) -> MIMEBase:
    if Pathology.CHARSET_LEGACY in active:
        charset, sample = _LEGACY_CHARSETS[rng.randrange(len(_LEGACY_CHARSETS))]
        part = MIMEBase("text", "plain")
        part.set_param("charset", charset)
        part.set_payload((sample + "\n\n" + body).encode(charset, "replace"))
        part.add_header("Content-Transfer-Encoding", "8bit")
        return part
    if Pathology.CHARSET_NONEXISTENT in active:
        part = MIMEBase("text", "plain")
        part.set_param("charset", "unicode")  # not a real charset; never was
        part.set_payload(body.encode("utf-8"))
        part.add_header("Content-Transfer-Encoding", "8bit")
        return part
    if Pathology.CHARSET_ABSENT in active:
        part = MIMEBase("text", "plain")
        part.set_payload(body.encode("utf-8"))
        part.add_header("Content-Transfer-Encoding", "8bit")
        return part
    # 8bit rather than MIMEText's base64. Not cosmetic: a base64 body hides its
    # own content from the mbox writer, so a `From ` line inside it would never
    # be quoted and an embedded NUL would never reach the file as a NUL. The
    # defects have to survive serialization to be defects at all — and real
    # Takeout bodies are overwhelmingly 7bit/8bit/quoted-printable anyway.
    part = MIMEBase("text", "plain")
    part.set_param("charset", "utf-8")
    part.set_payload(body.encode("utf-8"))
    part.add_header("Content-Transfer-Encoding", "8bit")
    return part


def _alternative(text_part: MIMEBase, body: str) -> MIMEBase:
    """Wrap a text part with an HTML sibling, the way most real mail is shaped.

    `multipart/alternative` with `text/plain` first and `text/html` second, per
    RFC 2046: least-faithful representation first, so a client that stops at
    the first part it understands gets the plain one.

    8bit for the same reason `_text_part` uses it — a base64 part hides its
    content from the mbox writer, and a defect that cannot reach the file is
    not a defect the parser will ever see.
    """
    html = MIMEBase("text", "html")
    html.set_param("charset", "utf-8")
    escaped = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = "".join(f"<p>{line}</p>" for line in escaped.split("\n") if line)
    html.set_payload(f"<html><body>{paragraphs}</body></html>".encode())
    html.add_header("Content-Transfer-Encoding", "8bit")

    alternative = MIMEMultipart("alternative")
    alternative.attach(text_part)
    alternative.attach(html)
    return alternative


def _nest(rng: random.Random, inner: MIMEBase, depth: int) -> MIMEBase:
    """alternative inside related inside mixed, repeating outwards."""
    subtypes = ("alternative", "related", "mixed")
    current = inner
    for level in range(depth):
        wrapper = MIMEMultipart(subtypes[level % len(subtypes)])
        wrapper.attach(current)
        current = wrapper
    return current


def _set_boundaries(part: MIMEBase, index: int, counter: list[int]) -> None:
    """Set deterministic boundaries.

    MIMEMultipart picks a random one at construction — see the module docstring.
    """
    if part.is_multipart():
        part.set_boundary(f"=-=fixture-{index:06d}-{counter[0]:03d}=-=")
        counter[0] += 1
        for sub in part.get_payload():
            if isinstance(sub, MIMEBase):
                _set_boundaries(sub, index, counter)


def _build(
    rng: random.Random, index: int, active: set[Pathology], dup_msgid: str
) -> tuple[bytes, bool]:
    """Return (raw RFC822 bytes, whether the writer should mbox-quote it)."""
    body = _body_text(rng)
    if Pathology.BODY_OVER_TSVECTOR in active:
        # Comfortably past the 1 MB tsvector hard limit — ~97 bytes a sentence,
        # so this lands near 1.4 MB. Being merely close to the limit would make
        # the fixture pass or fail on word-length luck.
        body = "\n".join(_sentence(rng, 12) for _ in range(14_500))
    if Pathology.BODY_NUL in active:
        body = body + "\n" + _TOKEN_NUL + "\n"
    if Pathology.QUOTED_FROM in active or Pathology.BARE_FROM in active:
        body = (
            body
            + "\n\nFrom the desk of the archivist, a line that looks like a "
            + "separator.\n"
        )

    root: MIMEBase = _text_part(rng, active, body)
    if Pathology.HTML_ALTERNATIVE in active:
        root = _alternative(root, body)
    attachment = _attachment(rng, active)

    if Pathology.BASE64_BAD_PADDING in active:
        broken = MIMEBase("application", "octet-stream")
        # Pre-encoded by hand with the padding filed off; encoders would fix it.
        broken.set_payload("QUJDREVGRw")
        broken.add_header("Content-Transfer-Encoding", "base64")
        broken.add_header("Content-Disposition", "attachment", filename="broken.bin")
        mixed = MIMEMultipart("mixed")
        mixed.attach(root)
        mixed.attach(broken)
        root = mixed
    elif attachment is not None:
        mixed = MIMEMultipart("mixed")
        mixed.attach(root)
        mixed.attach(attachment)
        root = mixed

    if Pathology.BODY_TRUNCATED in active and not root.is_multipart():
        # Truncation is only observable if there is a structure left dangling.
        # Wrapping guarantees a closing boundary exists to be cut off, which is
        # what the parser will actually trip over.
        wrapper = MIMEMultipart("mixed")
        wrapper.attach(root)
        wrapper.attach(MIMEText("trailing part that will be lost", "plain", "utf-8"))
        root = wrapper

    if Pathology.DEEP_NESTING in active:
        root = _nest(rng, root, depth=5)

    subject = rng.choice(_SUBJECT_PREFIXES) + _sentence(rng, rng.randint(3, 7))
    if Pathology.HEADER_8BIT in active:
        subject = subject + " " + _TOKEN_8BIT
    root["Subject"] = subject

    if Pathology.HEADER_RFC2047_SPLIT in active:
        # U+2026 is e2 80 a6; split across two encoded-words so neither half is
        # decodable alone. Real 2000s-era mailers did exactly this.
        del root["Subject"]
        root["Subject"] = "=?utf-8?b?4oA=?= =?utf-8?b?pg==?= truncated ellipsis"

    root["From"] = pick_display_address(rng)
    root["To"] = pick_display_address(rng)
    if rng.random() < 0.15:
        root["Cc"] = pick_display_address(rng)

    date = _date_header(rng, index, active)
    if date is not None:
        root["Date"] = date

    if Pathology.MSGID_DUPLICATE in active:
        root["Message-ID"] = dup_msgid
    elif Pathology.MSGID_MISSING not in active:
        root["Message-ID"] = message_id(rng, index)

    root["X-GM-THRID"] = str(1_500_000_000_000 + index)
    labels = _labels(rng, active)
    if labels is not None:
        root["X-Gmail-Labels"] = labels

    _set_boundaries(root, index, [0])

    buf = BytesIO()
    # mangle_from_ is False because this module does its own mbox quoting in the
    # writer, where the BARE_FROM pathology can opt out of it.
    BytesGenerator(
        buf, mangle_from_=False, maxheaderlen=0, policy=policy.compat32
    ).flatten(root)
    raw = buf.getvalue()

    # ── stage two: byte-level corruption ────────────────────────────────────
    raw = raw.replace(_TOKEN_8BIT.encode(), b"\xe9\xa0")
    raw = raw.replace(_TOKEN_NUL.encode(), b"\x00")
    if Pathology.BODY_TRUNCATED in active:
        raw = raw[: len(raw) * 3 // 4].rstrip() + b"\n"

    return raw, Pathology.BARE_FROM not in active


def _mbox_quote(raw: bytes) -> bytes:
    """mboxo quoting: a body line starting with `From ` becomes `>From `.

    This is what Takeout does, measured at ~1% of the real export, and it is why
    file bytes are not the original message — see the raw_sha256 decision.
    """
    return raw.replace(b"\nFrom ", b"\n>From ")


def _envelope_line(rng: random.Random, index: int) -> bytes:
    when = _EPOCH + timedelta(minutes=index * 37)
    stamp = when.strftime("%a %b %d %H:%M:%S %Y")
    return f"From {pick_address(rng)} {stamp}\n".encode()


def _select(
    rng: random.Random, requested: Sequence[Pathology] | None, index: int
) -> set[Pathology]:
    if requested:
        # Round-robin, so every requested pathology is present at least once for
        # any count >= len(requested) — which the CLI enforces.
        return {requested[index % len(requested)]}
    active = {p for p, rate in MEASURED_RATES.items() if rng.random() < rate}
    return _resolve_conflicts(active)


def generate(
    out: Path,
    *,
    count: int,
    seed: int,
    pathologies: Iterable[Pathology] | None = None,
) -> GenerationReport:
    """Write `count` synthetic messages to `out` as an mbox. Deterministic in `seed`."""
    requested = list(pathologies) if pathologies else None
    if requested and count < len(requested):
        raise ValueError(
            f"count={count} is fewer than the {len(requested)} requested "
            "pathologies; each must appear at least once"
        )

    rng = random.Random(seed)
    counts: dict[str, int] = {}
    written = 0
    # Drawn from its own generator so the shared id does not depend on how many
    # messages preceded it — every MSGID_DUPLICATE message collides with the
    # same value, which is what makes the collision observable at any count.
    dup_msgid = message_id(random.Random(seed), 0)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as fh:
        for index in range(count):
            active = _select(rng, requested, index)
            raw, quote = _build(rng, index, active, dup_msgid)
            for p in active:
                counts[p.value] = counts.get(p.value, 0) + 1
            payload = _mbox_quote(raw) if quote else raw
            chunk = _envelope_line(rng, index) + payload
            if not chunk.endswith(b"\n"):
                chunk += b"\n"
            chunk += b"\n"
            fh.write(chunk)
            written += len(chunk)

    return GenerationReport(
        path=out,
        count=count,
        seed=seed,
        bytes_written=written,
        pathology_counts=dict(sorted(counts.items())),
    )
