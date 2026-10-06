"""Backup a live WAL database and restore strategy ownership from the snapshot."""
import asyncio
import sqlite3

import pytest

from backup_db import backup_database
from db import Database
from test_execution_safety import database, condor


async def test_live_backup_preserves_ownership_namespace_and_pending_exit(database, tmp_path):
    await database._conn.execute("PRAGMA wal_autocheckpoint=0")
    original = await condor(database)
    namespace = await database.order_namespace()
    state = {"pending": {"client_order_id": "stable-exit", "qty": 1}}
    await database.save_position_monitor_state("AAPL", state)
    snapshot = tmp_path / "backups" / "snapshot.db"
    await asyncio.to_thread(backup_database, database.path, snapshot)
    assert snapshot.stat().st_mode & 0o777 == 0o600
    restored = Database(snapshot)
    await restored.connect()
    try:
        assert await restored.order_namespace() == namespace
        assert await restored.get_active_condors() == [original]
        assert await restored.get_position_monitor_states() == {"AAPL": state}
        assert await restored.get_active_condor_leg_symbols() == {"SC", "LC", "SP", "LP"}
    finally:
        await restored.close()
    with pytest.raises(FileExistsError):
        backup_database(database.path, snapshot)
    assert not list(snapshot.parent.glob(".stonk-backup-*"))


def test_backup_rejects_missing_or_wrong_database_without_publishing(tmp_path):
    missing, snapshot = tmp_path / "missing.db", tmp_path / "snapshot.db"
    with pytest.raises(FileNotFoundError):
        backup_database(missing, snapshot)
    assert not missing.exists() and not snapshot.exists()
    with sqlite3.connect(missing) as wrong:
        wrong.execute("CREATE TABLE irrelevant (value TEXT)")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(missing, snapshot)
    assert not snapshot.exists() and not list(tmp_path.glob(".stonk-backup-*"))
