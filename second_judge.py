"""Second-opinion review layer: an independent judge re-reads the day's
interesting zone and its opinions render in the report as evidence.

The zone is PASS at/above the cold-apply bar plus RECRUITER_ONLY at/above the
recruiter-route bar (states.COLD_APPLY_MIN_FIT / RECRUITER_ROUTE_MIN_FIT) —
undecided, unfiltered, posted within 14 days: every row the user might act on
plus the border band where the primary judge's ~22% draw noise buries or
under-scores real candidates. Opinions live in the `second_opinions` table and
NEVER touch `jobs.verdict`/`eval_json` — review, never re-route, same philosophy
as `eval_issues` surfacing. The model choice (claude-opus-5) and the zone come
from the 2026-08-11/12 judge measurement: see CHANGELOG 2026-08-12.

Runs through the Anthropic Message Batches API (50% price) because the layer is
deliberately not latency-coupled to applying: the report always shows the primary
verdicts immediately, and a row whose opinion hasn't landed reads exactly like
today's status quo. Batches usually finish inside an hour; `collect` ingests
whatever has ended and leaves the rest pending for the next run.

One opinion per duplicate chain: zone selection collapses dupe-linked siblings
(both keep status='evaluated') to one submission, and `opinion_summaries` maps
an opinion back to every current-chain member through the same repost_of-root
convention the packet/event reads use.

Imports `core` (paths/db/key + recency_dt), `evaluation` (the SAME system
prompt, user message, parser, normalize_result caps, and MODEL_PRICES the
primary judge uses — the two judges must read the same evidence and obey the
same code-enforced routing), and `states` (the action bars and
classify_disagreement). The anthropic SDK is imported lazily inside the
network paths so importing this module stays free.
"""

import hashlib
import sys
import time
from datetime import date, datetime, timedelta

from core import _ensure_api_key, recency_dt
from evaluation import (MODEL_PRICES, build_system_prompt, build_user_msg,
                        first_text, normalize_result, parse_eval_json)
from states import (COLD_APPLY_MIN_FIT, RECRUITER_ROUTE_MIN_FIT, VERDICT_PASS,
                    VERDICT_RECRUITER_ONLY, classify_disagreement)

MODEL = "claude-opus-5"
PASS_MIN_FIT = COLD_APPLY_MIN_FIT        # PASS zone floor (13+ includes the rescue band)
RO_MIN_FIT = RECRUITER_ROUTE_MIN_FIT     # RECRUITER_ONLY zone floor
# date_posted backstop — the standing allocation's own freshness rule. Nearly inert in
# the daily flow (LinkedIn ships no date_posted at all and the fetchers run every 3h at
# hours_old=4, so discovery age IS posting age for half the zone); it earns its keep only
# on genuinely stale sourced dates — ATS backlog boards, Adzuna's long-dead ads — which
# is also the only place a --backfill-days run can drag in something months old.
FRESH_DAYS = 14
# first_seen pre-window (local calendar days) — the REAL timing lever, because for the
# LinkedIn half of the zone it is the only age evidence there is. Sized off the
# apply-timing evidence (2026-08-09: TalentWorks n≈1600 puts the reply peak at 2-4 days
# with ~28%/day decay after; recruiters review by date in batches; the behavior rule that
# came out of it is "当天/次日投"): 2 days keeps the queue inside the window where an
# opinion can still change an action, while still covering the two plumbing tails a
# 1-day window drops — a MAX_PER_SUBMIT overflow tail, and rows that only became
# `evaluated` a day after first_seen (error requeue). A row sitting undecided for 3+ days
# was passed on by inaction; re-reading it for $0.046 cannot un-pass it. Reviewing the
# backlog anyway is a deliberate act, not a constant: use --backfill-days.
# first_seen is written by the local clock, so the cutoff is computed in Python from
# the same clock — never SQLite's UTC date('now').
DELTA_DAYS = 2
# Batch size is a COST lever, not just a pacing one: the system prompt is ~17k tokens and
# batch prompt caching is best-effort, so a request that starts before the first cache write
# lands pays to write its OWN copy at 1.25x input. Measured 2026-08-12 on two real batches:
# 51 requests shared it (17.2k read / 4.7k written per row, $0.046/row) while 150 fanned out
# and duplicated it (5.0k read / 16.9k WRITTEN per row, $0.082/row -- cache writes alone were
# 65% of that batch's $12.22). Sized to the steady-state daily flow (~50 zone rows) so one
# slot normally drains it in a single well-cached batch. Two data points, not a curve: if a
# batch this size still thrashes, dropping cache_control entirely is the bounded fallback
# (a plain uncached system prompt is $0.043/row, cheaper than a write that nobody reads).
MAX_PER_SUBMIT = 50
MAX_TOKENS = 8000   # thinking is on by default on this model and counts against the cap
MAX_RETRIES = 2     # bounded: a persistently failing row must not become a paid loop
RETRY_AFTER_HOURS = 6

