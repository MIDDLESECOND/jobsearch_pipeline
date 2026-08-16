"""Post-second-judge doorbell for the Claude Code deepdive batch.

Run by run_second_judge.bat right after opinions land -- the moment the actionable
zone is at its most informed. Counts the zone, estimates the batch's wall time and
plan-quota cost, reads the live plan utilization, and pops a Yes/No box. Yes opens a
visible Claude Code console already carrying the batch trigger.

Boundaries:
- jobs.db is opened READ-ONLY (uri mode=ro); this script never writes the database.
- It decides nothing about postings; the popup is information, the human is the trigger.
- Auth: the Claude Code credentials file's interactive-login access token (the only one
  carrying the `user:profile` scope the usage endpoint requires). Used for exactly one
  GET to api.anthropic.com; never printed, logged, or stored. Its PRESENCE is read once
  more at launch — never its value — to decide whether the batch console can be handed
  the plan login instead of an inherited env token (see `_launch_env`). Quota display is
  best-effort: any failure degrades to the row/time estimate alone.
- Calibration + last-batch stamp live in the gitignored .deepdive_state.json, written
  by the deepdive skill at the end of each confirmed batch.

Popup requires the scheduled task to run as the logged-on user ("Run only when user is
logged on"), same as the visible run_pipeline.bat console.
"""
import ctypes
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

from core import ADZUNA_SNIPPET_MAX_CHARS, recency_dt, run_log
from states import COLD_APPLY_MIN_FIT, RECRUITER_ROUTE_MIN_FIT

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / ".deepdive_state.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
FRESH_DAYS = 14            # mirror the standing allocation / second-judge window
# 2026-08-16: the flat top-10 cap is gone. A batch now takes EVERY full-text batchable
# row in the window plus a fixed snippet-completion quota (snippets pay a browser fetch
# each and 10 of the first 12 completed pairs lost their action on full text, so
# uncapping them buys wall-clock mostly to confirm skips). The size estimate below has
# to mirror that shape or the popup misprices exactly the batches the widening created.
# MIRRORED in .claude/skills/deepdive/SKILL.md's snippet-quota leg, which reserves this
# many completion slots per batch. Change one, change both — the same coupling
# BATCH_TRIGGER carries below, and for the same reason: that pair has drifted twice, and
# SKILL.md is gitignored, so the committed tree shows no sign the other half exists. A
# quota raised here but not there makes every popup overstate the batch, and the
# calibration the skill writes back is then measured against a row count that never ran.
SNIPPET_QUOTA = 3
# The session guard: the ceiling on ABSOLUTE 5h-window utilization, not on the share a
# single batch adds. Stated absolutely because that is the only reading a doorbell can
# check before launching and the skill can check between rows — "50% of the window" as a
# per-batch allowance would let a batch started at 60% push the window past 100%, which
# is the failure the guard exists to prevent. Uncapping full text removed the natural
# ~10-row ceiling, so this is now the ONLY bound on a batch's session cost.
SESSION_GUARD_PCT = 50.0
SESSION_LABEL = "5h 窗口"   # plan_usage's label for the window the guard watches
# BATCHABLE = the row a batch can actually consume, mirroring ALL of the skill's batch
# scope, not just its fit leg: PASS at/above the standing allocation's cold-apply bar,
# no second-judge downgrade, and inside the apply window. Rows failing any leg stay in
# the zone — the second judge still reviews them, the popup still counts them as
# context — but they are never batch material, so they must not drive the fire/skip
# decision or the size estimate. Getting this subset wrong in either direction is what
# the 2026-08-15 reviews kept catching: too wide and "no popup" is unreachable, too
# narrow and real work goes unannounced.
BATCH_MIN_FIT = 15
BATCH_FRESH_DAYS = 5       # the apply-window evidence: first 1-2 days / first review batch
DEFAULT_MIN_PER_ROW = 4.0
DEFAULT_KTOK_PER_ROW = 35.0
POPUP_TIMEOUT_MS = 2 * 60 * 60 * 1000   # self-dismiss so a missed slot can't stack
# MIRRORED in .claude/skills/deepdive/SKILL.md's trigger section, which names this exact
# phrase as the pre-confirmed batch trigger ("start the batch immediately, no re-ask").
# Change one, change both: a phrase the skill does not recognize turns the launch into a
# console that opens and ASKS — indistinguishable, from the user's chair, from the
# swallowed-prompt dead window this launcher was just fixed for.
BATCH_TRIGGER = "run today's deepdive batch"


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_doorbell_state(patch):
    """Merge a patch into the state file's `doorbell` key.

    Ownership split inside one file: the deepdive skill owns `last_batch_iso`,
    `calibration`, and `processed_urls`; the doorbell owns only `doorbell`. Both
    read-modify-write, and they run at different moments (skill at batch end, doorbell
    right after the judge), so a clobber would need a collision inside milliseconds and
    would cost at most one stale cooldown marker.
    """
    try:
        state = load_state()
        if not isinstance(state, dict):      # valid JSON, wrong shape
            state = {}
        # setdefault returns the STORED value when the key exists, so a `doorbell` key
        # holding null (or anything non-dict) would make .update() raise past an
        # OSError-only guard — and this runs inside _token(), whose caller invokes it
        # outside its own try, so that exception would take the whole popup down.
        current = state.get("doorbell")
        state["doorbell"] = {**current, **patch} if isinstance(current, dict) else dict(patch)
        STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False),
                              encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass    # a cooldown we cannot persist is a nudge we retry — not a failure


