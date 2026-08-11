# Changelog

Revision log for the job-search pipeline's **evaluation framework** — the guide, the
schema, and the scoring/routing logic. Append a new dated section on top for each
substantive change. Day-to-day search-term edits in `config.yaml` don't belong here;
changes to *how postings are judged* do.

---

## 2026-08-11 — title_trajectory: one-rung functional-lateral scores 2

**Scoring calibration (user decision, 2026-08-11).** The law-firm AI-analyst family (Godfrey &
Kahn *AI Solutions Analyst*, 2026-08-10; Steptoe & Johnson PLLC *AI Solutions Analyst*,
2026-08-11) posed the same swing line twice: SA → Analyst drops one title rung (reports to a
manager, supervises none, strategy ownership explicitly carved out), but the seat's substance —
configuration, integration, testing, runbook documentation — is the current operating level.
Decision: that shape scores `title_trajectory` **2** (functional lateral), not 1 (de-level).
Junior-coded BI/reporting de-levels and ≥2-rung reaches stay 0–1; the management-drift and
enablement-cluster 0–1 rules are untouched.

- Scoring-table cell amended in BOTH hand-synced guide copies (pipeline `evaluation_guide.md`
  + the Downloads Claude-Project copy) the same day — the known bidirectional guide-divergence
  failure mode.
- **The pipeline judge already scored both postings 2** (17/18 and 16/18 PASS), so this
  ratifies existing pipeline behavior; the drift being closed was the manual/Project-side
  scoring, which had them at 1 (14/18, under the cold-apply bar). No code change, no backfill,
  no expected verdict movement on re-eval.
- Effect: both manual evals move 14 → 15 and clear the fit ≥ 15 cold-apply bar. Freshness
  verified from the fetch window rather than the LinkedIn UI: `hours_old: 4` bounds posting
  time to ≤4h before `first_seen` (G&K 2026-08-10 17:00, Steptoe 2026-08-11 09:22).

## 2026-08-10 — employment-type gate: fully-remote contract exception

**Judgment change (user decision, 2026-08-10).** Contract engagements are now acceptable when
the seat is **fully remote** — relocating or commuting for a temporary seat stays disqualifying.
Contract/contract-to-hire/temporary/fixed-term (including staffing-agency W2) passes the gate
only when the posting states a fully remote arrangement; on-site, hybrid, "remote or hybrid,"
and arrangement-unstated all still FAIL (ambiguity fails closed). Part-time and internship/co-op
remain unconditional fails; the unstated-employment-TYPE default-PASS is unchanged. A passing
remote contract carries a `remote-contract` flag in `flags` for triage (compare hourly rates
annualized minus a benefits gap; a pre-October start needs outside-employment clearance).

- Gate text amended the same day in BOTH hand-synced copies — the pipeline's
  `evaluation_guide.md` and the Claude Project copy in Downloads — to head off the known
  bidirectional guide-divergence failure mode. The ITMC worked example's contract aside got a
  dated annotation.
- Enforcement layer: this gate lives in the LLM eval only. `filters.yaml` never had a contract
  rule, so there is no deterministic-filter change and no code change.