# $/token at Batch rates, derived from the one price table (evaluation.MODEL_PRICES):
# 50% of list; cache read 0.1x list, cache write 1.25x list, both likewise halved.
# Stored per row so the 2026-08-19 cost review can read real spend from the table.
_LIST_IN, _LIST_OUT = MODEL_PRICES[MODEL]
BATCH_IN = _LIST_IN * 0.5
BATCH_OUT = _LIST_OUT * 0.5
BATCH_CACHE_READ = _LIST_IN * 0.1 * 0.5
BATCH_CACHE_WRITE = _LIST_IN * 1.25 * 0.5


def _clip(text, limit):
    """Bound `text` to `limit` chars with a VISIBLE cut: a truncated note ending
    mid-word with no marker reads as complete text (the Chevron work-auth flag
    vanished exactly this way), so any cut must show itself."""
    return text if len(text) <= limit else text[:limit - 1] + "…"


def opinion_summaries(conn, rows):
    """job_url -> second-judge summary for a bounded row set, or None when the row's
    chain has no opinion. Chain-scoped exactly like packet/event reads: the opinion may
    sit on any current-chain member (dupe merges), so lookup maps both sides through
    the repost_of root. Each summary carries status/model/verdict/fit_score/note plus
    `direction` (states.classify_disagreement vs THIS row's verdict; None while
    pending/errored or in agreement). The report section and the UI card both read THIS
    function so the two surfaces can't drift; the UI additionally drops direction-less
    summaries — agreement spends zero pixels, the razor that keeps this layer an
    attention saver instead of an attention cost. The note is whitespace-collapsed and
    bounded HERE, once, so no consumer re-invents its own cut — at 400 to match the
    storage cap in _collect_one (stored notes arrive intact; the bound only fires on
    the failed_gate fallback or a legacy over-long value), with _clip's visible
    ellipsis when it does fire."""
    out = {r["job_url"]: None for r in rows}
    if not out:
        return out
    roots = {r["repost_of"] or r["job_url"] for r in rows}
    marks = ",".join("?" * len(roots))
    ops = conn.execute(
        f"""SELECT s.job_url, s.status, s.model, s.verdict, s.fit_score, s.gate_notes,
                   s.failed_gate, s.submitted_at,
                   COALESCE(j.repost_of, s.job_url) AS root
            FROM second_opinions s
            LEFT JOIN jobs j ON j.job_url = s.job_url
            WHERE COALESCE(j.repost_of, s.job_url) IN ({marks})""",
        list(roots)).fetchall()
    # A chain can hold more than one opinion (two postings judged independently, then
    # linked by hand). Resolve it deterministically instead of letting row order pick:
    # a posting's OWN opinion always wins on its own card, otherwise the chain's newest.
    own = {o["job_url"]: o for o in ops}
    newest = {}
    for o in ops:
        cur = newest.get(o["root"])
        if cur is None or ((o["submitted_at"] or "", o["job_url"])
                           > (cur["submitted_at"] or "", cur["job_url"])):
            newest[o["root"]] = o
    for r in rows:
        o = own.get(r["job_url"]) or newest.get(r["repost_of"] or r["job_url"])
        if o is None:
            continue
        out[r["job_url"]] = {
            "status": o["status"],
            "model": o["model"],
            "verdict": o["verdict"],
            "fit_score": o["fit_score"],
            "note": _clip(" ".join(
                (o["gate_notes"] or o["failed_gate"] or "").split()), 400) or None,
            "direction": (classify_disagreement(
                r["verdict"], r["fit_score"], o["verdict"], o["fit_score"])
                if o["status"] == "done" else None),
        }
    return out


