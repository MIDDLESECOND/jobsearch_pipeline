# Job Posting Evaluation Guide — EXAMPLE TEMPLATE

Copy this to `evaluation_guide.md` and adapt it to your own situation (it pairs with
`profile.md`). `evaluation_guide.md` is gitignored so your tailored version stays local.

This example is written for a **hypothetical low-code AI / automation candidate** whose
strongest shipped artifact is a production AI Builder + Power Automate classification/
extraction deployment. The *framework* (hard gates → split AI scoring → bucket/channel
routing → verdict cap) is the reusable part; the specific artifact references are
illustrative — swap in your own.

**How to use:** Run Part 1 (hard gates) FIRST. If any gate fails, stop and log the reason —
do not score fit. Only proceed to Part 2 if all gates pass. Always name the trade-offs
explicitly (what's foregrounded, what's de-emphasized) and steelman the unflattering reading
before the generous one.

**Scope note:** In this example, location is open (relocation-willing) and industry is open —
any sector is fine *as long as the role doesn't require sector experience not held*. Because
domain and location don't screen here, the binding filter has shifted to **AI-depth realism**
and the **tool-requirement gate**. AI-depth realism has two parts, and conflating them is
what produces a "high score, zero conversion" result.

**Critical lesson (the "50/0" finding):** A candidate applying an earlier version of this
framework found high-scoring primary-tier cold applications failing to convert. The framework
was scoring roles correctly *as fits* and missing whether the application could *clear the screen*.
Two structural blind spots caused it:

1. **AI-depth realism was doing two jobs at once.** "Is this applied AI, not research?" (the
   artifact passes cleanly) was tangled together with "can my current artifact *evidence* the
   AI depth this role requires?" (often it can't — the market floor for AI-titled delivery
   roles has moved to production agentic systems, a generation ahead of a low-code
   classification deployment). A role can be genuinely applied-AI *and* require a depth the
   artifact can't show. The old single dimension scored those 15–16/18 and said APPLY.
2. **A high fit score overrode a known structural screen-out.** The interview-vulnerability
   section repeatedly flagged "your strongest artifact is classification/extraction, not
   orchestration" — correctly — but it never changed the apply/skip verdict, because the total
   kept winning. The signal was there; it wasn't load-bearing.

The fixes below make the artifact-depth question its own scored line and give it a **verdict
cap** so it can't be outvoted, plus a **channel-routing rule** so AI roles that fail it route
to recruiters instead of dying in an ATS.

---

## PART 1 — HARD GATES (any FAIL = stop, don't apply)

Evaluate *before* reading bullets or assessing skill fit. A single fail kills it.

| Gate | Question | PASS / FAIL | Notes |
|---|---|---|---|
| **Years floor** | Stated minimum ≤ ~5–6 yrs? (Tune to your own years of experience.) | | |
| **Domain requirement** | Does the role *require* sector experience not held — or will it accept domain-naivety and ramp? Industry subject is irrelevant; the **requirement** is what gates. FAIL only when prior sector experience is a stated qualification. | | |
| **Role substance** | Is the *work* integration/delivery/BA/BI/applied-AI — not from-scratch model training, evals/benchmarks, or "published work"? (Title can say "Architect" and still fail this.) | | |
| **Tool requirement** | Any *specific named tool or platform with years attached* that I lack and that is genuinely non-rampable? Willingness to learn clears ramp-able tools only. **Do NOT fail this gate just because the role requires production agentic/orchestration AI depth beyond my artifact** — that depth is *buildable*, so it CLEARS this gate and is handled instead by the `ai_artifact_depth` line (which scores it 0 → RECRUITER_ONLY). A role built *on* agentic systems is the canonical Bucket 1 case, not a gate fail. | | |
| **Work auth** | No requirement that can't be met? (This gate is just "any disqualifier?" — never a reason to self-screen.) | | |
| **Employment type** | Is the role permanent full-time (or whatever you require)? FAIL if it's contract, contract-to-hire, temporary, fixed-term, part-time, internship/co-op, or staffing-agency W2-contract. A recruiter posting a *permanent* placement is fine; the fail is the *engagement type*, not the intermediary. When unstated, default PASS but flag it. | | |

*Location is not a hard gate in this example — relocation-willing. Capture relo/comp logistics in Part 4 instead.*

---

