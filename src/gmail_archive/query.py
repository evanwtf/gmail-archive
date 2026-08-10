"""Read-only query surface for the archive.

This module is the **only** place allowed to build read SQL against the `messages`
table. The CLI, the web UI, and any future IMAP SEARCH all go through here. A
test greps for stray SQL against an explicit allowlist and fails.

The keyset pagination ordering must match the `messages_keyset_idx` index exactly:
`(internal_date desc nulls last, raw_sha256 desc)`. A mismatch means a sequential
scan over the whole archive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import psycopg

from gmail_archive.searchquery import ParsedQuery
from gmail_archive.searchquery import parse as parse_search

logger = logging.getLogger(__name__)

#: Gmail's own labels, as they arrive in a Takeout export. The UI renders these
#: as mailboxes and icons rather than as chips, so they are filtered out of a
#: row's user labels.
SYSTEM_LABELS: frozenset[str] = frozenset(
    {
        "Inbox",
        "Sent",
        "Drafts",
        "Spam",
        "Trash",
        "Chat",
        "Starred",
        "Important",
        "Unread",
        "Opened",
        "Archived",
        "Category",
    }
)

#: The left rail, in Gmail's order. (label, display name, icon). `None` as the
#: label means "everything" — Gmail's All Mail.
MAILBOXES: tuple[tuple[str | None, str, str], ...] = (
    ("Inbox", "Inbox", "inbox"),
    ("Starred", "Starred", "star"),
    ("Important", "Important", "important"),
    ("Sent", "Sent", "sent"),
    ("Drafts", "Drafts", "draft"),
    ("Chat", "Chat", "chat"),
    ("Spam", "Spam", "spam"),
    ("Archived", "Archived", "archive"),
    (None, "All Mail", "all"),
)

#: Gmail's inbox tabs, mapped to the `Category *` labels Takeout writes.
CATEGORY_TABS: tuple[tuple[str | None, str], ...] = (
    (None, "Primary"),
    ("Category Social", "Social"),
    ("Category Promotions", "Promotions"),
    ("Category Updates", "Updates"),
    ("Category Purchases", "Purchases"),
)

#: Trimmed body text shown under the subject on a list row.
#:
#: `left()` before `regexp_replace`, not after. Collapsing whitespace across the
#: whole column first means de-TOASTing and scanning every byte of a multi-
#: megabyte HTML body to produce 220 characters — measured at 190ms for a page
#: of 50 rows against 10ms with the truncation first. 600 characters is a
#: generous margin for whitespace collapsing to eat into.
_SNIPPET_SQL = "regexp_replace(left(coalesce(m.body_text, ''), 600), '\\s+', ' ', 'g')"

#: Labels for one row, as a json array. Lateral rather than a join so a message
#: with twelve labels still produces exactly one row.
_LABELS_SQL = (
    "coalesce((select json_agg(l2.label order by l2.label) from labels l2"
    " where l2.raw_sha256 = m.raw_sha256), '[]'::json)"
)


def _row(row: object) -> tuple[Any, ...]:
    """Cast a psycopg Row to a tuple for indexing.

    psycopg's type stubs return `object` from fetchone/fetchall, which mypy
    refuses to index. This cast is safe because psycopg rows always support
    tuple-like access.
    """
    return tuple(row)  # type: ignore[arg-type]


@dataclass
class ArchiveStats:
    """Aggregate statistics about the archive."""

    total_messages: int
    total_blobs: int
    total_attachments: int
    total_labels: int
    total_failures: int
    total_runs: int
    total_bytes: int
    date_earliest: datetime | None
    date_latest: datetime | None
    blob_bytes: int


@dataclass
class MessageRow:
    """One row from a message listing or search result."""

    raw_sha256: str
    subject: str | None
    from_addr: str | None
    to_addrs: list[str]
    internal_date: datetime | None
    thread_id: str | None
    snippet: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def is_unread(self) -> bool:
        """Gmail's own Unread label, carried through Takeout."""
        return "Unread" in self.labels

    @property
    def is_starred(self) -> bool:
        return "Starred" in self.labels

    @property
    def is_important(self) -> bool:
        return "Important" in self.labels

    @property
    def user_labels(self) -> list[str]:
        """Labels worth showing as chips on a row.

        Excludes the system labels the UI already renders as icons or as the
        current mailbox, and the `Category *` labels that drive the inbox tabs
        — a row in Promotions does not need a chip saying "Promotions".
        """
        return [
            label
            for label in self.labels
            if label not in SYSTEM_LABELS and not label.startswith("Category")
        ]


