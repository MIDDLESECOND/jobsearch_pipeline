"""Chain-scoped next-action service tests."""

import re

import pytest

import chain
import tasks
from conftest import make_job


def test_add_list_complete_reopen_snooze_and_cancel(conn):
    row = make_job(conn, job_url="root")
    added = tasks.add_task(
        conn, row, title="  Ask Alex for referral  ", due_date="2026-08-08",
        note="Mention the AI launch role",
    )
    task = added["task"]
    assert task["title"] == "Ask Alex for referral"
    assert task["status"] == tasks.TASK_OPEN
    assert task["interaction_url"] == "root"
    assert added["tasks"] == [task]
    assert tasks.chain_tasks(conn, row) == [task]

    completed_result = tasks.change_task(
        conn, row, task["id"], "complete", expected_version=task["version"])
    completed = completed_result["task"]
    assert completed["status"] == tasks.TASK_COMPLETED and completed["closed_at"]
    assert completed["version"] == task["version"] + 1
    assert completed_result["tasks"] == []
    assert tasks.chain_tasks(conn, row) == []
    assert tasks.chain_tasks(conn, row, include_closed=True)[0]["status"] == "completed"
    with pytest.raises(ValueError, match="task is not open"):
        tasks.change_task(
            conn, row, task["id"], "snooze", expected_version=completed["version"],
            due_date="2026-08-09",
        )

    reopened = tasks.change_task(
        conn, row, task["id"], "reopen", expected_version=completed["version"])["task"]
    assert reopened["status"] == tasks.TASK_OPEN and reopened["closed_at"] is None
    with pytest.raises(ValueError, match="task is already open"):
        tasks.change_task(
            conn, row, task["id"], "reopen", expected_version=reopened["version"])
    snoozed = tasks.change_task(
        conn, row, task["id"], "snooze", expected_version=reopened["version"],
        due_date="2026-08-15")["task"]
    assert snoozed["due_date"] == "2026-08-15"
    cancelled = tasks.change_task(
        conn, row, task["id"], "cancel", expected_version=snoozed["version"])["task"]
    assert cancelled["status"] == tasks.TASK_CANCELLED and cancelled["closed_at"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "", "title is required"),
        ("due_date", "08/08/2026", "YYYY-MM-DD"),
        ("due_date", "2026-02-30", "YYYY-MM-DD"),
        ("note", "x" * 2001, "note exceeds 2000"),
    ],
)
def test_task_validation(conn, field, value, message):
    row = make_job(conn)
    fields = {"title": "Next action", "due_date": "2026-08-08", field: value}
    with pytest.raises(ValueError, match=re.escape(message)):
        tasks.add_task(conn, row, **fields)


def test_change_validation_and_cross_chain_guard(conn):
    left = make_job(conn, job_url="left")
    right = make_job(conn, job_url="right")
    task = tasks.add_task(
        conn, left, title="Left task", due_date="2026-08-08")["task"]

    assert tasks.change_task(
        conn, right, task["id"], "complete",
        expected_version=task["version"],
    ) is None
    with pytest.raises(ValueError, match="task_id must be an integer"):
        tasks.change_task(conn, left, "1", "complete", expected_version=0)
    with pytest.raises(ValueError, match="task action must be"):
        tasks.change_task(
            conn, left, task["id"], "delete", expected_version=task["version"])
    with pytest.raises(ValueError, match="due_date is required"):
        tasks.change_task(
            conn, left, task["id"], "snooze", expected_version=task["version"])
    with pytest.raises(ValueError, match="expected_version must be"):
        tasks.change_task(conn, left, task["id"], "complete", expected_version=None)


def test_tasks_union_on_merge_and_separate_on_unlink(conn):
    left = make_job(conn, job_url="left")
    right = make_job(conn, job_url="right")
    tasks.add_task(conn, left, title="Left task", due_date="2026-08-08")
    tasks.add_task(conn, right, title="Right task", due_date="2026-08-07")

    conn.execute("UPDATE jobs SET repost_of='left' WHERE job_url='right'")
    conn.commit()
    assert [item["title"] for item in tasks.chain_tasks(conn, left)] == [
        "Right task", "Left task"]

    conn.execute("UPDATE jobs SET repost_of=NULL WHERE job_url='right'")
    conn.commit()
    assert [item["title"] for item in tasks.chain_tasks(conn, left)] == ["Left task"]
    assert [item["title"] for item in tasks.chain_tasks(conn, right)] == ["Right task"]


