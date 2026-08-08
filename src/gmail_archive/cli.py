"""Command-line surface.

Kept thin on purpose: every command delegates immediately. This layer is
expected to churn, so behavior worth trusting lives in the modules below it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import click
import psycopg

from gmail_archive.config import Settings
from gmail_archive.fixtures import MEASURED_RATES as _MEASURED
from gmail_archive.fixtures import Pathology, generate
from gmail_archive.logging_setup import configure
from gmail_archive.parser import strip_unstorable
from gmail_archive.version import build_info

logger = logging.getLogger(__name__)


@click.group()
def main() -> None:
    """Archive a Google Takeout Gmail mbox export into Postgres."""
    configure(Settings.from_env().log_level)


@main.command()
def version() -> None:
    """Print build and runtime identity."""
    click.echo(json.dumps(build_info(), indent=2))


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def serve(host: str, port: int) -> None:
    """Run the local web UI.

    Binds 0.0.0.0 inside the container; the compose `ports:` mapping is what
    restricts exposure to the loopback interface on the host.
    """
    import uvicorn

    uvicorn.run("gmail_archive.web.app:app", host=host, port=port)


@main.command("gen-fixture")
@click.argument("out", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--count", default=100, show_default=True, type=int)
@click.option(
    "--seed",
    default=0,
    show_default=True,
    type=int,
    help="Same seed, same bytes. Asserted by the test suite.",
)
@click.option(
    "--pathologies",
    default=None,
    help=(
        "Comma-separated defects to generate, each guaranteed present. "
        "Omit for a realistic mix at the rates measured on a real export. "
        "Use 'list' to print the menu."
    ),
)
def gen_fixture(out: Path, count: int, seed: int, pathologies: str | None) -> None:
    """Write a synthetic mbox fixture.

    The real Takeout export cannot be a test fixture in a public repository, so
    the project generates its own input. Every address is confined to an RFC
    2606 reserved domain by construction.
    """
    if pathologies == "list":
        for p in Pathology:
            measured = " (in default mix)" if p in _MEASURED else ""
            click.echo(f"{p.value}{measured}")
        return

    selected: list[Pathology] | None = None
    if pathologies:
        try:
            selected = [Pathology(name.strip()) for name in pathologies.split(",")]
        except ValueError as exc:
            raise click.BadParameter(
                f"{exc}. Run with --pathologies list to see the menu."
            ) from exc

    report = generate(out, count=count, seed=seed, pathologies=selected)
    logger.info(
        "wrote %d messages (%.1f MiB) to %s",
        report.count,
        report.bytes_written / 1024 / 1024,
        report.path,
    )
    click.echo(
        json.dumps(
            {
                "path": str(report.path),
                "count": report.count,
                "seed": report.seed,
                "bytes": report.bytes_written,
                "pathologies": report.pathology_counts,
            },
            indent=2,
        )
    )


@main.command()
def migrate() -> None:
    """Apply pending database schema migrations."""
    from gmail_archive.migrate import migrate as _migrate

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    ran = _migrate(settings.database_url)
    click.echo(json.dumps({"applied": len(ran)}, indent=2))


@main.command()
@click.argument("mbox", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--workers", default=None, type=int, help="Worker count (default: cpu count)"
)
@click.option(
    "--batch-size", default=None, type=int, help="Messages per batch (default: 1000)"
)
def ingest(mbox: Path, workers: int | None, batch_size: int | None) -> None:
    """Ingest an mbox file into Postgres.

    Resumable and idempotent: re-running after a kill picks up where it left
    off, and re-ingesting the same file twice adds nothing.
    """
    from gmail_archive.ingest import IngestAlreadyRunningError
    from gmail_archive.ingest import ingest as _ingest

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    try:
        report = _ingest(
            settings,
            mbox,
            workers=workers,
            batch_size=batch_size,
        )
    except IngestAlreadyRunningError as exc:
        # A refusal, not a crash: two ingests at once corrupt each other.
        click.echo(str(exc), err=True)
        raise click.Abort() from exc
    click.echo(
        json.dumps(
            {
                "source_path": report.source_path,
                "messages_seen": report.messages_seen,
                "messages_new": report.messages_new,
                "messages_duplicate": report.messages_duplicate,
                "failures": report.failures,
                "elapsed_seconds": round(report.elapsed_seconds, 1),
                "run_id": report.run_id,
            },
            indent=2,
        )
    )


@main.command("set-password")
@click.option(
    "--password",
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Prompted for if omitted, so it never lands in shell history.",
)
def set_password(password: str) -> None:
    """Hash a password for the web UI and print the line to add to .env.

    The hash is printed, never the password, and the prompt is hidden — so
    neither reaches shell history or a terminal scrollback. Changing the
    password invalidates every existing session, because the cookie signing
    key is derived from the hash.
    """
    from gmail_archive.web.auth import hash_password

    if len(password) < 8:
        click.echo("Use at least 8 characters.", err=True)
        raise click.Abort()

    click.echo("\nAdd this line to .env, then restart the web container:\n")
    click.echo(f"GMAIL_ARCHIVE_WEB_PASSWORD_HASH={hash_password(password)}\n")


@main.command()
def analyze() -> None:
    """Classify senders as human correspondence or automated mail.

    One pass over the corpus, writing `sender_profiles`. Run it after an
    ingest: the signals are corpus-wide (has this address ever been replied
    to?) and cannot be evaluated one message at a time. Manual overrides are
    preserved.
    """
    from gmail_archive.analytics import profile_summary, rebuild_sender_profiles

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    with psycopg.connect(settings.database_url) as conn:
        profiled = rebuild_sender_profiles(conn)
        conn.commit()
        summary = profile_summary(conn)

    click.echo(json.dumps({"profiled": profiled, **summary}, indent=2))


@main.command()
def stats() -> None:
    """Print archive statistics."""
    from gmail_archive.query import stats as _stats

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    with psycopg.connect(settings.database_url) as conn:
        result = _stats(conn)

    click.echo(
        json.dumps(
            {
                "total_messages": result.total_messages,
                "total_blobs": result.total_blobs,
                "total_attachments": result.total_attachments,
                "total_labels": result.total_labels,
                "total_failures": result.total_failures,
                "total_runs": result.total_runs,
                "total_bytes": result.total_bytes,
                "blob_bytes": result.blob_bytes,
                "date_earliest": (
                    str(result.date_earliest) if result.date_earliest else None
                ),
                "date_latest": (
                    str(result.date_latest) if result.date_latest else None
                ),
            },
            indent=2,
        )
    )


@main.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--limit", default=50, show_default=True, type=int)
@click.option("--offset", default=0, show_default=True, type=int)
def search(query: tuple[str, ...], limit: int, offset: int) -> None:
    """Full-text search over archived messages.

    QUERY is a websearch-style string (words, quoted phrases, or -excluded terms).
    """
    from gmail_archive.query import search as _search

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    query_str = " ".join(query)
    with psycopg.connect(settings.database_url) as conn:
        result = _search(conn, query_str, limit=limit, offset=offset)

    click.echo(
        json.dumps(
            {
                "query": result.query,
                "total": result.total,
                "messages": [
                    {
                        "raw_sha256": m.raw_sha256,
                        "subject": m.subject,
                        "from_addr": m.from_addr,
                        "to_addrs": m.to_addrs,
                        "internal_date": (
                            str(m.internal_date) if m.internal_date else None
                        ),
                        "thread_id": m.thread_id,
                        "snippet": m.snippet,
                    }
                    for m in result.messages
                ],
            },
            indent=2,
        )
    )


@main.command()
@click.option("--deep", is_flag=True, help="Re-hash every blob on disk")
def verify(deep: bool) -> None:
    """Verify archive integrity.

    Reconciles the database against the blob store and message sightings.
    With --deep, re-hashes every blob on disk against its sha256 filename.
    """
    from gmail_archive.storage import BlobStore
    from gmail_archive.verify import verify as _verify

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    store = BlobStore(settings.blob_dir)
    with psycopg.connect(settings.database_url) as conn:
        report = _verify(conn, store, deep=deep)

    click.echo(
        json.dumps(
            {
                "messages_in_db": report.messages_in_db,
                "sightings_in_db": report.sightings_in_db,
                "blobs_in_db": report.blobs_in_db,
                "blobs_on_disk": report.blobs_on_disk,
                "orphaned_blobs": len(report.orphaned_blobs),
                "orphaned_blob_list": report.orphaned_blobs[:20],
                "missing_blobs": len(report.missing_blobs),
                "missing_blob_list": report.missing_blobs[:20],
                "deep_checked": report.deep_checked,
                "deep_corrupt": len(report.deep_corrupt),
                "deep_corrupt_list": report.deep_corrupt[:20],
                "sighting_mismatch": report.sighting_mismatch,
                "messages_without_sightings": report.messages_without_sightings,
            },
            indent=2,
        )
    )

    if report.missing_blobs:
        click.echo(
            "WARNING: missing blobs detected — data loss may have occurred",
            err=True,
        )
    if report.deep_corrupt:
        click.echo(
            "WARNING: corrupt blobs detected — content hash mismatch",
            err=True,
        )


@main.command()
@click.argument("output", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--label", default=None, help="Filter by label")
@click.option("--query", default=None, help="Full-text search filter")
@click.option("--limit", default=None, type=int, help="Max messages to export")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["mbox", "eml"]),
    default="mbox",
    show_default=True,
    help="Output format: mbox (single file) or eml (one file per message)",
)
def export(
    output: Path,
    label: str | None,
    query: str | None,
    limit: int | None,
    fmt: str,
) -> None:
    """Export archived messages.

    OUTPUT is the output path. For mbox format, this is a single file. For eml
    format, this is a directory (one .eml file per message).
    """
    from gmail_archive.export import export_eml, export_mbox
    from gmail_archive.storage import BlobStore

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    store = BlobStore(settings.blob_dir)
    with psycopg.connect(settings.database_url) as conn:
        if fmt == "mbox":
            count = export_mbox(
                conn,
                store,
                output,
                label=label,
                query=query,
                limit=limit,
            )
        else:
            count = export_eml(
                conn,
                store,
                output,
                label=label,
                query=query,
                limit=limit,
            )

    click.echo(
        json.dumps({"format": fmt, "output": str(output), "count": count}, indent=2)
    )


@main.command()
def labels() -> None:
    """List all labels with message counts."""
    from gmail_archive.query import list_labels as _list_labels

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    with psycopg.connect(settings.database_url) as conn:
        result = _list_labels(conn)

    click.echo(
        json.dumps(
            [{"label": lb.label, "message_count": lb.message_count} for lb in result],
            indent=2,
        )
    )


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=1143, show_default=True, type=int)
@click.option(
    "--user",
    default="archive",
    show_default=True,
    help="IMAP login username",
)
@click.option(
    "--password",
    default=None,
    help="IMAP login password (default: $GMAIL_ARCHIVE_IMAP_PASSWORD)",
)
def imap(host: str, port: int, user: str, password: str | None) -> None:
    """Run the read-only IMAP server.

    Serves archived messages over IMAP, mapping Gmail labels to folders.
    Connect with any IMAP client (Thunderbird, mutt, etc.) using the
    configured username and password.
    """
    import asyncio

    from gmail_archive.imap import GmailArchiveBackend

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    imap_password = password or settings.imap_password
    if not imap_password:
        click.echo(
            "IMAP password not set. Provide --password or set"
            " GMAIL_ARCHIVE_IMAP_PASSWORD.",
            err=True,
        )
        raise click.Abort()

    # Build the argument namespace the way pymap itself does, rather than
    # hand-assembling one.
    #
    # A hand-built Namespace was missing `cert`, `key` and `tls` — pymap's
    # IMAPService contributes those to the *top-level* parser, not to the
    # backend subparser, so they are easy to overlook. The result was that
    # `gmail-archive imap` had never once started: it died in
    # `Config.from_args` with AttributeError before binding a socket.
    #
    # Reconstructing pymap's parser means every default it expects exists by
    # construction, and a future pymap release that adds an option cannot
    # silently break this again.
    from argparse import ArgumentParser

    from pymap.service import services

    parser = ArgumentParser(prog="gmail-archive imap")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--pid-file")
    parser.add_argument("--logging-cfg")
    subparsers = parser.add_subparsers(dest="backend", required=True)
    subparser = GmailArchiveBackend.add_subparser("gmail-archive", subparsers)
    subparser.set_defaults(backend_type=GmailArchiveBackend)
    for service_type in services.values():
        service_type.add_arguments(parser)
    # pymap's own main() adds --set-uid/--set-gid only on POSIX, and its
    # run() reads them unconditionally. Supplied as defaults rather than
    # flags: dropping privileges is the container's job, not this CLI's.
    parser.set_defaults(skip_services=[], passlib_cfg=None, set_uid=None, set_gid=None)

    # Service options come before the backend name; backend options after.
    args = parser.parse_args(
        [
            "--host",
            host,
            "--port",
            str(port),
            # Without this pymap advertises LOGINDISABLED and refuses
            # plaintext auth, which is correct of it — but there is no cert
            # here, and the server is published on loopback only. The
            # alternative is a self-signed cert every client then has to be
            # told to trust. Documented in the runbook, and the reason the
            # compose service does not reach the network.
            "--no-tls",
            "gmail-archive",
            "--database-url",
            settings.database_url,
            "--user",
            user,
            "--password",
            imap_password,
        ]
    )

    async def _run() -> None:
        from pymap.imap import IMAPService
        from pymap.main import run as _pymap_run

        service_types = [IMAPService]
        await _pymap_run(args, GmailArchiveBackend, service_types)

    logger.info("Starting IMAP server on %s:%s", host, port)
    asyncio.run(_run())


@main.command("imap-backfill")
def imap_backfill() -> None:
    """Backfill envelope and bodystructure for all messages.

    Reads every message from the blob store, parses it with pymap's MIME parser,
    and stores the envelope and bodystructure in the database. Also assigns UIDs
    for every (folder, message) pair.

    Run this once after the initial ingest to enable efficient IMAP FETCH responses.
    """
    import json as _json

    from gmail_archive.storage import BlobStore

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    store = BlobStore(settings.blob_dir)

    with psycopg.connect(settings.database_url) as conn:
        # Ensure folders exist for all labels
        conn.execute(
            """
            INSERT INTO imap_folders (name, uid_validity)
            SELECT 'INBOX', 1
            WHERE NOT EXISTS (SELECT 1 FROM imap_folders WHERE name = 'INBOX')
            """
        )
        conn.execute(
            """
            INSERT INTO imap_folders (name, uid_validity)
            SELECT DISTINCT l.label, 1
            FROM labels l
            WHERE NOT EXISTS (SELECT 1 FROM imap_folders f WHERE f.name = l.label)
            """
        )

        # Get all messages that need backfill
        rows = conn.execute(
            """
            SELECT m.raw_sha256, m.size_bytes
            FROM messages m
            WHERE m.envelope IS NULL OR m.bodystructure IS NULL
            ORDER BY m.ingested_at
            """
        ).fetchall()

        total = len(rows)
        if total == 0:
            click.echo("All messages already have envelope and bodystructure.")
            return

        click.echo(f"Backfilling {total} messages...")

        from pymap.mime import MessageContent

        done = 0
        for raw_sha256, _size_bytes in rows:
            raw_bytes = store.get(raw_sha256)
            if raw_bytes is None:
                logger.warning("Blob not found for %s, skipping", raw_sha256)
                continue

            content = MessageContent.parse(raw_bytes)
            envelope = _json.dumps(_scrub(_envelope_to_dict(content)))
            bodystructure = _json.dumps(_scrub(_bodystructure_to_dict(content)))

            conn.execute(
                "UPDATE messages SET envelope = %s::jsonb,"
                " bodystructure = %s::jsonb WHERE raw_sha256 = %s",
                (envelope, bodystructure, raw_sha256),
            )

            done += 1
            if done % 100 == 0:
                conn.commit()
                click.echo(f"  {done}/{total}")

        conn.commit()

        # ── Assign UIDs ──────────────────────────────────────────────
        #
        # UIDs must be assigned once, ascend strictly within a folder, and
        # never be reused: clients cache them hard and read a changed UID as
        # data loss. The migration says so in as many words.
        #
        # This previously numbered messages by their position in an
        # `ORDER BY raw_sha256` listing. A single new message with a low hash
        # shifted every later position by one, so a re-run offered an existing
        # UID to a different message and hit the `(folder_id, uid)` primary
        # key. The ON CONFLICT clause names `(folder_id, raw_sha256)`, so that
        # collision was not caught — the statement raised and the backfill
        # aborted mid-folder with earlier folders already committed.
        #
        # Now: only messages with no UID in this folder get one, numbered from
        # the folder's current maximum, ordered by arrival. Existing UIDs are
        # never touched, and a re-run after an ingest simply appends.
        click.echo("Assigning UIDs...")
        folder_rows = conn.execute(
            "SELECT id, name FROM imap_folders ORDER BY name"
        ).fetchall()
        for folder_id, folder_name in folder_rows:
            label_filter = None if folder_name == "INBOX" else folder_name

            if label_filter:
                source = (
                    "SELECT m.raw_sha256, m.ingested_at"
                    " FROM messages m JOIN labels l ON l.raw_sha256 = m.raw_sha256"
                    " WHERE l.label = %(label)s"
                )
            else:
                # INBOX gets everything.
                source = "SELECT m.raw_sha256, m.ingested_at FROM messages m"

            assigned = conn.execute(
                f"""
                WITH candidates AS (
                    {source}
                ),
                unassigned AS (
                    SELECT c.raw_sha256,
                           row_number() OVER (
                               ORDER BY c.ingested_at, c.raw_sha256
                           ) AS n
                    FROM candidates c
                    WHERE NOT EXISTS (
                        SELECT 1 FROM imap_uids u
                        WHERE u.folder_id = %(folder)s
                          AND u.raw_sha256 = c.raw_sha256
                    )
                ),
                base AS (
                    SELECT coalesce(max(uid), 0) AS start
                    FROM imap_uids WHERE folder_id = %(folder)s
                )
                INSERT INTO imap_uids (folder_id, raw_sha256, uid)
                SELECT %(folder)s, u.raw_sha256, base.start + u.n
                FROM unassigned u, base
                """,
                {"folder": folder_id, "label": label_filter},
            ).rowcount

            conn.commit()
            click.echo(f"  Folder '{folder_name}': {assigned} new UIDs")

    click.echo("Backfill complete.")


def _scrub(value: Any) -> Any:
    """Recursively strip NUL and lone surrogates from a decoded structure.

    pymap's MIME parser returns whatever the message contained, and a real
    export contains subjects with embedded NULs. `json.dumps` happily encodes
    one as `\u0000`, and Postgres then rejects the whole jsonb value —
    which killed a backfill 194,000 messages in.
    """
    if isinstance(value, str):
        return strip_unstorable(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def _envelope_to_dict(content: Any) -> dict[str, Any]:
    """Convert a pymap EnvelopeStructure to a JSON-serializable dict."""
    parsed = content.header.parsed
    return {
        "date": parsed.date,
        "subject": parsed.subject,
        "from": _addr_list(parsed.from_),
        "sender": _addr_list(parsed.sender),
        "reply_to": _addr_list(parsed.reply_to),
        "to": _addr_list(parsed.to),
        "cc": _addr_list(parsed.cc),
        "bcc": _addr_list(parsed.bcc),
        "in_reply_to": parsed.in_reply_to,
        "message_id": parsed.message_id,
    }


def _bodystructure_to_dict(content: Any) -> dict[str, Any]:
    """Convert a pymap BodyStructure to a JSON-serializable dict."""
    return _body_part(content)


def _body_part(msg: Any) -> dict[str, Any]:
    """Recursively convert a MIME part to a dict."""
    maintype = msg.body.content_type.maintype
    subtype = msg.body.content_type.subtype
    params = dict(msg.body.content_type.params)
    parsed = msg.header.parsed

    result: dict[str, Any] = {
        "type": f"{maintype}/{subtype}",
        "params": params,
    }

    if maintype == "multipart":
        result["parts"] = [_body_part(part) for part in msg.body.nested]
    else:
        if maintype == "text":
            result["lines"] = msg.lines
        result["size"] = len(msg)
        result["encoding"] = parsed.content_transfer_encoding
        result["id"] = parsed.content_id
        result["description"] = parsed.content_description

    return result


def _addr_list(addrs: Any) -> list[dict[str, Any] | None]:
    """Convert an address list to JSON."""
    if not addrs:
        return []
    return [
        {"name": a.name, "mailbox": a.mailbox, "host": a.host, "addr": a.addr}
        if hasattr(a, "addr")
        else None
        for a in addrs
    ]
