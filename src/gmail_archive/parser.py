"""Bytes in, typed `ParsedMessage` out.

The raw bytes are ground truth. Everything here is a derived view, and every
field is best-effort: a failure records a `ParseWarning` and moves on. `parse()`
does not raise, for any input — one bad 2009 Outlook message must not kill a
full-corpus run, and a hypothesis property test holds that line.

Three hazards this module exists to contain, each measured against a real
export before being coded for:

- **Postgres `text` cannot hold NUL (U+0000)**, and decoded bodies do contain
  them. Lone surrogates are the same class of problem, arriving via
  `surrogateescape` on undecodable bytes. One of either aborts a COPY batch of
  thousands, so both are stripped here rather than at the storage layer.
- **The tsvector 1 MB hard limit.** `search_text` is bounded well under it. The
  bound is applied in *bytes*, not characters, because a character bound says
  nothing about the encoded size.
- **mbox quoting is ambiguous.** See `unquote_mbox`.
"""

from __future__ import annotations

import csv
import email
import email.header
import email.message
import email.utils
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from io import StringIO

# Well under the 1 MB tsvector hard limit. Halving it costs nothing — nobody
# searches for a term 500 KB into a message — and leaves room for the fact that
# a tsvector is not always smaller than its input.
SEARCH_TEXT_MAX_BYTES = 512_000

#: Headers kept in queryable form beyond the ones with their own columns (#34).
#:
#: The blob holds every header, so nothing is lost either way — the question is
#: only what can be queried without reading 277,000 files. These are the ones
#: that settle "machine or person", which is the distinction the whole
#: analytics side rests on and currently guesses at from address shape and
#: Gmail categories. Both of those have holes: Gmail categories thin out in the
#: early years, which is exactly where the human correspondence is, and
#: `no-reply@` matching misses the bulk sender with a real-looking address.
#:
#: An allowlist rather than everything, because "every header" on this corpus
#: is tens of millions of rows of `X-Spam-Status` and DKIM signatures nobody
#: will ever query. Growing the list is a migration-free parser change; the
#: table shape does not move.
KEPT_HEADERS: tuple[str, ...] = (
    # RFC 2369. Near-conclusive for bulk, and present on essentially all
    # legitimate marketing and notification mail.
    "List-Unsubscribe",
    # Which list, not just that it is one — makes list traffic a dimension
    # rather than a lump.
    "List-Id",
    # The pre-List-Unsubscribe convention. Matters for the 2004-2010 half of
    # this archive, when the modern header was not yet universal.
    "Precedence",
    # RFC 3834: vacation responders, bounces, ticket robots.
    "Auto-Submitted",
    # Which client sent it — a human at a mail client versus a bulk platform,
    # and incidentally a 22-year history of what the user ran.
    "X-Mailer",
    "User-Agent",
    # Bounce address. Often differs from From: for bulk senders, and the
    # mismatch is itself the signal.
    "Return-Path",
)

#: Case-insensitive lookup to the canonical spelling above.
_KEPT_HEADERS_LC = {name.lower(): name for name in KEPT_HEADERS}

#: A single stored header value. Long enough for a real `List-Unsubscribe`
#: with several URLs; short enough that a pathological header cannot turn one
#: message into a megabyte of index.
KEPT_HEADER_MAX_CHARS = 4_000

_SURROGATES = re.compile("[\ud800-\udfff]")

# mboxrd: a body line of `>*From ` carries one extra `>`. Anchored at line start.
_QUOTED_FROM = re.compile(rb"(?m)^(>+)(From )")


class Warn(StrEnum):
    """Warning codes. Stable strings — they end up in a database column."""

    HEADER_UNDECODABLE = "header-undecodable"
    DATE_MISSING = "date-missing"
    DATE_UNPARSEABLE = "date-unparseable"
    DATE_IMPLAUSIBLE = "date-implausible"
    DATE_TZ_OUT_OF_RANGE = "date-tz-out-of-range"
    MESSAGE_ID_MISSING = "message-id-missing"
    BODY_UNDECODABLE = "body-undecodable"
    CHARSET_UNKNOWN = "charset-unknown"
    NUL_STRIPPED = "nul-stripped"
    SURROGATE_STRIPPED = "surrogate-stripped"
    SEARCH_TEXT_TRUNCATED = "search-text-truncated"
    UNQUOTE_AMBIGUOUS = "unquote-ambiguous"
    ATTACHMENT_UNDECODABLE = "attachment-undecodable"
    STRUCTURE_UNPARSEABLE = "structure-unparseable"
    DATE_FROM_RECEIVED = "date-from-received"


