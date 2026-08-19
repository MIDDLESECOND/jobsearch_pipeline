"""Hard-filter pattern matching — the deterministic pre-eval reject layer."""

import filters


def test_substring_is_case_insensitive():
    assert filters._pattern_matches("clearance", "Active Security CLEARANCE required")
    assert not filters._pattern_matches("clearance", "no gatekeeping here")


def test_regex_prefix():
    assert filters._pattern_matches(r"re:\b10\+ years\b", "needs 10+ years experience")
    assert not filters._pattern_matches(r"re:\b10\+ years\b", "needs 3 years experience")


def test_invalid_regex_does_not_raise():
    # A malformed pattern fails closed (no match), never blows up the filter pass.
    assert filters._pattern_matches("re:[unterminated", "anything") is False


def test_validate_pattern():
    # The shared validator that catches, BEFORE a pattern is stored, what _pattern_matches
    # would otherwise fail silently on — used by both `reject --pattern` and settings.ats.
    assert filters.validate_pattern("clearance") is None
    assert filters.validate_pattern(r"re:\b10\+ years\b") is None
    # Non-strings, blanks, non-compiling regex, and the empty-body regex (compiles but
    # matches everything) all return a reason string.
    assert filters.validate_pattern("") is not None
    assert filters.validate_pattern("   ") is not None
    assert filters.validate_pattern(2024) is not None
    assert filters.validate_pattern("re:[unterminated") is not None
    assert filters.validate_pattern("re:") is not None
    assert filters.validate_pattern("re: ") is not None


def test_rule_hit_returns_first_matching_pattern():
    rule = {"any": ["citizenship", "clearance"]}
    assert filters._rule_hit(rule, "US citizenship required") == "citizenship"
    assert filters._rule_hit(rule, "TS/SCI clearance") == "clearance"
    assert filters._rule_hit(rule, "open to all") is None


def test_rule_hit_empty_rule():
    assert filters._rule_hit({}, "anything") is None


def test_rule_hit_company_scope_does_not_leak():
    # company_any matches ONLY the company name; `any` matches ONLY title+description.
    # Neither scope sees the other's text — a JD that merely mentions the aggregator's
    # brand must not trip the company rule, and vice versa.
    #
    # The patterns here are deliberately UNANCHORED. With `re:^shellco$` the first
    # assertion passes whether or not the scoping works, because the anchors alone can
    # never match a surrounding sentence — the test would be green with company_any wired
    # to the title+description blob.
    rule = {"company_any": ["shellco"]}
    assert filters._rule_hit(rule, "JD text mentioning shellco tooling", "Acme Corp") is None
    assert filters._rule_hit(rule, "clean description", "ShellCo") == "shellco"
    text_rule = {"any": ["shellco"]}
    assert filters._rule_hit(text_rule, "clean description", "ShellCo") is None
    assert filters._rule_hit(text_rule, "we integrate shellco", "Acme") == "shellco"


def test_salary_floor_is_inclusive_a_posting_at_the_floor_survives(conn):
    """The rule is 'at/above the floor or not mentioned': a salary EQUAL to the floor
    is not below it (`<`, not `<=` — a mutation round survived because nothing tested
    apply_salary_filter directly; only two intake tests reached it in passing)."""
    from conftest import make_job
    make_job(conn, job_url="at-floor", status="new", verdict=None, salary_max=80000)
    make_job(conn, job_url="below-floor", status="new", verdict=None, salary_max=79999)
    cfg = {"searches": [{"name": "s1", "min_salary": 80000}]}
    filters.apply_salary_filter(cfg, conn)
    statuses = {u: s for u, s in conn.execute("SELECT job_url, status FROM jobs")}
    assert statuses["at-floor"] == "new"
    assert statuses["below-floor"] == "salary_filtered"


def test_apply_hard_filters_company_rule_skips_shell_keeps_employer(conn, monkeypatch):
    """A company_any rule fails the aggregator shell pre-eval (the eval-slot saving it
    exists for) while the real employer's same-title posting stays 'new' for the eval."""
    from conftest import make_job
    make_job(conn, job_url="agg", status="new", verdict=None, company="ShellCo",
             location="", title="AI Engineer", description="agency shell relist")
    # The employer's own description NAMES the shell — so if company_any ever leaked into
    # the title+description scope, this row would be filtered too and the test would fail.
    make_job(conn, job_url="real", status="new", verdict=None,
             company="Real Employer Inc.", location="Houston, TX", title="AI Engineer",
             description="the employer's own posting, also listed via ShellCo")
    monkeypatch.setattr(filters, "load_filters",
                        lambda: [{"name": "aggregator_shell", "gate": "other",
                                  "company_any": ["re:^shellco$"]}])
    filters.apply_hard_filters({"settings": {}}, conn)
    agg = conn.execute("SELECT status, verdict, filter_source, filter_gate FROM jobs "
                       "WHERE job_url='agg'").fetchone()
    assert agg["status"] == "rule_filtered"
    assert agg["verdict"] == "GATE_FAIL"
    assert agg["filter_source"] == "rule:aggregator_shell"
    assert agg["filter_gate"] == "other"
    real = conn.execute("SELECT status FROM jobs WHERE job_url='real'").fetchone()
    assert real["status"] == "new"