def test_stale_rows_refresh_ownership_across_real_dupe_round_trip(conn):
    early = make_job(conn, job_url="early", first_seen="2026-08-01T00:00:00")
    stale_late = make_job(conn, job_url="late", first_seen="2026-08-02T00:00:00")
    early_task = tasks.add_task(
        conn, early, title="Early task", due_date="2026-08-08")["task"]

    plan, err = chain.dupe_resolve(conn, "late", "early")
    assert err is None
    chain.dupe_commit(conn, plan)
    added_after_merge = tasks.add_task(
        conn, stale_late, title="Merged task", due_date="2026-08-09")["task"]
    owner = conn.execute(
        "SELECT job_url FROM job_tasks WHERE id=?", (added_after_merge["id"],)
    ).fetchone()[0]
    assert owner == "early"

    merged_late = conn.execute("SELECT * FROM jobs WHERE job_url='late'").fetchone()
    ok, _, _, _ = chain.dupe_unlink(conn, merged_late)
    assert ok
    # The stale merged row still says repost_of=early. Refreshing it inside the task write
    # must prevent the now-independent late chain from closing early's task.
    assert tasks.change_task(
        conn, merged_late, early_task["id"], "complete",
        expected_version=early_task["version"],
    ) is None


def test_stale_version_cannot_overwrite_a_newer_snooze(conn):
    row = make_job(conn, job_url="root")
    original = tasks.add_task(
        conn, row, title="Follow up", due_date="2026-08-08")["task"]
    first = tasks.change_task(
        conn, row, original["id"], "snooze",
        expected_version=original["version"], due_date="2026-08-09",
    )["task"]

    with pytest.raises(ValueError, match="task changed; refresh and retry"):
        tasks.change_task(
            conn, row, original["id"], "snooze",
            expected_version=original["version"], due_date="2026-08-15",
        )
    current = tasks.chain_tasks(conn, row)[0]
    assert current["due_date"] == "2026-08-09"
    assert current["version"] == first["version"]


def test_task_mutations_do_not_rollback_a_caller_owned_transaction(conn):
    row = make_job(conn, job_url="root")
    conn.execute("INSERT INTO meta(key,value) VALUES ('caller','pending add')")
    with pytest.raises(RuntimeError, match="clean database connection"):
        tasks.add_task(conn, row, title="Task", due_date="2026-08-08")
    assert conn.in_transaction
    assert conn.execute(
        "SELECT value FROM meta WHERE key='caller'"
    ).fetchone()[0] == "pending add"
    conn.rollback()

    task = tasks.add_task(
        conn, row, title="Task", due_date="2026-08-08")["task"]
    conn.execute("INSERT INTO meta(key,value) VALUES ('caller','pending change')")
    with pytest.raises(RuntimeError, match="clean database connection"):
        tasks.change_task(
            conn, row, task["id"], "complete", expected_version=task["version"])
    assert conn.in_transaction
    assert conn.execute(
        "SELECT value FROM meta WHERE key='caller'"
    ).fetchone()[0] == "pending change"
    conn.rollback()
    assert tasks.chain_tasks(conn, row)[0]["status"] == tasks.TASK_OPEN


def test_task_summaries_are_batched_by_current_root(conn):
    left = make_job(conn, job_url="left")
    relisting = make_job(conn, job_url="relisting", repost_of="left")
    other = make_job(conn, job_url="other")
    tasks.add_task(conn, relisting, title="Chain task", due_date="2026-08-08")
    tasks.add_task(conn, other, title="Other task", due_date="2026-08-09")

    got = tasks.task_summaries(conn, [left, relisting, other])
    assert set(got) == {"left", "other"}
    assert [item["title"] for item in got["left"]] == ["Chain task"]
    assert [item["title"] for item in got["other"]] == ["Other task"]
    assert tasks.task_counts(conn, [left, relisting, other]) == {"left": 1, "other": 1}


def test_schema_is_local_and_indexed(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_tasks)")}
    assert columns == {
        "id", "job_url", "interaction_url", "title", "note", "due_date",
        "status", "created_at", "closed_at", "version",
    }
    indexes = {row[1] for row in conn.execute("PRAGMA index_list(job_tasks)")}
    assert "idx_job_tasks_chain_due" in indexes
    assert "idx_job_tasks_due" in indexes
