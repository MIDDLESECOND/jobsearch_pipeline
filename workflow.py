#!/usr/bin/env python3
"""Read models for the local triage UI: bounded job pages and the action center.

This is deliberately separate from chain.py.  Chain owns decisions, repost membership,
events, and mutations; this module only answers presentation/work-queue questions over
that state.  app.py flattens the selected rows into HTTP payloads, so the Flask layer stays
thin and neither the report nor the decision service becomes a dashboard query module.
"""

import math
from datetime import date, datetime, timedelta, timezone

from chain import effective_decisions
from dupe_candidates import query_candidate_page
from report import recency_sort_key
from states import (STATUS_ERROR, STATUS_EVALUATED, STATUS_NEEDS_MANUAL,
                    STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED,
                    EVENT_FOLLOWUP_SENT, EVENT_INTERVIEW, EVENT_RECRUITER_SCREEN,
                    VERDICT_PASS, VERDICT_RECRUITER_ONLY)
from tasks import TASK_OPEN


VALID_VIEWS = ("today", "backlog", "applied", "passed")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
DEFAULT_ACTION_LIMIT = 10
FOLLOWUP_MAX = 2
ACTION_SECTION_IDS = ("fresh_strong", "recruiter_route", "possible_duplicates",
                      "starred_roles", "upcoming_interviews", "interview_prep", "tasks_due", "followups_due",
                      "needs_attention")
_TRIAGE_VERDICTS = (VERDICT_PASS, VERDICT_RECRUITER_ONLY)
_DECIDED_ROOTS_CTE = (
    "decided_roots(root) AS ("
    "SELECT DISTINCT COALESCE(repost_of,job_url) FROM jobs "
    "WHERE app_status IS NOT NULL OR filter_source IS NOT NULL)"
)
# Chains with at least one recorded contact.  Contacts are canonical-keyed at write time
# and may sit on a former canonical after a merge, so map each contact row through jobs
# to its CURRENT root — same read rule as outreach.contact_summaries.
_CONTACT_ROOTS_CTE = (
    "contact_roots(root) AS ("
    "SELECT DISTINCT COALESCE(k.repost_of,k.job_url) "
    "FROM job_contacts c JOIN jobs k ON k.job_url=c.job_url)"
)
_UNDECIDED_BACKLOG_CTE = "WITH " + _DECIDED_ROOTS_CTE + " "


def _like_literal(value):
    """Escape user text for a literal contains-search under SQLite LIKE."""
    return "%" + str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _where(view, for_date, filters, today):
    if view not in VALID_VIEWS:
        raise ValueError(f"view must be one of {list(VALID_VIEWS)}")
    clauses = []
    params = []
    if view == "backlog":
        clauses.extend([
            "j.app_status IS NULL",
            "j.filter_source IS NULL",
            "j.status=?",
            "j.verdict IN (?,?)",
        ])
        params.extend((STATUS_EVALUATED, VERDICT_PASS, VERDICT_RECRUITER_ONLY))
    elif view in ("applied", "passed"):
        clauses.append("j.app_status=?")
        params.append(view)
    else:
        try:
            for_date = date.fromisoformat(str(for_date)).isoformat()
        except (TypeError, ValueError):
            raise ValueError("date must be YYYY-MM-DD") from None
        clauses.append("substr(j.first_seen,1,10)=?")
        params.append(for_date)

    q = str(filters.get("q") or "").strip()
    if len(q) > 200:
        raise ValueError("q must be at most 200 characters")
    if q:
        like = _like_literal(q)
        clauses.append(
            "(j.title LIKE ? ESCAPE '\\' OR j.company LIKE ? ESCAPE '\\' "
            "OR j.location LIKE ? ESCAPE '\\' OR j.search_name LIKE ? ESCAPE '\\')"
        )
        params.extend((like, like, like, like))

    source = str(filters.get("source") or "").strip()
    if source:
        clauses.append("j.source=?")
        params.append(source)

    verdict = str(filters.get("verdict") or "").strip()
    if verdict:
        if verdict not in _TRIAGE_VERDICTS:
            raise ValueError(f"verdict must be one of {list(_TRIAGE_VERDICTS)}")
        # Backlog rows are evaluated rows, so their own judgment is the displayed one.
        # Other views can contain verdict-less reposts whose card inherits the chain's
        # judgment; those filters are applied after effective_decisions below.
        if view == "backlog":
            clauses.append("j.verdict=?")
            params.append(verdict)

    min_score = filters.get("min_score")
    if min_score is not None:
        if isinstance(min_score, bool) or not isinstance(min_score, int) or not 0 <= min_score <= 18:
            raise ValueError("min_score must be an integer from 0 to 18")
        if view == "backlog":
            clauses.append("j.fit_score>=?")
            params.append(min_score)

    has_contact = filters.get("has_contact")
    if has_contact is not None:
        if not isinstance(has_contact, bool):
            raise ValueError("has_contact must be a boolean")
        if view != "backlog":
            # A chain-level fact applied via a CTE only the backlog scan attaches;
            # refusing elsewhere keeps the filter vocabulary honest.
            raise ValueError("has_contact filter applies only to the backlog view")

    days = filters.get("days")
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
            raise ValueError("days must be an integer from 1 to 3650")
        # Calendar-day filter: days=1 means today only; days=3 means today plus the
        # preceding two dates.  The UI labels this honestly rather than claiming an exact
        # rolling-hour window over the date-normalized comparison.
        cutoff = (today - timedelta(days=days - 1)).isoformat()
        clauses.append("substr(j.first_seen,1,10)>=?")
        params.append(cutoff)

    return " AND ".join(clauses), params


