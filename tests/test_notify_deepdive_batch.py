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


def test_zone_window_mirrors_the_second_judge():
    """FRESH_DAYS says "mirror the ... second-judge window" in a comment, and until now
    the comment was the only coupling: widen one side to 21 and both modules' suites
    stay green while the popup counts a different zone than the judge actually reviews
    (the AGENTS.md change-one-change-both pair — the predicate has already drifted
    twice). Import-and-compare is deliberately the whole test."""
    import second_judge
    assert nb.FRESH_DAYS == second_judge.FRESH_DAYS


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


# ------------------------------------------------------------------ launch argv

def test_launch_puts_the_trigger_before_the_variadic_add_dir():
    """The one thing a silent launch failure looks like: a console that opens and sits
    there. `--add-dir <directories...>` is variadic, so a trailing trigger becomes just
    another directory and the session starts with an empty prompt (observed 2026-08-15).
    Pin the order: positional first, and nothing after the variadic but its own values.
    """
    argv = nb._launch_argv(r"C:\bin\claude.exe", r"C:\brain")
    assert argv[argv.index(r"C:\bin\claude.exe") + 1] == nb.BATCH_TRIGGER
    assert argv.index(nb.BATCH_TRIGGER) < argv.index("--add-dir")
    assert argv[-1] == r"C:\brain"       # the variadic list ends the command line


def test_launch_keeps_cmd_from_eating_quotes_around_a_spaced_path():
    """Measured: with the exe path quoted (it has a space) and sitting directly after
    /k, cmd strips the outer quote pair and splits at the space — 'C:\\Program' is not
    recognized. A plain word in front of it stops the heuristic from ever firing."""
    argv = nb._launch_argv(r"C:\Program Files\claude.exe", r"C:\Users\A B\brain")
    assert argv[argv.index("/k") + 1] == "call"
    assert argv[argv.index("call") + 1] == r"C:\Program Files\claude.exe"


# ------------------------------------------------------------------- launch env

