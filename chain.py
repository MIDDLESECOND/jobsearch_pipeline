#!/usr/bin/env python3
"""Repost / content-dedup and decision-chain core.

This module owns everything about treating multiple postings as one role:

  * normalization + content fingerprint (the blocking key),
  * fetch-time repost linking (_find_repost),
  * the repost *chain* abstraction — members, the user's effective decision across a
    chain, and propagation/reconcile (skip_decided_reposts),
  * the manual dupe-link cores (dupe_resolve / dupe_commit / dupe_unlink),
  * the post-application outcome cores (record_event / undo_event / chain_events /
    set_resume / set_channel, plus _recompute_outcome — the one writer of the cached
    outcome_status/outcome_date columns).

It was extracted from pipeline.py so the "what is this chain's decision?" question has
ONE implementation. The report, the web UI, and the dupe conflict-guard all call
`effective_decision` / `_chain_decision` here instead of each re-deriving it.

No imports from pipeline (keeps the dependency one-way). Imports only states (the enum leaf)
and the stdlib.
"""

import re
import sys
from datetime import date, datetime

from states import (GATE_NAMES, APP_EVENTS, ALL_EVENTS, EVENT_NOTE, ALL_CHANNELS,
                    STATUS_NEW, STATUS_EVALUATED, STATUS_RULE_FILTERED,
                    STATUS_REPOST_DECIDED, STATUS_REPOST_EVALUATED, VERDICT_FAVOR, sql_list)


# ------------------------------------------------------- normalization / fingerprint
#
# LinkedIn mints a fresh job_url every time a role is reposted, so URL-level dedup (the
# PRIMARY-KEY conflict skip in fetch._insert_posting) misses relistings. The content fingerprint adds a
# second layer: postings with the same normalized company+location AND the same
# normalized title are treated as the same role across URL churn — guarding a double-apply.
#
# Matching is EXACT on the normalized title, not fuzzy. A backtest over the real DB (2,677
# rows) showed fuzzy title matching collapsing distinct roles that share a generic core —
# 'Workday Business Analyst' vs 'SalesForce Business Analyst' — into false reposts. The cost
# is asymmetric the wrong way: a false "ALREADY APPLIED" banner on a genuinely new role makes
# you SKIP a job you should apply to. Normalization (case, punctuation, company suffixes,
# Sr/Jr→Senior/Junior) absorbs the noise that isn't role-distinguishing; exact match on the
# result is both safe and accurate.

_COMPANY_SUFFIXES = re.compile(
    r"\b(?:llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|limited|co|company|"
    r"plc|gmbh|llp|lp|holdings|group)\b\.?",
    re.IGNORECASE,
)
_TITLE_ABBREVS = {
    "sr": "senior",
    "snr": "senior",
    "jr": "junior",
    "jnr": "junior",
    "mgr": "manager",
    "eng": "engineer",
    "engr": "engineer",
    "dev": "developer",
    "ml": "machine learning",
    "ai": "ai",  # kept as-is, listed for clarity
}


def _clean(s):
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    if not isinstance(s, str):
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _norm_company(s):
    # Guard non-str BEFORE the regex sub: pandas yields float NaN for an empty company cell
    # (as it does for description — see fetch.py), and NaN is truthy, so `s or ""` would keep
    # it and _COMPANY_SUFFIXES.sub() would TypeError on a float. _clean()'s own isinstance
    # guard runs too late (after the sub). Mirrors _norm_location's non-str -> "".
    s = _clean(_COMPANY_SUFFIXES.sub(" ", s if isinstance(s, str) else ""))
    return s


def _norm_title(s):
    toks = _clean(s).split()
    expanded = []
    for t in toks:
        expanded.append(_TITLE_ABBREVS.get(t, t))
    return " ".join(expanded).strip()


# Bumped whenever the fingerprint normalization changes; gates a one-time recompute of stored
# fingerprints (see core._recompute_fingerprints) so existing rows and new inserts share a
# key space. Current scheme: comma-aware _norm_location with tail-only metro-cruft + state-abbrev.
_NORM_VERSION = 3

# US state full-name -> 2-letter abbreviation. Applied only to a location's trailing
# state/region component (see _norm_location), so a city named after a state
# ("New York, NY") is never rewritten to "ny ny".
_US_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}
_COUNTRY_TOKENS = {"united states", "usa", "us", "u s", "u s a"}
# LinkedIn metro labels: "...New York Metropolitan Area", "Greater Boston",
# "San Francisco Bay Area" — drop the cruft, keep the place name.
_METRO_CRUFT = re.compile(r"\b(?:greater|metropolitan|metro|area|region)\b")


def _norm_location(s):
    """Blocking-key form of a location. Parse the raw "City, State, Country" structure
    BEFORE _clean() flattens the commas: drop the country, then canonicalize the trailing
    state/region component (metro cruft removed; full state name -> 2-letter abbrev) while
    leaving the city verbatim. Handling city and state separately is what lets "Rochester,
    New York Metropolitan Area" and "Rochester, NY" collapse to one key without mangling a
    city literally named after a state ("New York, NY" stays "new york ny"). Deliberately
    conservative: a present state is NOT dropped to match a state-absent variant — over-
    matching (a false "ALREADY APPLIED") is the worse error here (see the title-match note)."""
    if not isinstance(s, str):
        return ""
    parts = [_clean(p) for p in s.split(",")]
    parts = [p for p in parts if p and p not in _COUNTRY_TOKENS]
    if not parts:
        return ""
    # Strip metro cruft from the TRAILING (state/region) component only, then map a full state
    # name to its abbrev when a city precedes it. Kept to the tail on purpose: 'area'/'region'
    # are ordinary words inside real city names ("Capital Region", "Bay Area"), so stripping
    # them from city components over-collapses distinct places — a false "ALREADY APPLIED" is
    # the worse error. This handles the documented "Rochester, New York Metropolitan Area" vs
    # "Rochester, NY" case (cruft sits in the tail) while leaving the city verbatim. Metro cruft
    # that LinkedIn puts in the city slot ("Greater Boston") is left as a known under-match.
    tail = re.sub(r"\s+", " ", _METRO_CRUFT.sub(" ", parts[-1])).strip()
    if len(parts) > 1:
        tail = _US_STATES.get(tail, tail)
    parts[-1] = tail
    return " ".join(p for p in parts if p).strip()


def _fingerprint(company, location):
    return f"{_norm_company(company)}|{_norm_location(location)}"


def _find_repost(conn, fingerprint, norm_title, exclude_url=None):
    """Return the canonical job_url of an earlier posting for the same role, or None.
    A match requires the same fingerprint (normalized company+location) AND an exact
    normalized-title match. The canonical url is the earliest match's own repost_of
    when set, so every relisting in a chain points at the single first posting."""
    if not fingerprint or not norm_title:
        return None
    rows = conn.execute(
        "SELECT job_url, repost_of FROM jobs "
        "WHERE fingerprint=? AND norm_title=? ORDER BY first_seen ASC",
        (fingerprint, norm_title),
    ).fetchall()
    for r in rows:
        if exclude_url and r["job_url"] == exclude_url:
            continue
        return r["repost_of"] or r["job_url"]
    return None


# ------------------------------------------------------------- chain reconcile