## PART 2 — FIT SCORING (only if all gates passed)

Score each 0–3. **0** = absent/wrong, **1** = weak/adjacent, **2** = solid match, **3** = strong/foregrounded.

The two starred AI lines are the primary filters. They are **separate** — do not merge them.
A role can pass the first and fail the second; that combination is the single most common
reason a high-scoring role still won't convert cold.

| Dimension | What "strong" looks like | Score (0–3) | Evidence to cite |
|---|---|---|---|
| **AI-realism: applied vs. research** ⭐ (`ai_applied_vs_research`) | What they want = applied AI / production deployment / prompt eng / integration — NOT research depth (model training/tuning, evals, published work) that would have to be invented. Score on whether the *role* is applied vs. research. **Score the SEAT, not the company:** an "AI-native" employer whose seat's only AI content is "use/explore AI tools to work faster" is a convenience layer, not AI work — score 0–1. | | |
| **AI-realism: artifact-evidences-required-depth** ⭐ (`ai_artifact_depth`) | Does my **current shipped artifact** evidence the AI depth this role lists as **required**? **3** = exactly what I've shipped (e.g. low-code GenAI automation, prompt design, classification/routing). **1–2** = adjacent but a step beyond (some orchestration, light agent work). **0** = a generation ahead (production agentic systems, multi-agent orchestration, LangChain/CrewAI/LangGraph/MCP as a *built* requirement, SDK/connector/middleware engineering). *This is the line a single AI dimension is blind to.* | | |
| **Learning value** (`learning_value`) | Does the role *grow* AI capability — a step beyond current depth? Note: a role can be high learning value AND score 0 on artifact-depth — that's the Bucket 1 trap. High learning value is a reason to *want* the role, not evidence you can *land* it cold. | | |
| **Technical skill match** (`technical_skill_match`) | Core skills map to required (not "plus") skills — OR gaps are ramp-able, not stated core requirements. | | |
| **Title trajectory** (`title_trajectory`) | Lateral or modest step up; not a de-level, not a 2-rung reach. | | |
| **Years vs. stated req** (`years_vs_stated`) | Comfortably inside the band (vs. just clearing the floor) — **against the FUNCTION-MATCHED tenure** from the profile's by-function split, not the total (a recent title change means "N yrs as [new title]" measures against the short tenure). | | |

**Total: ___ / 18.**  14–18 = strong, tailor and apply. 10–13 = acceptable-tier, apply only if friction is low. <10 = likely pass.

**⭐ Starred-line rules (these override the total):**
- If *applied-vs-research* (`ai_applied_vs_research`) scores 0–1 → near-disqualifying regardless of total. Two mirror-image failure shapes: a research role wearing a delivery title, and a barely-AI seat wearing an AI-company logo (there, `ai_artifact_depth`'s 3 is vacuous — the required depth is ~zero, so the score carries no signal and does not rescue the role).
- **If *artifact-evidences-required-depth* (`ai_artifact_depth`) scores 0 → the verdict is CAPPED at "RECRUITER_ONLY," regardless of total.** A 16/18 with this line at 0 is NOT the same role as a 16/18 with it at 3. Cold-applying the former is the "50/0" pattern. It does not become a PASS just because every other line is strong.
- **Formal-leadership check (code-enforced cap, like the artifact-depth line).** If the posting's *required* qualifications state N+ years of formal **people leadership / management / technical program management** the candidate lacks (per the profile's leadership line), set `formal_leadership_required: true` in the output — the verdict is CAPPED at RECRUITER_ONLY regardless of total. Boundaries: (a) *required*, not preferred; (b) formal authority over people, not stakeholder/project leadership or mentoring; (c) if the leadership requirement makes the whole role management-of-delivery, the years-floor or role-substance gate may fail it first.

**Enablement-cluster (assistive flag, not a cap).** The pure-enablement false positive: a role whose *entire* responsibility set is awareness campaigns, workshops/training, evangelism, adoption playbooks, and tool-selection guidance, with **no build/own/ship verbs anywhere** (strongest tell: self-declared "not hands-on" language in the posting itself). It still passes the gates — it IS applied-AI work, not research — so do not gate-fail it: emit an `enablement-cluster` flag, score `title_trajectory` honestly (0–1), and route it as deadline insurance (below Bucket 3 priority; see Part 2.5). Enablement-in-title with real build content (enablement *engineer/developer* roles) gets no flag — read the responsibilities, not the title. Decide deliberately whether this cluster should harden into a role-substance gate fail once your deadline pressure lifts (e.g., after an offer lands).

