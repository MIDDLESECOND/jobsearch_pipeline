"""Local, spreadsheet-safe exports derived from the authoritative SQLite state.

This is a role-summary export, not a backup: it intentionally excludes descriptions,
contact details, notes, event history, and material bytes. A complete evidence backup
still needs jobs.db and application_materials/ as one unit.
"""

import csv
from datetime import datetime, timezone
from io import StringIO

from chain import effective_decisions
from interviews import interview_summaries
from tasks import task_summaries
from watchlist import star_summaries


ROLE_EXPORT_FIELDS = (
    "chain_root", "title", "company", "location", "sources", "posting_urls",
    "first_seen", "date_posted", "verdict", "fit_score", "workflow_status",
    "starred", "starred_at", "application_status", "status_date", "rejection_gate", "outcome_status",
    "outcome_date", "resume_variant", "channel", "open_tasks", "next_task_due",
    "upcoming_interviews", "next_interview_at",
)


def _safe_cell(value):
    """Neutralize spreadsheet formulas while leaving ordinary Unicode/CSV quoting intact."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    visible = value.lstrip(" \t\r\n")
    if visible.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def roles_csv(conn, *, now=None):
    """Return one UTF-8-BOM CSV row per current role chain under one read snapshot."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        roots = conn.execute(
            "SELECT * FROM jobs WHERE repost_of IS NULL ORDER BY first_seen DESC,job_url"
        ).fetchall()
        decisions = effective_decisions(conn, roots)
        open_tasks = task_summaries(conn, roots)
        upcoming = interview_summaries(conn, roots, now=now)
        stars = star_summaries(conn, roots)
        members = conn.execute(
            """SELECT job_url,COALESCE(repost_of,job_url) AS root,source,first_seen
               FROM jobs ORDER BY root,first_seen,job_url"""
        ).fetchall()
        by_root = {row["job_url"]: [] for row in roots}
        for member in members:
            if member["root"] in by_root:
                by_root[member["root"]].append(member)

        output = StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=ROLE_EXPORT_FIELDS, lineterminator="\r\n")
        writer.writeheader()
        for row in roots:
            root = row["job_url"]
            dec = decisions[root]
            chain_members = by_root[root]
            role_tasks = open_tasks[root]
            scheduled = upcoming[root]
            workflow_status = (dec["app_status"] or
                               ("rejected" if dec["reject"] else "undecided"))
            values = {
                "chain_root": root,
                "title": row["title"],
                "company": row["company"],
                "location": row["location"],
                "sources": " | ".join(sorted({m["source"] for m in chain_members
                                                if m["source"]})),
                "posting_urls": " | ".join(m["job_url"] for m in chain_members),
                "first_seen": min((m["first_seen"] for m in chain_members
                                   if m["first_seen"]), default=""),
                "date_posted": row["date_posted"],
                "verdict": dec["chain_verdict"],
                "fit_score": dec["chain_fit_score"],
                "workflow_status": workflow_status,
                "starred": stars[root]["starred"],
                "starred_at": stars[root]["starred_at"],
                "application_status": dec["app_status"],
                "status_date": dec["status_date"],
                "rejection_gate": dec["filter_gate"] if dec["reject"] else None,
                "outcome_status": dec["outcome_status"],
                "outcome_date": dec["outcome_date"],
                "resume_variant": dec["resume_variant"],
                "channel": dec["channel"],
                "open_tasks": len(role_tasks),
                "next_task_due": role_tasks[0]["due_date"] if role_tasks else None,
                "upcoming_interviews": len(scheduled),
                "next_interview_at": scheduled[0]["starts_at"] if scheduled else None,
            }
            writer.writerow({key: _safe_cell(values[key]) for key in ROLE_EXPORT_FIELDS})
        result = "\ufeff" + output.getvalue()
        if owns_snapshot:
            conn.commit()
        return result
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise
