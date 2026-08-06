"""Command-line surface.

Kept thin on purpose: every command delegates immediately. This layer is
expected to churn, so behavior worth trusting lives in the modules below it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import click
import psycopg

from gmail_archive.config import Settings
from gmail_archive.fixtures import MEASURED_RATES as _MEASURED
from gmail_archive.fixtures import Pathology, generate
from gmail_archive.logging_setup import configure
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
    from gmail_archive.ingest import ingest as _ingest

    settings = Settings.from_env()
    if not settings.database_url:
        click.echo("GMAIL_ARCHIVE_DATABASE_URL is not set", err=True)
        raise click.Abort()

    report = _ingest(
        settings,
        mbox,
        workers=workers,
        batch_size=batch_size,
    )
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
