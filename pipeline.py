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
  python pipeline.py applied --url X [--resume V]  # mark a posting (full URL or unique substring)
                                                #   as applied-to; --resume records the variant sent
  python pipeline.py passed  --url X            # mark a posting as reviewed-and-passed
  python pipeline.py reject  --url X --gate G   # override the model: mark a hard-fail it missed
                                                #   (--pattern P also writes a reusable rule to filters.yaml)
  python pipeline.py event   --url X --type T   # record what happened after applying (interview, offer,
                                                #   ghosted, …; --type note = bare note) [--date D] [--note N]
                                                #   --undo removes the chain's last recorded event
  python pipeline.py prune [--days 90] [--vacuum]  # clear old rejected postings' descriptions; shrink jobs.db
  # add --undo to applied / passed / reject to clear what you set

Requires the API key for the configured provider (config.yaml): DEEPSEEK_API_KEY by default,
or ANTHROPIC_API_KEY when provider is "anthropic".
"""

import argparse
import re
import sys
import traceback
from datetime import date, timedelta

# This module is the CLI/orchestrator ONLY: it imports exactly what `run` and the cmd_*
# wrappers call. Consumers (app.py, the tests, backtest_v2 / compare_models) import the real
# modules directly — do not re-export names here for them.
from core import load_config, get_db, run_log
from states import GATE_NAMES, ALL_EVENTS, VERDICT_GATE_FAIL, STATUS_SALARY_FILTERED
from chain import (
    skip_decided_reposts, skip_evaluated_reposts, resolve_posting, _fmt_decision,
    mark_posting, reject_posting, dupe_resolve, dupe_commit, dupe_unlink,
    record_event, undo_event, chain_events,
)
from fetch import fetch_new_jobs, fetch_adzuna, fetch_ats
from filters import (
    apply_salary_filter, apply_hard_filters,
    load_filters, save_filters, _pattern_matches, validate_pattern, FILTERS_PATH,
)
from evaluation import evaluate_new_jobs, requeue_error_rows
from report import generate_report


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
    # Outcome funnel over the applied roles, counted per CHAIN (canonical), not per row —
    # the cache is propagated to every member, so a per-row count would inflate reposted roles.
    outcomes = conn.execute(
        "SELECT COALESCE(outcome_status,'(no response)') s, COUNT(DISTINCT COALESCE(repost_of, job_url)) n "
        "FROM jobs WHERE app_status='applied' GROUP BY outcome_status ORDER BY n DESC"
    ).fetchall()
    if outcomes:
        print("  -- applied: outcomes (roles) --")
        for row in outcomes:
            print(f"{row['s']:>20} {row['n']:>12}")
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


def cmd_prune(conn, days, vacuum):
    """Reclaim DB space: NULL the (up to 12KB) description of rows that will never be read
    again — GATE_FAIL verdicts and salary-filtered rows older than `days`. Deliberately
    narrow, because three consumers still need old descriptions:
      * gates-passed rows (PASS / RECRUITER_ONLY) — backtest_v2 re-evaluates known postings
        from their stored text, and applied/passed history keeps its JD for reference;
      * repost_decided AND repost_evaluated rows — undoing the chain's decision (or unlinking
        the dupe) returns them to 'new' for a re-eval, which needs the text; both are part of
        the keep-list CONTRACT (their protection here is that verdict stays NULL and their
        status isn't in the pruned pair — a widened prune must keep honoring it);
      * manual rejects on never-evaluated rows (verdict NULL) — `reject --undo` re-news them.
    eval_json is kept everywhere (small, and old reports rebuild their one-liners from it).
    The freed pages only shrink the file with `--vacuum`."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "UPDATE jobs SET description=NULL "
        "WHERE substr(first_seen,1,10) < ? AND description IS NOT NULL "
        "AND app_status IS NULL AND (verdict=? OR status=?)",
        (cutoff, VERDICT_GATE_FAIL, STATUS_SALARY_FILTERED),
    )
    conn.commit()
    print(f"[prune] cleared descriptions on {cur.rowcount} rejected posting(s) "
          f"first seen before {cutoff}")
    if vacuum:
        print("[prune] VACUUM…")
        conn.execute("VACUUM")
        print("[prune] done — file compacted")


