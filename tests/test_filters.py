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
