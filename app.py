#!/usr/bin/env python3
"""
Local web UI for triaging job postings — a faster alternative to the
`applied` / `passed` / `reject` CLI commands.

Launched via `python pipeline.py ui`. It is a thin Flask layer over workflow.py's bounded
work queues, funnel.py's chain-level conversion snapshot, the chain-service cores, and the
existing `jobs.db`: `mark_posting` /
`reject_posting` (so repost-chain
propagation and the status lift behave exactly like the CLI wrappers in pipeline.py),
and the shared `dupe_resolve` / `dupe_commit` / `dupe_unlink` cores for manually
linking duplicates (the `dupe` command's two-click equivalent). Review-only cross-source
candidate discovery is owned by `dupe_candidates.py`; confirmation still uses those same
guarded chain cores. It makes no schema
changes directly; schema ownership remains in core.get_db. Single-user, local-only — binds
to 127.0.0.1.

Launch through serve() (what `pipeline.py ui` / `python app.py` do) — it runs the one-time
schema/migration pass the routes rely on. A serve-less launch (`flask run`, a WSGI import)
is unsupported: routes open plain connect_db connections and would fail on a fresh DB.
"""

import json
import sys
import time
import webbrowser
from datetime import date

from flask import Flask, Response, jsonify, render_template, request, send_file

from chain import (resolve_posting, mark_posting, mark_expired, reject_posting,
                   effective_decisions, effective_decision, dupe_resolve, dupe_commit,
                   dupe_unlink, record_event, undo_event, chain_events, set_resume,
                   set_channel)
from core import connect_db, get_db, load_config, prewarm_db
from dupe_candidates import confirm_candidate, set_candidate_dismissed
from second_judge import opinion_summaries
from exports import roles_csv
from funnel import DEFAULT_FUNNEL_DAYS, funnel_snapshot, parse_funnel_days
from health import MAX_HEALTH_DAYS, MAX_RUN_HISTORY, health_snapshot
from interviews import (INTERVIEW_MODES, add_interview, chain_interviews,
                        change_interview, interview_ics, interview_summaries)
from intake import PostingAlreadyExists, add_manual_posting
from jd_diff import (JD_DIFF_MAX_CONTEXT, JDDiffTooLarge, JDEvidenceUnavailable,
                     jd_diff_bundle, jd_versions_bundle)
from materials import (MAX_UPLOAD_BYTES, attach_upload, chain_materials, download_info,
                       material_summaries, prep_context_bundle, snapshot_jd)
from outreach import (CONTACT_KINDS, OUTREACH_PURPOSES, add_contact, chain_contacts,
                      contact_summaries, outreach_context_bundle, remove_contact)
from prep_library import (ENTRY_KINDS, MAX_LIBRARY_ENTRIES, archive_entry, confirm_entry,
                          create_entry, list_entries, restore_entry, role_entry_choices,
                          set_role_link, update_entry)
from report import BUCKET_LABELS, posting_age, recency_sort_key, score_band
from states import (GATE_NAMES_WITH_OTHER, ALL_EVENTS, ALL_CHANNELS, STATUS_EVALUATED,
                    STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED, VERDICT_PASS,
                    VERDICT_RECRUITER_ONLY)
from tasks import add_task, chain_tasks, change_task, task_counts, task_summaries
from timeline import DEFAULT_TIMELINE_LIMIT, role_timeline
from watchlist import set_starred, star_summaries
from workflow import DEFAULT_PAGE_SIZE, action_center, query_action_page, query_job_page

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024

GATE_OPTIONS = GATE_NAMES_WITH_OTHER


@app.errorhandler(413)
def _upload_too_large(_error):
    return jsonify({"ok": False, "message": "file exceeds the 10 MB limit"}), 413

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


