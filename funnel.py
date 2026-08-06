#!/usr/bin/env python3
"""Chain-scoped application-funnel read model for the local UI.

The jobs table stores one copy of an applied decision on every repost-chain member,
while app_events stores each lifecycle event once on the canonical url that existed at
write time.  This module reduces both shapes to one record per *current* chain before it
counts anything, so relistings and later manual duplicate merges cannot inflate the
funnel.

This is a descriptive snapshot of applications that are currently marked ``applied``;
undoing an application intentionally removes it.  The date window selects by application
date, then considers that application's lifecycle history through ``today``.
"""

from datetime import date, timedelta

from states import (ALL_CHANNELS, EVENT_GHOSTED, EVENT_INTERVIEW,
                    EVENT_OFFER, EVENT_RECRUITER_SCREEN,
                    EVENT_REJECTED_BY_EMPLOYER, EVENT_WITHDREW)


DEFAULT_FUNNEL_DAYS = 90
MAX_FUNNEL_DAYS = 3650
UNKNOWN_CHANNEL = "unknown"

_RESPONSE_EVENTS = (EVENT_RECRUITER_SCREEN, EVENT_INTERVIEW, EVENT_OFFER,
                    EVENT_REJECTED_BY_EMPLOYER)
_INTERVIEW_EVENTS = (EVENT_INTERVIEW, EVENT_OFFER)
_OUTCOME_ORDER = (None, EVENT_RECRUITER_SCREEN, EVENT_INTERVIEW, EVENT_OFFER,
                  EVENT_REJECTED_BY_EMPLOYER, EVENT_GHOSTED, EVENT_WITHDREW)
_OUTCOME_LABELS = {
    None: "No recorded outcome",
    EVENT_RECRUITER_SCREEN: "Recruiter screen",
    EVENT_INTERVIEW: "Interview",
    EVENT_OFFER: "Offer",
    EVENT_REJECTED_BY_EMPLOYER: "Rejected by employer",
    EVENT_GHOSTED: "Ghosted",
    EVENT_WITHDREW: "Withdrew",
}
_CHANNEL_LABELS = {
    "direct": "Direct",
    "agency": "Agency",
    "referral": "Referral",
    UNKNOWN_CHANNEL: "Not recorded",
}


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator * 100.0 / denominator, 1) if denominator else None


def _validate_days(days: int | None) -> None:
    if days is None:
        return
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_FUNNEL_DAYS:
        raise ValueError(f"days must be an integer from 1 to {MAX_FUNNEL_DAYS}, or None")


def parse_funnel_days(value: str) -> int | None:
    """Parse the HTTP range spelling without widening the route's 400 boundary."""
    raw = value.strip().lower()
    if raw == "all":
        return None
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(
            f"days must be an integer from 1 to {MAX_FUNNEL_DAYS}, or 'all'"
        ) from None
    _validate_days(days)
    return days


def _application_rows(conn, *, days: int | None, today: date):
    """Return one flag row per applied root, with former event canonicals remapped.

    ``JOIN jobs k`` is load-bearing: after a manual dupe merge, an older event remains
    stored on the former canonical.  Mapping k through its current repost_of mirrors
    chain.chain_events and the follow-up read model without moving history.
    """
    response_qs = ",".join("?" * len(_RESPONSE_EVENTS))
    interview_qs = ",".join("?" * len(_INTERVIEW_EVENTS))
    sql = (
        "WITH event_flags AS ("
        "SELECT COALESCE(k.repost_of,k.job_url) AS root,"
        f"MAX(CASE WHEN e.event_type IN ({response_qs}) THEN 1 ELSE 0 END) AS responded,"
        f"MAX(CASE WHEN e.event_type IN ({interview_qs}) THEN 1 ELSE 0 END) AS interviewed,"
        "MAX(CASE WHEN e.event_type=? THEN 1 ELSE 0 END) AS offered "
        "FROM app_events e JOIN jobs k ON k.job_url=e.job_url "
        "WHERE date(e.event_date)<=? GROUP BY COALESCE(k.repost_of,k.job_url)) "
        "SELECT j.job_url,j.status_date,j.channel,j.resume_variant,j.outcome_status,"
        "COALESCE(f.responded,0) AS responded,"
        "COALESCE(f.interviewed,0) AS interviewed,"
        "COALESCE(f.offered,0) AS offered "
        "FROM jobs j LEFT JOIN event_flags f ON f.root=j.job_url "
        "WHERE j.repost_of IS NULL AND j.app_status='applied' "
        "AND j.status_date IS NOT NULL AND date(j.status_date)<=?"
    )
    params = (*_RESPONSE_EVENTS, *_INTERVIEW_EVENTS, EVENT_OFFER,
              today.isoformat(), today.isoformat())
    if days is not None:
        cutoff = (today - timedelta(days=days - 1)).isoformat()
        sql += " AND date(j.status_date)>=?"
        params += (cutoff,)
    return conn.execute(sql, params).fetchall()


