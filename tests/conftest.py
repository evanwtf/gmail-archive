"""Shared test setup.

`scripts/` holds standalone hook entry points rather than an installed package,
so it is put on the path here instead of with a sys.path edit inside each test
module.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
