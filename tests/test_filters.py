"""Hard-filter pattern matching — the deterministic pre-eval reject layer."""

import filters
import pipeline


def test_substring_is_case_insensitive():
    assert pipeline._pattern_matches("clearance", "Active Security CLEARANCE required")
    assert not pipeline._pattern_matches("clearance", "no gatekeeping here")


def test_regex_prefix():
    assert pipeline._pattern_matches(r"re:\b10\+ years\b", "needs 10+ years experience")
    assert not pipeline._pattern_matches(r"re:\b10\+ years\b", "needs 3 years experience")


def test_invalid_regex_does_not_raise():
    # A malformed pattern fails closed (no match), never blows up the filter pass.
    assert pipeline._pattern_matches("re:[unterminated", "anything") is False


def test_validate_pattern():
    # The shared validator that catches, BEFORE a pattern is stored, what _pattern_matches
    # would otherwise fail silently on — used by both `reject --pattern` and settings.ats.
    assert pipeline.validate_pattern("clearance") is None
    assert pipeline.validate_pattern(r"re:\b10\+ years\b") is None
    # Non-strings, blanks, non-compiling regex, and the empty-body regex (compiles but
    # matches everything) all return a reason string.
    assert pipeline.validate_pattern("") is not None
    assert pipeline.validate_pattern("   ") is not None
    assert pipeline.validate_pattern(2024) is not None
    assert pipeline.validate_pattern("re:[unterminated") is not None
    assert pipeline.validate_pattern("re:") is not None
    assert pipeline.validate_pattern("re: ") is not None


def test_rule_hit_returns_first_matching_pattern():
    rule = {"any": ["citizenship", "clearance"]}
    assert pipeline._rule_hit(rule, "US citizenship required") == "citizenship"
    assert pipeline._rule_hit(rule, "TS/SCI clearance") == "clearance"
    assert pipeline._rule_hit(rule, "open to all") is None


def test_rule_hit_empty_rule():
    assert pipeline._rule_hit({}, "anything") is None


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
