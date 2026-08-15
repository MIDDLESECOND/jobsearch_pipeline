"""second_judge: zone selection and opinion ingestion.

Pure DB tests over the conftest schema — no network, no anthropic SDK. The
submit/collect network paths are exercised operationally; what tests must pin
is the population contract (who gets a second opinion) and that ingestion runs
results through the SAME normalize_result caps as the primary judge.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import evaluation
import second_judge
from conftest import make_job
from states import classify_disagreement


def _recent():
    return datetime.now().isoformat(timespec="seconds")


def _stale():
    return (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")


def _urls(rows):
    return {r["job_url"] for r in rows}


def test_zone_includes_pass13_pass15_and_ro15(conn):
    a = make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent())
    b = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    c = make_job(conn, verdict="RECRUITER_ONLY", fit_score=15, first_seen=_recent())
    urls = _urls(second_judge.pending_rows(conn))
    assert {a["job_url"], b["job_url"], c["job_url"]} <= urls


def test_zone_excludes_low_fit_decided_filtered_and_failed(conn):
    out = [
        make_job(conn, verdict="PASS", fit_score=12, first_seen=_recent()),
        make_job(conn, verdict="RECRUITER_ONLY", fit_score=14, first_seen=_recent()),
        make_job(conn, verdict="GATE_FAIL", fit_score=None, first_seen=_recent()),
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                 app_status="applied"),
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                 filter_source="filters.yaml"),
        # outside the first_seen delta window entirely
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_stale()),
    ]
    urls = _urls(second_judge.pending_rows(conn))
    assert not (_urls([]) | {r["job_url"] for r in out}) & urls


def test_zone_excludes_stale_posted_but_keeps_unparseable_dates(conn):
    stale_posted = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                            date_posted=(datetime.now() - timedelta(days=30))
                            .date().isoformat())
    no_date = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                       date_posted="")
    urls = _urls(second_judge.pending_rows(conn))
    assert stale_posted["job_url"] not in urls
    assert no_date["job_url"] in urls


def test_zone_excludes_adzuna_snippet_rows_but_not_short_jds(conn):
    snip = make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                    source="adzuna", description="x" * 500)
    full_adzuna = make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                           source="adzuna", description="x" * 2000)
    short_linkedin = make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                              source="linkedin", description="thin but complete JD")
    urls = _urls(second_judge.pending_rows(conn))
    assert snip["job_url"] not in urls           # snippet: no paid re-read of 500 chars
    assert full_adzuna["job_url"] in urls        # full-text Adzuna row stays eligible
    assert short_linkedin["job_url"] in urls     # shortness alone is not snippet-ness


def test_zone_skips_already_submitted(conn):
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    conn.execute(
        "INSERT INTO second_opinions (job_url, custom_id, model, status, submitted_at) "
        "VALUES (?, 'cid', 'm', 'pending', 'now')", (r["job_url"],))
    conn.commit()
    assert r["job_url"] not in _urls(second_judge.pending_rows(conn))


def _pending_opinion(conn, url, cid="cid1"):
    conn.execute(
        "INSERT INTO second_opinions (job_url, custom_id, model, status, submitted_at) "
        "VALUES (?, ?, ?, 'pending', 'now')", (url, cid, second_judge.MODEL))
    conn.commit()


def _usage(in_tok=15000, out_tok=2000, cr=0, cw=0):
    return SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok,
                           cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def test_record_success_applies_normalize_caps(conn):
    # A PASS with ai_artifact_depth 0 must land as RECRUITER_ONLY: ingestion runs
    # the same code-enforced routing as the primary judge, or the two judges'
    # verdicts wouldn't be comparable.
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _pending_opinion(conn, r["job_url"])
    text = json.dumps({
        "verdict": "PASS", "fit_score": 16, "bucket": 3,
        "score_breakdown": {"ai_applied_vs_research": 3, "ai_artifact_depth": 0},
        "gate_results": {"years_floor": "PASS"},
    })
    second_judge._record_success(conn, "cid1", text, _usage())
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='cid1'").fetchone()
    assert o["status"] == "done"
    assert o["verdict"] == "RECRUITER_ONLY"
    assert o["cost_usd"] > 0


def test_record_success_stores_parse_failure_as_error_row(conn):
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _pending_opinion(conn, r["job_url"], cid="cid2")
    second_judge._record_success(conn, "cid2", "not json at all", _usage())
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='cid2'").fetchone()
    assert o["status"] == "error"
    assert o["error"] and o["error"].startswith("parse:")
    # spend happened even though parsing failed — the cost review must see it
    assert o["cost_usd"] > 0


def test_batch_pricing_includes_cache_tiers(conn):
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _pending_opinion(conn, r["job_url"], cid="cid3")
    text = json.dumps({"verdict": "RECRUITER_ONLY", "fit_score": 12, "bucket": 1,
                       "gate_results": {}, "score_breakdown": {}})
    second_judge._record_success(conn, "cid3", text, _usage(in_tok=1000, out_tok=1000,
                                                            cr=10000, cw=5000))
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='cid3'").fetchone()
    expected = (1000 * second_judge.BATCH_IN + 1000 * second_judge.BATCH_OUT
                + 10000 * second_judge.BATCH_CACHE_READ
                + 5000 * second_judge.BATCH_CACHE_WRITE)
    assert abs(o["cost_usd"] - expected) < 1e-9


def test_record_success_empty_text_is_error_with_stop_reason(conn):
    # A refusal / thinking-exhausted response has no text block; that must record
    # the real cause (and the spend, which happened regardless), never a
    # misleading "parse:" error over an empty string.
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _pending_opinion(conn, r["job_url"], cid="cid4")
    second_judge._record_success(conn, "cid4", "  ", _usage(), stop_reason="max_tokens")
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='cid4'").fetchone()
    assert o["status"] == "error"
    assert "max_tokens" in o["error"]
    assert o["cost_usd"] > 0


def test_zone_submits_one_row_per_duplicate_chain(conn):
    # Two dupe-linked postings both keep status='evaluated' (AGENTS.md); the zone
    # must submit the chain once, not pay for both sides of the same role.
    a = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    b = make_job(conn, verdict="PASS", fit_score=15, first_seen=_recent(),
                 repost_of=a["job_url"])
    rows = second_judge.pending_rows(conn)
    chain = {a["job_url"], b["job_url"]}
    assert len(chain & _urls(rows)) == 1


def test_zone_skips_chain_with_opinion_on_sibling(conn):
    a = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    b = make_job(conn, verdict="PASS", fit_score=15, first_seen=_recent(),
                 repost_of=a["job_url"])
    _pending_opinion(conn, a["job_url"])
    urls = _urls(second_judge.pending_rows(conn))
    assert a["job_url"] not in urls
    assert b["job_url"] not in urls


def _done_opinion(conn, url, verdict, fit, cid="done1", notes="the reason"):
    conn.execute(
        "INSERT INTO second_opinions (job_url, custom_id, model, status, submitted_at,"
        " verdict, fit_score, gate_notes) VALUES (?, ?, ?, 'done', 'now', ?, ?, ?)",
        (url, cid, second_judge.MODEL, verdict, fit, notes))
    conn.commit()


def test_opinion_summaries_map_through_chain_root(conn):
    # The opinion sits on the canonical; a dupe-linked sibling's card must still
    # show the warning, with the direction computed against the SIBLING's verdict.
    a = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    b = make_job(conn, verdict="PASS", fit_score=15, first_seen=_recent(),
                 repost_of=a["job_url"])
    _done_opinion(conn, a["job_url"], "GATE_FAIL", None)
    row_b = conn.execute("SELECT * FROM jobs WHERE job_url=?", (b["job_url"],)).fetchone()
    ops = second_judge.opinion_summaries(conn, [row_b])
    o = ops[b["job_url"]]
    assert o is not None and o["direction"] == "demote"
    assert o["verdict"] == "GATE_FAIL"


def test_opinion_summaries_collapse_note_whitespace(conn):
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _done_opinion(conn, r["job_url"], "PASS", 10, notes="line one\n\nline two\t tabbed")
    o = second_judge.opinion_summaries(conn, [r])[r["job_url"]]
    assert o is not None and o["note"] == "line one line two tabbed"


def test_opinion_summaries_note_matches_storage_cap_and_marks_a_cut(conn):
    # A stored note at the 400-char storage cap must reach the card whole (the old
    # 200 display cut silently ate a work-auth flag), and anything longer must end
    # in a visible ellipsis instead of a mid-word stop that reads as complete.
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _done_opinion(conn, r["job_url"], "PASS", 10, notes="x" * 400)
    at_cap = second_judge.opinion_summaries(conn, [r])[r["job_url"]]
    assert at_cap is not None and at_cap["note"] == "x" * 400

    over = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    _done_opinion(conn, over["job_url"], "PASS", 10, cid="done2", notes="y" * 450)
    cut = second_judge.opinion_summaries(conn, [over])[over["job_url"]]
    assert cut is not None
    note = cut["note"]
    assert len(note) == 400 and note == "y" * 399 + "…"


def test_clip_marks_write_side_truncation():
    assert second_judge._clip("z" * 400, 400) == "z" * 400
    clipped = second_judge._clip("z" * 401, 400)
    assert len(clipped) == 400 and clipped.endswith("…")


def test_requeue_errors_is_bounded_and_preserves_recorded_spend(conn):
    old = (datetime.now() - timedelta(hours=12)).isoformat(timespec="seconds")
    exhausted, retryable, fresh_err = [make_job(conn, verdict="PASS", fit_score=16,
                                                first_seen=_recent()) for _ in range(3)]
    conn.execute("INSERT INTO second_opinions (job_url, custom_id, model, status,"
                 " submitted_at, retry_count) VALUES (?, 'e1', 'm', 'error', ?, ?)",
                 (exhausted["job_url"], old, second_judge.MAX_RETRIES))
    conn.execute("INSERT INTO second_opinions (job_url, custom_id, model, status,"
                 " submitted_at, retry_count, cost_usd) VALUES (?, 'e2', 'm', 'error',"
                 " ?, 1, 0.05)", (retryable["job_url"], old))
    conn.execute("INSERT INTO second_opinions (job_url, custom_id, model, status,"
                 " submitted_at, retry_count) VALUES (?, 'e3', 'm', 'error', ?, 0)",
                 (fresh_err["job_url"], _recent()))
    conn.commit()
    assert second_judge._requeue_errors(conn) == 1   # only the cooled-off, under-cap row
    rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM second_opinions")}
    # nothing is deleted: the failed attempt's spend must survive for the cost review,
    # and the retry bound must survive a later --backfill-days
    assert len(rows) == 3
    released = rows[retryable["job_url"]]
    assert released["status"] == "retry" and released["retry_count"] == 2
    assert released["cost_usd"] == 0.05
    assert rows[exhausted["job_url"]]["status"] == "error"
    assert rows[fresh_err["job_url"]]["status"] == "error"
    # and the released row is zone-eligible again, while the other two stay blocked
    urls = _urls(second_judge.pending_rows(conn))
    assert retryable["job_url"] in urls
    assert exhausted["job_url"] not in urls and fresh_err["job_url"] not in urls


def test_record_success_accumulates_spend_across_attempts(conn):
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    conn.execute("INSERT INTO second_opinions (job_url, custom_id, model, status,"
                 " submitted_at, retry_count, cost_usd, out_tokens)"
                 " VALUES (?, 'cid9', 'm', 'pending', 'now', 1, 0.05, 100)",
                 (r["job_url"],))
    conn.commit()
    text = json.dumps({"verdict": "PASS", "fit_score": 16, "bucket": 3,
                       "gate_results": {}, "score_breakdown": {}})
    second_judge._record_success(conn, "cid9", text, _usage(in_tok=0, out_tok=1000))
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='cid9'").fetchone()
    assert o["status"] == "done"
    assert o["out_tokens"] == 1100                                   # 100 + 1000
    assert abs(o["cost_usd"] - (0.05 + 1000 * second_judge.BATCH_OUT)) < 1e-9


def test_opinion_summaries_pick_is_deterministic_on_a_multi_opinion_chain(conn):
    # Two postings judged independently, then linked by hand: a posting's OWN opinion
    # wins on its own card, and the sibling falls back to the chain's newest.
    a = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    b = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                 repost_of=a["job_url"])
    for url, cid, verdict, when in ((a["job_url"], "oa", "GATE_FAIL", "2026-08-01"),
                                    (b["job_url"], "ob", "PASS", "2026-08-02")):
        conn.execute(
            "INSERT INTO second_opinions (job_url, custom_id, model, status,"
            " submitted_at, verdict, fit_score) VALUES (?, ?, 'm', 'done', ?, ?, 16)",
            (url, cid, when, verdict))
    conn.commit()
    rows = conn.execute("SELECT * FROM jobs WHERE job_url IN (?,?)",
                        (a["job_url"], b["job_url"])).fetchall()
    ops = second_judge.opinion_summaries(conn, rows)
    own_a, own_b = ops[a["job_url"]], ops[b["job_url"]]
    assert own_a is not None and own_a["verdict"] == "GATE_FAIL"   # a's own
    assert own_b is not None and own_b["verdict"] == "PASS"        # b's own
    # a third member with no opinion of its own falls back to the newest in the chain
    c = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                 repost_of=a["job_url"])
    row_c = conn.execute("SELECT * FROM jobs WHERE job_url=?", (c["job_url"],)).fetchone()
    inherited = second_judge.opinion_summaries(conn, [row_c])[c["job_url"]]
    assert inherited is not None and inherited["verdict"] == "PASS"


def test_ingested_days_cover_every_chain_members_day(conn):
    canonical = make_job(conn, verdict="PASS", fit_score=16,
                         first_seen="2026-08-05T09:00:00")
    relisting = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                         repost_of=canonical["job_url"])
    _pending_opinion(conn, relisting["job_url"], cid="cidD")
    days = second_judge._ingested_days(conn, ["cidD"])
    # the submitted relisting's day AND the canonical's, whose report also changes
    assert "2026-08-05" in days
    assert datetime.now().date().isoformat() in days


def test_collect_ages_interrupted_submissions_into_errors(conn):
    # A 'submitting' row older than the batch lifetime (crash between the intent
    # insert and the create/flip) must become a visible error — never a silent
    # forever-blocked row, and never an immediate paid resubmission.
    r = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    stale = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO second_opinions (job_url, custom_id, model, status,"
                 " submitted_at) VALUES (?, 's1', 'm', 'submitting', ?)",
                 (r["job_url"], stale))
    conn.commit()
    all_done, days = second_judge.collect(conn)   # no pending rows -> no network
    assert all_done and days == set()
    o = conn.execute("SELECT * FROM second_opinions WHERE custom_id='s1'").fetchone()
    assert o["status"] == "error"
    assert o["error"] == "submit interrupted"


def test_classify_disagreement_flags_bar_crossings_within_a_verdict():
    # verdict-level moves
    assert classify_disagreement("PASS", 16, "GATE_FAIL", None) == "demote"
    assert classify_disagreement("RECRUITER_ONLY", 15, "PASS", 16) == "promote"
    # fit-band moves inside an unchanged verdict: crossing the cold-apply bar (13)
    # or the recruiter-route bar (15) is a real action change
    assert classify_disagreement("PASS", 16, "PASS", 14) == "demote"
    assert classify_disagreement("PASS", 14, "PASS", 12) == "demote"
    assert classify_disagreement("PASS", 12, "PASS", 14) == "promote"
    assert classify_disagreement("RECRUITER_ONLY", 16, "RECRUITER_ONLY", 8) == "demote"
    # same band = agreement, whatever the raw delta
    assert classify_disagreement("PASS", 13, "PASS", 14) is None
    assert classify_disagreement("PASS", 16, "PASS", 17) is None


def test_empty_anthropic_response_is_not_retried_and_still_bills(conn):
    # A refusal / thinking-exhausted reply is deterministic: retrying pays for it
    # again for the same answer. It must also carry its usage, or the run's cost
    # line silently under-reports money that was really spent.
    e = evaluation.EmptyResponseError("no text (stop_reason=refusal)",
                                      in_tokens=15000, out_tokens=8000,
                                      cache_read=12000, cache_write=0)
    assert evaluation._retryable(e) is False
    assert (e.in_tokens, e.out_tokens, e.cache_read) == (15000, 8000, 12000)
    # every other non-HTTP failure stays retryable (a malformed body can re-emit)
    assert evaluation._retryable(ValueError("no JSON object in model response")) is True


def test_anthropic_temperature_is_allowlisted_not_denylisted():
    # Older models keep temperature=0 so their stored baselines stay comparable...
    assert evaluation.anthropic_extras("claude-sonnet-4-6") == {"temperature": 0}
    assert evaluation.anthropic_extras("claude-haiku-4-5-20251001") == {"temperature": 0}
    # ...while Claude 5 and ANY unknown/newer id omit it: sending temperature there is
    # a 400 on every call, and _retryable won't retry a 400.
    for model in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5",
                  "claude-haiku-5", "claude-opus-6-20270101"):
        assert evaluation.anthropic_extras(model) == {}