@dataclass
class MessageFull:
    """Full message data including bodies, labels, and attachments."""

    raw_sha256: str
    size_bytes: int
    message_id: str | None
    thread_id: str | None
    subject: str | None
    from_addr: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
    reply_to: str | None
    in_reply_to: str | None
    references_ids: list[str]
    internal_date: datetime | None
    body_text: str | None
    labels: list[str]
    parse_warnings: list[dict[str, str]]
    #: One entry per attachment: part_index, filename, mime_type, size_bytes.
    #: The bytes are not stored separately — they live in the raw message in
    #: the blob store and are re-extracted on demand.
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SearchResult:
    """Result of a full-text search."""

    messages: list[MessageRow]
    total: int
    query: str
    #: How the query string was understood, so the UI can echo the filters
    #: back and report any operator it had to reject.
    parsed: ParsedQuery = field(default_factory=ParsedQuery)


@dataclass
class LabelCount:
    """A label and how many messages carry it."""

    label: str
    message_count: int


@dataclass
class IngestRun:
    """One `ingest` invocation, with what it read and what it produced."""

    id: int
    source_path: str
    started_at: datetime
    finished_at: datetime | None
    checkpoint_offset: int
    messages_seen: int
    messages_new: int
    failures: int
    status: str
    account_address: str | None
    #: Sightings recorded against this run's source file. Not the same as
    #: `messages_seen`, which counts what this run walked past: a resumed
    #: import has several runs over one file, and the file's sighting count is
    #: the total across all of them. Null when the source has no sightings —
    #: a run that failed before writing anything.
    source_sightings: int | None
    #: Oldest and newest message in the source file. The newest is the closest
    #: thing to a timestamp on the export itself: a Takeout dump contains mail
    #: up to the moment it was generated, and nothing records that moment
    #: anywhere else. Both use the same plausibility window as `stats()`, so a
    #: message dated 2611 does not become the export's date.
    oldest_message: datetime | None
    newest_message: datetime | None

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock time, or None while the run is still going."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


def ingest_runs(conn: psycopg.Connection[object]) -> list[IngestRun]:
    """Every import ever run against this database, newest first.

    Two queries rather than one. The per-source message-date bounds need a
    join across all 277k sightings and takes about 700ms on the reference
    archive; folding it in as a correlated subquery would pay that once per
    run. Grouped separately and merged here, it is paid once regardless — and
    the bounds are a property of the *file*, not of the run, so several
    resumed runs over one export correctly report the same range.
    """
    by_source: dict[str, tuple[int, datetime | None, datetime | None]] = {}
    for raw in conn.execute(
        "select s.source_path, count(*),"
        "  min(m.internal_date) filter (where m.internal_date >= '1970-01-01'"
        "    and m.internal_date <= now() + interval '90 days'),"
        "  max(m.internal_date) filter (where m.internal_date >= '1970-01-01'"
        "    and m.internal_date <= now() + interval '90 days')"
        " from message_sightings s"
        " join messages m on m.raw_sha256 = s.raw_sha256"
        " group by s.source_path"
    ).fetchall():
        r = _row(raw)
        by_source[str(r[0])] = (int(r[1]), r[2], r[3])

    runs = []
    for raw in conn.execute(
        "select r.id, r.source_path, r.started_at, r.finished_at,"
        "  r.checkpoint_offset, r.messages_seen, r.messages_new, r.failures,"
        "  r.status, a.address"
        " from ingest_runs r"
        " left join accounts a on a.id = r.account_id"
        " order by r.started_at desc, r.id desc"
    ).fetchall():
        r = _row(raw)
        sightings, oldest, newest = by_source.get(str(r[1]), (0, None, None))
        runs.append(
            IngestRun(
                id=int(r[0]),
                source_path=str(r[1]),
                started_at=r[2],
                finished_at=r[3],
                checkpoint_offset=int(r[4]),
                messages_seen=int(r[5]),
                messages_new=int(r[6]),
                failures=int(r[7]),
                status=str(r[8]),
                account_address=r[9],
                source_sightings=sightings or None,
                oldest_message=oldest,
                newest_message=newest,
            )
        )
    return runs


