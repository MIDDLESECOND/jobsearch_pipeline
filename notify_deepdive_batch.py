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
  GET to api.anthropic.com; never printed, logged, or stored. Quota display is
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
BATCH_CAP = 10
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
                label = "5h 窗口"
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


def main():
    print_only = "--print" in sys.argv
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
    finally:
        conn.close()

    state = load_state()
    if not isinstance(state, dict):     # a state file that is valid JSON but not an object
        state = {}
    last_batch = state.get("last_batch_iso", "")
    # PENDING drives the popup, not "newer than the last batch": a batch takes the TOP
    # ~10 rows by freshness+fit, so the last-batch stamp is normally the newest row in
    # the zone, and a watermark test would mark every row the batch DIDN'T reach as
    # "not new" forever. `processed_urls` is what a batch actually consumed (the skill
    # prunes it to current-zone membership when writing). Pending counts only BATCHABLE
    # rows — the sub-bar rows a batch may never take would otherwise keep it permanently
    # non-empty, pinning the estimate at BATCH_CAP and making "no popup" unreachable.
    processed = set(state.get("processed_urls") or [])
    pending = [u for u, _, _, batchable in fresh if batchable and u not in processed]
    # Arrivals are counted over the SAME batchable subset the popup nests them inside.
    # Scanning the whole zone made "其中新进 M" not a subset of "待看 N" — observed:
    # 待看 494（其中新进 1256）— and reported work no batch could take.
    if last_batch:
        new_rows = [u for u, seen, _, batchable in fresh
                    if batchable and seen > last_batch and u not in processed]
    else:
        new_rows = pending    # no batch has run yet: everything waiting is new
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

    n = min(len(pending), BATCH_CAP)
    est_min = round(n * _cal("minutes_per_row", DEFAULT_MIN_PER_ROW))
    est_ktok = round(n * _cal("ktokens_per_row", DEFAULT_KTOK_PER_ROW))
    pct_per_row = _cal("session_pct_per_row", 0)   # measured across past batches
    est_pct = f" ≈ 5h 窗口的 ~{round(n * pct_per_row)}%" if pct_per_row else ""

    quota = plan_usage()
    if quota:
        lines = "\n".join(f"    {lab:9} {pct:>3}%   （{res} 重置）" for lab, pct, res in quota)
        quota_block = f"\n当前额度：\n{lines}\n"
    else:
        quota_block = "\n当前额度：读取失败（token 未设或已过期，见脚本注释）\n"

    body = (
        f"待看 {len(pending)} 条（其中新进 {len(new_rows)}）；区内共 {len(fresh)}："
        f"全文 {len(fresh) - n_snippet} / snippet 待补全 {n_snippet}；"
        f"{opinions} 条已有二审意见\n"
        f"一批（上限 {BATCH_CAP} 条）预计：约 {est_min} 分钟 / 约 {est_ktok}k tokens{est_pct}\n"
        f"{quota_block}\n"
        f"现在跑吗？\n"
        f"【是】开一个 Claude Code 窗口立即开跑（可随时打断）\n"
        f"【否】不跑；之后想跑就说\"跑今天的批\""
    )
    print(body)
    if print_only:
        return

    MB_YESNO, MB_TOPMOST, MB_SETFOREGROUND, MB_ICONQUESTION = 0x4, 0x40000, 0x10000, 0x20
    IDYES = 6
    rc = ctypes.windll.user32.MessageBoxTimeoutW(
        None, body, "Deepdive 批：二审已收好 — 现在跑吗？",
        MB_YESNO | MB_TOPMOST | MB_SETFOREGROUND | MB_ICONQUESTION, 0, POPUP_TIMEOUT_MS,
    )
    if rc == IDYES:
        # Launch as a clean TOP-LEVEL session. When the doorbell itself is fired from
        # inside a Claude Code session (testing), the child inherits markers that switch
        # transcript persistence off -- strip them so the batch session is recorded
        # normally. Task Scheduler runs carry none of these to begin with.
        env = os.environ.copy()
        for marker in ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
                       "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE"):
            env.pop(marker, None)
        env["CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"] = "1"
        # --permission-mode auto: a default-mode console stops at a confirm prompt on
        # the very first brain-file read (they live outside the repo), which defeats a
        # one-click launch. Auto mode matches the main session; the skill's own
        # confirm-before-CLI discipline still gates every DB write at the conversation
        # level. --add-dir grants the brain/resume workspace (derived from the user
        # home, not hardcoded).
        brain_dir = str(Path.home() / "Downloads" / "resume_variant")
        claude_exe = shutil.which("claude")
        if not claude_exe:
            print("cannot launch the batch: `claude` is not on this environment's PATH")
            return
        subprocess.Popen(
            ["cmd", "/c", "start", "Deepdive Batch", "cmd", "/k",
             claude_exe, "--permission-mode", "auto", "--add-dir", brain_dir,
             "run today's deepdive batch"], cwd=str(ROOT), env=env,
        )
        print("launched: Claude Code console with the batch trigger (auto mode)")
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
