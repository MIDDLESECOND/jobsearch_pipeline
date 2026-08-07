"""Explicit, chain-scoped starred-role state."""

import sqlite3

import pytest

import chain
import core
from conftest import make_job
import watchlist


def test_star_unstar_and_chain_summaries(conn):
    root = make_job(conn, job_url="root")
    relist = make_job(conn, job_url="relist", repost_of="root")

    starred = watchlist.set_starred(
        conn, relist, True, expected_starred=False, expected_version=0,
    )
    assert starred["starred"] is True
    assert starred["affected"] == ["relist", "root"]
    assert conn.execute("SELECT job_url FROM role_stars").fetchone()[0] == "root"
    summaries = watchlist.star_summaries(conn, [root, relist])
    assert summaries["root"]["starred"] is True
    assert summaries["relist"]["starred"] is True

    cleared = watchlist.set_starred(
        conn, root, False, expected_starred=True,
        expected_version=starred["star_version"],
    )
    assert cleared["starred"] is False
    tombstone = conn.execute(
        "SELECT starred,version FROM role_stars WHERE job_url='root'"
    ).fetchone()
    assert tombstone["starred"] == 0
    assert tombstone["version"] == cleared["star_version"]


def test_star_timestamps_are_strictly_increasing(conn):
    first = make_job(conn, job_url="first")
    second = make_job(conn, job_url="second")
    future = "2999-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO role_stars(job_url,starred_at) VALUES (?,?)",
        (first["job_url"], future),
    )
    conn.commit()

    second_result = watchlist.set_starred(
        conn, second, True, expected_starred=False, expected_version=0,
    )

    assert second_result["starred_at"] > future


def test_merge_unlink_preserves_canonical_at_write_ownership(conn):
    early = make_job(conn, job_url="early", first_seen="2026-08-01T00:00:00")
    late = make_job(conn, job_url="late", first_seen="2026-08-02T00:00:00")
    watchlist.set_starred(
        conn, early, True, expected_starred=False, expected_version=0,
    )

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)
    assert watchlist.star_summaries(conn, [late])["late"]["starred"] is True

    merged_late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    assert chain.dupe_unlink(conn, merged_late)[0]
    summaries = watchlist.star_summaries(conn, [early, merged_late])
    assert summaries["early"]["starred"] is True
    assert summaries["late"]["starred"] is False


def test_stale_premerge_row_writes_the_current_canonical(conn):
    early = make_job(conn, job_url="early", first_seen="2026-08-01T00:00:00")
    stale_late = make_job(conn, job_url="late", first_seen="2026-08-02T00:00:00")
    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)

    watchlist.set_starred(
        conn, stale_late, True, expected_starred=False, expected_version=0,
    )

    assert conn.execute("SELECT job_url FROM role_stars").fetchone()[0] == "early"


def test_stale_state_and_caller_transaction_are_refused(conn):
    row = make_job(conn, job_url="root")
    starred = watchlist.set_starred(
        conn, row, True, expected_starred=False, expected_version=0,
    )
    with pytest.raises(ValueError, match="star changed; refresh and retry"):
        watchlist.set_starred(
            conn, row, False, expected_starred=False, expected_version=0,
        )

    conn.execute("INSERT INTO meta(key,value) VALUES ('caller','pending')")
    with pytest.raises(RuntimeError, match="clean database connection"):
        watchlist.set_starred(
            conn, row, False, expected_starred=True,
            expected_version=starred["star_version"],
        )
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM meta WHERE key='caller'").fetchone()[0] == "pending"
    conn.rollback()


def test_schema_is_small_and_indexed(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(role_stars)")}
    assert columns == {"job_url", "starred_at", "starred", "version"}
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(role_stars)")}
    assert "idx_role_stars_at" in indexes


def test_stale_star_and_unstar_are_refused_after_both_aba_sequences(conn):
    row = make_job(conn, job_url="root")

    first_star = watchlist.set_starred(
        conn, row, True, expected_starred=False, expected_version=0,
    )
    first_unstar = watchlist.set_starred(
        conn, row, False, expected_starred=True,
        expected_version=first_star["star_version"],
    )
    second_star = watchlist.set_starred(
        conn, row, True, expected_starred=False,
        expected_version=first_unstar["star_version"],
    )
    with pytest.raises(ValueError, match="star changed; refresh and retry"):
        watchlist.set_starred(
            conn, row, False, expected_starred=True,
            expected_version=first_star["star_version"],
        )

    second_unstar = watchlist.set_starred(
        conn, row, False, expected_starred=True,
        expected_version=second_star["star_version"],
    )
    with pytest.raises(ValueError, match="star changed; refresh and retry"):
        watchlist.set_starred(
            conn, row, True, expected_starred=False, expected_version=0,
        )
    assert second_unstar["starred"] is False


def test_legacy_star_rows_migrate_to_version_one_active_markers():
    legacy = sqlite3.connect(":memory:")
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute(
            "CREATE TABLE role_stars (job_url TEXT PRIMARY KEY,starred_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO role_stars VALUES ('root','2026-08-01T00:00:00+00:00')"
        )

        core._migrate_role_stars(legacy)

        row = legacy.execute(
            "SELECT starred,version FROM role_stars WHERE job_url='root'"
        ).fetchone()
        assert row["starred"] == 1 and row["version"] == 1
    finally:
        legacy.close()
