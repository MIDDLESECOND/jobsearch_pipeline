#!/usr/bin/env python3
"""Repost / content-dedup and decision-chain core.

This module owns everything about treating multiple postings as one role:

  * normalization + content fingerprint (the blocking key),
  * fetch-time repost linking (_find_repost),
  * the repost *chain* abstraction — members, the user's effective decision across a
    chain, and propagation/reconcile (skip_decided_reposts),
  * the manual dupe-link cores (_dupe_resolve / _dupe_commit / _dupe_unlink).

It was extracted from pipeline.py so the "what is this chain's decision?" question has
ONE implementation. The report, the web UI, and the dupe conflict-guard all call
`effective_decision` / `_chain_decision` here instead of each re-deriving it.

No imports from pipeline (keeps the dependency one-way); pipeline re-imports these names
so existing call sites and `pipeline.X` references keep working.
"""

import re
import sys


# ------------------------------------------------------- normalization / fingerprint
#
# LinkedIn mints a fresh job_url every time a role is reposted, so URL-level dedup (the
# INSERT OR IGNORE on the PRIMARY KEY) misses relistings. The content fingerprint adds a
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
# fingerprints (see pipeline._recompute_fingerprints) so existing rows and new inserts share a
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

def skip_decided_reposts(conn):
    """Skip the paid eval for a relisting whose role the user has already decided. A repost links
    to its canonical original via `repost_of`, and every applied/passed/reject decision propagates
    to that canonical (see _chain_targets), so the canonical's decision state is authoritative for
    the whole chain. Matched rows get status='repost_decided' (skipped by evaluate_new_jobs).
    Mirrors apply_salary_filter / apply_hard_filters — a deterministic pre-eval pass."""
    # Reconciles in BOTH directions from current decision state, so it self-corrects: a 'new'
    # relisting of a decided chain is skipped, and a previously-skipped relisting whose chain
    # decision was since undone returns to 'new' to be (re-)evaluated. Without the reverse pass
    # an undo would strand the sibling at 'repost_decided' forever (never re-evaluated).
    # Keyed off the canonical (repost_of) only, which is sound for the decisions this is meant to
    # propagate: applied/passed/manual-reject all write chain-wide (incl. the canonical) via the
    # propagate_* helpers, so the canonical is authoritative for them. NOT chain-wide for the
    # deterministic rule filters — apply_hard_filters stamps filter_source on the single matched
    # row only — so a rule-rejected NON-canonical relisting can leave its canonical "undecided"
    # here while effective_decision (chain-wide) reports the role rejected. Accepted: the only cost
    # is one extra eval on a later relisting whose own text didn't re-trip the rule; no wrong verdict.
    decided = ("(SELECT job_url FROM jobs WHERE app_status IS NOT NULL "
               "OR filter_source IS NOT NULL)")
    cur = conn.execute(
        f"UPDATE jobs SET status='repost_decided' WHERE status='new' AND repost_of IN {decided}"
    )
    # repost_of / job_url are never NULL here, so NOT IN is safe (no NULL-row short-circuit).
    rev = conn.execute(
        f"UPDATE jobs SET status='new' WHERE status='repost_decided' AND repost_of NOT IN {decided}"
    )
    conn.commit()
    if cur.rowcount:
        print(f"[repost-skip] {cur.rowcount} relistings of already-decided roles (eval skipped, cost saved)")
    if rev.rowcount:
        print(f"[repost-skip] {rev.rowcount} relistings restored to 'new' (chain decision undone)")


# ------------------------------------------------------ url resolution / chain reads