def last_import_finished(conn: psycopg.Connection[object]) -> datetime | None:
    """When the most recent *completed* import finished, for the chrome badge.

    Deliberately narrower than `max(finished_at)`: an interrupted or failed
    run has a `finished_at` too, and dating the archive from one would claim
    freshness the archive does not have. Returns None until something has
    finished cleanly — including on a database whose data predates run
    bookkeeping — and the badge is then omitted rather than guessed at.

    Called from `_chrome`, so it runs on every page render. `ingest_runs` has
    one row per import; this is a scan of a table that will never be large.
    """
    raw = conn.execute(
        "select max(finished_at) from ingest_runs where status = 'complete'"
    ).fetchone()
    return None if raw is None else _row(raw)[0]


def stats(conn: psycopg.Connection[object]) -> ArchiveStats:
    """Return aggregate statistics about the archive."""
    raw = conn.execute(
        "select"
        "  (select count(*) from messages) as total_messages,"
        "  (select count(*) from blobs) as total_blobs,"
        "  (select count(*) from attachments) as total_attachments,"
        "  (select count(*) from labels) as total_labels,"
        "  (select count(*) from failed_messages) as total_failures,"
        "  (select count(*) from ingest_runs) as total_runs,"
        "  (select coalesce(sum(size_bytes), 0) from messages) as total_bytes,"
        "  (select min(internal_date) from messages"
        "    where internal_date >= '1970-01-01'"
        "    and internal_date <= now() + interval '90 days') as date_earliest,"
        "  (select max(internal_date) from messages"
        "    where internal_date >= '1970-01-01'"
        "    and internal_date <= now() + interval '90 days') as date_latest,"
        "  (select coalesce(sum(size_bytes), 0) from blobs) as blob_bytes"
    ).fetchone()
    assert raw is not None
    row = _row(raw)
    return ArchiveStats(
        total_messages=int(row[0]),
        total_blobs=int(row[1]),
        total_attachments=int(row[2]),
        total_labels=int(row[3]),
        total_failures=int(row[4]),
        total_runs=int(row[5]),
        total_bytes=int(row[6]),
        date_earliest=row[7],
        date_latest=row[8],
        blob_bytes=int(row[9]),
    )


