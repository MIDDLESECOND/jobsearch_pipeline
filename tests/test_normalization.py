"""Normalization + fingerprint — the blocking key behind repost dedup.

These functions carry the longest 'why' comments in the codebase (the false-repost
class an exact title match avoids; the conservative location matching). The edge cases
below are the contract those comments describe.
"""

import chain


# ----- _clean ------------------------------------------------------------------

def test_clean_lowercases_strips_punct_collapses_ws():
    assert chain._clean("  Foo,  BAR!! baz ") == "foo bar baz"


def test_clean_non_string_is_empty():
    assert chain._clean(None) == ""
    assert chain._clean(123) == ""


# ----- _norm_company -----------------------------------------------------------

def test_company_suffixes_stripped():
    # Inc / Corp / LLC / Ltd all reduce to the same bare name.
    assert chain._norm_company("Acme Corp") == "acme"
    assert chain._norm_company("Acme, Inc.") == "acme"
    assert chain._norm_company("Acme LLC") == "acme"
    assert chain._norm_company("Acme Holdings") == "acme"


def test_company_none_is_empty():
    assert chain._norm_company(None) == ""


def test_company_nan_is_empty():
    # pandas yields float NaN for an empty company cell; NaN is truthy, so the regex sub
    # must not run on it (would TypeError). Same intent as None -> "".
    assert chain._norm_company(float("nan")) == ""


# ----- _norm_title -------------------------------------------------------------

def test_title_abbrevs_expanded():
    assert chain._norm_title("Sr. ML Engineer") == "senior machine learning engineer"
    assert chain._norm_title("Jr Dev") == "junior developer"
    assert chain._norm_title("Eng Mgr") == "engineer manager"


def test_title_distinct_qualifiers_stay_distinct():
    # The whole point of exact (not fuzzy) title matching: these must NOT collapse.
    a = chain._norm_title("Workday Business Analyst")
    b = chain._norm_title("SalesForce Business Analyst")
    assert a != b


# ----- _norm_location ----------------------------------------------------------

def test_location_city_named_after_state_not_mangled():
    # "New York, NY" must stay "new york ny", not become "ny ny".
    assert chain._norm_location("New York, NY") == "new york ny"


def test_location_full_state_name_canonicalized_in_tail():
    assert chain._norm_location("Rochester, New York") == "rochester ny"


def test_location_metro_cruft_collapses_with_plain():
    # The documented case: these two labels are the same place and must share a key.
    metro = chain._norm_location("Rochester, New York Metropolitan Area")
    plain = chain._norm_location("Rochester, NY")
    assert metro == plain == "rochester ny"


def test_location_country_token_dropped():
    assert chain._norm_location("New York, NY, United States") == "new york ny"


def test_location_present_state_not_dropped_to_match_absent():
    # Conservative by design: a state-present label must NOT match a state-absent one
    # (over-matching = a false "ALREADY APPLIED", the worse error).
    assert chain._norm_location("Boston, MA") != chain._norm_location("Boston")


def test_location_non_string_is_empty():
    assert chain._norm_location(None) == ""


# ----- _fingerprint ------------------------------------------------------------

def test_fingerprint_is_company_pipe_location():
    assert chain._fingerprint("Acme Corp", "New York, NY") == "acme|new york ny"


def test_fingerprint_same_role_diff_company_suffix_matches():
    assert chain._fingerprint("Acme Inc", "Rochester, New York") == \
           chain._fingerprint("Acme LLC", "Rochester, NY")