def test_launch_env_strips_session_markers_and_forces_persistence():
    """A doorbell fired from inside a Claude Code session must still hand the batch a
    clean top-level session, or the transcript is never written."""
    env = nb._launch_env({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x",
                          "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "p"}, file_login=None)
    assert not {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"} & set(env)
    assert env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1"
    assert env["PATH"] == "p"           # everything else passes through


def test_oauth_token_is_stripped_only_when_a_file_login_can_take_over():
    """The strip exists so the console runs the plan's judge, not the API-side default.
    With no file login there is nothing to fall back TO, and stripping would park the
    console on a login prompt — the same dead window the launcher just stopped making."""
    base = {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in nb._launch_env(base, file_login="live")
    assert nb._launch_env(base, file_login=None)["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


def test_launch_env_keeps_the_pipeline_api_key():
    """Measured: a console carrying only ANTHROPIC_API_KEY still comes up on the plan,
    so stripping it changes no auth — it only forces every pipeline.py command the batch
    runs to re-read the key from HKCU."""
    for login in ("live", None):
        assert nb._launch_env({"ANTHROPIC_API_KEY": "k"}, login)["ANTHROPIC_API_KEY"] == "k"


def test_launch_env_does_not_mutate_the_caller_environment():
    """main() passes os.environ itself; a pop against that would unset the doorbell's
    own key for the rest of the process."""
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "tok"}
    nb._launch_env(base, file_login="live")
    assert base == {"CLAUDECODE": "1", "CLAUDE_CODE_OAUTH_TOKEN": "tok"}


# ----------------------------------------------------------------- launch wiring

def _spawns(monkeypatch, which: str | None = "C:\\bin\\claude.exe",
            token: str | None = "live-token"):
    """Capture what _launch_batch hands subprocess.Popen, spawning nothing."""
    calls = []
    monkeypatch.setattr(nb.shutil, "which", lambda name: which)
    monkeypatch.setattr(nb, "_file_token", lambda: token)
    monkeypatch.setattr(nb.subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)))
    return calls


def test_launch_batch_hands_popen_the_argv_and_the_stripped_env(monkeypatch):
    """The two halves have their own tests; this covers the line that JOINS them, which
    is where both real failures lived. Swap the arguments or forget the call parens and
    every other test in this file still passes."""
    calls = _spawns(monkeypatch)
    argv = nb._launch_batch()
    assert argv is not None and len(calls) == 1 and calls[0][0] == argv
    assert argv[argv.index(r"C:\bin\claude.exe") + 1] == nb.BATCH_TRIGGER
    env = calls[0][1]["env"]
    assert env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] == "1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env      # a live file login took over


def test_launch_batch_keeps_the_env_token_when_no_file_login_is_available(monkeypatch):
    """Degrade, never park the console on a login prompt."""
    calls = _spawns(monkeypatch, token=None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-tok")
    nb._launch_batch()
    assert calls[0][1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "env-tok"


def test_launch_batch_spawns_nothing_when_claude_is_off_the_path(monkeypatch):
    calls = _spawns(monkeypatch, which=None)
    assert nb._launch_batch() is None
    assert calls == []


# --------------------------------------------------------------- popup arithmetic

def test_batch_is_uncapped_full_text_plus_snippet_quota():
    """2026-08-16 widening: the flat top-10 cap is gone. Full text scales with what is
    pending; snippets clamp to the quota (each costs a browser completion, and 10 of
    the first 12 completed pairs lost their action on full text)."""
    assert nb.batch_counts(21, 0) == (21, 0)
    assert nb.batch_counts(21, 31) == (21, nb.SNIPPET_QUOTA)
    assert nb.batch_counts(0, 2) == (0, 2)      # under-quota: take what exists, no padding
    assert nb.batch_counts(0, 0) == (0, 0)


def test_session_guard_truncates_the_batch_to_what_fits():
    """The guard is the ONLY bound left on a batch's session cost. Full text is spent
    first: snippets are the measured low-yield class, so they are what a squeezed batch
    drops."""
    # 10% of window left, 1%/full row -> 10 full rows fit, nothing left for snippets
    assert nb.batch_counts(21, 31, budget_pct=10, full_pct=1.0, snippet_pct=2.0) == (10, 0)
    # 14% left: 10 full (10%) then 2 snippets (4%)
    assert nb.batch_counts(10, 31, budget_pct=14, full_pct=1.0, snippet_pct=2.0) == (10, 2)
    # over the ceiling: nothing fits, and no negative counts from a negative budget
    assert nb.batch_counts(21, 31, budget_pct=-8, full_pct=1.0, snippet_pct=2.0) == (0, 0)


def test_guard_budget_sign_survives_a_healthy_window():
    """The one line bridging the live quota read to the guard. Inverted, it goes negative
    whenever the window is HEALTHY, so batch_counts returns nothing and the doorbell falls
    permanently silent — a failure invisible to every test that computes its own budget,
    which is why the composition (not just the arithmetic) is asserted here."""
    assert nb.guard_budget(44) == nb.SESSION_GUARD_PCT - 44
    assert nb.guard_budget(None) is None
    # healthy window -> real work proposed; exhausted window -> nothing
    assert nb.batch_counts(21, 31, nb.guard_budget(10), 0.27, 0.27)[0] > 0
    assert nb.batch_counts(21, 31, nb.guard_budget(90), 0.27, 0.27) == (0, 0)


def test_quota_tail_and_guard_holds_are_counted_separately():
    """Two different exclusions the popup must not merge: over-quota snippets are never
    batched and age out, while guard-dropped rows come back next time. Merging them
    overstated permanent loss (77 claimed vs 74 real at a 44% window)."""
    pending_snip, full = 77, 30
    in_quota = min(pending_snip, nb.SNIPPET_QUOTA)
    for now, want_held in ((10, 0), (44, None)):
        n_full, n_snip = nb.batch_counts(full, pending_snip, nb.guard_budget(now),
                                         0.27, 0.27)
        quota_tail = pending_snip - in_quota
        held = (full - n_full) + (in_quota - n_snip)
        assert quota_tail == 74          # never depends on the guard
        if want_held == 0:
            assert held == 0             # roomy window defers nothing
        else:
            assert held > 0              # squeezed window defers, and says so separately


def test_batch_fails_open_when_the_window_or_calibration_is_unknown():
    """plan_usage never lets a quota hiccup suppress the doorbell; the guard inherits
    that rule. An unreadable window or an uncalibrated cost must not read as zero."""
    assert nb.batch_counts(21, 31, budget_pct=None, full_pct=1.0) == (21, nb.SNIPPET_QUOTA)
    assert nb.batch_counts(21, 31, budget_pct=5, full_pct=0, snippet_pct=0) == (
        21, nb.SNIPPET_QUOTA)


def test_session_pct_reads_the_window_row_and_survives_a_shapeless_payload():
    """The usage endpoint is undocumented: percent may be missing or non-numeric, and
    None must mean 'unknown' (fail open), never 0 (which would read as a full window)."""
    assert nb.session_pct([("周 全模型", 19, "x"), (nb.SESSION_LABEL, 16, "y")]) == 16.0
    assert nb.session_pct([(nb.SESSION_LABEL, None, "y")]) is None
    assert nb.session_pct([(nb.SESSION_LABEL, "n/a", "y")]) is None
    assert nb.session_pct([]) is None


def test_arrivals_watermark_comes_from_the_column_it_is_compared_against(conn):
    """The state file's last_batch_iso is written by prose instruction in a gitignored
    skill file, and on 2026-08-16 it arrived as a UTC wall clock where the contract asks
    for the newest processed first_seen — 5 hours ahead of every (local) first_seen, so
    nothing could ever read as new. Deriving it from `jobs` removes the clock entirely."""
    old = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(days=3))
    new = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    assert nb.arrivals_watermark(conn, set()) == ""          # nothing batched yet
    assert nb.arrivals_watermark(conn, {old["job_url"]}) == old["first_seen"]
    assert nb.arrivals_watermark(
        conn, {old["job_url"], new["job_url"]}) == new["first_seen"]


def test_arrivals_mark_cannot_fall_back_when_processed_urls_is_pruned(conn):
    """A watermark that moves backwards is not one. The skill prunes decided rows out of
    processed_urls, so marking the newest batched row applied drops it from the set and
    the raw max falls to an older row — re-reporting rows that batch already read as
    fresh arrivals. Deciding rows right after a batch IS the workflow, so this is the
    common path, not an edge case."""
    old = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(days=3))
    new = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    batched = {old["job_url"], new["job_url"]}
    mark = nb.arrivals_mark(conn, batched)
    assert mark == new["first_seen"]

    pruned = {old["job_url"]}                    # `new` was decided, so the skill dropped it
    assert nb.arrivals_watermark(conn, pruned) == old["first_seen"]   # raw value regresses
    assert nb.arrivals_mark(conn, pruned, remembered=mark) == mark    # the mark does not


def test_arrivals_mark_degrades_on_a_malformed_remembered_value(conn):
    """It runs before any popup, so an exception here is a doorbell that never rings —
    the failure every other reader in this module is written to survive (_cal on a null
    constant, _save_doorbell_state on a non-dict `doorbell`, session_pct on a non-numeric
    percent). max() over a str and an int raises, and the key is hand-editable."""
    row = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    for junk in (None, 123, 4.5, [], ["x"], {"a": 1}, True):
        assert nb.arrivals_mark(conn, {row["job_url"]}, junk) == row["first_seen"]


def test_arrivals_mark_is_clamped_to_the_newest_row_in_the_table(conn):
    """A type check passes any high-sorting string, and monotonicity would then make it
    permanent — pinning 新进 at 0 with no way back but hand-editing the state file. The
    mark is some processed row's first_seen, so the table's newest row bounds it; that
    bound also retro-catches the UTC stamp this whole thread started from."""
    row = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    newest = conn.execute("SELECT MAX(first_seen) FROM jobs").fetchone()[0]
    for poison in ("not-a-date", "9999-12-31T00:00:00", "2099-01-01T00:00:00"):
        assert nb.arrivals_mark(conn, {row["job_url"]}, poison) == newest
    # a legitimate remembered value below the ceiling still wins over a pruned derivation
    older = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(days=3))
    assert nb.arrivals_mark(conn, {older["job_url"]}, row["first_seen"]) == row["first_seen"]


