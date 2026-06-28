#!/usr/bin/env python3
"""
Local web UI for triaging job postings — a faster alternative to the
`applied` / `passed` / `reject` CLI commands.

Launched via `python pipeline.py ui`. It is a thin Flask layer over the existing
pipeline functions and the existing `jobs.db`: it reuses `cmd_mark` / `cmd_reject`
(so repost-chain propagation and the status lift behave exactly like the CLI) and
makes no schema changes. Single-user, local-only — binds to 127.0.0.1.
"""

import json
import webbrowser
from datetime import date

from flask import Flask, jsonify, render_template, request

import pipeline

app = Flask(__name__)

GATE_OPTIONS = pipeline.GATE_NAMES + ["other"]


def _band(score):
    """Match the report's score-band wording (pipeline._render_scored_job)."""
    s = score or 0
    return "strong" if s >= 14 else ("acceptable" if s >= 10 else "likely pass")


def row_to_dict(row, cap):
    """Flatten a jobs row + its eval_json into the fields the UI renders. `cap` is the
    configured max_description_chars — a stored description at that length was truncated."""
    ev = {}
    try:
        ev = json.loads(row["eval_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        ev = {}
    bucket = row["bucket"]
    return {
        "job_url": row["job_url"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "tier": row["tier"],
        "search_name": row["search_name"],
        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "verdict": row["verdict"],
        "failed_gate": row["failed_gate"],
        "fit_score": row["fit_score"],
        "band": _band(row["fit_score"]) if row["fit_score"] is not None else None,
        "bucket": bucket,
        "bucket_label": pipeline.BUCKET_LABELS.get(bucket),
        # Pass the breakdown through as-stored — older rows use a different set of score
        # dimensions, and the report (pipeline._render_scored_job) renders whatever keys exist.
        "score_breakdown": ev.get("score_breakdown") or {},
        "one_line": ev.get("one_line"),
        "flags": ev.get("flags") or [],
        "app_status": row["app_status"],
        "status_date": row["status_date"],
        "filter_source": row["filter_source"],
        "filter_gate": row["filter_gate"],
        "is_repost": bool(row["repost_of"]),
        # Cheap booleans for the "Send to Claude" button — not the description text itself,
        # so the list payload stays small.
        "has_description": bool(row["description"]),
        "truncated": bool(row["description"] and len(row["description"]) >= cap),
    }


def jobs_for_view(conn, view, for_date, cap):
    """Run the query for a view and return a list of UI dicts."""
    if view == "backlog":
        # Only actionable undecided jobs — exclude GATE_FAIL, which the model already
        # hard-rejected (they'd otherwise swamp the list).
        rows = conn.execute(
            "SELECT * FROM jobs WHERE app_status IS NULL AND filter_source IS NULL "
            "AND status='evaluated' AND verdict IN ('PASS','RECRUITER_ONLY') "
            "ORDER BY fit_score DESC"
        ).fetchall()
    elif view in ("applied", "passed"):
        rows = conn.execute(
            "SELECT * FROM jobs WHERE app_status=? ORDER BY status_date DESC, fit_score DESC",
            (view,),
        ).fetchall()
    else:  # "today" (default) — postings first seen on the given date
        rows = conn.execute(
            "SELECT * FROM jobs WHERE substr(first_seen,1,10)=? ORDER BY fit_score DESC",
            (for_date,),
        ).fetchall()
    return [row_to_dict(r, cap) for r in rows]


@app.route("/")
def index():
    cfg = pipeline.load_config()
    return render_template(
        "index.html",
        gates=GATE_OPTIONS,
        today=date.today().isoformat(),
        feedback_url=cfg["settings"].get("feedback_project_url", "") or "",
    )


@app.route("/api/jobs")
def api_jobs():
    view = request.args.get("view", "today")
    for_date = request.args.get("date") or date.today().isoformat()
    cfg = pipeline.load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = pipeline.get_db(cfg)
    try:
        return jsonify(jobs_for_view(conn, view, for_date, cap))
    finally:
        conn.close()


@app.route("/api/clip")
def api_clip():
    """Assemble the clipboard text for one posting (header + JD) to paste into the claude.ai
    project. Kept off the list payload so /api/jobs stays small."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"text": "", "truncated": False}), 400
    cfg = pipeline.load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = pipeline.get_db(cfg)
    try:
        row = conn.execute(
            "SELECT title, company, location, description, job_url FROM jobs WHERE job_url=?",
            (job_url,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row["description"]:
        return jsonify({"text": "", "truncated": False}), 404
    header = (
        f"{row['title'] or '(no title)'} — {row['company'] or '(no company)'}\n"
        f"Location: {row['location'] or 'n/a'}\n"
        f"Posting: {row['job_url']}\n\n"
    )
    text = header + row["description"]
    return jsonify({"text": text, "truncated": len(row["description"]) >= cap})


@app.route("/api/decision", methods=["POST"])
def api_decision():
    body = request.get_json(force=True) or {}
    job_url = body.get("job_url")
    action = body.get("action")
    gate = body.get("gate") or "other"
    if not job_url or action not in ("applied", "passed", "reject", "undo_app", "undo_reject"):
        return jsonify({"ok": False, "message": "bad request"}), 400

    conn = pipeline.get_db(pipeline.load_config())
    try:
        # job_url is unique, so passing it as the CLI's "unique substring" resolves to one row
        # and reuses the exact same propagation / status logic as the command line.
        if action in ("applied", "passed"):
            ok = pipeline.cmd_mark(conn, job_url, action)
        elif action == "undo_app":
            ok = pipeline.cmd_mark(conn, job_url, None)
        elif action == "reject":
            ok = pipeline.cmd_reject(conn, job_url, gate, None, None, False)
        else:  # undo_reject
            ok = pipeline.cmd_reject(conn, job_url, "other", None, None, True)
    finally:
        conn.close()
    return jsonify({"ok": bool(ok), "message": "done" if ok else "no matching posting"})


def serve(host="127.0.0.1", port=5000):
    url = f"http://{host}:{port}"
    print(f"[ui] triage UI at {url}  (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host=host, port=port)


if __name__ == "__main__":
    serve()