def _resolve_posting(conn, url, label):
    """Resolve a --url (full or unique substring) to a single jobs row, or None. Prints a
    helpful message on no-match / ambiguity. Shared by the `applied`/`passed`/`reject`
    commands so they behave identically."""
    if not url:
        print(f"[{label}] provide --url (full or unique substring of the job_url)", file=sys.stderr)
        return None
    # Escape LIKE metacharacters so a substring containing % or _ matches literally
    # (the resolved row drives a destructive UPDATE, so a mis-match must not happen).
    safe = url.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    matches = conn.execute(
        "SELECT * FROM jobs WHERE job_url LIKE ? ESCAPE '\\'", (f"%{safe}%",)
    ).fetchall()
    if not matches:
        print(f"[{label}] no posting matches '{url}'", file=sys.stderr)
        return None
    if len(matches) > 1:
        # A full job_url is a substring of any longer one (LinkedIn ids nest: .../view/123 is a
        # substring of .../view/1234), so an exact url would otherwise read as "ambiguous". When the
        # input exactly equals one row's job_url, take it — that's the caller naming a specific row
        # (always true for the web UI, which passes full urls), not a fuzzy substring.
        exact = [m for m in matches if m["job_url"] == url]
        if len(exact) == 1:
            return exact[0]
        print(f"[{label}] '{url}' is ambiguous ({len(matches)} matches):", file=sys.stderr)
        for m in matches:
            print(f"    {m['title']} — {m['company']}  {m['job_url']}", file=sys.stderr)
        return None
    return matches[0]


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
        f"SELECT app_status, status_date, filter_source, filter_gate, filter_date "
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


def effective_decision(conn, row):
    """The single source of truth for "what has the user decided about this row's role?",
    spanning the whole repost chain. Used by the report (_repost_info), the web UI
    (row_to_dict), and the dupe conflict guard — so the three can never drift.

    Returns a dict (never None):
      app_status            'applied' | 'passed' | None  (applied outranks, chain-wide)
      status_date           date the surviving app_status was set, or None
      reject                bool — any chain member is a hard-fail override
      filter_gate, filter_date  the surviving reject's attribution, or None
      is_repost             True if `row` itself is a relisting (repost_of set)
      original_first_seen   the canonical original's first_seen (for the repost note), or None
      original_verdict      the canonical original's model verdict, or None
    """
    canonical_url = row["repost_of"] or row["job_url"]
    # One query for the whole chain: fetch every member row with the columns _decide needs PLUS the
    # canonical's first_seen/verdict for the repost note (the canonical is itself a member). Replaces
    # the old three-query path (_chain_members + _chain_decision + a separate canonical SELECT).
    rows = conn.execute(
        "SELECT job_url, first_seen, verdict, app_status, status_date, "
        "filter_source, filter_gate, filter_date FROM jobs WHERE job_url=? OR repost_of=?",
        (canonical_url, canonical_url),
    ).fetchall()
    dec = _decide(rows) or {}
    # The canonical's own row (None only if repost_of points at a row that doesn't exist — an
    # orphaned manual edit; original_* then stay None, as before).
    canon = next((r for r in rows if r["job_url"] == canonical_url), None)
    return {
        "app_status": dec.get("app_status"),
        "status_date": dec.get("status_date"),
        "reject": dec.get("reject", False),
        "filter_gate": dec.get("filter_gate"),
        "filter_date": dec.get("filter_date"),
        "is_repost": bool(row["repost_of"]),
        "original_first_seen": canon["first_seen"] if canon else None,
        "original_verdict": canon["verdict"] if canon else None,
    }


# ----------------------------------------------- chain writes (decision propagation)
#
# A per-posting decision applies to the whole repost chain, not just the named row. These three
# functions own those writes so the SET clauses — and the load-bearing status='new' -> 'rule_filtered'
# lift on a reject — live in ONE place. cmd_mark, cmd_reject, and _dupe_commit all route through
# them, so the write paths can't drift the way they did when each had its own inline UPDATE.
# (The read counterpart is effective_decision; callers commit.)

# SET clause for a manual hard-fail override: stamp 'manual' + attribution, and lift a still-'new'
# row to 'rule_filtered' so the next run's paid eval skips it (an already-evaluated row keeps its
# status — the report groups by filter_source either way). Two placeholders: (gate, date).
_REJECT_SET = ("filter_source='manual', filter_gate=?, filter_date=?, "
               "status=CASE WHEN status='new' THEN 'rule_filtered' ELSE status END")


def propagate_app_status(conn, member_urls, status, status_date):
    """Set the user's applied/passed decision (or clear it, status=None) across every member of a
    repost chain, so the decision follows the role across all relistings. Shared by cmd_mark and
    the dupe merge."""
    members = set(member_urls)
    if not members:
        return
    qs = ",".join("?" * len(members))
    conn.execute(
        f"UPDATE jobs SET app_status=?, status_date=? WHERE job_url IN ({qs})",
        (status, status_date, *members),
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
        "status=CASE WHEN status='rule_filtered' AND verdict IS NULL THEN 'new' ELSE status END "
        f"WHERE job_url IN ({qs}) AND filter_source='manual'",
        tuple(members),
    )


