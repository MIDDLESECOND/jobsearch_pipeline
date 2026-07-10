#!/usr/bin/env python3
"""
Local web UI for triaging job postings — a faster alternative to the
`applied` / `passed` / `reject` CLI commands.

Launched via `python pipeline.py ui`. It is a thin Flask layer over the chain-service
cores and the existing `jobs.db`: `mark_posting` / `reject_posting` (so repost-chain
propagation and the status lift behave exactly like the CLI wrappers in pipeline.py),
and the shared `dupe_resolve` / `dupe_commit` / `dupe_unlink` cores for manually
linking duplicates (the `dupe` command's two-click equivalent). It makes no schema
changes. Single-user, local-only — binds to 127.0.0.1.

Launch through serve() (what `pipeline.py ui` / `python app.py` do) — it runs the one-time
schema/migration pass the routes rely on. A serve-less launch (`flask run`, a WSGI import)
is unsupported: routes open plain connect_db connections and would fail on a fresh DB.
"""

import json
import sys
import webbrowser
from datetime import date

from flask import Flask, jsonify, render_template, request

from chain import (resolve_posting, mark_posting, reject_posting, effective_decisions,
                   effective_decision, dupe_resolve, dupe_commit, dupe_unlink,
                   record_event, undo_event, chain_events, set_resume, set_channel)
from core import connect_db, get_db, load_config
from report import BUCKET_LABELS, posting_age, recency_sort_key, score_band
from states import (GATE_NAMES, ALL_EVENTS, ALL_CHANNELS, STATUS_EVALUATED,
                    STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED, VERDICT_PASS,
                    VERDICT_RECRUITER_ONLY)

app = Flask(__name__)

GATE_OPTIONS = GATE_NAMES + ["other"]

# Hostnames this app may be addressed as. The Origin check below is defeated by DNS
# rebinding on its own (the browser would send the attacker's domain as BOTH Host and
# Origin, which then "match"), so every request first has its Host pinned to loopback
# names. serve() extends the set when run on a non-default host/port. Kept hand-rolled
# rather than Flask 3's TRUSTED_HOSTS config, deliberately: this returns the JSON shape
# the UI's fetch() error paths read (TRUSTED_HOSTS emits an HTML 400), and the set is
# extended at serve() time with the actual port.
ALLOWED_HOSTS = {"127.0.0.1:5000", "localhost:5000"}


@app.before_request
def _pin_host():
    if request.host not in ALLOWED_HOSTS:
        return jsonify({"ok": False, "message": "unrecognized Host header"}), 403


