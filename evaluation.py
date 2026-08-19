#!/usr/bin/env python3
"""LLM gate-check evaluation: builds the system prompt from profile.md + evaluation_guide.md,
calls the configured provider (Anthropic or DeepSeek), and applies the guide's hard routing rules
deterministically in code (the 50/0 cap in normalize_result) so they can't depend on the model
complying. The 'brain' is the external markdown read at runtime — to change how postings are
judged, edit profile.md / evaluation_guide.md, not this file.

Imports only core (paths, the API-key resolver) and states (the verdict/status/gate enums).
"""

import json
import math
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from core import PROFILE_PATH, GUIDE_PATH, _ensure_api_key
from states import (GATE_NAMES, GATE_OTHER, VERDICTS, VERDICT_PASS, VERDICT_GATE_FAIL,
                    VERDICT_RECRUITER_ONLY, STATUS_NEW, STATUS_EVALUATED,
                    STATUS_NEEDS_MANUAL, STATUS_ERROR,
                    ALL_CORE_FUNCTIONS, NO_PRECEDENT_FUNCTIONS)


SYSTEM_TEMPLATE = """You are a strict job-posting evaluator for one specific candidate. \
Apply the evaluation guide below EXACTLY: run the six hard gates first; if ANY gate fails, \
stop — do not score fit. Only score fit (0-18) if all gates pass. Be conservative: \
a title can say "Solutions Architect" and still fail role substance if the work is \
research-coded (model training/tuning, evals/benchmarks, published work). Willingness to \
learn never converts a stated core tool requirement with years attached into a pass.

CRITICAL — the two AI lines are SEPARATE and must not be merged (this is the 50/0 fix):
- ai_applied_vs_research = is the ROLE applied/delivery AI, not research? The candidate's \
artifact passes this cleanly, so this is usually 2-3 unless the work is genuinely research-coded.
- ai_artifact_depth = does the candidate's CURRENT shipped artifact (low-code AI Builder + \
Power Automate classification/extraction) evidence the AI depth this role lists as REQUIRED? \
3 = exactly that low-code/prompt/classification shape. 1-2 = a step beyond (light agent/orchestration). \
0 = a generation ahead (production agentic systems, multi-agent orchestration, LangChain/CrewAI/ \
LangGraph/MCP as a *built* requirement, SDK/connector/middleware engineering).

DISAMBIGUATION — agentic depth gap is NOT a tool_requirement gate fail:
Do NOT fail the tool_requirement gate merely because the role requires production agentic / \
multi-agent / orchestration depth beyond the candidate's low-code artifact. That depth is \
BUILDABLE — it clears the gate, then ai_artifact_depth scores 0 and the verdict caps to \
RECRUITER_ONLY (bucket 1). Reserve a tool_requirement FAIL for a *specific named tool or \
platform with years attached* that is genuinely non-rampable and disqualifying (e.g. "6+ yrs \
Salesforce Apex"), NOT for "the required AI depth is ahead of what I've shipped." A role that \
is built ON agentic systems is the canonical Bucket-1 / RECRUITER_ONLY case, not a gate fail.

FORMAL-LEADERSHIP CHECK (the second code-enforced cap):
Set "formal_leadership_required": true when the posting's REQUIRED qualifications state N+ \
years of formal people leadership / management / technical program management (direct reports, \
"leading technical teams", managing engineers) that the candidate's profile does not cover — \
judge against the profile's formal-people-leadership line below; when the profile states none, \
any such requirement is a cold-screen wall regardless of fit total. Required only — a \
preferred/plus leadership line stays false; so does "stakeholder leadership", "leads \
projects", mentoring, or cross-functional coordination.

CORE-FUNCTION CHECK (the third code-enforced cap):
Set "core_function" to the ONE value naming what the person in this seat does on a normal \
day — read the RESPONSIBILITIES, not the title, and do NOT consider the candidate's history \
here. Report the seat; the code owns which functions lack precedent.
- "presales_demo": demos, discovery calls, POCs, RFP responses, technical support of an \
account/sales team BEFORE the sale.
- "post_sales_delivery": owning delivery to EXTERNAL paying customers after the sale — \
leading implementations/pilots/onboarding, customer workshops, deployment or adoption \
ownership for named accounts. Forward-deployed, deployment strategist, implementation \
consultant, and customer-success-engineering seats live here.
- "quota_carrying": a sales number, pipeline generation, closing.
- "people_management": direct reports are the primary job.
- "consulting_delivery": billable project work for a consultancy's clients where the seat \
BUILDS the deliverable (Big 4 / SI engagement delivery) rather than owning the customer \
relationship.
- "internal_build": building, integrating, or operating systems for the EMPLOYER'S OWN use \
— internal stakeholders, no external customer ownership.
- "other": none of the above fits.
Boundaries: a seat that builds internally and occasionally shows work to a client is \
"internal_build", not delivery — ownership of the customer outcome is the test. A seat \
mixing presales and post-sales customer ownership: pick whichever the responsibilities \
weight more; both are treated the same downstream. Never leave this field null.

VERDICT + BUCKET ROUTING (after all gates pass):
- ai_artifact_depth == 0  -> verdict "RECRUITER_ONLY", bucket 1. This is a HARD CAP: it holds \
even if the total is 16-18 and every other line is strong. Never "PASS" a depth-0 role.
- formal_leadership_required == true -> verdict "RECRUITER_ONLY", bucket 1. Same hard cap: a \
17/18 with a required "3 years of leadership" is still a role the resume cannot screen into cold.
- core_function in ({capped_functions}) \
-> verdict "RECRUITER_ONLY", bucket 1. Same hard cap, applied in code: these functions have zero \
career precedent, and skill-line overlap does not clear a cold screen without it. Score the role \
honestly anyway — do not deflate the fit total to express this; the cap does that.
- Acceptable-tier BI/BA with a small title gap -> verdict "PASS", bucket 2.
- Clean low-code / Power Platform AI delivery (ai_artifact_depth == 3) -> verdict "PASS", bucket 3.
- A gate failed -> verdict "GATE_FAIL", bucket null, fit_score null.

=== CANDIDATE PROFILE ===
{profile}

=== EVALUATION GUIDE ===
{guide}

=== OUTPUT FORMAT ===
Respond with ONLY a JSON object, no markdown fences, no preamble:
{{
  "verdict": "PASS" or "GATE_FAIL" or "RECRUITER_ONLY",
  "gate_results": {{"years_floor": "PASS" or "FAIL", "domain_requirement": "PASS" or "FAIL", "role_substance": "PASS" or "FAIL", "tool_requirement": "PASS" or "FAIL", "work_auth": "PASS" or "FAIL", "employment_type": "PASS" or "FAIL"}} — MANDATORY: give an explicit verdict for EVERY one of the six gates by name, even on a GATE_FAIL (evaluate the remaining gates anyway) and even when a gate is trivially satisfied. A gate you did not consider must be reported, not omitted.,
  "failed_gate": null or one of ["years_floor","domain_requirement","role_substance","tool_requirement","work_auth","employment_type","other"] — use "other" ONLY for a gate failure the six named gates don't cover (a stated qualification the profile definitionally cannot meet, e.g. an experience ceiling or an unsatisfiable precondition), and say which one in gate_notes. On any GATE_FAIL this field must name something; null with verdict GATE_FAIL is not a valid answer.,
  "gate_notes": "one short sentence on the decisive gate finding",
  "fit_score": null or integer 0-18 (set whenever gates pass — i.e. for PASS and RECRUITER_ONLY),
  "score_breakdown": null or {{"ai_applied_vs_research": 0-3, "ai_artifact_depth": 0-3, "learning_value": 0-3, "technical_skill_match": 0-3, "title_trajectory": 0-3, "years_vs_stated": 0-3}},
  "formal_leadership_required": true or false (true ONLY when required — not preferred — formal people-leadership/management years are stated),
  "core_function": one of [{all_functions}] — the seat's core DAILY function per the CORE-FUNCTION CHECK above, judged from the responsibilities alone. MANDATORY on every gates-passed role; never null.,
  "bucket": null or 1 or 2 or 3,
  "one_line": "For gates-passed roles: Can perform: ... | Can screen: ... | Career capital: builds ...; visibly lacks ... . Career capital is explanatory only. For gate failures: one-line decisive reason.",
  "flags": ["anything needing human judgment, e.g. ambiguous seniority, possible research-coding, recruiter posting with unnamed client"]
}}"""


