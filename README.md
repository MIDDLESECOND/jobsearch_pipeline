# Job Search Pipeline — Setup (Windows)

Fetches your configured LinkedIn searches via the logged-out guest endpoints, dedupes against
a local SQLite database, runs each new posting through a gate-check evaluation framework
(Claude **or** DeepSeek), and writes one markdown report per day in `reports/`.

Dedup is two-layer: exact job URL, **plus** a content fingerprint (company + title + location)
that catches the same role relisted under a fresh URL — so a repost doesn't read as new, and
you don't accidentally apply twice. You can mark postings `applied` or `passed` (see Commands),
and those decisions follow a role across its reposts.

**It never touches your LinkedIn account. Do not add login cookies to it.**

Your personal files — `config.yaml`, `profile.md`, `evaluation_guide.md`, the `jobs.db`
database, `reports/`, and `logs/` — are gitignored. The repo ships `*.example.*` templates;
you create the real files from them in setup below. Nothing personal is committed.

## 1. One-time setup

1. Install Python 3.10+ from python.org (check "Add Python to PATH" during install).
2. Unzip this folder somewhere permanent, e.g. `C:\jobsearch_pipeline`.
3. Open Command Prompt in that folder and run:
   ```
   pip install -r requirements.txt
   ```
4. Create your private files from the shipped templates, then edit them:
   ```
   copy config.example.yaml config.yaml
   copy profile.example.md profile.md
   copy evaluation_guide.example.md evaluation_guide.md
   ```
   Edit `profile.md` and `evaluation_guide.md` to describe *your* situation and how *you*
   want postings judged — they are the evaluator's brain. Adjust searches in `config.yaml`.
5. Set your API key (one-time, persists across reboots). Use whichever provider you set in
   `config.yaml` — the default is `deepseek` → `DEEPSEEK_API_KEY`; switch to `anthropic` →
   `ANTHROPIC_API_KEY`:
   ```
   setx DEEPSEEK_API_KEY "sk-..."
   ```
   Close and reopen Command Prompt after this.

## 2. Test run

```
python pipeline.py run
```

First run will take a few minutes (9 searches × 20s delay + evaluations). Then open
`reports\report_YYYY-MM-DD.md`. If fetches fail, see Troubleshooting.

## 3. Commands (CLI)

You drive the pipeline by typing `python pipeline.py <command>` in a terminal (PowerShell or
Command Prompt) opened in the project folder. The five commands:

| Command | What it does |
|---------|--------------|
| `run` | Full daily cycle: fetch → salary filter → evaluate → write today's report. The only one that hits the network/API (costs money). |
| `report` | Rebuild a report from the database — **free**, no fetching, no API calls. Defaults to today; `--date YYYY-MM-DD` for a past day. |
| `stats` | Quick database counts: by status/verdict, plus an application-status breakdown (applied / passed / backlog). |
| `applied --url X` | Mark a posting as **applied-to**. |
| `passed --url X` | Mark a posting as **reviewed & decided no**. |

**`--url` takes a unique substring**, not the whole URL — the LinkedIn job id is easiest. If
the substring matches more than one posting, the command refuses and lists them so you can be
more specific. Add **`--undo`** to `applied`/`passed` to clear a status you set by mistake.

```
python pipeline.py run                         # morning fetch + report
python pipeline.py applied --url 4431386393    # you applied to this one
python pipeline.py passed  --url 4431386393    # you looked and skipped this one
python pipeline.py passed  --url 4431386393 --undo   # oops, undo it
python pipeline.py report                      # refresh the report after marking
python pipeline.py report --date 2026-06-10    # rebuild a past day's report
python pipeline.py stats                        # counts
```

**Typical daily loop:** `run` in the morning → read the report and click through to jobs you
like → as you go, `applied` the ones you apply to and `passed` the ones you reject → `report`
to refresh. Passed jobs become muted, and a future repost of anything you applied to or passed
stays flagged (see Reading the report). Jobs you never mark stay in the backlog and keep
showing — nothing is hidden without your say-so. Marking is also how reposts avoid a
double-apply: an `applied` decision follows the role across every future relisting.

## 4. Schedule (Task Scheduler)

1. Open **Task Scheduler** → **Create Task** (not "Basic Task").
2. General tab: name it `Job Pipeline`; select "Run whether user is logged on or not"
   only if your machine is always on — otherwise "Run only when user is logged on" and
   tick "Run task as soon as possible after a scheduled start is missed".
3. Triggers tab: add Daily triggers. The config is tuned for **2 runs/day** (~9:00 and
   ~21:00, 12h apart); a third midday run is fine if you want fresher coverage.
4. Actions tab: Start a program → Program: `C:\jobsearch_pipeline\run_pipeline.bat`
   → "Start in": `C:\jobsearch_pipeline`.