def pending_rows(conn, backfill_days=None):
    """The zone rows not yet submitted — at most one per duplicate chain. Freshness
    runs through core.recency_dt (the ONE effective posted-at reading, shared with the
    report's age label and sort), so this can't drift from how the same stored shapes
    are read everywhere else; a row with no usable date stays eligible (first_seen is
    already inside the delta window)."""
    delta = DELTA_DAYS if backfill_days is None else int(backfill_days)
    seen_floor = (date.today() - timedelta(days=delta)).isoformat()
    # outcome_status needs no clause: chain._recompute_outcome clears it whenever a
    # chain is not applied, so app_status IS NULL already implies it.
    rows = conn.execute(
        """SELECT * FROM jobs
           WHERE status='evaluated' AND filter_source IS NULL
             AND app_status IS NULL
             AND ((verdict=? AND fit_score>=?) OR (verdict=? AND fit_score>=?))
             AND substr(first_seen,1,10) >= ?
             AND job_url NOT IN (SELECT job_url FROM second_opinions
                                 WHERE status != 'retry')
           ORDER BY first_seen DESC""",
        (VERDICT_PASS, PASS_MIN_FIT, VERDICT_RECRUITER_ONLY, RO_MIN_FIT, seen_floor),
    ).fetchall()
    # Chains, not rows: dupe-linked siblings both keep status='evaluated', but one
    # opinion covers the whole chain (opinion_summaries maps it back to every member).
    # 'retry' rows are released errors — they block nothing, here or above.
    opinion_roots = {row[0] for row in conn.execute(
        "SELECT COALESCE(j.repost_of, s.job_url) FROM second_opinions s "
        "LEFT JOIN jobs j ON j.job_url = s.job_url WHERE s.status != 'retry'")}
    cutoff = date.today() - timedelta(days=FRESH_DAYS)
    fresh, taken = [], set()
    for r in rows:
        root = r["repost_of"] or r["job_url"]
        if root in opinion_roots or root in taken:
            continue
        dt, mode = recency_dt(r["date_posted"], r["first_seen"])
        if mode is not None and dt.date() < cutoff:
            continue
        taken.add(root)
        fresh.append(r)
    if len(fresh) > MAX_PER_SUBMIT:
        # Loud, never silent: the tail stays eligible on the next slot (the dedup query
        # plus the DELTA_DAYS window keep it in view), so the cap paces spend across the
        # day's six slots rather than dropping rows.
        print(f"[second-judge] capping this submit at {MAX_PER_SUBMIT} of "
              f"{len(fresh)} eligible rows (newest first); the rest go next run")
        fresh = fresh[:MAX_PER_SUBMIT]
    return fresh


def _requeue_errors(conn):
    """Release errored opinions for a bounded re-attempt (MAX_RETRIES per posting) after
    a cooling-off period — the review layer's analogue of the primary pipeline's
    error-row requeue, so a transient batch/parse failure doesn't permanently exile a
    zone row. The row is MOVED to status='retry', never deleted: a failed attempt can
    already have been billed (a parse failure still paid for its tokens), and the
    2026-08-19 cost review reads spend straight off this table — deleting the row would
    quietly subtract real money from that answer. Keeping it also makes the retry bound
    durable: retry_count survives, so a later --backfill-days can't reset it to 0.
    Returns the number released. The cutoff is computed on the local clock because
    submitted_at is written by datetime.now()."""
    floor = ((datetime.now() - timedelta(hours=RETRY_AFTER_HOURS))
             .isoformat(timespec="seconds"))
    with conn:
        n = conn.execute(
            "UPDATE second_opinions SET status='retry', retry_count=retry_count+1 "
            "WHERE status='error' AND retry_count < ? AND submitted_at <= ?",
            (MAX_RETRIES, floor)).rowcount
    if n:
        print(f"[second-judge] released {n} errored opinion(s) for another attempt "
              f"(their recorded spend stays on the row)")
    return n


