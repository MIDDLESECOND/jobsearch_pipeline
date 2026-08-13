"""core.prewarm_db — the sequential OS-cache pre-read app.serve runs before opening the
browser. The observable contract is small: it reads the db plus whatever WAL/SHM sidecars
exist (returning the byte count serve() prints), skips absent sidecars silently, and a
missing database is a 0-byte no-op, never an error — get_db creates the file later."""

import core


def _cfg(tmp_path):
    return {"settings": {"db_path": str(tmp_path / "test.db")}}


def test_prewarm_reads_db_and_existing_sidecars(tmp_path):
    (tmp_path / "test.db").write_bytes(b"m" * 3000)
    (tmp_path / "test.db-wal").write_bytes(b"w" * 400)
    # no -shm — a cleanly closed DB has neither sidecar, and partial presence must not raise
    assert core.prewarm_db(_cfg(tmp_path)) == 3400


def test_prewarm_on_missing_db_is_a_noop(tmp_path):
    assert core.prewarm_db(_cfg(tmp_path)) == 0
