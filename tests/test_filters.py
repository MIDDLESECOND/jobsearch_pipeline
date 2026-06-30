"""Hard-filter pattern matching — the deterministic pre-eval reject layer."""

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


def test_rule_hit_returns_first_matching_pattern():
    rule = {"any": ["citizenship", "clearance"]}
    assert pipeline._rule_hit(rule, "US citizenship required") == "citizenship"
    assert pipeline._rule_hit(rule, "TS/SCI clearance") == "clearance"
    assert pipeline._rule_hit(rule, "open to all") is None


def test_rule_hit_empty_rule():
    assert pipeline._rule_hit({}, "anything") is None