def skip_decided_reposts(conn, forward=True, restore=True):
    """Skip the paid eval for a relisting whose role the user has already decided. A repost links
    to its canonical original via `repost_of`, and every applied/passed/reject decision propagates
    to that canonical (see _chain_targets), so the canonical's decision state is authoritative for
    the whole chain. Matched rows get status='repost_decided' (skipped by evaluate_new_jobs).
    Mirrors apply_salary_filter / apply_hard_filters — a deterministic pre-eval pass.

    `forward`/`restore` select the directions to run. `run` calls the RESTORE direction before
    the salary/hard filters and the FORWARD direction after them: a restored row must re-face
    the CURRENT rules before the paid eval (the same re-facing contract requeue_error_rows'
    stage placement provides), while forward-skipping stays after the filters so a rule keeps
    first claim on a 'new' relisting. Every other caller runs both (order-independent there —
    no eval follows)."""
    # Reconciles in BOTH directions from current decision state, so it self-corrects: a 'new'
    # relisting of a decided chain is skipped, and a previously-skipped relisting whose chain
    # decision was since undone returns to 'new' to be (re-)evaluated. Without the reverse pass
    # an undo would strand the sibling at 'repost_decided' forever (never re-evaluated).
    # Both directions key on COALESCE(repost_of, job_url) — the row's own canonical — which is
    # sound for the decisions this propagates: applied/passed/manual-reject all write chain-wide
    # (incl. the canonical) via the propagate_* helpers. The COALESCE matters twice: a decided
    # row that is itself a still-'new' CANONICAL (marked before its eval ran) is skipped by its
    # own stamp, and an unlinked ex-canonical (dupe_unlink sets repost_of=NULL) is judged by its
    # own row — kept skipped while its copied decision stands, restored once that is undone
    # (bare `repost_of NOT IN` was NULL-false for such rows and stranded them forever).
    # NOT chain-wide for the deterministic rule filters — apply_hard_filters stamps
    # filter_source on the single matched row only — so a rule-rejected NON-canonical relisting
    # can leave its canonical "undecided" here while effective_decision (chain-wide) reports the
    # role rejected. Accepted: the only cost is one extra eval on a later relisting whose own
    # text didn't re-trip the rule; no wrong verdict.
    # The forward pass also UPGRADES 'repost_evaluated' rows: a user decision is the more
    # informative skip reason, and leaving the old label would keep the row in the report's
    # "already-evaluated" section after the user acted on the role.
    # (job_url IS NOT NULL: SQLite tolerates NULL in a TEXT PRIMARY KEY, and one NULL in the
    # subquery would poison NOT IN to never-true, silently disabling the reverse pass.)
    decided = ("(SELECT job_url FROM jobs WHERE (app_status IS NOT NULL "
               "OR filter_source IS NOT NULL) AND job_url IS NOT NULL)")
    if forward:
        cur = conn.execute(
            f"UPDATE jobs SET status='{STATUS_REPOST_DECIDED}' "
            f"WHERE status IN ('{STATUS_NEW}', '{STATUS_REPOST_EVALUATED}') "
            f"AND COALESCE(repost_of, job_url) IN {decided}"
        )
        if cur.rowcount:
            print(f"[repost-skip] {cur.rowcount} relistings of already-decided roles (eval skipped, cost saved)")
    if restore:
        # Own-row guard (same as the evaluated pass's): a row whose OWN stamp survives while
        # its canonical reads undecided (legacy/partially-synced rows only — propagate/clear
        # write chain-wide) must not be released to the paid eval.
        rev = conn.execute(
            f"UPDATE jobs SET status='{STATUS_NEW}' WHERE status='{STATUS_REPOST_DECIDED}' "
            f"AND app_status IS NULL AND filter_source IS NULL "
            f"AND COALESCE(repost_of, job_url) NOT IN {decided}"
        )
        if rev.rowcount:
            print(f"[repost-skip] {rev.rowcount} relistings restored to 'new' (chain decision undone)")
    conn.commit()


def skip_evaluated_reposts(conn, forward=True, restore=True):
    """Skip the paid eval for a relisting whose role chain already holds a verdict — the eval's
    answer for the ROLE exists; re-asking just re-samples a noisy judge (a 2026-07 backtest over
    the real DB found only 72% of multi-eval chains verdict-stable) while costing money on every
    relisting cycle (Adzuna re-serves its whole active pool daily). Matched rows get
    status='repost_evaluated' and keep verdict=NULL; readers see the role's verdict via
    effective_decision's chain_verdict (most favorable member — see states.VERDICT_FAVOR).
    Runs AFTER skip_decided_reposts (a user decision is the more informative skip reason) and,
    like it, mirrors the deterministic pre-eval filter passes. `forward`/`restore` split the
    directions for `run`'s stage order — restores happen BEFORE the salary/hard filters so a
    restored row re-faces the current rules (see skip_decided_reposts).

    The match keys off canonicals of any JUDGE-verdict-bearing member: verdicts don't propagate
    chain-wide the way decisions do, so a chain's only verdict may sit on a sibling (e.g. the
    canonical was salary-filtered, a later relisting was evaluated). The subquery requires
    status='evaluated' because verdict alone is NOT eval-only — apply_hard_filters stamps a
    synthetic verdict=GATE_FAIL on rule_filtered rows, and counting those would silently close
    the gap the decided pass's docstring deliberately pays one extra eval for (a relisting whose
    reworded text no longer trips the rule must get its safety-valve eval, not inherit a rule
    stamp dressed up as a judge verdict). It also requires the verdict to be IN the current
    vocabulary, matching chain_verdict's `in VERDICT_FAVOR` filter — an off-vocabulary legacy
    verdict (pre-CHECK DBs are unconstrained) must not skip rows it can't label. Both directions
    use COALESCE(repost_of, job_url) so a still-'new' CANONICAL whose verdict sits on a sibling
    is skipped too (requeued error rows, dupe merges with a not-yet-evaluated winner) and stays
    skipped through the reverse pass.
    The reverse pass restores a row only while it is UNDECIDED: mark_posting stamps app_status
    without lifting status, so without the guard an unlink would hand an APPLIED row back to
    the paid eval (rejects are already lifted to rule_filtered by _REJECT_SET; the
    filter_source arm is belt-and-braces for legacy rows). No full-text exception for
    snippet-evaluated (Adzuna) canonicals: the same backtest found 5 of 14,058 chains would
    ever qualify — not worth the code path."""
    _verdicts = sql_list(VERDICT_FAVOR)
    evaluated = (f"(SELECT COALESCE(repost_of, job_url) FROM jobs "
                 f"WHERE verdict IN ({_verdicts}) AND status='{STATUS_EVALUATED}' "
                 f"AND job_url IS NOT NULL)")
    if forward:
        # verdict IS NULL: a manually reset row carrying its own verdict must keep its re-eval.
        cur = conn.execute(
            f"UPDATE jobs SET status='{STATUS_REPOST_EVALUATED}' WHERE status='{STATUS_NEW}' "
            f"AND verdict IS NULL AND COALESCE(repost_of, job_url) IN {evaluated}"
        )
        if cur.rowcount:
            print(f"[repost-skip] {cur.rowcount} relistings of already-evaluated roles (eval skipped, cost saved)")
    if restore:
        # Restore direction so no undecided row is stranded: dupe_unlink clears repost_of (the
        # row is no longer a relisting of anything → it needs its own eval), and a chain can
        # lose its verdicts (e.g. a manual reset for re-evaluation).
        rev = conn.execute(
            f"UPDATE jobs SET status='{STATUS_NEW}' WHERE status='{STATUS_REPOST_EVALUATED}' "
            f"AND app_status IS NULL AND filter_source IS NULL "
            f"AND COALESCE(repost_of, job_url) NOT IN {evaluated}"
        )
        if rev.rowcount:
            print(f"[repost-skip] {rev.rowcount} relistings restored to 'new' (chain unlinked or verdict cleared)")
    conn.commit()


def _reconcile_chain_skips(conn, canonical_url):
    """Chain-SCOPED skip-label reconcile for the decision/dupe write paths — the one name for
    'this chain's decision state just changed; fix its members' skip labels NOW'. Semantics:
      * decided, both directions: a decision skips/upgrades still-pending members immediately
        (decided rows never need the eval, so no filter re-facing is owed), and an undo
        releases them;
      * evaluated, RESTORE direction only: an unlink/undo must free stranded rows, but the
        evaluated-FORWARD skip deliberately stays in `run`'s post-filter phase — re-skipping a
        just-released row here would let it bypass the current salary/hard rules (the
        restore-before-filters contract), so a released row honestly shows as 'new' (the UI
        badges it with its chain verdict) until the next run labels it after the filters.
    Scoped to one chain via the indexable `(repost_of=? OR job_url=?)` form — the global
    passes' subqueries full-scan the table, which measured ~0.7-1.0s per call at 20k rows;
    this form measured ~90ms. (COALESCE(repost_of, job_url)=? is NOT indexable — don't.)
    Quiet on purpose (service code, like resolve_posting): the callers report their own
    outcome, and a reconcile failure must never unwind a decision that already committed —
    it degrades to a warning; the global passes self-heal on the next run."""
    member = "(repost_of=? OR job_url=?)"
    u2 = (canonical_url, canonical_url)
    _verdicts = sql_list(VERDICT_FAVOR)
    try:
        decided = conn.execute(
            f"SELECT 1 FROM jobs WHERE {member} AND (app_status IS NOT NULL "
            f"OR filter_source IS NOT NULL) LIMIT 1", u2
        ).fetchone() is not None
        if decided:
            conn.execute(
                f"UPDATE jobs SET status='{STATUS_REPOST_DECIDED}' "
                f"WHERE {member} AND status IN ('{STATUS_NEW}', '{STATUS_REPOST_EVALUATED}')",
                u2,
            )
        else:
            # Undone: release decided-skips; keep evaluated-skips only while the chain still
            # holds a judge verdict AND the row itself is undecided (mirrors the global passes).
            conn.execute(
                f"UPDATE jobs SET status='{STATUS_NEW}' WHERE {member} "
                f"AND status='{STATUS_REPOST_DECIDED}' "
                f"AND app_status IS NULL AND filter_source IS NULL", u2,
            )
            has_verdict = conn.execute(
                f"SELECT 1 FROM jobs WHERE {member} AND verdict IN ({_verdicts}) "
                f"AND status='{STATUS_EVALUATED}' LIMIT 1", u2
            ).fetchone() is not None
            if not has_verdict:
                conn.execute(
                    f"UPDATE jobs SET status='{STATUS_NEW}' WHERE {member} "
                    f"AND status='{STATUS_REPOST_EVALUATED}' "
                    f"AND app_status IS NULL AND filter_source IS NULL", u2,
                )
        conn.commit()
    except Exception as e:  # noqa: BLE001 — the decision is durable; labels self-heal next run
        print(f"[repost-skip] chain reconcile failed (labels self-heal next run): {e}",
              file=sys.stderr)


