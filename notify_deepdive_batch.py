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

from core import ADZUNA_SNIPPET_MAX_CHARS
from states import COLD_APPLY_MIN_FIT, RECRUITER_ROUTE_MIN_FIT

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / ".deepdive_state.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
FRESH_DAYS = 14            # mirror the standing allocation / second-judge window
BATCH_CAP = 10
DEFAULT_MIN_PER_ROW = 4.0
DEFAULT_KTOK_PER_ROW = 35.0
POPUP_TIMEOUT_MS = 2 * 60 * 60 * 1000   # self-dismiss so a missed slot can't stack


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def zone_rows(conn):
    """Deepdive-actionable rows: undecided, at the action bars, fresh."""
    cutoff = (dt.date.today() - dt.timedelta(days=FRESH_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT job_url, first_seen, date_posted,
               (source='adzuna' AND length(COALESCE(description,'')) <= ?) AS snippet
        FROM jobs
        WHERE status='evaluated' AND filter_source IS NULL AND app_status IS NULL
          AND ((verdict='PASS' AND fit_score >= ?)
               OR (verdict='RECRUITER_ONLY' AND fit_score >= ?))
        """,
        (ADZUNA_SNIPPET_MAX_CHARS, COLD_APPLY_MIN_FIT, RECRUITER_ROUTE_MIN_FIT),
    ).fetchall()
    out = []
    for url, first_seen, date_posted, snippet in rows:
        eff = (date_posted or "")[:10] or (first_seen or "")[:10]
        if eff >= cutoff:
            out.append((url, first_seen or "", bool(snippet)))
    return out


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
            subprocess.run([claude, "-p", "ok"], capture_output=True, timeout=90,
                           env=env)
        except (subprocess.SubprocessError, OSError):
            pass
    return _file_token()


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
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []   # never let a quota hiccup suppress the doorbell

    out = []
    for lim in data.get("limits") or []:
        kind = lim.get("kind")
        if kind == "session":
            label = "5h 窗口"
        elif kind == "weekly_all":
            label = "周 全模型"
        elif kind == "weekly_scoped":
            model = ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "scoped"
            label = f"周 {model}"
        else:
            continue
        out.append((label, lim.get("percent"), _local(lim.get("resets_at"))))
    return out


def _local(iso):
    if not iso:
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
        urls = [u for u, _, _ in fresh]
        n_snippet = sum(1 for _, _, s in fresh if s)
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
    last_batch = state.get("last_batch_iso", "")
    new_rows = [u for u, seen, _ in fresh if seen > last_batch] if last_batch else urls
    if not new_rows:
        print("deepdive doorbell: nothing new in the zone since the last batch - no popup")
        return

    cal = state.get("calibration", {})
    n = min(len(new_rows), BATCH_CAP)
    est_min = round(n * float(cal.get("minutes_per_row", DEFAULT_MIN_PER_ROW)))
    est_ktok = round(n * float(cal.get("ktokens_per_row", DEFAULT_KTOK_PER_ROW)))
    pct_per_row = cal.get("session_pct_per_row")   # measured across past batches
    est_pct = f" ≈ 5h 窗口的 ~{round(n * float(pct_per_row))}%" if pct_per_row else ""

    quota = plan_usage()
    if quota:
        lines = "\n".join(f"    {lab:9} {pct:>3}%   （{res} 重置）" for lab, pct, res in quota)
        quota_block = f"\n当前额度：\n{lines}\n"
    else:
        quota_block = "\n当前额度：读取失败（token 未设或已过期，见脚本注释）\n"

    body = (
        f"行动区新进 {len(new_rows)} 条（区内共 {len(fresh)}：全文 {len(fresh) - n_snippet}"
        f" / snippet 待补全 {n_snippet}；{opinions} 条已有二审意见）\n"
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


if __name__ == "__main__":
    main()
