# Changelog

Revision log for the job-search pipeline's **evaluation framework** — the guide, the
schema, and the scoring/routing logic. Append a new dated section on top for each
substantive change. Day-to-day search-term edits in `config.yaml` don't belong here;
changes to *how postings are judged* do.

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
  `Forward Deployed Engineer … @ Jack & Jill`, `SR HRIS ANALYST @ RemoteHunter` across days).
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
Applying the v1 framework produced ~50 primary-tier cold applications and **zero**
interviews. The framework scored roles correctly *as fits* but couldn't tell whether an
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