#: Orderings `search()` will accept, mapped to SQL. A lookup, not interpolation:
#: the caller's string selects a key and never reaches the query text.
#:
#: `date` matches `messages_keyset_idx` exactly, so it sorts from the index
#: rather than the rank expression. Both orderings break ties on `raw_sha256`
#: so a page boundary is stable across requests.
#: Sorts a date the sender cannot have meant after the real ones.
#:
#: A `Date` header is whatever the sending client claimed, and the parser
#: keeps implausible values rather than discarding them — correctly, since the
#: header is a fact about the message. But a message dated 2611 is not the
#: newest mail in the archive, and newest-first put it above everything (#27).
#:
#: Demoted rather than hidden, and demoted the same way NULLs already are, so
#: the row is still reachable at the end of the walk.
def _like(value: str) -> str:
    """A substring LIKE pattern that matches `value` literally (#43).

    `%` and `_` are LIKE metacharacters, and neither the parser nor psycopg
    treats them as special — parameterisation prevents injection, not
    over-matching. So `from:%` became `ilike '%%%'` and matched every message
    in the archive, and `from:first_last` quietly also matched `firstXlast`.
    The underscore case is the one that actually bites: addresses are full of
    them, and the result is silently too broad rather than an error.

    Backslash is escaped first. Doing it after would double-escape the
    backslashes the other two replacements just introduced.

    Callers must pair this with `escape '\\'`, because Postgres's default
    LIKE escape character is already backslash but `standard_conforming_strings`
    makes that dependent on how the literal was written. Declaring it removes
    the doubt.
    """
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_IMPLAUSIBLE_LAST = "(internal_date > now() + interval '90 days') asc"

#: The same expression, qualified, for queries that alias `messages` as `m`.
_IMPLAUSIBLE_LAST_M = "(m.internal_date > now() + interval '90 days') asc"

SEARCH_SORTS: dict[str, str] = {
    "relevance": (
        "ts_rank(search_tsv, websearch_to_tsquery('english', %(q)s)) desc,"
        f" {_IMPLAUSIBLE_LAST},"
        " internal_date desc nulls last, raw_sha256 desc"
    ),
    "date": f"{_IMPLAUSIBLE_LAST}, internal_date desc nulls last, raw_sha256 desc",
    "date-asc": "internal_date asc nulls last, raw_sha256 asc",
}

#: Newest first, not relevance. For a personal archive the common query is a
#: sender or a domain, where every hit is equally "relevant" and ts_rank
#: effectively returns an arbitrary order — so the useful default is recency.
DEFAULT_SEARCH_SORT = "date"