@dataclass(frozen=True, slots=True)
class ParseWarning:
    code: Warn
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str | None
    mime_type: str
    size: int
    sha256: str


@dataclass(slots=True)
class ParsedMessage:
    raw_sha256: str
    size_bytes: int
    message_id: str | None = None
    gmail_id: str | None = None
    thread_id: str | None = None
    subject: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    bcc_addrs: list[str] = field(default_factory=list)
    reply_to: str | None = None
    in_reply_to: str | None = None
    references_ids: list[str] = field(default_factory=list)
    internal_date: datetime | None = None
    labels: list[str] = field(default_factory=list)
    body_text: str = ""
    body_html: str = ""
    search_text: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    kept_headers: list[tuple[str, str]] = field(default_factory=list)
    """Allowlisted raw headers, `(canonical name, value)`. See `KEPT_HEADERS`."""
    parse_warnings: list[ParseWarning] = field(default_factory=list)


def unquote_mbox(raw: bytes) -> tuple[bytes, bool]:
    """Reverse mbox From_ quoting. Returns (unquoted, ambiguous).

    Measured on a real Takeout export: 3,855 lines of `>From `, 86 of `>>From `,
    and zero `>>>From `. Every one of the 86 sits among *unquoted* neighbours —
    84 with plain text on both sides. A genuinely double-quoted reply line would
    be surrounded by other `>>` lines, and it is not. That is the signature of
    **mboxrd** (the writer prefixes any `>*From `), not the mboxo the plan
    originally assumed, so stripping exactly one `>` is the correct inverse.

    It is not provably the correct inverse. Under mboxo a file `>>From ` would
    mean the original had `>>From `, and stripping a `>` corrupts it. The
    evidence favours mboxrd but does not settle it, so any line carrying more
    than one `>` is reported as ambiguous and the caller records a warning: 86
    lines corpus-wide, findable later rather than silently rewritten.

    The file round-trips byte-identically either way — re-quoting is this
    function's inverse — so the exposure is limited to `.eml` export fidelity
    on those lines.
    """
    ambiguous = False

    def sub(m: re.Match[bytes]) -> bytes:
        nonlocal ambiguous
        carets = m.group(1)
        if len(carets) > 1:
            ambiguous = True
        return carets[:-1] + m.group(2)

    return _QUOTED_FROM.sub(sub, raw), ambiguous


def requote_mbox(raw: bytes) -> bytes:
    """Inverse of `unquote_mbox`, for export. Round-trips at the file level."""
    return re.sub(rb"(?m)^(>*)(From )", lambda m: b">" + m.group(1) + m.group(2), raw)


def strip_unstorable(value: str) -> str:
    """Remove what Postgres `text` and `jsonb` cannot hold.

    NUL and lone surrogates. Public because this hazard is not confined to
    parsing: `imap-backfill` builds envelope JSON straight from pymap and hit
    exactly the same wall 194,000 messages into a run —

        UntranslatableCharacter: \u0000 cannot be converted to text

    — on a subject line containing a NUL. Anything assembling a value bound
    for Postgres from message content has to come through here.
    """
    if "\x00" in value:
        value = value.replace("\x00", "")
    if _SURROGATES.search(value):
        value = _SURROGATES.sub("", value)
    return value


def _sanitize(text: str, warnings: list[ParseWarning]) -> str:
    """Strip what Postgres `text` cannot store. Order matters: NUL first."""
    if "\x00" in text:
        text = text.replace("\x00", "")
        warnings.append(ParseWarning(Warn.NUL_STRIPPED))
    if _SURROGATES.search(text):
        text = _SURROGATES.sub("", text)
        warnings.append(ParseWarning(Warn.SURROGATE_STRIPPED))
    return text


def _header_str(value: object) -> str | None:
    """Coerce a header value to `str`.

    `Message.get()` under compat32 does **not** always return a string. When the
    raw header contains bytes it cannot decode as ASCII it returns an
    `email.header.Header` instance instead, and every type annotation that says
    otherwise — including this module's, before it was corrected — is wrong.

    Found the hard way: three messages out of 277,020 in a real export killed
    `parse()` with `AttributeError: 'Header' object has no attribute 'split'`,
    raised inside `parsedate_to_datetime`. Neither the hypothesis property test
    nor the 8-bit-header fixture caught it, because both put their bad bytes in
    an *unstructured* header (Subject) where nothing later calls `.split()`. The
    hazard is an 8-bit byte in a *structured* header — Date, Message-ID — which
    a downstream stdlib parser then treats as text.
    """
    if value is None or isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return None