- **Backfill:** 5,584 employment_type GATE_FAIL rows predate the amendment. The remote-signaled
  ∩ contract-worded ∩ ≤45-day ∩ undecided subset (305 rows → 277 chains after folding chain
  siblings and dropping 3 chains already holding PASS/RECRUITER_ONLY) was requeued: one
  best-evidence representative per chain (full-text source over Adzuna snippet, then longest
  description) reset to `status='new'` with its old verdict retained, riding
  `skip_evaluated_reposts`' manual-reset carve-out (`verdict IS NULL` guard) so the forward
  skip pass leaves them for the next run's re-eval. Remote contracts whose stored text carries
  no detectable remote/contract signal (Adzuna's 500-char truncation) stay dead — accepted.

## 2026-08-10 — function-precedent cap moves from the guide into code (`core_function`)

**Routing change.** The function-precedent cap (added 2026-07-25) was guide-enforced only,
phrased as "cap the verdict at RECRUITER_ONLY." Measured against the 2026-06-15..08-10 corpus
it fired on **96 of 924** gates-passed fit ≥ 15 rows in the pre/post-sales family — about 10%.
Nolro *Founding Solutions Engineer* scored 3/3 on all six dimensions with the cap unfired,
while Legora and Evolver at the same total were capped correctly; the rule was not drifting,
the judge simply was not applying it. That family is **20.7% of the entire cold-apply band**
(828 of 4000 postings), and because those rows carry high fit they sort to the top of
`fresh_strong` — the defect displaces correct candidates rather than merely adding noise.

- **Diagnosis:** the two caps that hold (`ai_artifact_depth == 0`, `formal_leadership_required`)
  ask the model only to REPORT a fact and let code route. The function-precedent rule asked it
  to overturn scoring it had just produced. Same guide, same model, different reliability.
- **New output field `core_function`** — a closed vocabulary in `states.py`
  (`ALL_CORE_FUNCTIONS`): `presales_demo`, `post_sales_delivery`, `quota_carrying`,
  `people_management`, `consulting_delivery`, `internal_build`, `other`. Extracted from the
  responsibilities, explicitly WITHOUT reference to the candidate's history.
- **`states.NO_PRECEDENT_FUNCTIONS`** holds the capped subset (the first four).
  `evaluation.normalize_result` caps those to RECRUITER_ONLY / bucket 1 — the third
  code-enforced cap, sharing the existing caps' branch.
- **`consulting_delivery` is deliberately NOT capped**: Big 4 / SI engagement delivery is an
  active target track. Widening the capped set is a judgment change and belongs here with
  evidence.
- **Fails OPEN** on a missing, empty, non-string, or unrecognized value, matching the
  leadership cap: every eval_json written before this field lacks the key and `backtest_v2`
  re-normalizes stored rows, so capping on absence would bucket-1 the whole history. An
  unrecognized string is normalized to `None` and logged — never guessed toward a member.
- **Bucket semantics:** a capped role gets `bucket = 1` even when `ai_artifact_depth == 3`,
  following the leadership cap's existing precedent. Bucket 1 therefore now means "recruiter/
  referral only" for three distinct reasons; the specific one is recoverable from `eval_json`.
  This slightly deflates bucket-3 counts going forward — a reporting consequence, accepted.
- Guide rewritten to match: the model extracts, the code caps, and it must NOT deflate the fit
  total to express a precedent gap.
- Tests: 8 cases in `tests/test_eval_routing.py` (each capped function, the two non-capped
  ones, fail-open on absence/non-string, no-guessing on unknown strings, and that the cap
  cannot resurrect or re-bucket a GATE_FAIL row).

`backtest_cases.local.json` was checked before running: 1 of its 10 cases is in the family
(Augment *Solutions Engineer*) and already accepts `RECRUITER_ONLY`, so no baseline edit was
needed. Should a future presales/delivery case flip from `"PASS"`, that flip is the intended
behavior — update the case rather than reverting the cap.

`canary.py` was rebaselined (its documented trigger: an accepted guide change). A diagnostic
run against the 08-08 baseline first, to catch over-firing before anchoring: 71% agreement, no
alerts, and **none of its 7 flips carried the cap's signature** — the cap can only move a
verdict toward RECRUITER_ONLY, and 4 of the 7 moved the other way. Recorded so the next
reader does not repeat the mistake: the canary compares run-to-run, NOT against the stored
`jobs.verdict`, so "the sentinel I expected to flip didn't" was a prediction error, not a
cap failure — evaluating that sentinel (Databricks *Sr. Field TPM, Forward Deployed*)
directly returns `core_function='post_sales_delivery'` → RECRUITER_ONLY / bucket 1. New
baseline: fit_mean 9.62, 23 of 24 sentinels scored.

---

## 2026-08-10 — read-only Outlook job-alert shadow report (no judgment change)

- Added an opt-in `email-shadow` command backed by delegated Microsoft Graph `Mail.Read` and
  the Windows authentication broker. It queries only exact configured alert senders, extracts
  bounded Indeed/Lensa/Adzuna/Glassdoor/Robert Half job-detail anchors, and compares them with
  `jobs.db` through a SQLite read-only/query-only connection. Locally encoded tracking
  destinations can be unwrapped without requests; opaque trackers are never followed.
- The output is a gitignored daily discovery report only. The command does not alter mailbox or
  job state, follow posting links, insert roles, or invoke the LLM. Exact URL matches are distinct
  from uncertain title-only hints so the shadow measurement cannot claim false incremental yield.
- First authentication is explicit (`email-shadow --login`); scheduled runs never open login UI.
  No client secret or token cache file is stored by the project; Windows' broker owns refresh.
- A bounded 30-day run now acts as a historical overlap backtest, with candidate-bearing email,
  candidate-link, exact-URL, possible-title, and unseen-URL counts broken out by provider. The
  report explicitly avoids treating those counts as causal first-discovery evidence.
- Graph pagination is bound to the original folder, exact sender, time window, selected fields,
  order, and page size before any next page is requested. Provider route allowlists accept only
  observed job-detail shapes, so same-domain preference, search, and profile pages cannot pollute
  the historical counts.

---

## 2026-08-09 — Dice as the fourth posting source (`fetch_dice`)

- **New fetcher for Dice search pages** — public, logged-out, no keys; the job list is
  embedded in the page HTML as an escaped Next.js flight payload. Per-query like Adzuna
  (`dice:` phrase block per search, `settings.dice` for the knobs), wrapped by
  `_run_fetch_stage` after `fetch_ats`, inserting `status='new'` rows through the shared
  posting-store path. No schema change; `source='dice'` is a new provenance value.
- Why: an overlap probe (`tests/validation/dice_overlap_probe.py`, 2026-08-09) measured
  ~210 new company+title keys per 7-day window against the existing three sources — 78
  keeping the strictest cut (primary-tier phrases, pure full-time) — including
  forward-deployed/SA-AI roles at direct employers the LinkedIn queries never surfaced.
  54% of Dice's population was already-known chains, confirming it also serves as
  cross-post evidence rather than pure novelty.
- Evaluation-relevant mechanics: the search payload carries no JD, so each genuinely new
  URL costs one detail-page fetch (known URLs are skipped first; a posting whose detail
  page yields no JD is NOT inserted — an empty description must never reach the paid
  eval — and retries as still-unseen next run). That JD is read from the detail page's
  schema.org JSON-LD `JobPosting` block specifically, NOT as "the longest description
  string on the page": a page carries several other description values, and longest-wins
  returns one of those whenever it outgrows the JD. A substituted JD is undetectable
  downstream — it reaches the paid eval, the verdict caches onto the chain, and applying
  freezes it as immutable packet evidence — so a page without exactly one anchor yields no
  JD rather than a guess. The JSON-LD block is the anchor rather than a framework-internal
  key because it is a public contract that survives Next.js renaming its component props;
  verified against live pages on 2026-08-10, where its description reproduced byte-for-byte
  what the longest-wins reader had already stored. `postedDate` is a precise timestamp
  stored through `_ats_date` (real intra-day recency; the chain's best true-age lower
  bound). Salaries are display text → stored NULL/unstated, the Adzuna/ATS convention;
  the detail page's schema.org `baseSalary` stays unused until its provenance
  (employer-stated vs imputed) is established. `employment_exclude` (default
  `third party`) keeps the C2C staffing flood out of the DB and the eval spend — the
  probe put contract/third-party flow at roughly half of Dice's new keys.
- The search page is parsed from INSIDE the sliced `jobList` object. Key order stops
  mattering (anchoring on `"jobList":{"data":[` made "data" required to be the first key),
  and the totals are jobList's own rather than the first `totalResults` anywhere after the
  anchor — which used to pick up an unrelated widget's site-wide figure and so let a page
  that parsed ZERO rows report a healthy total.
- Health facts are fail-loud, matching the Greenhouse reader's rule that a wrong-shaped
  200 must never read as an empty board. A genuine no-results page ships the envelope with
  `totalResults: 0`; recorded as a categorized FAILURE instead are a first page with no
  usable envelope (layout change, or a 200-status block page), an envelope claiming results
  none of which parsed, a sweep where no row carries a posting URL or an employment type
  (either rename silently disables the C2C flood guard, straight into the paid eval), and
  a sweep of at least three new URLs whose detail pages all FETCH cleanly yet yield no
  description. Network failures are excluded from that last rule on purpose: a delisted ad
  stays in the posted window and is never inserted, so counting timeouts would re-fire
  every run for a week and blame the parser for a connectivity event. All of these
  previously recorded `success, returned=0` — indistinguishable from a healthy run, and a
  false green for the cooldown's "at least one target succeeded" stamp. The config knobs
  validate out loud for the same reason: `results_pages: -1` fetched nothing while
  reporting success, `results_pages: yes` quietly fetched one page, and the value is now a
  whole number bounded 1..20.
- A LATER page that comes back unusable stops the sweep with a notice instead of failing
  the query. Page 1's detail fetches are already paid for, and since a failed query inserts
  nothing, failing would have discarded them and re-bought the same rows every run.
- Detail-page fetches run BEFORE any insert, so no HTTP request or politeness sleep
  happens inside the SQLite write transaction. Dice is the only source that fetches per
  row, so inserting as it went held the WAL writer lock for minutes on a first crawl
  against a 30s `busy_timeout` — long enough to fail a triage click in the local UI or an
  overlapping scheduled run. The rows and their success fact still commit together.

- **`filters.yaml` rules can now carry `company_any` patterns**, matched against the
  posting's company name only (`any` patterns keep matching title+description — existing
  rules' semantics unchanged). The shipped example rule is `aggregator_shell`; the actual
  patterns live in the gitignored `filters.yaml`, since they name specific firms.
- Why: multi-client aggregator accounts repost other employers' roles under their own
  brand — in the observed case one account held 85 rows across 48 distinct titles with
  every location field empty. The content fingerprint (company+location+exact title) can
  never link such a shell to the real employer's posting, because the shell carries
  neither the real company nor a location, so each relisting reached the paid eval. The
  concrete trigger was a role first evaluated in July under its employer's own posting
  and re-evaluated three weeks later under a shell's relisting — one wasted eval AND two
  contradictory verdicts for the same role. Company-name *normalization* was considered
  and rejected: with many unknown client employers behind one brand there is nothing to
  normalize the shell's company to, and title-only linking is the false-repost class the
  exact fingerprint deliberately avoids (one generic analyst title appeared under 170
  companies in 14 days).
- Mechanics: same `rule_filtered`/`GATE_FAIL` stamps and attribution
  (`filter_source='rule:<name>'`) as every hard rule, so shells land in the auditable
  Hard-fail report section, repost passes see the stamp, and the eval never runs.
  `company_any` patterns get the same load-time validation. `reject --pattern` still
  writes only `any` patterns, and it will not extend a company-only rule: such a rule is
  naturally `gate: other`, which is also `--gate`'s default, so an un-gated
  `reject --pattern` would otherwise append a description pattern to a company rule.

---

## 2026-08-08 — covering indexes for the triage read paths (schema, no judgment change)

- **Five covering indexes on `jobs`** (built in the `get_db` migration path, recreated after
  a stale-CHECK rebuild like the existing three): `idx_backlog_cover`, `idx_decided_cover`,
  `idx_applied_cover`, `idx_decision_pages`, and the expression-led `idx_first_seen_day`.
  Root cause: every triage/worklist scan filters on columns stored *after* the two big TEXT
  payloads (`description`, `eval_json`), so each scan walked every row's overflow chain —
  200–700ms per Action Center queue on the 76k-row history, 3–4s per `/api/actions`, and the
  web UI refetches that after **every** pass/reject click. Covering indexes make those scans
  index-only: fresh_strong/recruiter_route 681ms → ~1ms, decided_roots CTE 243ms → ~1ms,
  interview_prep/followups ~220ms → ~0ms, date view 331ms → ~17ms. The index comments and
  the query sites (workflow.py, dupe_candidates.py) cross-reference each other: a column
  added to one of those SELECT/WHEREs must be added to its index.
- **`dupe_candidates._candidate_map` pushes the cross-source necessary condition into SQL**
  (an `eligible` CTE keeps only company+title keys seen under >1 source in the window) and
  iterates only cross-source pairs per key. Output is byte-identical on the real history
  (2,548 pairs, 365/8,273 suppressed — verified old-vs-new before landing); the section
  drops ~1.3s → ~0.4s, now dominated by the GROUP BY over the 76k-row window.
- No verdicts, gates, scores, routing, or stored rows changed — read-path speed only.

---

## 2026-08-08 — boundary-band arbitration: majority vote where a flip would change an action

- **The flip problem was re-cut by consequence before building anything.** The new
  `tests/validation/flip_consequence.py` re-reads the 08-07 noise probe by ACTION boundaries
  (cold-apply = PASS & fit≥13, recruiter-route = RECRUITER_ONLY & fit≥15): **25%** of
  evaluated postings flip *verdict* on a temp-0 rerun (Wilson 95% 16–37%), but only
  **8%** (5/60, CI 4–18%) change what the user would *do*. The other 17% is verdict/
  visibility churn on rows no action touches.
- **`evaluation.py` now arbitrates the boundary band.** A first draw landing scored with
  fit 11–17 (`ARBITRATION_BAND`) triggers 2 more draws and a majority vote: measured
  **fire ~20%** of draws, **catch ~87%** of action flips, ~$0.03 per 100 postings (input
  is cache-hit; cost is all output tokens). A strict majority replaces the first draw with
  the winning draw closest below the winners' median fit — the draw is kept WHOLE, so
  `score_breakdown` stays consistent with `fit_score`; the losing draws are recorded in an
  `arbitration` evidence block inside `eval_json`.
- **No majority = no re-route.** A 3-way split (or 1-1 when an extra draw fails) keeps the
  production draw and surfaces `eval_issues='arbitration-split'` into **Needs attention** —
  same review-not-reroute policy as the gate-contract diagnostics. GATE_FAIL first draws
  are deliberately NOT arbitrated: covering that leak (the remaining ~13% of action flips)
  measured a 73% fire rate for +6pp catch.
- **Silent-swap watch is now scheduled measurement, not annual archaeology.** The new
  `tests/validation/canary.py` freezes 24 sentinel postings (8 per verdict class, inputs
  copied into `canary_set.local.json` at init so pruning can't mutate them), re-evaluates
  them with the exact production request body, appends to `results/canary_history.jsonl`,
  and alerts (exit 2) on: verdict agreement vs baseline <60%, |paired fit delta| >1.2
  (postings scored in BOTH runs; 0731-scale drift was −1.3), token-median ratio outside
  0.6–1.6× (0731 was ×2.8), or >10% bad draws (0731 went 0→50-100 retries/day). An alert
  is a signal to run `noise_probe.py`/`backtest_v2.py`, not proof by itself.
- **The instrument's own first alert was a calibration bug, kept as a lesson.** Two runs
  30 minutes apart tripped the v1 thresholds (67% agreement vs a <70% floor; −2.88 on
  *unpaired* fit means). Both were artifacts: the floor had been set from the *population*
  noise floor (~86% pairwise), but the stratified 8/8/8 set is boundary-heavy — per-class
  pairwise agreement is GATE_FAIL 0.85 / PASS 0.83 / RECRUITER_ONLY 0.67, stratified
  expectation ~0.78, 2σ lower bound ~0.61 — and unpaired means mix in composition shift
  when postings flip in/out of the scored set (the paired delta was −0.88, inside noise).
  Thresholds are now calibrated to the set, the fit alert is paired-only, per-class
  agreement is always printed, and `--recheck` re-compares the last entry offline so
  threshold work costs nothing.

---

## 2026-08-07 — 20 pre-`gate_results` rejections flagged for review; the 07-31 step change

- The "scored yet rejected" signature was **falsification-tested before use**: if scoring
  said nothing about gates, rejections that DID name a failing gate would carry a
  `score_breakdown` too. Of **36,886** named-gate rejections, exactly **1** does (0.00%;
  0.00% for role_substance, years_floor, tool_requirement, employment_type,
  domain_requirement individually). The signature is real, not an artifact of the model
  filling in fields. Of the 21 rows it selects, 16 say in their own words that the gates
  passed and 18 argue a recruiter/referral channel.
- **The onset is a step, not a slope.** Against a flat denominator of Adzuna rows evaluated
  per day, the rate is 0.00% on every day from 07-24 through 07-30 — including 07-24, the
  highest-volume day at 1,722 rows — then **0.91% on 07-31, 0.72% on 08-01, 0.28% on 08-02**,
  decaying after. 17 of the 21 land in those three days. This is the 0731 drift with a
  countable signature: from that date the model began rejecting postings it had scored.
- `core._derive_scored_yet_rejected` marks those rows `eval_issues='scored-yet-rejected'`
  once (keyed in `meta`, never overwrites an evaluator-written value, verdicts untouched), so
  they surface in **Needs attention**. On the real database: 20 rows flagged, 0 named-gate
  rows wrongly caught, migration 0.7s.
- **Re-evaluating them was considered and rejected.** All 21 still have their stored text and
  none has been decided, so a requeue was possible — but 20 of 21 are Adzuna rows whose
  description sits at the documented 500-char snippet cap. Feeding the same thin text back to
  the judge that already mishandled it twice is not a fix; a human with the posting link is.
- **Not promoted into `normalize_result`.** On rows carrying `gate_results`, the two
  signatures flagged exactly the same single row and neither caught anything the other
  missed, so a second live detector in the load-bearing path buys nothing.
- Separately, the 43 "cannot evaluate" rejections are **not a fetch bug**: 41 are Adzuna,
  42 of 43 sit at or below the 500-char cap, and none is empty (min 143 chars). That is the
  documented cost of the thin source, not a retrievable failure.

---

## 2026-08-07 — Schema: `jobs.eval_issues`; flagged verdicts enter the attention queue

- Audit of the contract diagnostics on real data. The live `gate_results` check flags **1**
  row — an agentic-solutions engineering role with six PASSes, `failed_gate` NULL and
  `ai_artifact_depth` 0, the guide's canonical depth-0 → RECRUITER_ONLY case stored as a
  rejection (1 of 208 GATE_FAIL rows carrying `gate_results`, 0.5%). It caught that the day
  it shipped.
- **A second signature reaches the whole history**, and it is the larger number. The guide
  scores fit ONLY when every gate passes ("if ANY gate fails, stop — do not score fit"), so a
  rejection still carrying a complete `score_breakdown` contradicts itself just as loudly —
  and that needs no `gate_results`. `tests/validation/audit_causeless_gate_fails.py` splits
  the 72 no-named-gate rejections into **21 scored-yet-rejected** (11 of them
  `ai_artifact_depth` 0; their own `one_line` text says "pursue via recruiter or referral,
  not cold application"), **43 unevaluable-input** (truncated/missing posting text — no gate
  was ever tested, so these are a fetch problem sitting in the rejected pile instead of
  `needs_manual`), and 8 unlabelled but genuinely-reasoned rejections. The scored-yet-rejected
  dates cluster hard on the drift window: 9 on 07-31, 6 on 08-01, 2 on 08-02. Named targets
  include PwC AI Engineer, Google Forward Deployed Engineer GenAI, Deloitte FDE Frontier
  GenAI, Accenture Applied AI Engineer, EY AI Cybersecurity Architect.
  (An earlier draft of this entry said those rows were "unknowable from stored evidence" —
  wrong: the gate table was missing, the contradiction was not.)
- **Two alarms that did NOT survive measurement**, recorded so they are not re-raised:
  (a) 6 rows showing "all six gates
  PASS + GATE_FAIL" are not missed inconsistencies — every one carries `failed_gate='other'`,
  which is deliberately outside the six-gate table, so an empty `eval_issues` is correct.
  A third hypothesis — that the model routinely violates the agentic-depth disambiguation by
  failing `tool_requirement` — also failed: of 29 such rejections mentioning agentic/MCP
  language, reading the stored `gate_notes` shows ~9 of 10 sampled are exactly what the guide
  sanctions (3 yrs GCP, 4+ yrs Terraform, 5+ yrs Informatica, TypeScript/Node, Java 17). The
  keyword matched job-ad vocabulary, not the rejection's reasoning.
- **`jobs.eval_issues`** (TEXT, comma-joined, NULL = clean) denormalizes the diagnostics out
  of `eval_json`; the eval UPDATE is its one writer and an additive migration lifts existing
  values once. Motive is purely read cost: the JSON predicate took 3,829 ms per Action Center
  load on the real history versus 9 ms for a column. A PARTIAL `idx_eval_issues` keeps the
  attention queue's OR against `status` on SQLite's MULTI-INDEX OR path — without it the OR
  defeats `idx_status` and full-scans (230 ms). Measured after: 0.1 ms; the one-time
  migration on the 293 MB database is 0.5 s.
- Undecided flagged rows now appear in **Needs attention**. They had no other surface: a
  GATE_FAIL row is in no queue and no backlog listing, and its NULL `fit_score` sorts it
  below every scored row in the date view — that flagged row was on page 7 of 11 for its day.
- **Deliberately not done: automatic re-routing.** Rewriting a self-contradicting GATE_FAIL
  to RECRUITER_ONLY would trust the model's gate table over its own verdict on n=1 evidence,
  in a judge with a measured ~18-25% verdict flip rate — automating a guess. The row is
  surfaced for human judgment instead, and deciding it is the queue's exit.

---

## 2026-08-07 — Duplicate suggestions drop mass-posted company/title keys (`MAX_BUCKET_PAIRS = 3`)

- The Possible duplicates blocking key is company+title **without** location on purpose —
  cross-source location strings rarely agree ("Grand Central, Manhattan" vs "New York, NY"), so
  requiring them to match would defeat the queue. The cost: one requisition mass-posted across
  many cities becomes a full LinkedIn × Adzuna cross product under a single key. One
  `deloitte | microsoft dynamics senior consultant…` key alone yielded 792 pairs, and the ten
  largest keys were 26% of the queue.
- A key producing more than `MAX_BUCKET_PAIRS` chain pairs is now read as evidence of
  mass-posting rather than of duplication and is dropped **whole**. Dropped pairs also stop being
  confirmable through this queue (the eligibility check shares the same map); the manual
  assertion paths — CLI `dupe`, the UI's duplicate controls — are untouched, so nothing becomes
  unlinkable.
- `query_candidate_page` returns `suppressed_keys`/`suppressed_pairs` and the Action Center
  section appends them to its description, so a trimmed queue is never presented as the complete
  population.
- **Measured on the real DB at 3**: 2,486 pairs shown, 8,054 pairs across 358 keys suppressed —
  **76% of the candidate population**. That is a much wider cut than the "ten largest keys = 26%"
  observation that motivated the threshold. Left at 3 deliberately (mass-posting really is that
  common here, and the counts are visible rather than silent), but the number is recorded so the
  threshold can be revisited against evidence rather than intuition.

---

## 2026-08-07 — recruiter_route becomes a channel-finding worklist (RO ≥15 · 14d · clears on contact)

- Diagnosis: the RECRUITER_ONLY bucket was effectively untouched (8,659 evaluated rows, 38 ever
  decided, zero recorded contacts) — partly because the Action Center + contacts features only
  landed 08-05/08-06, but also because the queue had no completion semantics: finding a channel
  changed nothing on screen, and the 3-day `fresh_days` window modeled a freshness race that the
  RO verdict's own premise (cold-applying is low-value) contradicts.
- The **Route to a human** queue is re-scoped: undecided RECRUITER_ONLY chains, `fit_score >= 15`
  (was 14 — re-aligned with the restored bar, see entry below), first seen in the last **14 days**
  (was 3), **and no contact recorded on the current chain**. Recording a contact (any kind) is the
  queue's completion event, mirroring tasks_due/interview_prep exit semantics; it never decides
  the role — the chain stays in the Backlog where Draft outreach is the next step.
- Mechanics: a `contact_roots` CTE joins `job_contacts` through each contact row's current chain
  root (same read rule as `outreach.contact_summaries`), rides the single backlog candidate scan
  (no per-row probes — pinned by a new query-bounds test), and is exposed as a backlog-only
  `has_contact` filter key. Cadence is overridable via optional `recruiter_route_days` /
  `recruiter_route_min_score` settings. Measured on the real DB at ship time: 90 rows in the
  queue, today one per chain (90 distinct roots, 78 distinct normalized companies, fit 15-18,
  window 07-25..08-07), Adzuna 46 of 90. The queue inherits the backlog view's row scoping, so a
  `dupe`-linked cross-source pair — both members still `evaluated` — occupies two cards; that is
  pre-existing and unchanged here. No automated people-lookup: finding the person stays manual.

## 2026-08-07 — Eval runs at `reasoning_effort: "low"` (was provider-default "high")

- Discovered while pricing the 0731 cost jump: `_call_deepseek` never set a reasoning
  effort, and V4-Flash's default is thinking-on at **high** — so every eval has been paying
  for maximum-depth reasoning on a rubric task. External evidence (LLM-judge studies; the
  precision-vs-planning constraint split) predicted high effort adds nothing here; two
  `effort_probe.py` runs (18 + 30 postings × 3 conditions × 3 reps, 432 calls) confirmed it
  locally.
- **Replicated across both runs**: low agrees with high's majority verdicts at the noise
  floor (83% / 90%, vs high's own re-run agreement of 71–89%) — no detectable judgment
  change; ~29% fewer output tokens (≈4.2k → ≈3.0k per eval); fit mean +0.7 (≈9.9 → ≈10.6,
  closer to the pre-0731 ≈10.9 scale); zero empty answers in 144 low draws vs 3 in high's.
  **Not replicated** (one run each way, treat as noise): any stability difference.
- **`thinking: disabled` was rejected** despite 25× cheaper output and perfect determinism:
  it is a *different judge* — 50–60% agreement, systematically harsher, GATE_FAILs most of
  what high/low pass. Determinism is worthless if it's a different verdict.
- Verdict-level anchors re-verified at low effort via backtest_v2 before landing.
- **Calibration decided (user, same day): bar restored to 15.** The morning's 15 → 14 move
  compensated for the high-effort scale (gap to mean ≈ 4.1, matching the historical
  15-on-10.9); with low's scale back at ≈ 10.6, bar-15's gap is 4.4 (slightly conservative)
  vs bar-14's 3.4 (looser than the bar has ever been). Guide updated at all three sites,
  mirror re-synced. Net of the whole day: the bar is back where it started and the scale
  moved most of the way back under it — the two changes must be read together in any
  historical fit-threshold analysis.

## 2026-08-07 — Schema: per-gate explicit results (`gate_results` in eval output)

- The eval output contract now requires an explicit PASS/FAIL for **each of the six hard
  gates by name**, on every evaluation — including gate failures (the remaining gates are
  still reported) and trivially-satisfied gates. Motivation: the documented failure mode of
  long rule documents is *silent omission* — a model can skip a rule while narrating
  compliance (the 07-21 matcher-line decay was this mechanism), and a bare verdict carries
  no trace of it. A structured per-gate field turns a skipped gate into a visible hole.
- `normalize_result` normalizes the field case/whitespace-insensitively and adds
  **assistive flags only**: `gate-results-incomplete` (a gate has no explicit verdict) and
  `gate-results-inconsistent` (the gate table contradicts the verdict — with the exception
  that `failed_gate: "other"` alongside six PASSes is the documented shape of the
  unmeetable-qualification rule, not a conflict). Deliberately **never verdict-changing**,
  unlike the depth/leadership caps: auto-capping on a diagnostics field would let one
  hallucinated FAIL string re-bucket a clean role.
- `backtest_v2` is the enforcement point: an incomplete or self-contradictory gate table
  fails the case even when the verdict matched. Old `eval_json` rows lack the field and are
  unaffected (nothing re-reads them through the new path).
- **Follow-up the same day — the contract findings live in `eval_issues`, not `flags`.**
  Profiling the flag channel before designing its rendering showed it is not the tidy
  token list it looks like: 76% of evaluated rows carry at least one flag, median two,
  across **53,655 distinct phrasings**, and only **0.8%** of flag strings match a
  guide-defined token — the rest is model prose. `flags` answers "what about this ROLE
  needs human judgment"; a gate-table contract check answers "how much should you trust
  this evaluation". Putting the second in the first read as one more caveat about the job
  and would have been unfindable among the prose. `normalize_result` now writes an
  `eval_issues` list and no longer touches `flags` at all (it previously also rewrote a
  malformed `flags` value to `[]`, which was never its business). The report prints them
  with the verdict, above the role's caveats; the web UI renders them under the verdict
  line in a muted monospace style deliberately unlike a flag.
- **Correction + first real catch.** The line above originally read "no live row has ever
  tripped the contract" — measured wrong, because the completeness check was tested
  before the causeless-`GATE_FAIL` check existed. Re-normalizing all 417 stored rows that
  carry `gate_results` gives **3 trips (0.7%): 2 incomplete, 1 inconsistent**. The
  inconsistent one is a genuine model error — an agentic-solutions engineering role
  returned `GATE_FAIL` with `failed_gate: null` while its own gate table read six PASSes
  and its own `one_line` said "all gates pass, but required depth is a generation ahead",
  i.e. the textbook `ai_artifact_depth = 0` → RECRUITER_ONLY case. A wrong GATE_FAIL is
  the irreversible direction of the judge's ~25% instability: rejected rows surface
  nowhere, so the role simply vanishes. The gate-fail section of the report now carries
  the diagnostic too — it previously rendered only for gates-passed rows, i.e. the
  finding that matters most appeared exactly where it could not be seen.
- **Follow-up the same day — `failed_gate` enum now includes `"other"`.** Merging the
  unmeetable-stated-qualification rule into the production guide created a contradiction
  between the two halves of the prompt: the guide says log such a failure as `other`, while
  the output spec listed only the six gate names or `null`. (Contradictory instructions are
  specifically costly on reasoning models — they spend tokens reconciling rather than
  picking.) The spec now lists `other` with its narrow definition and states that a
  `GATE_FAIL` must name a cause. With that ambiguity gone, `normalize_result` also flags the
  previously-unjudgeable shape — `GATE_FAIL` with no `failed_gate` and no gate reading FAIL
  — as inconsistent, while a fail named only in the gate table (cause stated, just not
  duplicated) stays consistent. `"other"` was already reaching the DB from the merged rule;
  storage and the report/UI render it unchanged, and there is no CHECK on the column.

## 2026-08-07 — Guide surfaces reconverged: the 07-30 gate rules reach the pipeline

Discovered while re-syncing the Claude.ai Project's uploaded guide: the repo guide and the
Project's copy had drifted in BOTH directions, because each surface was receiving edits from a
different workflow (repo ← Claude Code sessions, CHANGELOG'd; Project copy ← Claude.ai Project
conversations, not CHANGELOG'd). Neither was a stale mirror of the other.

- **Now in the production guide (was Project-copy-only, never CHANGELOG'd, so the pipeline has
  never enforced it):** the **qualifications-column reading** (2026-07-30 — a posting with no
  required column has its Preferred block gate as de facto required) and the
  **unmeetable-stated-qualification rule** (2026-07-30 — a stated experience ceiling or an
  unsatisfiable binary precondition is a gate FAIL logged as `other`). This is a judgment
  change: postings that previously defaulted to PASS on a mislabeled column now fail the gates
  before reaching the paid eval.
- **Now in the Project copy (was repo-only):** the 2026-08-03 realignment — production-eval
  disambiguation in `role_substance`/`ai_applied_vs_research`, the three-answer contract, the
  non-scoring career-capital note, and the role-positioning note. Until this sync the Project's
  guide still listed "evals/benchmarks" as disqualifying substance, i.e. it was GATE_FAILing the
  production-eval roles the 08-03 decision makes a target.
- Both surfaces now carry identical text, including the fit ≥ 14 bar. The Project's *uploaded*
  file is a third surface that only a manual re-upload updates.

## 2026-08-07 — 0731 flash build: eval max_tokens 16000, cold-apply bar 15 → 14

- DeepSeek's silently cut-over V4-Flash-0731 build reasons ~2.5× longer per eval (out-tokens/eval
  ~1.2k → ~3.4k; per-eval cost doubled, no price change). `max_tokens` raised 8000 → 16000 in
  `_call_deepseek`: a measured completion on one posting spent 9,888 output tokens (9,398 of them
  reasoning), which the old cap would have truncated to an empty answer.
- **Correction to the first version of this entry (measured, not inferred):** the raise does NOT
  explain most of the 30–100 daily "no JSON object" retries, and does not eliminate them. Probing
  the recurring case three times reproduced the failure once (1/3): that attempt returned
  `finish_reason: "stop"` with **362 reasoning tokens and zero content tokens** — the model ends
  normally and simply emits no answer, nowhere near any cap. Empty-answer retries are a 0731 build
  behavior, not a truncation; each one re-bills the full ~13.7k-token prompt. Treat the cap raise
  as removing one of two causes.
- Likewise **do not read a single green backtest as a fix**: the run right after the raise went
  8/8, a later run on the same build reproduced the same case's failure (7/8 + 1 error). Same
  lesson as the 08-01 boundary-variance finding — one run cannot establish a fix or a drift.
- The same build also scores gates-passed roles ~1 point lower (daily fit-score mean 10.9 → 9.6;
  backtest re-scores of identical postings confirm the level shift). Routing and gates are intact —
  across three backtest runs the artifact-depth and leadership caps fired correctly every time and
  no anchor changed side — so this is a scale drift, not a judgment break. The same three runs also
  show the noise band on identical postings: one case scored 11 / 13 / 15, another 16 / 17 / 16, and
  one swung GATE_FAIL ↔ RECRUITER_ONLY. Standing-allocation cold-apply bar lowered fit ≥ 15 → ≥ 14
  (guide, three sites) to keep the bar's real-world strictness unchanged on the new scale.
- Watch item: eval_json verbosity was still climbing daily a week after cutover; if the scale keeps
  sliding, revisit the bar rather than the scoring rules.

## 2026-08-07 — Confirmed interview-story and application-answer library

- Added local `prep_entries` drafts for reusable interview stories and application answers, plus
  versioned `prep_entry_roles` relevance links that follow current duplicate chains.
- Only entries the user has explicitly confirmed and linked to a role enter its copied interview
  context. Editing confirmed content or restoring an archive returns it to draft for re-review.
- Kept the library out of ordinary job-card payloads and external outreach briefs. Prep context
  labels selected notes as untrusted, user-maintained claims and asks the drafting assistant to
  flag conflicts instead of inventing or silently reconciling details.
- Retained archived content and link tombstones with optimistic versions so stale tabs and ABA
  changes cannot silently overwrite newer state.

## 2026-08-07 — Pipeline health and search-yield evidence

- Added durable `pipeline_runs` and per-target `pipeline_fetch_attempts` records for each LinkedIn
  search, Adzuna query, and ATS board. Successful target facts commit with their posting inserts;
  failures roll back partial inserts before recording a categorized error.
- Added a local **Health & yield** view for recent run/source/target status and bounded search-track
  cohorts, including configured tracks with zero observed roles.
- Kept the metrics honest: a successful zero-result response is healthy, missing configuration is
  skipped rather than failed, cooldown advances only after a real target success, and yield uses
  raw posting volume plus one earliest-touch attribution per current duplicate chain rather than
  double-crediting cross-posted roles or claiming causal conversion.
- Run records retain counts, configuration hashes, stages, and error categories/classes only—never
  raw exception messages, request URLs, credentials, descriptions, contacts, or document content.

## 2026-08-07 — Unified role activity timeline

- Replaced the card's event-only History panel with a bounded, newest-first **Activity** view across
  posting discovery, current chain decision, application events, material attachments, contacts,
  tasks, interview schedules, and the current star.
- Mapped every owner through its posting's current duplicate root, so merges union activity and
  unlink restores the original separation without rewriting records.
- Kept the timeline strictly read-only and factual: it emits only timestamps retained by the owning
  tables, does not turn mutable state into invented append-only history, and leaves the legacy
  `/api/events` contract intact.
- Limited item detail and total response size; contact email/profile/note, document hashes/bytes,
  and full private artifacts are not loaded into the timeline payload.

## 2026-08-07 — Explicit manual role intake

- Added a local **Add role** form for jobs found outside configured LinkedIn, Adzuna, or ATS
  sources. A configured search track, URL, title, and company are required; location, posted date,
  salary range, and pasted JD text are optional. ATS-only configs use a neutral manual track.
- Extracted the fetched-posting insertion tail into one shared store path, so manual rows use the
  same normalization, exact-URL dedup, fingerprint, repost-linking, and `status='new'` transition.
- Saving never fetches the supplied URL, spends on an LLM call, or overwrites an existing row. The
  next normal pipeline run applies current filters, repost skips, and evaluation in order.
- Added strict field, http(s)-URL, embedded-credential, date, salary, description-loss, transaction,
  same-origin API, and UI-contract regression coverage.

## 2026-08-07 — Verified evidence-unit backups

- Added `pipeline.py backup` to create a non-overwriting ZIP containing one consistent SQLite
  snapshot and exactly the immutable application-material objects catalogued by that snapshot.
- Added a versioned JSON manifest with database/object sizes and SHA-256 digests. Creation fails
  closed on missing, corrupt, invalid, duplicated, or path-escaping material metadata.
- Added `backup --verify` to cross-check ZIP membership, hashes, SQLite integrity, material links,
  object catalog, and manifest counts without loading private config or mutating live data.
- Kept restore deliberately out of scope: a valid archive is evidence that the backup is coherent,
  not authorization to overwrite the active database and object store.

## 2026-08-06 — Explicit starred-role shortlist

- Added chain-scoped manual stars and a bounded **Starred roles** Action Center queue, independent
  of model score and application decisions.
- Added versioned tombstones and absolute expected-state/version checks under an immediate write
  transaction so a stale browser tab cannot invert a newer star/unstar action, including ABA
  sequences where the state changes away and back.
- Duplicate merges union starred visibility; unlink restores canonical-at-write ownership without
  rewriting markers. CSV summaries include the current star state.

## 2026-08-06 — Spreadsheet-safe role summary export

- Added a local **Export CSV** download with one row per current duplicate chain, using the shared
  effective decision plus open-task and upcoming-interview summaries.
- Neutralized formula-like cells before spreadsheet use and emitted UTF-8 with a BOM for Excel.
- Deliberately excluded descriptions, contacts, notes, event history, and document bytes. This is
  a portable summary, not a replacement for the paired `jobs.db` + `application_materials/` backup.

## 2026-08-06 — Role notes on every workflow card

- Exposed the existing chain-scoped `note` event on undecided, passed, filtered, and applied cards
  instead of hiding it inside the applied-only outcome controls.
- Added one shared History control on every card. Notes remain append-only local evidence and do
  not change application decisions, outcomes, evaluation, or follow-up cadence.
- Hardened `/api/event` to reject non-object JSON with the normal JSON error contract.

## 2026-08-06 — Local interview schedule and calendar export

### Why

Open-source application trackers including [Job Trail](https://github.com/aplaza1/job-trail),
[Applic](https://github.com/rpunia29/applic), and
[Candidex](https://github.com/sebai-dhia/candidex) make upcoming interview dates visible instead
of burying them in notes. The useful subset here is a local, chain-scoped schedule beside the
existing application evidence—not an external calendar integration and not a second
representation of events that have already happened. Reviewed revisions, licenses, and the
adopted/rejected boundary are recorded in [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

### Changes (schema/workflow only; evaluation judgment unchanged)

- Added `job_interviews` plus `interviews.py` for validated, timezone-aware schedules on applied
  role chains. Canonical-at-write/current-chain reads preserve the established merge/unlink model.
- Added optimistic versions and immediate write transactions so stale tabs cannot overwrite a
  newer edit or cancellation, and stale posting rows cannot attach a schedule to an obsolete root.
- Added the bounded **Upcoming interviews** Action Center queue: one canonical card per chain,
  ordered by the next scheduled start in the coming 14 days.
- Added local-time card chips and controls to schedule, edit, or cancel a round. A scheduled round
  can be downloaded as a standards-compatible `.ics` file for user-controlled import.
- Added validation, chain merge/unlink, stale-version, transaction-ownership, queue paging, API,
  card-payload, and calendar escaping regression coverage.

### Explicitly unchanged

No Google/Outlook calendar writes, email reminders, external notifications, LLM-generated
schedules, evaluation changes, or automatic `interview` outcome events. A schedule is a plan; the
user records the outcome only after it actually happens.

## 2026-08-06 — Review-only cross-source duplicate suggestions

### Why

The strict company+location+exact-title fingerprint safely prevents false automatic merges, but
source-specific location labels mean the same role can still appear independently on LinkedIn,
Adzuna, and an ATS board. Finding those misses by manually searching two histories is avoidable;
loosening the automatic fingerprint is not, because a false merge can hide a role that should be
considered or cause the wrong decision to propagate.

### Changes (schema/workflow only; automatic dedup and evaluation unchanged)

- Added `dupe_candidates.py`, which derives recent cross-source suggestions only when normalized
  company and exact normalized title agree. It emits at most one comparison per pair of current
  chains and keeps location disagreement visible as evidence rather than silently overriding it.
- Added a bounded **Possible duplicates** Action Center queue with side-by-side source, location,
  dates, posting link, and description preview. Confirming reuses the existing guarded manual-dupe
  core; the suggestion layer cannot merge on its own.
- Limited each comparison side's API payload to those visible posting fields. Duplicate discovery
  no longer loads or returns contacts, tasks, application materials, interview details, or
  evaluation/decision card data.
- Ignore/restore now validates the preview roots and a persistent review version inside the write
  transaction. Restores retain a versioned tombstone, preventing stale tabs—including ABA state
  sequences—from overwriting a newer judgment or hiding a changed pair.
- Added `dupe_candidate_dismissals` for persistent **Not the same role** judgments, plus an ignored
  queue and restore action. Mutations refresh current chain roots under `BEGIN IMMEDIATE`, so a
  stale browser cannot dismiss a pair that a concurrent merge already changed.
- Candidate confirmation carries the previewed roots through `confirm_candidate`; it checks both
  current roots and any newer **Not the same role** judgment before resolving and merging under the
  same immediate write transaction. A stale tab therefore cannot override the review action that
  acquired the write lock first.
- Added false-positive boundary, chain-collapse, transaction ownership, API, paging, dismiss,
  restore, and confirm regression coverage.

### Explicitly unchanged

The automatic fingerprint, fetch/eval stage order, paid-evaluation eligibility, existing posting
decisions, and manual merge conflict guards. Suggestions never auto-link, auto-skip, or change a
status.

## 2026-08-06 — Chain-scoped next actions and due-task queue

### Why

The fixed application follow-up cadence covers one standard workflow but cannot represent the
other concrete commitments around a role: prepare questions, request a referral, send a portfolio,
or check a deadline. Open-source trackers such as
[JobSync](https://github.com/Gsync/jobsync) make tasks and upcoming work first-class; the useful
subset here is explicit local next actions, not general project management or another notification
service. Reviewed revision and license details are recorded in
[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

### Changes (schema/workflow only; evaluation judgment unchanged)

- Added `job_tasks`, using canonical-at-write/current-chain reads so manual duplicate merges union
  task lists and unlink restores original ownership without data migration.
- Added validated local task creation with a title, due date, and optional note; completion and
  cancellation retain closed records, while reopen/reschedule operations preserve task identity.
- Added optimistic task versions so concurrent browser tabs reject stale updates instead of
  silently overwriting a newer completion, cancellation, reopen, or snooze.
- Added a bounded **Next actions due** Action Center section with one canonical card per chain and
  earliest-due-first paging. Future and closed tasks do not enter the queue.
- Added card controls to create tasks, complete or cancel the selected task, and snooze it by 1, 3,
  or 7 days. Overdue tasks reschedule from today rather than remaining overdue.

### Explicitly unchanged

No external notifications, calendar writes, automatic task generation, evaluation changes,
application/outcome mutations, or general-purpose priority/time-tracking system.

## 2026-08-06 — Chain-scoped role contacts and no-send outreach briefs

### Why

The application history knew that a follow-up happened but not which recruiter, hiring manager,
or referral was involved. Mature open-source trackers place contacts beside the application and
use the same role context for drafting; the useful part here is the evidence model and human review
boundary, not automated account access or message sending.

### Changes (schema/workflow only; evaluation judgment unchanged)

- Added `job_contacts`, with canonical-at-write/current-chain reads matching application events
  and packet links. Duplicate merges union contact evidence without rewriting rows; deletion is
  scoped to the current chain so one role cannot remove another role's contact.
- Added local UI controls to add/remove recruiter, hiring-manager, referral, and other contacts;
  cards expose the chain's current contact list with optional email/profile links and context notes.
- Added a clipboard-only outreach brief for application follow-ups, recruiter introductions, and
  referral requests. It uses the selected contact plus the frozen JD, actual submitted packet, and
  event history, labels incomplete evidence, and instructs the drafting assistant not to invent
  facts. The app never sends a message or marks a follow-up as sent.
- Kept contact details local in SQLite and retained the existing explicit user action for recording
  `followup_sent` only after a message was actually sent.

### Explicitly unchanged

Posting gates, scores, verdicts, bucket routing, fetch/eval stage order, historical decisions,
and external account access.

## 2026-08-05 — Chain-scoped application packet evidence

### Why

The free-text `resume_variant` label could not establish which document was actually submitted,
and the mutable posting row could not reliably reconstruct the JD seen at application time.

### Changes (schema/workflow only; evaluation judgment unchanged)

- Added content-addressed `material_objects` and append-only `application_materials` tables.
  Material links use canonical-at-write/current-chain reads, matching event-history behavior
  across duplicate merges and unlinks. Document bytes and JD snapshots stay in the local object
  store; SQLite keeps metadata/relations rather than a second copy of extracted document text.
- Marking a role applied now freezes the interaction posting's stored JD (including a relisting's
  own text rather than its canonical's potentially older text). The local UI accepts the actual
  resume and cover letter, deduplicates bytes by SHA-256, and exposes the submitted packet on
  Applied cards. Packet links retain that interaction URL separately from chain ownership; Prep
  Context uses the same interaction posting for its heading and URL. In addition,
  cached checksum verification makes missing/corrupt objects explicit on cards and prep output.
  Downloads always rehash at the trust boundary, while unreadable document extraction marks Prep
  Context partial instead of claiming the complete submitted packet was copied.
- Added PDF/DOCX/text extraction, explicit PDF text-layer/contact checks, and a conservative
  reading-order fragmentation heuristic. The UI calls a clean result "Basic checks passed," not
  ATS certification. Uploaded files remain local and gitignored.
- Added a recent interview-prep Action Center queue and clipboard context assembled from the
  frozen JD, submitted documents, and prior events/notes. It does not send or mutate state.
- Documented `jobs.db` plus its adjacent `application_materials/` object store as one quiesced
  backup/restore unit; either part alone is incomplete application evidence.

### Explicitly unchanged

Posting gates, scores, verdicts, bucket routing, fetch/eval stage order, and historical decisions.

## 2026-08-03 — Career-strategy alignment: production eval disambiguation and non-scoring capital note

### Why

The canonical decision `D-2026-08-03-CAREER-REALIGNMENT` makes production/application
evaluation a core capability while keeping foundation-model research and from-scratch training
out of scope. The prior prompt used “evals/benchmarks” as an undifferentiated research signal,
so a production deployment role could be misclassified from keywords alone. The same decision
requires capability, current screenability, and long-term career capital to remain separate.

### Changes (prompt/templates/docs only; no DB schema or historical rerun)

- `profile.md` and its committed example now distinguish held production workflow/integration
  capability from production agentic/SDK, RAG, eval-in-CI, observability, incident, and external-
  customer deployment capabilities that are still being built.
- `evaluation_guide.md` and its example classify eval work by its object: foundation-model or
  research contribution remains out of scope; production application/workflow eval, reliability,
  verification, observability, and deployment validation are in scope.
- Gates-passed evaluations answer performability, screenability, and career capital separately.
  Career capital reuses `one_line`, is explicitly non-scoring, and cannot add a gate or duplicate
  `learning_value` / `title_trajectory` penalties.
- `docs/career_strategy_alignment.md` records the downstream mapping, nine-case anonymized manual
  adjudication matrix, unchanged boundaries, and a deferred structured-field option.
- Local prompt-contract tests cover the ambiguity fix, no-inflation boundary, preserved routing
  contract, three-answer separation, and requested adjudication archetypes.

### Explicitly unchanged

The score thresholds, Bucket definitions, verdict vocabulary, application allocation, current
artifact-depth and formal-leadership caps, function-precedent and enablement/management rules,
work-auth/employment/location/compensation facts, SQLite schema, `jobs.db`, and historical verdicts.

## 2026-08-01 — Boundary-variance finding (V4-Flash-0731 cutover); fuzzy-zone backtest anchors

### Why
DeepSeek re-post-trained V4-Flash and cut the `deepseek-v4-flash` endpoint over in place on
2026-07-31 (build V4-Flash-0731) — no opt-in, same model name. A same-day A/B on the same 25
postings (temperature 0, hours apart; `tests/validation/compare_models.py`) showed 4 verdicts
flipped, all toward PASS, all on truncated ~500-char Adzuna snippets — which first read as
retrain-induced drift. A follow-up stability probe (3 repeat runs per flipped posting, same
build, temperature 0) reframed it: two of the four cases are intrinsically unstable
(Tenex "Bring Your Own Pod": RECRUITER_ONLY / GATE_FAIL / parse-error across three runs;
AJAIA "Executive Strategy Lead" produced all three verdicts within one day), and the other
two are stable at a non-PASS verdict, making their round-2 PASS an outlier draw. Conclusion:
**truncated postings sit on the judge's decision boundary, where temperature-0 verdicts are
high-variance**; the A/B flips cannot be attributed to the build cutover. A kimi-k3
arbitration over the 12 model-disagreement cases (`tests/validation/arbitrate_k3.py` →
`arbitration_k3.json`) set the invariants: the two Strategy & Ops seats legitimately PASS;
the pod-only mechanism and the Executive-titled seat must never cold-PASS.

### Changes (test coverage only — no guide text, no schema)
- **backtest_v2 gains three fuzzy-zone anchors** pinning "never a cold PASS" on: a
  quota-carrying sales role (talentpluto AE — stable), a pod-only application mechanism
  (Tenex "Bring Your Own Pod"), and an Executive-titled strategy seat (AJAIA). The latter
  two ride the decision boundary, so a red on them is not test flakiness — it means the
  judge drew a cold PASS on a never-PASS case in that run, and production evaluates each
  posting exactly once, so the red frequency samples the real boundary-error rate.
- **Pending observation, not yet a rule**: if triage shows cold-PASSed truncated
  strategy/executive postings arriving at a bothersome rate, add a "truncated posting →
  conservative default" rule to the guide (that change would get its own entry here).

---

## 2026-07-25 — Artifact check, function-precedent cap, variant→file routing map

### Why
A ten-JD audit (triggered by four rejections landing in two days on fit-14–16 PASS roles)
examined the 202-application / 0-interview funnel. Corrected same-day findings:
(1) the eval reads the profile but the screener reads the PDF — the pre-07-25 BIAnalyst
variant contained zero AI tokens, so any fit stamp on an AI-titled req was unachievable by
that paper; however, where the variant WAS recorded (56/202), routing was already correct
(all 35 BIAnalyst applies → analyst-titled reqs) — the documented failure is that 146
applies (incl. all 72 pre-variant-system June applies) are unattributable, so misrouting
can be neither proven nor ruled out; (2) the eval over-scored roles whose core daily
function has no career precedent (pre-sales SE @ Trucker Path fit 18, post-sales SE @
Instabase fit 17 — unwinnable cold at any resume quality); (3) that same 146-row
`resume_variant` gap makes variant-level outcome analysis impossible. Full report:
`resume_variant\linkedin_job_review_material\application_conversion_diagnosis_2026-07-25.md`
(local, not in repo).

### Changes (guide text only — no code, no schema; verdicts vocabulary unchanged)
- **Part 2.5 gains an "Artifact check"**: the cold-apply bar is evaluated against the exact
  variant being sent (named in Part 4), never against profile facts; a PASS whose evidence
  isn't on the paper routes to *switch/fix variant first*, not apply.
- **Part 2.5 gains a "Function-precedent check"** (guide-enforced cap, sibling to the
  code-enforced artifact-depth and leadership caps): zero career precedent in the role's core
  daily function (pre-/post-sales, quota motion, people management) caps the verdict at
  RECRUITER_ONLY regardless of skill overlap.
- **Part 4's Resume-variant line becomes a variant→file routing map** (BIAnalyst = pure
  BI/analyst reqs only; Industry = SA/internal-builder; AIAdvisor = legal-tech/advisory;
  AI_Data_Analyst = AI-flavored analyst) and requires recording the variant at apply time
  (`applied --resume`).
- Resume artifacts updated in the same pass (outside the repo): BIAnalyst gained an
  AI-production clause, split dated titles (SA / BIA), senior SQL vocabulary, Power BI (DAX),
  AI Builder + Git; all four variants gained Git; all re-exported and verified 1 page.

### Expected effect
Fit scores become achievable by the paper submitted; pre/post-sales function mismatches stop
consuming paid evals and application hours as PASS verdicts; variant-level conversion becomes
measurable from the DB.

---

## 2026-07-23 — Freshness line, enablement star-scoring rule, allocation/overlay clarification

### Why
Review of the first post-07-21-template evaluation (an enablement-cluster specimen) confirmed
the two 07-21 fixes took (on-enum verdict + disposition, matcher line emitted with correct
trigger reasoning) and surfaced three remaining gaps, all guide-side:
(1) the standing allocation's third leg — posted ≤ 14 days — had no template line anywhere,
so evaluations asserted freshness conclusions without ever stating the posting date (the same
no-line-binds-it decay mode the 07-21 audit identified); (2) the guide never said how an
enablement-cluster seat scores `ai_applied_vs_research` — Example D has no score table, so the
same posting could defensibly score 1 (convenience-layer reading) or 3 (applied reading), a
±2 swing that flips dispositions at the fit ≥ 15 allocation boundary; (3) the standing
allocation ("Bucket 3 only") and the enablement overlay ("cold-apply fine, insurance behind
Bucket 3") contradicted each other on whether flagged enablement roles may still cold-apply.

### Changes (guide text only — no code, no schema, verdicts unchanged)
- **Part 4 gains a "Posted date / freshness" line**, emitted even when another leg of the
  allocation bar already fails.
- **"How to use" gains an emit-every-line rule**: every template line in Parts 1–4 is
  written (N/A rather than omitted) — covers the Part 3 internal checklist, the
  location/relo-comp line, and the unstated-employment-type flag, all of which had drifted
  to prose or been dropped.
- **Enablement-cluster flag gains a star-scoring rule**: `ai_applied_vs_research` scores as
  applied (2–3) on flagged seats — driving AI adoption IS the seat's job, unlike the Eulerity
  0–1 case where AI is incidental to a non-AI seat; the penalty is carried by
  `learning_value`, `title_trajectory`, and the flag, not double-counted in the star.
- **Allocation/overlay contradiction resolved (user decision)**: flagged enablement roles
  remain cold-appliable under the standing allocation at the SAME bar as Bucket 3
  (fit ≥ 15, posted ≤ 14 days, permanent FTE *confirmed*, not unstated-default), always
  behind Bucket 3 in priority, until the Part 2 sunset triggers.

### Expected effect
Freshness becomes visible in every evaluation instead of asserted; enablement specimens
score consistently at the allocation boundary; a fresh fit-15+ enablement FTE role now has
one unambiguous disposition (apply, behind Bucket 3) instead of two readings.

---

## 2026-07-21 — Part 4 anti-drift: matcher spot-check line + "skip at triage" disposition

### Why
An audit of the external triage chats found two drifts since the 07-16 recalibration:
(1) evaluations stopped emitting the Part 4 resume-variant line and never mention the
local resume↔JD matcher — the spot-check policy's triggers rarely fire under the standing
allocation, and with no template line forcing the question, the whole tail of Part 4
silently fell away; (2) "SKIP" crept in as an ad-hoc *verdict* for all-gates-pass roles
the allocation rules out, which is off-vocabulary (the verdict enum is PASS /
RECRUITER_ONLY / GATE_FAIL and is pinned by code and schema).

### Changes (guide template text only — no code, no schema, verdicts unchanged)
- **Part 4 gains a mandatory "Matcher spot-check: YES (trigger) / NO" line** on every
  gates-passed evaluation. A YES names the variant to run. This makes the narrow
  spot-check triggers visible instead of relying on the evaluator to volunteer them;
  it does not loosen the policy.
- **Part 4's verdict line now states that "SKIP" is a triage disposition, not a fourth
  verdict**: an allocation-ruled-out role keeps PASS/RECRUITER_ONLY and gets
  "skip at triage" as its disposition. Pipeline vocabulary untouched.
- The external triage instructions gain an explicit output-format section (the exact
  Part 1→4 block structure); that lives instructions-side only, since the pipeline's
  JSON output spec already pins its own format.

### Expected effect
Chat-side evaluations regain the variant recommendation + matcher direction where the
policy warrants it, and stop drifting in structure; no change to pipeline scoring,
routing, or stored verdicts.

---

## 2026-07-21 — AI-recruiter intermediaries route as lead-gen only

### Why
One high-volume posting source is an AI-recruiter service — every posting's description
carries boilerplate saying an AI agent screens candidates on behalf of an unnamed customer.
The DB holds 400+ rows from it (166 gates-passed, max fit 18, 0 applied). Applying means
entering an AI screening funnel: under the standing allocation that is the worst of both
worlds (can't carry a ramp narrative like a human recruiter; unverifiable intermediary
unlike a portal). But ~75% of the gates-passed postings name the real client in the title
(early-stage AI startup FDE/Deployment-Strategist roles with salary bands — the Bucket 1
target tier), so hard-filtering the feed would discard genuine lead-gen.

### Changes (guide text only — no code, no schema, verdicts unchanged)
- **Part 2.5 — AI-recruiter-intermediary overlay:** score and bucket normally, emit an
  `ai-recruiter-intermediary` flag (flags are free text; report/UI render whatever is
  emitted), never apply through the intermediary's funnel. Client named in the title →
  a lead: pursue the company directly or via a human recruiter; salary band = negotiating
  intel. Client anonymized ("VC-backed…", "stealth") → skip.
- `evaluation_guide.example.md` carries the overlay generically (boilerplate-tell pattern,
  no company name). The external triage instructions get a one-line pointer (the full rule
  lives in the guide only — the anti-drift lesson from the 07-09/07-16 divergence).

### Expected effect
The intermediary's rows keep their verdicts and stay visible in the report, now flagged;
triage treats named-client rows as direct/recruiter leads and skips anonymized ones. No
application ever goes through the AI funnel.

---

## 2026-07-21 — guide sync with the external triage rules: cold-channel allocation + convenience-layer scoring

### Why
The external triage workspace and the pipeline had drifted onto different-vintage rules: the
workspace's instructions were updated 2026-07-16 after a cold-channel conversion audit
(mechanical causes — PDF parsing, knockout answers — ruled out; the cold portal channel was
underperforming warm channels by a wide margin), while the pipeline still ran the 07-09
guide. Canonical divergence: an AI-native company's "Forward Deployed Engineer" posting
(2026-07-21) — pipeline scored it **18/18 PASS** (DeepSeek read the company branding as AI
work); the workspace scored the same posting **12/18 SKIP** (`ai_applied_vs_research` 1 —
the seat's only AI content is "explore AI tools to streamline tasks", a productivity
convenience layer). High-scoring pipeline PASSes were routinely being vetoed at triage.

### Changes (guide/profile only — no code, no schema)
- **Part 2.5 — the standing allocation** (supersedes the 50/0-era channel text): cold
  applies = minimum insurance only — fresh Bucket 3, **fit ≥ 15, posted ≤ 14 days**. Bucket 3
  is the ONLY bucket where cold applies are permitted (the audit's Bucket 3 slice is too
  recent to prove cold conversion works — treat it as unproven, not established); Bucket 2 is
  insurance that rarely clears the bar. Explicitly volume/priority only: **PASS remains the
  verdict standard for cold-apply eligibility** — no verdict vocabulary or `normalize_result`
  change.
- **Part 2 — convenience-layer scoring rule** on the `ai_applied_vs_research` starred line:
  score the SEAT's responsibilities, not the company's branding; "use/explore AI tools to work
  faster" as the only AI content → 0–1 (near-disqualifying), with `learning_value` 0–1 and
  `ai_artifact_depth`'s 3 explicitly vacuous. New worked **Example E** — the inverse of
  the research-under-a-delivery-title case. This is the fix for the 18/18-vs-SKIP class:
  the score comes out honest
  (~11/18) and falls below the allocation bar on its own.
- **profile.md**: current title aligned with the HR record, matching the workspace's 07-16
  framing rules.
- `evaluation_guide.example.md` mirrors the two scoring changes generically, with the
  allocation expressed as an audit-your-own-data escape valve.

### Expected effect
AI-branding-only seats stop surfacing as top-of-report 17–18/18 cold applies; the report's
PASS list and the external triage verdicts converge. Bucket routing and the two code caps
unchanged.

---

## 2026-07-20 — schema: `meta` table; scheduled-run cooldown skip

### Why
Near-duplicate runs (a manual catch-up, or a missed trigger fired late on wake, shortly
before a fixed Task Scheduler slot) cost ~$0.10–0.19 of eval each and a full LinkedIn
scrape cycle of rate-limit exposure. Postings are judged identically; this is purely
run-level routing.

### Changes
- **Schema:** new `meta` key/value table (run-level state; created idempotently in
  `core.get_db`). One key so far: `last_run_ok_ended` — ISO end time of the last
  *successful* full run, written only after `generate_report` completes AND at least
  one fetch source didn't crash (`_run_fetch_stage` returns None on crash vs the
  fetcher's own 0), so neither a crashed run nor an all-sources-down catch-up run
  (wake before Wi-Fi) suppresses the next slot.
- **`pipeline.py run --scheduled`** (what `run_pipeline.bat` now passes): if the last
  successful run ended < 60 min ago (`COOLDOWN_MINUTES`), the slot logs
  `[cooldown] … skipping` inside the day's run markers and exits 0 without fetching.
  Manual `run` (no flag) always executes. The predicate (`_cooldown_active`) fails
  OPEN on missing/garbage/future stamps — including the TypeError shapes (bytes;
  offset-aware stamps are normalized to local, not rejected) — corrupt state must
  never stop the pipeline. A skip does not re-stamp, so consecutive slots can't
  cascade-skip; a skip is a full no-op, so error-row requeue and reconciles wait for
  the next executed slot (accepted: ≤1 slot of extra delay).

---

## 2026-07-09 — recruiter-screen realism: tenure split, formal-leadership cap, cold-apply bar

### Why
A conversion audit of the applications to date showed the evaluator scoring conceptual fit
correctly while talking past three cold-screen walls. Canonical miss: a manager-titled
"AI Enablement & Engineering" role — required "Minimum of 5 years… AI enablement" +
"Minimum of 3 years of leadership" — scored 17/18 PASS Bucket 3, yet was a role the resume
could not screen into cold. Root cause was partly the evaluator's own input: `profile.md`
stated one total years figure with no title-tenure split, so architect-function years
requirements were scored against total tenure when the tenure actually held **in the
current title is a small fraction of it** (the exact figures live in the gitignored
profile.md, deliberately not here).

### Changes
- **`profile.md`** (input correction): experience split by function — years requirements
  measure against the MATCHING tenure ("N yrs architecture/AI enablement" vs. the short
  current-title tenure, not the total); explicit formal-people-leadership line (none held);
  previous title corrected against the resume sources.
- **Guide — formal-leadership check (new starred-line CAP, code-enforced):** a *required*
  N+ years of formal people-leadership/management → `formal_leadership_required: true` →
  verdict capped at **RECRUITER_ONLY / bucket 1** regardless of total. Enforced in
  `evaluation.normalize_result` beside the 50/0 depth cap, with opposite polarity: it fails
  **OPEN** on a missing/negative field (most roles require no leadership; pre-cap eval_json
  rows lack the key), where the depth cap fails CLOSED. Recognized affirmatives
  (`true`/`"true"`/`"yes"`/`1`, any case) count; unrecognized values warn to stderr and
  fail open.
- **Guide — `years_vs_stated` scores against function-matched tenure**, and the years-floor
  gate note calls out "N yrs in <function>" walls dressed as floors.
- **Guide — cold-apply bar:** PASS + cold-apply now requires the resume as written to
  directly prove every requirement in the required column; anything needing *explanation*
  (title change, depth gap, years split) routes RECRUITER_ONLY.
- New model-output field `formal_leadership_required` (prompt JSON contract). No
  re-evaluation of existing rows — the cap applies to future evals; old eval_json lacking
  the key normalizes exactly as before.

### Expected effect
Bucket 3 / PASS shrinks to roles that can actually convert cold (fewer, better-routed cold
applications); manager-titled and architect-years-walled roles surface as RECRUITER_ONLY
instead of high-scoring cold PASSes.

---

## 2026-07-09 — application channel tracking (schema: `jobs.channel`)

### Why
A conversion audit found the funnel unreadable in aggregate: direct
cold-applies, staffing-agency submissions, and referrals convert at very different rates,
and the DB couldn't say which was which — the response-rate question the outcome tracking
below exists to answer needs the channel axis to mean anything.

### Schema
- **One additive `jobs` column**: `channel` (`direct | agency | referral`,
  `states.ALL_CHANNELS`). Contract mirrors `resume_variant` exactly — **applied-only**,
  recorded at apply time (`applied --channel C` / the UI's apply flow) or edited later via
  `chain.set_channel` (UI select / API `set_channel` action), written uniformly chain-wide,
  inherited on a re-assert without a value, coalesced across a dupe merge (winner's
  preferred), and cleared whenever the chain leaves `applied`. No history, NOT restored on
  re-apply. No backfill: NULL means "not recorded".
- **No CHECK**, same policy as `outcome_status` — but unlike `resume_variant`'s free text
  the vocabulary is CLOSED, enforced code-side in `chain.mark_posting`/`set_channel`
  (a per-user spelling like "staffing" would split the funnel counts the field exists to
  make comparable).

---

## 2026-07-09 — post-application outcome tracking (schema: `app_events` table + 3 `jobs` columns)

### Why
`applied` was a terminal state: no record of interviews/offers/employer
rejections/ghosting, no notes, no resume-variant memory — so no feedback loop (does
fit_score predict responses? which search converts?) and no way to surface "applied N
days ago, no response". This is the foundation; the follow-up/funnel view and outcome
analytics are fast-follows on this schema.

### Schema
- **New `app_events` table** (append-only history): `id, job_url, event_type, event_date,
  note, created_at` + `idx_app_events_job_url`. A row is written ONCE, keyed to the
  chain's **canonical url at write time**, and always read chain-wide — a dupe merge
  unions both sides' histories with no data migration; unlink leaves rows where they sit.
- **Three additive `jobs` columns**: `outcome_status`/`outcome_date` (cache of the chain's
  latest non-note event, propagated to every member like `app_status`;
  `chain._recompute_outcome` is the ONE writer) and `resume_variant` (free text; **applied-
  only**: recorded at apply time or edited later via `set_resume`, always written uniformly
  chain-wide, and cleared whenever the chain leaves `applied` — undo or a switch to
  `passed`. Unlike the outcome cache it has no history, so it is NOT restored on re-apply).
  No backfill: NULL outcome on an applied row *means* "no response recorded" — the
  follow-up bucket, pure SQL:
  `app_status='applied' AND outcome_status IS NULL AND status_date < cutoff`.
- **Deliberately NO CHECK** on `app_events.event_type`/`jobs.outcome_status`: user-decision
  vocabulary (like `app_status`), enforced code-side in `chain.record_event` against
  `states.ALL_EVENTS` — a CHECK would be a second frozen-CHECK liability outside
  `_rebuild_for_stale_checks`' jobs-only scope.

### Vocabulary & rules (states.py)
- Lifecycle events `recruiter_screen | interview (repeatable = rounds) | offer |
  rejected_by_employer | ghosted | withdrew` require the chain applied; `note` attaches
  free text to any posting and never sets the outcome.
- Latest event wins the cache (`event_date`, insertion-order tiebreak). Undoing `applied`
  clears the cache but KEEPS history; re-applying recomputes it back. `event --undo`
  deletes the chain's most recently *recorded* event.

### Surfaces
- CLI: `pipeline.py event --url X --type T [--date D] [--note N] [--undo]`;
  `applied --resume V`; `stats` gains a per-role outcome funnel.
- UI: applied cards get an outcome tag (or "no response — applied Nd ago"), record
  controls, lazy History timeline, Undo last, inline resume-variant field; the Applied
  button asks (optionally) for the variant. New `POST /api/event` + `GET /api/events`.
- Report: the ALREADY APPLIED banner appends the outcome
  (`2026-06-20 · interview 2026-07-01`).

---

## 2026-07-06 — `enablement-cluster` flag + deadline-insurance routing (guide only, no code)

### Why
A staffing-vendor "Senior Technology Consultant – GenAI & AI Adoption" posting passed the
eval (PASS, 15/18, bucket 3) while manual triage failed it categorically on role substance —
the second such divergence (an "AI Training and Adoption Consultant" posting, 2026-07-01).
Investigation showed the pipeline followed its spec: the guide's
role_substance gate only screens out *research* roles, and the management-drift note is
explicitly flag-only "until the pattern proves structural." It now has. But with the
search's deadline pressure and the cold-conversion history, pure-enablement roles are also
the highest-conversion slice of the funnel (~4-5 enablement-titled passes/day), so hiding
them in GATE_FAIL was rejected in favor of keeping them visible as flagged insurance.

### What changed (evaluation_guide.md + evaluation_guide.example.md — data, not code)
- **New `enablement-cluster` assistive flag** (Part 2, sibling of management-drift):
  responsibilities are entirely awareness/workshops/evangelism/adoption-playbooks with no
  build/own/ship verbs (strongest tell: self-declared "not hands-on" language). Gates
  still PASS; `title_trajectory` scored 0–1. Distinct from management-drift (managing
  real delivery) and from enablement-*engineer* roles with build content (no flag).
- **Part 2.5 insurance overlay:** flagged roles route like Bucket 2 — cold-apply OK,
  always below Bucket 3 in priority; permanent-FTE only (many are staffing-vendor seats).
- **Sunset written into the guide:** once an offer lands or the search's deadline passes,
  the cluster hardens into a role_substance hard FAIL.
- **Worked Example D** added (personal `evaluation_guide.md` only — the committed
  `.example` template carries the flag + routing text but still ends at Example C); two
  backtest cases added to the local `backtest_v2.py`
  asserting PASS + `enablement-cluster` flag.
- Also noted during investigation: `filters.yaml` has never existed, so the deterministic
  hard-filter layer has been a no-op every run. Left as-is deliberately — a title-based
  enablement rule was rejected (enablement-titled ≠ pure enablement; a major bank's
  "Legal GenAI Enablement – AI Practitioner" scored 18/18).

---

## 2026-07-05 — one eval per role chain; Adzuna URLs canonicalized to the ad id

### Why
A read-only investigation (local `tests/validation/investigate_adzuna_churn.py`) confirmed two
compounding leaks behind the ~3× eval-cost jump since Adzuna launched (2026-06-29). First,
Adzuna's `redirect_url` embeds a per-request tracking token (`?se=...`), so the same ad got a
fresh `job_url` (the PK) on every API call — 2,956 redundant rows from URL churn alone; the
fingerprint linked them as reposts but couldn't stop the insert. Second, `skip_decided_reposts`
only spared relistings of USER-decided chains, so a relisting of a merely-*evaluated* role
re-entered the paid eval on every cycle — 3,564 avoidable evals (~60% of daily spend). The
investigation also found repeat evals are NOISY: only 72% of multi-eval chains got the same
verdict every time, so re-evaluating was buying re-rolls of a noisy judge (and verdict flapping
between daily reports), not accuracy.

### What changed
- **Adzuna `job_url` is now canonical** (`fetch._adzuna_job_url`): built from the stable ad id
  (`https://www.adzuna.com/details/<id>`; the `id` field, else parsed from either observed
  `redirect_url` shape; raw-URL fallback if neither parses). Re-serves of the same ad now hit
  the PK's `ON CONFLICT DO NOTHING` — no row, no eval. Going-forward only; existing tokened
  rows stay and keep dedup'ing via the fingerprint.
- **New pre-eval pass `chain.skip_evaluated_reposts`** (runs right after `skip_decided_reposts`):
  a `'new'` relisting whose chain already holds ANY member verdict → `status='repost_evaluated'`
  (new `states.py` status), skipping the paid eval. Bidirectional like the decided pass: a
  `dupe --undo` unlink (or a cleared chain verdict) restores the row to `'new'`. The row's own
  verdict stays NULL — no copies to go stale.
- **The role's verdict is read through the chain**: `chain.effective_decision(s)` now also
  returns `chain_verdict` = the MOST FAVORABLE member verdict (`states.VERDICT_FAVOR`:
  PASS > RECRUITER_ONLY > GATE_FAIL). Rationale: the eval is a cheap pre-filter in front of a
  human — with a noisy judge, a false PASS costs seconds of triage while a false GATE_FAIL
  buries a role; `max()` is also order-independent, unlike "canonical's" or "latest". Spam that
  lucks into one PASS remains a `filters.yaml` / `reject --pattern` problem, as before.
- **Report**: new compact "Already-evaluated roles seen again (eval skipped)" section
  (title/company/chain verdict/age/link, most-favorable-first) — a PASS relisting still
  surfaces, it just isn't re-scored; the summary's repost-skipped count now includes these.
  **Web UI**: `row_to_dict` exposes `chain_verdict` (additive JSON field).
- Known accepted gap in "one eval per chain": two members of one chain first seen in the
  SAME run both get evaluated (no verdict exists yet when the skip pass runs) — bounded to a
  chain's debut run; the canonical-URL fix removes most same-run duplicates at insert.
- Deliberately NOT included: a "re-evaluate when full text arrives for a snippet-evaluated
  role" exception — only 5 of 14,058 chains would ever qualify. `prune` keeps
  `repost_evaluated` descriptions (mirrors the `repost_decided` precedent: an unlink can send
  the row back to eval, which needs the text).
- **Hardened by a third max-effort review round** (13 confirmed findings applied):
  - **Per-click cost**: the decision paths' reconcile is now CHAIN-SCOPED
    (`chain._reconcile_chain_skips`, indexable `(repost_of=? OR job_url=?)` form) — the
    round-2 global sweeps measured ~0.7–1.0s on every applied/passed/reject click; the
    scoped form measured ~90ms. Policy folded in: inline reconciles run decided(both) +
    evaluated(restore only), so an undo-released row honestly shows as 'new' and re-faces
    the current rules in the next run before any label spares it the eval (the previous
    inline evaluated-forward skip bypassed that contract). Reconcile failures after the
    decision commit degrade to a warning — the decision is durable, labels self-heal.
  - **Sort fallback scoped**: the today-view chain-fit fallback is gated on the two
    eval-skip statuses — a salary-filtered or description-less relisting of a scored chain
    no longer outranks genuinely scored cards.
  - **`apply_hard_filters` never clobbers an existing attribution** (`filter_source IS
    NULL` guard): a row rejected while in 'error' kept its manual attribution through
    requeue, so `reject --undo` works instead of silently no-oping.
  - **Rebuild hardening**: hand-added columns survive the swap with quoted identifiers
    (keyword/spaced names) and carried DEFAULT/NOT NULL; the off-vocabulary error now names
    values the OLD schema also accepts (the previous instruction was self-defeating — the
    stale CHECK rejects new values); the CHECK probe tolerates quoted identifiers and
    treats an unparseable CHECK as stale (rebuild) rather than absent (skip);
    `in_transaction` is pinned before BEGIN IMMEDIATE; the RuntimeError gets the clean
    `[db]`-style exit in both front-ends instead of a traceback.
  - **Report**: the `not filter_source` double-count guard now covers ALL status buckets
    (errors, salary-filtered, repost-decided); the eval-skipped section prints 'first seen'
    only for actual reposts. The decided pass's restore direction gained the same own-row
    decision guard as the evaluated pass (symmetry; legacy rows only).
  - Cleanups: `states.sql_list` (one owner for the quoted IN-list idiom), the UI badge
    shows the chain's fit score (`↻ chain PASS 14/18`), plus ordering/regression tests for
    the sort fallback, bucket guards, attribution guard, and keyword-column rebuild.
  - Declined as over-engineering: eval-roster grouping for the same-run debut gap
    (documented above instead), boolean-kwarg API reshaping, and aesthetic reflows of
    already-hardened code.
- **Hardened by a second max-effort review round** (13 more confirmed findings applied):
  - **Stage order**: the skip passes' RESTORE direction now runs BEFORE the salary/hard filters
    (their FORWARD direction stays after) — a row released back to `'new'` re-faces the current
    rules instead of slipping straight to the paid eval past a rule added while it sat skipped.
  - **Rebuild robustness**: the stale-CHECK probe reads each column's CHECK clause (not the
    whole DDL, whose comments contain quoted words that masked future collisions); the swap runs
    under an explicit `BEGIN IMMEDIATE` (its atomicity no longer rides on Python sqlite3's
    legacy transaction mode); hand-added columns are carried through the swap instead of
    silently dropped; and off-vocabulary stored values abort with an actionable message instead
    of a bare IntegrityError bricking every command.
  - **Decisions reconcile immediately**: `mark_posting`/`reject_posting` now run both skip
    passes like the dupe paths, so a UI/CLI decision upgrades or releases skipped rows at once.
  - **Report/UI consistency**: the `repost_decided` summary term got the same
    `not filter_source` double-count guard; the scored-card banner gained the chain-reject
    marker the tag already had (and the tag stops repeating it inside the Hard-fail section);
    the "(model under-filtered)" note reads `chain_verdict`; the eval-skipped section is titled
    for roles, not reposts (still-'new' canonicals land there too); and the UI today-view sort
    falls back to the chain's fit score (`chain_fit_score`) so a PASS-chain relisting no longer
    sinks to the bottom band.
  - **Adzuna URL edge cases**: the ad-id regex is ASCII-only and searches only the URL path
    (a query-string id is another page's id — minting from it collided distinct ads on one PK);
    a malformed or scheme-less `redirect_url` falls back to the raw URL instead of raising
    (one bad row aborted the whole Adzuna batch) or minting a wrong-country host.
- **Hardened by a max-effort review before landing** (13 confirmed findings applied):
  - The "evaluated" predicate counts JUDGE verdicts only (`status='evaluated'`) — the synthetic
    `GATE_FAIL` a filters.yaml rule stamps must neither suppress the safety-valve eval the
    decided pass's docstring reserves for drifted relistings, nor masquerade as a chain verdict.
  - Both skip passes key on `COALESCE(repost_of, job_url)` in BOTH directions: a still-'new'
    canonical whose verdict/decision sits on a sibling (requeued error rows, dupe merges,
    `applied` before eval) is now skipped too, and an unlinked ex-canonical is no longer
    stranded by `repost_of NOT IN`'s NULL-false (a pre-existing `repost_decided` bug).
  - The decided pass upgrades `repost_evaluated` → `repost_decided`; `_REJECT_SET` lifts
    `repost_evaluated` → `rule_filtered`; the evaluated reverse pass restores only UNDECIDED
    rows — together closing a leak where a rejected-then-unlinked row re-entered the paid eval.
  - Stale-CHECK migration (`core._rebuild_for_stale_checks`): a DB whose baked-in status/verdict
    CHECK predates a vocabulary addition is rebuilt once at startup instead of aborting every
    run with IntegrityError (SQLite can't ALTER a CHECK; this guards all future additions too).
  - Adzuna canonical URLs keep `redirect_url`'s host (country-correct site, not hardcoded
    `.com`) and require a `redirect_url` (id-only degenerate results are skipped, as before).
  - Report: chain-rejected skipped rows render only under Hard-fail (no double-count); the
    repost banner reads `chain_verdict` (no more "prior verdict None"); rejected chains get an
    inline `🚫 rejected` marker. Web UI: the card badge falls back to the chain verdict, so an
    eval-skipped PASS relisting is visible in the today view. New `idx_status` index.

### Expected effect
~450 avoidable evals/day stop (~$0.22/day → run cost from ~$0.35 toward ~$0.10–0.15), the DB
stops accreting URL-churn rows, and a role's verdict is stable across daily reports instead of
re-rolling with each relisting.

---

## 2026-07-05 — status-machine hardening: error retry, fail-fast auth, schema CHECKs, prune

### Why
Three failure paths were rougher than the rest of the pipeline: `status='error'` rows were
dead-ends (nothing ever re-read them — a transient DeepSeek outage stranded its batch forever),
the eval retry loop treated a dead API key like a rate limit (3 retries × N rows × backoff
sleeps for the same 401), and a `config.yaml` typo surfaced as a bare `KeyError` deep inside a
fetch stage. Separately, `jobs.db` grew without bound (rejected postings keep their 12KB
descriptions forever).

### What changed
- **Error rows are requeued.** A new `run` stage (`evaluation.requeue_error_rows`) flips
  `status='error'` → `'new'` right after the fetchers, so provider-outage casualties are
  retried automatically on the next run instead of stranding. It runs BEFORE the deterministic
  filters on purpose: a requeued row re-faces the salary filter, the current hard rules, and
  `skip_decided_reposts`, so a rule added (or a chain decision made) while the row sat in
  'error' still catches it before the paid eval.
- **Retryable vs. fatal eval errors.** Only 408/429/5xx/non-HTTP failures are retried; any
  other 4xx fails the row immediately (our request is wrong — retrying triples the cost for
  the same failure), and a 401/403 aborts the whole batch (`EvalAuthError`) leaving unevaluated
  rows `'new'` for a run with a fixed key.
- **Config shape is validated at load** (`core.validate_config`): required settings keys,
  searches-list shape, and the provider/model consistency check (moved out of the eval stage)
  all die at startup with one collected message, before any fetch/eval spend.
- **Status/verdict vocabulary centralized in `states.py`**, and fresh databases get
  `CHECK (status IN (...))` / `CHECK (verdict IN (...))` constraints (existing DBs are not
  rebuilt — the code-side constants are the enforcement that covers both). To keep those
  CHECKs fail-loud, the fetchers' `INSERT OR IGNORE` became `INSERT ... ON CONFLICT(job_url)
  DO NOTHING` (needs SQLite ≥ 3.24): only the PK duplicate is skipped; any other constraint
  violation now raises instead of silently dropping the row.
- **New `prune` command** (`pipeline.py prune [--days N] [--vacuum]`): clears descriptions of
  GATE_FAIL / salary-filtered rows older than N days (default 90). Never touches gates-passed
  rows (backtest_v2 re-evaluates those from stored text), repost-skipped rows, or undoable
  manual rejects; `eval_json` is kept everywhere.

(Same day, no judgment change: the `pipeline.py` re-export facade was removed — consumers
import the owning modules directly; the CLI and web UI now share `chain.mark_posting` /
`reject_posting` service cores; the web UI opens plain per-request connections after a
one-time schema pass and pins the `Host` header against DNS rebinding.)

---

## 2026-07-04 — posting recency as a triage signal + every-3h runs

### Why
BI/SA postings collect hundreds of applicants within hours of going live; an application a day
later is rarely seen. The pipeline captured `date_posted`/`first_seen` for all three sources but
never used them: everything sorted `fit_score DESC` with no age shown, and the 2×/day schedule
meant most postings were half a day old before triage.

### What changed
- **Two-band triage order (`report.recency_sort_key`, shared by report + web UI).** Within each
  report section and in the UI's today/backlog views: postings at/above the **apply line**
  (fit ≥ 10 — `score_band`'s existing "acceptable" threshold, not a new number) sort
  **freshest-first** with fit as tiebreak — freshness is king where the role is worth applying
  to; below the line, fit-only (recency last tiebreak). Applied/passed views keep
  `status_date DESC` (decision history, not triage).
- **Age labels everywhere (`report.posting_age`).** `🕐 3h ago` / `2d ago` on every rendered
  posting (report headers, one-liner sections, UI cards). Precision degrades honestly: real
  timestamps (Adzuna; ATS boards, whose full posted-at is now stored, not truncated to a date)
  → hours; a date-only posting date at/after the fetch day falls back to `first_seen`, hedged
  as `seen 3h ago` (a lower bound, never claimed as posting time); an OLD date-only posting
  (ATS backlog) shows its real day-granularity age and can NOT masquerade as fresh via a recent
  `first_seen`. LinkedIn's guest scrape currently returns no posting date (verified across the
  full DB), so in practice LinkedIn rows carry the hedged `seen Xh ago` form — under the
  3-hourly cadence that bounds true posting time within ~4h; if jobspy starts returning dates,
  the day-only paths handle them. One implementation (core.parse_iso → report._recency_dt)
  feeds the fetch-side normalization, the label, and the sort key, with a sanity window so a
  placeholder date ("9999-12-31") can neither crash the sort nor pin itself to the top.
- **Recency is triage metadata only** — deliberately NOT an eval-prompt input (a one-time verdict
  must not embed a time-sensitive fact) and NOT a filter (old postings stay visible, just lower).
- **Cadence: 2×/day → every 3h** (Task Scheduler 8:00–23:00, 6 runs/day) with `hours_old` 13 → 4.
  Known tradeoff: postings created ~23:00–04:00 on LinkedIn are picked up late-or-never; Adzuna's
  1-day lookback and the full ATS board fetch backstop those sources overnight.
- Plumbing, same motivation: logs are now per-day (`logs/pipeline-YYYY-MM-DD.log`, 30-day
  retention) instead of one size-rotated file — 6 runs/day interleaved unreadably.
- A `run` keys its report to the date the run STARTED, not the date it finishes — a 23:xx run
  dragging past midnight (throttled fetch) previously filed its report under the new day,
  leaving its own postings (first_seen 23:xx) in no report at all.
- No schema change (`date_posted`/`first_seen` already existed); no judgment/verdict change.

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
  every sampled one a genuine same-title relisting (`Data Analyst`, `Forward Deployed
  Engineer`, `SR HRIS ANALYST` — same title, same poster, across days).
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
Field results from the v1 framework showed high-scoring primary-tier cold applications
failing to convert. The framework scored roles correctly *as fits* but couldn't tell whether
an application would *clear the screen*. Two structural blind spots:

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
