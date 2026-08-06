# ADR-003: Keyset pagination with nulls-last ordering

**Status:** Accepted (Phase 4)

**Context:** The message list view needs pagination. Offset pagination is
unstable (new messages shift offsets) and slow on large tables (the DB still
scans all skipped rows). Keyset pagination (WHERE cursor > last_seen) is
stable and efficient, but requires a careful index design.

Measured on the real export: ~2.7% of messages have no parseable `Date` header,
so `internal_date` is NULL for those rows. A plain `ORDER BY internal_date
DESC` puts NULLs first, which means a keyset walk starting from a real date
never reaches them, and a walk starting from NULL falls off the end immediately.

**Decision:** Use a composite index on `(internal_date DESC NULLS LAST,
raw_sha256 DESC)` and match it exactly in queries. The `raw_sha256` tiebreaker
ensures a deterministic ordering when two messages have the same date. The
`NULLS LAST` clause ensures the NULL-date tail is reachable from any starting
point.

**Consequences:**
- Three query paths: first page (no cursor), page with both cursor values, and
  page through the NULL date tail
- The index is a btree, not a GIN — equality and range queries only
- `query.py` must match the index ordering exactly; the test suite asserts this
