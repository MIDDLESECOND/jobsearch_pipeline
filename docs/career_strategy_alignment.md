# Career Strategy Alignment

- Decision authority: `D-2026-08-03-CAREER-REALIGNMENT`
- Canonical decision: `D:\Github\learning_path\decisions\D-2026-08-03_CAREER_REALIGNMENT.md`
- Canonical strategy: `D:\Github\learning_path\CAREER_STRATEGY.md`
- Downstream scope: job-posting evaluation semantics only; this project does not redefine long-term career strategy.
- Effective date: 2026-08-03

## Implemented

- “Eval,” “evaluation,” and “benchmark” are no longer research proxies. The evaluator must identify the evaluation object: foundation-model/research contribution versus production application/workflow behavior.
- Foundation-model research, from-scratch training/tuning, research benchmark creation, published-research requirements, and core model-algorithm/training research remain out of scope.
- Production/application evaluation, eval harnesses, regression testing, reliability/observability, verifiers, human-in-the-loop systems, deployment validation, agent/workflow quality measurement, error taxonomy, cost per accepted outcome, and production safety/governance testing are explicitly in scope.
- Current held capability and building/not-yet-held production capability are separated. Strategic interest does not raise current artifact depth or turn mature agentic/SDK requirements into a cold-apply PASS.
- Every gates-passed evaluation must answer capability, screenability, and career capital separately. Career capital reuses the existing `one_line` explanation and is non-scoring.
- BA/BI remains tactical fallback. Pure reporting/dashboard/documentation carries an explanatory trajectory risk, not a new gate; automation, AI workflow, system ownership, or an internal-transformation path can still provide useful capital.

## Manual adjudication matrix

These anonymized archetypes preserve the expected semantics of a small set of previously adjudicated historical role patterns without copying private JD text.

| ID | Historical archetype | Expected invariant |
|---|---|---|
| PAE | Production AI deployment plus application/workflow eval | Must not fail `role_substance` merely because the JD says eval, evaluation, or benchmark. Evaluate other gates and current artifact depth normally. |
| RMT | Foundation-model research benchmark or from-scratch model training | Remains `GATE_FAIL` on role substance when that is the core work. |
| BAD | Pure BA/reporting/dashboard/documentation | Remains tactical fallback under existing scoring/routing; `one_line` states trajectory risk and missing production/build capital without adding another penalty. |
| FDE | FDE/ADE requiring mature production agentic SDK experience | Remains recruiter/stretch through the existing artifact-depth rule; no cold-apply PASS based on strategic interest. |
| ENA | Enablement-only role | Existing enablement rule and routing remain in force. Career-capital wording must not add a duplicate penalty. |
| MGT | Management-drift role | Existing management-drift flag/title treatment remains in force; career capital does not create a second penalty. |
| FUN | Core pre-sales/post-sales/quota function with no career precedent | Existing function-precedent cap remains in force. |
| LDR | Required formal people-leadership tenure | Existing `formal_leadership_required` cap remains in force. |
| AUT | Citizenship, clearance, or clearance-eligibility requirement | Existing work-auth gate remains in force; “authorized to work without sponsorship” remains a PASS fact. |

## Recorded only

- Near-term bridge role families and medium-term observation/stretch role families are explanatory positioning. They do not change gates, scores, buckets, verdicts, thresholds, or application allocation.
- Career-capital examples are interpretive prompts, not claims that every role with a matching title provides that capital.

## Deferred

A structured career-capital representation is deferred. A future design could add one non-routing object inside `eval_json`, for example a compact `career_capital` object with `builds` and `lacks` lists. It should be considered only with a separate schema/output-contract decision, migration/backward-compatibility review, UI/report rendering plan, and tests proving it cannot affect verdict, score, gate, bucket, or channel. No SQLite column or structured output field is added in this change.

## Explicitly unchanged

- Permanent-FTE-only, work-authorization, no-sponsorship-needed, citizenship/clearance, years, function-matched tenure, formal-leadership, function-precedent, compensation/location/remote/relocation, and no-inflation boundaries.
- Current artifact depth and the separation between aspirational fit and present screenability.
- The 0–18 score dimensions and thresholds, Bucket 1/2/3 definitions, verdict vocabulary, application quotas/allocation, deterministic filter order, and database schema.
- Historical evaluations and verdicts. No database write, deletion, full-corpus rerun, or report reconstruction is part of this alignment.
