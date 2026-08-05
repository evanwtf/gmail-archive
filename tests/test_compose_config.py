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


class TestWebService:
    def test_builds_the_hardened_runtime_stage(self) -> None:
        assert _service("web")["build"]["target"] == "runtime", (
            "the default stack must build the nonroot runtime stage, not the "
            "builder stage that still carries uv and a shell"
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

    def test_published_only_on_loopback(self) -> None:
        # The UI has no authentication. Binding it to 0.0.0.0 on the host would
        # serve the whole archive to anything that can route to the box.
        for mapping in _service("web")["ports"]:
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{mapping!r} must be published on 127.0.0.1 only"
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