# ------------------------------------------------------ url resolution / chain reads

def resolve_posting(conn, url):
    """Resolve a url (full or unique substring) to a single jobs row. Returns `(row, error)`
    with exactly one non-None — the same shape as `dupe_resolve`, so both front-ends (CLI
    prints the error, web UI returns it as JSON) share one resolution behavior. No printing
    here: this is service code, not a command."""
    if not url:
        return None, "provide a url (full or unique substring of the job_url)"
    # Escape LIKE metacharacters so a substring containing % or _ matches literally
    # (the resolved row drives a destructive UPDATE, so a mis-match must not happen).
    safe = url.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    matches = conn.execute(
        "SELECT * FROM jobs WHERE job_url LIKE ? ESCAPE '\\'", (f"%{safe}%",)
    ).fetchall()
    if not matches:
        return None, f"no posting matches '{url}'"
    if len(matches) > 1:
        # A full job_url is a substring of any longer one (LinkedIn ids nest: .../view/123 is a
        # substring of .../view/1234), so an exact url would otherwise read as "ambiguous". When the
        # input exactly equals one row's job_url, take it — that's the caller naming a specific row
        # (always true for the web UI, which passes full urls), not a fuzzy substring.
        exact = [m for m in matches if m["job_url"] == url]
        if len(exact) == 1:
            return exact[0], None
        listing = "\n".join(f"    {m['title']} — {m['company']}  {m['job_url']}" for m in matches)
        return None, f"'{url}' is ambiguous ({len(matches)} matches):\n{listing}"
    return matches[0], None


def _chain_members(conn, canonical_url):
    """All job_urls in the flat chain rooted at `canonical_url`: the canonical itself
    plus every relisting pointing at it."""
    rows = conn.execute(
        "SELECT job_url FROM jobs WHERE job_url=? OR repost_of=?", (canonical_url, canonical_url)
    ).fetchall()
    return {r["job_url"] for r in rows}


def _chain_targets(conn, m):
    """The set of job_urls a per-posting decision should apply to: the entire repost chain of row
    `m` — its canonical original PLUS every relisting pointing at it — so a decision follows the
    role across all relistings, not just the one named. (Resolving only the named row and its
    repost_of would leave sibling relistings with stale verdicts/overrides.) Same set as
    _chain_members, keyed by a row rather than a canonical url."""
    return _chain_members(conn, m["repost_of"] or m["job_url"])


def _decide(rows):
    """Reduce a set of chain-member rows to the user's decision dict, or None if undecided.
    PURE (no DB). Rows must carry app_status, status_date, filter_source, filter_gate, filter_date.
    'applied' outranks 'passed'; the reject side is any member with a filter_source. Shared by
    _chain_decision and effective_decision so the reduction logic lives once."""
    applied = next((r for r in rows if r["app_status"] == "applied"), None)
    passed = next((r for r in rows if r["app_status"] == "passed"), None)
    rej = next((r for r in rows if r["filter_source"]), None)
    app_row = applied or passed
    if app_row is None and rej is None:
        return None
    return {
        "app_status": app_row["app_status"] if app_row else None,
        "status_date": app_row["status_date"] if app_row else None,
        # The outcome cache rides the applied/passed member — same propagation as app_status
        # (_recompute_outcome writes it chain-wide), so any member's copy is authoritative.
        "outcome_status": app_row["outcome_status"] if app_row else None,
        "outcome_date": app_row["outcome_date"] if app_row else None,
        "resume_variant": app_row["resume_variant"] if app_row else None,
        "channel": app_row["channel"] if app_row else None,
        "reject": rej is not None,
        "filter_gate": rej["filter_gate"] if rej else None,
        "filter_date": rej["filter_date"] if rej else None,
    }


def _chain_decision(conn, member_urls):
    """The user's decision across an arbitrary set of chain members, or None if undecided. Used to
    detect a cross-chain conflict and to copy the surviving decision onto newly-linked members
    (where the member set isn't a single canonical-rooted chain, so it can't share
    effective_decision's one-query path)."""
    if not member_urls:
        return None
    qs = ",".join("?" * len(member_urls))
    rows = conn.execute(
        f"SELECT app_status, status_date, outcome_status, outcome_date, resume_variant, "
        f"channel, filter_source, filter_gate, filter_date "
        f"FROM jobs WHERE job_url IN ({qs})",
        tuple(member_urls),
    ).fetchall()
    return _decide(rows)


def _decision_sig(dec):
    """Conflict-comparison signature: two decided chains clash unless these match."""
    if dec is None:
        return None
    return (dec["app_status"], dec["reject"], dec["filter_gate"])


def _fmt_decision(dec):
    if dec is None:
        return "undecided"
    bits = []
    if dec["app_status"]:
        bits.append(f"{dec['app_status']} {dec['status_date'] or ''}".strip())
    if dec["reject"]:
        bits.append(f"rejected (gate: {dec['filter_gate']})")
    return ", ".join(bits) or "undecided"


# Columns effective_decision / effective_decisions fetch per chain member: what _decide needs to
# reduce a decision, PLUS the canonical's first_seen/verdict for the repost note, PLUS repost_of so
# the batched variant can group members back to their canonical. Both queries share this list so
# their column shapes can't drift.
_MEMBER_COLS = ("job_url, repost_of, first_seen, verdict, status, fit_score, app_status, "
                "status_date, outcome_status, outcome_date, resume_variant, channel, "
                "filter_source, filter_gate, filter_date")


def effective_decision(conn, row):
    """The single source of truth for "what has the user decided about this row's role?",
    spanning the whole repost chain. Used by the report (_repost_info), the web UI
    (row_to_dict), and the dupe conflict guard — so the three can never drift.

    Returns a dict (never None):
      app_status            'applied' | 'passed' | None  (applied outranks, chain-wide)
      status_date           date the surviving app_status was set, or None
      outcome_status        the chain's post-application state: latest non-note app_events
                            type (the cache _recompute_outcome maintains), or None —
                            None on an applied chain means "no response yet" (the
                            follow-up bucket)
      outcome_date          that event's event_date, or None
      resume_variant        free text recorded at apply time (or via set_resume), or None
      channel               how the application went out (states.ALL_CHANNELS: direct |
                            agency | referral), recorded at apply time or via set_channel,
                            or None — same applied-only propagation as resume_variant
      reject                bool — any chain member is a hard-fail override
      filter_gate, filter_date  the surviving reject's attribution, or None
      is_repost             True if `row` itself is a relisting (repost_of set)
      original_first_seen   the canonical original's first_seen (for the repost note), or None
      original_verdict      the canonical original's model verdict, or None
      chain_verdict         the ROLE's verdict: most favorable across all members' (noisy)
                            JUDGE verdicts (status='evaluated' only — a rule-stamped
                            GATE_FAIL on a rule_filtered row is a deterministic text match,
                            not a judgment), per states.VERDICT_FAVOR, or None if no judge
                            ever scored the chain. What readers show for a
                            'repost_evaluated' row, whose own verdict stays NULL.
      chain_fit_score       the winning member's fit_score (best fit among the most
                            favorable verdicts), or None — the triage-sort fallback for
                            rows whose own fit_score is NULL.
    """
    canonical_url = row["repost_of"] or row["job_url"]
    # One query for the whole chain: fetch every member row with the columns _decide needs PLUS the
    # canonical's first_seen/verdict for the repost note (the canonical is itself a member). Replaces
    # the old three-query path (_chain_members + _chain_decision + a separate canonical SELECT).
    rows = conn.execute(
        f"SELECT {_MEMBER_COLS} FROM jobs WHERE job_url=? OR repost_of=?",
        (canonical_url, canonical_url),
    ).fetchall()
    return _effective_from_members(row, rows)


