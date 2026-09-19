"""Synthetic fixture generation.

Public surface is deliberately small: a `Pathology` enum, `generate()`, and the
address factory's guard predicate. Everything else is an implementation detail
of `generator.py`.
"""

from gmail_archive.fixtures.addresses import RESERVED_DOMAINS, is_reserved
from gmail_archive.fixtures.generator import (
    MEASURED_RATES,
    GenerationReport,
    Pathology,
    generate,
)

__all__ = [
    "MEASURED_RATES",
    "RESERVED_DOMAINS",
    "GenerationReport",
    "Pathology",
    "generate",
    "is_reserved",
]