def row_to_dict(row, cap, dec, packet=None, contacts=None, role_tasks=None,
                task_count=0, scheduled_interviews=None, star=None,
                second_opinion=None):
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
    row_keys = set(row.keys())
    return {
        "job_url": row["job_url"],
        "chain_root": row["repost_of"] or row["job_url"],
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
        # Separate channel from `flags`: these describe the EVALUATION's completeness,
        # not the role (see evaluation.normalize_result). The UI renders them next to
        # the verdict so a low-trust card is visibly low-trust.
        "eval_issues": ev.get("eval_issues") or [],
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
        # Present only on the dedicated follow-up read model.  Keeping these derived fields
        # out of jobs means the append-only event history remains the source of truth.
        "followup_count": row["followup_count"] if "followup_count" in row_keys else None,
        "last_followup_date": (row["last_followup_date"]
                               if "last_followup_date" in row_keys else None),
        "next_followup_date": (row["next_followup_date"]
                               if "next_followup_date" in row_keys else None),
        "next_task_due": row["next_task_due"] if "next_task_due" in row_keys else None,
        "next_interview_at": (row["next_interview_at"]
                              if "next_interview_at" in row_keys else None),
        "materials": packet or {"resume": None, "cover_letter": None,
                                "jd_snapshot": None},
        "contacts": contacts or [],
        "tasks": role_tasks or [],
        "task_count": task_count,
        "interviews": scheduled_interviews or [],
        "starred": bool(star and star["starred"]),
        "starred_at": star["starred_at"] if star else None,
        "star_version": star["star_version"] if star else 0,
        # Present only when the second judge DISAGREES (direction/verdict/fit/note);
        # agreement and pending are None — rows_to_dicts filters the chain-scoped
        # second_judge.opinion_summaries down to direction-carrying entries.
        "second_opinion": second_opinion,
    }


def rows_to_dicts(conn, rows, cap, decisions=None):
    """Batch chain decisions, packets, contacts, tasks, and interviews for bounded rows."""
    decisions = decisions or effective_decisions(conn, rows)
    packets = material_summaries(conn, rows)
    contacts = contact_summaries(conn, rows)
    role_tasks = task_summaries(conn, rows)
    role_task_counts = task_counts(conn, rows)
    scheduled = interview_summaries(conn, rows)
    stars = star_summaries(conn, rows)
    # The card renders only DISAGREEING done opinions (direction set): agreement and
    # pending spend zero pixels — the razor that keeps the layer an attention saver.
    # The full summaries (statuses, tallies) are the report section's job; both read
    # the same chain-scoped second_judge.opinion_summaries, so they can't drift.
    opinions = {url: (o if o and o["direction"] else None)
                for url, o in opinion_summaries(conn, rows).items()}
    return [row_to_dict(
        row, cap, decisions[row["job_url"]],
        packet=packets[row["repost_of"] or row["job_url"]],
        contacts=contacts[row["repost_of"] or row["job_url"]],
        role_tasks=role_tasks[row["repost_of"] or row["job_url"]],
        task_count=role_task_counts[row["repost_of"] or row["job_url"]],
        scheduled_interviews=scheduled[row["job_url"]],
        star=stars[row["job_url"]],
        second_opinion=opinions[row["job_url"]],
    ) for row in rows]


