"""Build metadata, threaded in from the image build.

The values are baked into both Dockerfile stages as ENV. Build args do not
cross stage boundaries, so each stage re-declares its ARGs; if that wiring
breaks, these fall back to "unknown" rather than failing to import.
"""

from __future__ import annotations

import os
import platform
import sys
from typing import Final

_UNKNOWN: Final = "unknown"


def build_info() -> dict[str, str]:
    """Return build/runtime identity for `/version` and `gmail-archive version`."""
    return {
        "version": os.environ.get("GMAIL_ARCHIVE_VERSION") or _UNKNOWN,
        "commit": os.environ.get("GMAIL_ARCHIVE_COMMIT") or _UNKNOWN,
        "build_time": os.environ.get("GMAIL_ARCHIVE_BUILD_TIME") or _UNKNOWN,
        # The Chainguard base images are only published at :latest on the free
        # tier, so the interpreter minor version can move under a rebuild.
        # Report it rather than assuming it matches requires-python.
        "python": platform.python_version(),
        "executable": sys.executable,
    }