def funnel_snapshot(conn, *, days: int | None = DEFAULT_FUNNEL_DAYS,
                    today: date | None = None):
    """Build the chain-level application funnel and channel/outcome breakdowns."""
    _validate_days(days)
    today = today or date.today()
    if not isinstance(today, date):
        raise ValueError("today must be a date")
    rows = _application_rows(conn, days=days, today=today)

    applied = len(rows)
    responded = sum(bool(r["responded"]) for r in rows)
    interviewed = sum(bool(r["interviewed"]) for r in rows)
    offered = sum(bool(r["offered"]) for r in rows)
    stage_values = (
        ("applied", "Applied", applied, applied),
        ("response", "Employer response", responded, applied),
        ("interview", "Interview", interviewed, responded),
        ("offer", "Offer", offered, interviewed),
    )
    stages = []
    for stage_id, label, count, previous in stage_values:
        stages.append({
            "id": stage_id,
            "label": label,
            "count": count,
            "rate_from_applied": _rate(count, applied),
            "rate_from_previous": _rate(count, previous),
        })

    channel_ids = (*ALL_CHANNELS, UNKNOWN_CHANNEL)
    channel_rows = []
    for channel_id in channel_ids:
        selected = [r for r in rows if (
            r["channel"] if r["channel"] in ALL_CHANNELS else UNKNOWN_CHANNEL
        ) == channel_id]
        total = len(selected)
        responses = sum(bool(r["responded"]) for r in selected)
        interviews = sum(bool(r["interviewed"]) for r in selected)
        offers = sum(bool(r["offered"]) for r in selected)
        channel_rows.append({
            "id": channel_id,
            "label": _CHANNEL_LABELS[channel_id],
            "applied": total,
            "responses": responses,
            "interviews": interviews,
            "offers": offers,
            "response_rate": _rate(responses, total),
            "interview_rate": _rate(interviews, total),
            "offer_rate": _rate(offers, total),
        })

    outcome_counts = {key: 0 for key in _OUTCOME_ORDER}
    other_outcomes = {}
    for row in rows:
        outcome = row["outcome_status"]
        if outcome in outcome_counts:
            outcome_counts[outcome] += 1
        else:
            other_outcomes[outcome] = other_outcomes.get(outcome, 0) + 1
    outcomes = [
        {"id": key or "no_outcome", "label": _OUTCOME_LABELS[key],
         "count": outcome_counts[key]}
        for key in _OUTCOME_ORDER
    ]
    outcomes.extend(
        {"id": str(key), "label": str(key).replace("_", " ").title(), "count": count}
        for key, count in sorted(other_outcomes.items(), key=lambda item: str(item[0]))
    )

    cutoff = (today - timedelta(days=days - 1)).isoformat() if days is not None else None
    return {
        "range": {
            "days": days,
            "start": cutoff,
            "end": today.isoformat(),
            "label": f"Last {days} calendar days" if days is not None else "All current applications",
        },
        "stages": stages,
        "outcomes": outcomes,
        "by_channel": channel_rows,
        "quality": {
            "channel_recorded": sum(r["channel"] in ALL_CHANNELS for r in rows),
            "resume_recorded": sum(bool((r["resume_variant"] or "").strip()) for r in rows),
            "applications": applied,
        },
        "definitions": {
            "population": "Current applied chains; one role counts once across reposts.",
            "response": "Recruiter screen, interview, offer, or employer rejection.",
            "inference": "Interview implies response; offer implies interview and response.",
        },
    }
