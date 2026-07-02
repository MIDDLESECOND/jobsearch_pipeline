# Changelog

Revision log for the job-search pipeline's **evaluation framework** — the guide, the
schema, and the scoring/routing logic. Append a new dated section on top for each
substantive change. Day-to-day search-term edits in `config.yaml` don't belong here;
changes to *how postings are judged* do.

---

## 2026-07-02 — third source: company ATS boards (Greenhouse / Lever / Ashby)

### Why
Aggregators lag or miss postings that only live on a company's own ATS board, and the two existing
sources can't see them: LinkedIn shows what's cross-posted, Adzuna what it happens to index.
Greenhouse, Lever, and Ashby all expose **public, no-auth JSON APIs** per company board — an
official API like Adzuna, but with **full** job descriptions instead of a 500-char snippet, so the
eval judges the whole JD.

### What changed
- **New `fetch_ats()` (`fetch.py`).** Third fetcher in the `run` order, after `fetch_adzuna`.
  ATS boards are per-company (no search query), so config is a curated company list
  (`settings.ats.companies`: slug + board type) plus shared filters: a **required** `title_any`
  (a board returns every open role — the filter is what keeps it from flooding the paid eval) and
  an optional `location_any` (the exact term "remote" opts into remote-flagged postings, but a
  matching city always wins, so hybrid roles aren't lost). Both filters speak the filters.yaml
  pattern dialect (`filters._pattern_matches`: case-insensitive substring or `re:` regex); a
  scalar YAML value is normalized to a list-of-one, and malformed patterns (non-strings, blanks,
  non-compiling regexes, and empty-body `re:` that would match everything) are dropped with a
  stderr notice rather than crashing or silently matching everything/nothing. If a configured
  `location_any` empties out that way it refuses the run (like an empty `title_any`) rather than
  falling through to accept-all. Location matching covers every posted location (Lever
  `allLocations`, Ashby `secondaryLocations`), not just the primary string. Each board is one
  failure unit: a bad payload or row logs FAILED and rolls back that board's partial inserts,
  never aborting the run.
- **Shared pattern validator (`filters.validate_pattern`).** The `re:`-compile / non-empty check
  the ATS sanitizer needs now lives once next to `_pattern_matches`, and `reject --pattern`
  (`pipeline.py`) calls it too — so a broken or empty regex is refused at write time on both
  config surfaces instead of being persisted to `filters.yaml` and failing silently forever. Inserts `status='new'` rows through
  `_insert_posting` — the normalize/fingerprint/repost/INSERT tail now shared by all three
  fetchers, so the jobs column list exists once and the sources can't drift. Salaries are stored NULL ("unstated",
  kept by the salary filter — the same convention as Adzuna's predicted salaries). No posting-age
  filter on purpose: boards list only open roles, and `INSERT OR IGNORE` makes whole-board
  re-fetches idempotent.
- **No schema change.** `source` gains three values (`greenhouse`/`lever`/`ashby`) used for the
  report's 🏢 provenance tag and the UI's source line. The cross-source dedup caveat extends to
  ATS: the same role seen via LinkedIn and via its ATS board usually differs in location text, so
  it appears once per source — `dupe` remains the manual escape hatch.
- **Config shape (`config.example.yaml`).** New `settings.ats:` block; absent/empty → the source
  is off and `run` behaves exactly as before.
- **Tests (`tests/test_fetch_ats.py`).** The pure core (HTML→text, per-board extractors, filters)
  plus `fetch_ats` end-to-end against payload fixtures mirroring the live APIs, with the network
  layer monkeypatched.

---

## 2026-06-30 — manual duplicate linking in the web UI

### Why
`dupe` was CLI-only; the triage UI is where duplicates are actually spotted (two same-role cards in
Today/Backlog). Surfacing the link there closes the loop without dropping to a terminal.

### What changed
- **Shared dupe cores (`_dupe_resolve` / `_dupe_commit` / `_dupe_unlink`, `pipeline.py`).** Extracted
  the validate → preview → commit and the unlink logic out of `cmd_dupe`/`_dupe_undo` so the CLI and
  the web UI run the *same* guard/conflict/propagation code (no duplicated logic). `cmd_dupe` is now a
  thin CLI wrapper (preview + confirm); the guards return user-facing strings instead of printing.
- **`/api/dupe` route + UI controls (`app.py`, `templates/index.html`).** Two-click linking: "⧉
  duplicate" pins a card as an anchor (a sticky banner that survives tab/date changes, so cross-day
  duplicates can be matched), then "↩ same role" on the other card links them; "Unlink dup" splits a
  manual link. `is_manual_repost` is exposed in `/api/jobs` to gate the unlink control. No schema
  change — the merge writes the same `repost_of`/`repost_source` the CLI does, so report/UI rendering
  is unchanged.

