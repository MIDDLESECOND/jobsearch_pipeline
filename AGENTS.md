# AGENTS.md

This file provides guidance to coding agents working with code in this repository.
(Claude Code loads it via the `@AGENTS.md` import in [CLAUDE.md](CLAUDE.md); agents that
read AGENTS.md natively get it directly. One source of truth, zero sync surfaces.)

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
  `resolve_posting`, `mark_posting`, `reject_posting`, `dupe_resolve`/`dupe_commit`/
  `dupe_unlink`, and the post-application outcome cores (`record_event`/`undo_event`/
  `chain_events`/`set_resume` over the `app_events` table; `_recompute_outcome` is the ONE
  writer of the cached `outcome_status`/`outcome_date` columns). Imports only `states` + stdlib.
- `core.py` — paths, `load_config` (+ shape validation), the SQLite open/schema/**migrations**,
  `_ensure_api_key`, and `parse_iso` (the ONE posting-date parser + sanity window — the fetch-side
  normalizer and the report/UI recency triage both go through it, so the stored `date_posted`
  shape's producer and consumers can't drift). The foundation; imports only `chain` and `states`.
- `materials.py` — application-packet evidence: automatic JD snapshots, content-addressed
  resume/cover-letter storage, document extraction + ATS diagnostics, chain-scoped packet reads,
  downloads, and interview-prep clipboard context. Imports `core` and `chain`.
- `outreach.py` — manually verified role contacts plus clipboard-only outreach drafting briefs.
  Contacts follow canonical-at-write/current-chain reads; briefs reuse packet/event evidence and
  never send messages. Imports `chain` and `materials`.
- `tasks.py` — user-defined role next actions with due dates and open/completed/cancelled state.
  Tasks follow canonical-at-write/current-chain reads and import only the standard library.
- `interviews.py` — explicit interview schedules plus local iCalendar export. Schedules follow
  canonical-at-write/current-chain reads and import only the standard library.
- `exports.py` — local chain-deduped CSV role summaries. It composes chain decisions, tasks, and
  interviews under one read snapshot and neutralizes spreadsheet-formula cells.
- `watchlist.py` — explicit manual role priority. Stars follow canonical-at-write/current-chain
  reads, use absolute optimistic state, and do not alter decisions or evaluation.
- `dupe_candidates.py` — recent cross-source duplicate suggestions plus persistent reviewed-pair
  dismissals. It derives candidates without mutation and never links chains on its own;
  `confirm_candidate` transactionally validates preview/dismissal state before reusing the guarded
  `chain.dupe_*` cores. Imports `chain` plus the standard library.
- `filters.py` — the deterministic pre-eval salary + hard-requirement filters, and
  `_pattern_matches` (the one user-facing pattern dialect: substring or `re:` regex). Imports
  only `core` and `states`.
- `fetch.py` — the three sources (`fetch_new_jobs` = LinkedIn, `fetch_adzuna` = Adzuna API,
  `fetch_ats` = Greenhouse/Lever/Ashby ATS boards). Imports `core`, `chain`, `states`, and
  `filters` (`_pattern_matches`, so the ATS title/location filters speak the filters.yaml dialect).
- `evaluation.py` — the LLM gate-check (prompt, providers, `normalize_result`'s 50/0 cap, eval loop).
- `report.py` — the daily markdown report + renderers (uses `chain.effective_decision`).
- `workflow.py` — bounded, filterable UI read models and Action Center aggregation. It
  imports `report`/`chain`/`states`/`dupe_candidates`, performs no mutations, and keeps dashboard-query logic
  out of both the decision service and Flask routes.
- `funnel.py` — chain-scoped application-funnel aggregation over current applied decisions
  and append-only outcome history. It imports only `states`, performs no mutations, maps
  events from former canonicals to the current chain, and owns conversion definitions.
- `pipeline.py` — the CLI/orchestrator: the `run` stage order, thin `cmd_*` wrappers over the
  chain service cores, and `main`.
- `app.py` (Flask) + `templates/index.html` — the local triage UI (a thin HTTP/presentation
  layer over `workflow`/`funnel` reads and the `chain` service cores).

**Growth rule**: `chain.py` is already the biggest module (~29% of production code) and is
where every decision-adjacent feature has landed. A new *concern-level* feature (e.g. outreach
drafting, a funnel view's aggregation logic) gets its own module in the DAG — `chain.py` only
absorbs changes to the concerns it already owns (chains, decisions, events, dupes).

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

python pipeline.py run                    # full cycle: fetch → error requeue → repost-skip restores → salary filter → hard filters → repost-skip forward → eval → report
#   --scheduled (passed by run_pipeline.bat): cooldown guard — no-op if the last successful run ended <60 min ago; bare `run` always executes
python pipeline.py report [--date YYYY-MM-DD]   # rebuild a report from the DB only (no fetch, no API cost)
python pipeline.py stats                  # DB counts

# Per-posting user decisions (--url takes a unique substring of the job_url, e.g. the job id):
python pipeline.py applied --url <id> [--resume V] [--channel C] [--undo]   # --resume records the variant sent; --channel how it went out (direct|agency|referral)
python pipeline.py passed  --url <id> [--undo]
python pipeline.py expired --url <id> [--undo]   # posting is dead/delisted (Adzuna lists stale ads): fixed note event + chain-wide passed, so relistings auto-skip; refused on applied chains — record an outcome event instead
python pipeline.py reject  --url <id> --gate <name> [--pattern P] [--undo]

# Post-application outcome tracking (what happened AFTER applying — interview rounds, offer,
# ghosted, …; --type note = bare note on any posting; --undo removes the chain's last event):
python pipeline.py event   --url <id> --type <followup_sent|recruiter_screen|interview|offer|rejected_by_employer|ghosted|withdrew|note> [--date YYYY-MM-DD] [--note N] [--undo]

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
tests can't reach). Real-DB validation scripts live in `tests/validation/` (not collected by pytest —
no `test_*.py` names there; all test/validation scripts, existing and future, belong under `tests/`):
`python tests/validation/backtest_v2.py` (asserts expected verdicts on known postings — the
eval-framework regression guard; the script is committed, its cases live in
`tests/validation/backtest_cases.local.json`, gitignored because they name real postings — the
same split as `boundary_cases.local.json`) and `python tests/validation/compare_models.py`
(cross-model comparison). Every validation script writes its outputs to
`tests/validation/results/` (gitignored) — never the repo root. Scheduling is `run_pipeline.bat`
via Windows Task Scheduler.

## Architecture invariants (the non-obvious parts)

- **The `run` stage order is load-bearing.** Before any stage, a `--scheduled` run (the .bat's
  spelling) may cooldown-skip: if the `meta` table's `last_run_ok_ended` (stamped only after a
  FULL successful cycle in which ≥1 fetch source didn't crash — an all-sources-down catch-up
  run must not suppress the first slot that can fetch) is <60 min old, the slot no-ops —
  visibly, inside the day's run-log markers; the predicate fails open on missing/garbage
  stamps and manual runs never skip. A skip is a full no-op (error-row requeue, reconciles,
  and report rebuild all wait for the next executed run).
  Each stage gates on the `status` column, and only
  `status='new'` rows reach the *paid* eval. So the deterministic, zero-cost passes run *before*
  the LLM and short-circuit obvious rejects: the two repost-skip reconciles' RESTORE direction
  (released rows re-face the current rules), then the salary and hard-requirement filters, then
  the skip passes' FORWARD direction (`skip_decided_reposts` runs first; the decided label also
  wins order-independently — its forward pass upgrades `repost_evaluated` rows — and the
  restore-before-filters order is behaviorally pinned by tests). A new
  pre-eval filter must set a non-`new` status, mirroring the existing salary/hard-filter passes,
  and must run BEFORE the forward skip passes so their reconciles see its stamps. All three
  fetchers (`fetch_new_jobs` for LinkedIn, then `fetch_adzuna`, then `fetch_ats`) run first and only
  insert `status='new'` rows, so everything downstream is source-agnostic — the `source` column is
  for provenance/flagging only. Each fetcher is wrapped by `_run_fetch_stage` (the untrusted-input
  boundary): one source's crash is logged, rolled back, and skipped so the run still reaches the
  filters/eval/report for the sources that succeeded. The deterministic stages after the fetchers
  are deliberately **not** guarded — they must fail loud, since limping past a crashed filter would
  let un-filtered rows reach the paid eval.

- **SQLite (`jobs.db`) is the authoritative store for posting/workflow state; reports are
  disposable derivations.** Never reconstruct state from `reports/` (per-day and lossy).
  Application evidence is the one deliberate two-part store: SQLite owns its metadata/links and
  the adjacent `application_materials/` object store owns its content-addressed bytes. A complete
  evidence backup or restore MUST treat both as one unit: stop the UI, pipeline, and scheduled runs,
  then copy/restore `jobs.db` and `application_materials/` together. The schema and all migrations
  live inline in the DB-open path and run on *every* startup — idempotent and additive, plus ONE sanctioned
  non-additive mechanism: a one-shot row-preserving table rebuild when a DB's baked-in
  status/verdict CHECK falls behind `states.py` (`_rebuild_for_stale_checks` — SQLite can't ALTER
  a CHECK, and a stale one aborts every run). Add schema changes there; there are no separate
  migration files. The DB runs in WAL mode: the `jobs.db-wal`/`jobs.db-shm` sidecars are part
  of the database whenever a process has it open — copy/back up the evidence unit only when nothing has
  it open (a clean close checkpoints and removes them), and never delete a hot `-wal` (it holds
  committed rows).

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
  there, not in either front-end. The Action Center's **Possible duplicates** queue is a separate,
  review-only discovery layer: it considers recent cross-source rows with exact normalized
  company+title, shows both postings, and never links or skips anything automatically. Its API
  serializes only the posting fields rendered by that comparison; it does not load or expose role
  contacts, tasks, application materials, interview details, or evaluation/decision card data. A confirmed
  pair goes through `dupe_candidates.confirm_candidate` and the same `dupe_resolve` /
  `dupe_commit` guards. A rejected suggestion is
  stored as an ordered current-root pair in `dupe_candidate_dismissals`; it can be restored, and a
  later root-changing merge may surface changed evidence again rather than inheriting a stale
  dismissal. Review rows retain a monotonic version even after restore; ignore/restore requests
  carry the preview roots, expected dismissal state, and expected review version. All three are
  revalidated under `BEGIN IMMEDIATE`, so a stale tab cannot hide a changed pair or overwrite a
  newer review, including an ABA sequence. **The "what has the user decided about this role's chain?" question
  has exactly one implementation — `chain.effective_decision` — used by the report (`_repost_info`),
  the web UI (`row_to_dict`), and the dupe conflict guard, so the three can't drift.** The same
  function owns the chain-level verdict reading (`chain_verdict`/`chain_fit_score`: the most
  favorable JUDGE verdict, `status='evaluated'` rows only — a filters.yaml GATE_FAIL stamp is not a
  judgment); `chain.skip_evaluated_reposts`' SQL subquery mirrors that predicate, and the two carry
  cross-referencing comments — change one, change both.

- **Application tracking is history + an outcome cache, both chain-scoped.** What happened
  after `applied` (screen/interview/offer/ghosted/…, `followup_sent`, and notes) lives in the
  append-only `app_events` table: an
  event is written ONCE, keyed to the chain's **canonical url at write time**, and always read
  chain-wide (`chain.chain_events`) — so a dupe merge unions both sides' histories with no data
  migration and unlink leaves rows where they sit. The chain's *current* outcome is denormalized
  onto every member (`jobs.outcome_status`/`outcome_date`) exactly like `app_status`;
  `chain._recompute_outcome` is the ONE writer of those two cache columns, and the cache is
  always a pure function of (chain applied?, `states.APP_EVENTS`) — latest outcome event wins,
  cleared while not applied, restored on re-apply (undoing `applied` never deletes history).
  `followup_sent` is applied-only history but is NOT an outcome: it advances the Action Center's
  7-day cadence (maximum two sends) without claiming the employer responded.
  `jobs.resume_variant` and `jobs.channel` are separate: applied-only fields written uniformly
  chain-wide by `propagate_app_status`/`set_resume`/`set_channel` (given → written; absent on a
  re-assert → inherited from the chain's stored value; chain leaves applied → cleared), with no
  history — NOT restored on re-apply. `channel` is a closed vocabulary (`states.ALL_CHANNELS`:
  direct | agency | referral, validated in chain, no CHECK); `resume_variant` is free text.
  `states.APPLIED_ONLY_EVENTS` require the chain applied; `note` events attach anywhere.
  The local UI exposes that note path on every role card; it must not be hidden behind applied-only
  outcome controls or treated as an outcome.
  `app_events.event_type`/`jobs.outcome_status`
  carry **no schema CHECK** on purpose (user-decision vocabulary, enforced in
  `chain.record_event` against `states.ALL_EVENTS` — see states.py's docstring); don't add one.
  Follow-up eligibility is derived, never cached: applied + no employer outcome, then the
  next due date is application/last-`followup_sent` date + 7 days; two sent follow-ups retire
  the reminder. Events on former canonicals are mapped through current chain membership.

- **Application packets are immutable evidence, read chain-wide.** Marking a role applied through
  either front-end freezes the exact interaction posting's stored JD (a relisting may differ from
  its canonical) and appends a `jd_snapshot` link. JD snapshots and resume/cover-letter uploads
  live under the gitignored `application_materials/` object store, deduplicated by SHA-256;
  SQLite stores file/ATS metadata plus append-only links, never the documents' extracted text.
  Each link stores both its canonical-at-write owner and the exact interaction posting URL;
  ownership is mapped through the attachment job's CURRENT chain root at read time, exactly like
  `app_events`: merge unions packets and unlink separates them without data migration. Latest
  attachment per kind is the current submitted packet; older links remain evidence. The
  summary/read path checksum-verifies stored objects (cached by path/size/mtime within the process)
  and surfaces `missing`/`corrupt` instead of presenting cached ATS metadata as live evidence. The
  `interview_prep` Action Center queue is derived from a current recruiter-screen/interview
  outcome within 14 days—no separate completion state—and prep context is read-only clipboard
  text from the frozen JD, actual documents, and chain event history. ATS reading-order output is
  explicitly heuristic; it must not be presented as proof of any vendor parser's behavior.

- **Role contacts are local evidence; outreach is review-only.** `job_contacts` rows store the
  canonical URL at write time plus the exact interaction posting and are mapped through that
  owner posting's current chain root on read, matching `app_events` and packet-link behavior.
  Duplicate merges union contact sets; unlink leaves each pre-merge canonical's contacts where
  they were recorded. `outreach.py` owns the contact vocabulary, validation, chain-scoped CRUD,
  batch reads, and drafting context. The context may contain untrusted JD/document text, must tell
  an assistant to treat it as evidence rather than instructions, and may only be copied for human
  review. Do not add automatic email/LinkedIn sending, login cookies, or implicit
  `followup_sent` events: the user records that event only after a message actually went out.

- **Role tasks are explicit local next actions, not inferred state.** `job_tasks` rows use the
  same canonical-at-write/current-chain read pattern as contacts and events, so duplicate merges
  union task lists and unlink restores their original ownership without rewriting rows. `tasks.py`
  owns validation and mutation; `workflow.py` owns the bounded `tasks_due` Action Center query.
  Only open tasks due today or earlier enter that queue, one card per current chain. Completion
  and cancellation retain closed rows. Snooze only reschedules an open task; a closed task must
  be explicitly reopened. Mutations use an optimistic version so a stale browser tab cannot
  overwrite newer task state. Do not derive tasks from LLM output, send external notifications,
  or silently treat a task mutation as an application/outcome event.

- **Interview schedules are plans, not outcomes.** `job_interviews` rows use the same
  canonical-at-write/current-chain read model as tasks, contacts, packets, and events. Only applied
  chains can add or mutate a schedule; future scheduled rounds enter the bounded
  `upcoming_interviews` Action Center queue, one card per current chain. Store starts as normalized
  UTC instants, display them in the browser's timezone, and retain cancelled records. Mutations use
  optimistic versions and refresh ownership under `BEGIN IMMEDIATE`. `.ics` is a local download
  for explicit user import. Do not write Google/Outlook calendars, send reminders, infer schedules
  from LLM output, or convert a scheduled plan into an `interview` outcome event automatically.

- **Stars are manual priority, not judgment.** `role_stars` maps canonical-at-write markers through
  current duplicate chains. Merge unions starred state; unlink restores the original owner without
  rewriting rows. The `starred_roles` Action Center queue shows one current canonical per starred
  chain. Star/unstar uses an absolute expected-state guard under `BEGIN IMMEDIATE` so stale tabs
  cannot invert a newer choice. Do not derive stars from fit scores or let them change application,
  filter, outcome, task, or event state. Unstar retains a versioned tombstone; each actual
  star/unstar transition receives a monotonic version, and the UI sends its expected version so
  stale tabs cannot overwrite a newer choice even after the state changes away and back.

- **The evaluator's "brain" is external data, not code.** `profile.md` (candidate facts) and
  `evaluation_guide.md` (the gate/scoring framework) are read at runtime and embedded in the system
  prompt. To change *how postings are judged*, edit those markdown files — do not hardcode judgment
  in Python. The exceptions: the guide's load-bearing routing rules — the "50/0 fix"
  (`ai_artifact_depth == 0` caps a PASS to RECRUITER_ONLY; fails CLOSED on a missing score) and
  the formal-leadership cap (`formal_leadership_required: true` caps the same way; fails OPEN on
  a missing field, since pre-cap eval_json rows lack the key) — are enforced in code
  (`evaluation.normalize_result`) so they can't depend on the model complying.

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

## Frontier Radar instructions for Codex

Read `CLAUDE.md` for the repository's existing workflow and evidence rules.

For career-related claims, also read:

- `D:\Github\learning_path\governance\CROSS_PROJECT_RULES.md`
- `D:\Github\learning_path\assumptions\AI_CAREER_CLAIM_LEDGER.md`
- `D:\Github\learning_path\decisions\D-2026-08-03_CAREER_REALIGNMENT.md`

If the canonical files are unavailable, report the missing access instead of reconstructing them
from memory.

This repository owns external evidence monitoring and claim updates. It does not own career
decisions.

- Separate capability, task automation, job redesign, headcount, wages, and career meaning.
- Single articles, demos, vendor claims, or anecdotes cannot directly change the canonical
  strategy.
- Preserve existing user modifications, especially `config/sources.yaml`.
- Private career implications remain in the private layer.
