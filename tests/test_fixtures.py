"""The fixture generator is the project's only source of input, so its defects
have to be real ones.

The load-bearing test here is `TestAddressSafety`: it scans generated *bytes*
for anything address-shaped and asserts every hit is an RFC 2606 reserved
domain. Testing the factory's call sites instead would prove nothing — a call
site can be bypassed, and a public repository full of real-looking addresses is
exactly the mistake this project cannot make.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

from gmail_archive.fixtures import MEASURED_RATES, Pathology, generate, is_reserved

# The mbox separator, as the parser will eventually have to find it.
FROM_RE = re.compile(rb"(?m)^From \S+ (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} ")

# Deliberately greedy: it should catch anything a reader would read as an
# address, including ones the generator never meant to emit.
ADDRESS_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def split_mbox(raw: bytes) -> list[bytes]:
    """Split on From_ lines and undo mboxo quoting — the parser's job, in miniature."""
    starts = [m.start() for m in FROM_RE.finditer(raw)]
    out: list[bytes] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(raw)
        body_start = raw.index(b"\n", start) + 1
        out.append(raw[body_start:end].replace(b"\n>From ", b"\nFrom "))
    return out


def generate_one(tmp_path: Path, pathology: Pathology, count: int = 6) -> list[bytes]:
    out = tmp_path / f"{pathology.value}.mbox"
    generate(out, count=count, seed=11, pathologies=[pathology])
    return split_mbox(out.read_bytes())


def parts(raw: bytes) -> Iterator[email.message.Message]:
    yield from email.message_from_bytes(raw).walk()


def depth_of(msg: email.message.Message, level: int = 0) -> int:
    if not msg.is_multipart():
        return level
    return max(
        (
            depth_of(sub, level + 1)
            for sub in msg.get_payload()
            if isinstance(sub, email.message.Message)
        ),
        default=level,
    )


# ── the acceptance criterion: every pathology individually selectable ────────
#
# One predicate per Pathology member. The parametrised test below fails if a
# member is added without a check here, so the menu cannot drift from its proof.

Check = Callable[[list[bytes]], bool]
CHECKS: dict[Pathology, Check] = {}


def check(p: Pathology) -> Callable[[Check], Check]:
    def register(fn: Check) -> Check:
        CHECKS[p] = fn
        return fn

    return register


def _decoded_labels(raw: bytes) -> str:
    value = email.message_from_bytes(raw).get("X-Gmail-Labels")
    if value is None:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


@check(Pathology.LABELS_ABSENT)
def _(msgs: list[bytes]) -> bool:
    return any("X-Gmail-Labels" not in email.message_from_bytes(m) for m in msgs)


@check(Pathology.LABELS_NESTED)
def _(msgs: list[bytes]) -> bool:
    return any("/" in _decoded_labels(m) for m in msgs)


@check(Pathology.LABELS_PUNCTUATED)
def _(msgs: list[bytes]) -> bool:
    # Decoded, not raw. A label set containing non-ASCII pushes the whole header
    # through RFC 2047, which encodes the separating commas as `=2C` — so a
    # naive split(",") on the raw header value silently sees *one* label. The
    # parser has to decode before it splits, and this check follows that order.
    return any('"' in _decoded_labels(m) for m in msgs)


@check(Pathology.DATE_MISSING)
def _(msgs: list[bytes]) -> bool:
    return any("Date" not in email.message_from_bytes(m) for m in msgs)


def _parsed_dates(msgs: list[bytes]) -> list[datetime | None]:
    out: list[datetime | None] = []
    for m in msgs:
        raw = email.message_from_bytes(m).get("Date")
        if raw is None:
            continue
        try:
            out.append(email.utils.parsedate_to_datetime(raw))
        except (ValueError, TypeError):
            out.append(None)
    return out


@check(Pathology.DATE_NAIVE)
def _(msgs: list[bytes]) -> bool:
    return any(d is not None and d.tzinfo is None for d in _parsed_dates(msgs))


@check(Pathology.DATE_UNPARSEABLE)
def _(msgs: list[bytes]) -> bool:
    return any(d is None for d in _parsed_dates(msgs))


@check(Pathology.DATE_FAR_FUTURE)
def _(msgs: list[bytes]) -> bool:
    return any(d is not None and d.year > 2500 for d in _parsed_dates(msgs))


@check(Pathology.MSGID_MISSING)
def _(msgs: list[bytes]) -> bool:
    return any("Message-ID" not in email.message_from_bytes(m) for m in msgs)


@check(Pathology.MSGID_DUPLICATE)
def _(msgs: list[bytes]) -> bool:
    ids = [email.message_from_bytes(m).get("Message-ID") for m in msgs]
    present = [i for i in ids if i]
    return len(present) != len(set(present))


@check(Pathology.QUOTED_FROM)
def _(msgs: list[bytes]) -> bool:
    # split_mbox already unquoted, so this proves the round trip survives it.
    return any(b"\nFrom the desk of the archivist" in m for m in msgs)


