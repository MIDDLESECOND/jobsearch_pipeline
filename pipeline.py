#!/usr/bin/env python3
"""
LinkedIn job search pipeline.

  fetch -> dedupe (SQLite) -> salary filter -> hard-requirement filters -> LLM gate
  evaluation (Claude or DeepSeek) -> daily markdown report

Usage:
  python pipeline.py run                       # full cycle: fetch + filter + evaluate + report
  python pipeline.py report                     # regenerate today's report only (no fetch, no API calls)
  python pipeline.py stats                      # quick database stats
  python pipeline.py ui                         # local web UI to triage postings (applied/passed/reject)
  python pipeline.py applied --url X            # mark a posting (full URL or unique substring) as applied-to
  python pipeline.py passed  --url X            # mark a posting as reviewed-and-passed
  python pipeline.py reject  --url X --gate G   # override the model: mark a hard-fail it missed
                                                #   (--pattern P also writes a reusable rule to filters.yaml)
  # add --undo to applied / passed / reject to clear what you set

Requires the API key for the configured provider (config.yaml): DEEPSEEK_API_KEY by default,
or ANTHROPIC_API_KEY when provider is "anthropic".
"""

import argparse
import re
import sys
import traceback
from datetime import date

# The foundation (paths, cross-cutting constants, config, the DB open/schema/migration path, the
# API-key resolver) lives in core.py; the repost/content-dedup + decision-chain core in chain.py.
# Their public names are re-imported into this module's namespace so the pipeline-stage code here —
# and every `pipeline.X` reference from app.py / the tests / the validation scripts — keeps working.
from core import (  # noqa: E402,F401
    BASE_DIR, CONFIG_PATH, PROFILE_PATH, GUIDE_PATH,
    GATE_NAMES, SCORE_DIMS, VERDICTS,
    load_config, get_db, _ensure_api_key, run_log,
)
from chain import (  # noqa: E402,F401
    _clean, _norm_company, _norm_title, _norm_location, _fingerprint, _NORM_VERSION,
    _find_repost, skip_decided_reposts, _resolve_posting, _chain_targets, _chain_members,
    _chain_decision, _decision_sig, _fmt_decision, effective_decision, effective_decisions,
    propagate_app_status, propagate_reject, clear_reject,
    _dupe_resolve, _dupe_commit, _dupe_unlink,
)


# load_config / get_db + the schema and all migrations moved to core.py (re-imported above).


# The three sources (fetch_new_jobs = LinkedIn scrape, fetch_adzuna = Adzuna API, fetch_ats =
# Greenhouse/Lever/Ashby ATS boards) live in fetch.py; the entry points are re-imported below
# for `run`.
from fetch import fetch_new_jobs, fetch_adzuna, fetch_ats  # noqa: E402,F401


# Repost / content dedup (normalization, fingerprint, _find_repost) moved to chain.py;
# imported at the top of this module so call sites here read unchanged.


# The deterministic pre-eval filters (apply_salary_filter, and the hard-requirement rules:
# load_filters / save_filters / _pattern_matches / _rule_hit / apply_hard_filters + FILTERS_PATH)
# moved to filters.py; re-imported below (the `reject` rule-writer below still reuses them).
from filters import (  # noqa: E402,F401
    apply_salary_filter, apply_hard_filters,
    load_filters, save_filters, _pattern_matches, _rule_hit, validate_pattern, FILTERS_PATH,
)


# skip_decided_reposts (the deterministic pre-eval repost-skip pass) moved to chain.py.


# The LLM gate-check evaluation (system prompt, provider calls, the deterministic 50/0
# routing in normalize_result, and the eval loop) moved to evaluation.py; re-imported for
# `run` and for the validation scripts (backtest_v2 / compare_models) that read these names.
from evaluation import (  # noqa: E402,F401
    SYSTEM_TEMPLATE, MODEL_PRICES, build_system_prompt, parse_eval_json, normalize_result,
    _call_anthropic, _call_deepseek, evaluate_new_jobs,
)


# The daily markdown report (generate_report + the renderers: _repost_info, _repost_tag,
# _source_tag, score_band, _render_scored_job, _fmt_sal, BUCKET_LABELS) moved to report.py;
# re-imported for `run`/`report` and so app.py's pipeline.score_band / pipeline.BUCKET_LABELS resolve.
# posting_age / recency_sort_key / APPLY_LINE: the recency triage helpers, shared with app.py the
# same way so the UI's age labels and two-band ordering can't drift from the report's.
from report import (  # noqa: E402,F401
    generate_report, score_band, BUCKET_LABELS, posting_age, recency_sort_key, APPLY_LINE,
)


