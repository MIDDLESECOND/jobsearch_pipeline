"""The DB-open migration path — specifically _rebuild_for_stale_checks.

SQLite freezes CHECK constraints at CREATE TABLE time, so a DB created while an older
STATUSES/VERDICTS vocabulary was current REJECTS newly-added legal values (IntegrityError
in the deliberately-unguarded run stages — every run aborts). get_db must detect the stale
CHECK text and rebuild the table once, preserving rows; pre-CHECK DBs and current-CHECK
DBs must pass through untouched. This is the guard for every FUTURE vocabulary addition,
not just 'repost_evaluated'.
"""

import sqlite3

import chain
import core
from conftest import job_status
from states import STATUSES, STATUS_REPOST_EVALUATED

# The literal 7-value vocabulary that shipped with the first CHECK-bearing schema
# (commit 479b3b1) — frozen here on purpose; do NOT derive it from states.STATUSES.
_OLD_STATUSES = ("new", "evaluated", "needs_manual", "salary_filtered",
                 "rule_filtered", "repost_decided", "error")
_OLD_VERDICTS = ("PASS", "GATE_FAIL", "RECRUITER_ONLY")


def _jobs_ddl(status_ck="", verdict_ck=""):
    """The full historical column set, with optional CHECK clauses — a pre-CHECK DB is this
    shape without them, a 479b3b1-era DB is this shape with the old 7/3-value vocabulary."""
    return f"""
        CREATE TABLE jobs (
            job_url      TEXT PRIMARY KEY,
            title        TEXT, company TEXT, location TEXT, search_name TEXT, tier TEXT,
            date_posted  TEXT, first_seen TEXT, salary_min REAL, salary_max REAL,
            description  TEXT,
            status       TEXT{status_ck},
            verdict      TEXT{verdict_ck},
            failed_gate  TEXT, fit_score INTEGER, bucket INTEGER, eval_json TEXT,
            norm_company TEXT, norm_title TEXT, fingerprint TEXT,
            repost_of    TEXT, repost_source TEXT,
            app_status   TEXT, status_date TEXT,
            filter_source TEXT, filter_gate TEXT, filter_date TEXT, source TEXT
        )
    """


def _make_old_check_db(path):
    """A DB whose jobs table carries the old, smaller CHECK — as a fresh clone at the
    previous release would have created it."""
    conn = sqlite3.connect(path)
    status_ck = " CHECK (status IN (" + ", ".join(f"'{s}'" for s in _OLD_STATUSES) + "))"
    verdict_ck = " CHECK (verdict IN (" + ", ".join(f"'{v}'" for v in _OLD_VERDICTS) + "))"
    conn.execute(_jobs_ddl(status_ck, verdict_ck))
    conn.execute(
        "INSERT INTO jobs (job_url, title, status, verdict, first_seen) "
        "VALUES ('c', 'Canonical', 'evaluated', 'PASS', '2026-06-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO jobs (job_url, title, status, repost_of, first_seen) "
        "VALUES ('r1', 'Relisting', 'new', 'c', '2026-06-02T00:00:00')"
    )
    conn.commit()
    conn.close()


def test_stale_check_db_is_rebuilt_and_accepts_new_statuses(tmp_path):
    path = str(tmp_path / "old.db")
    _make_old_check_db(path)
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        # Rows survived the table swap.
        rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM jobs")}
        assert set(rows) == {"c", "r1"} and rows["c"]["verdict"] == "PASS"
        # The rebuilt CHECK names every current status — the exact write that aborted
        # every run on the stale schema now succeeds through the real pass.
        chain.skip_evaluated_reposts(conn)
        assert job_status(conn, "r1") == STATUS_REPOST_EVALUATED
        # The swap is one-shot: the current DDL text now names every status.
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()[0]
        assert all(f"'{s}'" in sql for s in STATUSES)
        # And the indexes dropped with the old table were recreated by get_db.
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='jobs'")}
        assert {"idx_fingerprint", "idx_repost_of", "idx_status"} <= idx
    finally:
        conn.close()