def search(
    conn: psycopg.Connection[object],
    query: str,
    *,
    limit: int = 50,
    offset: int = 0,
    sort: str = DEFAULT_SEARCH_SORT,
) -> SearchResult:
    """Full-text search over messages using `websearch_to_tsquery`.

    Returns messages with highlighted snippets, ordered by `sort` — one of the
    keys in `SEARCH_SORTS`, defaulting to newest first. An unknown key raises
    `ValueError` rather than quietly falling back, so a typo in a caller is
    loud.

    Undated messages sort last under every ordering, including `date-asc`:
    a missing `Date` is unknown, not old, and putting nulls first would open
    every ascending search with the messages that have the least information.
    """
    if sort not in SEARCH_SORTS:
        raise ValueError(
            f"unknown sort {sort!r}; expected one of {sorted(SEARCH_SORTS)}"
        )

    parsed = parse_search(query)
    if parsed.is_empty:
        return SearchResult(messages=[], total=0, query=query, parsed=parsed)

    conditions: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    # Free text goes to the GIN index. A query made only of operators skips it
    # entirely — `from:alice` is a perfectly good search with no text in it.
    if parsed.text:
        conditions.append("m.search_tsv @@ websearch_to_tsquery('english', %(q)s)")
        params["q"] = parsed.text

    # Address and subject matching is substring, case-insensitive: people
    # remember "amazon" or a first name, not a full RFC 5322 address.
    #
    # `_like` escapes LIKE metacharacters. Without it `from:first_last` also
    # matches `firstXlast`, and `from:%` matches the entire archive (#43).
    for i, value in enumerate(parsed.from_addrs):
        conditions.append(f"m.from_addr ilike %(from{i})s escape '\\'")
        params[f"from{i}"] = _like(value)
    for i, value in enumerate(parsed.to_addrs):
        # array_to_string so one pattern can match any recipient.
        conditions.append(
            f"array_to_string(m.to_addrs, ' ') ilike %(to{i})s escape '\\'"
        )
        params[f"to{i}"] = _like(value)
    for i, value in enumerate(parsed.subjects):
        conditions.append(f"m.subject ilike %(subj{i})s escape '\\'")
        params[f"subj{i}"] = _like(value)

    # Labels match exactly — they are a controlled vocabulary, and `label:Bank`
    # matching "Bank Alerts" would be a surprise.
    for i, value in enumerate(parsed.labels):
        conditions.append(
            f"exists (select 1 from labels l where l.raw_sha256 = m.raw_sha256"
            f" and l.label = %(label{i})s)"
        )
        params[f"label{i}"] = value

    if parsed.before is not None:
        conditions.append("m.internal_date < %(before)s::timestamptz")
        params["before"] = parsed.before
    if parsed.after is not None:
        # Inclusive: `after:2020-01-01` should include that day's mail.
        conditions.append("m.internal_date >= %(after)s::timestamptz")
        params["after"] = parsed.after
    if parsed.on is not None:
        conditions.append(
            "m.internal_date >= %(on)s::timestamptz"
            " and m.internal_date < %(on)s::timestamptz + interval '1 day'"
        )
        params["on"] = parsed.on

    if parsed.has_attachment:
        conditions.append(
            "exists (select 1 from attachments a where a.raw_sha256 = m.raw_sha256)"
        )

    where = " where " + " and ".join(conditions)

    # Relevance ranks against the free-text query; with no free text there is
    # nothing to rank, and the clause would reference a parameter that is not
    # in `params`. Fall back to the default ordering.
    order_by = SEARCH_SORTS[sort]
    if "%(q)s" in order_by and not parsed.text:
        order_by = SEARCH_SORTS[DEFAULT_SEARCH_SORT]

    raw_count = conn.execute(
        f"select count(*) from messages m{where}", params
    ).fetchone()
    assert raw_count is not None
    total = int(_row(raw_count)[0])

    if total == 0:
        return SearchResult(messages=[], total=0, query=query, parsed=parsed)

    # ts_headline needs a tsquery; with no free text there is nothing to
    # highlight, so fall back to the same plain snippet the mailbox list uses.
    snippet_sql = (
        "ts_headline("
        "  'english',"
        "  coalesce(m.subject, '') || ' ' || coalesce(m.search_text, ''),"
        "  websearch_to_tsquery('english', %(q)s),"
        "  'MaxWords=40, MinWords=20, StartSel=[hl], StopSel=[/hl]'"
        ")"
        if parsed.text
        else _SNIPPET_SQL
    )

    raw_rows = conn.execute(
        "select"
        "  m.raw_sha256,"
        "  m.subject,"
        "  m.from_addr,"
        "  m.to_addrs,"
        "  m.internal_date,"
        "  m.thread_id,"
        f" {snippet_sql} as snippet,"
        f" {_LABELS_SQL} as labels"
        " from messages m"
        f"{where}"
        f" order by {order_by}"
        " limit %(limit)s offset %(offset)s",
        params,
    ).fetchall()

    messages = [_message_row(r) for r in (_row(rr) for rr in raw_rows)]

    return SearchResult(messages=messages, total=total, query=query, parsed=parsed)


