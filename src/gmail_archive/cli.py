"""Command-line surface.

Kept thin on purpose: every command delegates immediately. This layer is
expected to churn, so behavior worth trusting lives in the modules below it.
"""

from __future__ import annotations

import json

import click

from gmail_archive.config import Settings
from gmail_archive.logging_setup import configure
from gmail_archive.version import build_info


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