@check(Pathology.BARE_FROM)
def _(msgs: list[bytes]) -> bool:
    return any(b"\nFrom the desk of the archivist" in m for m in msgs)


@check(Pathology.HEADER_8BIT)
def _(msgs: list[bytes]) -> bool:
    return any(any(b > 0x7F for b in m.split(b"\n\n", 1)[0]) for m in msgs)


@check(Pathology.HEADER_RFC2047_SPLIT)
def _(msgs: list[bytes]) -> bool:
    return any(b"=?utf-8?b?4oA=?=" in m for m in msgs)


@check(Pathology.BODY_OVER_TSVECTOR)
def _(msgs: list[bytes]) -> bool:
    return any(len(m) > 1024 * 1024 for m in msgs)


@check(Pathology.BODY_NUL)
def _(msgs: list[bytes]) -> bool:
    return any(b"\x00" in m for m in msgs)


@check(Pathology.BODY_TRUNCATED)
def _(msgs: list[bytes]) -> bool:
    # A truncated multipart has lost its closing --boundary-- delimiter.
    for m in msgs:
        msg = email.message_from_bytes(m)
        boundary = msg.get_boundary()
        if boundary and f"--{boundary}--".encode() not in m:
            return True
    return False


@check(Pathology.CHARSET_LEGACY)
def _(msgs: list[bytes]) -> bool:
    legacy = {"iso-8859-1", "windows-1252", "koi8-r", "iso-8859-15", "ansi_x3.4-1968"}
    return any(p.get_content_charset() in legacy for m in msgs for p in parts(m))


@check(Pathology.CHARSET_NONEXISTENT)
def _(msgs: list[bytes]) -> bool:
    return any(p.get_content_charset() == "unicode" for m in msgs for p in parts(m))


@check(Pathology.CHARSET_ABSENT)
def _(msgs: list[bytes]) -> bool:
    return any(
        p.get_content_maintype() == "text" and p.get_content_charset() is None
        for m in msgs
        for p in parts(m)
    )


@check(Pathology.DEEP_NESTING)
def _(msgs: list[bytes]) -> bool:
    return any(depth_of(email.message_from_bytes(m)) >= 5 for m in msgs)


@check(Pathology.BASE64_BAD_PADDING)
def _(msgs: list[bytes]) -> bool:
    for m in msgs:
        for p in parts(m):
            if p.get("Content-Transfer-Encoding") == "base64":
                payload = p.get_payload()
                if isinstance(payload, str) and len(payload.strip()) % 4:
                    return True
    return False


def _attachment_blobs(msgs: list[bytes]) -> list[tuple[str | None, bytes]]:
    out: list[tuple[str | None, bytes]] = []
    for m in msgs:
        for p in parts(m):
            if p.get_content_disposition() == "attachment":
                payload = p.get_payload(decode=True)
                out.append(
                    (p.get_filename(), payload if isinstance(payload, bytes) else b"")
                )
    return out


@check(Pathology.ATTACH_REPEATED)
def _(msgs: list[bytes]) -> bool:
    digests = [hashlib.sha256(b).hexdigest() for _, b in _attachment_blobs(msgs) if b]
    return len(digests) != len(set(digests))


@check(Pathology.ATTACH_ZERO_BYTE)
def _(msgs: list[bytes]) -> bool:
    return any(len(b) == 0 for _, b in _attachment_blobs(msgs))


@check(Pathology.ATTACH_PATH_FILENAME)
def _(msgs: list[bytes]) -> bool:
    return any(".." in (n or "") for n, _ in _attachment_blobs(msgs))


@check(Pathology.HTML_ALTERNATIVE)
def _(msgs: list[bytes]) -> bool:
    # Both halves matter. An HTML part alone would not be `alternative`, and
    # `parse()` distinguishes the HTML *body* from an attached .html file by
    # disposition, so the part must be inline.
    return any(
        b"multipart/alternative" in m and b"text/html" in m and b"<html>" in m
        for m in msgs
    )


@check(Pathology.ATTACH_UNICODE_FILENAME)
def _(msgs: list[bytes]) -> bool:
    return any(any(ord(c) > 127 for c in (n or "")) for n, _ in _attachment_blobs(msgs))


@check(Pathology.ATTACH_OVERSIZED)
def _(msgs: list[bytes]) -> bool:
    return any(len(b) > 25 * 1024 * 1024 for _, b in _attachment_blobs(msgs))


class TestEveryPathologyIsSelectable:
    def test_menu_is_fully_covered_by_checks(self) -> None:
        # Guards the guard: a new Pathology with no predicate would otherwise
        # silently never be verified.
        assert set(CHECKS) == set(Pathology)

    @pytest.mark.parametrize("pathology", list(Pathology), ids=lambda p: p.value)
    def test_present_when_requested(self, tmp_path: Path, pathology: Pathology) -> None:
        count = 2 if pathology is Pathology.ATTACH_OVERSIZED else 6
        msgs = generate_one(tmp_path, pathology, count=count)
        assert len(msgs) == count
        assert CHECKS[pathology](msgs), f"{pathology.value} requested but not present"


