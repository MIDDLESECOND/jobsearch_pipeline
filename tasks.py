"""Chain-scoped next actions for the local job-search workflow.

Tasks are keyed to the role canonical at write time and mapped through that posting's
current chain root on read.  Duplicate merge/unlink therefore behaves like application
events, packet links, and role contacts without rewriting history.
"""

from datetime import date, datetime, timezone


TASK_OPEN = "open"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"
ALL_TASK_STATUSES = (TASK_OPEN, TASK_COMPLETED, TASK_CANCELLED)

_MAX_TITLE = 240
_MAX_NOTE = 2000


def _root_url(row):
    return row["repost_of"] or row["job_url"]


def _chain_urls(conn, row):
    root = _root_url(row)
    return [item[0] for item in conn.execute(
        "SELECT job_url FROM jobs WHERE job_url=? OR repost_of=? ORDER BY job_url",
        (root, root),
    )]


def _text(value, field, limit, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _due_date(value):
    raw = _text(value, "due_date", 10, required=True)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        raise ValueError("due_date must be YYYY-MM-DD") from None
    if parsed.isoformat() != raw:
        raise ValueError("due_date must be YYYY-MM-DD")
    return raw


def _as_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "note": row["note"],
        "due_date": row["due_date"],
        "status": row["status"],
        "interaction_url": row["interaction_url"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "version": row["version"],
    }


def _begin_write(conn):
    """Own one write transaction without disturbing a caller-owned transaction."""
    if conn.in_transaction:
        raise RuntimeError("task mutation requires a clean database connection")
    conn.execute("BEGIN IMMEDIATE")


def add_task(conn, row, *, title: object, due_date: object, note: object = ""):
    """Add one explicit next action and return its transaction-consistent chain snapshot."""
    title = _text(title, "title", _MAX_TITLE, required=True)
    due_date = _due_date(due_date)
    note = _text(note, "note", _MAX_NOTE)
    created_at = datetime.now(timezone.utc).isoformat()
    # Serialize with dupe merge/unlink, then refresh the posting: a row fetched before
    # the transaction may carry an obsolete repost_of after another connection commits.
    _begin_write(conn)
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            raise ValueError("posting no longer exists")
        cur = conn.execute(
            """INSERT INTO job_tasks
               (job_url,interaction_url,title,note,due_date,status,created_at,closed_at,version)
               VALUES (?,?,?,?,?,?,?,NULL,0)""",
            (_root_url(current), current["job_url"], title, note or None, due_date,
             TASK_OPEN, created_at),
        )
        stored = conn.execute(
            "SELECT * FROM job_tasks WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = {"task": _as_dict(stored), "tasks": chain_tasks(conn, current)}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def change_task(conn, row, task_id, action, *, expected_version, due_date=None):
    """Mutate one task if the caller's version is current; return one atomic chain snapshot."""
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        raise ValueError("task_id must be an integer")
    if action not in ("complete", "reopen", "cancel", "snooze"):
        raise ValueError("task action must be complete, reopen, cancel, or snooze")
    if (isinstance(expected_version, bool)
            or not isinstance(expected_version, int) or expected_version < 0):
        raise ValueError("expected_version must be a non-negative integer")
    due_date = _due_date(due_date) if action == "snooze" else None
    # The write lock and fresh jobs row make ownership and version validation atomic with
    # the update. BEGIN lives outside the try so a failed nested-BEGIN never rolls back a
    # transaction owned by the caller.
    _begin_write(conn)
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            raise ValueError("posting no longer exists")
        urls = _chain_urls(conn, current)
        qs = ",".join("?" * len(urls))
        stored = conn.execute(
            f"SELECT * FROM job_tasks WHERE id=? AND job_url IN ({qs})",
            (task_id, *urls),
        ).fetchone()
        if stored is None:
            conn.rollback()
            return None
        if stored["version"] != expected_version:
            raise ValueError("task changed; refresh and retry")
        if action in ("complete", "cancel", "snooze") and stored["status"] != TASK_OPEN:
            raise ValueError("task is not open")
        if action == "reopen" and stored["status"] == TASK_OPEN:
            raise ValueError("task is already open")
        now = datetime.now(timezone.utc).isoformat()
        if action == "snooze":
            changed = conn.execute(
                """UPDATE job_tasks SET due_date=?,status=?,closed_at=NULL,version=version+1
                   WHERE id=? AND version=?""",
                (due_date, TASK_OPEN, task_id, expected_version),
            )
        elif action == "reopen":
            changed = conn.execute(
                """UPDATE job_tasks SET status=?,closed_at=NULL,version=version+1
                   WHERE id=? AND version=?""",
                (TASK_OPEN, task_id, expected_version),
            )
        else:
            status = TASK_COMPLETED if action == "complete" else TASK_CANCELLED
            changed = conn.execute(
                """UPDATE job_tasks SET status=?,closed_at=?,version=version+1
                   WHERE id=? AND version=?""",
                (status, now, task_id, expected_version),
            )
        if changed.rowcount != 1:
            raise RuntimeError("task version changed while holding the write transaction")
        updated = conn.execute(
            "SELECT * FROM job_tasks WHERE id=?", (task_id,)
        ).fetchone()
        result = {"task": _as_dict(updated), "tasks": chain_tasks(conn, current)}
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def task_summaries(conn, rows, *, include_closed=False):
    """Tasks for every current chain represented by a bounded job-row set."""
    roots = {_root_url(row) for row in rows}
    out = {root: [] for root in roots}
    if not roots:
        return out
    root_list = list(roots)
    for start in range(0, len(root_list), 800):
        chunk = root_list[start:start + 800]
        qs = ",".join("?" * len(chunk))
        status_sql = "" if include_closed else "AND t.status=?"
        params = [*chunk] + ([] if include_closed else [TASK_OPEN])
        found = conn.execute(
            f"""SELECT COALESCE(k.repost_of,k.job_url) AS current_root,t.*
                FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
                WHERE COALESCE(k.repost_of,k.job_url) IN ({qs}) {status_sql}
                ORDER BY t.due_date ASC,t.created_at ASC,t.id ASC""",
            tuple(params),
        ).fetchall()
        for task in found:
            out[task["current_root"]].append(_as_dict(task))
    return out


def task_counts(conn, rows):
    """Total task history size per current chain for lazy-history affordances."""
    roots = {_root_url(row) for row in rows}
    out = {root: 0 for root in roots}
    if not roots:
        return out
    root_list = list(roots)
    for start in range(0, len(root_list), 800):
        chunk = root_list[start:start + 800]
        qs = ",".join("?" * len(chunk))
        found = conn.execute(
            f"""SELECT COALESCE(k.repost_of,k.job_url) AS current_root,COUNT(*) AS n
                FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
                WHERE COALESCE(k.repost_of,k.job_url) IN ({qs})
                GROUP BY COALESCE(k.repost_of,k.job_url)""",
            tuple(chunk),
        ).fetchall()
        for item in found:
            out[item["current_root"]] = item["n"]
    return out


def chain_tasks(conn, row, *, include_closed=False):
    return task_summaries(conn, [row], include_closed=include_closed)[_root_url(row)]