# ------------------------------------------------------- manual repost linking
#
# `_find_repost` only links reposts at fetch time, and only when normalized
# company+location AND exact title match — deliberately conservative, so it
# misses a relisting whose title/location drifted and (in practice) the same role
# cross-posted to Adzuna vs LinkedIn. `dupe` is the manual escape hatch: link two
# postings already in the DB as the same role, reusing the existing chain machinery
# (repost_of + _chain_targets + skip_decided_reposts). It adds NO fuzzy matching —
# the user asserts the duplicate; the code just records and propagates it safely.

def _dupe_resolve(conn, url, of_url):
    """Validate a link request and build the merge plan WITHOUT mutating. Returns `(plan, error)`
    with exactly one non-None: `plan` is a dict (winner, loser, *_members, dec) ready for
    `_dupe_commit`; `error` is a user-facing string explaining a guard failure. Shared by the CLI
    (`cmd_dupe`) and the web UI (`app.api_dupe`) so the guard logic lives in one place."""
    a = _resolve_posting(conn, url, "dupe")
    if a is None:
        return None, "no posting matches that URL"
    if not of_url:
        return None, "provide the other posting (--of <id or unique substring of its job_url>)"
    b = _resolve_posting(conn, of_url, "dupe")
    if b is None:
        return None, "no posting matches the other URL"

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
    }
    return plan, None


def _dupe_commit(conn, plan):
    """Apply a merge plan from `_dupe_resolve`: repoint the loser chain under the winner canonical,
    propagate the surviving decision (preserving original dates), eval-skip still-`new` members.
    Returns the affected job_url list. Caller is responsible for any preview/confirmation."""
    winner, loser = plan["winner"], plan["loser"]
    winner_members, loser_members, dec = plan["winner_members"], plan["loser_members"], plan["dec"]

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
            propagate_app_status(conn, all_members, dec["app_status"], dec["status_date"])
        if dec["reject"]:
            # Merge: fill in only members with no attribution yet (overwrite_manual=False) — leave
            # any existing manual/rule attribution intact.
            propagate_reject(conn, all_members, dec["filter_gate"], dec["filter_date"],
                             overwrite_manual=False)
    conn.commit()
    skip_decided_reposts(conn)  # eval-skip any still-'new' member now under a decided canonical
    return sorted(winner_members | loser_members)


def _dupe_unlink(conn, a):
    """Core of `dupe --undo`: detach the manually-linked relisting `a` (and the sub-chain it
    originally headed) from its canonical, restoring the two independent chains. Structure only — a
    decision that propagated across the merge is left as-is. Returns `(ok, message, affected)`.
    Shared by the CLI and the web UI."""
    src = a["repost_source"]
    # Identify the loser canonical L: the original head of the merged-in sub-chain. `a` may BE it
    # ('manual') or be one of its relistings ('manual:<L>').
    if src == "manual":
        loser_canon_url = a["job_url"]
    elif src and src.startswith("manual:"):
        loser_canon_url = src.split(":", 1)[1]
    else:
        return False, f"'{a['title']} — {a['company']}' is not a manually-linked relisting", []

    # Resolve the canonical row up front and bail BEFORE mutating if it's gone — else the detach
    # loop would repoint children at a non-existent canonical (orphan) and commit before the final
    # dereference. Nothing in the pipeline deletes rows, so this only guards manual DB edits.
    loser_canon = conn.execute(
        "SELECT title, company FROM jobs WHERE job_url=?", (loser_canon_url,)
    ).fetchone()
    if loser_canon is None:
        return False, f"encoded original {loser_canon_url!r} no longer exists; cannot undo", []

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
    conn.commit()
    skip_decided_reposts(conn)  # reverse pass restores any 'repost_decided' member to 'new'
    msg = (f"unlinked: {loser_canon['title']} — {loser_canon['company']} "
           f"({len(rows)} row(s) restored to their own chain); any decision propagated by the merge "
           f"was left as-is — undo it separately (passed/applied/reject) if it shouldn't carry over")
    return True, msg, [r["job_url"] for r in rows]