def _dupe_side_to_dict(row):
    """Serialize only posting evidence rendered by the duplicate comparison UI."""
    description = " ".join((row["description"] or "").split())
    return {
        "job_url": row["job_url"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "source": row["source"],
        "first_seen": row["first_seen"],
        "date_posted": row["date_posted"],
        "description_preview": description[:280],
    }


def dupe_pairs_to_dicts(pairs):
    """Shape pair evidence without loading unrelated private role evidence."""
    return [{
        "left_root": pair["left_root"],
        "right_root": pair["right_root"],
        "left": _dupe_side_to_dict(pair["left"]),
        "right": _dupe_side_to_dict(pair["right"]),
        "same_location": pair["same_location"],
        "first_seen_gap_days": pair["first_seen_gap_days"],
        "dismissed_at": pair["dismissed_at"],
        "review_version": pair["review_version"],
    } for pair in pairs]


def _action_section_payload(conn, section, cap, *, paged=False):
    """Serialize ordinary job queues and the pair-shaped duplicate queue consistently."""
    if section["id"] == "possible_duplicates":
        items = dupe_pairs_to_dicts(section["pairs"])
    else:
        decisions = effective_decisions(conn, section["rows"])
        items = rows_to_dicts(conn, section["rows"], cap, decisions)
    payload = {
        "id": section["id"],
        "title": section["title"],
        "description": section["description"],
        "total": section["total"],
        "items": items,
    }
    if section["id"] == "possible_duplicates":
        payload["dismissed_total"] = section["dismissed_total"]
    if paged:
        payload.update({
            "page": section["page"],
            "page_size": section["page_size"],
            "pages": section["pages"],
        })
    return payload


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
    visible = []
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
        visible.append(r)
    return rows_to_dicts(conn, visible, cap, decisions)


@app.route("/")
def index():
    cfg = load_config()
    return render_template(
        "index.html",
        gates=GATE_OPTIONS,
        events=list(ALL_EVENTS),
        channels=list(ALL_CHANNELS),
        contact_kinds=list(CONTACT_KINDS),
        outreach_purposes=list(OUTREACH_PURPOSES),
        interview_modes=list(INTERVIEW_MODES),
        prep_entry_kinds=list(ENTRY_KINDS),
        max_description_chars=cfg["settings"]["max_description_chars"],
        searches=cfg["searches"],
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
        # Backward-compatible legacy contract: callers that do not ask for paging still get
        # the historical bare array.  The local UI opts into the bounded envelope by sending
        # page/page_size; scripts or old tabs already open during an upgrade keep working.
        if "page" not in request.args and "page_size" not in request.args:
            return jsonify(jobs_for_view(conn, view, for_date, cap))
        try:
            page = int(request.args.get("page", "1"))
            page_size = int(request.args.get("page_size", str(DEFAULT_PAGE_SIZE)))
            min_raw = request.args.get("min_score")
            days_raw = request.args.get("days")
            filters = {
                "q": request.args.get("q", ""),
                "source": request.args.get("source", ""),
                "verdict": request.args.get("verdict", ""),
                "min_score": int(min_raw) if min_raw not in (None, "") else None,
                "days": int(days_raw) if days_raw not in (None, "") else None,
            }
            result = query_job_page(
                conn, view, for_date=for_date, page=page, page_size=page_size,
                filters=filters,
            )
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "message": str(e)}), 400
        decisions = effective_decisions(conn, result["rows"])
        items = rows_to_dicts(conn, result["rows"], cap, decisions)
        return jsonify({
            "items": items,
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "pages": result["pages"],
        })
    finally:
        conn.close()


def _route_cadence(cfg):
    """Optional config overrides for the recruiter_route worklist cadence.

    Both routes serving the Action Center must read the same values, or a section's
    "View all" would page a different queue than the front page shows.
    """
    settings = cfg["settings"]
    return {
        "route_days": settings.get("recruiter_route_days", 14),
        "route_min_score": settings.get("recruiter_route_min_score", 15),
    }


@app.route("/api/actions")
def api_actions():
    cfg = load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = connect_db(cfg)
    try:
        try:
            sections = action_center(conn, **_route_cadence(cfg))
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "message": str(e)}), 400
        payload = [_action_section_payload(conn, section, cap)
                   for section in sections]
        return jsonify({"sections": payload})
    finally:
        conn.close()


@app.route("/api/actions/<section_id>")
def api_action_section(section_id):
    """One bounded Action Center queue for the section's View all surface."""
    cfg = load_config()
    cap = cfg["settings"]["max_description_chars"]
    conn = connect_db(cfg)
    try:
        try:
            page = int(request.args.get("page", "1"))
            page_size = int(request.args.get("page_size", str(DEFAULT_PAGE_SIZE)))
            dismissed_raw = request.args.get("dismissed", "0")
            if dismissed_raw not in ("0", "1"):
                raise ValueError("dismissed must be 0 or 1")
            section = query_action_page(
                conn, section_id, page=page, page_size=page_size,
                dismissed=dismissed_raw == "1", **_route_cadence(cfg),
            )
        except (TypeError, ValueError) as e:
            return jsonify({"ok": False, "message": str(e)}), 400
        return jsonify(_action_section_payload(conn, section, cap, paged=True))
    finally:
        conn.close()


