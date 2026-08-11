# Getting started

From nothing to a searchable archive of your mail. The whole path is here in
order; nothing branches until the end.

The long pole is Google, not this software. Requesting the export takes hours
to days, so **start with step 1 and come back**.

---

## 1. Request the export from Google Takeout

Go to [takeout.google.com](https://takeout.google.com):

1. **Deselect all**, then select **Mail** only. Everything else is wasted
   download.
2. Under Mail, leave "All Mail data included" unless you want to skip labels
   like Spam or Trash. Including them is recommended — you can filter later,
   and you cannot un-exclude without a new export.
3. Export once, `.zip` or `.tgz`, and set the split size to the largest
   offered (50 GB).

That last setting matters. **Google splits large exports into multiple
files**, and if it splits, you get several archives that each contain part of
one `.mbox` — you will need all of them, and you ingest each resulting `.mbox`
separately.

Google emails you when it is ready. Expect hours; a large mailbox can take a
day or more.

When it arrives, extract it. You are looking for:

```
Takeout/Mail/All mail Including Spam and Trash.mbox
```

**Keep this file.** You need it again if the archive is ever rebuilt, and
requesting a fresh export later gives you a *different* file — mail added
since, mail deleted since.

---

## 2. Prerequisites

- **Docker** with Compose. That is all you need to run it.
- **[uv](https://docs.astral.sh/uv/)** and Python 3.13, only for the
  `set-password` step below and for development.

Postgres comes from Compose. You do not install it.

---

## 3. Set up

```bash
git clone https://github.com/evanwtf/gmail-archive.git
cd gmail-archive
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` to any long random string:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

> **Avoid `$` in any `.env` value.** Compose interprets it as a variable
> reference and the value reaches the container mangled. This is not
> hypothetical — it silently broke authentication during development, and the
> only symptom was every correct password being rejected.

---

## 4. Set a password for the web UI

```bash
uv run gmail-archive set-password
```

It prompts without echoing and prints one line. Put that line in `.env`,
replacing the empty `GMAIL_ARCHIVE_WEB_PASSWORD_HASH=`.

**Do not skip this.** Without it the UI is served with no authentication at
all, and Compose publishes it on every network interface — anyone who can
reach the port can read your entire mail history. The UI shows a red
`NO PASSWORD SET` warning until you do.

---

## 5. Start it

```bash
docker compose pull      # published image; no build needed
docker compose up -d
docker compose run --rm web migrate
curl localhost:8000/healthz
```

`{"status":"ok"}` means it is up. `migrate` applies the database schema and
must run before the first ingest.

---

## 6. Ingest your mail

Put the `.mbox` where Compose can see it:

```bash
cp "/path/to/Takeout/Mail/All mail Including Spam and Trash.mbox" ./data/mbox/
docker compose --profile ingest run --rm ingest "/mbox/All mail Including Spam and Trash.mbox"
```

**If Takeout gave you several `.mbox` files, run this once per file.** They
share one archive; a message appearing in two files is stored once.

### More than one Gmail account

Add `--account you@gmail.com` and run the ingest once per account:

```bash
docker compose --profile ingest run --rm ingest --account you@gmail.com "/mbox/..."
```

Omit it for a single mailbox and everything lands in a default account.

The flag matters more than it looks. Messages are stored by content hash, so
the *same* message in two accounts is one row — and the account is the only
thing recording that it arrived twice, and the only thing that can hold
different labels for it in each. Without the flag a second export merges into
the first irreversibly.

There is no account switcher in the UI yet
([#54](https://github.com/evanwtf/gmail-archive/issues/54)); the data is kept
correctly now so the view can be added later without re-ingesting.

### How long

Measured on this project's reference hardware (Intel i3-7100, 4 threads),
ingesting the real 18.9 GB export:

**277,020 messages in 42 minutes 54 seconds** — 108 messages/second, 7.3 MB/s.

Throughput is bound by bytes, not message count, so scale from the size of
your `.mbox` rather than the number of messages in it. The ingest logs live
`msg/s` and `MiB/s` at every checkpoint, so you can extrapolate a few seconds
in.

Budget memory too: peak RSS was 7.8 GB, most of it the memory-mapped export
and therefore reclaimable, but a machine with 4 GB and no swap will struggle.

It is resumable: if it is killed, run the same command again and it continues
from its checkpoint. Only one ingest may run at a time; a second refuses
rather than corrupting the first.

---

## 7. Classify senders

```bash
docker compose run --rm web analyze
```

About five seconds for a few hundred thousand messages. This decides, per
sender, whether mail is correspondence or automated — and it matters more than
it sounds: **on the reference archive, 82% of all mail is bulk.** Without it,
the People and Trends pages have nothing to show and every message counts as
human.

Re-run it after any later ingest.

---

## 8. Check it landed

```bash
docker compose run --rm web verify --deep
```

Worth the few minutes the first time. `verify` reconciles the database against
the blob store; `--deep` also re-hashes every blob against its own filename,
which works because the name *is* the checksum ([ADR-001](adr/001-blob-store.md)).

Every counter should be zero except the four totals, which should agree:

```json
{
  "messages_in_db": 277020, "blobs_on_disk": 277020,
  "orphaned_blobs": 0, "missing_blobs": 0,
  "deep_corrupt": 0, "sighting_mismatch": 0
}
```

`missing_blobs` above zero is the one that matters: those messages have a row
and no body, and the UI will not tell you — a missing blob renders as an empty
message rather than an error.

---

## 9. Use it

Open **http://localhost:8000** and sign in.

- **Inbox** — the front door, Gmail's own labels and categories
- **Search** — the box at the top. It covers subject and body text; the
  operators below reach everything else
- **People** — who you actually correspond with, who just mails you, and who
  you have lost touch with
- **Trends** — how your mail changed over the years
- **Stats** — what is in the archive and where the disk went
- **imported** chip, top right — when the archive was last built, and a link
  to `/imports` for the runs behind it
- **?** in the top right — the full search syntax, always current

Search operators worth knowing immediately:

```
from:amazon                        sender contains this
to:someone@example.com             any recipient
subject:invoice                    subject line
before:2026-01-01  after:2020-06-01  on:2026-07-30
label:"Bank Alerts"                exact Gmail label
has:attachment
is:unread   is:starred   is:important
"exact phrase"   invoice -receipt   invoice or receipt
```

They combine: `from:amazon has:attachment after:2025-01-01 refund`.

---

## 10. Optional: IMAP

Read the archive in a real mail client.

```bash
# .env needs GMAIL_ARCHIVE_IMAP_PASSWORD set first
docker compose run --rm web imap-backfill    # ~40 minutes for 277k messages
docker compose --profile imap up -d imap
```

Safe to re-run after a later ingest: it assigns UIDs only to messages that
do not have one yet, and never renumbers an existing one.

Then connect to `localhost:1143`, username `archive`, no encryption. Gmail
labels appear as folders. It is strictly read-only.

One caveat: every message shows as read and none as flagged, because `Seen` is
applied unconditionally — Gmail's `Unread` and `Starred` labels do not reach
the client ([#58](https://github.com/evanwtf/gmail-archive/issues/58)).

Published on loopback only, deliberately: one shared password and no TLS.

---

## Troubleshooting

**Every login fails, including the right password.**
Check for a `$` in your `.env` values — Compose interpolates it and the
container receives something different from what you wrote.

**The UI shows a red `NO PASSWORD SET` chip.**
`GMAIL_ARCHIVE_WEB_PASSWORD_HASH` is empty. See step 4. Until you fix it, the
archive is readable by anyone on your network.

**Searching feels slow right after ingesting.**
Ingest refreshes planner statistics itself, but a full vacuum after a large
import also builds the visibility map:
`docker compose exec postgres psql -U gmail_archive -d "$GMAIL_ARCHIVE_DB" -c "vacuum (analyze) messages, blobs, labels, attachments"`

**"another ingest is already running against this database".**
Exactly what it says. Wait, or check
`select * from ingest_runs where status = 'running'`.

**`docker compose run --rm web python ...` does not work.**
The image's entrypoint *is* the CLI, so the subcommand is the argument:
`docker compose run --rm web stats`. To reach Postgres directly, use
`docker compose exec postgres psql -U gmail_archive -d "$GMAIL_ARCHIVE_DB"`.

**People and Trends are empty.**
Run `analyze` (step 7).

**Nothing at localhost:8000.**
`docker compose logs web`. If the database is unreachable the UI still serves
and reports it rather than failing blank.

---

## What next

- [README](../README.md) — command reference, architecture, and the current
  list of known defects
- [runbook.md](runbook.md) — verifying integrity, exporting, backups,
  restoring a single message
- [Known defects](../README.md#known-defects) — worth reading before you rely
  on this for anything