def _effective_from_members(row, members):
    """Build effective_decision's return dict for `row` from its already-fetched chain `members`.
    PURE (no DB). Shared by the single-row effective_decision and the batched effective_decisions
    so the dict shape and the _decide reduction have exactly one implementation."""
    canonical_url = row["repost_of"] or row["job_url"]
    dec = _decide(members) or {}
    # The canonical's own row (None only if repost_of points at a row that doesn't exist — an
    # orphaned manual edit; original_* then stay None, as before).
    canon = next((m for m in members if m["job_url"] == canonical_url), None)
    # Judge verdicts only: status='evaluated' excludes the synthetic GATE_FAIL that
    # apply_hard_filters stamps on rule_filtered rows (same predicate as skip_evaluated_reposts).
    # The WINNING member (most favorable verdict, best fit as tiebreak) supplies both the chain
    # verdict and the chain fit score — the latter lets a verdict-less 'repost_evaluated' row
    # sort where its role belongs instead of sinking to the bottom band (fit NULL reads as 0).
    winner = max(
        (m for m in members
         if m["status"] == STATUS_EVALUATED and m["verdict"] in VERDICT_FAVOR),
        key=lambda m: (VERDICT_FAVOR[m["verdict"]], m["fit_score"] or 0), default=None,
    )
    return {
        "app_status": dec.get("app_status"),
        "status_date": dec.get("status_date"),
        "outcome_status": dec.get("outcome_status"),
        "outcome_date": dec.get("outcome_date"),
        "resume_variant": dec.get("resume_variant"),
        "channel": dec.get("channel"),
        "reject": dec.get("reject", False),
        "filter_gate": dec.get("filter_gate"),
        "filter_date": dec.get("filter_date"),
        "is_repost": bool(row["repost_of"]),
        "original_first_seen": canon["first_seen"] if canon else None,
        "original_verdict": canon["verdict"] if canon else None,
        "chain_verdict": winner["verdict"] if winner else None,
        "chain_fit_score": winner["fit_score"] if winner else None,
    }


def effective_decisions(conn, rows):
    """Batched effective_decision: returns {job_url: decision-dict} for a whole row set in a couple
    of queries instead of one query per row. Same dict shape and same _decide reduction as
    effective_decision (it delegates to the shared _effective_from_members) — still one source of
    truth, just amortized. The list views (web UI) use this; calling effective_decision per row was
    O(N) round-trips and made the backlog view take seconds."""
    rows = list(rows)
    if not rows:
        return {}
    canon_urls = list({r["repost_of"] or r["job_url"] for r in rows})
    # Fetch every chain member (canonical + its relistings) for all canonicals at once, grouped back
    # to their canonical url. Chunked to stay under SQLite's bound-variable limit (999 on old builds):
    # each chunk binds 2*len(part) params (job_url IN ... OR repost_of IN ...), so 400 keeps it <800.
    members_by_canon = {}
    CHUNK = 400
    for i in range(0, len(canon_urls), CHUNK):
        part = canon_urls[i:i + CHUNK]
        qs = ",".join("?" * len(part))
        q = (f"SELECT {_MEMBER_COLS} FROM jobs "
             f"WHERE job_url IN ({qs}) OR repost_of IN ({qs})")
        for m in conn.execute(q, tuple(part) + tuple(part)):
            members_by_canon.setdefault(m["repost_of"] or m["job_url"], []).append(m)
    return {
        r["job_url"]: _effective_from_members(r, members_by_canon.get(r["repost_of"] or r["job_url"], []))
        for r in rows
    }


# ----------------------------------------------- chain writes (decision propagation)
#
# A per-posting decision applies to the whole repost chain, not just the named row. These three
# functions own those writes so the SET clauses — and the load-bearing status='new' -> 'rule_filtered'
# lift on a reject — live in ONE place. cmd_mark, cmd_reject, and dupe_commit all route through
# them, so the write paths can't drift the way they did when each had its own inline UPDATE.
# (The read counterpart is effective_decision; callers commit.)

# SET clause for a manual hard-fail override: stamp 'manual' + attribution, and lift a
# never-evaluated row — still-'new' OR eval-skipped 'repost_evaluated' (its own verdict is NULL
# by construction) — to 'rule_filtered' so the paid eval can never see it again (an
# already-evaluated row keeps its status — the report groups by filter_source either way).
# Without the repost_evaluated arm, a rejected skipped relisting kept its skip status, and a
# later dupe-unlink's reverse pass would hand the REJECTED row back to the eval queue.
# Two placeholders: (gate, date).
_REJECT_SET = ("filter_source='manual', filter_gate=?, filter_date=?, "
               f"status=CASE WHEN status IN ('{STATUS_NEW}', '{STATUS_REPOST_EVALUATED}') "
               f"THEN '{STATUS_RULE_FILTERED}' ELSE status END")


def propagate_app_status(conn, member_urls, status, status_date, resume_variant=None,
                         channel=None):
    """Set the user's applied/passed decision (or clear it, status=None) across every member of a
    repost chain, so the decision follows the role across all relistings. Shared by cmd_mark and
    the dupe merge. `resume_variant` and `channel` are APPLIED-ONLY fields with identical
    propagation: with status='applied' each is written when given, INHERITED from the chain's
    stored value when not (a re-assert without one can't blank a stored value — and must not
    strand a late-fetched, never-stamped relisting at applied-with-NULL beside it, since _decide
    reads the fields off an arbitrary applied member), and any other status — undo OR a switch
    to 'passed' — clears them chain-wide, so neither can sit invisibly on a non-applied chain
    (set_resume/set_channel refuse those and the UI only renders the fields on applied cards).
    Empty/whitespace input reads as "not given", never as "blank it" — set_resume/set_channel
    are the explicit clear paths. Channel's ALL_CHANNELS validation lives in the callers
    (mark_posting / set_channel), not here — the dupe merge feeds back already-stored values.
    The write is ONE statement over all four columns so every member always ends identical —
    the "any member's copy is authoritative" premise is enforced here, not assumed. The outcome
    CACHE columns are deliberately not touched — _recompute_outcome is their one writer, called
    right after this."""
    members = set(member_urls)
    if not members:
        return
    qs = ",".join("?" * len(members))
    resume_variant = (resume_variant or "").strip() or None
    channel = (channel or "").strip() or None
    if status == "applied" and (resume_variant is None or channel is None):
        # ONE aggregate read covers both inherits: MAX() skips NULLs, and the single-UPDATE
        # uniform-write rule below means every non-NULL copy within a chain is identical,
        # so "MAX over the members" == "any member's stored value". (Mixed non-NULL values
        # only exist across two chains at merge time, and dupe_commit pre-coalesces those
        # before calling here — they never reach this read.)
        stored = conn.execute(
            f"SELECT MAX(resume_variant), MAX(channel) FROM jobs WHERE job_url IN ({qs})",
            tuple(members),
        ).fetchone()
        resume_variant = resume_variant or stored[0]
        channel = channel or stored[1]
    conn.execute(
        f"UPDATE jobs SET app_status=?, status_date=?, resume_variant=?, channel=? "
        f"WHERE job_url IN ({qs})",
        (status, status_date,
         resume_variant if status == "applied" else None,
         channel if status == "applied" else None, *members),
    )