# ----------------------------------------------------------------------- main

def cmd_stats(conn):
    for row in conn.execute(
        "SELECT status, verdict, COUNT(*) n FROM jobs GROUP BY status, verdict ORDER BY n DESC"
    ):
        print(f"{row['status']:>16} {str(row['verdict']):>10} {row['n']:>5}")
    print("  -- application status --")
    for row in conn.execute(
        "SELECT COALESCE(app_status,'(backlog)') s, COUNT(*) n FROM jobs GROUP BY app_status ORDER BY n DESC"
    ):
        print(f"{row['s']:>16} {row['n']:>16}")
    fsrc = conn.execute(
        "SELECT COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL"
    ).fetchone()["n"]
    if fsrc:
        print("  -- hard-fail overrides --")
        for row in conn.execute(
            "SELECT filter_source s, COUNT(*) n FROM jobs WHERE filter_source IS NOT NULL "
            "GROUP BY filter_source ORDER BY n DESC"
        ):
            print(f"{row['s']:>16} {row['n']:>16}")


# _resolve_posting (url → row) and _chain_targets (decision propagation set) moved to chain.py.


def cmd_mark(conn, url, status):
    """Record the user's decision on a posting: `status` is 'applied', 'passed', or None
    (undo). `url` may be a unique substring of the job_url. The decision propagates to the
    canonical original of a repost chain, so the whole group is covered."""
    label = status or "undo"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    today = date.today().isoformat()
    stamp = today if status else None
    propagate_app_status(conn, _chain_targets(conn, m), status, stamp)
    conn.commit()
    verb = f"marked {status}" if status else "cleared status"
    print(f"[{label}] {verb}: {m['title']} — {m['company']}" + (f" ({today})" if status else ""))
    return True


def cmd_reject(conn, url, gate, pattern, note, undo):
    """Manually correct the model: mark a posting as a hard-fail it missed (distinct from the
    softer `passed`). `--pattern` additionally promotes the catch into a deterministic rule in
    filters.yaml so future postings with the same requirement are auto-failed. `--undo` clears
    the override (it does not remove any rule)."""
    label = "reject"
    m = _resolve_posting(conn, url, label)
    if m is None:
        return False
    if gate not in GATE_NAMES + ["other"]:
        print(f"[{label}] --gate must be one of {GATE_NAMES + ['other']}", file=sys.stderr)
        return False

    today = date.today().isoformat()
    targets = _chain_targets(conn, m)
    if undo:
        clear_reject(conn, targets)
        conn.commit()
        print(f"[{label}] cleared override: {m['title']} — {m['company']}")
        return True

    # The explicitly named row is always (re)stamped — you're overruling it, possibly re-attributing
    # a row a filters.yaml rule auto-failed; siblings are stamped too but never clobber a rule:<name>.
    propagate_reject(conn, targets, gate, today, force_url=m["job_url"], overwrite_manual=True)
    conn.commit()
    print(f"[{label}] rejected (gate: {gate}): {m['title']} — {m['company']} ({today})")

    if pattern:
        _add_filter_rule(conn, gate, pattern, note, m)
    return True


def _add_filter_rule(conn, gate, pattern, note, posting):
    """Promote a pattern into filters.yaml under the rule named for `gate`. Shows the matching
    sentence from this posting and how many existing postings the pattern would also catch
    (false-positive preview) before saving. De-dupes identical patterns."""
    # Validate before persisting: a broken/empty `re:` written to filters.yaml would fail
    # silently in _pattern_matches forever (matching nothing, or everything) — the same check
    # the ATS config sanitizer applies, so the one dialect can't drift between the two writers.
    reason = validate_pattern(pattern)
    if reason:
        print(f"[reject] refusing to add pattern {pattern!r} — {reason}", file=sys.stderr)
        return
    # False-positive preview: how many existing postings would this pattern also match?
    rows = conn.execute("SELECT title, description FROM jobs").fetchall()
    hits = sum(1 for r in rows if _pattern_matches(pattern, f"{r['title'] or ''}\n{r['description'] or ''}"))
    # Show the sentence in THIS posting that the pattern matches, to sanity-check the phrase.
    desc = posting["description"] or ""
    snippet = next(
        (s.strip() for s in re.split(r"(?<=[.!?\n])\s+", desc) if _pattern_matches(pattern, s)),
        None,
    )
    print(f"[reject] pattern {pattern!r} → would match {hits} existing posting(s) in the DB")
    if snippet:
        print(f"[reject] matched here: …{snippet[:200]}…")

    rules = load_filters()
    # Match on `gate` (not `name`) so a hand-edited rule whose name differs from its gate
    # is still extended rather than duplicated.
    rule = next((r for r in rules if r.get("gate") == gate), None)
    if rule is None:
        rule = {"name": gate, "gate": gate, "note": note or "", "any": []}
        rules.append(rule)
    elif note and not rule.get("note"):
        rule["note"] = note
    if pattern in (rule.get("any") or []):
        print(f"[reject] pattern already in rule '{gate}' — nothing to add")
        return
    rule.setdefault("any", []).append(pattern)
    save_filters(rules)
    print(f"[reject] added pattern to rule '{gate}' in {FILTERS_PATH.name} "
          f"({len(rule['any'])} pattern(s) now)")


