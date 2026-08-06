# ADR-004: pymap for IMAP server

**Status:** Accepted (Phase 9)

**Context:** The project plan specified a read-only IMAP server. The options
were: (a) hand-roll the IMAP protocol over asyncio, (b) use Dovecot with a
Postgres backend (dsync or similar), or (c) use a Python IMAP library with a
pluggable backend.

**Decision:** Use **pymap v0.36.7**, a pure-Python IMAP server with a plugin
backend system. The backend registers via a `pymap.backend` entry point and
implements `MailboxDataInterface`, `MailboxSetInterface`, `LoginInterface`,
and `IdentityInterface`.

**Rationale:**
- Hand-rolling IMAP is months of work — the protocol is large (RFC 3501) and
  clients are unforgiving of deviations
- Dovecot with a Postgres backend would work but adds a second mail store to
  maintain, and the schema is Dovecot-specific
- pymap handles all the protocol details (FETCH, SEARCH, LIST, SELECT, IDLE,
  TLS, SASL auth) and lets us focus on the data access layer
- pymap's `MessageContent.parse()` handles MIME parsing for BODYSTRUCTURE and
  ENVELOPE responses, which are the fiddliest parts of IMAP

**Consequences:**
- The backend is read-only (pymap supports read-only mailboxes natively)
- BODYSTRUCTURE is computed from raw bytes via pymap's MIME parser, cached in
  the database after backfill
- Single-user authentication (pymap supports SASL via pysasl)
- pymap is a relatively young project; upstream changes may require adaptation
