"""Command-line surface.

Kept thin on purpose: every command delegates immediately. This layer is
expected to churn, so behavior worth trusting lives in the modules below it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import click

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