def propagate_reject(conn, member_urls, gate, date, force_url=None, overwrite_manual=False):
    """Stamp the manual hard-fail override across a chain, with the 'don't clobber a sibling's
    rule:<name> attribution' guard in one place.
      force_url         a url stamped unconditionally — the explicitly rejected row, re-attributed
                        even if a filters.yaml rule had auto-failed it (None in the merge case).
      overwrite_manual  whether an existing filter_source='manual' may be overwritten: True for a
                        direct reject re-asserting on siblings; False for a merge, which leaves ANY
                        prior attribution (manual or rule) intact and only fills in un-attributed rows."""
    members = set(member_urls)
    if force_url is not None:
        conn.execute(f"UPDATE jobs SET {_REJECT_SET} WHERE job_url=?", (gate, date, force_url))
        members.discard(force_url)
    if members:
        qs = ",".join("?" * len(members))
        guard = "filter_source IS NULL" + (" OR filter_source='manual'" if overwrite_manual else "")
        conn.execute(
            f"UPDATE jobs SET {_REJECT_SET} WHERE job_url IN ({qs}) AND ({guard})",
            (gate, date, *members),
        )


def clear_reject(conn, member_urls):
    """Undo a manual reject across a chain: clear ONLY filter_source='manual' rows (a sibling
    auto-failed by a filters.yaml rule keeps its 'rule:<name>'), and restore status='new' for a row
    the reject had lifted out of 'new' before it was ever evaluated (rule_filtered + no verdict), so
    it isn't permanently excluded from the eval stage."""
    members = set(member_urls)
    if not members:
        return
    qs = ",".join("?" * len(members))
    conn.execute(
        "UPDATE jobs SET filter_source=NULL, filter_gate=NULL, filter_date=NULL, "
        f"status=CASE WHEN status='{STATUS_RULE_FILTERED}' AND verdict IS NULL "
        f"THEN '{STATUS_NEW}' ELSE status END "
        f"WHERE job_url IN ({qs}) AND filter_source='manual'",
        tuple(members),
    )


# ----------------------------------------------------- decision services
#
# The one implementation of "apply this user decision to a resolved posting", shared by the
# CLI (cmd_mark / cmd_reject print the message) and the web UI (api_decision returns it as
# JSON) — the same single-core-two-front-ends shape as the dupe trio below. Both return
# (ok, message, affected_urls, exempt_urls); `affected` is the whole repost chain, which the
# UI uses to update sibling cards. `exempt` is the subset the UI's hide-decided filter must
# keep visible — computed HERE because only the core knows which rows' *displayed* decision
# state each operation actually changes (the UI briefly re-derived it from propagation
# behavior and got every edge wrong: rule-vs-manual survivors, merge-stamped siblings,
# whole-chain over-exemption). The CLI ignores it. Callers resolve the row first
# (resolve_posting) so the CLI can keep it for the filters.yaml rule promotion.

def _member_decisions(conn, urls):
    """The per-row decision fields for a set of chain members — the inputs to the exempt
    computation, read either before (forward ops) or after (undos) the mutation."""
    urls = sorted(urls)
    qs = ",".join("?" * len(urls))
    return conn.execute(
        f"SELECT job_url, app_status, filter_source FROM jobs WHERE job_url IN ({qs})",
        tuple(urls),
    ).fetchall()


def _forward_exempt(conn, row, targets):
    """Exempt list for a decision landing on `row`'s chain, computed BEFORE propagation.
    The operative contract is rows whose displayed state flips undecided→decided — the only
    transition the hide filter acts on — plus the named row as the interaction handle. (A
    forward op can also REPLACE a displayed decision, e.g. applied outranking a sibling's
    manual reject; that row stays unexempted on purpose — it was already displaying decided
    and may be deliberately hidden.) So: the whole chain when it displayed undecided, else
    just the named row."""
    pre = _member_decisions(conn, targets)
    if any(r["app_status"] or r["filter_source"] for r in pre):
        return [row["job_url"]]
    return sorted(targets)


def _undo_exempt(conn, row, targets):
    """Exempt list for an undo on `row`'s chain, computed AFTER the clear: empty when the
    chain now displays undecided (nothing gets hidden), else the named row (the user's click
    must not vanish the card) plus every member still carrying an app_status or MANUAL
    reject — the handles for the next undo. Rule-stamped survivors stay unexempted:
    display-only, correctly hidden."""
    post = _member_decisions(conn, targets)
    if not any(r["app_status"] or r["filter_source"] for r in post):
        return []
    keep = {r["job_url"] for r in post if r["app_status"] or r["filter_source"] == "manual"}
    keep.add(row["job_url"])
    return sorted(keep)


def mark_posting(conn, row, status, resume_variant=None, channel=None):
    """Set (status='applied'|'passed') or clear (status=None) the user's decision across
    `row`'s whole repost chain. `resume_variant` and `channel` (optional, applied only)
    record which resume went out and through which channel (states.ALL_CHANNELS).
    Returns (ok, message, affected_urls, exempt_urls)."""
    channel, err = _norm_channel(channel)
    if err:
        return False, err, [], []
    targets = _chain_targets(conn, row)
    exempt = _forward_exempt(conn, row, targets) if status else None  # pre-state read
    stamp = date.today().isoformat() if status else None
    propagate_app_status(conn, targets, status, stamp, resume_variant, channel)
    # Sync the outcome cache with the decision: re-applying restores the outcome from any
    # surviving event history; an undo (or a switch to 'passed') clears the cached columns
    # chain-wide while the app_events rows themselves are KEPT — history is never destroyed
    # by a decision toggle, and the next 'applied' recomputes it right back.
    _recompute_outcome(conn, row["repost_of"] or row["job_url"], targets)
    conn.commit()
    # Reconcile this chain's skip labels immediately: a decision must upgrade any
    # 'repost_evaluated' sibling to 'repost_decided' (and an undo must release skipped rows)
    # NOW, not at the next run — a report rebuild or UI refresh in between would show the
    # stale label. Chain-scoped: the global passes cost ~1s per call and every triage click
    # lands here (see _reconcile_chain_skips).
    _reconcile_chain_skips(conn, row["repost_of"] or row["job_url"])
    if exempt is None:
        exempt = _undo_exempt(conn, row, targets)  # post-state read
    verb = f"marked {status}" if status else "cleared status"
    msg = f"{verb}: {row['title']} — {row['company']}" + (f" ({stamp})" if status else "")
    if not status:
        # Lifecycle events only: a notes-only history restores no outcome, so the
        # "re-applying restores it" promise must not be made for it.
        kept = _count_events(conn, targets, exclude_notes=True)
        if kept:
            msg += f" — outcome history kept ({kept} event(s); re-applying restores it)"
    return True, msg, sorted(targets), exempt


def reject_posting(conn, row, gate, undo=False):
    """Apply (or with undo=True clear) the manual hard-fail override across `row`'s chain.
    Returns (ok, message, affected_urls, exempt_urls). The named row is always (re)stamped —
    the user is overruling it, possibly re-attributing a filters.yaml auto-fail; siblings are
    stamped too but never clobber a rule:<name> attribution (see propagate_reject)."""
    targets = _chain_targets(conn, row)
    if undo:
        clear_reject(conn, targets)
        conn.commit()
        # Reconcile this chain now (see mark_posting): an undone reject may release skipped
        # siblings back to 'new' (they re-face the filters in the next run, before any eval).
        _reconcile_chain_skips(conn, row["repost_of"] or row["job_url"])
        exempt = _undo_exempt(conn, row, targets)
        return True, f"cleared override: {row['title']} — {row['company']}", sorted(targets), exempt
    if gate not in GATE_NAMES + ["other"]:
        return False, f"gate must be one of {GATE_NAMES + ['other']}", [], []
    exempt = _forward_exempt(conn, row, targets)  # pre-state read
    today = date.today().isoformat()
    propagate_reject(conn, targets, gate, today, force_url=row["job_url"], overwrite_manual=True)
    conn.commit()
    _reconcile_chain_skips(conn, row["repost_of"] or row["job_url"])
    return True, f"rejected (gate: {gate}): {row['title']} — {row['company']} ({today})", sorted(targets), exempt


# ------------------------------------------------------- outcome events
#
# Post-application lifecycle tracking: what happened AFTER 'applied' (screen, interview
# rounds, offer, employer rejection, ghosted, withdrew) plus free-text notes. History lives
# in the append-only `app_events` table (core._events_table_sql); the chain's CURRENT
# outcome is denormalized onto every member as jobs.outcome_status/outcome_date — the same
# cached-decision pattern as app_status, with _recompute_outcome as the ONE writer, so the
# follow-up query ("applied N days ago, no response") stays a pure-SQL predicate:
#   app_status='applied' AND outcome_status IS NULL AND status_date < cutoff
# An event row is written ONCE, keyed to the chain's canonical url AT WRITE TIME, and always
# read chain-wide (via _chain_targets) — so a later dupe merge unions both sides' histories
# with no data migration, and dupe_unlink leaves rows where they sit. Same
# single-core-two-front-ends shape as the decision services above: cmd_event and
# app.api_event are thin wrappers; event vocabulary is enforced HERE against
# states.ALL_EVENTS (no schema CHECK — see states.py's docstring).

