# ADR-001: Content-addressed blob store

**Status:** Accepted (Phase 4)

**Context:** The archive stores raw message bytes. A `pg_dump` of the database
should stay small enough to run regularly — it carries derived metadata, not
the full corpus. The raw bytes need to be verified against their content hash
and must survive a database rebuild.

**Decision:** Store raw message bytes on disk in a content-addressed layout
(`data/blobs/{sha256[:2]}/{sha256}`), with only the hash and size in Postgres.
The write ordering is: file fsync → atomic rename → directory fsync → row
insert. This guarantees that a blob on disk has a corresponding database row,
but a database row may point at a blob that was never written (detected by
`verify`).

**Consequences:**
- `pg_dump` stays small (metadata only)
- Blob integrity is verified by filename = sha256 of content
- Orphaned blobs (on disk but not in DB) are harmless; missing blobs (in DB
  but not on disk) are data loss detected by `verify`
- The blob store is a local filesystem; no network filesystem, ever
