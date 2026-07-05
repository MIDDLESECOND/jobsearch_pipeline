"""core.validate_config — the load-time shape check.

Every problem must surface in ONE collected error (not first-fail), and a valid config —
including the ATS-only empty-searches shape — must pass untouched. The provider/model
consistency rule lives here too (moved out of evaluate_new_jobs so it fires before any
fetch/eval spend).
"""

import pytest

import core


def _valid():
    return {
        "settings": {
            "location": "United States",
            "hours_old": 4,
            "results_per_search": 30,
            "delay_between_searches": 20,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "max_description_chars": 12000,
            "db_path": "jobs.db",
            "reports_dir": "reports",
        },
        "searches": [{"name": "s1", "term": "data analyst", "min_salary": 80000}],
    }


def test_valid_config_passes_through():
    cfg = _valid()
    assert core.validate_config(cfg) is cfg


def test_empty_searches_is_valid():
    cfg = _valid()
    cfg["searches"] = []  # ATS-only setup: LinkedIn/Adzuna simply no-op
    assert core.validate_config(cfg) is cfg


def test_missing_settings_section():
    with pytest.raises(ValueError, match="settings"):
        core.validate_config({"searches": []})


def test_missing_key_and_wrong_type_are_both_collected():
    cfg = _valid()
    del cfg["settings"]["location"]
    cfg["settings"]["hours_old"] = "four"
    with pytest.raises(ValueError) as e:
        core.validate_config(cfg)
    msg = str(e.value)
    assert "settings.location is missing" in msg
    assert "settings.hours_old" in msg


def test_provider_model_mismatch():
    cfg = _valid()
    cfg["settings"]["provider"] = "anthropic"  # model stays deepseek-v4-flash
    with pytest.raises(ValueError, match="expects a 'claude-\\*' model"):
        core.validate_config(cfg)


def test_unknown_provider():
    cfg = _valid()
    cfg["settings"]["provider"] = "openai"
    with pytest.raises(ValueError, match="provider must be one of"):
        core.validate_config(cfg)


def test_search_entry_problems_reported_with_index():
    cfg = _valid()
    cfg["searches"] = [{"term": "x"}, "not-a-dict", {"name": "ok", "term": "y", "min_salary": "80k"}]
    with pytest.raises(ValueError) as e:
        core.validate_config(cfg)
    msg = str(e.value)
    assert "searches[0].name" in msg
    assert "searches[1]" in msg
    assert "searches[2].min_salary" in msg


def test_bool_is_not_a_number():
    cfg = _valid()
    cfg["settings"]["hours_old"] = True  # YAML `hours_old: yes` — a real footgun
    with pytest.raises(ValueError, match="hours_old"):
        core.validate_config(cfg)
