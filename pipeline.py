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
  python pipeline.py applied --url X [--resume V] [--channel C]  # mark a posting (full URL or unique
                                                #   substring) as applied-to; --resume records the variant
                                                #   sent, --channel how it went out (direct|agency|referral)
  python pipeline.py passed  --url X            # mark a posting as reviewed-and-passed
  python pipeline.py expired --url X            # posting is dead/expired: chain-wide passed + a fixed
                                                #   note event, so relistings auto-skip (refused on an
                                                #   applied chain — record an outcome event instead)
  python pipeline.py reject  --url X --gate G   # override the model: mark a hard-fail it missed
                                                #   (--pattern P also writes a reusable rule to filters.yaml)
  python pipeline.py event   --url X --type T   # record what happened after applying (interview, offer,
                                                #   ghosted, …; --type note = bare note) [--date D] [--note N]
                                                #   --undo removes the chain's last recorded event
  python pipeline.py prune [--days 90] [--vacuum]  # clear old rejected postings' descriptions; shrink jobs.db
  python pipeline.py backup [--output PATH]        # verified jobs.db + application_materials ZIP
  python pipeline.py backup --verify PATH          # validate an existing backup without restoring it
  # add --undo to applied / passed / reject to clear what you set

Requires the API key for the configured provider (config.yaml): DEEPSEEK_API_KEY by default,
or ANTHROPIC_API_KEY when provider is "anthropic".
"""

import argparse
import re
import sys
import traceback
from datetime import date, datetime, timedelta

# This module is the CLI/orchestrator ONLY: it imports exactly what `run` and the cmd_*
# wrappers call. Consumers (app.py, the tests, backtest_v2 / compare_models) import the real
# modules directly — do not re-export names here for them.
from core import BASE_DIR, load_config, get_db, run_log, meta_get, meta_set
from backup import BackupError, create_backup, verify_backup
from states import (GATE_NAMES_WITH_OTHER, ALL_EVENTS, ALL_CHANNELS, VERDICT_GATE_FAIL,
                    STATUS_SALARY_FILTERED)
from chain import (
    skip_decided_reposts, skip_evaluated_reposts, resolve_posting, _fmt_decision,
    mark_posting, mark_expired, reject_posting, dupe_resolve, dupe_commit, dupe_unlink,
    record_event, undo_event, chain_events,
)
from fetch import fetch_new_jobs, fetch_adzuna, fetch_ats
from health import (
    FetchSummary, completed_run_status, current_pipeline_run_id, fetch_error_kind,
    finish_pipeline_run, record_active_fetch_attempt, reset_active_pipeline_run,
    run_has_successful_target, set_active_pipeline_run, source_attempts_exist,
    start_pipeline_run,
)
from filters import (
    apply_salary_filter, apply_hard_filters,
    load_filters, save_filters, _pattern_matches, validate_pattern, FILTERS_PATH,
)
from evaluation import evaluate_new_jobs, requeue_error_rows
from report import generate_report
from materials import snapshot_jd


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


def cmd_backup(conn, output=None):
    """Create and independently verify a non-overwriting evidence-unit archive."""
    destination = output or (
        BASE_DIR / "backups" /
        f"jobsearch-evidence-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    )
    summary = create_backup(conn, destination)
    print(
        f"[backup] verified {destination} — {summary['jobs']} job(s), "
        f"{summary['material_objects']} material object(s)"
    )
    return destination, summary


def cmd_mark(conn, url, status, resume=None, channel=None):
    """CLI wrapper over chain.mark_posting: record the user's decision on a posting
    (`status` is 'applied', 'passed', or None for undo; `resume` and `channel` optionally
    record the resume variant sent / the application channel with an 'applied'). `url` may
    be a unique substring of the job_url. The decision propagates across the whole repost
    chain."""
    label = status or "undo"
    for flag, val in (("--resume", resume), ("--channel", channel)):
        if val and status != "applied":
            # propagate_app_status would drop it anyway (these ride 'applied' only) — say so
            # instead of silently ignoring a flag the user typed.
            print(f"[{label}] {flag} only applies with `applied` — ignored", file=sys.stderr)
    if status != "applied":
        resume = channel = None
    m, err = resolve_posting(conn, url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    ok, msg, _, _ = mark_posting(conn, m, status, resume, channel)
    print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
    if ok and status == "applied":
        try:
            snapshot_jd(conn, m)
        except Exception as exc:
            # mark_posting has already committed the user's decision.  Do not report the
            # command as failed; make the missing evidence explicit so it can be retried.
            print(f"[{label}] warning: JD snapshot failed: {exc}", file=sys.stderr)
    return ok


def cmd_expired(conn, url, undo):
    """CLI wrapper over chain.mark_expired: the posting is dead/expired (delisted, req
    closed) — a triage disposition, not a fit judgment. Marks the chain passed and records
    the fixed expired note; `--undo` removes both. Refused on applied chains (record an
    outcome event instead)."""
    label = "expired"
    m, err = resolve_posting(conn, url)
    if err:
        print(f"[{label}] {err}", file=sys.stderr)
        return False
    ok, msg, _, _ = mark_expired(conn, m, undo)
    print(f"[{label}] {msg}", file=sys.stdout if ok else sys.stderr)
    return ok


def cmd_event(conn, url, event_type, event_date, note, undo):
    """CLI wrapper over chain.record_event / undo_event: track what happened after applying
    (follow-up sent, recruiter screen, interview rounds, offer, employer rejection, ghosted,
    withdrew — or a bare `--type note`). The event lands on the chain's canonical; outcome
    events propagate the derived cache, while follow-up sent advances cadence only. `--undo`
    deletes the chain's last recorded event and the full timeline is printed."""
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
    assert plan is not None  # dupe_resolve returns plan when err is None
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
        result = fn(cfg, conn)
        run_id = current_pipeline_run_id()
        if run_id is not None and not source_attempts_exist(conn, run_id, label):
            # Built-in fetchers record their configured targets transactionally. This family
            # fallback keeps orchestration tests and future adapters observable until they do.
            if isinstance(result, FetchSummary) and result.status == "skipped":
                record_active_fetch_attempt(
                    conn, source_family=label, target_kind="family", target_label=label,
                    definition_hash=None, status="skipped",
                    skip_reason=result.skipped_reason,
                )
            elif isinstance(result, FetchSummary) and result.status == "failed":
                record_active_fetch_attempt(
                    conn, source_family=label, target_kind="family", target_label=label,
                    definition_hash=None, status="failed", error_kind="unexpected",
                )
            else:
                inserted = (int(result) if isinstance(result, int)
                            and not isinstance(result, bool) and result >= 0 else None)
                record_active_fetch_attempt(
                    conn, source_family=label, target_kind="family", target_label=label,
                    definition_hash=None, status="success", inserted_count=inserted,
                )
                if isinstance(result, FetchSummary) and result.status == "partial":
                    record_active_fetch_attempt(
                        conn, source_family=label, target_kind="family",
                        target_label=f"{label} unclassified failure", definition_hash=None,
                        status="failed", error_kind="unexpected",
                    )
        return result
    except Exception as exc:
        conn.rollback()
        if current_pipeline_run_id() is not None:
            record_active_fetch_attempt(
                conn, source_family=label, target_kind="family", target_label=label,
                definition_hash=None, status="failed", error_kind=fetch_error_kind(exc),
            )
        print(f"[run] {label} fetch FAILED — skipping this source for this run:", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        # Preserve the established None-on-family-crash contract for direct callers. The run
        # branch now keys cooldown on structured target-level success instead, so internal
        # all-query failures cannot masquerade as a healthy integer 0.
        return None


# Scheduled-run cooldown: a slot fired < this many minutes after the last SUCCESSFUL run's
# end becomes a no-op, so a manual catch-up run (or a missed trigger fired late on wake)
# shortly before a slot doesn't buy a near-duplicate fetch+eval — the savings are ~$0.10-0.19
# of eval and, more importantly, a whole LinkedIn scrape cycle of rate-limit exposure.
# Applies ONLY to `run --scheduled` (what run_pipeline.bat passes); a bare `run` is an
# explicit human command and always executes. Hardcoded, not config: one consumer, and a
# knob would need config.example + validation for a number nobody has wanted to tune yet.
COOLDOWN_MINUTES = 60


def _cooldown_active(last_ok_iso, now, minutes=COOLDOWN_MINUTES):
    """True when last_ok_iso (meta 'last_run_ok_ended') is within `minutes` of `now`.

    Fails OPEN on missing/garbage stamps and on stamps in the future (clock change):
    corrupt state must never suppress runs — the failure cost is one redundant run,
    the inverse is a pipeline that silently stops running. TypeError is in the net
    because two garbage shapes raise it, not ValueError: a non-str value (BLOB-typed
    meta row) inside fromisoformat, and — for an offset-aware stamp, which fromisoformat
    parses happily — the naive-minus-aware subtraction below. Aware stamps are
    normalized to local-naive instead of treated as garbage: they're a valid spelling
    of a real instant (e.g. a hand-restored '...+00:00' row)."""
    if not last_ok_iso:
        return False
    try:
        last = datetime.fromisoformat(last_ok_iso)
        if last.tzinfo is not None:
            last = last.astimezone().replace(tzinfo=None)
        return timedelta(0) <= now - last < timedelta(minutes=minutes)
    except (ValueError, TypeError):
        return False


def main():
    ap = argparse.ArgumentParser(description="LinkedIn job search pipeline")
    ap.add_argument("command", choices=["run", "report", "stats", "applied", "passed",
                                        "expired", "reject", "event", "dupe", "prune",
                                        "backup", "ui"])
    ap.add_argument("--date", help="report date YYYY-MM-DD (default today); "
                                   "`event`: the date the event happened (default today)")
    ap.add_argument("--url", help="job_url (or unique substring) for `applied` / `passed` / "
                                  "`expired` / `reject` / `event` / `dupe`")
    ap.add_argument("--of", help="`dupe`: job_url (or unique substring) of the other posting this duplicates")
    ap.add_argument("--yes", action="store_true", help="`dupe`: skip the confirmation prompt")
    ap.add_argument("--undo", action="store_true", help="clear the status/override/link instead of setting it")
    ap.add_argument("--gate", default="other",
                    help="hard gate a `reject` represents — one of: " + ", ".join(GATE_NAMES_WITH_OTHER))
    ap.add_argument("--pattern", help="`reject`: promote this pattern into filters.yaml (re: prefix = regex)")
    ap.add_argument("--note", help="`reject`: optional note stored with a new filter rule; "
                                   "`event`: free text stored with the event")
    ap.add_argument("--type", dest="event_type", choices=list(ALL_EVENTS),
                    help="`event`: what happened — a lifecycle outcome, or `note` for a bare note")
    ap.add_argument("--resume", help="`applied`: resume variant sent (free text, stored on the chain)")
    ap.add_argument("--channel", choices=list(ALL_CHANNELS),
                    help="`applied`: how the application went out (stored on the chain)")
    ap.add_argument("--days", type=int, default=90,
                    help="`prune`: age floor in days — only rows first seen before this are touched (default 90)")
    ap.add_argument("--vacuum", action="store_true",
                    help="`prune`: also VACUUM so the freed pages shrink jobs.db on disk "
                         "(under WAL the shrink lands at checkpoint — i.e. once no other "
                         "process, e.g. the web UI, has the DB open)")
    ap.add_argument("--output", help="`backup`: destination ZIP (default: backups/ with timestamp)")
    ap.add_argument("--verify", dest="verify_backup_path",
                    help="`backup`: verify this archive without restoring or opening config.yaml")
    ap.add_argument("--scheduled", action="store_true",
                    help=f"`run`: invoked by the scheduler — skip (no-op) if the last successful "
                         f"run ended < {COOLDOWN_MINUTES} min ago; a bare `run` always executes")
    args = ap.parse_args()

    if args.command != "backup" and (args.output or args.verify_backup_path):
        ap.error("--output/--verify are only valid with `backup`")
    if args.command == "backup" and args.output and args.verify_backup_path:
        ap.error("`backup` accepts either --output or --verify, not both")

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

    if args.command == "backup" and args.verify_backup_path:
        try:
            summary = verify_backup(args.verify_backup_path)
        except (BackupError, OSError) as exc:
            print(f"[backup] verification failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        print(
            f"[backup] verified {args.verify_backup_path} — {summary['jobs']} job(s), "
            f"{summary['material_objects']} material object(s)"
        )
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
            run_date = args.date or date.today().isoformat()
            trigger = "scheduled" if args.scheduled else "manual"
            # Cooldown guard, scheduled runs only — INSIDE run_log so a skipped slot is
            # visible in the day's log (a silent non-run reads as a crash). A skip does
            # NOT re-stamp last_run_ok_ended, so consecutive slots can't cascade-skip.
            # Guarded on args.scheduled first so a manual `run` pays no meta read; the
            # stamp is then read ONCE and reused in the message (runs may overlap,
            # deliberately unguarded, and this log line is what a user reads to learn why
            # a slot didn't run — it must show the value the decision was made on).
            if args.scheduled:
                last_ok = meta_get(conn, "last_run_ok_ended")
                if _cooldown_active(last_ok, datetime.now()):
                    run_id = start_pipeline_run(
                        conn, trigger=trigger, run_date=run_date
                    )
                    finish_pipeline_run(conn, run_id, status="skipped")
                    print(f"[cooldown] last successful run ended {last_ok} "
                          f"(< {COOLDOWN_MINUTES} min ago) — skipping this scheduled slot")
                    return
            # The report is keyed to the date the run STARTED, not the date it finishes:
            # a run launched 23:xx that drags past midnight (throttled fetch, big eval batch)
            # stamps its rows with yesterday's first_seen — keying the report to "today at
            # report time" would file it under the new day and those rows would appear in NO
            # report at all. This is a code invariant, deliberately not a scheduling
            # constraint (any run can cross midnight if delayed).
            run_id = start_pipeline_run(conn, trigger=trigger, run_date=run_date)
            token = set_active_pipeline_run(run_id)
            stage = "fetch_linkedin"
            try:
                _run_fetch_stage(fetch_new_jobs, cfg, conn, "linkedin")
                stage = "fetch_adzuna"
                _run_fetch_stage(fetch_adzuna, cfg, conn, "adzuna")
                stage = "fetch_ats"
                _run_fetch_stage(fetch_ats, cfg, conn, "ats")
                stage = "error_requeue"
                requeue_error_rows(conn)
                # RESTORE direction first, BEFORE the filters: a skipped row whose chain
                # decision was undone returns to 'new' here and re-faces CURRENT rules.
                stage = "restore_decided_reposts"
                skip_decided_reposts(conn, forward=False)
                stage = "restore_evaluated_reposts"
                skip_evaluated_reposts(conn, forward=False)
                stage = "salary_filter"
                apply_salary_filter(cfg, conn)
                stage = "hard_filters"
                apply_hard_filters(cfg, conn)
                # Forward skips remain after deterministic filters and before paid eval.
                stage = "skip_decided_reposts"
                skip_decided_reposts(conn, restore=False)
                stage = "skip_evaluated_reposts"
                skip_evaluated_reposts(conn, restore=False)
                stage = "evaluation"
                evaluate_new_jobs(cfg, conn)
                stage = "report"
                generate_report(cfg, conn, run_date)
                stage = "cooldown_stamp"
                if run_has_successful_target(conn, run_id):
                    meta_set(conn, "last_run_ok_ended",
                             datetime.now().isoformat(timespec="seconds"))
                else:
                    if completed_run_status(conn, run_id) == "degraded":
                        print("[cooldown] all fetch sources failed — not stamping "
                              "last_run_ok_ended, so the next scheduled slot runs in full")
                    else:
                        print("[cooldown] no fetch target was configured — not stamping "
                              "last_run_ok_ended, so the next scheduled slot runs in full")
                finish_pipeline_run(
                    conn, run_id, status=completed_run_status(conn, run_id)
                )
            except BaseException as exc:
                # Ctrl-C/SystemExit remain fatal, but the durable row records that the run
                # did not complete. Error messages stay only in the human log, never SQLite.
                # Roll back FIRST: finish_pipeline_run commits its terminal marker, and must
                # never accidentally ship an interrupted downstream stage's partial writes.
                conn.rollback()
                try:
                    finish_pipeline_run(
                        conn, run_id,
                        status=("interrupted" if isinstance(
                            exc, (KeyboardInterrupt, SystemExit)
                        ) else "failed"),
                        error_stage=stage,
                        error_type=type(exc).__name__,
                    )
                except Exception as health_exc:
                    print(f"[health] could not finish failed run record: {health_exc}",
                          file=sys.stderr)
                raise
            finally:
                reset_active_pipeline_run(token)
    elif args.command == "report":
        generate_report(cfg, conn, args.date)
    elif args.command == "stats":
        cmd_stats(conn)
    elif args.command in ("applied", "passed"):
        cmd_mark(conn, args.url, None if args.undo else args.command, args.resume, args.channel)
    elif args.command == "expired":
        cmd_expired(conn, args.url, args.undo)
    elif args.command == "reject":
        cmd_reject(conn, args.url, args.gate, args.pattern, args.note, args.undo)
    elif args.command == "event":
        cmd_event(conn, args.url, args.event_type, args.date, args.note, args.undo)
    elif args.command == "dupe":
        cmd_dupe(conn, args.url, args.of, args.undo, args.yes)
    elif args.command == "prune":
        cmd_prune(conn, args.days, args.vacuum)
    elif args.command == "backup":
        try:
            cmd_backup(conn, args.output)
        except (BackupError, OSError) as exc:
            print(f"[backup] failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