def test_pre_check_db_is_left_alone(tmp_path):
    # A DB from before the CHECKs existed has no constraint to go stale — the rebuild must
    # not touch it (code-side constants remain its enforcement, per the documented policy).
    path = str(tmp_path / "precheck.db")
    conn = sqlite3.connect(path)
    conn.execute(_jobs_ddl())  # full column set, no CHECKs — the pre-479b3b1 shape
    conn.execute("INSERT INTO jobs (job_url, title, status) VALUES ('x', 'T', 'new')")
    conn.commit()
    conn.close()
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()[0]
        assert "CHECK" not in sql  # no rebuild happened
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()


def test_current_check_db_not_rebuilt_twice(tmp_path, capsys):
    # Idempotence: a fresh current-schema DB must pass the stale-CHECK probe untouched.
    # DDL-text comparison alone is VACUOUS here — a rebuild is a textual fixed point (the
    # renamed table re-rebuilds to byte-identical SQL), so a probe regression that rebuilds
    # on every startup would still pass `before == after`. Assert on the migration's own
    # print instead: no '[migrate] rebuilt' line may appear on either open of a current DB.
    path = str(tmp_path / "fresh.db")
    core.get_db({"settings": {"db_path": path}}).close()
    core.get_db({"settings": {"db_path": path}}).close()
    assert "[migrate] rebuilt" not in capsys.readouterr().out
    # And the stored DDL is the fresh-created form (unquoted name), not a rebuild residue
    # (a RENAME rewrites the stored SQL with a quoted "jobs").
    conn = sqlite3.connect(path)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0]
    conn.close()
    assert '"jobs"' not in sql