@app.route("/api/funnel")
def api_funnel():
    """One chain-scoped application funnel; ``days=all`` disables the date cutoff."""
    try:
        days = parse_funnel_days(
            request.args.get("days", str(DEFAULT_FUNNEL_DAYS))
        )
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        result = funnel_snapshot(conn, days=days)
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/health")
def api_health():
    """Bounded pipeline availability facts and descriptive first-touch search yield."""
    try:
        days = int(request.args.get("days", "30"))
        run_limit = int(request.args.get("run_limit", "20"))
        if not 1 <= days <= MAX_HEALTH_DAYS:
            raise ValueError(f"days must be an integer from 1 to {MAX_HEALTH_DAYS}")
        if not 1 <= run_limit <= MAX_RUN_HISTORY:
            raise ValueError(f"run_limit must be an integer from 1 to {MAX_RUN_HISTORY}")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        return jsonify(health_snapshot(conn, cfg, days=days, run_limit=run_limit))
    finally:
        conn.close()


@app.route("/api/prep-items", methods=["GET", "POST"])
def api_prep_items():
    """Bounded global prep library; content is lazy-loaded outside role-card payloads."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    if request.method == "GET":
        try:
            limit = int(request.args.get("limit", str(MAX_LIBRARY_ENTRIES)))
            include_archived = request.args.get("include_archived", "0") == "1"
            if not 1 <= limit <= MAX_LIBRARY_ENTRIES:
                raise ValueError(f"limit must be from 1 to {MAX_LIBRARY_ENTRIES}")
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        conn = connect_db(load_config())
        try:
            return jsonify({
                "ok": True,
                "entries": list_entries(
                    conn, include_archived=include_archived, limit=limit,
                ),
            })
        finally:
            conn.close()

    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    conn = connect_db(load_config())
    try:
        action = body.get("action", "create")
        try:
            if action == "create":
                entry = create_entry(
                    conn, kind=body.get("kind"), title=body.get("title"),
                    prompt=body.get("prompt"), response=body.get("response"),
                    tags=body.get("tags", []),
                )
            elif action == "update":
                entry = update_entry(
                    conn, body.get("entry_id"), expected_version=body.get("expected_version"),
                    kind=body.get("kind"), title=body.get("title"),
                    prompt=body.get("prompt"), response=body.get("response"),
                    tags=body.get("tags", []),
                )
            elif action == "confirm":
                entry = confirm_entry(
                    conn, body.get("entry_id"), expected_version=body.get("expected_version"),
                )
            elif action == "archive":
                entry = archive_entry(
                    conn, body.get("entry_id"), expected_version=body.get("expected_version"),
                )
            elif action == "restore":
                entry = restore_entry(
                    conn, body.get("entry_id"), expected_version=body.get("expected_version"),
                )
            else:
                return jsonify({"ok": False, "message": "unknown prep-entry action"}), 400
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "entry": entry}), 201 if action == "create" else 200
    finally:
        conn.close()


@app.route("/api/prep-links", methods=["GET", "POST"])
def api_prep_links():
    """Read or set absolute prep-entry relevance for a current duplicate chain."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True) if request.method == "POST" else {}
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    job_url = body.get("job_url") if request.method == "POST" else request.args.get("job_url")
    if not isinstance(job_url, str) or not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        if request.method == "GET":
            choices = role_entry_choices(conn, row, include_archived=True)
            return jsonify({
                "ok": True,
                # The role-link dialog renders identity/state only. Full private responses
                # stay behind the separately requested global-library endpoint.
                "entries": [{
                    key: item[key] for key in (
                        "id", "kind", "title", "status", "link_linked",
                        "link_revision", "link_root",
                    )
                } for item in choices],
            })
        try:
            state = set_role_link(
                conn, row, body.get("entry_id"), linked=body.get("linked"),
                expected_linked=body.get("expected_linked"),
                expected_revision=body.get("expected_revision"),
                expected_root=body.get("expected_root"),
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, **state})
    finally:
        conn.close()


