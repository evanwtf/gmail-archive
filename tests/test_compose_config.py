"""Pin the production posture of the default compose stack.

`docker-compose.yml` is the real deployment: a bare `docker compose up -d` in
this directory manages it. Each assertion below records a specific way that can
go wrong — a dev stage shipped as production, a database exposed on the box, a
UI serving twenty years of mail on a routable interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _compose() -> dict[str, Any]:
    with (_REPO_ROOT / "docker-compose.yml").open() as fh:
        loaded: dict[str, Any] = yaml.safe_load(fh)
    return loaded


def _service(name: str) -> dict[str, Any]:
    service: dict[str, Any] = _compose()["services"][name]
    return service


def _project_version() -> str:
    import tomllib

    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        version: str = tomllib.load(fh)["project"]["version"]
    return version


def _default_image_tag(raw: str) -> str:
    """The tag compose falls back to when GMAIL_ARCHIVE_IMAGE_TAG is unset.

    Parses `${VAR:-default}` out of the image line rather than running
    `docker compose config`, so this test needs no Docker daemon.
    """
    _, _, tag_expr = raw.rpartition(":${GMAIL_ARCHIVE_IMAGE_TAG:-")
    assert tag_expr, f"image line has no defaulted tag: {raw!r}"
    return tag_expr.removesuffix("}")


class TestPublishedImageTag:
    """The compose default must track the version this tree builds.

    Not a style rule. The pin sat at 0.2.8 while the database had been migrated
    to the 0.3.0 schema, and `docker compose up -d` pulled code that queried a
    column migration 0005 had dropped — every message detail page returned 503
    until the image was rebuilt. Compose defaults are load-bearing: nothing
    else in the repo notices when one goes stale.
    """

    def test_default_tag_matches_the_project_version(self) -> None:
        tag = _default_image_tag(_service("web")["image"])
        assert tag == _project_version(), (
            f"docker-compose.yml defaults to image tag {tag!r} but "
            f"pyproject.toml says {_project_version()!r}. Bump both together, "
            "or the default stack runs a version older than its own schema."
        )

    def test_the_example_env_does_not_pin_something_older(self) -> None:
        """`.env.example` is copied verbatim, so a stale value there sticks.

        An override in `.env` beats the compose default, which makes a wrong
        value here worse than no value: it survives every subsequent upgrade.
        """
        for line in (_REPO_ROOT / ".env.example").read_text().splitlines():
            if line.startswith("GMAIL_ARCHIVE_IMAGE_TAG="):
                pinned = line.split("=", 1)[1].strip()
                assert pinned == _project_version(), (
                    f".env.example pins image tag {pinned!r}, but this tree is "
                    f"{_project_version()!r}"
                )


class TestWebService:
    def test_builds_the_hardened_runtime_stage(self) -> None:
        assert _service("web")["build"]["target"] == "runtime", (
            "the default stack must build the nonroot runtime stage, not the "
            "builder stage that still carries uv and the build tooling"
        )

    def test_does_not_mount_host_source_over_baked_in_code(self) -> None:
        mounts = [str(v) for v in _service("web").get("volumes", [])]
        assert not any(m.startswith("./src:") for m in mounts), (
            "the default stack must run the code baked into the image; a live "
            "host mount makes the running code differ from the built image"
        )

    def test_does_not_run_the_reloader(self) -> None:
        web = _service("web")
        argv = [*(web.get("entrypoint") or []), *(web.get("command") or [])]
        assert "--reload" not in argv

    def test_published_on_all_interfaces(self) -> None:
        # The user chose to expose the web UI on 0.0.0.0. The archive has no
        # authentication, so this is only appropriate on a trusted network.
        for mapping in _service("web")["ports"]:
            assert str(mapping).startswith("0.0.0.0:"), (
                f"{mapping!r} must be published on 0.0.0.0"
            )

    def test_waits_for_postgres_and_the_permissions_one_shot(self) -> None:
        depends = _service("web")["depends_on"]
        assert depends["postgres"]["condition"] == "service_healthy"
        assert depends["init-perms"]["condition"] == "service_completed_successfully"


class TestPostgresService:
    def test_major_version_is_pinned(self) -> None:
        image = _service("postgres")["image"]
        assert image.startswith("postgres:"), image
        tag = image.split(":", 1)[1]
        assert tag != "latest" and tag[0].isdigit(), (
            f"postgres image tag {tag!r} must pin a major version — a major "
            "upgrade will not start against an existing data directory"
        )

    def test_not_published_to_the_host(self) -> None:
        assert "ports" not in _service("postgres"), (
            "postgres must stay on the compose network; the ports block is "
            "left commented out in the file for the occasions it is needed"
        )

    def test_watchtower_cannot_bump_the_major(self) -> None:
        labels = _service("postgres")["labels"]
        assert "com.centurylinklabs.watchtower.enable=false" in labels

    def test_password_is_required_not_defaulted(self) -> None:
        password = _service("postgres")["environment"]["POSTGRES_PASSWORD"]
        assert password.startswith("${POSTGRES_PASSWORD:?"), (
            "use the ${VAR:?message} form so a missing password fails the "
            "stack with an actionable error instead of starting an open server"
        )


class TestIngestService:
    def test_is_behind_a_profile(self) -> None:
        assert _service("ingest")["profiles"] == ["ingest"], (
            "ingest is a one-shot job and must not start on `docker compose up`"
        )

    def test_mounts_the_export_read_only(self) -> None:
        mounts = [str(v) for v in _service("ingest")["volumes"]]
        mbox = [m for m in mounts if ":/mbox" in m]
        assert mbox and all(m.endswith(":ro") for m in mbox), (
            "the Takeout export is the only copy until ingest finishes; mount "
            "it read-only"
        )


class TestPublicRepositoryHygiene:
    def test_no_log_shipper_is_defined(self) -> None:
        # The `logging=promtail` labels stay — they are inert without an agent.
        # The agent itself, and the central Loki it would ship to, are not this
        # repository's business.
        services = _compose()["services"]
        for name in services:
            assert "promtail" not in name and "loki" not in name, name


class TestImapService:
    """The IMAP server is runnable under compose, behind a profile (#25)."""

    def test_imap_service_exists_behind_a_profile(self) -> None:
        service = _service("imap")
        assert service["profiles"] == ["imap"]

    def test_the_imap_password_is_passed_into_the_container(self) -> None:
        # The bug: .env set it for the compose process, not for the container,
        # so the server always aborted with "IMAP password not set".
        env = _service("imap")["environment"]
        assert "GMAIL_ARCHIVE_IMAP_PASSWORD" in env

    def test_the_password_is_not_a_required_interpolation(self) -> None:
        # `${VAR:?...}` is interpolated for the whole file whatever profile is
        # active, so a required variable here breaks `docker compose ps` and
        # `up` for the default stack. Found the hard way.
        raw = (_REPO_ROOT / "docker-compose.yml").read_text()
        assert "GMAIL_ARCHIVE_IMAP_PASSWORD:?" not in raw

    def test_imap_binds_all_interfaces_inside_the_container(self) -> None:
        # 127.0.0.1 inside a container is the container's own loopback, which
        # nothing outside can reach; the ports mapping is what limits exposure.
        assert "0.0.0.0" in _service("imap")["command"]

    def test_imap_is_published_on_loopback_only(self) -> None:
        # Unlike the web UI: one shared password and no TLS, so reaching the
        # network should be a deliberate edit.
        for mapping in _service("imap")["ports"]:
            assert str(mapping).startswith("127.0.0.1:"), mapping