def test_rebuild_preserves_hand_added_columns(tmp_path):
    # A single-user tool accumulates ad-hoc columns; the swap must carry them (and their
    # data) into the new table rather than silently dropping them with the old one —
    # including SQL-keyword/spaced names (identifiers must be quoted) and DEFAULTs.
    path = str(tmp_path / "handcol.db")
    _make_old_check_db(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE jobs ADD COLUMN my_notes TEXT")
    conn.execute('ALTER TABLE jobs ADD COLUMN "order" INTEGER DEFAULT 7')
    conn.execute('ALTER TABLE jobs ADD COLUMN "my notes 2" TEXT')
    conn.execute("UPDATE jobs SET my_notes='irreplaceable', \"my notes 2\"='spaced' "
                 "WHERE job_url='c'")
    conn.commit()
    conn.close()
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        row = conn.execute(
            'SELECT my_notes, "order", "my notes 2" FROM jobs WHERE job_url=\'c\'').fetchone()
        assert row["my_notes"] == "irreplaceable"
        assert row["order"] == 7               # keyword-named column + its DEFAULT survived
        assert row["my notes 2"] == "spaced"   # spaced name survived
        # The carried DEFAULT applies to future inserts too.
        conn.execute("INSERT INTO jobs (job_url, status) VALUES ('z', 'new')")
        assert conn.execute('SELECT "order" FROM jobs WHERE job_url=\'z\'').fetchone()[0] == 7
    finally:
        conn.close()


def test_rebuild_refuses_off_vocabulary_values_loudly(tmp_path):
    # A stored value outside the current vocabulary (renamed status, hand-edited typo) would
    # make the copy raise a bare IntegrityError inside get_db — every command bricked with no
    # hint. The rebuild must fail with an actionable message naming the values instead.
    import pytest
    path = str(tmp_path / "badval.db")
    _make_old_check_db(path)
    conn = sqlite3.connect(path)
    # Simulate a vocabulary RENAME's aftermath: swap 'error' for 'gone_status' inside the
    # stored CHECK (writable_schema edits the DDL text without a rebuild), then stamp a row
    # with it. The edited CHECK now lacks 'error' → the probe fires; the row's value is
    # outside the CURRENT vocabulary → the copy would violate the fresh CHECK.
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute("""UPDATE sqlite_master SET sql=replace(sql, "'error'", "'gone_status'")
                    WHERE name='jobs'""")
    conn.commit()  # the sqlite_master UPDATE opens a transaction; close() would roll it back
    conn.execute("PRAGMA writable_schema=OFF")
    conn.close()
    conn = sqlite3.connect(path)  # reopen so the edited schema is re-read
    conn.execute("UPDATE jobs SET status='gone_status' WHERE job_url='r1'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="gone_status"):
        core.get_db({"settings": {"db_path": path}}).close()


def test_outcome_columns_and_events_table_added_additively(tmp_path):
    # A pre-outcome-tracking DB (the full historical column set, no CHECKs) must gain the
    # three cache columns and the app_events table on open, with existing applied rows left
    # untouched: outcome_status NULL on an applied row MEANS "no response recorded" — the
    # follow-up bucket — so there is deliberately no backfill.
    path = str(tmp_path / "preoutcome.db")
    conn = sqlite3.connect(path)
    conn.execute(_jobs_ddl())
    conn.execute("INSERT INTO jobs (job_url, title, status, app_status, status_date) "
                 "VALUES ('ap', 'T', 'evaluated', 'applied', '2026-06-01')")
    conn.commit()
    conn.close()
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        row = conn.execute(
            "SELECT app_status, status_date, outcome_status, outcome_date, resume_variant, "
            "channel FROM jobs WHERE job_url='ap'").fetchone()
        assert row["app_status"] == "applied" and row["status_date"] == "2026-06-01"
        assert row["outcome_status"] is None and row["resume_variant"] is None
        assert row["channel"] is None
        # The events table + its index exist and accept the service core's write.
        import chain
        ok, _, _, _ = chain.record_event(conn, conn.execute(
            "SELECT * FROM jobs WHERE job_url='ap'").fetchone(), "interview", "2026-06-12")
        assert ok
        assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 1
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='app_events'")}
        assert "idx_app_events_job_url" in idx
    finally:
        conn.close()


def test_stale_check_rebuild_carries_outcome_columns(tmp_path):
    # The stale-CHECK table swap runs AFTER the column migrations, so a 479b3b1-era DB must
    # come out of the rebuild with the outcome columns present and writable (they ride the
    # same hand-added-column carry as any post-DDL ALTER).
    path = str(tmp_path / "oldcheck.db")
    _make_old_check_db(path)
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        conn.execute("UPDATE jobs SET outcome_status='offer', outcome_date='2026-07-01', "
                     "resume_variant='v1' WHERE job_url='c'")
        row = conn.execute("SELECT outcome_status, resume_variant FROM jobs "
                           "WHERE job_url='c'").fetchone()
        assert row["outcome_status"] == "offer" and row["resume_variant"] == "v1"
    finally:
        conn.close()


def test_connections_run_in_wal_mode(tmp_path):
    # connect_db switches the DB to WAL so a slow concurrent reader (the UI's full-table
    # view scan) can never block an eval write past the busy timeout — the failure mode
    # that discarded a paid eval result mid-run. (Persistence of the mode in the file is
    # SQLite's own contract — only the switch itself is ours to pin.)
    path = str(tmp_path / "wal.db")
    conn = core.get_db({"settings": {"db_path": path}})
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_wal_conversion_lock_loss_degrades_gracefully(tmp_path, monkeypatch, capsys):
    # If the one-time WAL conversion loses its lock race, connect_db must hand back a
    # usable rollback-mode connection and warn once — not raise, and not stall on the 30s
    # busy timeout. A concurrent write lock makes the conversion fail fast.
    monkeypatch.setattr(core, "_wal_warned", False)
    path = str(tmp_path / "locked.db")
    holder = sqlite3.connect(path)
    holder.execute("CREATE TABLE t (x)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")  # write lock: the mode switch cannot proceed
    try:
        conn = core.connect_db({"settings": {"db_path": path}})
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0  # still usable
        finally:
            conn.close()
    finally:
        holder.rollback()
        holder.close()
    assert "[db] not in WAL mode" in capsys.readouterr().err
    # And the busy timeout was restored after the capped conversion attempt: a second
    # connect (lock now released) converts, proving the degrade was per-attempt, not sticky.
    conn = core.connect_db({"settings": {"db_path": path}})
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