@app.route("/api/jd-versions")
def api_jd_versions():
    """Lazy metadata for stored posting observations and verified application snapshots."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        try:
            return jsonify(jd_versions_bundle(
                conn, row, cfg,
                description_cap=cfg["settings"]["max_description_chars"],
            ))
        except JDDiffTooLarge as exc:
            return jsonify({"ok": False, "message": str(exc)}), 422
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/jd-diff")
def api_jd_diff():
    """Return one complete bounded text diff selected by opaque current-chain version IDs."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    try:
        context = int(request.args.get("context", "3"))
        if not 0 <= context <= JD_DIFF_MAX_CONTEXT:
            raise ValueError(f"context must be 0..{JD_DIFF_MAX_CONTEXT}")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        try:
            return jsonify(jd_diff_bundle(
                conn, row, left_id=request.args.get("left"),
                right_id=request.args.get("right"), context=context, cfg=cfg,
                description_cap=cfg["settings"]["max_description_chars"],
            ))
        except JDDiffTooLarge as exc:
            return jsonify({"ok": False, "message": str(exc)}), 422
        except JDEvidenceUnavailable as exc:
            return jsonify({"ok": False, "message": str(exc)}), 409
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
    finally:
        conn.close()


@app.route("/api/export/roles.csv")
def api_export_roles_csv():
    """Download a chain-deduped summary; intentionally not a full evidence backup."""
    conn = connect_db(load_config())
    try:
        content = roles_csv(conn)
    finally:
        conn.close()
    filename = f"job-search-roles-{date.today().isoformat()}.csv"
    return Response(
        content, content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/intake", methods=["POST"])
def api_intake():
    """Add one explicitly pasted external role; never fetch or evaluate implicitly."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        try:
            row = add_manual_posting(
                conn,
                body,
                searches=cfg["searches"],
                max_description_chars=cfg["settings"]["max_description_chars"],
            )
        except PostingAlreadyExists as exc:
            return jsonify({"ok": False, "message": str(exc)}), 409
        except (ValueError, RuntimeError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({
            "ok": True,
            "job_url": row["job_url"],
            "repost_of": row["repost_of"],
            "message": "Role added; the next pipeline run will process it through current rules.",
        }), 201
    finally:
        conn.close()


@app.route("/api/star", methods=["POST"])
def api_star():
    """Set explicit current-chain priority; independent of evaluation and decisions."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    job_url = body.get("job_url")
    starred = body.get("starred")
    expected = body.get("expected_starred")
    expected_version = body.get("expected_star_version")
    if (not isinstance(job_url, str) or not job_url
            or not isinstance(starred, bool) or not isinstance(expected, bool)
            or isinstance(expected_version, bool)
            or not isinstance(expected_version, int) or expected_version < 0):
        return jsonify({"ok": False, "message": "bad request"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        try:
            result = set_starred(
                conn, row, starred, expected_starred=expected,
                expected_version=expected_version,
            )
        except ValueError as exc:
            status = 409 if "refresh and retry" in str(exc) else 400
            return jsonify({"ok": False, "message": str(exc)}), status
        return jsonify({"ok": True, **result})
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
    # UI either omit Origin or send a matching one.) JSON routes also require their real content
    # type; the multipart material-upload route uses this same explicit Origin check.
    # (_pin_host has already vetted request.host, so host_url can't be a rebinding alias here.)
    origin = request.headers.get("Origin")
    return origin is None or origin == request.host_url.rstrip("/")


@app.route("/api/materials", methods=["GET", "POST"])
def api_materials():
    """Read the current chain packet or attach one actual submitted document."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    job_url = (request.form.get("job_url") if request.method == "POST"
               else request.args.get("job_url"))
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        if request.method == "GET":
            return jsonify({"ok": True, "materials": chain_materials(conn, row)})
        upload = request.files.get("file")
        kind = request.form.get("kind")
        if upload is None or not upload.filename:
            return jsonify({"ok": False, "message": "file is required"}), 400
        data = upload.stream.read(MAX_UPLOAD_BYTES + 1)
        try:
            item = attach_upload(conn, row, kind, upload.filename, data, cfg)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        current = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        return jsonify({"ok": True, "message": f"attached {item['name']}",
                        "item": item, "materials": chain_materials(conn, current)})
    finally:
        conn.close()


@app.route("/api/materials/<int:attachment_id>/download")
def api_material_download(attachment_id):
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        info = download_info(conn, row, attachment_id, cfg) if row is not None else None
    finally:
        conn.close()
    if info is None:
        return jsonify({"ok": False, "message": "material not found"}), 404
    return send_file(info["path"], mimetype=info["media_type"], as_attachment=True,
                     download_name=info["name"])


@app.route("/api/prep")
def api_prep():
    """One-click clipboard context: frozen JD, actual packet, and prior events/notes."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        bundle = prep_context_bundle(conn, row, cfg)
    finally:
        conn.close()
    return jsonify({"ok": True, **bundle})


@app.route("/api/contacts", methods=["GET", "POST"])
def api_contacts():
    """Read or mutate the current role chain's manually verified contacts."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    job_url = body.get("job_url") if request.method == "POST" else request.args.get("job_url")
    if not isinstance(job_url, str) or not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        if request.method == "GET":
            return jsonify({"ok": True, "contacts": chain_contacts(conn, row)})
        action = body.get("action", "add")
        try:
            if action == "add":
                result = add_contact(
                    conn, row, name=body.get("name"), role=body.get("role"),
                    kind=body.get("kind", "other"), email=body.get("email"),
                    profile_url=body.get("profile_url"), note=body.get("note"),
                )
                message = f"added {result['contact']['name']}"
            elif action == "delete":
                result = remove_contact(conn, row, body.get("contact_id"))
                if result is None:
                    return jsonify({"ok": False, "message": "contact not found"}), 404
                message = "contact removed"
            else:
                return jsonify({"ok": False, "message": "unknown contact action"}), 400
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "message": message, **result})
    finally:
        conn.close()


@app.route("/api/outreach")
def api_outreach():
    """Return a local-evidence drafting brief for copy/paste; never send a message."""
    job_url = request.args.get("job_url")
    purpose = request.args.get("purpose", "application_follow_up")
    try:
        contact_id = int(request.args.get("contact_id", ""))
    except ValueError:
        return jsonify({"ok": False, "message": "contact_id must be an integer"}), 400
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    cfg = load_config()
    conn = connect_db(cfg)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        try:
            bundle = outreach_context_bundle(
                conn, row, contact_id=contact_id, purpose=purpose, cfg=cfg,
            )
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
    finally:
        conn.close()
    return jsonify({"ok": True, **bundle})


@app.route("/api/tasks", methods=["GET", "POST"])
def api_tasks():
    """Read or mutate explicit next actions for the current role chain."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    job_url = body.get("job_url") if request.method == "POST" else request.args.get("job_url")
    if not isinstance(job_url, str) or not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        if request.method == "GET":
            include_closed = request.args.get("include_closed") == "1"
            return jsonify({"ok": True, "tasks": chain_tasks(
                conn, row, include_closed=include_closed,
            )})
        action = body.get("action", "add")
        try:
            if action == "add":
                result = add_task(
                    conn, row, title=body.get("title"), due_date=body.get("due_date"),
                    note=body.get("note"),
                )
                message = f"added {result['task']['title']}"
            else:
                result = change_task(
                    conn, row, body.get("task_id"), action,
                    expected_version=body.get("expected_version"),
                    due_date=body.get("due_date"),
                )
                if result is None:
                    return jsonify({"ok": False, "message": "task not found"}), 404
                message = {
                    "complete": "task completed",
                    "reopen": "task reopened",
                    "cancel": "task cancelled",
                    "snooze": "task rescheduled",
                }[action]
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "message": message, **result})
    finally:
        conn.close()


@app.route("/api/interviews", methods=["GET", "POST"])
def api_interviews():
    """Read or mutate explicit interview schedules for the current role chain."""
    if request.method == "POST" and not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = (request.get_json(silent=True) or {}) if request.method == "POST" else {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    job_url = body.get("job_url") if request.method == "POST" else request.args.get("job_url")
    if not isinstance(job_url, str) or not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        if request.method == "GET":
            include_cancelled = request.args.get("include_cancelled") == "1"
            return jsonify({"ok": True, "interviews": chain_interviews(
                conn, row, include_cancelled=include_cancelled,
            )})
        action = body.get("action", "add")
        try:
            if action == "add":
                result = add_interview(
                    conn, row, title=body.get("title"), starts_at=body.get("starts_at"),
                    duration_minutes=body.get("duration_minutes"), mode=body.get("mode"),
                    location=body.get("location"), meeting_url=body.get("meeting_url"),
                    note=body.get("note"),
                )
                message = f"scheduled {result['interview']['title']}"
            elif action in ("update", "cancel"):
                result = change_interview(
                    conn, row, body.get("interview_id"), action,
                    expected_version=body.get("expected_version"),
                    title=body.get("title"), starts_at=body.get("starts_at"),
                    duration_minutes=body.get("duration_minutes"), mode=body.get("mode"),
                    location=body.get("location"), meeting_url=body.get("meeting_url"),
                    note=body.get("note"),
                )
                if result is None:
                    return jsonify({"ok": False, "message": "interview not found"}), 404
                message = "interview updated" if action == "update" else "interview cancelled"
            else:
                return jsonify({"ok": False, "message": "unknown interview action"}), 400
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "message": message, **result})
    finally:
        conn.close()


