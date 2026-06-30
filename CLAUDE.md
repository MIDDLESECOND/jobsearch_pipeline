# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal job-search pipeline: it pulls configured searches from LinkedIn (scraped via
python-jobspy logged-out guest endpoints — **never** add login cookies) and from the Adzuna API
(sanctioned, free), dedupes into SQLite, runs each new posting through an LLM "gate-check"
evaluation, and writes one markdown report per day. Single-user CLI tool, not a service. The CLI
and pipeline stages live in `pipeline.py`; the repost/content-dedup and decision-chain core is
`chain.py` (imported back into `pipeline`'s namespace, so `pipeline.X` still resolves); the local
triage UI is `app.py` (Flask) + `templates/index.html`. Unit tests are in `tests/` (`python -m
pytest`) — synthetic fixtures, never the real `jobs.db`. Other top-level files are config/data.

Why two sources: LinkedIn is the one *scrape* target that still works — Indeed, Glassdoor,
ZipRecruiter, and Google Jobs are all behind anti-bot walls (probed and confirmed). Adzuna is an
official API, so it sidesteps that entirely. It's optional: active only for searches with an
`adzuna:` block and only when `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` are set, else the run is LinkedIn-only.
Adzuna rows carry only a 500-char description snippet, and ML-*predicted* salaries are dropped to NULL
(so the deterministic salary filter never acts on a guess); both facts are flagged in the report/UI.

## Commands

```bash
pip install -r requirements.txt          # python-jobspy, anthropic, pyyaml (.venv present)

python pipeline.py run                    # full cycle: fetch → salary filter → hard filters → eval → report
python pipeline.py report [--date YYYY-MM-DD]   # rebuild a report from the DB only (no fetch, no API cost)
python pipeline.py stats                  # DB counts

# Per-posting user decisions (--url takes a unique substring of the job_url, e.g. the job id):
python pipeline.py applied --url <id> [--undo]
python pipeline.py passed  --url <id> [--undo]
python pipeline.py reject  --url <id> --gate <name> [--pattern P] [--undo]

# Manually link a duplicate the fingerprint missed (drifted title, or LinkedIn<->Adzuna cross-post).
# Earliest-first_seen posting becomes canonical; any decision propagates across the merged chain:
python pipeline.py dupe    --url <id> --of <other-id> [--yes] [--undo]
```

No test framework. Validation is two scripts that read the real `jobs.db`: `python backtest_v2.py`
(asserts expected verdicts on known postings — the eval-framework regression guard) and
`python compare_models.py` (cross-model comparison → `compare_results.json`). Scheduling is
`run_pipeline.bat` via Windows Task Scheduler.

## Architecture invariants (the non-obvious parts)

- **The `run` stage order is load-bearing.** Each stage gates on the `status` column, and only
  `status='new'` rows reach the *paid* eval. So the deterministic, zero-cost filters (salary, then
  hard-requirement rules) run *before* the LLM and short-circuit obvious rejects. A new pre-eval
  filter must set a non-`new` status, mirroring the existing salary/hard-filter passes. Both fetchers
  (`fetch_new_jobs` for LinkedIn, then `fetch_adzuna`) run first and only insert `status='new'` rows,
  so everything downstream is source-agnostic — the `source` column is for provenance/flagging only.

- **SQLite (`jobs.db`) is the single source of truth; reports are disposable derivations.** Never
  reconstruct state from `reports/` (per-day and lossy). The schema and all migrations live inline
  in the DB-open path and run on *every* startup — idempotent and additive. Add schema changes
  there; there are no separate migration files.

- **Dedup is two-layer and guards against double-applying.** Beyond the `job_url` primary key, a
  content fingerprint (normalized company+location + **exact** normalized title) catches LinkedIn
  relistings under fresh URLs. Title matching is intentionally exact, not fuzzy — a backtest showed
  fuzzy matching collapsed distinct roles sharing a generic core. User decisions
  (applied/passed/reject) propagate across a repost chain. Caveat: this fingerprint is *within-source*
  in practice — Adzuna's location strings ("Grand Central, Manhattan") rarely match LinkedIn's
  ("New York, NY"), so the same role cross-posted to both usually appears once per source. Loosening
  to company+title-only was rejected: it reintroduces the false-repost class the exact match avoids.
  The manual escape hatch for a miss is `pipeline.py dupe --url A --of B` (`cmd_dupe`), or the web UI's
  two-click "⧉ duplicate" → "↩ same role" controls (and "Unlink dup"): it links two existing rows as
  one role by hand — earliest `first_seen` becomes canonical, the link is recorded in `repost_source`
  (`manual` / `manual:<prev_url>`) so undo can reconstruct the split — without any fuzzy matching (the
  user asserts the duplicate; code only records and propagates it). CLI and UI share one core
  (`_dupe_resolve` / `_dupe_commit` / `_dupe_unlink`) in `chain.py`; the guard/conflict logic lives
  there, not in either front-end. **The "what has the user decided about this role's chain?" question
  has exactly one implementation — `chain.effective_decision` — used by the report (`_repost_info`),
  the web UI (`row_to_dict`), and the dupe conflict guard, so the three can't drift.**

- **The evaluator's "brain" is external data, not code.** `profile.md` (candidate facts) and
  `evaluation_guide.md` (the gate/scoring framework) are read at runtime and embedded in the system
  prompt. To change *how postings are judged*, edit those markdown files — do not hardcode judgment
  in Python. The one exception: the guide's load-bearing routing rule (the "50/0 fix":
  `ai_artifact_depth == 0` caps a PASS to RECRUITER_ONLY) is enforced in code so it can't depend on
  the model complying.

- **Provider default is DeepSeek** (cheap, but deliberately under-filters — which is why the
  hard-filter / `reject` override layer exists). `filters.yaml` holds user-maintained deterministic
  rules (substring, or `re:`-prefixed regex); `reject --pattern` appends to it, and it's
  tool-managed/append-safe, separate from the hand-edited `config.yaml`.

## Conventions

- **CHANGELOG.md is for judgment/schema/routing changes** — any change to how postings are
  *evaluated* (gates, scoring, verdicts, bucket routing, or the `jobs` schema) gets a dated entry on
  top. Day-to-day `config.yaml` search-term edits do not.
- **Personal files are gitignored; `*.example.*` are the committed templates** (`config.yaml`,
  `profile.md`, `evaluation_guide.md`, `filters.yaml`, `jobs.db`, `reports/`, `logs/`). When changing
  the *shape* of config or filters, update the matching `.example` file.
- **Windows environment**: PowerShell/cmd; API keys via `setx` with a registry-read fallback
  (`_ensure_api_key`): `DEEPSEEK_API_KEY` (default eval provider) or `ANTHROPIC_API_KEY`, plus
  `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` for the optional Adzuna source.
