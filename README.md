# Job Search Pipeline — Setup (Windows)

Fetches your configured LinkedIn searches via the logged-out guest endpoints, dedupes against
a local SQLite database, runs each new posting through a gate-check evaluation framework
(Claude **or** DeepSeek), and writes one markdown report per day in `reports/`.

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
   `config.yaml` (`anthropic` → `ANTHROPIC_API_KEY`, `deepseek` → `DEEPSEEK_API_KEY`):
   ```
   setx ANTHROPIC_API_KEY "sk-ant-..."
   ```
   Close and reopen Command Prompt after this.

## 2. Test run

```
python pipeline.py run
```

First run will take a few minutes (9 searches × 20s delay + evaluations). Then open
`reports\report_YYYY-MM-DD.md`. If fetches fail, see Troubleshooting.

Other commands:
- `python pipeline.py report` — regenerate today's report without fetching (free, no API calls)
- `python pipeline.py report --date 2026-06-10` — regenerate a past day's report
- `python pipeline.py stats` — database counts by status/verdict

## 3. Schedule 3x/day (Task Scheduler)

1. Open **Task Scheduler** → **Create Task** (not "Basic Task").
2. General tab: name it `Job Pipeline`; select "Run whether user is logged on or not"
   only if your machine is always on — otherwise "Run only when user is logged on" and
   tick "Run task as soon as possible after a scheduled start is missed".
3. Triggers tab: add three Daily triggers, e.g. **7:30**, **12:30**, **17:30**.
4. Actions tab: Start a program → Program: `C:\jobsearch_pipeline\run_pipeline.bat`
   → "Start in": `C:\jobsearch_pipeline`.
5. OK to save. Right-click → Run once to verify; check `logs\pipeline.log`.

The times don't need to be exact — `hours_old: 10` in config.yaml gives each run overlap
with the previous one, and the database dedupes anything seen twice.

## 4. Editing searches

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

## 5. Reading the report

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

## Troubleshooting

- **Searches return 0 or error with 429/blocked**: LinkedIn is rate-limiting your IP.
  Raise `delay_between_searches` to 45–60, or drop to 2 runs/day. Blocks on the guest
  endpoint are temporary (hours).
- **Every search errors after working previously**: LinkedIn changed the guest endpoint.
  Run `pip install -U python-jobspy` — the library usually patches within days. The
  pipeline is a convenience layer; expect occasional downtime.
- **`ANTHROPIC_API_KEY not set`**: rerun the `setx` command, then fully close and reopen
  the terminal (or reboot before the scheduled task runs).
- **Evaluation errors in report**: usually transient API issues; those rows stay in
  status `error` and are listed in the report so you can review them manually.

## Cost

At typical volume (30–80 new postings/day, ~1,500 words each) with the default
`claude-sonnet-4-6` evaluator, expect roughly **$0.50–$1.50/day**. Switch `model` in
config.yaml to `claude-haiku-4-5-20251001` to cut that ~5x if volume grows; gate checks
are mostly pattern-matching against explicit posting text, which the smaller model handles
acceptably — though it will be somewhat weaker on the judgment-call flags.