@app.route("/api/interviews/<int:interview_id>.ics")
def api_interview_calendar(interview_id):
    """Download one local schedule as an iCalendar file; no external calendar write."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"ok": False, "message": "job_url is required"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"ok": False, "message": "posting not found"}), 404
        try:
            calendar = interview_ics(conn, row, interview_id)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if calendar is None:
            return jsonify({"ok": False, "message": "interview not found"}), 404
        return Response(
            calendar, mimetype="text/calendar",
            headers={"Content-Disposition":
                     f'attachment; filename="interview-{interview_id}.ics"'},
        )
    finally:
        conn.close()


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
    if not job_url or action not in ("applied", "passed", "expired", "reject", "undo_app",
                                     "undo_reject", "undo_expired", "set_resume", "set_channel"):
        return jsonify({"ok": False, "message": "bad request"}), 400
    if not (_opt_str(resume) and _opt_str(channel)):
        # A non-string here would AttributeError inside the cores (`.strip()`) — a Flask HTML
        # 500 instead of this route's JSON error contract — or be stored as a raw number.
        return jsonify({"ok": False, "message": "resume/channel must be strings"}), 400

    cfg = load_config()
    conn = connect_db(cfg)
    packet = None
    material_warning = None
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
        elif action == "expired":
            # Dead/delisted posting: chain-wide passed + the fixed note, one core with the
            # CLI's `expired` (chain.mark_expired) — refused on applied chains.
            ok, message, affected, exempt = mark_expired(conn, row)
        elif action == "undo_expired":
            ok, message, affected, exempt = mark_expired(conn, row, undo=True)
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
        if ok and action == "applied":
            try:
                snapshot_jd(conn, row, cfg)
            except Exception as exc:  # The application decision already committed; surface but keep it.
                material_warning = str(exc)
        # Post-mutation chain truth for the client to patch from — the outcome cache is
        # server-derived state the client CANNOT mirror by rule (a re-apply restores it from
        # event history; the prompted variant may be superseded by the chain's inherited
        # one), so hand it the answer instead of letting patchJob guess. Same contract as
        # /api/event's echo.
        dec = effective_decision(conn, row) if ok else None
        if ok:
            try:
                packet = chain_materials(conn, row)
            except Exception as exc:
                # As above, a packet-read failure must not turn a committed decision into a
                # misleading HTTP failure.  The warning makes the missing evidence visible.
                material_warning = material_warning or str(exc)
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
                     "channel": dec["channel"],
                     "materials": packet})
    if material_warning:
        resp["materials_warning"] = material_warning
    return jsonify(resp)


@app.route("/api/event", methods=["POST"])
def api_event():
    """Record a chain event or a role note; notes are valid before application too."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object",
                        "affected": [], "exempt": []}), 400
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
    """Legacy/read-specific append-only application events for one posting chain.

    The card's unified Activity view uses /api/timeline; this narrower contract remains
    available to callers that need event types and notes without other workflow records.
    """
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


