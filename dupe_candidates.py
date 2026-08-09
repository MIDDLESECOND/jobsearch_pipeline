#!/usr/bin/env python3
"""Conservative, review-only suggestions for likely cross-source duplicates.

The automatic repost fingerprint stays deliberately strict.  This module only surfaces
recent postings whose stored normalized company and exact normalized title agree across
different sources.  It never links chains: confirmation remains an explicit call through
``chain.dupe_resolve`` / ``chain.dupe_commit``.
"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import combinations

from chain import dupe_commit, dupe_resolve, resolve_posting


LOOKBACK_DAYS = 120
MAX_PAIR_GAP_DAYS = 45
MAX_PAGE_SIZE = 200
# The blocking key is company+title WITHOUT location, on purpose: cross-source location
# strings rarely agree ("Grand Central, Manhattan" vs "New York, NY"), so requiring them to
# match would defeat the whole point.  The cost is that one employer posting one requisition
# across many cities produces a full LinkedIn x Adzuna cross product under a single key.
# Observed: one "deloitte | microsoft dynamics senior consultant..." key alone yielded 792
# pairs, and the 10 largest keys were 26% of the entire queue.  Past this many pairs a key is
# evidence of mass-posting rather than of duplication, so the whole key is dropped.
MAX_BUCKET_PAIRS = 3


def _seen_date(row):
    try:
        return date.fromisoformat(str(row["first_seen"])[:10])
    except (TypeError, ValueError):
        return None


def _root(row):
    return row["repost_of"] or row["job_url"]


def _expected_root_pair(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(not isinstance(root, str) or not root for root in value)):
        raise ValueError("expected_roots must contain two posting roots")
    return sorted(value)


def _rank_cross_pairs(bucket, group_a, group_b):
    """Rank every eligible pair between two same-key source groups into ``bucket``.

    Entries are ``(row, seen, root)`` triples; ``bucket`` keys are sorted root pairs and
    values are ``(rank, pair_dict)`` with the smallest rank kept.
    """
    for a, a_seen, a_root in group_a:
        for b, b_seen, b_root in group_b:
            if a_root == b_root:
                continue
            gap = abs((a_seen - b_seen).days)
            if gap > MAX_PAIR_GAP_DAYS:
                continue
            roots = tuple(sorted((a_root, b_root)))
            if a_root == roots[0]:
                left, right = a, b
            else:
                left, right = b, a
            same_location = bool(
                left["fingerprint"] and left["fingerprint"] == right["fingerprint"]
            )
            newest = max(a_seen, b_seen)
            # One current-chain pair can have several physical relistings.  Prefer the pair
            # closest in time, then equal normalized location, then the newest evidence.
            rank = (gap, 0 if same_location else 1, -newest.toordinal(),
                    left["job_url"], right["job_url"])
            if roots not in bucket or rank < bucket[roots][0]:
                bucket[roots] = (rank, {
                    "left_root": roots[0],
                    "right_root": roots[1],
                    "left": left,
                    "right": right,
                    "same_location": same_location,
                    "first_seen_gap_days": gap,
                    "newest_seen": newest.isoformat(),
                    "dismissed_at": None,
                    "review_version": 0,
                })


def _candidate_map(conn, today):
    """Return the eligible pairs plus the counts suppressed as mass-posting keys.

    Returns ``(pairs_by_roots, suppression)``; ``suppression`` carries the dropped key and
    pair counts so no caller can present a trimmed queue as a complete one.
    """
    cutoff = (today - timedelta(days=LOOKBACK_DAYS - 1)).isoformat()
    # Served index-only by idx_first_seen_day (core.get_db): the window covers the whole
    # table while the history is younger than LOOKBACK_DAYS (76k rows as of 2026-08), so a
    # column added to this SELECT must be added to that index too, or the scan walks every
    # row's TEXT overflow chain again (~700ms per Action Center load).  The eligible CTE
    # keeps only keys observed under more than one source inside the window: a single-source
    # key cannot produce a cross-source pair, and materializing its rows just to bucket and
    # drop them was most of this function's cost (76k rows fetched to yield ~2.5k pairs).
    # It is a pure NECESSARY-condition pushdown — the multi-source test runs before the
    # per-row seen-date filter below, so it admits a superset of every key that filter could
    # still pair, and the Python walk is unchanged.
    rows = conn.execute(
        "WITH eligible(norm_company,norm_title) AS ("
        "SELECT norm_company,norm_title FROM jobs "
        "WHERE norm_company IS NOT NULL AND norm_company<>'' "
        "AND norm_title IS NOT NULL AND norm_title<>'' "
        "AND source IS NOT NULL AND source<>'' AND substr(first_seen,1,10)>=? "
        "GROUP BY norm_company,norm_title HAVING COUNT(DISTINCT source)>1) "
        "SELECT j.job_url,j.repost_of,j.source,j.norm_company,j.norm_title,"
        "j.fingerprint,j.first_seen FROM jobs j "
        "JOIN eligible e ON e.norm_company=j.norm_company AND e.norm_title=j.norm_title "
        "WHERE j.norm_company IS NOT NULL AND j.norm_company<>'' "
        "AND j.norm_title IS NOT NULL AND j.norm_title<>'' "
        "AND j.source IS NOT NULL AND j.source<>'' AND substr(j.first_seen,1,10)>=?",
        (cutoff, cutoff),
    ).fetchall()
    # Group each key's entries by source up front: only cross-source pairs are candidates,
    # and a single-source mass-posting (one requisition posted across many cities) would
    # otherwise cost its full O(n^2) combinations just to skip every one.  Pure iteration
    # pruning — the pairs considered, the winner per root pair (ranks embed both job_urls,
    # so no two physical pairs tie), and the suppression counts are identical to the flat
    # combinations() walk this replaces.
    buckets = defaultdict(lambda: defaultdict(list))
    for row in rows:
        seen = _seen_date(row)
        if seen is not None and seen <= today:
            buckets[(row["norm_company"], row["norm_title"])][row["source"]].append(
                (row, seen, _root(row)))

    chosen = {}
    suppressed_keys = 0
    suppressed_pairs = 0
    for by_source in buckets.values():
        bucket = {}
        groups = list(by_source.values())
        for group_a, group_b in combinations(groups, 2):
            _rank_cross_pairs(bucket, group_a, group_b)
        # Suppressed pairs deliberately stop being confirmable through this queue too: the
        # eligibility check below shares this map.  The manual assertion path is unaffected
        # (CLI ``dupe``, the UI's "duplicate" controls), so nothing becomes unlinkable.
        if len(bucket) > MAX_BUCKET_PAIRS:
            suppressed_keys += 1
            suppressed_pairs += len(bucket)
            continue
        # One chain pair can sit under two keys when a member's normalized title drifted, so
        # keep merging across keys by the same rank rather than overwriting blindly.
        for roots, value in bucket.items():
            if roots not in chosen or value[0] < chosen[roots][0]:
                chosen[roots] = value
    return ({roots: value[1] for roots, value in chosen.items()},
            {"keys": suppressed_keys, "pairs": suppressed_pairs})


def _hydrate_pairs(conn, pairs):
    """Hydrate only the bounded page, not every recent posting scanned for blocking."""
    urls = {pair[side]["job_url"] for pair in pairs for side in ("left", "right")}
    by_url = {}
    url_list = sorted(urls)
    for start in range(0, len(url_list), 800):
        chunk = url_list[start:start + 800]
        qs = ",".join("?" * len(chunk))
        for row in conn.execute(
            "SELECT job_url,title,company,location,source,first_seen,"
            f"date_posted,description FROM jobs WHERE job_url IN ({qs})",
            tuple(chunk),
        ).fetchall():
            by_url[row["job_url"]] = row
    for pair in pairs:
        pair["left"] = by_url[pair["left"]["job_url"]]
        pair["right"] = by_url[pair["right"]["job_url"]]
    return pairs


def _all_candidates(conn, today):
    candidates, suppression = _candidate_map(conn, today)
    reviews = {
        (row["left_root"], row["right_root"]): row
        for row in conn.execute(
            "SELECT left_root,right_root,dismissed_at,dismissed,version "
            "FROM dupe_candidate_dismissals"
        )
    }
    for roots, pair in candidates.items():
        review = reviews.get(roots)
        if review is not None:
            pair["review_version"] = review["version"]
            pair["dismissed_at"] = (
                review["dismissed_at"] if review["dismissed"] else None
            )
    return list(candidates.values()), suppression


def query_candidate_page(conn, *, page=1, page_size=50, today=None, dismissed=False):
    """Return one bounded active or ignored candidate page.

    Suggestions are current-state derivations.  A dismissal only hides the exact pair of
    roots reviewed at that time; if a later merge changes either root, the changed evidence
    may surface again instead of silently inheriting an obsolete judgment.
    """
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if (isinstance(page_size, bool) or not isinstance(page_size, int)
            or not 1 <= page_size <= MAX_PAGE_SIZE):
        raise ValueError(f"page_size must be an integer from 1 to {MAX_PAGE_SIZE}")
    if not isinstance(dismissed, bool):
        raise ValueError("dismissed must be a boolean")
    today = today or date.today()
    pairs, suppression = _all_candidates(conn, today)
    dismissed_total = sum(pair["dismissed_at"] is not None for pair in pairs)
    pairs = [pair for pair in pairs
             if (pair["dismissed_at"] is not None) == dismissed]
    pairs.sort(key=lambda pair: (
        -date.fromisoformat(pair["newest_seen"]).toordinal(),
        pair["first_seen_gap_days"],
        0 if pair["same_location"] else 1,
        pair["left_root"], pair["right_root"],
    ))
    total = len(pairs)
    offset = (page - 1) * page_size
    page_pairs = _hydrate_pairs(conn, pairs[offset:offset + page_size])
    return {
        "pairs": page_pairs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
        "dismissed_total": dismissed_total,
        "suppressed_keys": suppression["keys"],
        "suppressed_pairs": suppression["pairs"],
    }


def set_candidate_dismissed(conn, left_url, right_url, dismissed, *,
                            expected_roots, expected_dismissed,
                            expected_review_version, today=None):
    """Persist or restore one reviewed pair if its preview state is still current."""
    if not isinstance(left_url, str) or not left_url:
        raise ValueError("left_url is required")
    if not isinstance(right_url, str) or not right_url:
        raise ValueError("right_url is required")
    if not isinstance(dismissed, bool):
        raise ValueError("dismissed must be a boolean")
    if not isinstance(expected_dismissed, bool):
        raise ValueError("expected_dismissed must be a boolean")
    if (isinstance(expected_review_version, bool)
            or not isinstance(expected_review_version, int)
            or expected_review_version < 0):
        raise ValueError("expected_review_version must be a non-negative integer")
    expected_roots = _expected_root_pair(expected_roots)
    if conn.in_transaction:
        raise RuntimeError("duplicate-candidate mutation requires a clean database connection")
    today = today or date.today()
    conn.execute("BEGIN IMMEDIATE")
    try:
        left = conn.execute(
            "SELECT job_url,repost_of FROM jobs WHERE job_url=?", (left_url,)
        ).fetchone()
        right = conn.execute(
            "SELECT job_url,repost_of FROM jobs WHERE job_url=?", (right_url,)
        ).fetchone()
        if left is None or right is None:
            raise ValueError("posting no longer exists")
        roots = tuple(sorted((_root(left), _root(right))))
        if list(roots) != expected_roots:
            raise ValueError(
                "duplicate chains changed since preview; refresh and review again"
            )
        pair = _candidate_map(conn, today)[0].get(roots)
        if pair is None:
            raise ValueError("no longer an eligible duplicate suggestion; refresh and retry")
        existing = conn.execute(
            "SELECT dismissed_at,dismissed,version FROM dupe_candidate_dismissals "
            "WHERE left_root=? AND right_root=?",
            roots,
        ).fetchone()
        current_dismissed = bool(existing and existing["dismissed"])
        current_version = existing["version"] if existing else 0
        if (current_dismissed != expected_dismissed
                or current_version != expected_review_version):
            raise ValueError("duplicate suggestion review changed; refresh and retry")
        if dismissed != current_dismissed:
            reviewed_at = datetime.now(timezone.utc).isoformat()
            if existing:
                changed = conn.execute(
                    "UPDATE dupe_candidate_dismissals "
                    "SET dismissed_at=?,dismissed=?,version=version+1 "
                    "WHERE left_root=? AND right_root=? AND version=?",
                    (reviewed_at, int(dismissed), *roots, current_version),
                )
                if changed.rowcount != 1:
                    raise RuntimeError(
                        "duplicate review version changed while holding the write transaction"
                    )
            else:
                conn.execute(
                    "INSERT INTO dupe_candidate_dismissals "
                    "(left_root,right_root,dismissed_at,dismissed,version) "
                    "VALUES (?,?,?,?,1)",
                    (*roots, reviewed_at, int(dismissed)),
                )
            review_version = current_version + 1
            dismissed_at = reviewed_at if dismissed else None
        else:
            review_version = current_version
            dismissed_at = (existing["dismissed_at"]
                            if existing and existing["dismissed"] else None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "left_root": roots[0], "right_root": roots[1],
        "dismissed": dismissed, "dismissed_at": dismissed_at,
        "review_version": review_version,
    }


def confirm_candidate(conn, left_url, right_url, expected_roots):
    """Confirm one previewed candidate without overriding newer review state.

    The root check and dismissal check run under the same immediate write transaction as
    the merge.  Whichever review action gets the lock first wins: a later stale confirmation
    cannot override ``Not the same role``, and a later dismissal cannot target an already
    merged pair.
    """
    if conn.in_transaction:
        raise RuntimeError("duplicate confirmation requires a clean database connection")
    expected_roots = _expected_root_pair(expected_roots)
    conn.execute("BEGIN IMMEDIATE")
    try:
        left, left_err = resolve_posting(conn, left_url)
        right, right_err = resolve_posting(conn, right_url)
        if left_err or right_err:
            conn.rollback()
            return None, left_err or right_err, [], []
        actual_roots = sorted((_root(left), _root(right)))
        if actual_roots != expected_roots:
            conn.rollback()
            return (None, "duplicate chains changed since preview; refresh and review again",
                    [], [])
        ignored = conn.execute(
            "SELECT 1 FROM dupe_candidate_dismissals "
            "WHERE left_root=? AND right_root=? AND dismissed=1",
            tuple(expected_roots),
        ).fetchone()
        if ignored is not None:
            conn.rollback()
            return (None, "duplicate suggestion was reviewed as different roles; refresh",
                    [], [])
        plan, err = dupe_resolve(conn, left_url, right_url)
        if err:
            conn.rollback()
            return None, err, [], []
        affected, exempt = dupe_commit(conn, plan)
        return plan, None, affected, exempt
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