---

## 2026-06-30 — manual duplicate linking (`dupe` command)

### Why
`_find_repost` only links reposts at fetch time, and only on an exact normalized company+location+title
match — by design conservative. It misses a relisting whose title/location drifted, and (in practice)
the same role cross-posted to Adzuna vs LinkedIn, whose location strings never normalize alike. When the
user spotted such a duplicate there was no retroactive fix: marking each posting separately didn't
propagate decisions, didn't eval-skip the dupe, and didn't flag them as one role. The only recourse was
a raw `UPDATE jobs SET repost_of=...`.

### What changed
- **New `dupe` command (`cmd_dupe`, `pipeline.py`).** `pipeline.py dupe --url A --of B [--yes] [--undo]`
  links two existing rows as the same role, reusing the existing chain machinery (`repost_of` +
  `_chain_targets` + `skip_decided_reposts`). Adds **no** fuzzy matching and does **not** loosen the
  fingerprint — the user asserts the duplicate; the code only records and propagates it.
  - **Canonical = earliest `first_seen`** (tie-break on `job_url`); the other side is repointed under it.
  - **Repoints the whole sub-chain.** If the merged-in side already owned relistings, every one is
    repointed to the new canonical — the flat one-level chain model (`_chain_targets`) would orphan a
    child left pointing at the demoted original.
  - **Conflict guard.** If both sides are already decided *differently* (`applied`/`passed`/reject gate),
    it aborts rather than overwriting one — no silent data loss.
  - **Decision propagation.** A surviving decision is copied across the unified chain preserving the
    original `status_date`/`filter_date` (the one thing `cmd_mark`/`cmd_reject` can't do after the fact),
    then `skip_decided_reposts` eval-skips any still-`new` member.
  - **Confirmation preview** before commit (skippable with `--yes`; non-interactive stdin *or* Ctrl-C
    fails safe to "no") — a wrong merge buries a real job under another role's decision.
  - **Nested-merge guard.** The `manual:<prev>` encoding is single-level, so re-merging a chain that
    already holds a manual link would strand the inner link (un-undoable). `dupe` refuses and names the
    inner link(s) to undo first.
- **New `repost_source` column (schema + inline migration, `pipeline.py`).** `NULL` = auto-detected,
  `'manual'` = user-linked original, `'manual:<prev_url>'` = user-linked relisting with its prior parent
  encoded so `--undo` reconstructs the original two chains. Additive migration; existing rows backfill NULL.
- **Report/UI unchanged** — both already render off `repost_of`, so a manual link surfaces with the same
  `↻ repost` / ALREADY APPLIED treatment as an auto-detected one.

---

## 2026-06-29 — review fixes: fail-closed 50/0, chain propagation, location normalization

### Why
A multi-agent code review (with adversarial verification of every finding) surfaced five real
issues spanning routing, the repost-decision propagation path, fingerprinting, and the web UI.
A second max-effort review pass over the fixes themselves caught follow-on gaps (NaN/Infinity
slipping the cap, the `repost_decided` sibling class, rule-attribution clobbering), folded in below.

### What changed
- **50/0 cap now fails closed (`normalize_result`, `pipeline.py`).** The load-bearing
  `ai_artifact_depth == 0` → RECRUITER_ONLY cap fired only on a literal `0`, but the output spec
  allows a null/partial `score_breakdown`. A PASS with a missing or non-numeric depth slipped
  through to bucket 2. It now caps unless depth is a **finite number** — None, missing, string, and
  `NaN`/`Infinity` (which `json.loads` parses from bare tokens) all fail closed to RECRUITER_ONLY /
  bucket 1, so the rule no longer depends on the model emitting the literal `0`.
- **Per-posting decisions propagate across the *whole* repost chain (`_chain_targets`, `pipeline.py`).**
  `_chain_targets` previously returned only the named row plus its canonical original, leaving
  *sibling* relistings (R1, R3 when you decide on R2) with stale verdicts/overrides — they kept
  surfacing in regenerated reports. It now resolves the full chain (canonical + every relisting) so
  `applied`/`passed`/`reject` and the web UI's `affected` set cover all members. Signature changed to
  `_chain_targets(conn, m)`.
- **`reject --undo` no longer strands a pre-eval row, and decisions preserve rule attribution
  (`cmd_reject`, `pipeline.py`).** The forward path lifts a still-`new` row to `rule_filtered` to skip
  the paid eval; undo cleared the override but not the status, permanently excluding the row from
  evaluation. Undo now restores `status='new'` for a `rule_filtered` row with no verdict. Both the
  forward and undo passes now only touch `filter_source='manual'` rows, so propagating a manual
  reject (or its undo) across a chain never clobbers or wipes a sibling already auto-failed by a
  `filters.yaml` rule (`rule:<name>`).
- **`repost_decided` siblings are now self-correcting (`skip_decided_reposts`, `pipeline.py`).** A
  relisting skipped because its chain had a decision was never un-skipped when that decision was
  undone — stranded at `repost_decided`, excluded from eval forever. The pass now reconciles in BOTH
  directions: `new → repost_decided` when the chain is decided, and `repost_decided → new` when the
  chain decision is gone, so undo (of `applied`/`passed`/`reject`) re-queues the sibling on the next run.
- **Location normalization is comma-aware (`_norm_location`, `pipeline.py`; `_NORM_VERSION`/schema).**
  The fingerprint missed within-LinkedIn relistings whose location label drifted ("Rochester, New
  York Metropolitan Area" vs "Rochester, NY"). `_norm_location` now parses the raw `City, State,
  Country` structure: drops the country, then strips metro cruft from the **trailing** (state/region)
  component and maps a full state name → 2-letter abbrev, while leaving the city verbatim (so "New
  York, NY" isn't mangled to "ny ny"). A one-time `_recompute_fingerprints` (gated on `PRAGMA
  user_version`) re-derives `norm_company`/`norm_title`/`fingerprint` for all rows so old rows and new
  inserts share a key space (`repost_of` links are left as-is). Verified against the live DB: exactly
  one real repost group merges (an ECLARO relisting), zero over-collapse across the full table. Added
  an `idx_repost_of` index (chain resolution is now per-decision) and raised the SQLite connect
  `timeout` to 30s so a concurrent open during the recompute waits rather than erroring.
  *(Metro-cruft stripping is kept to the tail on purpose: `area`/`region` are ordinary words inside
  real city names — "Capital Region", "Bay Area" — so stripping them from city components would
  over-collapse distinct places, the worse error. LinkedIn metro labels in the city slot ("Greater
  Boston") are left as a known under-match.)*
- **Web UI decision route hardened (`api_decision`, `app.py`).** The only state-changing route
  (`POST /api/decision`) had no CSRF protection and parsed any body via `get_json(force=True)`,
  so a cross-site `text/plain` "simple request" could corrupt triage state. It now refuses a
  mismatched `Origin` (cross-site) and requires real `application/json` (forcing a CORS preflight a
  cross-site page can't satisfy).

### Decisions worth noting
- **Location normalization stays conservative on state-present-vs-absent.** "New York, NY" and
  "New York, United States" are *not* collapsed — that residual would require dropping a present
  state, reintroducing the same-city-different-state false-repost risk the exact-match design avoids.
  Per the documented cost asymmetry, a false "ALREADY APPLIED" (skip a real job) is the worse error,
  so under-matching here is the intended trade.
- The fingerprint recompute re-derives the normalized columns (`norm_company`/`norm_title`/
  `fingerprint`) but leaves existing `repost_of` links as-is (consistent with the original backfill —
  historical rows aren't retro-cross-linked).
  The fix takes effect for *future* relistings matching against the recomputed history.

---

## 2026-06-29 — second source: Adzuna API (multi-source provenance)

### Why
The pipeline had only one working source (LinkedIn). Probing the obvious additions showed Indeed,
Glassdoor, ZipRecruiter, and Google Jobs are all behind Cloudflare/anti-bot walls from a normal IP —
swapping scrapers won't beat that. Adzuna offers a **sanctioned free REST API** (no scraping, no
blocking) that returned 2,477 matches on a single probe, so it's added as a second source feeding
the same dedup → salary-filter → hard-filter → eval → report path.

### What changed
- **New `source` column on `jobs`** (`TEXT`, `'linkedin'` | `'adzuna'`) — added in the `CREATE TABLE`
  and idempotent `_migrate` (`pipeline.py`); existing rows backfill to `'linkedin'`. `fetch_new_jobs`
  now stamps `source='linkedin'`.
- **New `fetch_adzuna(cfg, conn)` (`pipeline.py`)** — called in `run` right after `fetch_new_jobs`,
  before the filters. Queries the Adzuna API (stdlib `urllib`) for every search with an `adzuna:`
  block, maps results onto the same row shape (reusing `_norm_company`/`_norm_title`/`_fingerprint`/
  `_find_repost`), and inserts as `status='new'`, `source='adzuna'`. Dedup is best-effort across
  sources (see Decisions) — URL-level always holds; the content fingerprint only collapses a
  LinkedIn↔Adzuna duplicate when both render the same company+location+title.
- **Predicted-salary guard** — Adzuna may return an ML-predicted salary (`salary_is_predicted`).
  Those are stored as NULL so the deterministic salary filter never rejects a real job on an estimate;
  only genuinely-posted salaries are kept.
- **Thin-text flag** — Adzuna descriptions are capped at 500 chars. A new `_source_tag` marks Adzuna
  rows in the report (scored, gate-fail, manual, hard-filtered sections); the web UI (`app.py`
  `row_to_dict` + `templates/index.html`) shows a `source: adzuna · 📋 500-char snippet` marker.

### Decisions worth noting
- **Cross-source dedup is intentionally limited.** The content fingerprint is `norm_company |
  norm_location` + exact title, and Adzuna's location strings differ structurally from LinkedIn's
  ("Grand Central, Manhattan" vs "New York, NY"), so the same role on both sources usually does *not*
  collapse — it appears once per source. We deliberately did **not** loosen the match to
  company+title-only: the original fingerprint matching was backtested to *avoid* false reposts
  (distinct roles sharing a generic title), and a false "ALREADY APPLIED" banner makes you skip a job
  you should apply to — a worse failure than seeing a role twice. URL-level dedup and same-source
  fingerprinting are unaffected.
- Adzuna's own `salary_min` API param is deliberately **not** used — it would filter on predicted
  salaries. The existing `apply_salary_filter` handles per-search `min_salary` on real salaries only.
- Adzuna is fetched newest-first (`sort_by=date`) and only the first page (≤`results_per_page`) is
  pulled per query — a deliberate cap mirroring LinkedIn's `results_per_search`, not full pagination.
- Adzuna can't parse LinkedIn boolean syntax, so queries are described per-search with Adzuna's
  `what_phrase`/`what_or`/`what_exclude` params; OR-of-phrases is a *list* of query blocks (one API
  call each), since Adzuna allows only one `what_phrase` per call.
- Thin 500-char descriptions mean Adzuna rows often score `ai_artifact_depth == 0`, which the guide's
  load-bearing "50/0" rule caps to RECRUITER_ONLY — a safe default for low-context postings.

### Where (files touched)
- `pipeline.py` — `source` column + migration/backfill; `fetch_new_jobs` source stamp; new
  `fetch_adzuna` + `_adzuna_search`; `run` wiring; `_source_tag` + report annotations.
- `app.py` — `row_to_dict` passes `source` through.
- `templates/index.html` — `card()` renders the source/thin-text marker.
- `config.yaml` / `config.example.yaml` — `settings.adzuna` block + per-search `adzuna:` blocks.

### How we verified
- `stats` ran the migration (`source` column added + backfilled) once, idempotently.
- `run` fetched Adzuna postings (`source='adzuna'`), with reposts of seen LinkedIn roles detected.
- Predicted-salary rows stored NULL salary; report/UI show the Adzuna marker; `backtest_v2.py` passes.
- No-key fallback: with credentials unset, `fetch_adzuna` no-ops and the run completes LinkedIn-only.

---

## 2026-06-29 — skip eval & flag relistings of already-decided roles

### Why
When LinkedIn relists a job the user has already applied to (or passed/rejected) under a fresh URL,
dedup correctly links the relisting to its canonical original (`repost_of`), and the markdown report
flags it via `_repost_info`. But the **web triage UI** only read each row's *own* `app_status` —
which is NULL on a relisting (only the canonical carries the decision) — so an already-applied job
re-surfaced as a fresh card with no warning, and the backlog query (`WHERE app_status IS NULL`) let
it back into the triage list. These relistings also burned a *paid* eval every time, despite a known
outcome (example: `4434454595`, a relisting of applied `4431753799`).

### What changed
- **New pre-eval pass `skip_decided_reposts` (`pipeline.py`)** — runs after the salary/hard-filter
  passes, before the paid eval. A `status='new'` relisting whose canonical original is already
  decided (`app_status` set, or `filter_source` set for a reject) is moved to the new terminal
  status **`repost_decided`**, which `evaluate_new_jobs` skips. Decisions always propagate to the
  canonical (`_chain_targets`), so the canonical is authoritative for the whole chain. Adds the
  `repost_decided` value to the `jobs.status` enum comment (no new column).
- **Web UI chain-effective decision (`app.py`, `templates/index.html`)** — every view query LEFT
  JOINs the canonical original; the client derives an *effective* status (own decision, else the
  chain's). A relisting now shows an "↻ already applied/passed/rejected" chip, renders read-only,
  and the backlog view excludes decided-chain relistings (covering legacy rows already evaluated
  before this change).

---

## 2026-06-28 — management-drift assistive flag

### Why
A day's exploration surfaced a recurring false positive: **"Program Manager" / "AI Program
Manager"** postings that pass all six gates, max out *both* starred AI lines (the role is genuinely
AI-adjacent), and land at 12–14/18 → PASS — yet the substance is vendor coordination, governance,
and adoption-driving. The role is *management of* AI delivery, not *doing* it: a trajectory mismatch
for an IC builder. Structurally the same leak as the 50/0 finding (a real screen-out hiding in
scorecard lines with no verdict cap), here in `title_trajectory` / `learning_value`. The user
triages passes manually and does not want these auto-hidden, so the fix surfaces rather than filters.

### What changed
- **`evaluation_guide.md` — `title_trajectory` row** gains a "Management-drift watch" clause: a
  Program-Manager-family / coordination title with no hands-on build verbs ("architect," "build,"
  "develop against," "integrate") in the responsibilities block scores `title_trajectory` 0–1 and
  emits a `management-drift` flag.
- **`evaluation_guide.md` — starred-line rules** gains a "Management-drift (assistive flag, not a
  cap)" note documenting the pattern and that it surfaces (flag + honest `title_trajectory`) without
  changing the verdict.

### What did NOT change
- **No verdict/routing change, no schema change, no code change.** The verdict stays PASS; the flag
  renders as a `⚠️ management-drift` line in the report (existing `flags` plumbing). This is
  deliberately *not* a code-enforced cap (unlike the `ai_artifact_depth` 50/0 line) until the
  pattern proves structural over more data — at which point it can be promoted.

---

## 2026-06-21 — hard-requirement filters + manual reject

### Why
DeepSeek Flash (the cheap default evaluator) **under-filters** by design — it occasionally
passes a posting that misses a hard requirement (security clearance, US citizenship, a 10+
year floor, contract-only). The candidate needed a way to (1) apply *their own* hard-fail
verdict when they catch a miss, distinct from the softer `passed`, and (2) turn that catch
into a cheap deterministic rule so the same requirement is caught automatically next time —
without paying for a stronger model.

### What changed
- **`reject` command** — `python pipeline.py reject --url X --gate <name>` records a manual
  hard-fail override (new `filter_source='manual'` + `filter_gate` columns). It keeps the
  model's original verdict (so the report can flag "model under-filtered" when you overrule a
  PASS), pulls the posting out of cold-apply, and propagates across the repost chain like
  `applied`/`passed`. `--undo` clears it.
- **Deterministic rules (`filters.yaml`)** — a new `apply_hard_filters` pass runs **before**
  the paid eval (mirroring `apply_salary_filter`): any new posting whose title/description
  matches a rule is set `status='rule_filtered'`, `verdict='GATE_FAIL'`, and **skipped by the
  evaluator** — so it costs nothing. A pattern is a case-insensitive substring unless prefixed
  `re:` (regex).
- **Assisted authoring** — `reject --pattern P` promotes the catch into `filters.yaml` under
  the gate's rule, first printing the matching sentence and **how many existing postings P
  would also match** (false-positive preview). De-dupes identical patterns.
- **Auditable report section** — `🚫 Hard-fail filters (your rules + manual rejects)` lists
  rule- and manually-failed postings tagged with source + gate, kept out of the verdict
  sections so they don't double-appear; an over-aggressive rule stays visible, not silent.
  Summary header + `stats` gained hard-filter counts.

### Decisions worth noting
- **Rules live in a dedicated `filters.yaml`, not `config.yaml`.** The tool appends to it
  programmatically; keeping it separate means the hand-commented `config.yaml` is never
  rewritten. Rules carry `note`/structure as data (no YAML comments to lose on round-trip).
- **Matcher: phrases by default, `re:` for regex.** Simple for the common case (clearance,
  citizenship), powerful when needed (numeric year floors), no regex tax on quick edits.
- **Pre-eval, not post-eval.** Running the deterministic filter before the model both saves
  API spend and makes the override authoritative regardless of what the model would say.
- **Manual reject keeps the model verdict** rather than overwriting it, so the cheap model's
  under-filter rate stays measurable.

### Where (files touched)
- `pipeline.py` — `filter_source`/`filter_gate`/`filter_date` columns + migration;
  `load_filters`/`save_filters`/`apply_hard_filters` and the `_pattern_matches`/`_rule_hit`
  matchers; `reject` command with `_resolve_posting`/`_chain_targets` factored out of
  `cmd_mark`; `apply_hard_filters` wired into the `run` sequence; report grouping + Hard-fail
  section; `stats` breakdown.
- `filters.example.yaml` — **new** template; `filters.yaml` gitignored.
- `README.md` — `reject` in Commands + new "§7 Hard-fail filters".

### How we verified
- Migration added the three columns on the live `jobs.db` and was idempotent on re-run.
- Offline: the substring + `re:` regex matchers (incl. a malformed regex → safe no-match);
  `apply_hard_filters` flags a clearance posting (`rule_filtered` + `GATE_FAIL`) and leaves a
  non-matching one `new`; the matched row is **excluded from the evaluator's `status='new'`
  set** (cost short-circuit confirmed).
- `reject` on a temp DB: manual override propagates across a repost chain, prints the
  false-positive count + matched sentence, appends the pattern to `filters.yaml`; `--undo`
  clears it. Report places a rule-filtered and a manually-rejected former-PASS only in the
  Hard-fail section (PASS stays in cold-apply) with the "model under-filtered" note.
- Regression: repost detection and `applied`/`passed` rendering unchanged alongside the new
  override (the backtest's absolute count tracks DB growth, not a logic change).

---

## 2026-06-21 — application-status lifecycle (applied / passed / backlog)

### Why
The repost feature (below) added a binary `applied` flag, but in practice not every
fetched job gets triaged in a day: a few links get opened, some get applied to, and some
get **rejected after human evaluation**. "Not applied" was conflating two opposite cases —
**passed** (reviewed, decided no → a repost should be *muted*, not re-triaged) and
**backlog** (never got to it → a repost should still show, you may apply later). The binary
flag couldn't tell them apart, so every repost of a role you'd already rejected came back
looking fresh.

### What changed
- **`applied` (boolean) → `app_status` (lifecycle).** A single column with values
  `NULL` (backlog/default), `applied`, or `passed`, plus `status_date`. The untouched
  default *is* the backlog, so no separate "viewed" state is needed (and a static markdown
  report can't detect link clicks anyway).
- **New `passed` CLI verb.** `python pipeline.py passed --url <full-or-substring>` mirrors
  `applied`; both take `--undo` to clear a mis-mark. Decisions propagate across the repost
  chain to the canonical original, same as before.
- **Report treatment, with `applied` > `passed` precedence.** Applied → the existing loud
  `🚫 ALREADY APPLIED`; passed → a quiet `↩ You reviewed & passed on <date>` note, and the
  job **stays visible** (non-destructive — you can still change your mind). Reads the row's
  *own* status too, so re-running `report` after marking same-day postings declutters
  today's report, not just future reposts. Header gained a "previously passed" count;
  `stats` gained an `app_status` breakdown.

### Decisions worth noting
- **Single enum, not two booleans.** A controlled vocabulary makes a future funnel state
  (`interviewing`, `rejected`, …) a one-line addition rather than another migration.
- **Passed reposts stay visible (muted), not hidden.** Lowest-regret default; switching to
  hide / separate-section later is a localized `generate_report` edit.
- **Manual CLI, no click auto-tracking.** Auto-capturing clicks would need a local redirect
  server and still couldn't distinguish applied from passed — that decision only exists in
  the user's head.

### Where (files touched)
- `pipeline.py` — only file changed: `app_status`/`status_date` in `CREATE TABLE`;
  `_migrate()` adds them and `_migrate_applied_to_status()` folds the old `applied` flag in
  then drops the dead columns (`DROP COLUMN`, guarded for SQLite < 3.35); `cmd_applied` →
  generalized `cmd_mark(conn, url, status)`; `applied` + new `passed` subcommands with
  `--undo`; `_repost_info` / `_repost_tag` / report header / `cmd_stats` updated.

### How we verified
- Migration ran on the live `jobs.db`: added the two columns, folded `applied` (0 set rows
  → all 2,677 land in backlog), dropped the old columns; a second `stats` run was a clean
  idempotent no-op.
- CLI on a temp DB: `applied`/`passed` set status + date and propagate to the canonical
  original; `--undo` clears; **precedence holds** (passed-then-applied on one chain renders
  ALREADY APPLIED).
- Report render of four chains — applied / passed / backlog / brand-new — produced
  `🚫 ALREADY APPLIED` / `↩ passed (visible)` / normal / normal respectively; marking a
  same-day non-repost `passed` and re-rendering muted it (no false repost line).
- Repost-detection backtest re-run: still **212** flagged, unchanged by the status work.

### Migration / operational notes
- `jobs.db` is the single source of truth and is gitignored — the in-place column
  migration is non-tracked. The old `applied`/`applied_date` columns are removed where the
  SQLite build supports `DROP COLUMN`; on older builds they're left in place, unused.

---

## 2026-06-21 — repost-aware dedup + applied tracking

### Why
Dedup was purely `INSERT OR IGNORE` on the `job_url` PRIMARY KEY. LinkedIn mints a
**fresh job ID/URL every time a role is reposted**, so a relisting of a job already in the
database — or one already *applied to* — sailed through as a brand-new row, got
re-evaluated, and landed in the daily report indistinguishable from a genuinely new
opening. The concrete risk: a **double-apply** to the same role under a different URL. The
schema had no content fingerprint and no notion of which postings had been applied to.

### What changed
- **Content fingerprint dedup.** Added a content-identity layer on top of the existing
  URL dedup (URL `INSERT OR IGNORE` still stands). A posting is matched to a prior one via
  a `company|location` **blocking key** plus an **exact normalized-title** match, so the
  same role is recognized across the URL churn of a repost. Normalization folds case,
  punctuation, company suffixes (LLC/Inc/…), and Sr/Jr→Senior/Junior, so cosmetic drift
  still matches while a different qualifier (the role-distinguishing word) does not.
  Reposts are **flagged, not suppressed** — they still insert and evaluate, consistent
  with manual triage.
- **`applied` flag + CLI.** New `python pipeline.py applied --url <full-or-substring>`
  marks a posting applied-to (sets `applied` / `applied_date`) and propagates to the
  canonical original of a repost chain, so the whole group is covered.
- **Report markers.** Gates-passed jobs show a `↻ Repost — original first seen … prior
  verdict …` line; any role whose repost chain has been applied to gets a loud
  `🚫 ALREADY APPLIED` banner. Gate-fail / manual one-liners get a compact `↻ repost` /
  `🚫 ALREADY APPLIED` tag. The summary header counts reposts and applied-reposts.

### Decisions worth noting
- **Match key is company + title + location** (not URL/ID). Location stays in the
  fingerprint, so a relisting in a different city counts as a distinct role.
- **Exact title match, not fuzzy — decided by a backtest, reversing the initial design.**
  The first cut used fuzzy title similarity (threshold 0.72). A backtest over the real
  2,677-row DB exposed it collapsing **1,598** pairs, the bulk of them *distinct* roles
  sharing a generic core — `Workday Business Analyst` vs `SalesForce Business Analyst`,
  `Legal Engineer (Corporate)` vs `(In-House)`. The cost asymmetry runs the *opposite* way
  from the initial assumption: a false `ALREADY APPLIED` banner on a genuinely new role
  makes you **skip a job you should apply to**, so false positives are harmful, not benign.
  Real reposts keep the title verbatim. Switching to exact normalized-title match dropped
  the flagged set to **212** clean, genuine relistings with no distinct-role collapses.
- **Known residual limitation:** aggregator/placeholder "companies" (`Jobright.ai`,
  `RemoteHunter`, `Confidential`) with empty locations and generic titles can still
  conflate two different underlying jobs — the real employer is hidden, so no fingerprint
  can separate them. Acceptable given flag-not-suppress + manual triage.
- **No new dependencies.**

### Where (files touched)
- `pipeline.py` — six new columns (`norm_company`, `norm_title`, `fingerprint`,
  `repost_of`, `applied`, `applied_date`) in `CREATE TABLE` + idempotent `_migrate()` with
  `_backfill_fingerprints()` and a `fingerprint` index; new normalization helpers and an
  exact-match `_find_repost()`; repost detection wired into `fetch_new_jobs()`'s insert
  loop; new `cmd_applied()` + `applied` subcommand; report gained `_repost_info()` /
  `_repost_tag()` and the markers above. *(Only file changed; no config/dependency edits.)*

### How we verified
- `_migrate()` ran against the existing `jobs.db`, added all six columns, and backfilled
  fingerprints for **2,677 existing rows**.
- **Backtest over the real DB (the decisive test).** Fuzzy matching flagged 1,598 pairs,
  manual inspection showing most were distinct roles sharing a generic core — which drove
  the switch to exact matching. Exact normalized-title matching flagged **212** reposts,
  every sampled one a genuine same-title relisting (`Data Analyst @ AARATECH`,
  `Forward Deployed Engineer … @ [an AI-recruiter agency]`, `SR HRIS ANALYST @ RemoteHunter` across days).
- Offline `_find_repost`: an identical-title repost matched its original across
  company-suffix drift (`Acme Corp` → `Acme Corp, LLC`), location-format drift
  (`Austin, TX` → `Austin TX`), and punctuation drift (`…, AI` → `… - AI`); a reworded
  title and a different company both correctly returned no match.
- End-to-end report render showed both banners on a repost and nothing on a genuinely new
  role; the `applied` CLI's substring resolution, chain propagation, ambiguity, and
  no-match paths all behaved.

### Migration / operational notes
- Existing rows are backfilled with fingerprints but **not** retroactively cross-linked
  (`repost_of` stays NULL for history), so past reports render unchanged. Repost detection
  applies on the next `python pipeline.py run`, matching new fetches against full history.
- `jobs.db` and `reports/` are gitignored; the in-place column migration is non-tracked
  and non-destructive (additive columns only).

---

## 2026-06-19 — v2 evaluation framework (the "50/0" fix)

### Why
Applying the v1 framework produced an initial batch of cold applications with no conversions. The framework scored roles correctly *as fits* but couldn't tell whether an
application would *clear the screen*. Two structural blind spots:

1. **One AI score did two jobs.** "Is this applied AI, not research?" was tangled with
   "can my current artifact *evidence* the required AI depth?" A role can be genuinely
   applied-AI **and** require a depth a generation ahead of the shipped artifact
   (low-code AI Builder + Power Automate classification). v1 scored those 15–16/18 and
   said APPLY.
2. **A high total overrode a known screen-out.** The "your artifact is classification,
   not orchestration" signal was present but never load-bearing — the total kept winning.

### What changed
- **Split the AI score.** `ai_depth_realism` → two separate dimensions:
  `ai_applied_vs_research` (is the *role* applied vs. research) and `ai_artifact_depth`
  (does the *shipped artifact* evidence the role's **required** depth). Dropped
  `domain_transferability`. Total still **/18** (6 dimensions × 3).
- **New verdict `RECRUITER_ONLY`.** Triggered when all gates pass but
  `ai_artifact_depth == 0`, **regardless of total** — a hard cap, so a 17/18 with depth 0
  routes to a human instead of dying in an ATS. Verdicts are now
  `PASS` / `RECRUITER_ONLY` / `GATE_FAIL`.
- **`bucket` field (1/2/3).** Channel routing: 1 = required AI depth a generation ahead
  (recruiter/referral only), 2 = acceptable-tier BI/BA (cold-apply where the title gap is
  small), 3 = clean low-code / Power Platform AI delivery (cold-apply, realistic
  conversion).
- **Recruiter-only report section.** Gates-passed-but-depth-0 roles surface under
  "🤝 Recruiter-only — route to a human," not buried as skips.
- **Sharpened the tool-requirement / artifact-depth boundary.** An agentic/orchestration
  *depth* gap is **buildable** — it CLEARS the tool gate and routes via the
  `ai_artifact_depth` cap to RECRUITER_ONLY. The tool gate is reserved for a *named tool
  with years attached* that's genuinely non-rampable. (Found during backtest: an
  agentic-engineer role was wrongly failing the tool gate where a structurally identical
  AI-startup SE role passed it.)

### Decisions worth noting
- **Kept the `employment_type` gate.** The new guide draft listed only 5 gates (dropped
  it), but `profile.md` requires permanent full-time, so dropping a working gate would be
  a regression. Folded back in as the 6th gate.
- **The depth-0 cap is enforced in code**, not just instructed in the prompt
  (`pipeline.normalize_result`) — the load-bearing rule can't depend on the model
  complying.

### Where (files touched)
- `evaluation_guide.md` — rewritten to the v2 standard (split AI lines, Part 2.5 bucket +
  channel routing, RECRUITER_ONLY verdict, tool-gate disambiguation, Bucket 1 worked
  example). *(Private; the committed `evaluation_guide.example.md` is the sanitized version.)*
- `pipeline.py` — new `SCORE_DIMS`/`VERDICTS` constants; `bucket` column in
  `CREATE TABLE` + idempotent `_migrate()`; rewritten system prompt; new
  `normalize_result()` (enforces the depth-0 cap + bucket defaults); `evaluate_new_jobs`
  stores `bucket`; report gained `_render_scored_job()` + the recruiter-only section.
- `backtest_v2.py` — **new** (local-only; gitignored, since it reads the private `jobs.db`).
  Re-evaluates known postings and asserts expected verdicts.
- `compare_models.py` — applies `normalize_result` so cross-model verdicts match prod;
  counts RECRUITER_ONLY; shows `bucket` in disagreements.
- `README.md` — "Reading the report" section documents the new verdict + buckets.

### How we verified
- `_migrate()` ran against the existing 1,970-row `jobs.db` and added the `bucket` column.
- Unit-checked `normalize_result` across all routing cases (depth 0 at high total → cap;
  depth 3 → bucket 3; depth 2 → bucket 2; gate fail → nulls).
- **Backtest (local `backtest_v2.py`), all 3 cases matched:**
  - an AI-startup Solutions Engineer (agentic/SDK depth required) → `RECRUITER_ONLY` (bucket 1, depth 0)
  - an "AI Agent Engineer" role (production agentic systems) → `RECRUITER_ONLY` (bucket 1, depth 0)
  - a Power Platform delivery role (low-code AI) → `PASS` (bucket 3, depth 3)

### Migration / operational notes
- Existing rows keep their v1 verdicts; legacy reports still render (no recruiter-only rows
  on past dates). The v2 framework applies on the next `python pipeline.py run`.
- No wholesale re-evaluation of the back catalog (passes are triaged manually).
