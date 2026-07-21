"""The fetch-stage guard: one source's crash must not abort the run.

`_run_fetch_stage` wraps each fetcher so an unexpected exception is logged + rolled back and
the run continues to the remaining fetchers and the downstream stages. These tests exercise
that contract against the real schema (the `conn` fixture), never the network or real jobs.db.
"""

import contextlib
import sys

import pytest

import pipeline

_CFG = {"settings": {}}  # the fake fetchers below ignore cfg


def _insert_uncommitted(conn, url):
    """Insert one status='new' row WITHOUT committing — mimics a fetcher's in-flight work."""
    conn.execute(
        "INSERT OR IGNORE INTO jobs (job_url, title, status) VALUES (?, 'T', 'new')", (url,)
    )


def _count(conn):
    return conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]


def test_crash_is_caught_rolled_back_and_logged(conn, capsys):
    def boom(cfg, c):
        _insert_uncommitted(c, "u1")  # partial work, uncommitted
        raise RuntimeError("network exploded")

    # None = crashed (distinct from a fetcher's own 0 = ran fine, nothing new) — the
    # cooldown stamp keys on this distinction, so pin the exact sentinel.
    assert pipeline._run_fetch_stage(boom, _CFG, conn, "linkedin") is None
    assert _count(conn) == 0  # the uncommitted row was rolled back
    err = capsys.readouterr().err
    assert "linkedin fetch FAILED" in err
    assert "RuntimeError: network exploded" in err  # full traceback, not just the class name


def test_success_passes_through_return_value(conn):
    def ok(cfg, c):
        _insert_uncommitted(c, "u1")
        c.commit()
        return 1

    assert pipeline._run_fetch_stage(ok, _CFG, conn, "adzuna") == 1
    assert _count(conn) == 1


def test_keyboardinterrupt_is_not_swallowed(conn):
    def interrupted(cfg, c):
        raise KeyboardInterrupt

    # Ctrl-C must abort the run, not be caught and turned into a skipped source.
    with pytest.raises(KeyboardInterrupt):
        pipeline._run_fetch_stage(interrupted, _CFG, conn, "ats")


def test_one_source_crash_does_not_lose_the_others(conn, capsys):
    # The whole point: with the middle fetcher crashing, the first and third fetchers'
    # committed rows both survive — a source outage costs only that source's rows.
    def ok_first(cfg, c):
        _insert_uncommitted(c, "first")
        c.commit()
        return 1

    def crash(cfg, c):
        _insert_uncommitted(c, "middle")  # uncommitted → must be rolled back
        raise RuntimeError("boom")

    def ok_third(cfg, c):
        _insert_uncommitted(c, "third")
        c.commit()
        return 1

    for fn, label in [(ok_first, "linkedin"), (crash, "adzuna"), (ok_third, "ats")]:
        pipeline._run_fetch_stage(fn, _CFG, conn, label)

    rows = conn.execute("SELECT job_url, status FROM jobs").fetchall()
    assert {r["job_url"] for r in rows} == {"first", "third"}  # crashed source left nothing behind
    # Survivors must remain 'new' — the state the downstream salary/hard/eval stages gate on;
    # a rollback that touched committed siblings' status (rather than deleting the crashed
    # source's rows) would silently exclude them from the eval.
    assert {r["status"] for r in rows} == {"new"}
    assert "adzuna fetch FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("crasher", ["linkedin", "adzuna", "ats"])
def test_run_command_guards_each_fetcher_independently(conn, monkeypatch, capsys, crasher):
    # Integration: the real `run` block must wrap EACH of the three fetchers, so ANY single
    # source crashing still lets the run reach every downstream stage. Parametrizing over WHICH
    # fetcher crashes is what makes this catch an unwrapped fetcher regardless of which one it
    # is — a test that only ever crashed `adzuna` would stay green even if linkedin/ats were
    # left bare, because they don't crash and the call order is unchanged. Each case also asserts
    # the FAILED log names the right source (catches a wrong label at the call site) and that the
    # crashed fetcher's uncommitted rows were rolled back (catches a lost rollback in the wiring).
    calls = []

    def make(label):
        def fetcher(cfg, c):
            calls.append(label)
            if label == crasher:
                c.execute("INSERT OR IGNORE INTO jobs (job_url, title, status) "
                          "VALUES ('partial', 'P', 'new')")  # uncommitted → must be rolled back
                raise RuntimeError(f"{label} outage")
        return fetcher

    monkeypatch.setattr(pipeline, "load_config", lambda: {"settings": {}, "searches": []})
    monkeypatch.setattr(pipeline, "get_db", lambda cfg: conn)
    monkeypatch.setattr(pipeline, "run_log", lambda label="run": contextlib.nullcontext())
    monkeypatch.setattr(pipeline, "fetch_new_jobs", make("linkedin"))
    monkeypatch.setattr(pipeline, "fetch_adzuna", make("adzuna"))
    monkeypatch.setattr(pipeline, "fetch_ats", make("ats"))
    monkeypatch.setattr(pipeline, "apply_salary_filter", lambda c, cn: calls.append("salary"))
    monkeypatch.setattr(pipeline, "apply_hard_filters", lambda c, cn: calls.append("hard"))
    def _skip_stub(name):
        def stub(cn, forward=True, restore=True):
            # Label each direction so the order assertion pins the restore-before-filters /
            # forward-after-filters split, not just "the passes ran". Fails loud (KeyError)
            # on a direction combination the run order doesn't use.
            suffix = {(True, True): "", (True, False): ":fwd", (False, True): ":restore"}[
                (forward, restore)]
            calls.append(name + suffix)
        return stub
    monkeypatch.setattr(pipeline, "skip_decided_reposts", _skip_stub("skip"))
    monkeypatch.setattr(pipeline, "skip_evaluated_reposts", _skip_stub("skip_eval"))
    monkeypatch.setattr(pipeline, "evaluate_new_jobs", lambda c, cn: calls.append("eval"))
    monkeypatch.setattr(pipeline, "generate_report", lambda c, cn, d: calls.append("report"))
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "run"])

    pipeline.main()

    # Every fetcher attempted (the crasher caught), every downstream stage ran, in order.
    assert calls == ["linkedin", "adzuna", "ats",
                     "skip:restore", "skip_eval:restore",   # restores BEFORE the filters
                     "salary", "hard",
                     "skip:fwd", "skip_eval:fwd",           # forward skips after them
                     "eval", "report"]
    # The FAILED log names the source that actually crashed — a wrong `label` at the call site
    # (e.g. passing "adzuna" for fetch_ats) would break this for the mislabeled source.
    assert f"[run] {crasher} fetch FAILED" in capsys.readouterr().err
    # The crashed fetcher's uncommitted partial row was rolled back through the real wiring.
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