def _fetch_selected(conn, urls):
    if not urls:
        return []
    qs = ",".join("?" * len(urls))
    rows = conn.execute(f"SELECT * FROM jobs WHERE job_url IN ({qs})", tuple(urls)).fetchall()
    by_url = {r["job_url"]: r for r in rows}
    return [by_url[u] for u in urls]


def _matches_chain_filters(row, decision, filters):
    """Apply verdict/score filters to the same values the card presents.

    A skipped repost deliberately keeps its own verdict and score NULL.  Its usable
    judgment lives on another current chain member, so filtering the physical row columns
    would hide a card that visibly shows the inherited judgment.
    """
    verdict = str(filters.get("verdict") or "").strip()
    shown_verdict = row["verdict"] or decision["chain_verdict"]
    if verdict and shown_verdict != verdict:
        return False
    min_score = filters.get("min_score")
    shown_score = (row["fit_score"] if row["fit_score"] is not None
                   else decision["chain_fit_score"])
    return min_score is None or (shown_score is not None and shown_score >= min_score)


def query_job_page(conn, view, *, for_date=None, page=1, page_size=DEFAULT_PAGE_SIZE,
                   filters=None, today=None):
    """Return one bounded, correctly ordered page of rows plus page metadata.

    Backlog performance is the load-bearing path: it scans only four narrow columns to
    preserve the shared two-band Python sort, then hydrates at most ``page_size`` full rows
    and computes chain decisions only for those rows in app.py.  The old implementation
    hydrated and serialized the entire actionable history on every Backlog click.
    """
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if (isinstance(page_size, bool) or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE):
        raise ValueError(f"page_size must be an integer from 1 to {MAX_PAGE_SIZE}")
    filters = filters or {}
    today = today or date.today()
    for_date = for_date or today.isoformat()
    where, params = _where(view, for_date, filters, today)
    offset = (page - 1) * page_size

    if view == "backlog":
        # Materialize decided roots once, then join against that small set.  A correlated
        # NOT EXISTS here turns into an O(candidate rows * all rows) scan because the chain
        # root is a COALESCE expression with no index; that made Action Center take tens of
        # seconds on the real history.  The CTE preserves the same stale-cache guard while
        # scanning the jobs table once.
        has_contact = filters.get("has_contact")
        cte, contact_join, contact_guard = _UNDECIDED_BACKLOG_CTE, "", ""
        if has_contact is not None:
            cte = "WITH " + _DECIDED_ROOTS_CTE + "," + _CONTACT_ROOTS_CTE + " "
            contact_join = (" LEFT JOIN contact_roots cr "
                            "ON cr.root=COALESCE(j.repost_of,j.job_url)")
            contact_guard = (" AND cr.root IS NOT NULL" if has_contact
                             else " AND cr.root IS NULL")
        candidates = conn.execute(
            cte
            + "SELECT j.job_url,j.fit_score,j.date_posted,j.first_seen FROM jobs j "
            "LEFT JOIN decided_roots d ON d.root=COALESCE(j.repost_of,j.job_url)"
            + contact_join + " WHERE "
            + where + " AND d.root IS NULL" + contact_guard, tuple(params)
        ).fetchall()
        ordered = sorted(candidates, key=recency_sort_key)
        total = len(ordered)
        urls = [r["job_url"] for r in ordered[offset:offset + page_size]]
        rows = _fetch_selected(conn, urls)
    elif view in ("applied", "passed"):
        chain_filtered = bool(str(filters.get("verdict") or "").strip()
                              or filters.get("min_score") is not None)
        if chain_filtered:
            # Post-filter before slicing so total/pages describe the visible result rather
            # than the larger SQL candidate set.
            all_rows = conn.execute(
                "SELECT j.* FROM jobs j WHERE " + where
                + " ORDER BY j.status_date DESC,j.fit_score DESC,j.job_url",
                tuple(params),
            ).fetchall()
            decisions = effective_decisions(conn, all_rows)
            all_rows = [row for row in all_rows if _matches_chain_filters(
                row, decisions[row["job_url"]], filters)]
            total = len(all_rows)
            rows = all_rows[offset:offset + page_size]
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM jobs j WHERE " + where, tuple(params)
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT j.* FROM jobs j WHERE " + where
                + " ORDER BY j.status_date DESC,j.fit_score DESC,j.job_url LIMIT ? OFFSET ?",
                (*params, page_size, offset),
            ).fetchall()
    else:
        # A single day is naturally bounded, and eval-skipped rows need their chain fit to
        # preserve report.recency_sort_key's established ordering contract.
        all_rows = conn.execute("SELECT j.* FROM jobs j WHERE " + where, tuple(params)).fetchall()
        decisions = effective_decisions(conn, all_rows)
        all_rows = [row for row in all_rows if _matches_chain_filters(
            row, decisions[row["job_url"]], filters)]

        def key(r):
            fit = r["fit_score"]
            if fit is None and r["status"] in (STATUS_REPOST_EVALUATED, STATUS_REPOST_DECIDED):
                fit = decisions[r["job_url"]]["chain_fit_score"] or 0
            return recency_sort_key(r, fit=fit)

        all_rows = sorted(all_rows, key=key)
        total = len(all_rows)
        rows = all_rows[offset:offset + page_size]

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _followup_page(conn, *, page, page_size, today, followup_days, followup_max):
    """Applied/no-response chains whose next follow-up date has arrived.

    ``followup_sent`` rows are canonical-keyed at write time but may sit on a former
    canonical after a manual duplicate merge.  Joining each event back through jobs maps it
    to the chain's current root before counting, matching chain.chain_events semantics.
    """
    stats_sql = (
        "WITH followup_stats AS ("
        "SELECT COALESCE(k.repost_of,k.job_url) AS root,COUNT(*) AS followup_count,"
        "MAX(e.event_date) AS last_followup_date FROM app_events e "
        "JOIN jobs k ON k.job_url=e.job_url WHERE e.event_type=? "
        "GROUP BY COALESCE(k.repost_of,k.job_url)) "
        "SELECT j.*,COALESCE(s.followup_count,0) AS followup_count,"
        "s.last_followup_date FROM jobs j LEFT JOIN followup_stats s ON s.root=j.job_url "
        "WHERE j.repost_of IS NULL AND j.app_status='applied' "
        "AND j.outcome_status IS NULL AND j.status_date IS NOT NULL "
        "AND COALESCE(s.followup_count,0)<?"
    )
    candidates = conn.execute(
        stats_sql, (EVENT_FOLLOWUP_SENT, followup_max)
    ).fetchall()
    due = []
    for raw in candidates:
        row = dict(raw)
        count = row["followup_count"]
        anchor = row["last_followup_date"] if count else row["status_date"]
        try:
            anchor_date = date.fromisoformat(str(anchor)[:10])
        except (TypeError, ValueError):
            continue  # malformed historical stamp: don't invent a follow-up age
        row["next_followup_date"] = (
            anchor_date + timedelta(days=followup_days)
        ).isoformat()
        if row["next_followup_date"] <= today.isoformat():
            due.append(row)
    due.sort(key=lambda r: (r["next_followup_date"], r["status_date"], r["job_url"]))
    total = len(due)
    offset = (page - 1) * page_size
    return {
        "rows": due[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _attention_page(conn, *, page, page_size):
    """Rows a human has to look at: evaluation failures, and stored verdicts whose own
    contract diagnostics contradict them.

    A flagged verdict is otherwise effectively unreachable.  A GATE_FAIL row enters no
    Action Center queue and no backlog listing, and its fit_score is NULL so it sorts below
    every scored row in the date view — the day the model rejected a role while its own gate
    table said every gate passed, that row was on page 7 of 11.  Surfacing it here is
    deliberately review, not re-routing: the verdict is left exactly as stored, and the card
    leaves once the user decides the role.
    """
    where = ("j.filter_source IS NULL AND (j.status IN (?,?) "
             "OR (j.eval_issues IS NOT NULL AND j.app_status IS NULL))")
    params = (STATUS_ERROR, STATUS_NEEDS_MANUAL)
    total = conn.execute("SELECT COUNT(*) FROM jobs j WHERE " + where, params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT j.* FROM jobs j WHERE " + where
        + " ORDER BY j.first_seen DESC,j.job_url LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _tasks_due_page(conn, *, page, page_size, today):
    """One canonical card per chain with at least one open task due today or earlier."""
    joins = (
        " FROM jobs root JOIN jobs owner "
        "ON COALESCE(owner.repost_of,owner.job_url)=root.job_url "
        "JOIN job_tasks t ON t.job_url=owner.job_url "
        "WHERE root.repost_of IS NULL AND t.status=? AND t.due_date<=?"
    )
    params = (TASK_OPEN, today.isoformat())
    total = conn.execute(
        "SELECT COUNT(DISTINCT root.job_url)" + joins, params
    ).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT root.*,MIN(t.due_date) AS next_task_due" + joins
        + " GROUP BY root.job_url ORDER BY next_task_due,root.job_url LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _upcoming_interviews_page(conn, *, page, page_size, now, days=14):
    """One canonical applied-role card per chain with a scheduled interview soon."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    starts = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    ends = (now.astimezone(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    joins = (
        " FROM jobs root JOIN jobs owner "
        "ON COALESCE(owner.repost_of,owner.job_url)=root.job_url "
        "JOIN job_interviews i ON i.job_url=owner.job_url "
        "WHERE root.repost_of IS NULL AND root.app_status='applied' "
        "AND i.status='scheduled' AND i.starts_at>=? AND i.starts_at<=?"
    )
    params = (starts, ends)
    total = conn.execute(
        "SELECT COUNT(DISTINCT root.job_url)" + joins, params
    ).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT root.*,MIN(i.starts_at) AS next_interview_at" + joins
        + " GROUP BY root.job_url ORDER BY next_interview_at,root.job_url LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _starred_roles_page(conn, *, page, page_size):
    """One current canonical card per explicitly starred role chain."""
    joins = (
        " FROM jobs root JOIN jobs owner "
        "ON COALESCE(owner.repost_of,owner.job_url)=root.job_url "
        "JOIN role_stars s ON s.job_url=owner.job_url "
        "WHERE root.repost_of IS NULL AND s.starred=1"
    )
    total = conn.execute(
        "SELECT COUNT(DISTINCT root.job_url)" + joins
    ).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT root.*,MAX(s.starred_at) AS starred_at" + joins
        + " GROUP BY root.job_url ORDER BY starred_at DESC,root.job_url LIMIT ? OFFSET ?",
        (page_size, offset),
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def _interview_prep_page(conn, *, page, page_size, today, prep_days=14):
    """Recent recruiter responses that should be prepared from the submitted packet.

    A rolling queue avoids inventing separate completion state: a fresh screen/interview
    enters automatically, a terminal later outcome removes it, and an untouched item ages
    out after two weeks.
    """
    cutoff = (today - timedelta(days=prep_days - 1)).isoformat()
    where = (
        "j.repost_of IS NULL AND j.app_status='applied' "
        "AND j.outcome_status IN (?,?) AND j.outcome_date>=?"
    )
    params = (EVENT_RECRUITER_SCREEN, EVENT_INTERVIEW, cutoff)
    total = conn.execute("SELECT COUNT(*) FROM jobs j WHERE " + where, params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        "SELECT j.* FROM jobs j WHERE " + where
        + " ORDER BY j.outcome_date DESC,j.status_date DESC,j.job_url LIMIT ? OFFSET ?",
        (*params, page_size, offset),
    ).fetchall()
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def query_action_page(conn, section_id, *, page=1, page_size=DEFAULT_PAGE_SIZE,
                      today=None, fresh_days=3, followup_days=7,
                      followup_max=FOLLOWUP_MAX, dismissed=False, now=None,
                      route_days=14, route_min_score=15):
    """Return one pageable Action Center section with its display metadata."""
    if section_id not in ACTION_SECTION_IDS:
        raise ValueError(f"section must be one of {list(ACTION_SECTION_IDS)}")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if (isinstance(page_size, bool) or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE):
        raise ValueError(f"page_size must be an integer from 1 to {MAX_PAGE_SIZE}")
    if fresh_days < 1 or followup_days < 1 or followup_max < 1:
        raise ValueError("action-center cadence values must be positive")
    # Validated here, not only in _where: an omitted YAML value reads as None, and _where
    # skips a None min_score entirely — which would silently drop this queue's score bar
    # instead of failing loudly on a half-written config key.
    if isinstance(route_days, bool) or not isinstance(route_days, int) or route_days < 1:
        raise ValueError("route_days must be a positive integer")
    if (isinstance(route_min_score, bool) or not isinstance(route_min_score, int)
            or not 0 <= route_min_score <= 18):
        raise ValueError("route_min_score must be an integer from 0 to 18")
    today = today or date.today()
    now = now or datetime.now(timezone.utc)

    if section_id == "fresh_strong":
        result = query_job_page(
            conn, "backlog", page=page, page_size=page_size,
            filters={"verdict": VERDICT_PASS, "min_score": 16, "days": fresh_days},
            today=today,
        )
        title = "Fresh strong matches"
        description = f"PASS · score 16+ · last {fresh_days} calendar days"
    elif section_id == "recruiter_route":
        # A channel-finding worklist, not a freshness race: the RO verdict's premise is
        # that cold-applying is low-value, so the queue runs on its own wider cadence and
        # a recorded contact — the work's completion evidence — is what clears a card.
        result = query_job_page(
            conn, "backlog", page=page, page_size=page_size,
            filters={"verdict": VERDICT_RECRUITER_ONLY, "min_score": route_min_score,
                     "days": route_days, "has_contact": False},
            today=today,
        )
        title = "Route to a human"
        description = (f"Recruiter-only · score {route_min_score}+ · last {route_days} "
                       "calendar days · exits once a contact is recorded")
    elif section_id == "possible_duplicates":
        result = query_candidate_page(
            conn, page=page, page_size=page_size, today=today,
            dismissed=dismissed,
        )
        # Keep the established ordinary-row key present so generic queue consumers can
        # distinguish this pair-shaped section without special missing-key handling.
        result["rows"] = []
        title = "Ignored duplicate suggestions" if dismissed else "Possible duplicates"
        description = ("Previously reviewed pairs · restore to reconsider"
                       if dismissed else
                       "Cross-source · exact normalized company and title · confirm manually")
        # A dropped mass-posting key is a real coverage gap, so say so rather than let the
        # trimmed count read as the whole population.
        if result.get("suppressed_pairs"):
            description += (f" · {result['suppressed_pairs']} pair(s) across "
                            f"{result['suppressed_keys']} mass-posted company/title key(s) "
                            "not shown")
    elif section_id == "starred_roles":
        result = _starred_roles_page(conn, page=page, page_size=page_size)
        title = "Starred roles"
        description = "Your explicit shortlist · independent of model score"
    elif section_id == "upcoming_interviews":
        result = _upcoming_interviews_page(
            conn, page=page, page_size=page_size, now=now,
        )
        title = "Upcoming interviews"
        description = "Scheduled in the next 14 days · local time shown on each card"
    elif section_id == "interview_prep":
        result = _interview_prep_page(
            conn, page=page, page_size=page_size, today=today,
        )
        title = "Interview prep"
        description = "Screens or interviews recorded in the last 14 days"
    elif section_id == "tasks_due":
        result = _tasks_due_page(
            conn, page=page, page_size=page_size, today=today,
        )
        title = "Next actions due"
        description = "Open role tasks due today or overdue"
    elif section_id == "followups_due":
        result = _followup_page(
            conn, page=page, page_size=page_size, today=today,
            followup_days=followup_days, followup_max=followup_max,
        )
        title = "Follow-ups due"
        description = (f"Every {followup_days} days · stop after {followup_max} sent "
                       "· no employer outcome")
    else:
        result = _attention_page(conn, page=page, page_size=page_size)
        title = "Needs attention"
        description = ("Evaluation errors, postings with no retrieved description, "
                       "or a stored verdict its own gate table contradicts")

    return {**result, "id": section_id, "title": title, "description": description}


def action_center(conn, *, today=None, fresh_days=3, followup_days=7,
                  limit=DEFAULT_ACTION_LIMIT, now=None,
                  route_days=14, route_min_score=15):
    """Return the small work queues that answer "what needs me now?".

    Rows remain ordinary jobs rows.  app.api_actions adds the shared chain/card fields;
    keeping HTTP shaping out of this module makes these queues reusable by a future CLI or
    daily report without importing Flask.
    """
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be from 1 to {MAX_PAGE_SIZE}")
    return [query_action_page(
        conn, section_id, page=1, page_size=limit, today=today,
        fresh_days=fresh_days, followup_days=followup_days,
        now=now, route_days=route_days, route_min_score=route_min_score,
    ) for section_id in ACTION_SECTION_IDS]