def row_to_dict(row, cap, dec):
    """Flatten a jobs row + its eval_json into the fields the UI renders. `cap` is the
    configured max_description_chars — a stored description at that length was truncated.
    `dec` is chain.effective_decision(conn, row) — the chain-wide decision, computed by the
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
        "band": score_band(row["fit_score"]) if row["fit_score"] is not None else None,
        "bucket": bucket,
        "bucket_label": BUCKET_LABELS.get(bucket),
        # Pass the breakdown through as-stored — older rows use a different set of score
        # dimensions, and the report (report._render_scored_job) renders whatever keys exist.
        "score_breakdown": ev.get("score_breakdown") or {},
        "one_line": ev.get("one_line"),
        "flags": ev.get("flags") or [],
        "app_status": row["app_status"],
        "status_date": row["status_date"],
        "outcome_status": row["outcome_status"],
        "outcome_date": row["outcome_date"],
        "resume_variant": row["resume_variant"],
        "channel": row["channel"],
        "date_posted": row["date_posted"],
        "first_seen": row["first_seen"],
        # Server-computed (report.posting_age) so the label wording can't drift from the report.
        "age_label": posting_age(row["date_posted"], row["first_seen"]),
        "filter_source": row["filter_source"],
        "filter_gate": row["filter_gate"],
        "is_repost": bool(row["repost_of"]),
        # Manually-linked relisting (repost_source set) → the UI offers an "Unlink" control; an
        # auto-detected repost (repost_source NULL) is not user-unlinkable here.
        "is_manual_repost": row["repost_source"] is not None,
        # The chain-wide decision (from chain.effective_decision), so the UI can show a
        # relisting's effective status even when its own app_status is NULL (only the canonical
        # carries the decision). The client only truthiness-checks chain_filter_source (index.html),
        # so the reject side collapses to the "manual" sentinel — this DROPS the real rule:<name> /
        # manual attribution that dec still carries; a future consumer needing it should read
        # dec["filter_gate"] (as the report does) rather than this field. Client recomputes
        # "effective" after a decision (see patchJob).
        "chain_app_status": dec["app_status"],
        "chain_filter_source": "manual" if dec["reject"] else None,
        "chain_status_date": dec["status_date"],
        # Chain-level outcome fields (same effective_decision source): what the Applied view
        # renders — the cache is propagated to every member, but reading it through dec keeps
        # a not-yet-synced relisting honest, exactly like chain_app_status above.
        "chain_outcome_status": dec["outcome_status"],
        "chain_outcome_date": dec["outcome_date"],
        "chain_resume_variant": dec["resume_variant"],
        "chain_channel": dec["channel"],
        # The ROLE's verdict read through the chain (most favorable member — states.VERDICT_FAVOR).
        # For a 'repost_evaluated' row (eval skipped, own verdict NULL) this is the one to show;
        # for an evaluated row it normally equals row.verdict.
        "chain_verdict": dec["chain_verdict"],
        "chain_fit_score": dec["chain_fit_score"],
        # Cheap booleans for the send-to-assistant button — not the description text itself,
        # so the list payload stays small.
        "has_description": bool(row["description"]),
        "truncated": bool(row["description"] and len(row["description"]) >= cap),
    }


def jobs_for_view(conn, view, for_date, cap):
    """Fetch rows for a view and return a list of UI dicts. The chain decision each row shows
    comes from chain.effective_decision (one source of truth, shared with the report and the
    dupe guard) rather than a per-view SQL join — so the three can't drift."""
    if view == "backlog":
        # Only actionable undecided jobs — exclude GATE_FAIL, which the model already
        # hard-rejected (they'd otherwise swamp the list). Relistings whose chain is already
        # decided are filtered out below, via the shared effective_decision.
        # No ORDER BY here or in the today branch: the Python sort below is the single owner
        # of triage ordering. Only applied/passed order in SQL (status_date — decision history).
        rows = conn.execute(
            "SELECT * FROM jobs WHERE app_status IS NULL AND filter_source IS NULL "
            "AND status=? AND verdict IN (?,?)",
            (STATUS_EVALUATED, VERDICT_PASS, VERDICT_RECRUITER_ONLY),
        ).fetchall()
    elif view in ("applied", "passed"):
        rows = conn.execute(
            "SELECT * FROM jobs WHERE app_status=? ORDER BY status_date DESC, fit_score DESC",
            (view,),
        ).fetchall()
    else:  # "today" (default) — postings first seen on the given date
        rows = conn.execute(
            "SELECT * FROM jobs WHERE substr(first_seen,1,10)=?",
            (for_date,),
        ).fetchall()
    # Batch the chain-decision lookup: one (chunked) query for the whole row set rather than a
    # per-row effective_decision call (that was O(N) round-trips — seconds on the backlog view).
    # Computed BEFORE the sort: the sort key needs each row's chain fit as a fallback.
    decisions = effective_decisions(conn, rows)

    if view not in ("applied", "passed"):
        # The triage views (today/backlog — any unknown view falls into the today branch above)
        # share the report's two-band order (report.recency_sort_key): at/above the apply line
        # freshest-first, below it fit-only. Applied/passed keep status_date DESC — they are
        # decision history, not triage. Eval-SKIPPED rows (fit_score NULL by design, the
        # role's score lives on the chain) sort by their CHAIN's fit — otherwise a relisting
        # of a strong PASS role sinks to the bottom band, burying exactly the rows the
        # chain_verdict badge exists to surface. Gated on the two skip statuses: other
        # fit-NULL rows (needs_manual, error, salary_filtered, still-'new') must NOT inherit
        # the chain's fit — a deterministically rejected or description-less row sorting
        # above genuinely scored cards would mislead triage.
        def _triage_key(r):
            fit = r["fit_score"]
            if fit is None and r["status"] in (STATUS_REPOST_EVALUATED, STATUS_REPOST_DECIDED):
                fit = decisions[r["job_url"]]["chain_fit_score"] or 0
            return recency_sort_key(r, fit=fit)
        rows = sorted(rows, key=_triage_key)
    out = []
    for r in rows:
        dec = decisions[r["job_url"]]
        # Backlog: drop a relisting whose chain the user already decided (its own app_status is
        # NULL, but the canonical/sibling carries the decision). This replaces the old join's
        # `j.repost_of IS NULL OR canonical-undecided` clause. Note effective_decision spans the
        # WHOLE chain (canonical + all siblings), not just the canonical row the old join looked at
        # — intentional, and equivalent under normal flow since a decision propagates to every
        # member; it only differs (more robustly) if chain rows are out of sync from a raw DB edit.
        if view == "backlog" and r["repost_of"] is not None and (dec["app_status"] or dec["reject"]):
            continue
        out.append(row_to_dict(r, cap, dec))
    return out