# ------------------------------------------------------- manual repost linking
#
# The dupe cores (_chain_members, _chain_decision, _decision_sig, _fmt_decision,
# _dupe_resolve, _dupe_commit, _dupe_unlink) live in chain.py and are imported above —
# `dupe` is the manual escape hatch for a relisting `_find_repost` missed (drifted
# title/location, or the same role cross-posted to Adzuna vs LinkedIn). The CLI wrapper
# below and the web UI (app.api_dupe) share those cores so the guard logic lives in one place.

def cmd_dupe(conn, url, of_url, undo, assume_yes):
    """CLI wrapper over the shared dupe cores. Manually link two existing postings as the same role
    (a duplicate `_find_repost` missed): earliest-`first_seen` becomes canonical, the other side is
    repointed under it, and any existing decision propagates across the unified chain. `--undo`
    splits a manual link apart. Previews the merge and confirms (skippable with `assume_yes`)."""
    label = "dupe"
    if undo:
        a = _resolve_posting(conn, url, label)
        if a is None:
            return False
        ok, msg, _ = _dupe_unlink(conn, a)
        print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
        return ok

    plan, err = _dupe_resolve(conn, url, of_url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    winner, loser, dec = plan["winner"], plan["loser"], plan["dec"]

    # Preview + confirm: a wrong merge buries a real job under another role's decision.
    print(f"[{label}] link as the SAME role:")
    print(f"    canonical (kept) : {winner['title']} — {winner['company']} ({winner['first_seen']})")
    print(f"    relisting (merge): {loser['title']} — {loser['company']} ({loser['first_seen']})")
    if len(plan["loser_members"]) > 1:
        print(f"    + {len(plan['loser_members']) - 1} relisting(s) already under the merged side")
    if dec:
        print(f"    decision propagated to the whole chain: {_fmt_decision(dec)}")
    if not assume_yes and not _confirm(f"[{label}] proceed?"):
        print(f"[{label}] aborted", file=sys.stderr)
        return False

    _dupe_commit(conn, plan)
    print(f"[{label}] linked: {loser['title']} — {loser['company']} → canonical {winner['job_url']}")
    return True


def _confirm(prompt):
    """Yes/no prompt; treats a closed stdin (non-interactive) or Ctrl-C as 'no' to fail safe."""
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()  # finish the prompt line so the caller's abort message isn't appended to it
        return False


def _run_fetch_stage(fn, cfg, conn, label):
    """Run one fetcher (fetch_new_jobs / fetch_adzuna / fetch_ats) as an independent failure
    unit: an unexpected crash is logged with its traceback and the fetcher's uncommitted
    partial work rolled back, then the run continues. So a single source's outage — a LinkedIn
    guest-endpoint change, an Adzuna/board envelope shift — doesn't abort the run before the
    filters, eval, and report get to work on the sources that DID succeed.

    Each fetcher commits its own rows internally (per search / query / board), so this rollback
    only discards the in-flight fetcher's uncommitted tail; earlier sources' committed rows
    persist (the connection is in deferred-transaction mode, not autocommit). Note rollback()
    discards the ENTIRE open transaction, so this per-source independence RELIES on each fetcher
    committing its own work before it returns — a future fetcher that defers its commit across
    sources would have that uncommitted work silently discarded by a later source's crash.
    Catches Exception, NOT BaseException, so Ctrl-C / SystemExit still abort the run. run_log
    tees stderr into the day's logs/pipeline-YYYY-MM-DD.log, so the message and traceback are
    captured there.

    This resilience wraps the FETCHERS only — the untrusted-input boundary. The deterministic
    downstream stages (salary/hard filters, eval, report) stay bare: they must fail loud, since
    limping past a crashed filter would let un-filtered rows reach the *paid* eval."""
    try:
        return fn(cfg, conn)
    except Exception:
        conn.rollback()
        print(f"[run] {label} fetch FAILED — skipping this source for this run:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 0


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed", "reject", "dupe", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / `reject` / `dupe`")
    ap.add_argument("--of", help="`dupe`: job_url (or unique substring) of the other posting this duplicates")
    ap.add_argument("--yes", action="store_true", help="`dupe`: skip the confirmation prompt")
    ap.add_argument("--undo", action="store_true", help="clear the status/override/link instead of setting it")
    ap.add_argument("--gate", default="other",
                    help="hard gate a `reject` represents — one of: " + ", ".join(GATE_NAMES + ["other"]))
    ap.add_argument("--pattern", help="`reject`: promote this pattern into filters.yaml (re: prefix = regex)")
    ap.add_argument("--note", help="`reject`: optional note stored with a new filter rule")
    args = ap.parse_args()

    # Validate --date at the CLI edge, BEFORE any fetch/eval money is spent: the report's
    # age-label anchor parses it strictly, so a typo'd date must die here with a usable
    # message, not as a fromisoformat traceback after the paid eval.
    if args.date:
        try:
            args.date = date.fromisoformat(args.date).isoformat()
        except ValueError:
            ap.error(f"--date must be YYYY-MM-DD (got {args.date!r})")

    if args.command == "ui":
        # Lazy import so the core pipeline runs without Flask installed.
        try:
            import app
        except ImportError:
            print("[ui] Flask is required — run: pip install -r requirements.txt", file=sys.stderr)
            return
        app.serve()
        return

    cfg = load_config()
    conn = get_db(cfg)

    if args.command == "run":
        # The `status` column is a state machine and THIS ORDER IS LOAD-BEARING: each stage gates
        # on status and only the deterministic, zero-cost filters run before the *paid* eval, so an
        # obvious reject never reaches the LLM. The transitions:
        #   fetch_new_jobs / fetch_adzuna / fetch_ats  insert rows as 'new'
        #   apply_salary_filter            'new' below floor      -> 'salary_filtered'
        #   apply_hard_filters             'new' hits a rule      -> 'rule_filtered'
        #   skip_decided_reposts           'new' relisting of a decided role -> 'repost_decided'
        #   evaluate_new_jobs              remaining 'new'        -> 'evaluated' | 'needs_manual' | 'error'
        # A new pre-eval filter must mirror this: set a non-'new' status so evaluate_new_jobs skips it.
        # run_log tees this whole cycle into the day's logs/pipeline-YYYY-MM-DD.log so a manual
        # terminal run is captured like a scheduled one (the .bat no longer redirects — that
        # would double-log).
        #
        # Each fetcher is guarded independently (_run_fetch_stage): one source's crash is logged
        # and rolled back, and the run still reaches the filters/eval/report for the sources that
        # succeeded. The deterministic stages below stay UNGUARDED on purpose — they must fail
        # loud, since continuing past a crashed filter would let un-filtered rows hit the paid eval.
        with run_log("run"):
            # The report is keyed to the date the run STARTED, not the date it finishes:
            # a run launched 23:xx that drags past midnight (throttled fetch, big eval batch)
            # stamps its rows with yesterday's first_seen — keying the report to "today at
            # report time" would file it under the new day and those rows would appear in NO
            # report at all. This is a code invariant, deliberately not a scheduling
            # constraint (any run can cross midnight if delayed).
            run_date = args.date or date.today().isoformat()
            _run_fetch_stage(fetch_new_jobs, cfg, conn, "linkedin")
            _run_fetch_stage(fetch_adzuna, cfg, conn, "adzuna")
            _run_fetch_stage(fetch_ats, cfg, conn, "ats")
            apply_salary_filter(cfg, conn)
            apply_hard_filters(cfg, conn)
            skip_decided_reposts(conn)
            evaluate_new_jobs(cfg, conn)
            generate_report(cfg, conn, run_date)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)
    elif args.command in ("applied", "passed"):
        cmd_mark(conn, args.url, None if args.undo else args.command)
    elif args.command == "reject":
        cmd_reject(conn, args.url, args.gate, args.pattern, args.note, args.undo)
    elif args.command == "dupe":
        cmd_dupe(conn, args.url, args.of, args.undo, args.yes)


if __name__ == "__main__":
    main()