def zone_rows(conn):
    """Deepdive-actionable rows: undecided, at the action bars, fresh.

    Returns (url, first_seen, is_snippet, is_batchable). `is_batchable` marks the
    narrower slice a batch can actually consume — ALL legs of the skill's scope: PASS at
    the cold-apply bar, no second-judge downgrade, and within BATCH_FRESH_DAYS. The wider
    zone (which also holds PASS 13–14 and RECRUITER_ONLY) is still returned, because the
    popup reports zone context; but only batchable rows may drive the fire/skip decision
    and the size estimate. Conflating the two is what made `pending` undrainable
    (measured 2026-08-15: 5,983 of 9,669 zone rows can never enter a batch), and testing
    only the fit leg still overstated it 3x (492 flagged vs 162 truly reachable).

    MIRRORS second_judge.pending_rows' zone predicate — that function owns it; this one
    reproduces it for counting because the doorbell must not pull the judge's submit
    window, chain dedup, or per-batch cap. Change one, change both: this pair has already
    drifted twice (a missing filter_source clause here; a textual date_posted slice that
    disagreed with recency_dt). The doorbell's own additions are the batchable flag and
    the wider verdict set it keeps for context.

    Freshness runs through core.recency_dt — the ONE effective posted-at reading the
    report label, the triage sort, and second_judge's window all share (AGENTS.md).
    A textual date_posted slice would diverge from it exactly where the sanity window
    earns its keep: a placeholder date ("9999-12-31") reads as permanently fresh here
    while the second judge correctly ages it out through first_seen.
    """
    cutoff = dt.date.today() - dt.timedelta(days=FRESH_DAYS)
    batch_cutoff = dt.date.today() - dt.timedelta(days=BATCH_FRESH_DAYS)
    rows = conn.execute(
        """
        SELECT j.job_url, j.first_seen, j.date_posted,
               (COALESCE(j.source,'')='adzuna'
                AND length(COALESCE(j.description,'')) <= ?) AS snippet,
               (j.verdict='PASS' AND j.fit_score >= ?
                AND NOT EXISTS (SELECT 1 FROM second_opinions s
                                WHERE s.job_url = j.job_url AND s.status='done'
                                  AND s.verdict <> 'PASS')) AS batchable
        FROM jobs j
        WHERE j.status='evaluated' AND j.filter_source IS NULL AND j.app_status IS NULL
          AND ((j.verdict='PASS' AND j.fit_score >= ?)
               OR (j.verdict='RECRUITER_ONLY' AND j.fit_score >= ?))
        """,
        (ADZUNA_SNIPPET_MAX_CHARS, BATCH_MIN_FIT,
         COLD_APPLY_MIN_FIT, RECRUITER_ROUTE_MIN_FIT),
    ).fetchall()
    out = []
    for url, first_seen, date_posted, snippet, batchable in rows:
        eff, mode = recency_dt(date_posted, first_seen)
        # mode None = no usable date; first_seen already put it in view, so keep it
        # (same call the second judge makes).
        if mode is not None and eff.date() < cutoff:
            continue
        # The apply window is the batch's last leg — evaluated on the same recency
        # reading, so a row cannot be "fresh enough to batch" by one clock and stale by
        # another. An undated row stays batchable: first_seen already vouched for it.
        fresh_enough = mode is None or eff.date() >= batch_cutoff
        out.append((url, first_seen or "", bool(snippet),
                    bool(batchable) and fresh_enough))
    return out


