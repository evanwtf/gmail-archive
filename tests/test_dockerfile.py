"""Pin the properties of the image that are easy to lose in a casual edit.

The base image was moved off cgr.dev/chainguard/python precisely because that
image cannot be version-pinned on the free tier, and an unpinned interpreter
silently changes stdlib `email` and `mailbox` behavior under a parser this
project trusts with irreplaceable data. Guard the property, not the vendor.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()

_FLOATING_TAGS = {"latest", "latest-dev", "slim", "alpine", "bookworm", "trixie"}


def _python_image() -> str:
    match = re.search(r"^ARG PYTHON_IMAGE=(.+)$", _DOCKERFILE, re.MULTILINE)
    assert match, "the base image must come from a single ARG PYTHON_IMAGE"
    return match.group(1).strip()


def test_base_image_pins_an_exact_python_patch_version() -> None:
    image = _python_image()
    _, _, tag = image.partition(":")
    assert tag, f"{image!r} has no tag and would resolve to :latest"
    assert tag not in _FLOATING_TAGS, f"{tag!r} floats across Python versions"
    assert re.match(r"^\d+\.\d+\.\d+", tag), (
        f"{tag!r} must pin major.minor.patch — a floating minor changes stdlib "
        "email/mailbox parsing behavior between rebuilds"
    )


def test_both_stages_share_one_base_image() -> None:
    # The venv's bin/python is a symlink into the builder's interpreter. If the
    # stages drift apart, that symlink dangles in the runtime stage and the
    # container dies at startup rather than at build time.
    froms = re.findall(r"^FROM (\S+)", _DOCKERFILE, re.MULTILINE)
    stage_bases = [f for f in froms if not f.startswith("ghcr.io/")]
    assert stage_bases == ["${PYTHON_IMAGE}", "${PYTHON_IMAGE}"], stage_bases


def test_uv_may_not_download_its_own_interpreter() -> None:
    # Otherwise uv honours .python-version by fetching a CPython into a home
    # directory the runtime stage never copies.
    assert "UV_PYTHON_DOWNLOADS=never" in _DOCKERFILE


def test_runtime_stage_drops_root() -> None:
    assert re.search(r"^USER nonroot$", _DOCKERFILE, re.MULTILINE)
    # 65532 is what the compose init-perms one-shot chowns bind mounts to.
    assert "65532" in _DOCKERFILE


def test_healthcheck_is_exec_form() -> None:
    # slim ships neither curl nor wget; a CMD-SHELL healthcheck calling either
    # would report unhealthy forever.
    assert 'CMD ["/app/.venv/bin/python"' in _DOCKERFILE
