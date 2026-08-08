"""Content-addressed blob store on local disk.

Raw message bytes live here, not in Postgres, so a `pg_dump` stays a few GB of
derived metadata instead of carrying the whole archive. A blob's path is derived
from its sha256, so there is no stored path to fall out of sync with the
filesystem, and `verify --deep` is a single pass with no side bookkeeping.

**The write ordering is the entire point of this module:**

    write to `.tmp` in the *same directory* -> fsync the file -> atomic rename
    -> **fsync the containing directory** -> only then insert the row

Every step earns its place:

- Same directory, because `rename(2)` is only atomic within a filesystem.
- fsync the file before the rename, or the rename can be durable while the
  contents are not — leaving a correctly named, partially written blob.
- **fsync the directory after the rename.** Without it the rename itself is not
  durable: the file can survive under its temporary name, or vanish entirely,
  while a committed database row already points at the final one.
- Row last. Blob-then-row can orphan a blob, which `verify` finds and which
  costs disk. Row-then-blob can leave a valid row pointing at nothing, which is
  data loss. Orphans are recoverable; silent absence is not.

Assumes local disk. `plan.md` forbids network filesystems here, and this is the
module that would break first on one — NFS does not give these guarantees.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Two hex characters gives 256 fan-out directories. At a few hundred thousand
# blobs that is a low four-figure count per directory, which every local
# filesystem handles without a second level of nesting.
_FANOUT = 2


@dataclass(frozen=True, slots=True)
class WriteResult:
    sha256: str
    size_bytes: int
    path: Path
    #: False when the blob was already present. Idempotency is a property of
    #: content addressing, not of bookkeeping — re-ingesting writes nothing.
    written: bool


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, sha256: str) -> Path:
        """Derive a blob's path from its hash. No lookup, no stored path."""
        if len(sha256) != 64:
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")
        return self.root / sha256[:_FANOUT] / sha256

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).is_file()

    def put(self, data: bytes, *, sha256: str | None = None) -> WriteResult:
        """Store `data`, returning its digest. Durable before it returns."""
        digest = sha256 or hashlib.sha256(data).hexdigest()
        if sha256 is not None and len(sha256) != 64:
            raise ValueError(f"not a sha256 hex digest: {sha256!r}")

        target = self.path_for(digest)
        if target.is_file():
            return WriteResult(digest, len(data), target, written=False)

        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)

        # NamedTemporaryFile in the *target* directory, so the rename below
        # cannot cross a filesystem boundary.
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".blob")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            _fsync_dir(directory)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return WriteResult(digest, len(data), target, written=True)

    def get(self, sha256: str) -> bytes:
        return self.path_for(sha256).read_bytes()

    def verify(self, sha256: str) -> bool:
        """Re-hash a blob against its own name.

        The payoff of making the content hash the identity: integrity checking
        needs no stored checksum to compare against, because the name *is* the
        checksum.
        """
        try:
            data = self.path_for(sha256).read_bytes()
        except OSError:
            return False
        return hashlib.sha256(data).hexdigest() == sha256

    def iter_blobs(self) -> list[str]:
        """Every digest present on disk, for orphan reconciliation."""
        if not self.root.is_dir():
            return []
        out: list[str] = []
        for shard in sorted(self.root.iterdir()):
            if not shard.is_dir() or len(shard.name) != _FANOUT:
                continue
            for blob in sorted(shard.iterdir()):
                # Skip temporaries from an interrupted write.
                if blob.is_file() and len(blob.name) == 64:
                    out.append(blob.name)
        return out

    def sweep_temporaries(self) -> int:
        """Remove `.tmp-*` files left by a killed run.

        **Only safe while no other ingest is writing.** This used to claim it
        was safe unconditionally, on the grounds that a temporary is never
        referenced by a row — true of a *dead* run, and wrong about a live
        one, whose in-flight temporaries this happily deletes. The worker's
        `os.replace` then fails, and if the sweep lands between the write and
        the rename the blob is simply gone.

        `ingest()` holds an advisory lock across the whole pipeline, which is
        what makes calling this safe there. Anything else calling it needs to
        establish the same thing first.
        """
        removed = 0
        if not self.root.is_dir():
            return 0
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            for leftover in shard.glob(".tmp-*"):
                leftover.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info("swept %d temporary blob(s) from an interrupted run", removed)
        return removed


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable.

    Without this the rename can be lost across a power failure even though the
    file's own contents were fsynced — the directory entry is separate metadata.
    """
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