---

## PART 2.5 — BUCKET + CHANNEL ROUTING (read before setting the verdict)

Every role that passes the gates falls into one of three buckets. The bucket determines the
*channel*, not just the verdict — the lever the 50/0 result says matters most: in this
example the only applications that converted came through recruiters/referrals; cold portal
applications converted near zero.

**Bucket 1 — AI roles where required depth is a generation ahead (`ai_artifact_depth` = 0).**
High learning value, genuinely applied (not research), real trajectory target — *and* gated
at the screen on production agentic/SDK depth the current artifact can't show.
- **Channel: RECRUITER / REFERRAL ONLY.** A human can hear "here's my ramp plan and here's
  what I've shipped"; an ATS screen cannot. Do **not** cold-apply.
- **Verdict: RECRUITER_ONLY** (never PASS cold), until the ramp produces a shippable artifact.
- These are the *target* tier — deferred to the channel that can carry a gap narrative, not abandoned.

**Bucket 2 — Acceptable-tier roles where overqualification is the silent filter.**
A senior applicant applying to a junior-coded title reads as flight risk; the rejection is an
invisible auto-filter.
- **Channel: cold-apply OK, but only where the title gap is small** (Senior/Lead/"II" with a
  real AI/automation angle) — not a 2-rung drop. Weight these as deadline-insurance, not trajectory.

**Bucket 3 — Clean delivery roles where the artifact IS the job (`ai_artifact_depth` = 3).**
Production GenAI automation, prompt design, classification/routing, low-code platform delivery —
where the required bar is "you've shipped a working AI workflow," not "you've built agent orchestration."
- **Channel: cold-apply is fine — this is where cold conversion is realistic.** This is the
  slice where AI-realism and landability *agree*. Concentrate cold-application effort here.

**Standing-allocation escape valve:** audit your own cold-channel response data periodically.
If cold portal applications flatline across a meaningful sample (with mechanical causes — PDF
parsing, knockout answers — ruled out first), tighten the allocation rather than the scoring: keep
PASS as the *eligibility* standard but restrict actual cold applies to fresh, high-fit Bucket 3
(e.g. fit ≥ 15, posted ≤ 14 days) and redirect the freed hours to recruiter threads and inbound
optimization. Volume and priority change; gates and scoring don't.

**Enablement-cluster overlay:** flagged pure-enablement roles (see Part 2) route like Bucket 2 —
cold-apply OK as deadline insurance, always below Bucket 3 in priority.

**AI-recruiter-intermediary overlay (lead-gen only).** Some agencies post via an AI recruiter —
the posting's boilerplate says an AI agent screens candidates on the client's behalf, and the
client is often anonymized ("VC-backed…", "stealth"). Never apply through the AI funnel: it can't
hear a gap narrative like a human recruiter, and it's an unverifiable intermediary unlike a
portal. Score the role normally and emit an `ai-recruiter-intermediary` flag; if the client is
named in the title, treat the posting as a *lead* (pursue the company directly or via a human
recruiter — the salary band is negotiating intel); if anonymized, skip.

**Routing summary:** Cold applications (verdict **PASS**) → Bucket 3 first, small-gap Bucket 2
second, flagged `enablement-cluster` roles as insurance behind both. Bucket 1 and
`formal_leadership_required` roles → verdict **RECRUITER_ONLY**, recruiters and referrals only.
The fix for a 50/0 is routing, not de-prioritizing AI.

**Cold-apply bar:** a PASS means more than "conceptual fit" — cold-apply only when the resume
as written **directly proves every requirement in the posting's required column** (tools used
in production, function-matched years met, no unheld leadership requirement, no title reach).
A role that needs any *explaining* goes through a human channel that can carry the
explanation; an ATS screen cannot. When in doubt, that's what RECRUITER_ONLY is for.

---

## PART 3 — INTERVIEW VULNERABILITY CHECK

- Biggest gap vs. requirements: _______________________
- Addressable with honest framing (vs. fabrication)? **Y / N**
- Any resume number that wouldn't hold up if pressed on *this* role's terms? _______________________
- **Is the biggest gap in the role's *required* column or *preferred* column?** Required +
  artifact-depth 0 → confirms Bucket 1 → recruiter channel. Preferred → honest-disclosure item, cold-apply OK.

