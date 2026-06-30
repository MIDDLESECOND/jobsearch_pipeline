#!/usr/bin/env python3
"""Deterministic, zero-cost pre-eval filters: the salary floor and the user-maintained
hard-requirement rules (filters.yaml). Both run BEFORE the paid LLM eval and set a non-'new'
status so evaluate_new_jobs short-circuits the obvious rejects. Imports only core; the `reject`
command's rule-writing helper (_add_filter_rule) lives with the CLI in pipeline.py but reuses
load_filters/save_filters/_pattern_matches from here.
"""

import re
from datetime import date

import yaml

from core import BASE_DIR

FILTERS_PATH = BASE_DIR / "filters.yaml"


# -------------------------------------------------------------- salary filter

def apply_salary_filter(cfg, conn):
    """Analyst-tier rule: drop only when annual salary is KNOWN and below the floor.
    Unstated salary is kept — this is the '>80k or not mentioned' rule."""
    filtered = 0
    for search in cfg["searches"]:
        floor = search.get("min_salary")
        if not floor:
            continue
        rows = conn.execute(
            "SELECT job_url, salary_min, salary_max FROM jobs WHERE search_name=? AND status='new'",
            (search["name"],),
        ).fetchall()
        for r in rows:
            known = r["salary_max"] or r["salary_min"]
            if known is not None and known < floor:
                conn.execute(
                    "UPDATE jobs SET status='salary_filtered' WHERE job_url=?", (r["job_url"],)
                )
                filtered += 1
    conn.commit()
    if filtered:
        print(f"[salary] {filtered} postings below floor, filtered")


# ----------------------------------------------------- hard-requirement filters
#
# DeepSeek Flash (the cheap default evaluator) under-filters by design — some postings
# that miss a hard requirement (clearance, citizenship, 10+ years) slip through as PASS.
# These deterministic, user-maintained rules catch them BEFORE the paid eval: zero cost,
# instant, fully predictable. The companion `reject` command writes rules here as you
# spot misses. Mirrors apply_salary_filter — a deterministic pre-eval hard rule.

def load_filters():
    """Read filters.yaml → list of rule dicts. Returns [] if the file is absent or empty."""
    if not FILTERS_PATH.exists():
        return []
    with open(FILTERS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("hard_filters") or []


def save_filters(rules):
    """Write the ruleset back. filters.yaml is tool-owned, so normalizing on write is fine."""
    with open(FILTERS_PATH, "w", encoding="utf-8") as f:
        f.write("# Hard-requirement filters — postings matching a pattern are auto-failed\n")
        f.write("# before evaluation. A pattern is a case-insensitive substring unless it is\n")
        f.write("# prefixed `re:`, which makes it a regex. Managed by `pipeline.py reject`;\n")
        f.write("# safe to hand-edit. See README.\n")
        yaml.safe_dump({"hard_filters": rules}, f, sort_keys=False, allow_unicode=True)


def _pattern_matches(pattern, text):
    """True if `pattern` matches `text`. `re:`-prefixed patterns are case-insensitive
    regexes; everything else is a case-insensitive substring."""
    if pattern.startswith("re:"):
        try:
            return re.search(pattern[3:], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def _rule_hit(rule, text):
    """Return the first pattern in `rule` that matches `text`, or None."""
    for pat in rule.get("any") or []:
        if _pattern_matches(pat, text):
            return pat
    return None


def apply_hard_filters(cfg, conn):
    """Auto-fail new postings that match a user-maintained hard-requirement rule, before
    the paid eval. Matched rows get status='rule_filtered' (skipped by evaluate_new_jobs)."""
    rules = load_filters()
    if not rules:
        return
    rows = conn.execute(
        "SELECT job_url, title, description FROM jobs WHERE status='new'"
    ).fetchall()
    today = date.today().isoformat()
    filtered = 0
    for r in rows:
        text = f"{r['title'] or ''}\n{r['description'] or ''}"
        # Rules are tried in file order; the FIRST match wins and records its gate. If a
        # posting could match several rules, reorder filters.yaml to control attribution.
        for rule in rules:
            if _rule_hit(rule, text):
                conn.execute(
                    "UPDATE jobs SET status='rule_filtered', verdict='GATE_FAIL', "
                    "failed_gate=?, filter_source=?, filter_gate=?, filter_date=? WHERE job_url=?",
                    (rule.get("gate", "other"), "rule:" + rule.get("name", "?"),
                     rule.get("gate", "other"), today, r["job_url"]),
                )
                filtered += 1
                break
    conn.commit()
    if filtered:
        print(f"[filter] {filtered} postings auto-failed by hard rules (eval skipped, cost saved)")