def cmd_mark(conn, url, status, resume=None):
    """CLI wrapper over chain.mark_posting: record the user's decision on a posting
    (`status` is 'applied', 'passed', or None for undo; `resume` optionally records the
    resume variant sent with an 'applied'). `url` may be a unique substring of the
    job_url. The decision propagates across the whole repost chain."""
    label = status or "undo"
    if resume and status != "applied":
        # propagate_app_status would drop it anyway (resume rides 'applied' only) — say so
        # instead of silently ignoring a flag the user typed.
        print(f"[{label}] --resume only applies with `applied` — ignored", file=sys.stderr)
        resume = None
    m, err = resolve_posting(conn, url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    _, msg, _, _ = mark_posting(conn, m, status, resume)
    print(f"[{label}] {msg}")
    return True


def cmd_event(conn, url, event_type, event_date, note, undo):
    """CLI wrapper over chain.record_event / undo_event: track what happened after applying
    (recruiter screen, interview rounds, offer, employer rejection, ghosted, withdrew — or a
    bare `--type note`). The event lands on the chain's canonical and the derived outcome
    propagates chain-wide; `--undo` deletes the chain's last recorded event. Prints the
    chain's full timeline after each mutation so the state is always visible."""
    label = "event"
    m, err = resolve_posting(conn, url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    if undo:
        ok, msg, _, _ = undo_event(conn, m)
    else:
        if not event_type:
            print(f"[{label}] --type is required — one of: {', '.join(ALL_EVENTS)}",
                  file=sys.stderr)
            return False
        ok, msg, _, _ = record_event(conn, m, event_type, event_date, note)
    print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
    if ok:
        for ev in chain_events(conn, m):
            note_part = f" — {ev['note']}" if ev["note"] else ""
            print(f"    {ev['event_date']}  {ev['event_type']}{note_part}")
    return ok


def cmd_reject(conn, url, gate, pattern, note, undo):
    """CLI wrapper over chain.reject_posting: mark a posting as a hard-fail the model missed
    (distinct from the softer `passed`). `--pattern` additionally promotes the catch into a
    deterministic rule in filters.yaml so future postings with the same requirement are
    auto-failed. `--undo` clears the override (it does not remove any rule)."""
    label = "reject"
    m, err = resolve_posting(conn, url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    ok, msg, _, _ = reject_posting(conn, m, gate, undo)
    print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
    if ok and pattern and not undo:
        _add_filter_rule(conn, gate, pattern, note, m)
    return ok


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
# dupe_resolve, dupe_commit, dupe_unlink) live in chain.py and are imported above —
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
        a, err = resolve_posting(conn, url)
        if err:
            print(f"[{label}] {err}", file=sys.stderr)
            return False
        ok, msg, _, _ = dupe_unlink(conn, a)
        print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
        return ok

    plan, err = dupe_resolve(conn, url, of_url)
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

    dupe_commit(conn, plan)
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
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed",
                                        "reject", "event", "dupe", "prune", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today); "
                                   "`event`: the date the event happened (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / `reject` / `event` / `dupe`")
    ap.add_argument("--of", help="`dupe`: job_url (or unique substring) of the other posting this duplicates")
    ap.add_argument("--yes", action="store_true", help="`dupe`: skip the confirmation prompt")
    ap.add_argument("--undo", action="store_true", help="clear the status/override/link instead of setting it")
    ap.add_argument("--gate", default="other",
                    help="hard gate a `reject` represents — one of: " + ", ".join(GATE_NAMES + ["other"]))
    ap.add_argument("--pattern", help="`reject`: promote this pattern into filters.yaml (re: prefix = regex)")
    ap.add_argument("--note", help="`reject`: optional note stored with a new filter rule; "
                                   "`event`: free text stored with the event")
    ap.add_argument("--type", dest="event_type", choices=list(ALL_EVENTS),
                    help="`event`: what happened — a lifecycle outcome, or `note` for a bare note")
    ap.add_argument("--resume", help="`applied`: resume variant sent (free text, stored on the chain)")
    ap.add_argument("--days", type=int, default=90,
                    help="`prune`: age floor in days — only rows first seen before this are touched (default 90)")
    ap.add_argument("--vacuum", action="store_true",
                    help="`prune`: also VACUUM so the freed pages shrink jobs.db on disk "
                         "(under WAL the shrink lands at checkpoint — i.e. once no other "
                         "process, e.g. the web UI, has the DB open)")
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

    # A broken config dies HERE with the collected problem list (core.validate_config) —
    # before any fetch/eval spend, and with a message instead of a KeyError traceback.
    try:
        cfg = load_config()
        # Inside the guard: get_db can raise the stale-CHECK rebuild's actionable
        # RuntimeError, which deserves the same clean exit as a config problem — in the
        # scheduled .bat log a traceback reads as a crash, not an instruction.
        conn = get_db(cfg)
    except FileNotFoundError:
        print("[config] config.yaml not found — copy config.example.yaml to config.yaml "
              "and edit it for your search", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print(f"[config] {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"[db] {e}", file=sys.stderr)
        sys.exit(2)

    if args.command == "run":
        # The `status` column is a state machine and THIS ORDER IS LOAD-BEARING: each stage gates
        # on status and only the deterministic, zero-cost filters run before the *paid* eval, so an
        # obvious reject never reaches the LLM. The transitions:
        #   fetch_new_jobs / fetch_adzuna / fetch_ats  insert rows as 'new'
        #   requeue_error_rows             last run's 'error'     -> 'new'  (retry — BEFORE the
        #                                  filters, so a requeued row re-faces the current rules
        #                                  and any chain decision made while it sat in 'error')
        #   skip passes (restore dir)      undone/unlinked 'repost_*' -> 'new'  (also BEFORE the
        #                                  filters — a restored row re-faces the current rules)
        #   apply_salary_filter            'new' below floor      -> 'salary_filtered'
        #   apply_hard_filters             'new' hits a rule      -> 'rule_filtered'
        #   skip_decided_reposts (fwd)     'new' relisting of a decided role -> 'repost_decided'
        #   skip_evaluated_reposts (fwd)   'new' relisting of an evaluated role -> 'repost_evaluated'
        #                                  (after the decided pass — a user decision is the more
        #                                  informative skip reason when both apply)
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
            requeue_error_rows(conn)
            # RESTORE direction first, BEFORE the filters: a skipped row whose chain decision
            # was undone (or whose chain verdict was cleared) returns to 'new' here so it
            # re-faces the CURRENT salary/hard rules — same re-facing contract as
            # requeue_error_rows above. Restoring after the filters would hand it straight to
            # the paid eval past a rule added while it sat skipped.
            skip_decided_reposts(conn, forward=False)
            skip_evaluated_reposts(conn, forward=False)
            apply_salary_filter(cfg, conn)
            apply_hard_filters(cfg, conn)
            # FORWARD direction after the filters: a rule keeps first claim on a 'new'
            # relisting; whatever the rules didn't take is then skip-checked before the eval.
            skip_decided_reposts(conn, restore=False)
            skip_evaluated_reposts(conn, restore=False)
            evaluate_new_jobs(cfg, conn)
            generate_report(cfg, conn, run_date)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)
    elif args.command in ("applied", "passed"):
        cmd_mark(conn, args.url, None if args.undo else args.command, args.resume)
    elif args.command == "reject":
        cmd_reject(conn, args.url, args.gate, args.pattern, args.note, args.undo)
    elif args.command == "event":
        cmd_event(conn, args.url, args.event_type, args.date, args.note, args.undo)
    elif args.command == "dupe":
        cmd_dupe(conn, args.url, args.of, args.undo, args.yes)
    elif args.command == "prune":
        cmd_prune(conn, args.days, args.vacuum)


if __name__ == "__main__":
    main()
