# Changelog

Revision log for the job-search pipeline's **evaluation framework** — the guide, the
schema, and the scoring/routing logic. Append a new dated section on top for each
substantive change. Day-to-day search-term edits in `config.yaml` don't belong here;
changes to *how postings are judged* do.

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