def _quoted(values):
    return ", ".join(f'"{v}"' for v in values)


def build_system_prompt():
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    # The two mechanical core_function lists are interpolated from states, never
    # hand-copied: a prompt that offers a value the code doesn't know fails SILENTLY
    # (the model emits it, normalize_result logs "unrecognized" and fails open, and the
    # cap quietly stops covering that class). Same lesson as GATE_NAMES_WITH_OTHER. The
    # per-value descriptions in the CORE-FUNCTION CHECK block stay prose — test_eval_
    # routing asserts every vocabulary member is actually described there.
    return SYSTEM_TEMPLATE.format(profile=profile, guide=guide,
                                  all_functions=_quoted(ALL_CORE_FUNCTIONS),
                                  capped_functions=_quoted(NO_PRECEDENT_FUNCTIONS))


def parse_eval_json(text):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model response")
    return json.loads(text[start : end + 1])


def normalize_result(result):
    """Apply the guide's hard routing rules deterministically, regardless of what
    the model returned. The artifact-depth cap is the load-bearing 50/0 fix, and the
    formal-leadership cap is its cold-screen sibling (a required "N yrs of
    leadership" is a wall the fit total talked past — the 17/18 manager-role
    case), so both are enforced in code, not left to the model: any role that passes
    the gates but scores ai_artifact_depth == 0 OR states a required formal-leadership
    tenure is RECRUITER_ONLY / bucket 1, even at 18/18. The function-precedent cap
    (2026-08-10) is the third: it reads the model's `core_function` extraction against
    states.NO_PRECEDENT_FUNCTIONS. It was guide-only for six weeks and fired on ~10% of
    the family it names, because "cap your own verdict" asks the model to overturn its
    own scoring; reporting a fact and capping in code is what the other two do.
    Mutates and returns `result`."""
    verdict = result.get("verdict", VERDICT_GATE_FAIL)
    if verdict not in VERDICTS:
        verdict = VERDICT_GATE_FAIL

    if verdict in (VERDICT_PASS, VERDICT_RECRUITER_ONLY):
        # Guard the CONTAINER type, not just the depth value: the model can emit
        # score_breakdown as a non-dict (list/string/number), and bd.get() would then
        # AttributeError. This runs OUTSIDE the eval retry try/except, so an unguarded
        # throw aborts the whole batch, not one row — fail closed to {} instead.
        bd = result.get("score_breakdown")
        bd = bd if isinstance(bd, dict) else {}
        depth = bd.get("ai_artifact_depth")
        # The 50/0 cap is load-bearing and must not depend on the model emitting a
        # literal 0: the output spec allows a null/partial score_breakdown, so any depth
        # that isn't a finite number (None, missing, string, NaN/Infinity — json.loads
        # parses bare NaN/Infinity tokens) must fail closed, not slip through to bucket 2.
        valid = (isinstance(depth, (int, float)) and not isinstance(depth, bool)
                 and math.isfinite(depth))
        # The leadership cap fails OPEN on a missing/negative field, opposite of the depth
        # cap's fail-closed: most roles require no leadership and pre-cap eval_json rows
        # lack the key entirely (backtest re-runs) — capping on absence would bucket-1 the
        # whole feed. But "affirmative" is judged on the normalized VALUE, not the JSON
        # type, so a model quoting the boolean ("true"), emitting 1, or answering "yes"
        # cannot dodge the cap; and a value that is neither a recognized affirmative nor a
        # recognized negative still fails open but is logged — a silent bypass here is the
        # cold-apply miss this cap exists to prevent.
        raw = result.get("formal_leadership_required")
        norm = str(raw).strip().lower()
        leadership = norm in ("true", "yes", "1")
        if not leadership and norm not in ("none", "false", "no", "0", ""):
            print(f"[eval] warning: unrecognized formal_leadership_required value "
                  f"{raw!r} — treating as no-requirement (fail-open)", file=sys.stderr)
        # Function-precedent cap. Fails OPEN like the leadership cap, for the same
        # reason: every eval_json written before this field existed lacks the key, and
        # backtest_v2 re-normalizes those stored rows — capping on absence would
        # bucket-1 the entire history and destroy the regression baseline. Unlike the
        # leadership cap this reads a CLOSED vocabulary, so an unrecognized string is
        # normalized to None rather than guessed at, then logged: a silent coercion to
        # some member would let one hallucinated value re-route a clean role, which is
        # the failure the gate_results normalization also refuses to risk.
        fn = result.get("core_function")
        fn = fn.strip().lower() if isinstance(fn, str) else None
        # An empty/whitespace value is ABSENCE, not a wrong answer — same reading the
        # leadership cap gives "" among its recognized negatives. Warning on it would
        # put a line on stderr for every row the model simply omitted the field on.
        fn = fn or None
        if fn is not None and fn not in ALL_CORE_FUNCTIONS:
            print(f"[eval] warning: unrecognized core_function value "
                  f"{result.get('core_function')!r} — no cap applied (fail-open)",
                  file=sys.stderr)
            fn = None
        result["core_function"] = fn
        no_precedent = fn in NO_PRECEDENT_FUNCTIONS
        if not valid or depth == 0 or leadership or no_precedent:
            verdict = VERDICT_RECRUITER_ONLY
            result["bucket"] = 1
        if not result.get("bucket"):
            # depth 3 -> clean low-code delivery (3); otherwise acceptable-tier (2)
            result["bucket"] = 3 if (valid and depth == 3) else 2
    else:  # GATE_FAIL
        result["bucket"] = None
        result["fit_score"] = None

    bucket = result.get("bucket")
    if bucket not in (1, 2, 3, None):
        result["bucket"] = None

    # Per-gate explicit results (schema addition 2026-08-07). Rationale: the failure
    # mode of long rule documents is SILENT omission — a model can skip a gate while
    # narrating compliance, and nothing in a bare verdict shows it (the 07-21
    # matcher-line decay was this mechanism; DriftBench measured models restating
    # rules they were violating). A structured per-gate field turns a skipped gate
    # into a visible None that backtest_v2 asserts on. Normalization here is
    # assistive and NEVER verdict-changing (unlike the depth/leadership caps): a
    # malformed or inconsistent gate_results is REPORTED, never acted on, because
    # auto-capping on a diagnostics field would let one hallucinated FAIL string
    # bucket-1 a clean role. Same no-throw discipline as the caps — this runs
    # outside the retry loop, so every access fails soft.
    #
    # The findings go in `eval_issues`, NOT in `flags`. `flags` answers "what about
    # this ROLE needs human judgment"; these answer "how much should you trust this
    # evaluation" — different subjects, and the first is a free-text channel the
    # model fills with ~2 prose warnings per posting (measured: 76% of rows carry
    # one, 53k distinct phrasings, only 0.8% matching a guide-defined token). Mixing
    # a machine-generated contract check into that stream would read as another
    # caveat about the job and would be unfindable among the prose.
    gr = result.get("gate_results")
    gr = gr if isinstance(gr, dict) else {}
    norm_gr = {}
    for gate in GATE_NAMES:
        v = gr.get(gate)
        v = v.strip().upper() if isinstance(v, str) else None
        norm_gr[gate] = v if v in ("PASS", "FAIL") else None
    result["gate_results"] = norm_gr

    issues = []

    def _issue(name):
        # Idempotent: normalize_result mutates in place, so a second call on the
        # same dict must not stack duplicates.
        if name not in issues:
            issues.append(name)

    # Derived from the arbitration evidence block _evaluate_one attaches, so this
    # stays true under re-normalization (_write_result normalizes again and rebuilds
    # `issues` from scratch — an issue appended anywhere else would be wiped). A split
    # (k draws with no majority verdict) is surfaced for review, never auto-rerouted,
    # same policy as the gate-contract findings below.
    arb = result.get("arbitration")
    if isinstance(arb, dict) and arb.get("split"):
        _issue("arbitration-split")

    if any(v is None for v in norm_gr.values()):
        _issue("gate-results-incomplete")
    explicit_fails = {g for g, v in norm_gr.items() if v == "FAIL"}
    failed_gate = result.get("failed_gate")
    if verdict == VERDICT_GATE_FAIL:
        # failed_gate "other" (the unmeetable-qualification rule) legitimately fails
        # OUTSIDE the six named gates — all six reading PASS is consistent there,
        # which is why the schema lists "other" explicitly: without it the guide
        # (log `other`) and the output spec (six names or null) contradict each
        # other, and an unnamed fail became indistinguishable from an unexplained
        # one. Note failed_gate is still the model's RAW value here; _write_result
        # coerces unknown strings to "other" only after this runs.
        if failed_gate in GATE_NAMES and norm_gr.get(failed_gate) == "PASS":
            _issue("gate-results-inconsistent")
        elif not failed_gate and not explicit_fails:
            # Rejected the gates while naming no cause anywhere: no failed_gate and
            # six PASSes. With "other" available this is unexplained, not the
            # unmeetable-qualification shape.
            _issue("gate-results-inconsistent")
    elif explicit_fails:
        _issue("gate-results-inconsistent")
    result["eval_issues"] = issues

    result["verdict"] = verdict
    return result