@app.route("/api/timeline")
def api_timeline():
    """Bounded, read-only activity across the posting's current duplicate chain."""
    job_url = request.args.get("job_url")
    if not job_url:
        return jsonify({"items": [], "total": 0, "truncated": False}), 400
    try:
        limit = int(request.args.get("limit", str(DEFAULT_TIMELINE_LIMIT)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "timeline limit must be an integer"}), 400
    conn = connect_db(load_config())
    try:
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (job_url,)).fetchone()
        if row is None:
            return jsonify({"items": [], "total": 0, "truncated": False}), 404
        try:
            result = role_timeline(conn, row, limit=limit)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
    finally:
        conn.close()
    return jsonify(result)


@app.route("/api/dupe-candidate", methods=["POST"])
def api_dupe_candidate():
    """Persist or restore the human review result for one suggested pair."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object"}), 400
    left_url = body.get("left_url")
    right_url = body.get("right_url")
    dismissed = body.get("dismissed")
    expected_roots = body.get("expected_roots")
    expected_dismissed = body.get("expected_dismissed")
    expected_review_version = body.get("expected_review_version")
    if (not isinstance(left_url, str) or not left_url
            or not isinstance(right_url, str) or not right_url
            or not isinstance(dismissed, bool)
            or not isinstance(expected_dismissed, bool)
            or isinstance(expected_review_version, bool)
            or not isinstance(expected_review_version, int)
            or expected_review_version < 0
            or not isinstance(expected_roots, list) or len(expected_roots) != 2
            or any(not isinstance(root, str) or not root for root in expected_roots)):
        return jsonify({"ok": False, "message": "bad request"}), 400
    conn = connect_db(load_config())
    try:
        try:
            result = set_candidate_dismissed(
                conn, left_url, right_url, dismissed,
                expected_roots=expected_roots,
                expected_dismissed=expected_dismissed,
                expected_review_version=expected_review_version,
            )
        except (ValueError, RuntimeError) as e:
            return jsonify({"ok": False, "message": str(e)}), 409
    finally:
        conn.close()
    return jsonify({"ok": True, **result})


@app.route("/api/dupe", methods=["POST"])
def api_dupe():
    """Manually link two postings as the same role (or `undo` a manual link). Thin layer over the
    shared dupe cores in pipeline — assume_yes is implicit (the browser does its own confirm)."""
    if not _origin_ok():
        return jsonify({"ok": False, "message": "cross-origin request refused"}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "message": "JSON body must be an object",
                        "affected": [], "exempt": []}), 400
    job_url = body.get("job_url")
    of_url = body.get("of")
    expected_roots = body.get("expected_roots")
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
            if expected_roots is not None:
                try:
                    plan, err, affected, exempt = confirm_candidate(
                        conn, job_url, of_url, expected_roots,
                    )
                except (ValueError, RuntimeError) as e:
                    plan, err = None, str(e)
            else:
                plan, err = dupe_resolve(conn, job_url, of_url)
                if not err:
                    affected, exempt = dupe_commit(conn, plan)
            if err:
                ok, message = False, err
            else:
                assert plan is not None  # candidate/manual confirm returns plan when err is None
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
        cfg = load_config()
        get_db(cfg).close()
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
    # Warm the OS cache BEFORE the browser opens: jobs.db lives on a USB HDD, and the first
    # Action Center query against a spun-down disk has blown the front-end's 30s fetch
    # timeout ("Failed to load: TimeoutError"). A sequential pre-read costs a few seconds of
    # startup, spends them before the first fetch exists, and makes that fetch hit RAM.
    start = time.monotonic()
    warmed = prewarm_db(cfg)
    print(f"[ui] warmed {warmed / 1048576:.0f} MB of jobs.db into the OS cache "
          f"in {time.monotonic() - start:.1f}s")
    url = f"http://{host}:{port}"
    print(f"[ui] triage UI at {url}  (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    app.run(host=host, port=port)


if __name__ == "__main__":
    serve()