def test_arrivals_watermark_chunks_a_long_processed_list(conn):
    """processed_urls is unbounded in principle; SQLite caps host parameters per query."""
    row = make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())
    processed = {f"https://example.test/{i}" for i in range(2500)} | {row["job_url"]}
    assert nb.arrivals_watermark(conn, processed, chunk=100) == row["first_seen"]


def _counts(conn, state):
    """main()'s own derivation, call for call — the functions, not a copy of them:
    `processed` and the remembered doorbell mark feed arrivals_mark, and ITS result is
    the stamp pending_split compares against. No state key reaches pending_split
    directly (`last_batch_iso` in particular has no production reader — the skill
    still writes it, but main() derives the mark from the DB since 7a0b350)."""
    processed = set(state.get("processed_urls") or [])
    door = state.get("doorbell")
    door = door if isinstance(door, dict) else {}
    last_batch = nb.arrivals_mark(conn, processed, door.get("arrivals_mark"))
    pending, _, new_rows = nb.pending_split(nb.zone_rows(conn), processed, last_batch)
    return pending, new_rows


def test_arrivals_are_always_a_subset_of_pending(conn):
    """The popup nests them ("待看 N（其中新进 M）"), so M ⊆ N must hold — including the
    no-state case, which once printed a nested count larger than its own total."""
    make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())          # batchable, new
    make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent())          # sub-bar, new
    make_job(conn, verdict="RECRUITER_ONLY", fit_score=16, first_seen=_recent())  # RO, new
    for state in ({}, {"doorbell": {"arrivals_mark": _recent(days=1)}}):
        pending, new_rows = _counts(conn, state)
        assert set(new_rows) <= set(pending)
        assert len(new_rows) <= len(pending)


def test_pending_snippet_count_stays_inside_pending(conn):
    """main() derives the full-text count by SUBTRACTING the snippet tally from pending,
    so the tally must be gathered over the same batchable/unprocessed subset. Counting
    snippets over the wider zone instead — the easy slip, since sub-bar Adzuna rows are
    the zone's bulk — makes that subtraction negative and the popup quote negative rows
    and negative minutes. The pure batch_counts test cannot see this; only the real
    derivation over a real zone can."""
    make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent())            # full, batchable
    make_job(conn, verdict="PASS", fit_score=16, first_seen=_recent(),
             source="adzuna", description="x" * 500)                              # snippet, batchable
    for _ in range(10):     # sub-bar snippets: in the zone, never batch material
        make_job(conn, verdict="PASS", fit_score=13, first_seen=_recent(),
                 source="adzuna", description="x" * 500)
    pending, n_snippet, _ = nb.pending_split(nb.zone_rows(conn), set())
    assert n_snippet <= len(pending)
    assert (len(pending) - n_snippet, n_snippet) == (1, 1)
    assert nb.batch_counts(len(pending) - n_snippet, n_snippet) == (1, 1)


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
