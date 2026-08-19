"""_redact — the Adzuna credential scrubber. Adzuna authenticates via query-string params
(the API's requirement, not a choice), so app_id/app_key travel INSIDE the request URL; the
Adzuna failure path runs every exception message through _redact before printing, as the
safety net for a future exception type that embeds the full URL in str(e). Pure function;
the secrets arrive as arguments (the caller passes the resolved app_id/app_key), so the
tests pass them the same way."""

from fetch import _redact


def test_scrubs_every_secret_occurrence_from_an_error_message():
    # The real hazard shape: an exception whose str() embeds the request URL — including a
    # secret that appears more than once. str.replace is all-occurrence; pin that.
    app_id, app_key = "id4242", "k-sekrit-9x"
    msg = (f"HTTP Error 401: https://api.adzuna.com/v1/api/jobs/us/search/1"
           f"?app_id={app_id}&app_key={app_key}&what=analyst (app_id={app_id})")
    out = _redact(msg, app_id, app_key)
    assert app_id not in out
    assert app_key not in out
    # Redaction replaces, it doesn't truncate: the diagnostic text must survive.
    assert "HTTP Error 401" in out
    assert "***" in out


def test_empty_and_none_secrets_pass_message_through_unchanged():
    # ''.replace('', '***') would interleave *** between every character (and None would
    # TypeError) — the `if s:` guard exists so an absent/blank credential can't mangle
    # the whole error line.
    msg = "urlopen error timed out"
    assert _redact(msg, "", None) == msg


def test_message_without_secrets_is_unchanged():
    assert _redact("plain failure, no URL", "id4242", "k-sekrit-9x") == \
        "plain failure, no URL"