def submit(conn, rows):
    """One Batches API submission for `rows`. The ordering is money-safe: intent rows
    commit BEFORE the paid batches.create (status 'submitting'), then flip to 'pending'
    once the API accepted. A crash in the window leaves visible 'submitting' rows that
    BLOCK resubmission (the dedup query sees them) instead of a paid orphan batch that
    a rerun would silently pay for again; collect() ages them into errors after the
    batch lifetime, which routes them through the bounded requeue. A released 'retry'
    row is UPDATEd in place (job_url is the primary key) so its accumulated spend and
    retry_count survive the re-attempt."""
    if not rows:
        print("[second-judge] nothing new to submit")
        return None
    if not _ensure_api_key("ANTHROPIC_API_KEY"):
        print("[second-judge] ANTHROPIC_API_KEY not set — skipping", file=sys.stderr)
        return None
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    client = anthropic.Anthropic()
    # Same brain as the primary judge: profile + guide, cache-marked (batch
    # caching is best-effort; the marker costs nothing when it misses).
    system_blocks = [{"type": "text", "text": build_system_prompt(),
                     "cache_control": {"type": "ephemeral"}}]
    requests, url_by_cid = [], {}
    for r in rows:
        cid = hashlib.sha256(r["job_url"].encode("utf-8")).hexdigest()  # 64 hex = the API's custom_id cap
        url_by_cid[cid] = r["job_url"]
        requests.append(Request(
            custom_id=cid,
            params=MessageCreateParamsNonStreaming(
                model=MODEL, max_tokens=MAX_TOKENS, system=system_blocks,
                messages=[{"role": "user", "content": build_user_msg(r)}],
            ),
        ))
    now = datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.executemany(
            """INSERT INTO second_opinions
               (job_url, custom_id, model, status, submitted_at)
               VALUES (?, ?, ?, 'submitting', ?)
               ON CONFLICT(job_url) DO UPDATE SET
                   custom_id=excluded.custom_id, model=excluded.model,
                   status='submitting', submitted_at=excluded.submitted_at,
                   batch_id=NULL, collected_at=NULL, error=NULL
               WHERE second_opinions.status='retry'""",
            [(url, cid, MODEL, now) for cid, url in url_by_cid.items()],
        )
    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        with conn:
            if isinstance(e, anthropic.APIStatusError):
                # The server answered with an error: no batch exists, so the intent rows
                # release immediately for a clean retry next run. Released, not deleted —
                # a row upserted from 'retry' still carries a previous attempt's billed
                # spend and its retry_count, and 'retry' is exactly the state the dedup
                # query re-offers (a never-attempted row simply releases with nothing on it).
                conn.executemany(
                    "UPDATE second_opinions SET status='retry' "
                    "WHERE job_url=? AND status='submitting'",
                    [(u,) for u in url_by_cid.values()])
            # Anything else (timeout, connection loss) is ambiguous — the batch MAY
            # exist server-side. Keep the 'submitting' rows: collect() ages them into
            # errors after the batch lifetime instead of resubmitting a possibly-paid
            # zone right away.
        raise
    with conn:
        conn.executemany(
            "UPDATE second_opinions SET status='pending', batch_id=? "
            "WHERE job_url=? AND status='submitting'",
            [(batch.id, u) for u in url_by_cid.values()])
    print(f"[second-judge] submitted batch {batch.id}: {len(requests)} posting(s)")
    return batch.id