def _count_events(conn, member_urls, exclude_notes=False):
    """Event count across a member-url set. `exclude_notes=True` counts only lifecycle
    events — what an outcome recompute can actually act on — for messages that promise
    restoration (a notes-only history restores no outcome)."""
    urls = sorted(member_urls)
    qs = ",".join("?" * len(urls))
    extra = " AND event_type != ?" if exclude_notes else ""
    params = (*urls, EVENT_NOTE) if exclude_notes else tuple(urls)
    return conn.execute(
        f"SELECT COUNT(*) FROM app_events WHERE job_url IN ({qs}){extra}", params
    ).fetchone()[0]


def _recompute_outcome(conn, canonical_url, members=None):
    """Re-derive the cached outcome columns for `canonical_url`'s whole chain from its stored
    events — the ONE writer of jobs.outcome_status/outcome_date. The cache is a pure function
    of (is the chain applied?, the chain's events): the latest non-note event wins (event_date
    order, insertion-order tiebreak for same-day events), and a chain that doesn't read
    'applied' gets NULLs — an outcome only means something for an application, and the events
    themselves are kept so a re-apply restores it. Caller commits (service-write convention,
    like propagate_app_status). `members` lets a caller that already holds the chain's member
    set (they all do, except dupe_unlink's remaining-winner chain) skip the membership
    re-query; when the answer is NULL and nothing is cached — every 'passed'/zero-event triage
    click — the chain-wide UPDATE is skipped entirely rather than dirtying pages with a
    NULL-over-NULL write."""
    members = set(members) if members is not None else _chain_members(conn, canonical_url)
    if not members:
        return
    urls = sorted(members)
    qs = ",".join("?" * len(urls))
    applied = conn.execute(
        f"SELECT 1 FROM jobs WHERE job_url IN ({qs}) AND app_status='applied' LIMIT 1",
        tuple(urls),
    ).fetchone() is not None
    latest = None
    if applied:
        latest = conn.execute(
            f"SELECT event_type, event_date FROM app_events WHERE job_url IN ({qs}) "
            f"AND event_type != ? ORDER BY event_date DESC, id DESC LIMIT 1",
            (*urls, EVENT_NOTE),
        ).fetchone()
    if latest is None:
        cached = conn.execute(
            f"SELECT 1 FROM jobs WHERE job_url IN ({qs}) AND outcome_status IS NOT NULL "
            f"LIMIT 1", tuple(urls),
        ).fetchone()
        if cached is None:
            return  # nothing to write, nothing to clear
    conn.execute(
        f"UPDATE jobs SET outcome_status=?, outcome_date=? WHERE job_url IN ({qs})",
        (latest["event_type"] if latest else None,
         latest["event_date"] if latest else None, *urls),
    )


def record_event(conn, row, event_type, event_date=None, note=None):
    """Record one outcome event on `row`'s chain. Lifecycle events (states.APP_EVENTS)
    require the chain's effective decision to be 'applied' — an interview on a role you
    never applied to is a data-entry error, not a state; bare 'note' events attach to any
    posting. Returns (ok, message, affected_urls, exempt_urls) like the decision services
    (exempt is just the interaction handle — an event never flips decided/undecided)."""
    if event_type not in ALL_EVENTS:
        return False, f"event type must be one of {list(ALL_EVENTS)}", [], []
    if event_type == EVENT_NOTE and not (note or "").strip():
        return False, "a 'note' event needs note text", [], []
    event_date = event_date or date.today().isoformat()
    try:
        d = date.fromisoformat(event_date)
    except (TypeError, ValueError):  # TypeError: a non-string from the JSON body
        return False, f"event date must be YYYY-MM-DD (got {event_date!r})", [], []
    # Sanity window, same rationale as core.parse_iso's (not importable here — core imports
    # chain): an absurd-but-parseable date is a typo, not information. The FUTURE bound is the
    # load-bearing one — _recompute_outcome is latest-event-date-wins, so one accepted
    # future-dated typo would pin the chain's outcome past every later real event, and
    # undo_event (insertion-order) couldn't reach it without unwinding everything after it.
    if not (date(2000, 1, 1) <= d <= date.today()):
        return False, (f"event date {d.isoformat()} is outside 2000-01-01..today — events "
                       f"record what already happened (a future date would pin the outcome, "
                       f"since the latest event wins)"), [], []
    event_date = d.isoformat()
    targets = _chain_targets(conn, row)
    if event_type in APP_EVENTS:
        dec = effective_decision(conn, row)
        if dec["app_status"] != "applied":
            state = _fmt_decision(_chain_decision(conn, targets))
            return False, (f"'{event_type}' needs the role marked applied first "
                           f"(currently: {state})"), [], []
    canonical = row["repost_of"] or row["job_url"]
    conn.execute(
        "INSERT INTO app_events (job_url, event_type, event_date, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (canonical, event_type, event_date, (note or "").strip() or None,
         datetime.now().isoformat(timespec="seconds")),
    )
    _recompute_outcome(conn, canonical, targets)
    conn.commit()
    msg = f"recorded {event_type} ({event_date}): {row['title']} — {row['company']}"
    return True, msg, sorted(targets), [row["job_url"]]


def undo_event(conn, row):
    """Delete the chain's most recently RECORDED event (insertion order, not event_date —
    'undo' means the user's last entry, which may have been backdated) and recompute the
    cache. Returns (ok, message, affected_urls, exempt_urls)."""
    targets = _chain_targets(conn, row)
    urls = sorted(targets)
    qs = ",".join("?" * len(urls))
    last = conn.execute(
        f"SELECT id, event_type, event_date FROM app_events WHERE job_url IN ({qs}) "
        f"ORDER BY id DESC LIMIT 1",
        tuple(urls),
    ).fetchone()
    if last is None:
        return False, f"no events recorded for: {row['title']} — {row['company']}", [], []
    conn.execute("DELETE FROM app_events WHERE id=?", (last["id"],))
    _recompute_outcome(conn, row["repost_of"] or row["job_url"], targets)
    conn.commit()
    msg = (f"removed last event — {last['event_type']} ({last['event_date']}): "
           f"{row['title']} — {row['company']}")
    return True, msg, sorted(targets), [row["job_url"]]


def chain_events(conn, row):
    """All events for `row`'s chain, chronological (event_date, insertion-order tiebreak) —
    the read counterpart of record_event, spanning every member url so merged histories
    union. Returns a list of app_events rows."""
    urls = sorted(_chain_targets(conn, row))
    qs = ",".join("?" * len(urls))
    return conn.execute(
        f"SELECT id, job_url, event_type, event_date, note, created_at "
        f"FROM app_events WHERE job_url IN ({qs}) ORDER BY event_date ASC, id ASC",
        tuple(urls),
    ).fetchall()


def _norm_channel(value):
    """The ONE copy of channel normalization + vocabulary validation, shared by every entry
    point to the column (mark_posting at apply time, set_channel after the fact) so the two
    paths can never drift into accepting different spellings — a per-path split in
    jobs.channel is exactly the funnel-count corruption the closed vocabulary prevents.
    Case-insensitive ("Direct" → "direct"): the case rule lives HERE, not in a front-end,
    so any client's raw input behaves identically through every surface. Returns
    (normalized_value_or_None, error_or_None); empty/whitespace reads as not-given."""
    value = (value or "").strip().lower() or None
    if value is not None and value not in ALL_CHANNELS:
        return None, f"channel must be one of {list(ALL_CHANNELS)}"
    return value, None


def _set_applied_field(conn, row, col, label, value):
    """The one implementation of "edit an applied-only field after the fact": applied-chain
    guard, strip-to-None (empty clears), chain-wide single-column write, commit, and the
    set/cleared message — shared by set_resume and set_channel so their docstrings' "same
    contract" promise is enforced by construction, not by keeping two copies in sync.
    Validation (where a field has a closed vocabulary) happens in the wrappers, before this.
    Returns (ok, message, affected, exempt)."""
    dec = effective_decision(conn, row)
    if dec["app_status"] != "applied":
        return False, f"{label} is recorded on applied roles — mark it applied first", [], []
    value = (value or "").strip() or None
    targets = _chain_targets(conn, row)
    qs = ",".join("?" * len(targets))
    conn.execute(
        f"UPDATE jobs SET {col}=? WHERE job_url IN ({qs})",
        (value, *sorted(targets)),
    )
    conn.commit()
    msg = (f"{label} {'set to ' + repr(value) if value else 'cleared'}: "
           f"{row['title']} — {row['company']}")
    return True, msg, sorted(targets), [row["job_url"]]