def test_apply_hard_filters_never_clobbers_existing_attribution(conn, monkeypatch):
    """A row rejected while it sat in 'error' returns through requeue as 'new' still carrying
    filter_source='manual' + the user's gate. The rule pass must leave it alone: re-stamping
    it 'rule:<name>' would silently replace the manual attribution, and `reject --undo`
    (which clears only 'manual' rows) would then report success while clearing nothing."""
    from conftest import make_job
    make_job(conn, job_url="u", status="new", verdict=None,
             description="requires TS/SCI clearance",
             filter_source="manual", filter_gate="employment_type",
             filter_date="2026-07-01")
    monkeypatch.setattr(filters, "load_filters",
                        lambda: [{"name": "clearance", "gate": "work_auth", "any": ["TS/SCI"]}])
    filters.apply_hard_filters({"settings": {}}, conn)
    row = conn.execute(
        "SELECT filter_source, filter_gate, status FROM jobs WHERE job_url='u'").fetchone()
    assert row["filter_source"] == "manual"          # attribution preserved
    assert row["filter_gate"] == "employment_type"   # user's gate preserved
    assert row["status"] == "new"  # decided skip pass (key: own stamp) parks it pre-eval


def test_load_filters_warns_on_broken_pattern_but_keeps_rule(tmp_path, monkeypatch, capsys):
    # A hand-edited filters.yaml with a broken `re:` must WARN (not drop the rule and not
    # crash), so the user learns the rule silently matches nothing — the "or loaded" half of
    # validate_pattern's contract.
    f = tmp_path / "filters.yaml"
    f.write_text(
        "hard_filters:\n  - name: seniority\n    gate: years_floor\n"
        "    any:\n      - 're:(senior|staff'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(filters, "FILTERS_PATH", f)
    rules = filters.load_filters()
    assert rules and rules[0]["name"] == "seniority"  # kept, not dropped
    err = capsys.readouterr().err
    assert "is unusable" in err and "seniority" in err


def test_load_filters_warns_on_broken_company_pattern(tmp_path, monkeypatch, capsys):
    # company_any patterns get the same load-time validation as `any` patterns.
    f = tmp_path / "filters.yaml"
    f.write_text(
        "hard_filters:\n  - name: aggregator_shell\n    gate: other\n"
        "    company_any:\n      - 're:(shellco'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(filters, "FILTERS_PATH", f)
    rules = filters.load_filters()
    assert rules and rules[0]["name"] == "aggregator_shell"  # kept, not dropped
    err = capsys.readouterr().err
    assert "is unusable" in err and "aggregator_shell" in err


def test_reject_pattern_never_extends_a_company_only_rule(tmp_path, monkeypatch, capsys,
                                                          conn):
    """`--gate` defaults to 'other', and a company_any rule is naturally gate 'other' (none
    of the six named gates describes "this company is an aggregator shell"). Matching purely
    on gate therefore dropped every un-gated `reject --pattern` into the COMPANY rule's
    `any` list, and attributed the posting to it."""
    import pipeline
    from conftest import make_job

    f = tmp_path / "filters.yaml"
    f.write_text(
        "hard_filters:\n  - name: aggregator_shell\n    gate: other\n"
        "    company_any:\n      - 're:^shellco$'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(filters, "FILTERS_PATH", f)
    monkeypatch.setattr(pipeline, "FILTERS_PATH", f)
    make_job(conn, job_url="u", status="new", verdict=None,
             description="requires an active TS/SCI clearance")
    posting = conn.execute("SELECT * FROM jobs WHERE job_url='u'").fetchone()

    pipeline._add_filter_rule(conn, "other", "ts/sci", None, posting)

    rules = filters.load_filters()
    shell = next(r for r in rules if r["name"] == "aggregator_shell")
    assert shell.get("any") in (None, [])           # untouched
    assert shell["company_any"] == ["re:^shellco$"]
    # The description pattern landed in its own rule instead.
    other = [r for r in rules if r is not shell]
    assert len(other) == 1 and other[0]["any"] == ["ts/sci"]