def _record_success(conn, custom_id, text, usage, stop_reason=None):
    """Parse one succeeded result through the SAME parse + normalize path as the
    primary judge (so the 50/0, leadership, and core_function caps bind here too)
    and store the opinion. Split out from `collect` so tests exercise it without
    SDK result objects. An empty text (refusal, or thinking that exhausted
    max_tokens) records the real cause as an error row — with its spend, which
    happened regardless — instead of a misleading parse error.

    Tokens and cost ACCUMULATE onto the row: after a requeued retry the column holds
    what this posting really cost across attempts, which is what the cost review sums."""
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = (in_tok * BATCH_IN + out_tok * BATCH_OUT
            + cr * BATCH_CACHE_READ + cw * BATCH_CACHE_WRITE)
    now = datetime.now().isoformat(timespec="seconds")

    def _fail(reason):
        conn.execute(
            """UPDATE second_opinions
               SET status='error', error=?, collected_at=?,
                   in_tokens=COALESCE(in_tokens,0)+?, out_tokens=COALESCE(out_tokens,0)+?,
                   cache_read_tokens=COALESCE(cache_read_tokens,0)+?,
                   cache_write_tokens=COALESCE(cache_write_tokens,0)+?,
                   cost_usd=COALESCE(cost_usd,0)+?
               WHERE custom_id=? AND status='pending'""",
            (reason[:200], now, in_tok, out_tok, cr, cw, cost, custom_id),
        )

    if not text.strip():
        _fail(f"no text (stop_reason={stop_reason})")
        return
    try:
        result = normalize_result(parse_eval_json(text))
    except Exception as e:  # malformed model output is data, not a crash
        _fail(f"parse: {type(e).__name__}: {e}")
        return
    issues = ",".join(result.get("eval_issues") or []) or None
    conn.execute(
        """UPDATE second_opinions
           SET status='done', verdict=?, fit_score=?, bucket=?, failed_gate=?,
               gate_notes=?, eval_issues=?, collected_at=?,
               in_tokens=COALESCE(in_tokens,0)+?, out_tokens=COALESCE(out_tokens,0)+?,
               cache_read_tokens=COALESCE(cache_read_tokens,0)+?,
               cache_write_tokens=COALESCE(cache_write_tokens,0)+?,
               cost_usd=COALESCE(cost_usd,0)+?
           WHERE custom_id=? AND status='pending'""",
        (result.get("verdict"), result.get("fit_score"), result.get("bucket"),
         result.get("failed_gate"), _clip(result.get("gate_notes") or "", 400) or None,
         issues, now, in_tok, out_tok, cr, cw, cost, custom_id),
    )


def _record_failure(conn, custom_id, kind):
    conn.execute(
        """UPDATE second_opinions SET status='error', error=?, collected_at=?
           WHERE custom_id=? AND status='pending'""",
        (kind[:200], datetime.now().isoformat(timespec="seconds"), custom_id),
    )


def _ingested_days(conn, custom_ids):
    """The daily reports the caller must rebuild to fold fresh opinions in: the
    first_seen day of every CURRENT CHAIN MEMBER of the postings behind `custom_ids`,
    not just the submitted posting's own day. Opinions are read chain-wide
    (opinion_summaries), so an opinion submitted for today's relisting also changes the
    report of the day its canonical was first seen."""
    days = set()
    for i in range(0, len(custom_ids), 500):
        chunk = custom_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        days.update(d for (d,) in conn.execute(
            f"""SELECT DISTINCT substr(m.first_seen, 1, 10)
                FROM second_opinions s
                JOIN jobs j ON j.job_url = s.job_url
                JOIN jobs m ON COALESCE(m.repost_of, m.job_url)
                             = COALESCE(j.repost_of, j.job_url)
                WHERE s.custom_id IN ({marks})""", chunk))
    return days


