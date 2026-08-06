# ADR-002: mboxrd unquoting

**Status:** Accepted (Phase 3)

**Context:** The mbox format prefixes body lines starting with `From ` with
`>`. The Google Takeout export uses **mboxrd**, where every `>*From ` line is
prefixed with one `>`. The question is what `raw_sha256` hashes: the file bytes
(quoted) or the original RFC822 message (unquoted).

Measured on the real export: 3,855 `>From ` lines, 86 `>>From `, and zero
`>>>From `. All 86 `>>From ` lines sit among unquoted neighbours — the
signature of mboxrd, not mboxo.

**Decision:** Hash the **unquoted** RFC822 message. Strip exactly one `>` from
any `>*From ` line at the start of a body line. Record an
`unquote-ambiguous` warning on lines that carried more than one `>` (86 lines
corpus-wide).

**Consequences:**
- `.eml` export is correct (original RFC822 bytes)
- `.mbox` export re-quotes and still round-trips byte-identically
- 86 lines corpus-wide are flagged as ambiguous, findable later
- The mbox splitter (`mbox.py`) operates on file bytes (it needs `From_`
  separators), while the parser (`parser.py`) unquotes
