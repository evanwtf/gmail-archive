"""Jinja filters for the web UI.

Presentation only — nothing here is allowed to touch the database or the blob
store. Kept out of `app.py` so it can be tested without standing up FastAPI.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from markupsafe import Markup, escape

#: Schemes that can make a browser fetch something, mapped to their defanged
#: form. `http` -> `hxxp` is the long-standing convention from malware
#: reporting, and the point is the same here: the URL stays readable, but no
#: parser — browser, mail client, terminal, or the reader's own reflexes —
#: treats it as something to fetch.
#:
#: `mailto:` and `data:` are deliberately absent. mailto fetches nothing, and
#: data: URIs are inline by definition, so defanging them would break embedded
#: images that never touch the network.
_DEFANGED_SCHEMES: dict[str, str] = {
    "http": "hxxp",
    "https": "hxxps",
    "ftp": "fxp",
    "ftps": "fxps",
    "ws": "wxs",
    "wss": "wxss",
    "file": "fxle",
}

_SCHEME_RE = re.compile(
    r"\b(" + "|".join(sorted(_DEFANGED_SCHEMES, key=len, reverse=True)) + r")://",
    re.IGNORECASE,
)

#: Protocol-relative URLs — `src="//tracker.example/pixel.gif"` — inherit the
#: page's scheme and fetch perfectly well, so they have to be caught too.
#: Anchored to an attribute so ordinary `//` in text and in code is left alone.
_PROTOCOL_RELATIVE_RE = re.compile(
    r"""(?i)\b(src|href|action|background|poster|srcset|data)(\s*=\s*["']?)//"""
)


def defang(value: str | None) -> str:
    """Rewrite URLs so nothing can be fetched from them.

    ``http://tracker.example/pixel.gif`` becomes
    ``hxxp://tracker.example/pixel.gif``: still legible, no longer a URL any
    browser will resolve. Applied to archived message content before it is
    rendered.

    This is defence in depth, not the only defence. The CSP already restricts
    loads to `'self'`, and the HTML body renders in a fully sandboxed iframe.
    Defanging survives someone loosening either of those, and it also stops a
    tracking pixel the moment the markup is copied out of the archive into
    something with no CSP at all.
    """
    if not value:
        return ""
    defanged = _SCHEME_RE.sub(
        lambda m: _DEFANGED_SCHEMES[m.group(1).lower()] + "://", value
    )
    return _PROTOCOL_RELATIVE_RE.sub(r"\1\2hxxp://", defanged)


def highlight_snippet(value: str | None) -> Markup:
    """Render a search snippet: defang, escape, then honour our own markers.

    The order is the whole point. `ts_headline` output is message text with
    `[hl]`/`[/hl]` inserted around the matched terms, so it must be escaped
    before anything in it becomes markup — the previous template piped it
    straight through `|safe`, and 274 messages in the reference archive carry
    `<script` or `onerror=` in their searchable text.

    Doing the substitution here rather than in the template is not a style
    preference: `escape()` returns `Markup`, and `Markup.replace` escapes its
    replacement argument, so `|escape|replace("[hl]", "<mark>")` in a template
    produces a literal `&lt;mark&gt;`.

    Caveat: a message body containing the literal text `[hl]` will render a
    spurious `<mark>`. That is cosmetic — the escaping above means it can only
    ever produce a `<mark>`, never arbitrary markup.
    """
    if not value:
        return Markup("")
    escaped = str(escape(defang(value)))
    return Markup(escaped.replace("[hl]", "<mark>").replace("[/hl]", "</mark>"))


#: Thresholds for `relative_date`, coarsest last. Each entry is
#: (upper bound in seconds, seconds per unit, unit name).
#:
#: The hour band runs to 48h rather than 24h on purpose: a message from
#: yesterday afternoon reads better as "32 hours ago" than as "1 day ago",
#: which is what the archive's owner asked for and is also more precise.
_BANDS: tuple[tuple[float, float, str], ...] = (
    (60, 1, "second"),
    (3600, 60, "minute"),
    (172_800, 3600, "hour"),  # 48 hours
    (5_184_000, 86_400, "day"),  # 60 days
    (63_072_000, 2_629_746, "month"),  # 24 months, mean Gregorian month
    (float("inf"), 31_556_952, "year"),  # mean Gregorian year
)


def filesize(value: int | float | None) -> str:
    """Bytes as a human-readable size: ``1.4 GB``, ``812 MB``, ``47 kB``.

    Decimal units, matching `pg_size_pretty`'s spirit if not its exact
    spelling, so a figure here can be compared with one from psql without
    mental arithmetic. Sizes below a kilobyte keep their exact byte count —
    "0.0 kB" tells you nothing.
    """
    if value is None:
        return "—"
    size = float(value)
    if size < 1000:
        return f"{int(size)} B"
    for unit in ("kB", "MB", "GB", "TB"):
        size /= 1000
        if size < 1000:
            # One decimal below 100, none above: "9.4 GB" but "412 MB".
            return f"{size:.1f} {unit}" if size < 100 else f"{size:.0f} {unit}"
    return f"{size:.1f} PB"


def gmail_date(value: datetime | None, now: datetime | None = None) -> str:
    """Format a date the way Gmail's message list does.

    Today shows a clock time, an earlier date this year shows month and day,
    and anything older shows a numeric date. The point is that the common case
    — recent mail — reads at a glance without the year taking up space.

    ``11:42 AM``  ·  ``Mar 4``  ·  ``3/4/09``
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    local = value.astimezone(reference.tzinfo)
    if (local.year, local.month, local.day) == (
        reference.year,
        reference.month,
        reference.day,
    ):
        # %-I strips the leading zero, as Gmail does. Linux/glibc only, which
        # is what this ships on.
        return local.strftime("%-I:%M %p")
    if local.year == reference.year:
        return local.strftime("%b %-d")
    return local.strftime("%-m/%-d/%y")


def sender_name(addr: str | None) -> str:
    """Reduce an address to what Gmail shows in the sender column.

    Gmail shows a display name when it has one. This archive stores only the
    address part, so derive something readable from the local part:
    ``order-update@amazon.com`` -> ``order-update``. The full address is still
    available as a tooltip and on the message page.
    """
    if not addr:
        return "(unknown sender)"
    local, _, _domain = addr.partition("@")
    if not local:
        return addr
    # Separators are conventional in machine-generated addresses; a human
    # reading "order update" beats reading "order-update".
    cleaned = local.replace(".", " ").replace("-", " ").replace("_", " ").strip()
    return cleaned.title() if cleaned else addr


def relative_date(value: datetime | None, now: datetime | None = None) -> str:
    """Render a datetime as an age: ``32 hours ago``, ``3 years ago``.

    Returns an empty string for `None`, so a template can call this on the
    ~2.7% of messages with no parseable `Date` without a conditional.

    Future timestamps render as ``in 2 hours`` rather than being clamped. The
    archive genuinely contains them — a `Date` header is whatever the sending
    client claimed, and the parser keeps implausible years rather than
    discarding them — so "0 seconds ago" would be a quiet lie about the data.

    A naive datetime is read as UTC. Everything from Postgres is `timestamptz`
    and therefore aware; this only guards a caller passing something else.
    """
    if value is None:
        return ""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    delta = (reference - value).total_seconds()
    future = delta < 0
    delta = abs(delta)

    if delta < 1:
        return "just now"

    for bound, per_unit, unit in _BANDS:
        if delta < bound:
            count = int(delta // per_unit)
            # Rounding down can land on zero at a band's floor (e.g. 59.9s in
            # the minute band); one unit is the honest floor, not zero.
            count = max(count, 1)
            plural = "" if count == 1 else "s"
            return (
                f"in {count} {unit}{plural}"
                if future
                else (f"{count} {unit}{plural} ago")
            )

    raise AssertionError("unreachable: the last band is unbounded")
