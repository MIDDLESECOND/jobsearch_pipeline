"""notify_deepdive_batch: the doorbell's zone reading and its popup arithmetic.

The popup itself (a Windows MessageBox) and the quota HTTP call are not exercised —
what tests must pin is the pure logic three review rounds kept breaking: which rows
count as BATCHABLE (all legs of the batch scope, not just the fit bar), the subset
relation the popup's nested counts claim, and the malformed-state guards that decide
whether a bad calibration file degrades or takes the whole notification down.
"""

import datetime as dt
import json

import notify_deepdive_batch as nb
from conftest import make_job


def _recent(days=0):
    return (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds")


def _rows(conn):
    """zone_rows keyed by url -> (is_snippet, is_batchable)."""
    return {u: (snip, batch) for u, _, snip, batch in nb.zone_rows(conn)}


def _opinion(conn, url, verdict="PASS"):
    conn.execute(
        "INSERT INTO second_opinions "
        "(job_url, custom_id, model, status, submitted_at, verdict) "
        "VALUES (?, ?, 'm', 'done', ?, ?)",
        (url, f"cid-{url}", _recent(), verdict))
    conn.commit()


# ----------------------------------------------------------------- zone membership

def test_zone_spans_both_bars_but_only_pass_at_the_cold_bar_is_batchable(conn):
    """The zone is context (it feeds the popup's "区内共" line); the batch scope is
    narrower. Conflating them is what made pending undrainable."""
    batchable = make_job(conn, verdict="PASS", fit_score=15, first_seen=_recent())
    sub_bar = make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent())
    recruiter = make_job(conn, verdict="RECRUITER_ONLY", fit_score=16, first_seen=_recent())
    rows = _rows(conn)
    assert rows[batchable["job_url"]][1] is True
    # both stay visible in the zone, neither may drive the fire/skip decision
    assert rows[sub_bar["job_url"]][1] is False
    assert rows[recruiter["job_url"]][1] is False


def test_zone_excludes_decided_filtered_and_unevaluated_rows(conn):
    out = [
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(), app_status="applied"),
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(),
                 filter_source="filters.yaml"),
        make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent(), status="new"),
        make_job(conn, verdict="GATE_FAIL", fit_score=None, first_seen=_recent()),
    ]
    rows = _rows(conn)
    assert not {r["job_url"] for r in out} & set(rows)


def test_stale_rows_leave_the_zone_and_the_batch_window_is_tighter(conn):
    """Two windows, one recency reading: 14 days keeps a row in the zone, 5 days keeps
    it batchable. A row between them is visible but not batch material."""
    fresh = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    mid = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(days=9))
    stale = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(days=30))
    rows = _rows(conn)
    assert rows[fresh["job_url"]][1] is True
    assert mid["job_url"] in rows and rows[mid["job_url"]][1] is False
    assert stale["job_url"] not in rows


def test_second_judge_downgrade_removes_a_row_from_the_batch_scope(conn):
    agreed = make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent())
    demoted = make_job(conn, verdict="PASS", fit_score=17, first_seen=_recent())
    _opinion(conn, agreed["job_url"], "PASS")
    _opinion(conn, demoted["job_url"], "RECRUITER_ONLY")
    rows = _rows(conn)
    assert rows[agreed["job_url"]][1] is True
    assert rows[demoted["job_url"]][1] is False      # still in the zone, never batched


def test_snippet_flag_is_source_scoped_not_length_scoped(conn):
    """An Adzuna 500-char row is a truncation artifact; a short LinkedIn JD is complete
    evidence. A snippet row stays batchable — the batch completes its JD in a browser."""
    snippet = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                       source="adzuna", description="x" * 500)
    full_adzuna = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                           source="adzuna", description="x" * 2000)
    short_linkedin = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                              source="linkedin", description="thin but complete")
    rows = _rows(conn)
    assert rows[snippet["job_url"]] == (True, True)
    assert rows[full_adzuna["job_url"]][0] is False
    assert rows[short_linkedin["job_url"]][0] is False


def test_null_source_row_is_not_treated_as_a_snippet(conn):
    """Three-valued logic trap: NULL source must not read as 'adzuna'."""
    row = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
                   description="short")
    conn.execute("UPDATE jobs SET source=NULL WHERE job_url=?", (row["job_url"],))
    conn.commit()
    assert _rows(conn)[row["job_url"]] == (False, True)


# --------------------------------------------------------------- popup arithmetic

def _counts(conn, state):
    """Reproduce main()'s pending/new_rows derivation from a given state dict."""
    fresh = nb.zone_rows(conn)
    processed = set(state.get("processed_urls") or [])
    last_batch = state.get("last_batch_iso", "")
    pending = [u for u, _, _, b in fresh if b and u not in processed]
    if last_batch:
        new_rows = [u for u, seen, _, b in fresh
                    if b and seen > last_batch and u not in processed]
    else:
        new_rows = pending
    return pending, new_rows


def test_arrivals_are_always_a_subset_of_pending(conn):
    """The popup nests them ("待看 N（其中新进 M）"), so M ⊆ N must hold — including the
    no-state case, which once printed a nested count larger than its own total."""
    make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())          # batchable, new
    make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent())          # sub-bar, new
    make_job(conn, verdict="RECRUITER_ONLY", fit_score=16, first_seen=_recent())  # RO, new
    for state in ({}, {"last_batch_iso": _recent(days=1)}):
        pending, new_rows = _counts(conn, state)
        assert set(new_rows) <= set(pending)
        assert len(new_rows) <= len(pending)


def test_processed_rows_leave_pending_and_can_empty_it(conn):
    """The no-popup branch must be reachable: a zone full of non-batchable rows plus
    fully-batched batchable ones means there is nothing to propose."""
    done = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent())    # never batchable
    pending, _ = _counts(conn, {"processed_urls": [done["job_url"]]})
    assert pending == []


# ------------------------------------------------------------- malformed state

def test_load_state_survives_missing_and_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.setattr(nb, "STATE_PATH", tmp_path / "absent.json")
    assert nb.load_state() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(nb, "STATE_PATH", bad)
    assert nb.load_state() == {}


def test_doorbell_state_write_repairs_a_non_dict_doorbell_key(tmp_path, monkeypatch):
    """setdefault would return the stored null and raise on .update(); the popup must
    not die because a cooldown marker could not be merged."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"doorbell": None, "calibration": {"minutes_per_row": 2}}),
                    encoding="utf-8")
    monkeypatch.setattr(nb, "STATE_PATH", path)
    nb._save_doorbell_state({"nudge_failed_for_expiry": 123})
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["doorbell"] == {"nudge_failed_for_expiry": 123}
    assert saved["calibration"] == {"minutes_per_row": 2}   # other keys untouched


def test_doorbell_state_write_tolerates_a_non_object_state_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(nb, "STATE_PATH", path)
    nb._save_doorbell_state({"nudge_failed_for_expiry": 7})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "doorbell": {"nudge_failed_for_expiry": 7}}