def guard_budget(now_pct):
    """Percent of the 5h window a batch may still spend before SESSION_GUARD_PCT.

    Its own function because it is the entire bridge from the live quota read to the
    guard, and its SIGN is the failure nobody would see: written the other way round it
    goes negative on a HEALTHY window, batch_counts returns nothing, and the doorbell
    falls permanently silent — while every test that computes its own budget still
    passes. None (window unreadable) propagates as fail-open.
    """
    return None if now_pct is None else SESSION_GUARD_PCT - float(now_pct)


def batch_counts(n_full_pending, n_snippet_pending, budget_pct=None,
                 full_pct=0.0, snippet_pct=0.0):
    """(n_full, n_snippet) the NEXT batch will actually read.

    Full text is uncapped; snippets clamp to SNIPPET_QUOTA. When a session budget is
    known (SESSION_GUARD_PCT minus current utilization) the batch is additionally
    truncated to what fits, spending it on full text FIRST: snippets are the measured
    low-yield class (10 of the first 12 completed pairs lost their action on full text),
    so they are what a squeezed batch should drop.

    The two classes are priced separately on purpose. A snippet row pays a browser
    completion a full-text row does not, and the widening moved the mix from ~23%
    snippets (the batch session_pct_per_row was measured on) to ~9%, so one blended
    constant now misprices every batch — high, which would trip this very guard early
    and truncate batches that had headroom.

    Pure so the popup arithmetic is testable; `budget_pct=None` (quota unreadable or
    uncalibrated) fails OPEN to the unguarded counts, matching plan_usage's rule that a
    quota hiccup must never suppress the doorbell.
    """
    n_full = max(0, n_full_pending)
    n_snip = min(max(0, n_snippet_pending), SNIPPET_QUOTA)
    if budget_pct is None or (full_pct <= 0 and snippet_pct <= 0):
        return n_full, n_snip
    budget = max(0.0, float(budget_pct))
    if full_pct > 0:
        n_full = min(n_full, int(budget // full_pct))
    if snippet_pct > 0:
        left = max(0.0, budget - n_full * full_pct)
        n_snip = min(n_snip, int(left // snippet_pct))
    return n_full, n_snip


def arrivals_watermark(conn, processed, chunk=500):
    """Newest `first_seen` among rows a batch has already consumed, read from the DB.

    Derived here instead of trusting the state file's `last_batch_iso`, whose contract
    ("max first_seen processed") lives only as prose in a gitignored skill file. Measured
    2026-08-16: it arrived as a UTC wall clock taken at batch end (23:12:15) where the
    newest processed first_seen was 17:09:17 — two errors at once, a wall clock instead
    of a row value AND a clock the column does not use (every `first_seen` here is local;
    the table's fetch cadence shows 08/11/14/17/20/23 local). Comparing a UTC stamp
    against local values makes every row fetched for the next ~5 hours read as "not new",
    which is why 其中新进 sat at 0 all day. Reading the same column the comparison uses
    cannot drift from it.

    This value alone is NOT a watermark: the skill prunes `processed_urls` down to rows
    still in the actionable zone, so deciding the newest row a batch read drops it out of
    the set and this max falls back to an older row (measured: the live file holds 68
    entries, 0 of them decided — pruning really runs). Deciding rows right after a batch
    is the normal cycle, so `arrivals_mark` wraps this in the monotonic guard; call that,
    not this, for anything user-facing.

    Ordering is lexicographic, which is chronological only while `first_seen` keeps one
    format. It very nearly does — 91,214 of 91,215 live rows are `...T..:..:..` and one
    is space-separated, which sorts before every same-day `T` row and so can never win
    this max. One row is noise; a producer that starts emitting them would not be.

    Chunked because `processed_urls` is unbounded in principle and SQLite caps host
    parameters per statement.
    """
    best = ""
    urls = list(processed)
    for i in range(0, len(urls), chunk):
        part = urls[i:i + chunk]
        q = ",".join("?" * len(part))
        got = conn.execute(
            f"SELECT MAX(first_seen) FROM jobs WHERE job_url IN ({q})", part).fetchone()[0]
        if got and got > best:
            best = got
    return best


def arrivals_mark(conn, processed, remembered=""):
    """The popup's arrivals watermark, monotonic by construction.

    A watermark that can move backwards is not one. The DB-derived value alone does move
    backwards, because `processed_urls` shrinks: the skill prunes decided rows out of it,
    so marking the newest batched row applied would re-expose every row fetched between
    the previous newest and it — rows that batch had already read — as fresh arrivals.
    Deciding rows right after a batch IS the workflow, so that is the common path.

    The fix is to remember the high-water value in the `doorbell` key, which this module
    owns outright (the skill owns `last_batch_iso`/`processed_urls`/`calibration` and is
    never touched here). Only ever rises, so pruning cannot claw it back.
    """
    return max(arrivals_watermark(conn, processed), remembered or "")


def pending_split(fresh, processed, last_batch=""):
    """(pending_urls, n_pending_snippet, new_urls) over the BATCHABLE subset only.

    All three counts come from ONE pass so the popup's arithmetic cannot drift between
    them. Two relations are load-bearing and are what this function exists to hold:

    * n_pending_snippet <= len(pending). main() derives the full-text count by
      SUBTRACTING one from the other, so a snippet tally gathered over a wider set than
      `pending` (dropping the batchable leg is the easy slip) yields a negative row
      count and a popup quoting negative minutes.
    * new_urls is a SUBSET of pending. The popup nests them ("待看 N（其中新进 M）"), and
      counting arrivals over the whole zone once printed 待看 494（其中新进 1256）.

    Pending counts only BATCHABLE rows: the sub-bar rows a batch may never take would
    otherwise keep it permanently non-empty and make "no popup" unreachable.
    """
    pending, n_snippet, new_rows = [], 0, []
    for url, first_seen, snippet, batchable in fresh:
        if not batchable or url in processed:
            continue
        pending.append(url)
        if snippet:
            n_snippet += 1
        # No stamp yet = no batch has run, so everything waiting is an arrival.
        if not last_batch or first_seen > last_batch:
            new_rows.append(url)
    return pending, n_snippet, new_rows


def session_pct(quota):
    """Current 5h-window utilization from plan_usage's rows, or None if unreadable.

    The endpoint is undocumented, so `percent` may be absent or non-numeric; None means
    "unknown", which every caller must treat as fail-open rather than as zero.
    """
    for label, pct, _ in quota:
        if label == SESSION_LABEL:
            try:
                return float(pct)
            except (TypeError, ValueError):
                return None
    return None


def _file_expiry():
    """The credentials file's stored expiresAt (ms epoch), or 0 if unreadable."""
    try:
        data = json.loads((Path.home() / ".claude" / ".credentials.json").read_text("utf-8"))
        return (data.get("claudeAiOauth") or {}).get("expiresAt") or 0
    except (OSError, ValueError):
        return 0


def _file_token():
    """Unexpired access token from the Claude Code credentials file, else None.

    NOTE (measured 2026-08-15): the long-lived `claude setup-token` value — the one in
    CLAUDE_CODE_OAUTH_TOKEN — is NOT usable here. The usage endpoint answers it with
    403 `OAuth token does not meet scope requirement user:profile`: that token carries
    inference scope only. Only the interactive-login token in the credentials file has
    the profile scope, and it lives ~8h.
    """
    try:
        data = json.loads((Path.home() / ".claude" / ".credentials.json").read_text("utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        if oauth.get("accessToken") and (oauth.get("expiresAt") or 0) > dt.datetime.now().timestamp() * 1000:
            return oauth["accessToken"]
    except (OSError, ValueError):
        pass
    return None


def _token():
    """File token; if expired, nudge the CLI to refresh it, then re-read.

    MEASURED (2026-08-15): a plain `claude -p` call does NOT rewrite the credentials file
    while the token is still valid, so this nudge only ever fires on an already-expired
    token. The first natural-expiry popup DID fail — root cause was env-token shadowing
    (see comment below), not the refresh path; the file was observed refreshed later the
    same morning, so refresh-on-expiry happens, though which process performed it was
    not cleanly attributed. Worst case if the nudge underdelivers: the quota block shows
    its 'unavailable' line; the doorbell's row/time estimate is unaffected.
    """
    tok = _file_token()
    if tok:
        return tok
    # One wasted nudge per expiry, not one per firing: the nudge costs a real (tiny)
    # inference call — spending the very quota the popup reports — and can block to its
    # timeout. If it already failed for THIS stored expiresAt, skip straight to the
    # graceful line instead of repeating it at all six of the day's slots.
    expiry = _file_expiry()
    state = load_state()
    doorbell = state.get("doorbell") if isinstance(state, dict) else None
    if isinstance(doorbell, dict) and doorbell.get("nudge_failed_for_expiry") == expiry:
        return None
    claude = shutil.which("claude")
    if claude:
        try:
            env = os.environ.copy()
            # ROOT CAUSE of the 2026-08-15 "读取失败" popup: a user-wide
            # CLAUDE_CODE_OAUTH_TOKEN (the setup-token; inference-only scope)
            # satisfies the child CLI's auth outright, so it never touches — and
            # never refreshes — the credentials file. Strip it to force the child
            # onto the file OAuth path, which is the one that rewrites the file.
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            subprocess.run([claude, "-p", "ok"], capture_output=True, timeout=60,
                           env=env)
        except (subprocess.SubprocessError, OSError):
            pass
    refreshed = _file_token()
    if not refreshed:
        _save_doorbell_state({"nudge_failed_for_expiry": expiry})
    return refreshed


def plan_usage():
    """[(label, percent, resets_at_local)] for the live plan limits, or [] if unavailable."""
    token = _token()
    if not token:
        return []
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    # Fetch AND parse under one guard. The endpoint is undocumented, so its shape is not
    # a contract we control; a cosmetic quota line must never cost the popup itself.
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = []
        for lim in data.get("limits") or []:
            kind = lim.get("kind")
            if kind == "session":
                label = SESSION_LABEL
            elif kind == "weekly_all":
                label = "周 全模型"
            elif kind == "weekly_scoped":
                scope = lim.get("scope")
                model = scope.get("model") if isinstance(scope, dict) else None
                name = model.get("display_name") if isinstance(model, dict) else None
                label = f"周 {name or 'scoped'}"
            else:
                continue
            out.append((label, lim.get("percent"), _local(lim.get("resets_at"))))
        return out
    except Exception:
        return []   # never let a quota hiccup suppress the doorbell


def _local(iso):
    if not isinstance(iso, str) or not iso:
        return ""
    try:
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return ""


def _launch_argv(claude_exe, brain_dir):
    """The console command line that starts a batch session.

    THE TRIGGER GOES FIRST, before any flag. `--add-dir <directories...>` is VARIADIC:
    a trigger sitting after it is folded into the directory list, so the console comes
    up with an EMPTY input box and the batch never starts. That is exactly what the
    first real doorbell launch did (2026-08-15 22:47: window up, session idle, nothing
    ran, no transcript written). The proof costs no API call — with the trigger trailing,
    `claude --add-dir <dir> "<trigger>" -p` answers "Input must be provided either
    through stdin or as a prompt argument when using --print", i.e. the parser never
    saw a prompt at all.

    So: positional first, flags after, and nothing may ever trail a variadic flag.

    THE `call` IS LOAD-BEARING TOO, for a second re-parse one layer down. Python quotes
    any argument containing a space, and when the string after `/k` STARTS with a quote
    cmd applies its outer-quote-stripping heuristic and splits the path at the space.
    Measured 2026-08-15 with a deliberately spaced path:

        cmd /c "C:\\...\\a b\\show.bat" "<trigger>" --add-dir "C:\\Users\\A B\\brain"
            -> rc=1, 'C:\\...\\a' is not recognized as an internal or external command
        cmd /c call "C:\\...\\a b\\show.bat" "<trigger>" --add-dir "C:\\Users\\A B\\brain"
            -> rc=0, ARGS=["<trigger>" --permission-mode auto --add-dir "C:\\Users\\A B\\brain"]

    `call` makes the first token after `/k` an ordinary word, so the heuristic never
    fires and every quote survives. It costs nothing today (this machine's claude lives
    in a space-free path) and covers the day `shutil.which` returns a Program Files
    install, or a user home like C:\\Users\\First Last reaches `brain_dir`.

    Those two runs isolate the quoting rule under `/c`; the line that SHIPS is `/k`
    nested inside `start`, so it was then run whole (2026-08-15): `cmd /c start
    "Deepdive Batch" cmd /k call <claude.exe> "<trigger>" --permission-mode auto
    --add-dir <brain>` brought the console up on "Opus 5 · Claude Max" with the trigger
    already submitted and answered. Both halves of the evidence are here so nobody has
    to re-derive whether `call` is safe under `/k` before touching this line.

    `--permission-mode auto`: a default-mode console stops at a confirm prompt on the
    very first brain-file read (they live outside the repo), which defeats a one-click
    launch. Auto mode matches the main session; the skill's own confirm-before-CLI
    discipline still gates every DB write at the conversation level. `--add-dir` grants
    the brain/resume workspace (derived from the user home, not hardcoded).
    """
    return ["cmd", "/c", "start", "Deepdive Batch", "cmd", "/k", "call",
            claude_exe, BATCH_TRIGGER,
            "--permission-mode", "auto", "--add-dir", brain_dir]


def _launch_env(base, file_login):
    """The environment a batch console inherits. `file_login` = is a credentials-file
    token usable right now (`_file_token()`), read at click time.

    Three decisions, each measured:

    - The CLAUDE_CODE_* markers go because a doorbell fired from INSIDE a Claude Code
      session (testing) would otherwise hand the child markers that switch transcript
      persistence off. Task Scheduler runs carry none of them to begin with.
    - CLAUDE_CODE_OAUTH_TOKEN goes ONLY when a file login can take over. It is the
      shadowing `_token()` documents, one level up: the env token satisfies the CLI's
      auth outright, so the console comes up on the API-side default model instead of
      the interactive-login Max session — the 2026-08-15 22:47 doorbell console read
      "Sonnet 5 · Claude API" where a hand-launched one read "Opus 5 · Claude Max".
      Task Scheduler builds its env from the user registry, so only a scheduled firing
      inherits it. But stripping it with no file login to fall back on parks the console
      on a login prompt — the same dead window, different door — so a stale-model
      session is deliberately preferred over no session at all.
    - ANTHROPIC_API_KEY deliberately STAYS. Measured the same day: a console carrying
      only that key still came up "Opus 5 · Claude Max", so removing it buys no auth
      change, while the `pipeline.py` commands the batch runs would have to re-read it
      from HKCU through `core._ensure_api_key`'s fallback.
    """
    env = dict(base)
    for marker in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                   "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
        env.pop(marker, None)
    if file_login:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
    return env


def _launch_batch():
    """Resolve the CLI, build both halves of the launch, spawn the console. Returns the
    argv it launched, or None when `claude` cannot be found.

    Split out of main()'s Yes branch because the WIRING is what actually keeps breaking
    here — first a trigger folded into a variadic flag, then an env that silently handed
    the batch a different judge — and inside main() it sat behind a MessageBox and a
    read-only open of the real jobs.db, so no test could reach it. Both halves had unit
    tests while the line joining them had none.
    """
    brain_dir = str(Path.home() / "Downloads" / "resume_variant")
    claude_exe = shutil.which("claude")
    if not claude_exe:
        print("cannot launch the batch: `claude` is not on this environment's PATH")
        return None
    argv = _launch_argv(claude_exe, brain_dir)
    # _file_token() is read HERE rather than reused from the quota block: the box can sit
    # for two hours, and what decides the env is whether a file login can take over at
    # the moment of Yes.
    subprocess.Popen(argv, cwd=str(ROOT), env=_launch_env(os.environ, _file_token()))
    # Log the RESOLVED command line, not a bare success claim. Popen does not wait, so
    # "launched" only ever meant a process was spawned: the 22:47 stall printed exactly
    # that word while the console sat at an empty prompt, and the window itself was the
    # only surviving evidence (a log line with 30-day retention is what this module's
    # run_log wrapper exists for). The command line is what tells the next investigation
    # whether the trigger reached the CLI at all.
    print(f"launched (auto mode): {subprocess.list2cmdline(argv)}")
    return argv


def main():
    print_only = "--print" in sys.argv
    state = load_state()
    if not isinstance(state, dict):     # a state file that is valid JSON but not an object
        state = {}
    processed = set(state.get("processed_urls") or [])
    door = state.get("doorbell")
    door = door if isinstance(door, dict) else {}

    conn = sqlite3.connect(f"file:{ROOT / 'jobs.db'}?mode=ro", uri=True)
    try:
        fresh = zone_rows(conn)
        urls = [u for u, _, _, _ in fresh]
        n_snippet = sum(1 for _, _, s, _ in fresh if s)
        opinions = 0
        if urls:
            q = ",".join("?" * len(urls))
            opinions = conn.execute(
                f"SELECT COUNT(DISTINCT job_url) FROM second_opinions"
                f" WHERE status='done' AND job_url IN ({q})", urls,
            ).fetchone()[0]
        last_batch = arrivals_mark(conn, processed, door.get("arrivals_mark"))
    finally:
        conn.close()

    # PENDING drives the popup, not "newer than the last batch": the snippet tail beyond
    # the quota and any session-guard remainder are rows a batch legitimately skipped,
    # so a last-batch watermark would mark them "not new" forever. `processed_urls` is
    # what a batch actually consumed (the skill prunes it to current-zone membership
    # when writing). pending_split owns the derivation and the relations between these
    # three counts; the tests exercise that function rather than a copy of it.
    pending, pending_snippet, new_rows = pending_split(fresh, processed, last_batch)
    if not pending:
        print("deepdive doorbell: no batchable row is waiting - no popup")
        return

    cal = state.get("calibration") or {}
    if not isinstance(cal, dict):
        cal = {}

    def _cal(key, default):
        """Calibration constants are written by the skill, so a key can exist holding
        JSON null — dict.get's default does NOT cover that, and float(None) raises."""
        value = cal.get(key)
        try:
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return float(default)

    # Per-class costs: a per-class key wins, else the legacy blended one, else the
    # default — so a state file written before the 2026-08-16 split still prices exactly
    # as it used to instead of silently reading 0.
    def _pair(per_class, blended, default):
        base = _cal(blended, default)
        return (_cal(f"{per_class}_full_row", base), _cal(f"{per_class}_snippet_row", base))

    full_min, snip_min = _pair("minutes_per", "minutes_per_row", DEFAULT_MIN_PER_ROW)
    full_ktok, snip_ktok = _pair("ktokens_per", "ktokens_per_row", DEFAULT_KTOK_PER_ROW)
    full_pct, snip_pct = _pair("session_pct_per", "session_pct_per_row", 0)

    quota = plan_usage()
    if quota:
        lines = "\n".join(f"    {lab:9} {pct:>3}%   （{res} 重置）" for lab, pct, res in quota)
        quota_block = f"\n当前额度：\n{lines}\n"
    else:
        quota_block = "\n当前额度：读取失败（token 未设或已过期，见脚本注释）\n"

    # The session guard, enforced here so the doorbell cannot propose work the skill's
    # between-rows check would abort on row 1. Unknown utilization or an uncalibrated
    # session cost leaves budget None = fail open.
    now_pct = session_pct(quota)
    budget = guard_budget(now_pct)
    n_full_pending = len(pending) - pending_snippet
    n_full, n_snip = batch_counts(n_full_pending, pending_snippet, budget,
                                  full_pct, snip_pct)
    n = n_full + n_snip
    if not n:
        # Worded to stay true in BOTH sub-cases: the window past the ceiling, and a
        # window still under it whose remainder cannot pay for one row. This log exists
        # because a 2026-08-15 investigation had nothing to read; naming a cause that
        # may be false is how it misdirects the next one.
        print(f"deepdive doorbell: 5h window at {now_pct:g}% leaves no room under the "
              f"{SESSION_GUARD_PCT:g}% guard - {len(pending)} rows wait, no popup")
        # Silence here is indistinguishable from "nothing was waiting" unless the skip
        # outlives this process. The next popup carries it, so a guard-skipped day can
        # never pass for a day that had no work. NOT under --print: that path is the
        # interactive check and stays side-effect-free (it is already kept out of the run
        # log for the same reason), or looking would manufacture a skip that never
        # suppressed anything.
        if not print_only:
            _save_doorbell_state({
                "guard_skipped_at": dt.datetime.now().isoformat(timespec="seconds"),
                "guard_skipped_pending": len(pending)})
        return

    est_min = round(n_full * full_min + n_snip * snip_min)
    est_ktok = round(n_full * full_ktok + n_snip * snip_ktok)
    est_pct_val = n_full * full_pct + n_snip * snip_pct
    est_pct = f" ≈ 5h 窗口的 ~{round(est_pct_val)}%" if est_pct_val else ""

    # Never let the trimmed batch read as the whole backlog (AGENTS.md's rule for the
    # duplicate queue, and the count-honesty bug this popup has now shipped twice). The
    # two exclusions are NOT the same and must never be merged: rows beyond the quota are
    # never batched and age out of the window, while rows the guard dropped are re-offered
    # next time, exactly like the full-text remainder. Charging the second to the first
    # overstates permanent loss — measured at a 44% window: 77 claimed vs 74 real.
    in_quota = min(pending_snippet, SNIPPET_QUOTA)
    quota_tail = pending_snippet - in_quota
    held = (n_full_pending - n_full) + (in_quota - n_snip)
    tail_note = f"；配额外 snippet {quota_tail} 条不进批（将自然过期）" if quota_tail else ""
    guard_note = (f"\n受窗口守卫（{SESSION_GUARD_PCT:g}%，当前 {now_pct:g}%）限制，"
                  f"另有 {held} 条顺延下次" if held > 0 else "")

    # A batch the guard withheld produced no popup at all, so the news has to ride the
    # NEXT one or the user never learns a day was skipped.
    skipped_at = door.get("guard_skipped_at")
    skip_note = ""
    if skipped_at:
        skip_note = (f"\n（上次 {_local(skipped_at)} 因窗口守卫跳过，"
                     f"当时 {door.get('guard_skipped_pending') or '?'} 条在等）")

    body = (
        f"待看 {len(pending)} 条（其中新进 {len(new_rows)}）；区内共 {len(fresh)}："
        f"全文 {len(fresh) - n_snippet} / snippet 待补全 {n_snippet}；"
        f"{opinions} 条已有二审意见\n"
        f"本批 {n} 条 = 全文 {n_full} + snippet {n_snip}（配额 {SNIPPET_QUOTA}）"
        f"{tail_note}{guard_note}\n"
        f"预计：约 {est_min} 分钟 / 约 {est_ktok}k tokens{est_pct}{skip_note}\n"
        f"{quota_block}\n"
        f"现在跑吗？\n"
        f"【是】开一个 Claude Code 窗口立即开跑（可随时打断）\n"
        f"【否】不跑；之后想跑就说\"跑今天的批\""
    )
    print(body)
    if print_only:
        return

    # Persist the high-water value before the box goes up, not after: it is a cache of a
    # computed reading, not news awaiting acknowledgement, so a self-dismissed popup must
    # still leave the mark raised. --print returned above and writes nothing.
    if last_batch and last_batch != door.get("arrivals_mark"):
        _save_doorbell_state({"arrivals_mark": last_batch})

    MB_YESNO, MB_TOPMOST, MB_SETFOREGROUND, MB_ICONQUESTION = 0x4, 0x40000, 0x10000, 0x20
    IDYES, IDTIMEOUT = 6, 32000
    rc = ctypes.windll.user32.MessageBoxTimeoutW(
        None, body, "Deepdive 批：二审已收好 — 现在跑吗？",
        MB_YESNO | MB_TOPMOST | MB_SETFOREGROUND | MB_ICONQUESTION, 0, POPUP_TIMEOUT_MS,
    )
    # Retire a carried skip notice only once it has actually been SEEN. The box
    # self-dismisses after POPUP_TIMEOUT_MS, so clearing when the body was built would
    # consume the news on exactly the runs nobody was at the machine for — and a
    # guard-skipped day would go back to being invisible. (--print returns above, so the
    # interactive check cannot eat it either.)
    if skipped_at and rc != IDTIMEOUT:
        _save_doorbell_state({"guard_skipped_at": None, "guard_skipped_pending": None})
    if rc == IDYES:
        _launch_batch()
    else:
        print(f"popup dismissed (MessageBox rc={rc}) - no batch launched")


if __name__ == "__main__":
    # Tee into the day's pipeline log exactly like the judge that precedes us. Under Task
    # Scheduler this process's console goes nowhere, so without it every diagnostic — which
    # branch ran, a failed quota read, a traceback — is unrecoverable after the fact (that
    # gap already cost one 2026-08-15 investigation). --print is the interactive path and
    # stays unlogged so a manual check does not pad the log.
    if "--print" in sys.argv:
        main()
    else:
        with run_log("deepdive-doorbell"):
            main()