def list_messages(
    conn: psycopg.Connection[object],
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[MessageRow]:
    """List messages in keyset order, newest first.

    The ordering matches `messages_keyset_idx` exactly so the query planner
    can use the index rather than a sequential scan.
    """
    raw_rows = conn.execute(
        "select"
        "  raw_sha256, subject, from_addr, to_addrs, internal_date, thread_id"
        " from messages"
        " order by internal_date desc nulls last, raw_sha256 desc"
        " limit %s offset %s",
        (limit, offset),
    ).fetchall()

    return [
        MessageRow(
            raw_sha256=str(r[0]),
            subject=r[1],
            from_addr=r[2],
            to_addrs=list(r[3]) if r[3] else [],
            internal_date=r[4],
            thread_id=r[5],
        )
        for r in (_row(rr) for rr in raw_rows)
    ]


def get_message(
    conn: psycopg.Connection[object],
    raw_sha256: str,
) -> MessageRow | None:
    """Fetch a single message by its content hash."""
    raw = conn.execute(
        "select"
        "  raw_sha256, subject, from_addr, to_addrs, internal_date, thread_id"
        " from messages where raw_sha256 = %s",
        (raw_sha256,),
    ).fetchone()
    if raw is None:
        return None
    row = _row(raw)
    return MessageRow(
        raw_sha256=str(row[0]),
        subject=row[1],
        from_addr=row[2],
        to_addrs=list(row[3]) if row[3] else [],
        internal_date=row[4],
        thread_id=row[5],
    )


def get_message_full(
    conn: psycopg.Connection[object],
    raw_sha256: str,
) -> MessageFull | None:
    """Fetch a single message with all metadata, bodies, and labels.

    Used by the export command to reconstitute messages.
    """
    raw = conn.execute(
        "select"
        "  m.raw_sha256, m.size_bytes, m.message_id, m.thread_id,"
        "  m.subject, m.from_addr, m.to_addrs, m.cc_addrs, m.bcc_addrs,"
        "  m.reply_to, m.in_reply_to, m.references_ids, m.internal_date,"
        "  m.body_text, m.parse_warnings,"
        "  coalesce((select json_agg(l.label) from labels l"
        "    where l.raw_sha256 = m.raw_sha256), '[]'::json) as labels,"
        "  coalesce((select json_agg(json_build_object("
        "      'part_index', a.part_index, 'filename', a.filename,"
        "      'mime_type', a.mime_type, 'size_bytes', a.size_bytes)"
        "    order by a.part_index) from attachments a"
        "    where a.raw_sha256 = m.raw_sha256), '[]'::json) as attachments"
        " from messages m"
        " where m.raw_sha256 = %s",
        (raw_sha256,),
    ).fetchone()
    if raw is None:
        return None
    row = _row(raw)
    return MessageFull(
        raw_sha256=str(row[0]),
        size_bytes=int(row[1]),
        message_id=row[2],
        thread_id=row[3],
        subject=row[4],
        from_addr=row[5],
        to_addrs=list(row[6]) if row[6] else [],
        cc_addrs=list(row[7]) if row[7] else [],
        bcc_addrs=list(row[8]) if row[8] else [],
        reply_to=row[9],
        in_reply_to=row[10],
        references_ids=list(row[11]) if row[11] else [],
        internal_date=row[12],
        body_text=row[13],
        labels=list(row[15]) if row[15] else [],
        parse_warnings=list(row[14]) if row[14] else [],
        attachments=list(row[16]) if row[16] else [],
    )


def list_labels(
    conn: psycopg.Connection[object],
) -> list[LabelCount]:
    """List all labels with their message counts, ordered by count descending."""
    raw_rows = conn.execute(
        "select label, count(*) as cnt"
        " from labels"
        " group by label"
        " order by cnt desc, label"
    ).fetchall()
    return [
        LabelCount(label=str(r[0]), message_count=int(r[1]))
        for r in (_row(rr) for rr in raw_rows)
    ]


def list_messages_keyset(
    conn: psycopg.Connection[object],
    *,
    after_date: datetime | None = None,
    after_sha: str | None = None,
    limit: int = 50,
    label: str | None = None,
    exclude_labels: tuple[str, ...] = (),
    on_day: date | None = None,
) -> list[MessageRow]:
    """List messages using keyset pagination.

    The ordering matches `messages_keyset_idx` exactly:
    ``(internal_date desc nulls last, raw_sha256 desc)``.

    Pass the ``internal_date`` and ``raw_sha256`` of the last message from the
    previous page as ``after_date`` and ``after_sha`` to get the next page.
    Pass ``None`` for both to get the first page.

    Messages with NULL ``internal_date`` sort last (oldest first among
    themselves, by sha256), so a keyset walk that starts from a real date
    never reaches them. Use ``after_date=NULL, after_sha=<last_sha>`` to
    page through the NULL tail.

    If ``label`` is provided, only messages carrying that label are returned.
    If ``exclude_labels`` is provided, messages carrying any of them are
    omitted — this is how Gmail's Primary tab is expressed: everything that is
    not in one of the other category tabs.

    ``on_day`` restricts the result to a single calendar day, **in UTC**. The
    archive stores `timestamptz` and the server has no notion of the reader's
    timezone, so a message sent at 23:30 local time may land on the next day
    here. For a twenty-year archive that is the honest boundary to pick; the
    alternative is guessing an offset and being wrong twice a year.
    """
    conditions: list[str] = []
    params: list[object] = []

    if on_day is not None:
        # Half-open interval against the index, not date(internal_date) = %s,
        # which would be a function on the column and could not use
        # messages_keyset_idx.
        conditions.append(
            "m.internal_date >= %s::timestamptz"
            " and m.internal_date < %s::timestamptz + interval '1 day'"
        )
        params += [on_day, on_day]

    # Keyset predicate. A row comparison against a NULL internal_date is NULL,
    # so dated and undated pages need different predicates; see the note above.
    if after_date is not None and after_sha is not None:
        conditions.append("(m.internal_date, m.raw_sha256) < (%s::timestamptz, %s)")
        params += [after_date, after_sha]
    elif after_sha is not None:
        conditions.append("m.internal_date is null and m.raw_sha256 < %s")
        params.append(after_sha)

    if label:
        conditions.append(
            "exists (select 1 from labels l"
            " where l.raw_sha256 = m.raw_sha256 and l.label = %s)"
        )
        params.append(label)

    for excluded in exclude_labels:
        conditions.append(
            "not exists (select 1 from labels x"
            " where x.raw_sha256 = m.raw_sha256 and x.label = %s)"
        )
        params.append(excluded)

    where = (" where " + " and ".join(conditions)) if conditions else ""

    raw_rows = conn.execute(
        "select"
        "  m.raw_sha256, m.subject, m.from_addr, m.to_addrs,"
        "  m.internal_date, m.thread_id,"
        f" {_SNIPPET_SQL} as snippet,"
        f" {_LABELS_SQL} as labels"
        " from messages m"
        f"{where}"
        # Implausible dates last, for the reason at _IMPLAUSIBLE_LAST. The
        # keyset predicate is unchanged, so paging still works: this only
        # moves the handful of broken rows to the end of the walk.
        f" order by {_IMPLAUSIBLE_LAST_M},"
        " m.internal_date desc nulls last, m.raw_sha256 desc"
        " limit %s",
        (*params, limit),
    ).fetchall()

    return [_message_row(r) for r in (_row(rr) for rr in raw_rows)]


def _message_row(r: tuple[Any, ...]) -> MessageRow:
    """Build a MessageRow from the standard listing column order."""
    return MessageRow(
        raw_sha256=str(r[0]),
        subject=r[1],
        from_addr=r[2],
        to_addrs=list(r[3]) if r[3] else [],
        internal_date=r[4],
        thread_id=r[5],
        snippet=(r[6] or "") if len(r) > 6 else "",
        labels=list(r[7]) if len(r) > 7 and r[7] else [],
    )


def get_thread_messages(
    conn: psycopg.Connection[object],
    thread_id: str,
) -> list[MessageRow]:
    """Fetch all messages in a thread, ordered by internal_date."""
    raw_rows = conn.execute(
        "select"
        "  m.raw_sha256, m.subject, m.from_addr, m.to_addrs,"
        "  m.internal_date, m.thread_id,"
        f" {_SNIPPET_SQL} as snippet,"
        f" {_LABELS_SQL} as labels"
        " from messages m"
        " where m.thread_id = %s"
        " order by m.internal_date desc nulls last, m.raw_sha256 desc",
        (thread_id,),
    ).fetchall()
    return [_message_row(r) for r in (_row(rr) for rr in raw_rows)]


@dataclass
class RelationSize:
    """On-disk size of one table, broken down by where the bytes live."""

    name: str
    #: Planner estimate from pg_stat_user_tables, not a count(*). Exact counts
    #: over ten tables cost about half a second; this costs nothing and is
    #: close enough for a size report. Labelled as an estimate in the UI.
    est_rows: int
    heap_bytes: int
    toast_bytes: int
    index_bytes: int
    total_bytes: int


@dataclass
class DatabaseStats:
    """Postgres-side storage accounting."""

    server_version: str
    database_name: str
    database_bytes: int
    relations: list[RelationSize]


def database_stats(conn: psycopg.Connection[object]) -> DatabaseStats:
    """Where the database's disk is actually going.

    The interesting number for this project is the ratio between the database
    and the blob store: ADR-001 puts raw message bytes on disk specifically so
    a `pg_dump` stays small enough to take regularly, and this is the page
    that shows whether that is still true.
    """
    raw = conn.execute(
        "select current_setting('server_version'), current_database(),"
        " pg_database_size(current_database())"
    ).fetchone()
    assert raw is not None
    version_row = _row(raw)

    raw_rows = conn.execute(
        "select c.relname,"
        "  coalesce(s.n_live_tup, 0),"
        "  pg_relation_size(c.oid),"
        # Total minus heap minus indexes is the TOAST side: the out-of-line
        # storage for large values, which for this schema is almost entirely
        # message bodies.
        "  pg_total_relation_size(c.oid)"
        "    - pg_relation_size(c.oid) - pg_indexes_size(c.oid),"
        "  pg_indexes_size(c.oid),"
        "  pg_total_relation_size(c.oid)"
        " from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " left join pg_stat_user_tables s on s.relid = c.oid"
        " where n.nspname = 'public' and c.relkind = 'r'"
        " order by pg_total_relation_size(c.oid) desc"
    ).fetchall()

    relations = [
        RelationSize(
            name=str(r[0]),
            est_rows=int(r[1]),
            heap_bytes=int(r[2]),
            toast_bytes=int(r[3]),
            index_bytes=int(r[4]),
            total_bytes=int(r[5]),
        )
        for r in (_row(rr) for rr in raw_rows)
    ]

    return DatabaseStats(
        server_version=str(version_row[0]),
        database_name=str(version_row[1]),
        database_bytes=int(version_row[2]),
        relations=relations,
    )


def date_bounds(conn: psycopg.Connection[object]) -> tuple[date | None, date | None]:
    """Earliest and latest plausible message dates, for the calendar's range.

    Uses the same plausibility window as `stats()`: a `Date` header of 2611
    is a broken header, not the end of the archive, and it should not stretch
    a date picker across six centuries. Both bounds come from
    `messages_keyset_idx`, so this is two index probes.
    """
    raw = conn.execute(
        "select min(internal_date), max(internal_date) from messages"
        " where internal_date >= '1970-01-01'"
        " and internal_date <= now() + interval '90 days'"
    ).fetchone()
    if raw is None:
        return None, None
    row = _row(raw)
    lo: datetime | None = row[0]
    hi: datetime | None = row[1]
    return (lo.date() if lo else None, hi.date() if hi else None)


def label_counts(conn: psycopg.Connection[object]) -> dict[str, int]:
    """Every label with its message count, as a dict.

    One grouped scan serves both halves of the chrome — the rail's mailbox
    counts and the user-label list. Fetching them separately meant two scans of
    a million-row table on every page render; the group-by costs the same
    whether you want nine labels or all of them, so do it once.
    """
    raw_rows = conn.execute(
        "select label, count(*) from labels group by label"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in (_row(rr) for rr in raw_rows)}