---

## PART 4 — APPLICATION CONFIG

- Bucket (1 / 2 / 3) and resulting channel: _______________________
- Resume variant: _______________________
- Work-auth phrasing: _______________________
- Location / relocation: _______________________
- Learning orientation: willing to ramp on industry-standard, AI-related tools. **Boundary:**
  this covers tools the role treats as *ramp-able*. It does **not** convert a stated *core
  requirement with years attached* that I lack into a match.
- Effort-weighted verdict: **PASS (cold-apply)** / **RECRUITER_ONLY** / **GATE_FAIL**
- One-line reason: _______________________

---
---

## WORKED EXAMPLES

These illustrate the framework. The first two fail Part 1; the third passes the gates, scores
high, and *still* should not be cold-applied — the "50/0" pattern made concrete. Company names
are public postings used illustratively.

### Example A — a Big-4 "AI Center of Excellence Solutions Architect"

**Verdict: GATE_FAIL (role substance).**

The title is primary-tier ("Solutions Architect"), but the substance is an AI research/
experimentation role: model training/tuning, designing AI experiments, building evals and
benchmarks. Qualifications make it explicit ("preferably... model training/tuning, performance
evaluations incl published work"). A low-code classification/extraction artifact is real and
valuable but explicitly NOT from-scratch model development — those interview questions can't be
answered without inventing experience, which violates no-inflation. Category fit (right employer,
right neighborhood) doesn't rescue a role whose core requirements would have to be fabricated.

### Example B — a "Senior Manager, Business Process Architect (Financial Services)"

**Verdict: GATE_FAIL (years floor, plus domain requirement).**

"15+ years of experience with a minimum of 6 years in business architecture... in Financial
Services" against a 4-years-total profile is a structural mismatch no tailoring closes. It also
fails the domain gate — not because finance is the wrong industry (industry is open), but because
FS-operations experience is a **stated qualification**, not a domain the role will ramp you on.
The right-level role here is a Staff/Senior Consultant or Manager-track seat, not Senior Manager.

### Example C — an AI startup "Solutions Engineer" (the 50/0 / Bucket 1 pattern)

**Verdict: PASSES ALL GATES, scores ~15/18 — and is RECRUITER_ONLY, not a cold PASS.**

This is the case a single AI dimension gets wrong. Primary-tier title, genuinely applied AI
(not research), industry-neutral, comfortably inside the years band. Under the old single
AI-realism dimension it scored 15/18 with both stars at 3 and the verdict was APPLY — and cold
applications of this shape converted at near zero.

- **All hard gates: PASS** (no years wall, no domain requirement, applied-not-research substance,
  no missing years-attached named tool, work auth fine, permanent full-time).
- **AI-realism: applied vs. research → 3.** Correctly applied/delivery — the right *category*.
- **AI-realism: artifact-evidences-required-depth → 0.** The role requires SDK/connector/
  middleware engineering and treats agent orchestration as built, not ramp-able. A low-code
  classification/extraction artifact is a generation behind. In a remote, high-volume posting,
  the pool includes people who hold exactly this as a strength.
- **Learning value → 3.** The trap: high learning value made it *look* like a strong apply.
  Learning value is why it's a target — not evidence the application clears the screen.

**Why RECRUITER_ONLY, not GATE_FAIL:** Bucket 1 roles are the actual target tier — abandoning
them is wrong. But cold-applying spends runway on a screen the artifact can't pass yet. So the
verdict is RECRUITER_ONLY: pursue via a recruiter or referral who can carry the ramp narrative,
and re-evaluate the moment the ramp ships something. The cap is not "this role is bad" — it's
"this channel is dead for this role right now."

### Pattern across all three

A and B catch wrong-substance and wrong-level roles at the gate. C catches the subtler failure:
a role that passes every gate and scores high can still be uncatchable through the cold channel
because the artifact is a generation behind the *required* AI depth. The split AI-realism
dimension surfaces it; the verdict cap stops a high total from overriding it; the bucket routing
sends it to the channel that can actually carry it. The response to a 50/0 is routing AI roles to
the right channel and concentrating cold volume where the artifact matches the bar — not lowering
the AI weighting.