@app.route("/")
def index():
    cfg = load_config()
    return render_template(
        "index.html",
        gates=GATE_OPTIONS,
        events=list(ALL_EVENTS),
        channels=list(ALL_CHANNELS),
        today=date.today().isoformat(),
        feedback_url=cfg["settings"].get("feedback_project_url", "") or "",
    )


@app.route("/api/jobs")
def api_jobs():
    view = request.args.get("view", "today")
    for_date = request.args.get("date") or date.today().isoformat()
    cfg = load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = connect_db(cfg)
    try:
        return jsonify(jobs_for_view(conn, view, for_date, cap))
    finally:
        conn.close()


@app.route("/api/clip")
def api_clip():
    """Assemble the clipboard text for one posting (header + JD) to paste into the configured
    assistant project (feedback_project_url). Kept off the list payload so /api/jobs stays small."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"text": "", "truncated": False}), 400
    cfg = load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = connect_db(cfg)
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


def _opt_str(v):
    """Body-field guard: optional string. The cores call .strip() on these — a number/list
    from a malformed JSON body would AttributeError into a Flask HTML 500 instead of the
    routes' JSON error shape, or be stored as a raw non-string."""
    return v is None or isinstance(v, str)


def _origin_ok():
    # CSRF guard for the state-changing routes. The browser sends an Origin header on any
    # cross-site POST; refuse it unless it matches our own origin. (Same-origin requests from the
    # UI either omit Origin or send a matching one.) Requiring real application/json — i.e. dropping
    # force=True on get_json — also forces a CORS preflight a cross-site page can't satisfy.
    # (_pin_host has already vetted request.host, so host_url can't be a rebinding alias here.)
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
    resume = body.get("resume")
    channel = body.get("channel")
    if not job_url or action not in ("applied", "passed", "reject", "undo_app", "undo_reject",
                                     "set_resume", "set_channel"):
        return jsonify({"ok": False, "message": "bad request"}), 400
    if not (_opt_str(resume) and _opt_str(channel)):
        # A non-string here would AttributeError inside the cores (`.strip()`) — a Flask HTML
        # 500 instead of this route's JSON error contract — or be stored as a raw number.
        return jsonify({"ok": False, "message": "resume/channel must be strings"}), 400

    conn = connect_db(load_config())
    try:
        # Same service cores as the CLI (chain.mark_posting / reject_posting), so propagation
        # and the status lift can't drift between the two front-ends. `affected` is the whole
        # repost chain — the client uses it to update sibling cards, not just the one clicked.
        row, err = resolve_posting(conn, job_url)
        if err:
            return jsonify({"ok": False, "message": err, "affected": [], "exempt": []})
        if action == "applied":
            ok, message, affected, exempt = mark_posting(conn, row, action, resume, channel)
        elif action == "passed":
            ok, message, affected, exempt = mark_posting(conn, row, action)
        elif action == "undo_app":
            ok, message, affected, exempt = mark_posting(conn, row, None)
        elif action == "reject":
            ok, message, affected, exempt = reject_posting(conn, row, gate)
        elif action == "set_resume":
            # Edit-after-the-fact for the resume variant (chain.set_resume requires the
            # chain applied); never flips decided/undecided, so exempt stays the handle.
            ok, message, affected, exempt = set_resume(conn, row, resume)
        elif action == "set_channel":
            # Same edit-after-the-fact contract as set_resume; the core validates the
            # value against states.ALL_CHANNELS.
            ok, message, affected, exempt = set_channel(conn, row, channel)
        else:  # undo_reject
            ok, message, affected, exempt = reject_posting(conn, row, "other", undo=True)
        # Post-mutation chain truth for the client to patch from — the outcome cache is
        # server-derived state the client CANNOT mirror by rule (a re-apply restores it from
        # event history; the prompted variant may be superseded by the chain's inherited
        # one), so hand it the answer instead of letting patchJob guess. Same contract as
        # /api/event's echo.
        dec = effective_decision(conn, row) if ok else None
    finally:
        conn.close()
    # `exempt` is chain.py's authoritative "keep these visible past the hide-decided filter"
    # list — the rows whose DISPLAYED decision this operation changed (see the service-core
    # docstrings); the client applies it verbatim instead of re-deriving propagation rules.
    resp = {"ok": bool(ok), "message": message,
            "affected": affected if ok else [], "exempt": exempt if ok else []}
    if dec is not None:
        resp.update({"outcome_status": dec["outcome_status"],
                     "outcome_date": dec["outcome_date"],
                     "resume_variant": dec["resume_variant"],
                     "channel": dec["channel"]})
    return jsonify(resp)