# _ensure_api_key (used by both the Adzuna fetch and the eval) moved to core.py (re-imported above).


# (input cache-miss, output) USD per token. DeepSeek V4 rates per the official
# card (api-docs.deepseek.com/quick_start/pricing); cache-hit input is ~$0.0028/1M
# for flash (auto-cached prefix), far below the 0.1x the tally assumes — so the
# DeepSeek cost line is a slight over-estimate, which is the safe direction.
MODEL_PRICES = {
    "claude-sonnet-4-6":          (3.0 / 1e6, 15.0 / 1e6),
    "claude-haiku-4-5":           (1.0 / 1e6, 5.0 / 1e6),
    "claude-haiku-4-5-20251001":  (1.0 / 1e6, 5.0 / 1e6),
    # Claude 5 list prices; second_judge derives its Batch-API rates from these
    # (x0.5, cache read x0.1, cache write x1.25) instead of keeping its own table.
    "claude-sonnet-5":            (3.0 / 1e6, 15.0 / 1e6),
    "claude-opus-5":              (5.0 / 1e6, 25.0 / 1e6),
    "claude-fable-5":             (10.0 / 1e6, 50.0 / 1e6),
    "deepseek-v4-flash":          (0.14 / 1e6, 0.28 / 1e6),
    "deepseek-v4-pro":            (0.435 / 1e6, 0.87 / 1e6),
}

