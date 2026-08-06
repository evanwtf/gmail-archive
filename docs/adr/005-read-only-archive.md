# ADR-005: Read-only archive

**Status:** Accepted (Phase 1)

**Context:** The archive is a permanent record of exported mail. The data
should never be modified after ingest — not by the web UI, not by the IMAP
server, not by any tool. Mutability would introduce complexity (conflict
resolution, flag sync, deletion propagation) with no benefit for an archive
that is, by definition, a snapshot.

**Decision:** Every access path is read-only:
- The web UI serves data but has no compose, delete, or flag-toggle endpoints
- The IMAP server raises `MailboxReadOnly` on APPEND, COPY, MOVE, DELETE, and
  flag updates
- The CLI has no delete or modify commands
- The only write path is the ingest pipeline, which is insert-only (idempotent
  via `ON CONFLICT DO NOTHING`)

**Consequences:**
- No conflict resolution needed
- No flag sync with Gmail
- No deletion propagation
- The archive is append-only; mistakes require a database rebuild from the
  original mbox export
- The `verify` command detects data loss but cannot repair it
