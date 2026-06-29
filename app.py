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
        # The canonical original's decision, so the UI can derive a relisting's effective status
        # (its own app_status is NULL — only the canonical carries the decision). Mirrors the
        # report's _repost_info; the client recomputes "effective" after a decision (see patchJob).
        "chain_app_status": row["c_app_status"],
        "chain_filter_source": row["c_filter_source"],
        "chain_status_date": row["c_status_date"],
        # Cheap booleans for the "Send to Claude" button — not the description text itself,
        # so the list payload stays small.
        "has_description": bool(row["description"]),
        "truncated": bool(row["description"] and len(row["description"]) >= cap),
    }


def jobs_for_view(conn, view, for_date, cap):
    """Run the query for a view and return a list of UI dicts. Every query LEFT JOINs the
    canonical original of a repost chain (c) so row_to_dict can read the c_* decision columns
    uniformly and surface a relisting's effective (chain) decision."""
    # SELECT j.*, plus the canonical's decision columns aliased for row_to_dict's chain_* passthrough.
    cols = ("j.*, c.app_status AS c_app_status, c.status_date AS c_status_date, "
            "c.filter_source AS c_filter_source")
    join = " FROM jobs j LEFT JOIN jobs c ON c.job_url = j.repost_of "
    if view == "backlog":
        # Only actionable undecided jobs — exclude GATE_FAIL, which the model already
        # hard-rejected (they'd otherwise swamp the list). Also exclude relistings whose chain
        # is already decided (a legacy evaluated repost the skip-eval pass never retro-touched).
        rows = conn.execute(
            "SELECT " + cols + join +
            "WHERE j.app_status IS NULL AND j.filter_source IS NULL "
            "AND j.status='evaluated' AND j.verdict IN ('PASS','RECRUITER_ONLY') "
            "AND (j.repost_of IS NULL OR (c.app_status IS NULL AND c.filter_source IS NULL)) "
            "ORDER BY j.fit_score DESC"
        ).fetchall()
    elif view in ("applied", "passed"):
        rows = conn.execute(
            "SELECT " + cols + join +
            "WHERE j.app_status=? ORDER BY j.status_date DESC, j.fit_score DESC",
            (view,),
        ).fetchall()
    else:  # "today" (default) — postings first seen on the given date
        rows = conn.execute(
            "SELECT " + cols + join +
            "WHERE substr(j.first_seen,1,10)=? ORDER BY j.fit_score DESC",
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
        # The decision propagates across a repost chain (cmd_mark/cmd_reject update the canonical
        # original too). Compute that target set so the UI can update every affected card, not just
        # the one clicked — repost_of is static, so resolving it before the write is fine.
        row = conn.execute(
            "SELECT job_url, repost_of FROM jobs WHERE job_url=?", (job_url,)
        ).fetchone()
        affected = sorted(pipeline._chain_targets(row)) if row else []
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
    return jsonify({
        "ok": bool(ok),
        "message": "done" if ok else "no matching posting",
        "affected": affected if ok else [],
    })


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
