"""Shared test setup.

`scripts/` holds standalone hook entry points rather than an installed package,
so it is put on the path here instead of with a sys.path edit inside each test
module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


#: A three-message mbox, generated rather than committed.
#:
#: This used to live at `tests/fixtures/simple.mbox` — untracked, because
#: `.gitignore` excludes `*.mbox` and the pre-commit guard rejects them, both
#: deliberately. So four tests depended on a file that existed only on the
#: machine that first wrote it: green locally, broken for anyone cloning, and
#: invisible until CI ran on a clean checkout.
#:
#: Generating it keeps the "no .mbox in git, ever" rule intact and makes the
#: tests self-contained. Addresses are RFC 2606 reserved domains.
SIMPLE_MBOX = b"""From test@example.com Mon Jan 01 00:00:00 2024
Subject: First message
From: Alice <alice@example.com>
To: Bob <bob@example.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Message-ID: <msg1@example.com>

This is the first message.
From test@example.com Mon Jan 02 00:00:00 2024
Subject: Second message
From: Bob <bob@example.com>
To: Alice <alice@example.com>
Date: Tue, 2 Jan 2024 00:00:00 +0000
Message-ID: <msg2@example.com>

This is the second message.
From test@example.com Mon Jan 03 00:00:00 2024
Subject: Third message
From: Charlie <charlie@example.com>
To: Alice <alice@example.com>
Date: Wed, 3 Jan 2024 00:00:00 +0000
Message-ID: <msg3@example.com>

This is the third message.
"""


@pytest.fixture
def simple_mbox(tmp_path: Path) -> Path:
    """Path to a freshly written three-message mbox."""
    path = tmp_path / "simple.mbox"
    path.write_bytes(SIMPLE_MBOX)
    return path