# DeepSeek bills clock-dependent rates from 2026-08-17 (same pricing page): peak =
# 01:00-04:00 and 06:00-10:00 UTC (Beijing 9-12 / 14-18) at 2x the off-peak rate.
# The windows are fixed in UTC, so the predicate reads UTC directly — immune to the
# DST shifts that would silently move a local-clock schedule back into peak twice a
# year. Checked once at eval-stage start, not per request: a batch that starts
# off-peak and drags across a boundary pays peak for its tail, so keep scheduled
# slots clear of the window edges rather than teaching this to re-check mid-run.
DEEPSEEK_PEAK_HOURS_UTC = ((1, 4), (6, 10))  # [start, end) hour windows


def in_deepseek_peak(now=None):
    """True inside DeepSeek's 2x peak-rate windows. `now`: aware datetime, any tz —
    a NAIVE value is read as machine-local (astimezone's rule), so never hand this
    a naive UTC clock. Defined through deepseek_peak_end so the window table is read
    in exactly ONE place: "am I in a window" and "when does it end" cannot disagree."""
    return deepseek_peak_end(now) is not None


def deepseek_peak_end(now=None):
    """Aware-UTC end of the peak window containing `now`, or None while off-peak —
    the ONE reading of DEEPSEEK_PEAK_HOURS_UTC (in_deepseek_peak delegates here).
    Both windows close before midnight UTC, so the end is always on `now`'s date."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for lo, hi in DEEPSEEK_PEAK_HOURS_UTC:
        if lo <= now.hour < hi:
            return now.replace(hour=hi, minute=0, second=0, microsecond=0)
    return None

# Claude 5-era models reject any non-default `temperature` outright. This is an
# ALLOWLIST of the older ids that still accept it, not a denylist of the ones that
# don't: an unknown/newer model must default to omitting temperature (harmless) rather
# than sending it and 400-ing every request (fatal, and _retryable won't retry a 400).
# temperature=0 on these keeps them as deterministic as the serving stack allows, and
# keeps backtest/compare baselines comparable with pre-2026-08 measurements.
_TEMP_ACCEPTED = ("claude-sonnet-4-6", "claude-haiku-4-5", "claude-3")


def anthropic_extras(model):
    """Request kwargs that vary by Anthropic model generation (see _TEMP_ACCEPTED).
    Prefix match, so dated model ids (…-20251001) follow their family."""
    return {"temperature": 0} if model.startswith(_TEMP_ACCEPTED) else {}


class EmptyResponseError(RuntimeError):
    """The provider billed us but returned no text — a refusal, or thinking that
    exhausted max_tokens. Deterministic for the same input, so it is deliberately NOT
    retryable (_retryable), and it carries the usage of the call that produced it so
    the caller can still bill spend that really happened."""

    def __init__(self, message, in_tokens=0, out_tokens=0, cache_read=0, cache_write=0):
        super().__init__(message)
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.cache_read = cache_read
        self.cache_write = cache_write


def first_text(content):
    """First text block's text, '' when there is none — Claude 5 content may lead
    with thinking blocks, so content[0] is not reliably the answer. Every Anthropic
    reader (here, second_judge.collect, compare_models) goes through this."""
    return next((b.text for b in content if getattr(b, "type", "") == "text"), "")


def _call_anthropic(client, model, system_prompt, user_msg):
    """Return (text, fresh_in_tok, out_tok, cache_read_tok, cache_write_tok).

    Claude 5 family compatible (2026-08-12): `temperature` is conditional
    (anthropic_extras — rejected outright on Claude 5), thinking is on by default
    and counts against max_tokens (the old 1200 cap would truncate mid-JSON), and
    the content list may lead with thinking blocks (first_text). An empty answer
    (refusal, or thinking that exhausted max_tokens) raises EmptyResponseError —
    carrying its usage, because that call was billed — instead of letting "" reach
    the JSON parser as a misleading parse error and then be retried three times.
    """
    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        system=[{"type": "text", "text": system_prompt,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
        **anthropic_extras(model),
    )
    u = resp.usage
    text = first_text(resp.content)
    if not text.strip():
        raise EmptyResponseError(
            f"anthropic returned no text (stop_reason={resp.stop_reason})",
            in_tokens=u.input_tokens, out_tokens=u.output_tokens,
            cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0)
    return (text, u.input_tokens, u.output_tokens,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0)


# The one production knob for DeepSeek reasoning depth. Module-level so probe
# scripts can mirror the EXACT production request shape by reference instead of
# hardcoding a copy that silently goes stale when this changes (noise_probe's
# "prod" condition measured the wrong tier for ten minutes the day this landed).
DEEPSEEK_EFFORT = "low"


def deepseek_request_body(model, system_prompt, user_msg, **overrides):
    """The ONE definition of the production DeepSeek request body.

    Every validation probe compares something against "what production does", so each
    one needs this shape — and each hand-copied version silently became a different
    experiment the moment production moved. That happened three times on 2026-08-07
    alone (a probe measured the wrong reasoning tier for ten minutes; a model-comparison
    column would have benchmarked the incumbent at a tier it doesn't run; an effort
    probe's "high" column, written as an empty override back when production sent no
    effort at all, would have quietly measured low and reported the two tiers
    identical). Call this instead of rebuilding the dict.

    `overrides` replaces top-level keys; an override of None DELETES its key, which is
    how a caller expresses "send no reasoning_effort at all" (e.g. alongside a
    thinking-disabled body)."""
    body = {
        "model": model,
        "max_tokens": 16000,
        "temperature": 0,
        "reasoning_effort": DEEPSEEK_EFFORT,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_msg}],
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def _call_deepseek(api_key, model, system_prompt, user_msg):
    """Return (text, fresh_in_tok, out_tok, cache_read_tok, cache_write_tok).
    V4 is a reasoning model — it thinks before the JSON answer, so max_tokens must
    be generous or the answer truncates to empty (billing the whole failed attempt).
    The 0731 flash build thinks ~3.4k tokens on average with a long tail; the old
    8000 cap produced 30-100 empty-answer retries/day, so 16000.
    reasoning_effort "low" (2026-08-07): the provider default is "high", which this
    rubric task doesn't need — two effort_probe.py runs (48 postings, 432 calls)
    showed low's verdicts agree with high's at the noise floor while spending ~29%
    fewer output tokens, scoring ~0.7 closer to the pre-0731 fit scale, and (weak
    signal) producing zero empty answers vs high's occasional ones. "none" was
    REJECTED despite being 25x cheaper and fully deterministic: it is a different,
    systematically harsher judge (50-60% agreement, near-everything GATE_FAILs).
    response_format forces valid JSON (DeepSeek otherwise wraps it in prose)."""
    import httpx

    r = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=deepseek_request_body(model, system_prompt, user_msg),
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage", {})
    cache_read = u.get("prompt_cache_hit_tokens", 0)  # DeepSeek auto-caches the prefix
    fresh_in = u.get("prompt_tokens", 0) - cache_read
    return (d["choices"][0]["message"]["content"], fresh_in,
            u.get("completion_tokens", 0), cache_read, 0)


class EvalAuthError(Exception):
    """The provider rejected our credentials (401/403). The same failure would hit every row,
    so the whole batch aborts instead of burning 3 retries x N rows on a dead key; unevaluated
    rows stay 'new' for the next run."""


def _http_status(e):
    """Best-effort HTTP status from a provider exception: anthropic's APIStatusError carries
    .status_code, httpx.HTTPStatusError carries .response.status_code. None = not an HTTP-status
    failure (network drop, timeout, JSON parse)."""
    status = getattr(e, "status_code", None)
    if status is None:
        status = getattr(getattr(e, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _retryable(e):
    """Retry only what can plausibly heal on its own: rate limits (429), timeouts (408),
    server errors (5xx), and non-HTTP failures (network drops; a malformed model response —
    it can re-emit). Any other 4xx is OUR request being wrong (bad model id, oversized
    payload) — retrying triples the latency for the same failure."""
    # The one non-HTTP failure that will NOT heal: a billed no-text response. The same
    # prompt refuses (or overruns thinking) again, so retrying just pays for it 3x.
    if isinstance(e, EmptyResponseError):
        return False
    status = _http_status(e)
    if status is None:
        return True
    return status in (408, 429) or status >= 500


def requeue_error_rows(conn):
    """Return status='error' rows to 'new' so this run retries them. Without this an
    'error' row (provider outage, rate-limit storm) is stranded forever — no stage reads
    'error'. Runs as its OWN stage in `run`, after the fetchers and BEFORE the deterministic
    filters — deliberately not inside evaluate_new_jobs: a requeued row must re-face the
    salary filter, the CURRENT hard rules, skip_decided_reposts, AND skip_evaluated_reposts,
    so a rule added, a chain decision made, or a chain verdict recorded while the row sat in
    'error' still catches it before the paid eval.
    A permanently failing row re-errors each run — visible in the report's errors section,
    costing only its own retries. Returns the requeued count."""
    n = conn.execute("UPDATE jobs SET status=? WHERE status=?",
                     (STATUS_NEW, STATUS_ERROR)).rowcount
    conn.commit()
    if n:
        print(f"[eval] requeued {n} previously-errored posting(s) for retry")
    return n


def build_user_msg(row):
    """The ONE builder of the per-posting eval message. The validation scripts
    (backtest_v2, compare_models, stratified_rerun, arbitrate_k3) re-evaluate stored
    rows and their whole premise is "the SAME prompt the pipeline sends" — they must
    call this, never rebuild the f-string by hand (the hand copies had already
    drifted: hyphen vs en dash in POSTED SALARY)."""
    return (
        f"TITLE: {row['title']}\nCOMPANY: {row['company']}\nLOCATION: {row['location']}\n"
        f"SOURCE SEARCH: {row['search_name']} (tier: {row['tier']})\n"
        f"POSTED SALARY: {row['salary_min']}–{row['salary_max']}\n\n"
        f"JOB DESCRIPTION:\n{row['description']}"
    )


# Boundary-band arbitration (2026-08-08). The temp-0 judge is not deterministic
# (MoE serving noise): flip_consequence.py over the 08-07 noise probe measured 25%
# of evaluated postings changing VERDICT on a rerun, but only 8% changing ACTION —
# cold-apply triage reads PASS at fit>=13, the recruiter_route queue reads
# RECRUITER_ONLY at fit>=15 (its default bar), everything else is un-acted-on. A
# first draw landing scored inside this band triggers ARBITRATION_EXTRA_DRAWS more
# and a majority vote: fires on ~20% of draws, catches ~87% of action flips, costs
# ~$0.03 per 100 postings on the DEFAULT DeepSeek provider (input is cache-hit, so
# it is ~all output tokens) — on an Anthropic model the same 20% fire rate costs
# ~25x that, so re-price this before switching providers, don't assume it stays
# rounding-error. GATE_FAIL first draws are deliberately NOT arbitrated —
# covering that leak (the remaining ~13% of action flips) measured a 73% fire rate
# for +6pp catch. Rerun tests/validation/flip_consequence.py before moving the band.
ARBITRATION_BAND = (11, 17)
ARBITRATION_EXTRA_DRAWS = 2


def needs_arbitration(result):
    """Trigger predicate over a NORMALIZED result: scored verdict, finite fit
    inside the band. Same bool/NaN discipline as the depth cap — a malformed fit
    must not arbitrate (it routed fail-closed already)."""
    if result.get("verdict") not in (VERDICT_PASS, VERDICT_RECRUITER_ONLY):
        return False
    f = result.get("fit_score")
    return (isinstance(f, (int, float)) and not isinstance(f, bool)
            and math.isfinite(f) and ARBITRATION_BAND[0] <= f <= ARBITRATION_BAND[1])


def arbitrate(draws):
    """Majority vote over >=1 NORMALIZED draws; draws[0] is the production draw.
    A strict-majority verdict wins and the winning draw closest below the winners'
    median fit is kept whole (score_breakdown stays consistent with fit_score —
    never a synthetic average). No strict majority (a 3-way split, or 1-1 when an
    extra draw failed) keeps draws[0]'s verdict and marks the result
    `arbitration.split` — normalize_result surfaces that as an eval_issue for
    review, it never re-routes. Returns the chosen draw with the evidence block
    attached; pure, no I/O."""
    counts = Counter(d["verdict"] for d in draws)
    top, top_n = counts.most_common(1)[0]
    split = top_n <= len(draws) // 2
    verdict = draws[0]["verdict"] if split else top
    winners = [d for d in draws if d["verdict"] == verdict]

    def _fit_key(d):
        # Only draws[0]'s fit was type-checked (by needs_arbitration); an extra
        # draw's is whatever the model emitted. Sort unusable fits last on a
        # SEPARATE key component so the comparison never mixes types — a bare
        # (is_none, fit) key raises TypeError on str-vs-int and would discard all
        # three already-paid draws by crashing the worker.
        f = d.get("fit_score")
        ok = (isinstance(f, (int, float)) and not isinstance(f, bool)
              and math.isfinite(f))
        return (0, f) if ok else (1, 0)

    winners.sort(key=_fit_key)
    chosen = winners[(len(winners) - 1) // 2]
    chosen["arbitration"] = {
        "k": len(draws), "split": split,
        "overrode_first": draws[0]["verdict"] != verdict,
        "draws": [{"verdict": d["verdict"], "fit_score": d.get("fit_score"),
                   "bucket": d.get("bucket")} for d in draws],
    }
    return chosen


def _provider_call(provider, client, api_key, model, system_prompt, user_msg):
    if provider == "anthropic":
        return _call_anthropic(client, model, system_prompt, user_msg)
    return _call_deepseek(api_key, model, system_prompt, user_msg)


def _evaluate_one(row, provider, model, system_prompt, client, api_key):
    """Pure worker (no DB access): build the message, call the provider with the same
    3-attempt backoff, parse, then boundary-band arbitrate (see ARBITRATION_BAND).
    Returns (job_url, result_or_None, in, out, cache_read, cache_write) with token
    counts summed across attempts and arbitration draws — identical to the serial
    tally, but off the main thread so calls overlap. All DB writes stay in
    evaluate_new_jobs on the main thread (a sqlite3 conn isn't safe across threads)."""
    user_msg = build_user_msg(row)
    tin = tout = cr = cw = 0
    result = None
    for attempt in range(3):
        try:
            text, a_in, a_out, a_cr, a_cw = _provider_call(
                provider, client, api_key, model, system_prompt, user_msg)
            tin += a_in
            cr += a_cr
            cw += a_cw
            tout += a_out
            result = parse_eval_json(text)
            break
        except Exception as e:
            # A failed call can still have been billed (EmptyResponseError carries its
            # usage). Tally it here or the run's cost line under-reports real spend.
            tin += getattr(e, "in_tokens", 0)
            tout += getattr(e, "out_tokens", 0)
            cr += getattr(e, "cache_read", 0)
            cw += getattr(e, "cache_write", 0)
            status = _http_status(e)
            if status in (401, 403):
                # Wrong credentials fail every row identically — abort the whole batch.
                raise EvalAuthError(f"{provider} rejected the API key ({status}): {e}") from e
            if not _retryable(e):
                print(f"[eval] non-retryable error ({e}); marking error", file=sys.stderr)
                break
            wait = 5 * (attempt + 1)
            print(f"[eval] attempt {attempt+1} failed ({e}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    if result is None:
        return row["job_url"], None, tin, tout, cr, cw

    # The trigger reads the NORMALIZED routing (post-caps verdict); _write_result's
    # later normalize_result call is idempotent on an already-normalized dict.
    normalize_result(result)
    if needs_arbitration(result):
        draws = [result]
        for _ in range(ARBITRATION_EXTRA_DRAWS):
            # One attempt per extra draw, no backoff: the first draw just proved the
            # provider healthy, and arbitrate() degrades honestly to fewer draws
            # (2 that disagree = split, surfaced for review).
            try:
                text, a_in, a_out, a_cr, a_cw = _provider_call(
                    provider, client, api_key, model, system_prompt, user_msg)
                tin += a_in
                cr += a_cr
                cw += a_cw
                tout += a_out
                draws.append(normalize_result(parse_eval_json(text)))
            except Exception as e:
                if _http_status(e) in (401, 403):
                    # Follows the established auth contract (abort the batch; the
                    # row stays 'new' and re-evaluates untouched next run) at the
                    # cost of discarding this row's already-paid first draw — a
                    # bounded loss of at most `concurrency` draws, taken so a dead
                    # key can't be silently absorbed by degraded arbitration.
                    raise EvalAuthError(
                        f"{provider} rejected the API key mid-arbitration: {e}") from e
                print(f"[eval] arbitration draw failed ({e}); "
                      f"voting with {len(draws)}", file=sys.stderr)
        result = arbitrate(draws)
    return row["job_url"], result, tin, tout, cr, cw


def _write_result(conn, job_url, result):
    """Persist one landed eval onto its row and commit: None (a failed call) marks the row
    'error' for the next run's requeue; a result dict is re-normalized and written whole."""
    if result is None:
        conn.execute("UPDATE jobs SET status=? WHERE job_url=?", (STATUS_ERROR, job_url))
    else:
        normalize_result(result)
        verdict = result["verdict"]
        failed_gate = result.get("failed_gate")
        if failed_gate and failed_gate not in GATE_NAMES:
            failed_gate = GATE_OTHER
        # eval_issues is denormalized onto the row so the review queue can find a
        # self-contradicting verdict with a column test instead of parsing every
        # stored blob; this UPDATE is its only writer.
        issues = ",".join(result.get("eval_issues") or []) or None
        conn.execute(
            """UPDATE jobs SET status=?, verdict=?, failed_gate=?,
               fit_score=?, bucket=?, eval_json=?, eval_issues=? WHERE job_url=?""",
            (
                STATUS_EVALUATED,
                verdict,
                failed_gate,
                result.get("fit_score"),
                result.get("bucket"),
                json.dumps(result, ensure_ascii=False),
                issues,
                job_url,
            ),
        )
    conn.commit()


def evaluate_new_jobs(cfg, conn):
    provider = cfg["settings"].get("provider", "anthropic")
    model = cfg["settings"]["model"]
    try:
        concurrency = max(1, int(cfg["settings"].get("eval_concurrency", 6)))
    except (TypeError, ValueError):
        print(f"[eval] invalid eval_concurrency "
              f"{cfg['settings'].get('eval_concurrency')!r}; using 6", file=sys.stderr)
        concurrency = 6

    client = api_key = None
    if provider == "anthropic":
        import anthropic
        if not _ensure_api_key("ANTHROPIC_API_KEY"):
            print("[eval] ANTHROPIC_API_KEY not set — skipping evaluation", file=sys.stderr)
            return
        client = anthropic.Anthropic()
    elif provider == "deepseek":
        api_key = _ensure_api_key("DEEPSEEK_API_KEY")
        if not api_key:
            print("[eval] DEEPSEEK_API_KEY not set — skipping evaluation", file=sys.stderr)
            return
    else:
        print(f"[eval] unknown provider '{provider}' — skipping evaluation", file=sys.stderr)
        return

    # (The provider/model consistency check lives in core.validate_config — it runs at
    # config load, before any fetch/eval money is spent.)
    system_prompt = build_system_prompt()
    price_in, price_out = MODEL_PRICES.get(model, (0.0, 0.0))

    rows = conn.execute("SELECT * FROM jobs WHERE status=?", (STATUS_NEW,)).fetchall()
    print(f"[eval] {len(rows)} postings to evaluate via {provider}:{model} "
          f"(concurrency={concurrency})")

    usage_in = usage_cache_write = usage_cache_read = usage_out = 0
    arb_n = arb_override = arb_split = 0

    # Empty-description rows never hit the API — mark and skip on the main thread.
    todo = []
    for r in rows:
        if not (r["description"] or "").strip():
            conn.execute("UPDATE jobs SET status=? WHERE job_url=?",
                         (STATUS_NEEDS_MANUAL, r["job_url"]))
            conn.commit()
        else:
            todo.append(r)

    # Each call is blocking network I/O (the GIL is released while waiting on the
    # provider), so a bounded pool overlaps them. Workers are pure — every DB write
    # happens here on the main thread as each future lands (as_completed), so the sqlite
    # conn is never touched off-thread and each commit is durable: a kill mid-run leaves
    # finished rows 'evaluated' and the <=concurrency in-flight rows 'new' for next run.
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {
            ex.submit(_evaluate_one, r, provider, model, system_prompt, client, api_key): r["job_url"]
            for r in todo
        }
        try:
            for fut in as_completed(futures):
                job_url = futures[fut]
                try:
                    _, result, tin, tout, cr, cw = fut.result()
                except EvalAuthError as e:
                    # A dead key fails every remaining row the same way — stop paying for
                    # retries. Nothing is written for the aborted rows: they stay 'new' and
                    # a run with a fixed key picks them up untouched.
                    print(f"[eval] {e} — aborting this batch; unevaluated rows stay 'new'",
                          file=sys.stderr)
                    ex.shutdown(cancel_futures=True)
                    break
                except Exception as e:
                    # Workers catch their own call errors, but guard anyway so one unexpected
                    # crash maps to 'error' instead of aborting the whole batch.
                    print(f"[eval] worker crashed for {job_url} ({e}); marking error", file=sys.stderr)
                    result, tin, tout, cr, cw = None, 0, 0, 0, 0
                usage_in += tin
                usage_cache_read += cr
                usage_cache_write += cw
                usage_out += tout
                arb = result.get("arbitration") if result else None
                if isinstance(arb, dict):
                    arb_n += 1
                    arb_override += bool(arb.get("overrode_first"))
                    arb_split += bool(arb.get("split"))
                for attempt in (1, 2):
                    try:
                        _write_result(conn, job_url, result)
                        break
                    except Exception as e:
                        # A write failure (sqlite 'database is locked' from a concurrent
                        # run, a transient disk error) must not abort the batch. Roll back
                        # so the failed row's staged UPDATE isn't later flushed by the next
                        # row's commit. The result in hand is already PAID for, so retry
                        # the write once (milliseconds vs re-billing the eval) before
                        # leaving the row 'new' for a clean re-eval next run (matching the
                        # log below).
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        if attempt == 1:
                            time.sleep(1)
                            continue
                        print(f"[eval] DB write failed for {job_url} ({e}); left 'new'",
                              file=sys.stderr)
        except KeyboardInterrupt:
            # Default pool exit calls shutdown(wait=True), which would drain the ENTIRE
            # remaining queue (hours of paid calls) before Ctrl-C takes effect. Cancel the
            # not-yet-started futures so an interrupt stops after the in-flight calls;
            # committed rows stay 'evaluated', the rest stay 'new' for the next run.
            print("[eval] interrupted — cancelling pending evaluations", file=sys.stderr)
            ex.shutdown(cancel_futures=True)
            raise

    cost = (
        (usage_in + usage_cache_read * 0.1 + usage_cache_write * 1.25) * price_in
        + usage_out * price_out
    )
    arb_note = (f" | arbitrated {arb_n} ({arb_override} overridden, {arb_split} split)"
                if arb_n else "")
    print(
        f"[eval] done | tokens: {usage_in} in, {usage_cache_read} cache-read, "
        f"{usage_cache_write} cache-write, {usage_out} out | est. cost ${cost:.2f}"
        f"{arb_note}"
    )
