# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal job-search pipeline: it pulls configured searches from LinkedIn (scraped via
python-jobspy logged-out guest endpoints — **never** add login cookies), from the Adzuna API
(sanctioned, free), and from per-company ATS boards (the Greenhouse/Lever/Ashby public JSON
APIs), dedupes into SQLite, runs each new posting through an LLM "gate-check" evaluation, and
writes one markdown report per day. Single-user CLI tool, not a service.

**Module layout** (a one-way DAG; every consumer — `app.py`, the tests, the validation scripts —
imports the module that owns a name directly; `pipeline.py` is the CLI orchestrator only, NOT a
re-export hub):
- `states.py` — the status/verdict/gate vocabulary and the `status` state-machine doc. The leaf;
  imports nothing.
- `chain.py` — repost/content-dedup + the decision-chain core (normalization, fingerprint,
  `effective_decision`, propagation), plus the decision service cores both front-ends share:
  `resolve_posting`, `mark_posting`, `reject_posting`, and `dupe_resolve`/`dupe_commit`/
  `dupe_unlink`. Imports only `states` + stdlib.
- `core.py` — paths, `load_config` (+ shape validation), the SQLite open/schema/**migrations**,
  `_ensure_api_key`, and `parse_iso` (the ONE posting-date parser + sanity window — the fetch-side
  normalizer and the report/UI recency triage both go through it, so the stored `date_posted`
  shape's producer and consumers can't drift). The foundation; imports only `chain` and `states`.
- `filters.py` — the deterministic pre-eval salary + hard-requirement filters, and
  `_pattern_matches` (the one user-facing pattern dialect: substring or `re:` regex). Imports
  only `core` and `states`.
- `fetch.py` — the three sources (`fetch_new_jobs` = LinkedIn, `fetch_adzuna` = Adzuna API,
  `fetch_ats` = Greenhouse/Lever/Ashby ATS boards). Imports `core`, `chain`, `states`, and
  `filters` (`_pattern_matches`, so the ATS title/location filters speak the filters.yaml dialect).
- `evaluation.py` — the LLM gate-check (prompt, providers, `normalize_result`'s 50/0 cap, eval loop).
- `report.py` — the daily markdown report + renderers (uses `chain.effective_decision`).
- `pipeline.py` — the CLI/orchestrator: the `run` stage order, thin `cmd_*` wrappers over the
  chain service cores, and `main`.
- `app.py` (Flask) + `templates/index.html` — the local triage UI (also a thin layer over the
  chain service cores).

Unit tests are in `tests/` (`python -m pytest`) — synthetic fixtures, never the real `jobs.db`.
Other top-level files are config/data.

Why three sources: LinkedIn is the one *scrape* target that still works — Indeed, Glassdoor,
ZipRecruiter, and Google Jobs are all behind anti-bot walls (probed and confirmed). Adzuna is an
official API, so it sidesteps that entirely. It's optional: active only for searches with an
`adzuna:` block and only when `ADZUNA_APP_ID`/`ADZUNA_APP_KEY` are set, else it is skipped.
Adzuna rows carry only a 500-char description snippet, and ML-*predicted* salaries are dropped to NULL
(so the deterministic salary filter never acts on a guess); both facts are flagged in the report/UI.
The ATS boards (Greenhouse/Lever/Ashby) are official public JSON APIs with **no keys at all** —
gated purely on config (`settings.ats.companies`). They are per-company, not per-query: a board
returns every open role, so the config-side `title_any` (required) and `location_any` (optional)
filters are what keep irrelevant roles out of the DB and the paid eval. ATS descriptions are full
text; salaries are stored NULL ("unstated" — kept by the salary filter, same convention as Adzuna's
predicted salaries).

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

# Manually link a duplicate the fingerprint missed (drifted title, or a cross-source cross-post,
# e.g. LinkedIn<->Adzuna or LinkedIn<->an ATS board).
# Earliest-first_seen posting becomes canonical; any decision propagates across the merged chain:
python pipeline.py dupe    --url <id> --of <other-id> [--yes] [--undo]

# Occasional maintenance: clear descriptions of old rejected rows (GATE_FAIL / salary-filtered
# only — never gates-passed rows, which backtest_v2 re-evaluates from their stored text):
python pipeline.py prune [--days 90] [--vacuum]
```

Tests: `python -m pytest` runs the unit suite in `tests/` — synthetic in-memory fixtures over the
pure logic (normalization, fingerprint/repost, the 50/0 routing cap, filters, the chain/dupe
machinery), never the real `jobs.db`. It includes `test_no_undefined_names.py`, a pyflakes gate that
fails on any undefined name in a module — the cheap guard for the extraction-refactor footgun where
a moved function references a name its new file forgot to import (a NameError on a path the network
tests can't reach). Two extra scripts validate against the real DB: `python backtest_v2.py` (asserts
expected verdicts on known postings — the eval-framework regression guard) and `python
compare_models.py` (cross-model comparison → `compare_results.json`). Scheduling is `run_pipeline.bat`
via Windows Task Scheduler.

## Architecture invariants (the non-obvious parts)

- **The `run` stage order is load-bearing.** Each stage gates on the `status` column, and only
  `status='new'` rows reach the *paid* eval. So the deterministic, zero-cost filters (salary, then
  hard-requirement rules) run *before* the LLM and short-circuit obvious rejects. A new pre-eval
  filter must set a non-`new` status, mirroring the existing salary/hard-filter passes. All three
  fetchers (`fetch_new_jobs` for LinkedIn, then `fetch_adzuna`, then `fetch_ats`) run first and only
  insert `status='new'` rows, so everything downstream is source-agnostic — the `source` column is
  for provenance/flagging only. Each fetcher is wrapped by `_run_fetch_stage` (the untrusted-input
  boundary): one source's crash is logged, rolled back, and skipped so the run still reaches the
  filters/eval/report for the sources that succeeded. The deterministic stages after the fetchers
  are deliberately **not** guarded — they must fail loud, since limping past a crashed filter would
  let un-filtered rows reach the paid eval.

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
  (`dupe_resolve` / `dupe_commit` / `dupe_unlink`) in `chain.py`; the guard/conflict logic lives
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
