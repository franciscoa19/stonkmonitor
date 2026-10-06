"""Online SQLite backup with verification and exclusive publication.

Usage: venv/bin/python backup_db.py stonkmonitor.db backups/stonkmonitor-YYYYMMDD.db
The source is opened read-only; an existing destination is never overwritten.
"""
import argparse
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time

REQUIRED_TABLES = {"db_meta", "iv_condors", "pending_trades", "position_monitor_state", "trade_fills"}


def backup_database(source, destination, timeout=30):
    source, destination = Path(source).resolve(), Path(destination).absolute()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".stonk-backup-", suffix=".db", dir=destination.parent)
    os.close(fd)  # mkstemp supplies mode 0600
    temporary = Path(name)
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup timed out; no backup published")

    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=.05)
                # A WAL source also copies its journal-mode flag. Publish a
                # standalone file whose restore needs no temporary WAL/SHM.
                if dst.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise sqlite3.DatabaseError("Unable to make backup standalone")
        # Reopen the standalone snapshot, as a restore check, before publication.
        with closing(sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True)) as restored:
            if restored.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("Backup failed SQLite integrity check")
            tables = {r[0] for r in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not REQUIRED_TABLES <= tables:
                raise sqlite3.DatabaseError("Backup missing execution/ownership tables")
        with temporary.open("rb") as saved:
            os.fsync(saved.fileno())
        os.link(temporary, destination)  # atomic, refuses races/overwrites
        return destination
    finally:
        temporary.unlink(missing_ok=True)
        Path(str(temporary) + "-wal").unlink(missing_ok=True)
        Path(str(temporary) + "-shm").unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(f"Verified backup: {backup_database(args.source, args.destination)}")