def set_resume(conn, row, text):
    """Set (or with empty text clear) the resume_variant on an already-applied chain — the
    edit-after-the-fact path; at apply time pass it to mark_posting instead. Free text;
    propagated chain-wide like the other decision fields (see _set_applied_field).
    Returns (ok, message, affected, exempt)."""
    return _set_applied_field(conn, row, "resume_variant", "resume variant", text)


def set_channel(conn, row, value):
    """Set (or with empty value clear) the application channel on an already-applied chain —
    set_resume's sibling (same core: _set_applied_field); at apply time pass it to
    mark_posting instead. Unlike the resume's free text, the value is validated against
    states.ALL_CHANNELS (_norm_channel — a closed vocabulary; per-user spellings would split
    the funnel counts). Returns (ok, message, affected, exempt)."""
    value, err = _norm_channel(value)
    if err:
        return False, err, [], []
    return _set_applied_field(conn, row, "channel", "channel", value)


# The fixed note text mark_expired writes and its undo verifies. Deliberately a plain
# 'note' payload, not a new ALL_EVENTS type: it asserts nothing about an application
# (no outcome cache), and the closed event vocabulary stays outcome-only.
EXPIRED_NOTE = "posting expired"


def mark_expired(conn, row, undo=False):
    """Mark `row`'s chain as a dead/expired posting (or with undo=True reverse it): a fixed
    'note' event (EXPIRED_NOTE) records WHY the chain left the queue, plus a chain-wide
    'passed' mark so relistings auto-skip (skip_decided_reposts keys on app_status). One
    core for the CLI (cmd_expired) and the web UI (api_decision). Composed from the
    propagation primitives rather than record_event+mark_posting so the note and the mark
    land in ONE commit (each service commits internally). Refused in BOTH directions on an
    applied chain: a dead posting you applied to is an outcome (rejected_by_employer /
    ghosted), not a triage disposition — and after expired→applied the marker is still the
    latest event, so an unguarded undo would clear the applied mark. Accepted loss, same
    class as re-asserting 'passed': marking an already-passed chain overwrites status_date,
    and undo returns the chain to undecided, not to the prior passed (app_status has no
    history). Returns (ok, message, affected_urls, exempt_urls)."""
    targets = _chain_targets(conn, row)
    canonical = row["repost_of"] or row["job_url"]
    if effective_decision(conn, row)["app_status"] == "applied":
        return False, (f"can't {'un' if undo else ''}mark expired on an applied chain — "
                       f"record what happened instead "
                       f"(event --type rejected_by_employer|ghosted|withdrew)"), [], []
    if undo:
        urls = sorted(targets)
        qs = ",".join("?" * len(urls))
        # By id, not undo_event: that deletes the last event UNCONDITIONALLY — this undo
        # must verify it is unwinding its own marker, not some later note.
        last = conn.execute(
            f"SELECT id, event_type, note FROM app_events WHERE job_url IN ({qs}) "
            f"ORDER BY id DESC LIMIT 1", tuple(urls),
        ).fetchone()
        if last is None or last["event_type"] != EVENT_NOTE or last["note"] != EXPIRED_NOTE:
            return False, (f"the chain's last event isn't the expired marker — remove later "
                           f"events first (event --undo), or just clear the mark "
                           f"(passed --undo): {row['title']} — {row['company']}"), [], []
        conn.execute("DELETE FROM app_events WHERE id=?", (last["id"],))
        propagate_app_status(conn, targets, None, None)
        _recompute_outcome(conn, canonical, targets)
        conn.commit()
        _reconcile_chain_skips(conn, canonical)
        exempt = _undo_exempt(conn, row, targets)
        return (True, f"unmarked expired: {row['title']} — {row['company']}",
                sorted(targets), exempt)
    exempt = _forward_exempt(conn, row, targets)  # pre-state read
    today = date.today().isoformat()
    conn.execute(
        "INSERT INTO app_events (job_url, event_type, event_date, note, created_at) "
        "VALUES (?,?,?,?,?)",
        (canonical, EVENT_NOTE, today, EXPIRED_NOTE,
         datetime.now().isoformat(timespec="seconds")),
    )
    propagate_app_status(conn, targets, "passed", today)
    _recompute_outcome(conn, canonical, targets)
    conn.commit()
    _reconcile_chain_skips(conn, canonical)
    return (True, f"marked expired (passed + note): {row['title']} — {row['company']} ({today})",
            sorted(targets), exempt)


# ------------------------------------------------------- manual repost linking
#
# `_find_repost` only links reposts at fetch time, and only when normalized
# company+location AND exact title match — deliberately conservative, so it
# misses a relisting whose title/location drifted and (in practice) the same role
# cross-posted to Adzuna vs LinkedIn. `dupe` is the manual escape hatch: link two
# postings already in the DB as the same role, reusing the existing chain machinery
# (repost_of + _chain_targets + skip_decided_reposts). It adds NO fuzzy matching —
# the user asserts the duplicate; the code just records and propagates it safely.

def dupe_resolve(conn, url, of_url):
    """Validate a link request and build the merge plan WITHOUT mutating. Returns `(plan, error)`
    with exactly one non-None: `plan` is a dict (winner, loser, *_members, dec) ready for
    `dupe_commit`; `error` is a user-facing string explaining a guard failure. Shared by the CLI
    (`cmd_dupe`) and the web UI (`app.api_dupe`) so the guard logic lives in one place."""
    a, err = resolve_posting(conn, url)
    if err:
        return None, err
    assert a is not None  # resolve_posting returns row when err is None
    if not of_url:
        return None, "provide the other posting (--of <id or unique substring of its job_url>)"
    b, err = resolve_posting(conn, of_url)
    if err:
        return None, err
    assert b is not None

    # Resolve each side to its chain's canonical, so we link canonical-to-canonical (never build
    # a 2-level chain the flat _chain_targets can't traverse).
    a_canon_url = a["repost_of"] or a["job_url"]
    b_canon_url = b["repost_of"] or b["job_url"]
    if a_canon_url == b_canon_url:
        return None, "already the same role — nothing to link"
    a_canon = conn.execute("SELECT * FROM jobs WHERE job_url=?", (a_canon_url,)).fetchone()
    b_canon = conn.execute("SELECT * FROM jobs WHERE job_url=?", (b_canon_url,)).fetchone()

    # Earliest first_seen wins; tie-break on job_url so the choice is deterministic.
    if (a_canon["first_seen"] or "", a_canon["job_url"]) <= (b_canon["first_seen"] or "", b_canon["job_url"]):
        winner, loser = a_canon, b_canon
    else:
        winner, loser = b_canon, a_canon
    winner_members = _chain_members(conn, winner["job_url"])
    loser_members = _chain_members(conn, loser["job_url"])

    # Nested-merge guard: the `manual:<prev>` encoding is single-level, so re-merging a chain that
    # already contains a manual link would relabel it and strand the inner link (un-undoable). The
    # merged-in side is the one whose members get repointed, so block when IT holds a manual link
    # (a canonical always has repost_source=NULL; only manually-linked members are non-NULL).
    qs_l = ",".join("?" * len(loser_members))
    nested = conn.execute(
        f"SELECT title, company, job_url FROM jobs WHERE job_url IN ({qs_l}) AND repost_source IS NOT NULL",
        tuple(loser_members),
    ).fetchall()
    if nested:
        names = "; ".join(f"{n['title']} — {n['company']} [{n['job_url']}]" for n in nested)
        return None, f"the merged-in role still contains manual link(s) — undo those first: {names}"

    # Conflict guard: never overwrite one side's decision with a different one — abort instead.
    w_dec = _chain_decision(conn, winner_members)
    l_dec = _chain_decision(conn, loser_members)
    if w_dec and l_dec and _decision_sig(w_dec) != _decision_sig(l_dec):
        return None, (f"both roles already decided differently — keep [{_fmt_decision(w_dec)}] "
                      f"vs merge [{_fmt_decision(l_dec)}]; resolve one first")
    plan = {
        "winner": winner, "loser": loser,
        "winner_members": winner_members, "loser_members": loser_members,
        "dec": w_dec or l_dec,  # the surviving decision (only one set, or both equal)
        # Per-side decisions, kept so dupe_commit can tell WHICH side the merge flips: the
        # side with no decision of its own inherits the other's (its members' displayed state
        # changes); the decided side's members already displayed it — stamped or not.
        "w_dec": w_dec, "l_dec": l_dec,
        # The two rows the user actually named — the UI's interaction handles, which its
        # hide-decided filter must keep visible whatever the merge stamps on them.
        "named": sorted({a["job_url"], b["job_url"]}),
    }
    return plan, None


