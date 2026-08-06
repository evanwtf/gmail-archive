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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from io import StringIO

# Well under the 1 MB tsvector hard limit. Halving it costs nothing — nobody
# searches for a term 500 KB into a message — and leaves room for the fact that
# a tsvector is not always smaller than its input.
SEARCH_TEXT_MAX_BYTES = 512_000

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
            warnings.append(
                ParseWarning(Warn.DATE_TZ_OUT_OF_RANGE, str(offset))
            )
            return None
    # A year outside this range is a broken header, not history. Kept rather
    # than discarded — the real export contains one — but flagged.
    if not (1970 <= parsed.year <= 2100):
        warnings.append(ParseWarning(Warn.DATE_IMPLAUSIBLE, str(parsed.year)))
    return parsed


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
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()

        if disposition == "attachment" or filename:
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
