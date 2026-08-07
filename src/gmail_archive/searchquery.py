"""Gmail-style search operators, parsed out of a query string.

`from:evan@example.com before:2026-01-01 invoice` becomes a set of structured
filters plus the leftover free text, which still goes to
`websearch_to_tsquery` so phrases and `-exclusions` keep working.

Why this exists rather than just documenting what Postgres gives us: the
full-text index covers `subject` and `body_text` only. Sender and recipient
addresses are not in it, so before these operators there was no way at all to
ask "mail from this person" — not even by typing the address, which finds only
the messages that happen to quote it in the body.

Everything here is a *parser*. It produces values; it never builds SQL. The
SQL lives in `query.py`, which is the only module allowed to write it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

#: `name:value`, where value is a quoted string or a run of non-space. The
#: name list is closed: an unrecognised `foo:bar` is left in the free text,
#: because it is far more likely to be a URL, a time, or a Message-ID than a
#: typo'd operator, and silently dropping it would lose the search term.
_OPERATOR_RE = re.compile(
    r"""\b(?P<name>from|to|subject|label|before|after|on|is|has)"""
    r""":(?P<value>"[^"]*"|\S+)""",
    re.IGNORECASE,
)

#: `is:` values that map to a Gmail label carried through Takeout.
IS_LABELS: dict[str, str] = {
    "unread": "Unread",
    "read": "Opened",
    "starred": "Starred",
    "important": "Important",
    "sent": "Sent",
    "draft": "Drafts",
    "spam": "Spam",
    "chat": "Chat",
}


@dataclass(frozen=True)
class ParsedQuery:
    """A search string split into structured filters and leftover text."""

    text: str = ""
    from_addrs: tuple[str, ...] = ()
    to_addrs: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    before: date | None = None
    after: date | None = None
    on: date | None = None
    has_attachment: bool = False
    #: Operators that were recognised but whose value made no sense, e.g.
    #: `before:tuesday`. Surfaced so the UI can say so rather than silently
    #: returning the wrong messages.
    rejected: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_filters(self) -> bool:
        return bool(
            self.from_addrs
            or self.to_addrs
            or self.subjects
            or self.labels
            or self.before
            or self.after
            or self.on
            or self.has_attachment
        )

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to search for at all."""
        return not self.text.strip() and not self.has_filters


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse(query: str) -> ParsedQuery:
    """Split a query into operators and free text.

    Unknown operators are deliberately left in the free text. Values may be
    quoted (`from:"john smith"`), and an operator with an unusable value —
    `before:tuesday` — is dropped and reported in `rejected` rather than
    guessed at.
    """
    if not query or not query.strip():
        return ParsedQuery()

    from_addrs: list[str] = []
    to_addrs: list[str] = []
    subjects: list[str] = []
    labels: list[str] = []
    rejected: list[str] = []
    before: date | None = None
    after: date | None = None
    on: date | None = None
    has_attachment = False

    def take(match: re.Match[str]) -> str:
        nonlocal before, after, on, has_attachment
        name = match.group("name").lower()
        value = _unquote(match.group("value")).strip()
        if not value:
            return ""

        if name == "from":
            from_addrs.append(value)
        elif name == "to":
            to_addrs.append(value)
        elif name == "subject":
            subjects.append(value)
        elif name == "label":
            labels.append(value)
        elif name in ("before", "after", "on"):
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                rejected.append(f"{name}:{value}")
                return ""
            if name == "before":
                before = parsed_date
            elif name == "after":
                after = parsed_date
            else:
                on = parsed_date
        elif name == "is":
            mapped = IS_LABELS.get(value.lower())
            if mapped is None:
                rejected.append(f"is:{value}")
                return ""
            labels.append(mapped)
        elif name == "has":
            if value.lower() in ("attachment", "attachments", "file"):
                has_attachment = True
            else:
                rejected.append(f"has:{value}")
        return ""

    text = _OPERATOR_RE.sub(take, query)

    return ParsedQuery(
        text=" ".join(text.split()),
        from_addrs=tuple(from_addrs),
        to_addrs=tuple(to_addrs),
        subjects=tuple(subjects),
        labels=tuple(labels),
        before=before,
        after=after,
        on=on,
        has_attachment=has_attachment,
        rejected=tuple(rejected),
    )