def dupe_commit(conn, plan):
    """Apply a merge plan from `dupe_resolve`: repoint the loser chain under the winner canonical,
    propagate the surviving decision (preserving original dates), eval-skip still-`new` members.
    Returns (affected_urls, exempt_urls) — exempt is the named pair plus every member whose
    displayed state the merge flips undecided→decided: the members of the side that had no
    decision of its own. Judged PER SIDE, not per row — displayed state is chain-level, so a
    decided side's unstamped members (a relisting fetched after the decision is deliberately
    never stamped) already displayed the decision and stay unexempted; the user may have
    deliberately hidden them. Caller is responsible for any preview/confirmation."""
    winner, loser = plan["winner"], plan["loser"]
    winner_members, loser_members, dec = plan["winner_members"], plan["loser_members"], plan["dec"]
    exempt = set(plan["named"])
    if dec:
        if not plan["w_dec"]:
            exempt |= winner_members
        if not plan["l_dec"]:
            exempt |= loser_members

    # Repoint the loser canonical AND every relisting it owned onto the winner canonical (the flat
    # model breaks if a child is left pointing at the now-demoted loser). Encode each row's prior
    # parent in repost_source so --undo can reconstruct the original two chains.
    for c in sorted(loser_members):
        prev = loser["job_url"] if c != loser["job_url"] else None
        src = "manual" if prev is None else f"manual:{prev}"
        conn.execute(
            "UPDATE jobs SET repost_of=?, repost_source=? WHERE job_url=?",
            (winner["job_url"], src, c),
        )

    if dec:
        all_members = winner_members | loser_members
        if dec["app_status"]:
            # Coalesce the resume variant and channel across BOTH sides (winner's preferred):
            # the chain model holds one of each, and _decision_sig deliberately ignores them
            # (outcome metadata must not block a merge), so without the coalesce a NULL
            # surviving side would either blank the other side's recorded value or leave a
            # mixed chain whose _decide read is SQL-row-order-dependent. When both sides
            # recorded DIFFERENT values the winner's wins — an accepted loss, same-role-twice.
            resume = ((plan["w_dec"] or {}).get("resume_variant")
                      or (plan["l_dec"] or {}).get("resume_variant"))
            channel = ((plan["w_dec"] or {}).get("channel")
                       or (plan["l_dec"] or {}).get("channel"))
            propagate_app_status(conn, all_members, dec["app_status"], dec["status_date"],
                                 resume, channel)
        if dec["reject"]:
            # Merge: fill in only members with no attribution yet (overwrite_manual=False) — leave
            # any existing manual/rule attribution intact.
            propagate_reject(conn, all_members, dec["filter_gate"], dec["filter_date"],
                             overwrite_manual=False)
    # Union of the two sides' outcome histories: events stay keyed to whichever canonical they
    # were written under (both urls are members of the merged chain now, so chain-wide reads
    # find them all); the cache just needs one recompute over the unified chain. Outcome
    # differences never block a merge (_decision_sig ignores them) — latest event wins here.
    _recompute_outcome(conn, winner["job_url"], winner_members | loser_members)
    conn.commit()
    # Chain-scoped reconcile of the merged chain: a decided merge skips still-'new' members
    # now; an undecided-but-evaluated merge deliberately leaves 'new' members for the next
    # run's post-filter forward pass (they must re-face the current rules before any label
    # spares them the eval — see _reconcile_chain_skips).
    _reconcile_chain_skips(conn, winner["job_url"])
    return sorted(winner_members | loser_members), sorted(exempt)


def dupe_unlink(conn, a):
    """Core of `dupe --undo`: detach the manually-linked relisting `a` (and the sub-chain it
    originally headed) from its canonical, restoring the two independent chains. Structure only — a
    decision that propagated across the merge is left as-is, and so are app_events rows (an event
    recorded while merged stays keyed where it was written); only the outcome CACHE is recomputed
    per resulting chain, since it must stay a pure function of each chain's own events. Returns
    `(ok, message, affected, exempt)`: exempt is just the interaction handles — the clicked
    row, the restored loser canonical (the detached chain's head, where a merge-propagated
    decision naturally gets undone), and the old winner canonical (which kept the decision on
    its side of the split) — since the split changes no member's displayed decision, the rest
    of the detached sub-chain stays deliberately unexempted. Shared by the CLI and the web UI."""
    src = a["repost_source"]
    # Identify the loser canonical L: the original head of the merged-in sub-chain. `a` may BE it
    # ('manual') or be one of its relistings ('manual:<L>').
    if src == "manual":
        loser_canon_url = a["job_url"]
    elif src and src.startswith("manual:"):
        loser_canon_url = src.split(":", 1)[1]
    else:
        return False, f"'{a['title']} — {a['company']}' is not a manually-linked relisting", [], []

    # Resolve the canonical row up front and bail BEFORE mutating if it's gone — else the detach
    # loop would repoint children at a non-existent canonical (orphan) and commit before the final
    # dereference. Nothing in the pipeline deletes rows, so this only guards manual DB edits.
    loser_canon = conn.execute(
        "SELECT title, company FROM jobs WHERE job_url=?", (loser_canon_url,)
    ).fetchone()
    if loser_canon is None:
        return False, f"encoded original {loser_canon_url!r} no longer exists; cannot undo", [], []

    # The sub-chain to detach: the loser canonical plus every row encoded as its former child.
    rows = conn.execute(
        "SELECT job_url, repost_source FROM jobs WHERE job_url=? OR repost_source=?",
        (loser_canon_url, f"manual:{loser_canon_url}"),
    ).fetchall()
    for r in rows:
        restored_parent = None if r["job_url"] == loser_canon_url else loser_canon_url
        conn.execute(
            "UPDATE jobs SET repost_of=?, repost_source=NULL WHERE job_url=?",
            (restored_parent, r["job_url"]),
        )
    # Event ROWS stay keyed where they were written (no data migration — the merge's
    # chain-wide reads simply stop spanning them), but the outcome CACHE is recomputed for
    # both resulting chains: it must always be a pure function of each chain's own events,
    # and leaving the merged value would show one side the other side's outcome.
    _recompute_outcome(conn, loser_canon_url, {r["job_url"] for r in rows})
    if a["repost_of"]:
        _recompute_outcome(conn, a["repost_of"])  # remaining winner chain — fresh membership
    conn.commit()
    # Reconcile BOTH resulting chains: the detached loser chain (skipped members whose new
    # chain lacks a decision/verdict are released) and the remaining winner chain (`a` still
    # holds its pre-detach repost_of — the winner canonical).
    _reconcile_chain_skips(conn, loser_canon_url)
    if a["repost_of"]:
        _reconcile_chain_skips(conn, a["repost_of"])
    msg = (f"unlinked: {loser_canon['title']} — {loser_canon['company']} "
           f"({len(rows)} row(s) restored to their own chain); any decision propagated by the merge "
           f"was left as-is — undo it separately (passed/applied/reject) if it shouldn't carry over")
    if a["repost_of"] and _count_events(conn, _chain_members(conn, a["repost_of"])):
        # Events recorded while merged are keyed to their then-canonical, which after an
        # earlier merge may be a mere MEMBER of the kept chain — so count over the kept
        # chain's full post-detach membership, not just its canonical url, or the warning
        # silently skips exactly the case it exists for. The detached chain's outcome cache
        # was just recomputed WITHOUT those events: an offer recorded from the detached
        # side's card reads "no response" now, and this message is the only notice.
        msg += ("; outcome events recorded while linked stay with the kept role — this "
                "chain's outcome was recomputed from its own events only")
    exempt = sorted({a["job_url"], loser_canon_url}
                    | ({a["repost_of"]} if a["repost_of"] else set()))
    return True, msg, [r["job_url"] for r in rows], exempt
