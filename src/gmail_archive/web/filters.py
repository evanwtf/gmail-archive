"""Jinja filters for the web UI.

Presentation only — nothing here is allowed to touch the database or the blob
store. Kept out of `app.py` so it can be tested without standing up FastAPI.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
