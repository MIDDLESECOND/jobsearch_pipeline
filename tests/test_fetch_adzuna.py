"""The Adzuna source's canonical-URL helper. Adzuna's redirect_url embeds a per-request
tracking token, so storing it raw gives the same ad a fresh job_url (the PK) every API
call — _adzuna_job_url must reduce a result to one stable URL per ad id so re-serves
dedup at insert. The host comes from redirect_url itself (it follows the configured
country — adzuna.co.uk for gb — so hardcoding www.adzuna.com would bake wrong-site
links into the PK). Pure function, no network."""

from fetch import _adzuna_job_url


def test_ad_id_field_wins():
    r = {"id": "5783524007",
         "redirect_url": "https://www.adzuna.com/land/ad/5783524007?se=TOKEN&utm_medium=api"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/details/5783524007"


def test_same_ad_different_tracking_tokens_canonicalize_identically():
    # int-typed id + exact expected value: an equality-only check would pass on any
    # wrong-but-deterministic output, which is exactly what the PK dedup can't survive.
    a = {"id": 5783524007,
         "redirect_url": "https://www.adzuna.com/land/ad/5783524007?se=jjSHb2B18RG"}
    b = {"id": 5783524007,
         "redirect_url": "https://www.adzuna.com/land/ad/5783524007?se=trbY4Lt18RG"}
    assert _adzuna_job_url(a) == "https://www.adzuna.com/details/5783524007"
    assert _adzuna_job_url(a) == _adzuna_job_url(b)


def test_host_follows_redirect_url_country_site():
    # country: "gb" serves ads on the national site; the canonical URL must keep that host
    # or every stored link points at the wrong country's site (and ad-id namespace).
    r = {"id": 12345,
         "redirect_url": "https://www.adzuna.co.uk/land/ad/12345?se=TOKEN"}
    assert _adzuna_job_url(r) == "https://www.adzuna.co.uk/details/12345"


def test_ad_id_parsed_from_land_ad_url_when_id_missing():
    r = {"redirect_url": "https://www.adzuna.com/land/ad/5783524007?se=TOKEN"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/details/5783524007"


def test_ad_id_parsed_from_details_url_when_id_missing():
    r = {"redirect_url": "https://www.adzuna.com/details/5785122046?utm_medium=api"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/details/5785122046"


def test_falls_back_to_raw_url_when_no_id_derivable():
    # A churny row beats a dropped posting: unrecognized URL shape + no id field.
    r = {"redirect_url": "https://www.adzuna.com/something/else?x=1"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/something/else?x=1"


def test_non_numeric_id_falls_back_to_url_parse():
    r = {"id": "abc-not-numeric",
         "redirect_url": "https://www.adzuna.com/land/ad/999?se=T"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/details/999"


def test_unicode_digit_id_is_not_trusted():
    # str.isdigit() accepts Unicode digits ('5783²'), which would mint a URL the regex-parsed
    # form of the same ad never matches — one ad split across two PKs. ASCII-only.
    r = {"id": "5783²",
         "redirect_url": "https://www.adzuna.com/land/ad/5783?se=T"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/details/5783"


def test_scheme_less_redirect_keeps_raw_url():
    # No scheme → urlsplit sees no host; silently minting the hardcoded .com host would bake
    # a wrong-country PK, so the raw URL (churny but honest) is kept instead.
    r = {"redirect_url": "www.adzuna.co.uk/land/ad/123?se=tok"}
    assert _adzuna_job_url(r) == "www.adzuna.co.uk/land/ad/123?se=tok"


def test_malformed_url_keeps_raw_url_instead_of_raising():
    # urlsplit raises ValueError on a malformed authority — uncaught it would abort the whole
    # Adzuna batch (one bad row, entire source rolled back by _run_fetch_stage).
    r = {"id": "123", "redirect_url": "https://[bad/land/ad/1"}
    assert _adzuna_job_url(r) == "https://[bad/land/ad/1"


def test_query_string_ids_are_never_trusted():
    # An id inside the query string (?return_to=/details/999) belongs to some OTHER page;
    # minting a canonical from it would collide distinct ads onto one PK (second silently
    # dropped by ON CONFLICT DO NOTHING). Path-less id → raw URL fallback.
    r = {"redirect_url": "https://www.adzuna.com/search?return_to=/details/999&x=1"}
    assert _adzuna_job_url(r) == "https://www.adzuna.com/search?return_to=/details/999&x=1"


def test_none_without_redirect_url():
    # No redirect_url → skip the result entirely, id or not: an id-only degenerate payload
    # carries no title/company/description worth a row (same guard as before this helper).
    assert _adzuna_job_url({}) is None
    assert _adzuna_job_url({"id": 5783524007}) is None
    assert _adzuna_job_url({"redirect_url": ""}) is None
    assert _adzuna_job_url({"redirect_url": 42}) is None