5. OK to save. Right-click → Run once to verify; check `logs\pipeline.log`.

The times don't need to be exact — `hours_old: 13` in config.yaml gives each run overlap
with the previous one, and the database dedupes anything seen twice.

## 5. Editing searches

Everything lives in `config.yaml` — add/remove searches, change Boolean terms, adjust the
salary floor. Analyst-tier searches carry `min_salary: 80000`: postings with a **known**
annual salary below that are dropped; postings with **no stated salary are kept** (this is
deliberately different from LinkedIn's own salary filter, which silently drops some
unlisted postings).

`profile.md` and `evaluation_guide.md` are the evaluator's brain. If your situation changes
(new certifications, a shipped project, a change in work authorization), update `profile.md` —
the next run picks it up automatically.

Changes to *how postings are judged* (scoring, verdicts, routing) are logged in
[`CHANGELOG.md`](CHANGELOG.md).

## 6. Reading the report

**Verdict sections** (how the evaluator triaged each posting):

- **Cold-apply (PASS)** = *worth your read*, not *apply*. The script reliably kills hard
  fails (years floors, clearance language, research-coded substance), but KPMG-style
  research-coding under an SA title can slip through — the ⚠️ flags mark where the
  evaluator was unsure. Strong-band (14–18) postings deserve a full manual gate check
  before you tailor anything. Each carries a **bucket**: 2 (acceptable-tier BI/BA) or
  3 (clean low-code / Power Platform AI delivery — where cold conversion is realistic).
- **Recruiter-only** = passed every gate and scored well, *but* the role's **required**
  AI depth is a generation ahead of the shipped artifact (`ai_artifact_depth` = 0, bucket 1).
  These die in an ATS but convert through a recruiter or referral who can carry the ramp
  narrative — so route them to a human, don't cold-apply. This is the "50/0" fix: the
  evaluation guide's split AI score (applied-vs-research **and** artifact-evidences-depth)
  plus a hard verdict cap stops a high total from masking an unwinnable cold screen.
- **Needs manual review** = the guest endpoint returned no description. Open the link
  and eyeball it; takes seconds.
- **Gate fails** = one-liners for audit. Skim occasionally to confirm the evaluator
  isn't killing things you'd want — especially the first week, while you calibrate trust.

**Status markers** (your decisions + repost history, shown on a posting regardless of verdict):

- **↻ Repost — original first seen …** = the same role relisted under a new URL. The line
  carries the original's first-seen date and prior verdict so you know you've seen it before.
- **🚫 ALREADY APPLIED — do not re-apply** = you ran `applied` on this role (or an earlier
  posting of it). The loudest marker — it's the double-apply guard.
- **↩ You reviewed & passed …** = you ran `passed` on it. A quiet note; the job stays visible
  in case you reconsider, but you've already decided no.
- **No marker** = backlog (you haven't acted on it yet) — shows normally. You set the applied/
  passed states with the `applied` / `passed` commands (see Commands); the full rationale is in
  [`CHANGELOG.md`](CHANGELOG.md).

## Troubleshooting

- **Searches return 0 or error with 429/blocked**: LinkedIn is rate-limiting your IP.
  Raise `delay_between_searches` to 45–60, or drop to 2 runs/day. Blocks on the guest
  endpoint are temporary (hours).
- **Every search errors after working previously**: LinkedIn changed the guest endpoint.
  Run `pip install -U python-jobspy` — the library usually patches within days. The
  pipeline is a convenience layer; expect occasional downtime.
- **`… _API_KEY not set`** (`DEEPSEEK_API_KEY` on the default config, `ANTHROPIC_API_KEY` if
  you switched provider): rerun the `setx` command for that key, then fully close and reopen
  the terminal (or reboot before the scheduled task runs).
- **Evaluation errors in report**: usually transient API issues; those rows stay in
  status `error` and are listed in the report so you can review them manually.

## Cost

The default evaluator is **`deepseek-v4-flash`** (`provider: deepseek` in config.yaml), which
runs at roughly **$0.07 per run** — about **50× cheaper** than Claude Sonnet — at typical
volume (30–80 new postings/day, ~1,500 words each). It's a reasoning model that under-filters
slightly (errs toward showing you more), which is fine since PASS means "worth your read," not
"apply."

To trade cost for evaluation quality, switch `provider`/`model` in config.yaml:
- **`anthropic` / `claude-sonnet-4-6`** — highest quality, ~**$0.50–$1.50/day**. Best on the
  judgment-call ⚠️ flags and research-coding edge cases.
- **`anthropic` / `claude-haiku-4-5-20251001`** — a middle option, ~5× cheaper than Sonnet;
  handles the mostly pattern-matching gate checks acceptably, weaker on the judgment calls.

Remember to set the matching API key (`DEEPSEEK_API_KEY` or `ANTHROPIC_API_KEY`) when you
switch providers.