class TestAddressSafety:
    def test_no_generated_address_escapes_rfc2606(self, tmp_path: Path) -> None:
        out = tmp_path / "all.mbox"
        # Every pathology at once: the widest surface the generator can produce.
        generate(
            out,
            count=len(Pathology) * 2,
            seed=3,
            pathologies=[p for p in Pathology if p is not Pathology.ATTACH_OVERSIZED],
        )
        found = {m.decode("latin-1") for m in ADDRESS_RE.findall(out.read_bytes())}
        assert found, "no addresses found at all — the scan is not working"
        escaped = sorted(a for a in found if not is_reserved(a))
        assert not escaped, f"addresses outside RFC 2606: {escaped}"

    def test_default_mix_is_also_clean(self, tmp_path: Path) -> None:
        out = tmp_path / "default.mbox"
        generate(out, count=300, seed=5)
        found = {m.decode("latin-1") for m in ADDRESS_RE.findall(out.read_bytes())}
        assert not [a for a in found if not is_reserved(a)]

    def test_no_hostname_leaks_via_message_id(self, tmp_path: Path) -> None:
        # email.utils.make_msgid() would stamp the build host's FQDN into every
        # fixture. This asserts the domains are ours, not the machine's.
        out = tmp_path / "ids.mbox"
        generate(out, count=40, seed=9)
        for raw in split_mbox(out.read_bytes()):
            mid = email.message_from_bytes(raw).get("Message-ID")
            if mid:
                assert is_reserved(mid)


class TestDeterminism:
    def test_same_seed_same_bytes(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.mbox", tmp_path / "b.mbox"
        generate(a, count=50, seed=1234)
        generate(b, count=50, seed=1234)
        assert a.read_bytes() == b.read_bytes()

    def test_different_seed_different_bytes(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.mbox", tmp_path / "b.mbox"
        generate(a, count=50, seed=1)
        generate(b, count=50, seed=2)
        assert a.read_bytes() != b.read_bytes()

    def test_multipart_boundaries_are_not_random(self, tmp_path: Path) -> None:
        # MIMEMultipart picks a random boundary at construction; if that ever
        # leaks through, this is the test that catches it rather than a
        # confusing diff in the byte-comparison test above.
        a, b = tmp_path / "a.mbox", tmp_path / "b.mbox"
        opts = {"count": 4, "seed": 77, "pathologies": [Pathology.DEEP_NESTING]}
        generate(a, **opts)  # type: ignore[arg-type]
        generate(b, **opts)  # type: ignore[arg-type]
        boundaries = re.findall(rb'boundary="([^"]+)"', a.read_bytes())
        assert boundaries
        assert boundaries == re.findall(rb'boundary="([^"]+)"', b.read_bytes())


class TestDefaultMix:
    def test_reproduces_measured_rates(self, tmp_path: Path) -> None:
        # The default mix exists so the parser is tested against a corpus shaped
        # like a real one. Loose bounds: this is a sample, not a proof.
        out = tmp_path / "mix.mbox"
        report = generate(out, count=4000, seed=42)
        for pathology, expected in MEASURED_RATES.items():
            if expected < 0.005:
                continue  # too rare to assert on 4000 messages
            seen = report.pathology_counts.get(pathology.value, 0) / report.count
            assert expected * 0.4 <= seen <= expected * 2.2, (
                f"{pathology.value}: expected ~{expected:.3f}, saw {seen:.3f}"
            )

    def test_rare_pathologies_are_not_in_the_default_mix(self, tmp_path: Path) -> None:
        # Absent from the real export, so absent from the default weighting.
        out = tmp_path / "mix.mbox"
        report = generate(out, count=2000, seed=8)
        for excluded in (
            Pathology.BARE_FROM,
            Pathology.CHARSET_NONEXISTENT,
            Pathology.DEEP_NESTING,
            Pathology.ATTACH_OVERSIZED,
            Pathology.BODY_TRUNCATED,
        ):
            assert excluded.value not in report.pathology_counts


class TestGeneratedFixtureIsWellFormed:
    def test_message_count_matches_request(self, tmp_path: Path) -> None:
        out = tmp_path / "n.mbox"
        generate(out, count=137, seed=2)
        assert len(split_mbox(out.read_bytes())) == 137

    def test_requesting_more_pathologies_than_messages_is_an_error(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="each must appear at least once"):
            generate(
                tmp_path / "x.mbox", count=1, seed=0, pathologies=list(Pathology)[:3]
            )


@pytest.mark.slow
def test_throughput_profile(tmp_path: Path) -> None:
    """100k-message size profile. Not part of the default suite — see pyproject."""
    out = tmp_path / "big.mbox"
    report = generate(out, count=100_000, seed=99)
    assert report.count == 100_000
    assert len(split_mbox(out.read_bytes())) == 100_000
