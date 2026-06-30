#!/usr/bin/env python3
"""
Local web UI for triaging job postings — a faster alternative to the
`applied` / `passed` / `reject` CLI commands.

Launched via `python pipeline.py ui`. It is a thin Flask layer over the existing
pipeline functions and the existing `jobs.db`: it reuses `cmd_mark` / `cmd_reject`
(so repost-chain propagation and the status lift behave exactly like the CLI), and
the shared `_dupe_resolve` / `_dupe_commit` / `_dupe_unlink` cores for manually
linking duplicates (the `dupe` command's two-click equivalent). It makes no schema
changes. Single-user, local-only — binds to 127.0.0.1.
"""

import json
import webbrowser
from datetime import date

from flask import Flask, jsonify, render_template, request

import pipeline

app = Flask(__name__)

GATE_OPTIONS = pipeline.GATE_NAMES + ["other"]


def row_to_dict(row, cap, dec):
    """Flatten a jobs row + its eval_json into the fields the UI renders. `cap` is the
    configured max_description_chars — a stored description at that length was truncated.
    `dec` is pipeline.effective_decision(conn, row) — the chain-wide decision, computed by the
    same function the report and dupe guard use, so the UI's "already applied/passed/rejected"
    marker can't drift from theirs."""
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
        "source": row["source"],
        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "verdict": row["verdict"],
        "failed_gate": row["failed_gate"],
        "fit_score": row["fit_score"],
        "band": pipeline.score_band(row["fit_score"]) if row["fit_score"] is not None else None,
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
        # Manually-linked relisting (repost_source set) → the UI offers an "Unlink" control; an
        # auto-detected repost (repost_source NULL) is not user-unlinkable here.
        "is_manual_repost": row["repost_source"] is not None,
        # The chain-wide decision (from pipeline.effective_decision), so the UI can show a
        # relisting's effective status even when its own app_status is NULL (only the canonical
        # carries the decision). The client only truthiness-checks chain_filter_source, so the
        # reject side collapses to a sentinel string. The client recomputes "effective" after a
        # decision (see patchJob).
        "chain_app_status": dec["app_status"],
        "chain_filter_source": "manual" if dec["reject"] else None,
        "chain_status_date": dec["status_date"],
        # Cheap booleans for the "Send to Claude" button — not the description text itself,
        # so the list payload stays small.
        "has_description": bool(row["description"]),
        "truncated": bool(row["description"] and len(row["description"]) >= cap),
    }


def jobs_for_view(conn, view, for_date, cap):
    """Fetch rows for a view and return a list of UI dicts. The chain decision each row shows
    comes from pipeline.effective_decision (one source of truth, shared with the report and the
    dupe guard) rather than a per-view SQL join — so the three can't drift."""
    if view == "backlog":
        # Only actionable undecided jobs — exclude GATE_FAIL, which the model already
        # hard-rejected (they'd otherwise swamp the list). Relistings whose chain is already
        # decided are filtered out below, via the shared effective_decision.
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

    out = []
    for r in rows:
        dec = pipeline.effective_decision(conn, r)
        # Backlog: drop a relisting whose chain the user already decided (its own app_status is
        # NULL, but the canonical/sibling carries the decision). Mirrors the old join's
        # `j.repost_of IS NULL OR canonical-undecided` clause.
        if view == "backlog" and r["repost_of"] is not None and (dec["app_status"] or dec["reject"]):
            continue
        out.append(row_to_dict(r, cap, dec))
    return out


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


def _origin_ok():
    # CSRF guard for the state-changing routes. The browser sends an Origin header on any
    # cross-site POST; refuse it unless it matches our own origin. (Same-origin requests from the
    # UI either omit Origin or send a matching one.) Requiring real application/json — i.e. dropping
    # force=True on get_json — also forces a CORS preflight a cross-site page can't satisfy.
    origin = request.headers.get("Origin")
    return origin is None or origin == request.host_url.rstrip("/")


@app.route("/api/decision", methods=["POST"])
def api_decision():
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True) or {}
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
        affected = sorted(pipeline._chain_targets(conn, row)) if row else []
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


@app.route("/api/dupe", methods=["POST"])
def api_dupe():
    """Manually link two postings as the same role (or `undo` a manual link). Thin layer over the
    shared dupe cores in pipeline — assume_yes is implicit (the browser does its own confirm)."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True) or {}
    job_url = body.get("job_url")
    of_url = body.get("of")
    undo = bool(body.get("undo"))
    if not job_url or (not undo and not of_url):
        return jsonify({"ok": False, "message": "bad request"}), 400

    conn = pipeline.get_db(pipeline.load_config())
    try:
        if undo:
            row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
            if row is None:
                return jsonify({"ok": False, "message": "no matching posting"}), 404
            ok, message, _ = pipeline._dupe_unlink(conn, row)
        else:
            plan, err = pipeline._dupe_resolve(conn, job_url, of_url)
            if err:
                ok, message = False, err
            else:
                pipeline._dupe_commit(conn, plan)
                w = plan["winner"]
                ok, message = True, f"linked under {w['title']} — {w['company']}"
    finally:
        conn.close()
    # The merge changes repost state across both chains; the client just reloads the view rather
    # than patching repost_of/repost_source/chain fields card-by-card.
    return jsonify({"ok": bool(ok), "message": message})


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