@app.route("/api/event", methods=["POST"])
def api_event():
    """Record (or with undo=true remove the last) post-application outcome event on a chain —
    thin layer over chain.record_event / undo_event, same request/response contract as
    /api/decision. An event never flips a card decided<->undecided, so `exempt` is just the
    clicked row (the interaction handle) and the hide-decided filter is unaffected."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True) or {}
    job_url = body.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "bad request", "affected": [], "exempt": []}), 400
    if not (_opt_str(body.get("note")) and _opt_str(body.get("date"))):
        return jsonify({"ok": False, "message": "note/date must be strings",
                        "affected": [], "exempt": []}), 400
    conn = connect_db(load_config())
    try:
        row, err = resolve_posting(conn, job_url)
        if err:
            return jsonify({"ok": False, "message": err, "affected": [], "exempt": []})
        if body.get("undo"):
            ok, message, affected, exempt = undo_event(conn, row)
        else:
            ok, message, affected, exempt = record_event(
                conn, row, body.get("type"), body.get("date") or None, body.get("note"))
        # The card patches its outcome tag from these (chain-wide cache, one truth source).
        dec = effective_decision(conn, row) if ok else None
    finally:
        conn.close()
    return jsonify({
        "ok": bool(ok), "message": message,
        "affected": affected if ok else [], "exempt": exempt if ok else [],
        "outcome_status": dec["outcome_status"] if dec else None,
        "outcome_date": dec["outcome_date"] if dec else None,
    })


@app.route("/api/events")
def api_events():
    """The chain's full event timeline for one posting — lazy-fetched by the card's
    History toggle, kept off the list payload like /api/clip."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify([]), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify([]), 404
        events = chain_events(conn, row)
    finally:
        conn.close()
    return jsonify([{"event_type": e["event_type"], "event_date": e["event_date"],
                     "note": e["note"]} for e in events])


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
        return jsonify({"ok": False, "message": "bad request", "affected": [], "exempt": []}), 400

    conn = connect_db(load_config())
    affected, exempt = [], []
    try:
        if undo:
            row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
            if row is None:
                return jsonify({"ok": False, "message": "no matching posting",
                                "affected": [], "exempt": []}), 404
            ok, message, affected, exempt = dupe_unlink(conn, row)
        else:
            plan, err = dupe_resolve(conn, job_url, of_url)
            if err:
                ok, message = False, err
            else:
                affected, exempt = dupe_commit(conn, plan)
                w = plan["winner"]
                ok, message = True, f"linked under {w['title']} — {w['company']}"
    finally:
        conn.close()
    # The merge changes repost state across both chains; the client just reloads the view rather
    # than patching repost_of/repost_source/chain fields card-by-card. Same contract as
    # /api/decision: `affected` = rows whose chain state changed, `exempt` = chain.py's
    # authoritative keep-visible list for the hide-decided filter.
    return jsonify({"ok": bool(ok), "message": message,
                    "affected": list(affected), "exempt": list(exempt)})


def serve(host="127.0.0.1", port=5000):
    ALLOWED_HOSTS.update({f"{host}:{port}", f"127.0.0.1:{port}", f"localhost:{port}"})
    # One-time schema/migration pass; every request after this opens a plain connect_db
    # connection instead of re-running the idempotent DDL per request. The config load is
    # guarded like the CLI path in pipeline.main(): validate_config raises on a broken
    # config.yaml, and the UI must die with the collected problem list, not a traceback.
    try:
        get_db(load_config()).close()
    except FileNotFoundError:
        print("[config] config.yaml not found — copy config.example.yaml to config.yaml "
              "and edit it for your search", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print(f"[config] {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        # The stale-CHECK rebuild's actionable message (core._rebuild_for_stale_checks) —
        # same clean-exit treatment as a config problem.
        print(f"[db] {e}", file=sys.stderr)
        sys.exit(2)
    url = f"http://{host}:{port}"
    print(f"[ui] triage UI at {url}  (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host=host, port=port)


if __name__ == "__main__":
    serve()