def _decode_header(value: str | None, warnings: list[ParseWarning]) -> str | None:
    """RFC 2047 decode, best effort. Never raises."""
    if value is None:
        return None
    try:
        decoded = str(email.header.make_header(email.header.decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError) as exc:
        warnings.append(ParseWarning(Warn.HEADER_UNDECODABLE, type(exc).__name__))
        decoded = value
    return _sanitize(decoded, warnings)


def _addresses(msg: email.message.Message, name: str) -> list[str]:
    out: list[str] = []
    for raw in msg.get_all(name, []):
        try:
            for _, addr in email.utils.getaddresses([str(raw)]):
                if addr:
                    out.append(addr)
        except (ValueError, TypeError):
            continue
    return out


def _labels(value: str | None, warnings: list[ParseWarning]) -> list[str]:
    """Split X-Gmail-Labels.

    Decode *before* splitting. One non-ASCII label pushes the whole header
    through RFC 2047, which encodes the separating commas as `=2C`; splitting
    the raw value on "," then yields a single label and loses the rest with no
    error at all. Gmail also double-quotes any label containing a comma, so the
    split has to respect quoting — hence csv rather than str.split.
    """
    if value is None:
        return []
    decoded = _decode_header(value, warnings) or ""
    if not decoded.strip():
        return []
    try:
        row = next(csv.reader(StringIO(decoded), skipinitialspace=True))
    except (csv.Error, StopIteration):
        row = decoded.split(",")
    return [label.strip() for label in row if label.strip()]


def _date(value: str | None, warnings: list[ParseWarning]) -> datetime | None:
    if value is None:
        warnings.append(ParseWarning(Warn.DATE_MISSING))
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except Exception as exc:
        # raises from depths no tuple of types reliably predicts, and a date is
        # never worth failing a message for.
        warnings.append(ParseWarning(Warn.DATE_UNPARSEABLE, type(exc).__name__))
        return None
    if parsed is None:
        warnings.append(ParseWarning(Warn.DATE_UNPARSEABLE))
        return None
    # Postgres rejects timezone offsets outside ±15:59:59.
    if parsed.tzinfo is not None:
        offset = parsed.utcoffset()
        if offset is not None and abs(offset) > timedelta(hours=15, minutes=59):
            warnings.append(ParseWarning(Warn.DATE_TZ_OUT_OF_RANGE, str(offset)))
            return None
    # A date more than 90 days in the future is a broken header, not history.
    # Kept rather than discarded — the real export contains one — but flagged.
    _plausible_upper = datetime.now(UTC) + timedelta(days=90)
    _parsed_aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    if (
        not (1970 <= parsed.year <= _plausible_upper.year)
        or _parsed_aware > _plausible_upper
    ):
        warnings.append(ParseWarning(Warn.DATE_IMPLAUSIBLE, str(parsed)))
    return parsed


def _kept_headers(msg: email.message.Message) -> list[tuple[str, str]]:
    """The allowlisted headers, in `KEPT_HEADERS` order (#34).

    Raw values, not decoded. These are machine headers — URLs, list ids,
    tokens — and RFC 2047 decoding a `List-Unsubscribe` would only corrupt it.
    `_sanitize` still runs, because `text` cannot hold a NUL no matter where it
    came from and a header is not exempt from that.

    A header may legitimately appear more than once; each occurrence is kept,
    which is why the return type is a list of pairs rather than a mapping.
    """
    out: list[tuple[str, str]] = []
    for name in KEPT_HEADERS:
        try:
            values = msg.get_all(name)
        except Exception:
            continue
        if not values:
            continue
        for value in values:
            text = _header_str(value)
            if text is None:
                continue
            text = strip_unstorable(text).strip()
            if not text:
                continue
            out.append((_KEPT_HEADERS_LC[name.lower()], text[:KEPT_HEADER_MAX_CHARS]))
    return out


def _received_date(
    msg: email.message.Message, warnings: list[ParseWarning]
) -> datetime | None:
    """The arrival time from the `Received` chain, or None (#27, option 3).

    `Date:` is written by the sender and is therefore only as trustworthy as
    the sender's clock and intentions. The live archive holds one message dated
    2611 — the header genuinely says that, so it is not a parse bug — and with
    newest-first as the default ordering, that one message sorts above 22 years
    of real mail.

    `Received:` is written by the receiving server, which had no reason to lie
    and a working clock. Its date is after the final semicolon:

        Received: from x by y; Fri, 4 Apr 2025 01:59:09 -0700

    Headers are stored oldest-last, so `msg.get_all` returns the most recent
    hop first — but the *first* one is the receiving end, which is the one
    wanted here. Each is tried in turn, because a malformed hop in the middle
    of a chain should not cost the whole fallback.

    Deliberately not used as the primary date. `Date:` is what the message says
    about itself and is right for the overwhelming majority; this only steps in
    where `Date:` is missing or impossible, and says so with a warning so the
    substitution stays visible rather than silently rewriting history.
    """
    try:
        received = msg.get_all("Received")
    except Exception:
        return None
    if not received:
        return None
    for hop in received:
        value = _header_str(hop)
        if value is None or ";" not in value:
            continue
        # rpartition: the timestamp follows the *last* semicolon, and the
        # `from`/`by` clauses before it can contain their own.
        _, _, stamp = value.rpartition(";")
        stamp = stamp.strip()
        if not stamp:
            continue
        # Its own warning list, not the message's. A junk hop is not a defect
        # in the message: `Date:` already recorded whatever went wrong, and
        # repeating it per hop would bury the real warning. The list is still
        # inspected, because an implausible `Received` is no better than the
        # implausible `Date:` it would be standing in for.
        hop_warnings: list[ParseWarning] = []
        candidate = _date(stamp, hop_warnings)
        if candidate is None:
            continue
        if any(w.code is Warn.DATE_IMPLAUSIBLE for w in hop_warnings):
            continue
        warnings.append(ParseWarning(Warn.DATE_FROM_RECEIVED, stamp))
        return candidate
    return None


def _part_text(part: email.message.Message, warnings: list[ParseWarning]) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception as exc:
        warnings.append(ParseWarning(Warn.BODY_UNDECODABLE, type(exc).__name__))
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset()
    if charset:
        try:
            "".encode(charset)
        except LookupError:
            warnings.append(ParseWarning(Warn.CHARSET_UNKNOWN, charset))
            charset = None
    text = payload.decode(charset or "utf-8", errors="replace")
    return _sanitize(text, warnings)


def _bound_search_text(text: str, warnings: list[ParseWarning]) -> str:
    """Bound in bytes. A character bound says nothing about encoded size."""
    encoded = text.encode("utf-8")
    if len(encoded) <= SEARCH_TEXT_MAX_BYTES:
        return text
    warnings.append(ParseWarning(Warn.SEARCH_TEXT_TRUNCATED, str(len(encoded))))
    # errors="ignore" drops a character left partial by the cut.
    return encoded[:SEARCH_TEXT_MAX_BYTES].decode("utf-8", errors="ignore")


def is_attachment_part(part: email.message.Message) -> bool:
    """Whether `parse()` counts this MIME part as an attachment.

    Shared with `iter_attachment_payloads` so the two can never disagree.
    `part_index` in the database is a position in *this* sequence, not a MIME
    part number, so anything re-deriving it has to apply the same predicate in
    the same walk order or it will serve the wrong file.
    """
    if part.is_multipart():
        return False
    return (part.get_content_disposition() or "").lower() == "attachment" or bool(
        part.get_filename()
    )


def iter_attachment_payloads(raw: bytes) -> Iterator[tuple[int, Attachment, bytes]]:
    """Re-extract attachments, with their bytes, from a raw message.

    Ingest records an attachment's name, type, size and content hash but not
    its bytes — the raw message in the blob store already holds them, and
    storing them twice would roughly double the archive. Serving one therefore
    means parsing the message again on demand, which is why this exists.

    Yields `(part_index, attachment, payload)` in exactly the order `parse()`
    numbered them. Undecodable parts are skipped by both, so the indices line
    up with the `attachments` table.
    """
    try:
        msg = email.message_from_bytes(raw)
        walker = list(msg.walk())
    except Exception:
        return

    index = 0
    for part in walker:
        if not is_attachment_part(part):
            continue
        try:
            blob = part.get_payload(decode=True)
        except Exception:
            continue
        if not isinstance(blob, bytes):
            continue
        filename = part.get_filename()
        yield (
            index,
            Attachment(
                filename=_decode_header(filename, []) if filename else None,
                mime_type=(part.get_content_type() or "").lower(),
                size=len(blob),
                sha256=hashlib.sha256(blob).hexdigest(),
            ),
            blob,
        )
        index += 1


def parse(raw: bytes, *, already_unquoted: bool = False) -> ParsedMessage:
    """Parse one message. Never raises.

    `raw` is the mbox slice with the From_ separator already removed. Unquoting
    happens here so that `raw_sha256` is the hash of the true RFC822 bytes, per
    the locked decision — pass `already_unquoted=True` if the caller did it.
    """
    warnings: list[ParseWarning] = []

    if already_unquoted:
        body_bytes = raw
    else:
        body_bytes, ambiguous = unquote_mbox(raw)
        if ambiguous:
            warnings.append(ParseWarning(Warn.UNQUOTE_AMBIGUOUS))

    parsed = ParsedMessage(
        raw_sha256=hashlib.sha256(body_bytes).hexdigest(),
        size_bytes=len(body_bytes),
        parse_warnings=warnings,
    )

    try:
        msg = email.message_from_bytes(body_bytes)
    except Exception as exc:
        warnings.append(ParseWarning(Warn.STRUCTURE_UNPARSEABLE, type(exc).__name__))
        return parsed

    parsed.subject = _decode_header(_header_str(msg.get("Subject")), warnings)
    from_addrs = _addresses(msg, "From")
    parsed.from_addr = from_addrs[0] if from_addrs else None
    parsed.to_addrs = _addresses(msg, "To")
    parsed.cc_addrs = _addresses(msg, "Cc")
    parsed.bcc_addrs = _addresses(msg, "Bcc")
    reply_to = _addresses(msg, "Reply-To")
    parsed.reply_to = reply_to[0] if reply_to else None

    message_id = _header_str(msg.get("Message-ID"))
    if message_id is None:
        warnings.append(ParseWarning(Warn.MESSAGE_ID_MISSING))
    else:
        parsed.message_id = message_id.strip()

    in_reply_to = _header_str(msg.get("In-Reply-To"))
    parsed.in_reply_to = in_reply_to.strip() if in_reply_to else None
    references = _header_str(msg.get("References"))
    if references:
        parsed.references_ids = re.findall(r"<[^>]+>", references)

    # Takeout supplies X-GM-THRID but no per-message Gmail id — confirmed
    # against a real export, where X-GM-MSGID appears on no message at all.
    thread_id = _header_str(msg.get("X-GM-THRID"))
    parsed.thread_id = thread_id.strip() if thread_id else None
    gmail_id = _header_str(msg.get("X-GM-MSGID"))
    parsed.gmail_id = gmail_id.strip() if gmail_id else None

    parsed.labels = _labels(_header_str(msg.get("X-Gmail-Labels")), warnings)
    parsed.internal_date = _date(_header_str(msg.get("Date")), warnings)

    # `Date:` is the sender's claim; `Received:` is the receiving server's
    # record. Fall back only when the claim is absent or impossible, so the
    # substitution is narrow and always flagged (#27).
    if parsed.internal_date is None or any(
        w.code is Warn.DATE_IMPLAUSIBLE for w in warnings
    ):
        from_received = _received_date(msg, warnings)
        if from_received is not None:
            parsed.internal_date = from_received

    parsed.kept_headers = _kept_headers(msg)

    text_chunks: list[str] = []
    html_chunks: list[str] = []
    try:
        walker = list(msg.walk())
    except Exception as exc:
        warnings.append(ParseWarning(Warn.STRUCTURE_UNPARSEABLE, type(exc).__name__))
        walker = []

    for part in walker:
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        filename = part.get_filename()

        if is_attachment_part(part):
            try:
                blob = part.get_payload(decode=True)
            except Exception as exc:
                warnings.append(
                    ParseWarning(Warn.ATTACHMENT_UNDECODABLE, type(exc).__name__)
                )
                continue
            if not isinstance(blob, bytes):
                warnings.append(ParseWarning(Warn.ATTACHMENT_UNDECODABLE, "no-payload"))
                continue
            # Stored as declared, never trusted as a path or for serving.
            decoded_name = _decode_header(filename, warnings) if filename else None
            parsed.attachments.append(
                Attachment(
                    filename=decoded_name,
                    mime_type=content_type,
                    size=len(blob),
                    sha256=hashlib.sha256(blob).hexdigest(),
                )
            )
            continue

        if content_type == "text/plain":
            text_chunks.append(_part_text(part, warnings))
        elif content_type == "text/html":
            html_chunks.append(_part_text(part, warnings))

    parsed.body_text = "\n".join(c for c in text_chunks if c)
    parsed.body_html = "\n".join(c for c in html_chunks if c)
    searchable = "\n".join(c for c in (parsed.subject or "", parsed.body_text) if c)
    parsed.search_text = _bound_search_text(searchable, warnings)
    return parsed
