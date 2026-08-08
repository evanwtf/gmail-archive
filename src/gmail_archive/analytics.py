"""Aggregate analysis over the archive: who you talk to, and how that changed.

`query.py` retrieves messages. This module answers questions *about* the
corpus, which is a different shape of SQL — grouped, corpus-wide, and slow
enough that some of it is precomputed rather than run per request.

The central problem is that most of a modern mail archive is not
correspondence. On the reference corpus, 69% of messages carry a Gmail bulk
category and 33% come from a no-reply-style address, so any ranking of
"top senders" without a filter is a ranking of marketing departments.
`rebuild_sender_profiles` decides human-or-bulk once per sender; everything
else here leans on that verdict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

logger = logging.getLogger(__name__)

#: Gmail's own bulk classifications, as they arrive in a Takeout export.
BULK_CATEGORIES = (
    "Category Promotions",
    "Category Updates",
    "Category Social",
    "Category Forums",
)

#: Addresses that structurally cannot receive mail. Nothing you send to one of
#: these reaches a person, so these — and only these — may outrank the fact
#: that you have written to the address.
_UNREPLYABLE_PATTERN = (
    r"(^|[.+_-])(no-?reply|do-?not-?reply|donotreply|noreply"
    r"|mailer(-daemon)?|bounces?|postmaster|auto-?confirm|auto-?reply"
    r"|automated|notifications?|notify)(\+[^@]*)?@"
)

#: Role addresses. A company sends from these, and a person may well answer
#: from behind one — a freelancer's `hello@`, a small firm's `info@`, a
#: shop's `support@`. They are evidence of bulk *only when nothing has ever
#: been sent to them*.
#:
#: Separating these two lists is the whole fix for #44. Treating a role
#: address as unreplyable filed 59 senders the archive's owner had actually
#: corresponded with — 123 messages sent to them — as marketing, and hid them
#: from the default view. The issue that specified this classifier said in as
#: many words that a false "bulk" is the harmful direction, and lumping the
#: two lists together did exactly that.
_ROLE_PATTERN = (
    r"(^|[.+_-])(alerts?|updates?|newsletter|news|info|support|billing"
    r"|receipts?|orders?|shipment|tracking|marketing|email|mail|hello"
    r"|team|contact|sales|admin|office)(\+[^@]*)?@"
)

#: A sender this prolific that has never been written to and never landed in
#: Gmail's Personal category is a machine, whatever its address looks like.
_HIGH_VOLUME_BULK = 50

#: The same rule, for senders Gmail *did* file under Personal.
#:
#: Gmail's Personal category turns out to be a poor "this is a person" signal
#: for notification mail that relays human content: LinkedIn's `invitations@`,
#: `inmail-hit-reply@` and `member@` are all filed Personal, and between them
#: they carried thousands of messages into the "human" bucket. They are not
#: correspondents — you cannot reply to them, and across 22 years never did.
#:
#: So volume plus zero reciprocity overrules the Personal label, but at a much
#: higher bar, because this is the direction of error that hides a real person.
#: A one-way human relationship of 50 messages stays human; a notification
#: relay at 200+ does not.
_HIGH_VOLUME_DESPITE_PERSONAL = 200

#: Domain-inheritance thresholds. A domain needs real volume before its
#: character is established, and the ratio has to be high enough that a mostly
#: human domain — gmail.com, a workplace — can never be swept up by it.
_MIN_DOMAIN_VOLUME = 200
_BULK_DOMAIN_RATIO = 0.90


@dataclass
class SenderProfile:
    address: str
    domain: str
    kind: str
    evidence: list[str]
    received_count: int
    sent_to_count: int
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass
class DomainRollup:
    domain: str
    message_count: int
    sender_count: int
    human_senders: int
    first_seen: datetime | None
    last_seen: datetime | None


@dataclass
class Recipient:
    address: str
    message_count: int
    first_sent: datetime | None
    last_sent: datetime | None


@dataclass
class YearActivity:
    year: int
    sent: int
    received: int
    human_received: int
    bulk_received: int
    people_mailed: int

    @property
    def sent_share(self) -> float:
        total = self.sent + self.received
        return (self.sent / total * 100) if total else 0.0


def _row(row: object) -> tuple[Any, ...]:
    return tuple(row)  # type: ignore[arg-type]


def rebuild_sender_profiles(conn: psycopg.Connection[object]) -> int:
    """Classify every sender, and return how many were profiled.

    One pass over the corpus rather than a query per sender. The signals, in
    the order they are trusted:

    1. **You have written to this address.** Twenty years is a long time for
       that to be an accident, so it outranks everything except an address
       that is structurally incapable of receiving mail.
    2. **A no-reply-shaped address.** Conclusive in the other direction.
    3. **Gmail's own category**, where the bulk categories outnumber Personal.
    4. **Volume with no reciprocity.** Never written to, never Personal, and
       prolific.

    Anything left over is called human, deliberately: a false "bulk" hides a
    real letter, and a false "human" only leaves some noise in a list.

    A manual `override` is preserved across rebuilds — the whole reason it is
    a column rather than a recomputed value.
    """
    result = conn.execute(
        """
        with per_message as (
            select
                m.raw_sha256,
                lower(trim(m.from_addr)) as address,
                m.internal_date,
                bool_or(l.label = any(%(bulk)s)) as bulk_labelled,
                bool_or(l.label = 'Category Personal') as personal_labelled
            from messages m
            left join labels l on l.raw_sha256 = m.raw_sha256
            where m.from_addr is not null and trim(m.from_addr) <> ''
            group by m.raw_sha256, m.from_addr, m.internal_date
        ),
        received as (
            select
                address,
                count(*) as received_count,
                min(internal_date) as first_seen,
                max(internal_date) as last_seen,
                count(*) filter (where bulk_labelled) as bulk_labelled,
                count(*) filter (where personal_labelled) as personal_labelled
            from per_message
            group by address
        ),
        sent as (
            select lower(trim(r)) as address, count(*) as sent_to_count
            from messages m,
                 lateral unnest(m.to_addrs) as r
            where exists (
                select 1 from labels l
                where l.raw_sha256 = m.raw_sha256 and l.label = 'Sent'
            )
            group by 1
        ),
        joined as (
            select
                r.address,
                split_part(r.address, '@', 2) as domain,
                r.received_count,
                coalesce(s.sent_to_count, 0) as sent_to_count,
                r.first_seen,
                r.last_seen,
                r.bulk_labelled,
                r.personal_labelled,
                r.address ~ %(unreplyable)s as unreplyable,
                r.address ~ %(role)s as role_shaped
            from received r
            left join sent s on s.address = r.address
        )
        insert into sender_profiles (
            address, domain, kind, evidence,
            received_count, sent_to_count, first_seen, last_seen, computed_at
        )
        select
            address,
            domain,
            case
                -- Only a structurally unreplyable address outranks the fact
                -- that mail was sent to this address.
                when unreplyable then 'bulk'
                when sent_to_count > 0 then 'human'
                when role_shaped then 'bulk'
                when bulk_labelled > personal_labelled and bulk_labelled > 0
                    then 'bulk'
                when received_count >= %(high_volume)s
                     and personal_labelled = 0
                    then 'bulk'
                when received_count >= %(high_volume_personal)s then 'bulk'
                else 'human'
            end as kind,
            array_remove(array[
                case when sent_to_count > 0 then 'replied-to' end,
                case when unreplyable then 'unreplyable-address' end,
                case when role_shaped and sent_to_count = 0
                     then 'role-address' end,
                case when bulk_labelled > 0 then 'gmail-bulk-category' end,
                case when personal_labelled > 0 then 'gmail-personal-category' end,
                case when received_count >= %(high_volume)s
                     then 'high-volume' end,
                case when received_count >= %(high_volume_personal)s
                          and sent_to_count = 0
                     then 'never-replied-to' end
            ], null) as evidence,
            received_count,
            sent_to_count,
            first_seen,
            last_seen,
            now()
        from joined
        on conflict (address) do update set
            domain = excluded.domain,
            -- A hand-set override wins over anything recomputed.
            kind = coalesce(sender_profiles.override, excluded.kind),
            evidence = excluded.evidence,
            received_count = excluded.received_count,
            sent_to_count = excluded.sent_to_count,
            first_seen = excluded.first_seen,
            last_seen = excluded.last_seen,
            computed_at = excluded.computed_at
        """,
        {
            "bulk": list(BULK_CATEGORIES),
            "unreplyable": _UNREPLYABLE_PATTERN,
            "role": _ROLE_PATTERN,
            "high_volume": _HIGH_VOLUME_BULK,
            "high_volume_personal": _HIGH_VOLUME_DESPITE_PERSONAL,
        },
    )
    count = result.rowcount

    # ── Second pass: inherit a verdict from the domain ────────────────────
    #
    # Per-address rules miss the long tail of a bulk sender's own addresses.
    # A company mails from `invitations@`, `member@`, `updates-noreply@` and
    # thirty more, each individually too small to trip the volume rule and
    # none matching the no-reply shapes — so a domain that has never sent a
    # human-written word still lands a few thousand messages under "human".
    #
    # If a domain's traffic is overwhelmingly bulk, an address on it that you
    # have never written to and that Gmail never called Personal is bulk too.
    # The ratio is what keeps this safe: a domain like gmail.com is mostly
    # human and never qualifies, no matter how much of it is newsletters.
    reclassified = conn.execute(
        """
        with domain_totals as (
            select
                domain,
                sum(received_count) as total,
                sum(received_count) filter (where kind = 'bulk') as bulk_total
            from sender_profiles
            group by domain
        ),
        bulk_domains as (
            select domain from domain_totals
            where total >= %(min_domain_volume)s
              and bulk_total::float / total >= %(bulk_ratio)s
        )
        update sender_profiles p
        set kind = 'bulk',
            evidence = array_append(p.evidence, 'bulk-domain')
        from bulk_domains d
        where p.domain = d.domain
          and p.kind = 'human'
          and p.override is null
          and p.sent_to_count = 0
          and not ('gmail-personal-category' = any(p.evidence))
        """,
        {"min_domain_volume": _MIN_DOMAIN_VOLUME, "bulk_ratio": _BULK_DOMAIN_RATIO},
    ).rowcount

    logger.info("profiled %d senders (%d reclassified by domain)", count, reclassified)
    return int(count)


def profile_summary(conn: psycopg.Connection[object]) -> dict[str, int]:
    """Counts by kind, plus whether profiles exist at all."""
    rows = conn.execute(
        "select kind, count(*), coalesce(sum(received_count), 0)"
        " from sender_profiles group by kind"
    ).fetchall()
    summary: dict[str, int] = {}
    for r in (_row(rr) for rr in rows):
        summary[f"{r[0]}_senders"] = int(r[1])
        summary[f"{r[0]}_messages"] = int(r[2])
    summary["total_senders"] = sum(
        v for k, v in summary.items() if k.endswith("_senders")
    )
    return summary


def top_senders(
    conn: psycopg.Connection[object],
    *,
    kind: str | None = None,
    limit: int = 40,
) -> list[SenderProfile]:
    """Senders by volume, optionally restricted to one kind."""
    where = " where kind = %(kind)s" if kind else ""
    rows = conn.execute(
        "select address, domain, kind, evidence, received_count,"
        " sent_to_count, first_seen, last_seen"
        f" from sender_profiles{where}"
        " order by received_count desc, address"
        " limit %(limit)s",
        {"kind": kind, "limit": limit},
    ).fetchall()
    return [
        SenderProfile(
            address=str(r[0]),
            domain=str(r[1]),
            kind=str(r[2]),
            evidence=list(r[3]) if r[3] else [],
            received_count=int(r[4]),
            sent_to_count=int(r[5]),
            first_seen=r[6],
            last_seen=r[7],
        )
        for r in (_row(rr) for rr in rows)
    ]


def top_domains(
    conn: psycopg.Connection[object],
    *,
    kind: str | None = None,
    limit: int = 40,
) -> list[DomainRollup]:
    """Sender domains by volume. This is where bulk shows itself most plainly."""
    where = " where kind = %(kind)s" if kind else ""
    rows = conn.execute(
        "select domain, sum(received_count), count(*),"
        " count(*) filter (where kind = 'human'), min(first_seen), max(last_seen)"
        f" from sender_profiles{where}"
        " group by domain"
        " order by sum(received_count) desc, domain"
        " limit %(limit)s",
        {"kind": kind, "limit": limit},
    ).fetchall()
    return [
        DomainRollup(
            domain=str(r[0]),
            message_count=int(r[1]),
            sender_count=int(r[2]),
            human_senders=int(r[3]),
            first_seen=r[4],
            last_seen=r[5],
        )
        for r in (_row(rr) for rr in rows)
    ]


def top_recipients(
    conn: psycopg.Connection[object], *, limit: int = 40
) -> list[Recipient]:
    """Who you wrote to, by volume.

    The hardest list to fake: these are the addresses you chose to type.
    Derived from the `Sent` label rather than from profiles, because a
    recipient need never have sent you anything.
    """
    rows = conn.execute(
        "select lower(trim(r)) as address, count(*),"
        " min(m.internal_date), max(m.internal_date)"
        " from messages m, lateral unnest(m.to_addrs) as r"
        " where exists (select 1 from labels l"
        "   where l.raw_sha256 = m.raw_sha256 and l.label = 'Sent')"
        " group by 1"
        " order by count(*) desc, address"
        " limit %s",
        (limit,),
    ).fetchall()
    return [
        Recipient(
            address=str(r[0]),
            message_count=int(r[1]),
            first_sent=r[2],
            last_sent=r[3],
        )
        for r in (_row(rr) for rr in rows)
    ]


def lost_touch(
    conn: psycopg.Connection[object], *, limit: int = 30, min_messages: int = 5
) -> list[SenderProfile]:
    """People with real two-way history who have gone quiet.

    Ranked by volume against silence, so a long correspondence that stopped
    outranks an acquaintance who sent six messages once. Only senders you have
    actually written back to qualify — otherwise this is a list of newsletters
    that stopped sending.
    """
    rows = conn.execute(
        "select address, domain, kind, evidence, received_count,"
        " sent_to_count, first_seen, last_seen"
        " from sender_profiles"
        " where kind = 'human' and sent_to_count > 0"
        "   and received_count >= %(min_messages)s"
        "   and last_seen < now() - interval '2 years'"
        " order by received_count::float"
        "   * extract(epoch from now() - last_seen) desc"
        " limit %(limit)s",
        {"limit": limit, "min_messages": min_messages},
    ).fetchall()
    return [
        SenderProfile(
            address=str(r[0]),
            domain=str(r[1]),
            kind=str(r[2]),
            evidence=list(r[3]) if r[3] else [],
            received_count=int(r[4]),
            sent_to_count=int(r[5]),
            first_seen=r[6],
            last_seen=r[7],
        )
        for r in (_row(rr) for rr in rows)
    ]


def yearly_activity(conn: psycopg.Connection[object]) -> list[YearActivity]:
    """Sent, received and distinct correspondents per calendar year.

    Bounded to plausible dates for the same reason `stats()` is: one message
    claiming 2611 should not add a column six centuries wide.
    """
    # Two narrow queries rather than one wide one.
    #
    # The first version grouped `messages left join labels` — a million label
    # rows collapsed to 269k messages before anything was counted, ~1.6s. But
    # only the `Sent` label matters here, and there are ~12k of those: a
    # semi-join against `labels_label_idx` reads a fraction of the table.
    # Recipient counting needs `unnest`, which multiplies rows, so it runs
    # separately over sent mail only and is merged in Python.
    rows = conn.execute(
        """
        select
            extract(year from m.internal_date)::int as yr,
            count(*) filter (where s.raw_sha256 is not null) as sent,
            count(*) filter (where s.raw_sha256 is null) as received,
            count(*) filter (
                where s.raw_sha256 is null
                  and coalesce(p.kind, 'human') = 'human'
            ) as human_received,
            count(*) filter (
                where s.raw_sha256 is null and p.kind = 'bulk'
            ) as bulk_received
        from messages m
        left join (
            select distinct raw_sha256 from labels where label = 'Sent'
        ) s on s.raw_sha256 = m.raw_sha256
        left join sender_profiles p on p.address = lower(trim(m.from_addr))
        where m.internal_date >= '1990-01-01' and m.internal_date <= now()
        group by 1
        order by 1
        """
    ).fetchall()

    people = conn.execute(
        """
        select extract(year from m.internal_date)::int as yr,
               count(distinct lower(trim(r))) as people
        from messages m
        join labels l on l.raw_sha256 = m.raw_sha256 and l.label = 'Sent',
             lateral unnest(m.to_addrs) as r
        where m.internal_date >= '1990-01-01' and m.internal_date <= now()
        group by 1
        """
    ).fetchall()
    people_by_year = {int(r[0]): int(r[1]) for r in (_row(rr) for rr in people)}

    return [
        YearActivity(
            year=int(r[0]),
            sent=int(r[1]),
            received=int(r[2]),
            human_received=int(r[3]),
            bulk_received=int(r[4]),
            people_mailed=people_by_year.get(int(r[0]), 0),
        )
        for r in (_row(rr) for rr in rows)
    ]


def correspondent(
    conn: psycopg.Connection[object], address: str
) -> SenderProfile | None:
    """One sender's profile, by address."""
    raw = conn.execute(
        "select address, domain, kind, evidence, received_count,"
        " sent_to_count, first_seen, last_seen"
        " from sender_profiles where address = %s",
        (address.lower().strip(),),
    ).fetchone()
    if raw is None:
        return None
    r = _row(raw)
    return SenderProfile(
        address=str(r[0]),
        domain=str(r[1]),
        kind=str(r[2]),
        evidence=list(r[3]) if r[3] else [],
        received_count=int(r[4]),
        sent_to_count=int(r[5]),
        first_seen=r[6],
        last_seen=r[7],
    )


def correspondent_years(
    conn: psycopg.Connection[object], address: str
) -> list[tuple[int, int]]:
    """Per-year message counts from one sender, for a sparkline."""
    rows = conn.execute(
        "select extract(year from internal_date)::int, count(*)"
        " from messages"
        " where lower(trim(from_addr)) = %s"
        "   and internal_date >= '1990-01-01' and internal_date <= now()"
        " group by 1 order by 1",
        (address.lower().strip(),),
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in (_row(rr) for rr in rows)]