def collect(conn):
    """Ingest every ended batch with pending rows. Returns (all_done, days):
    all_done is True when nothing is left pending (so `run` knows to stop
    polling); days is the set of first_seen days whose reports gained opinions
    in THIS call (pipeline rebuilds exactly those)."""
    # Age out interrupted submissions: a 'submitting' row older than the 24h batch
    # lifetime either never had a batch or belongs to one whose id we lost — either
    # way it will never be collected. Flip it to an error so the bounded requeue
    # gives the posting another attempt, loudly.
    #
    # Checked with a SELECT first, and the UPDATE wrapped so it always ends its
    # transaction: `collect` runs once a minute for up to 90 minutes, and an
    # unconditional UPDATE takes SQLite's writer lock the moment it executes. Left
    # open (nothing matched, so nothing committed) it starves the concurrently
    # scheduled `run` of every eval write for the whole poll — observed live on
    # 2026-08-12 as a stream of "[eval] DB write failed (database is locked)".
    stale_floor = ((datetime.now() - timedelta(hours=24))
                   .isoformat(timespec="seconds"))
    stale = conn.execute(
        "SELECT COUNT(*) FROM second_opinions "
        "WHERE status='submitting' AND submitted_at <= ?", (stale_floor,)).fetchone()[0]
    if stale:
        with conn:
            conn.execute(
                "UPDATE second_opinions SET status='error', error='submit interrupted', "
                "collected_at=? WHERE status='submitting' AND submitted_at <= ?",
                (datetime.now().isoformat(timespec="seconds"), stale_floor))
        print(f"[second-judge] aged {stale} interrupted submission(s) into errors "
              f"(bounded requeue will retry them)")

    batches = [r["batch_id"] for r in conn.execute(
        "SELECT DISTINCT batch_id FROM second_opinions WHERE status='pending'")]
    if not batches:
        print("[second-judge] no pending opinions")
        return True, set()
    if not _ensure_api_key("ANTHROPIC_API_KEY"):
        print("[second-judge] ANTHROPIC_API_KEY not set — cannot collect", file=sys.stderr)
        return False, set()
    import anthropic

    client = anthropic.Anthropic()
    still_processing, ingested_cids = 0, []
    for bid in batches:
        b = client.messages.batches.retrieve(bid)
        if b.processing_status != "ended":
            still_processing += 1
            continue
        # Drain the result stream FIRST, write after. Writing inside the loop would
        # hold SQLite's writer lock across every network read of a 150-result stream,
        # and the concurrently scheduled `run` (this job starts 15 min after it) would
        # lose eval writes to "database is locked" for that whole span. Results are
        # small and MAX_PER_SUBMIT-bounded, so buffering them costs a MB at most.
        landed = [(res.custom_id, res.result) for res in
                  client.messages.batches.results(bid)]
        with conn:
            for cid, result in landed:
                if result.type == "succeeded":
                    msg = result.message
                    _record_success(conn, cid, first_text(msg.content), msg.usage,
                                    getattr(msg, "stop_reason", None))
                else:
                    _record_failure(conn, cid, result.type)
                ingested_cids.append(cid)
        ingested = len(landed)
        n, spend, cr, cw = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0), COALESCE(SUM(cache_read_tokens),0),"
            " COALESCE(SUM(cache_write_tokens),0) FROM second_opinions "
            "WHERE batch_id=? AND status='done'", (bid,)).fetchone()
        # Prompt-cache sharing is the layer's dominant cost variable, so it gets printed
        # where the money is spent instead of being reconstructed from the table later.
        # "shared" = cache reads as a share of all cacheable prompt traffic: the ~17k
        # system prompt is either READ from one shared entry (0.1x input) or RE-WRITTEN
        # per request (1.25x). Measured 2026-08-12: 78% shared at 51 requests
        # ($0.046/row) vs 23% at 150 ($0.082/row, cache writes alone 65% of the bill).
        # A low number here means the batch fanned out faster than the first write landed
        # -- lower MAX_PER_SUBMIT, or drop cache_control for a flat $0.043/row.
        detail = ""
        if n:
            share = 100 * cr / max(cr + cw, 1)
            detail = (f" = ${spend / n:.3f}/row; prompt cache {share:.0f}% shared, "
                      f"{cw / n / 1000:.1f}k tok/row re-written")
        print(f"[second-judge] batch {bid}: ingested {ingested} result(s) "
              f"({n} opinions, ${spend:.2f}{detail})")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM second_opinions WHERE status='pending'").fetchone()[0]
    if still_processing:
        print(f"[second-judge] {still_processing} batch(es) still processing "
              f"({remaining} opinion(s) pending)")
    return remaining == 0, _ingested_days(conn, ingested_cids)


def run(conn, wait_minutes=90, backfill_days=None):
    """Requeue bounded errors, submit the current zone, then poll until every batch
    lands or the wait budget runs out — a batch that outlives the wait is collected
    by the next invocation, and the report simply shows those rows as pending
    meanwhile. Returns the set of first_seen days whose reports gained opinions."""
    _requeue_errors(conn)
    submit(conn, pending_rows(conn, backfill_days))
    days = set()
    deadline = time.monotonic() + wait_minutes * 60
    while True:
        all_done, new_days = collect(conn)
        days |= new_days
        if all_done:
            return days
        if not _ensure_api_key("ANTHROPIC_API_KEY"):
            # collect() already said why. Polling cannot fix a missing key, and the
            # scheduled slot must not sit here burning the whole wait budget.
            print("[second-judge] no API key — leaving the pending opinions "
                  "for a run that has one")
            return days
        if time.monotonic() >= deadline:
            print(f"[second-judge] wait budget ({wait_minutes} min) spent — "
                  "remaining opinions will be collected next run")
            return days
        time.sleep(60)
