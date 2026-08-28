"""The ATS source's pure core: HTML→text, date normalization, the per-board payload
extractors, the title/location filters, and fetch_ats itself with the network layer
(_ats_get) monkeypatched — payload fixtures mirror the real probed API shapes, so no
test ever touches the network."""

import re

import pytest

import fetch
from conftest import make_job
from fetch import (
    _ats_clean_patterns,
    _ats_date,
    _ats_location_ok,
    _ats_rows_ashby,
    _ats_rows_greenhouse,
    _ats_rows_lever,
    _ats_title_ok,
    _linkedin_date,
    _strip_html,
)

# ---------------------------------------------------------------- payload fixtures
# Shapes mirror live probes (2026-07-02) of boards-api.greenhouse.io / api.lever.co /
# api.ashbyhq.com, trimmed to the fields the extractors read.

GH_PAYLOAD = {
    "jobs": [
        {
            "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/1",
            "title": "Data Analyst",
            "company_name": "ExampleCorp",
            "location": {"name": "New York, NY"},
            "first_published": "2026-06-01T08:00:00-04:00",
            # Greenhouse ships content HTML-ESCAPED, with entities escaped twice.
            "content": "&lt;p&gt;Build &amp;amp; ship dashboards&lt;/p&gt;"
                       "&lt;ul&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
        },
        {
            "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/2",
            "title": "Account Executive",  # no title_any match
            "company_name": "ExampleCorp",
            "location": {"name": "New York, NY"},
            "first_published": "2026-06-02T08:00:00-04:00",
            "content": "&lt;p&gt;Sell things&lt;/p&gt;",
        },
        {
            "absolute_url": "https://boards.greenhouse.io/examplecorp/jobs/3",
            "title": "Data Analyst",
            "company_name": "ExampleCorp",
            "location": {"name": "London, UK"},  # no location_any match, not remote
            "first_published": "2026-06-03T08:00:00-04:00",
            "content": "&lt;p&gt;Mind the gap&lt;/p&gt;",
        },
    ]
}

LEVER_PAYLOAD = [  # Lever's endpoint returns a top-level list
    {
        "text": "Senior Data Analyst",
        "hostedUrl": "https://jobs.lever.co/anotherco/abc-123",
        "createdAt": 1781524800000,  # 2026-06-15T12:00:00Z, epoch MILLISECONDS
        "categories": {"location": "New York, NY",
                       "allLocations": ["New York, NY", "Toronto, ON"]},
        "workplaceType": "hybrid",
        "descriptionPlain": "Intro paragraph about the role.",
        "lists": [
            {"text": "Requirements", "content": "<li>SQL mastery</li><li>Python</li>"},
            {"text": "Nice to have", "content": "<li>dbt</li>"},
        ],
        "additionalPlain": "EEO statement.",
    }
]

ASHBY_PAYLOAD = {
    "jobs": [
        {
            "title": "Data Analyst II",
            "location": "New York, NY (HQ)",
            "jobUrl": "https://jobs.ashbyhq.com/thirdco/def-456",
            "publishedAt": "2026-06-11T17:21:26.410+00:00",
            "isListed": True,
            "isRemote": True,
            "secondaryLocations": [{"location": "Remote (US)"}],
            "descriptionPlain": "Plain-text description already.",
        },
        {
            "title": "Data Analyst (hidden)",
            "location": "New York, NY",
            "jobUrl": "https://jobs.ashbyhq.com/thirdco/ghi-789",
            "publishedAt": "2026-06-12T00:00:00+00:00",
            "isListed": False,  # unlisted → must be skipped
            "isRemote": False,
            "descriptionPlain": "Draft role.",
        },
    ]
}


# ------------------------------------------------------------------- _strip_html

def test_strip_html_escaped_mode_unescapes_then_strips():
    # Escaped Greenhouse-style content: unescape must precede tag-strip, and the
    # double-escaped &amp;amp; must resolve to a bare &.
    out = _strip_html("&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;&lt;p&gt;Fast&lt;/p&gt;", escaped=True)
    assert out == "Build & ship\nFast"


def test_strip_html_raw_mode_preserves_escaped_literals():
    # Lever-style RAW HTML: a once-escaped literal '<' is text, not markup — it must
    # survive, not open a phantom tag that swallows the following words.
    out = _strip_html("<li>Travel: &lt;5%</li><li>SQL required</li>")
    assert "<5%" in out
    assert "SQL required" in out


def test_strip_html_escaped_mode_keeps_double_escaped_literals():
    # Greenhouse escapes its whole payload, so a literal '<' arrives DOUBLE-escaped and
    # must come back as text after both unescape passes.
    out = _strip_html("&lt;p&gt;Travel: &amp;lt;5%&lt;/p&gt;", escaped=True)
    assert out == "Travel: <5%"


def test_strip_html_keeps_paragraph_breaks_and_collapses_whitespace():
    out = _strip_html("<p>One   two</p>\n\n\n\n<li>three</li><li>four</li>")
    assert "One two" in out
    assert "\n\n\n" not in out
    assert out.splitlines()[-1] == "four"


def test_strip_html_non_string():
    assert _strip_html(None) == ""
    assert _strip_html("") == ""


# --------------------------------------------------------------------- _ats_date

def test_ats_date_iso_timestamp_kept_as_local_naive():
    # Full board timestamps keep their time-of-day (the recency sort ranks by it), converted
    # to local naive — the first_seen storage convention. Local wall time varies by machine
    # timezone (the instant is 12:00 UTC), so assert the shape, not the exact hour.
    assert re.fullmatch(r"2026-06-0[12]T\d{2}:\d{2}:\d{2}", _ats_date("2026-06-01T08:00:00-04:00"))


def test_ats_date_fractional_seconds_normalized():
    assert re.fullmatch(r"2026-06-1[12]T\d{2}:\d{2}:\d{2}", _ats_date("2026-06-11T17:21:26.410+00:00"))


def test_ats_date_bare_date_passthrough():
    # A board that only gives a calendar date must stay day-granularity — no invented midnight.
    assert _ats_date("2026-06-11") == "2026-06-11"


def test_ats_date_compact_and_week_dates_stay_day_granularity():
    # Other ISO bare-date forms fromisoformat accepts must ALSO come out as bare dates,
    # not fake-midnight timestamps.
    assert _ats_date("20260601") == "2026-06-01"
    assert _ats_date("2026-W27-5") == "2026-07-03"


def test_ats_date_epoch_ms():
    # Mid-day UTC so any local timezone lands on the same calendar day; time-of-day kept.
    assert re.fullmatch(r"2026-06-1[56]T\d{2}:\d{2}:\d{2}", _ats_date(1781524800000))


def test_ats_date_unparseable():
    assert _ats_date(None) == ""
    assert _ats_date("junk") == ""
    assert _ats_date(True) == ""  # bool passes isinstance(int) — must not become 1969/1970
    assert _ats_date("Posted 3 weeks ago") == ""  # >=10 chars but not a date — no blind slice


def test_ats_date_zero_epoch_is_garbage_not_1970():
    # A zeroed Lever createdAt is missing data, not a 1969/1970 posting.
    assert _ats_date(0) == ""
    assert _ats_date(0.0) == ""


def test_ats_date_absurd_dates_rejected():
    # Placeholder dates outside the parse_iso sanity window must not be stored.
    assert _ats_date("9999-12-31") == ""
    assert _ats_date("9999-12-31T00:00:00Z") == ""


def test_ats_date_datelike_prefix_with_exotic_suffix_keeps_day():
    # Starts with a date but isn't parseable ISO: degrade to the day, not to "" — including
    # with incidental leading whitespace (both branches must strip identically).
    assert _ats_date("2026-06-01 (updated)") == "2026-06-01"
    assert _ats_date("  2026-06-01 (updated)") == "2026-06-01"


# ------------------------------------------------------------------ _linkedin_date

def test_linkedin_date_strips_pandas_fake_midnight():
    # THE reason _linkedin_date exists and must never be "simplified" into _ats_date:
    # a pandas datetime64 stringifies with a midnight time that isn't real precision.
    assert _linkedin_date("2026-07-04 00:00:00") == "2026-07-04"


def test_linkedin_date_passthrough_and_degrades():
    from datetime import date as _date
    assert _linkedin_date(_date(2026, 7, 4)) == "2026-07-04"  # jobspy's normal shape
    assert _linkedin_date("2026-07-04") == "2026-07-04"
    assert _linkedin_date(None) == ""
    assert _linkedin_date("nan") == ""    # str(float('nan')) from an empty pandas cell
    assert _linkedin_date("NaT") == ""    # str(pd.NaT)


# ----------------------------------------------------------------------- filters

def test_title_ok_any_of_case_insensitive():
    assert _ats_title_ok("Senior DATA Analyst, Growth", ["data analyst", "product analyst"])


def test_title_ok_no_match():
    assert not _ats_title_ok("Account Executive", ["data analyst"])


def test_title_ok_re_prefix_is_a_regex():
    # Same pattern dialect as filters.yaml: `re:` makes it a case-insensitive regex.
    assert _ats_title_ok("Product Analyst", ["re:(data|product) analyst"])
    assert not _ats_title_ok("Product Manager", ["re:(data|product) analyst"])


def test_location_ok_absent_accepts_all():
    assert _ats_location_ok(["Anywhere, Earth"], False, [])


def test_location_ok_substring():
    assert _ats_location_ok(["New York, NY (HQ)"], False, ["new york"])
    assert not _ats_location_ok(["London, UK"], False, ["new york"])


def test_location_ok_matches_any_listed_location():
    # Multi-location roles must match on ANY posted location, not just the primary.
    assert _ats_location_ok(["Miami", "New York"], False, ["new york"])
    assert not _ats_location_ok(["Miami", "Austin"], False, ["new york"])


def test_location_ok_remote_needs_exact_opt_in():
    # A remote role whose location strings don't match is accepted only when the
    # list contains the EXACT term "remote".
    assert _ats_location_ok(["Anywhere"], True, ["remote", "new york"])
    assert not _ats_location_ok(["Anywhere"], True, ["new york"])
    # A qualified term is a location pattern, NOT a remote opt-in — "remote - us"
    # must not silently admit remote-anywhere roles.
    assert not _ats_location_ok(["Anywhere"], True, ["remote - us", "new york"])
    assert _ats_location_ok(["Remote - US"], True, ["remote - us"])  # substring still matches


def test_location_ok_remote_flag_never_blocks_a_location_match():
    # Hybrid postings can carry isRemote=true AND a matching city — the substring
    # match wins, so listing the city (without "remote") still keeps them.
    assert _ats_location_ok(["New York, NY (HQ)"], True, ["new york"])


# ------------------------------------------------------------ _ats_clean_patterns

def test_clean_patterns_drops_non_strings_and_blanks(capsys):
    # YAML `- re: x` parses as a dict, unquoted numbers as ints; "" and " " are
    # always-true substrings that would bypass the flood guard.
    out = _ats_clean_patterns(
        ["data analyst", "", "  ", 2024, {"re": "(a|b) analyst"}], "title_any")
    assert out == ["data analyst"]
    assert capsys.readouterr().err.count("ignoring title_any pattern") == 4


def test_clean_patterns_drops_broken_regex(capsys):
    # _pattern_matches swallows re.error -> False, which here would silently match
    # NOTHING (fail-closed) — the pattern must be dropped loudly instead.
    out = _ats_clean_patterns(["re:(data|product analyst", "re:(data|product) analyst"],
                              "title_any")
    assert out == ["re:(data|product) analyst"]
    assert "invalid regex" in capsys.readouterr().err


def test_clean_patterns_drops_empty_body_regex(capsys):
    # `re:` / `re: ` compile fine (empty regex) but then match EVERYTHING — the opposite
    # flood — so they must be dropped, not kept.
    out = _ats_clean_patterns(["re:", "re: ", "data analyst"], "title_any")
    assert out == ["data analyst"]
    assert capsys.readouterr().err.count("would match everything") == 2


def test_clean_patterns_scalar_is_one_pattern():
    assert _ats_clean_patterns("data analyst", "title_any") == ["data analyst"]
    assert _ats_clean_patterns(None, "title_any") == []


# -------------------------------------------------------------------- extractors

def test_greenhouse_rows_shape():
    rows = _ats_rows_greenhouse(GH_PAYLOAD, "Fallback Co")
    assert len(rows) == 3
    r = rows[0]
    assert r["url"] == "https://boards.greenhouse.io/examplecorp/jobs/1"
    assert r["title"] == "Data Analyst"
    assert r["company"] == "ExampleCorp"  # payload company_name wins over fallback
    assert r["location"] == "New York, NY"
    assert re.fullmatch(r"2026-06-0[12]T\d{2}:\d{2}:\d{2}", r["date_posted"])  # local naive
    assert r["description"] == "Build & ship dashboards\nSQL\nPython"
    assert r["remote"] is False


def test_lever_rows_full_description():
    rows = _ats_rows_lever(LEVER_PAYLOAD, "Another Co")
    assert len(rows) == 1
    r = rows[0]
    assert r["company"] == "Another Co"  # no company in Lever payloads → config name
    assert r["locations"] == ["New York, NY", "Toronto, ON"]  # allLocations, not just primary
    assert r["remote"] is False  # workplaceType "hybrid" — no location-substring heuristic
    assert re.fullmatch(r"2026-06-1[56]T\d{2}:\d{2}:\d{2}", r["date_posted"])
    # The description must include the intro, every list section (title + bullets),
    # and the closing blurb — not just descriptionPlain.
    for piece in ("Intro paragraph", "Requirements", "SQL mastery", "Python",
                  "Nice to have", "dbt", "EEO statement"):
        assert piece in r["description"]


def test_ashby_rows_skip_unlisted_and_map_remote():
    rows = _ats_rows_ashby(ASHBY_PAYLOAD, "Third Co")
    assert [r["title"] for r in rows] == ["Data Analyst II"]
    r = rows[0]
    assert r["company"] == "Third Co"
    assert re.fullmatch(r"2026-06-1[12]T\d{2}:\d{2}:\d{2}", r["date_posted"])  # local naive
    assert r["description"] == "Plain-text description already."
    assert r["remote"] is True  # isRemote flag, not a location-substring heuristic
    assert r["locations"] == ["New York, NY (HQ)", "Remote (US)"]  # + secondaryLocations


def test_blank_primary_location_falls_back_to_listed():
    # A Lever/Ashby role whose primary location is blank but which lists others must display
    # the first listed location, not a blank — so a role matched on a secondary location
    # doesn't show up location-less in the report/UI.
    lever = _ats_rows_lever(
        [{"text": "Data Analyst", "hostedUrl": "u", "createdAt": 1781524800000,
          "categories": {"allLocations": ["Toronto, ON"]}}],  # no `location` key
        "Co")
    assert lever[0]["location"] == "Toronto, ON"
    assert lever[0]["locations"] == ["Toronto, ON"]
    ashby = _ats_rows_ashby(
        {"jobs": [{"title": "Data Analyst", "jobUrl": "u", "isListed": True, "location": "",
                   "secondaryLocations": [{"location": "Remote (US)"}]}]},
        "Co")
    assert ashby[0]["location"] == "Remote (US)"


def test_lever_blank_first_location_does_not_poison():
    # A blank entry in allLocations must NOT become the display location or fingerprint when
    # the primary is also blank — the first NON-blank location wins (parity with Ashby).
    rows = _ats_rows_lever(
        [{"text": "Data Analyst", "hostedUrl": "u", "createdAt": 1781524800000,
          "categories": {"location": "", "allLocations": ["", "New York, NY"]}}],
        "Co")
    assert rows[0]["location"] == "New York, NY"
    assert rows[0]["locations"] == ["New York, NY"]


# --------------------------------------------------------------------- fetch_ats

def _ats_cfg(**overrides):
    ats = {
        "title_any": ["data analyst"],
        "location_any": ["remote", "new york"],
        "delay_between_calls": 0,
        "companies": [
            {"slug": "examplecorp", "board": "greenhouse"},
            {"slug": "anotherco", "board": "lever", "name": "Another Co"},
            {"slug": "thirdco", "board": "ashby", "name": "Third Co"},
        ],
    }
    ats.update(overrides)
    return {"settings": {"max_description_chars": 12000, "ats": ats}}


def _fake_ats_get(url):
    if "greenhouse" in url:
        return GH_PAYLOAD
    if "lever" in url:
        return LEVER_PAYLOAD
    if "ashbyhq" in url:
        return ASHBY_PAYLOAD
    raise AssertionError(f"unexpected URL {url}")


def test_fetch_ats_inserts_and_filters(conn, monkeypatch):
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    assert fetch.fetch_ats(_ats_cfg(), conn) == 3  # GH job 1, Lever, Ashby listed

    rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM jobs").fetchall()}
    assert set(rows) == {
        "https://boards.greenhouse.io/examplecorp/jobs/1",
        "https://jobs.lever.co/anotherco/abc-123",
        "https://jobs.ashbyhq.com/thirdco/def-456",
    }  # jobs 2 (title) and 3 (location) filtered out; unlisted Ashby job skipped
    gh = rows["https://boards.greenhouse.io/examplecorp/jobs/1"]
    assert gh["status"] == "new"
    assert gh["source"] == "greenhouse"
    assert gh["search_name"] == "ats:examplecorp"
    assert gh["tier"] == "primary"
    assert gh["salary_min"] is None and gh["salary_max"] is None
    assert "Build & ship dashboards" in gh["description"]
    assert rows["https://jobs.lever.co/anotherco/abc-123"]["source"] == "lever"
    assert rows["https://jobs.ashbyhq.com/thirdco/def-456"]["source"] == "ashby"

    # Idempotency: the same boards re-fetched insert nothing new.
    assert fetch.fetch_ats(_ats_cfg(), conn) == 0


def test_fetch_ats_noop_without_config(conn, capsys):
    assert fetch.fetch_ats({"settings": {}}, conn) == 0
    assert "skipping ATS source" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_ats_refuses_empty_title_any(conn, capsys):
    assert fetch.fetch_ats(_ats_cfg(title_any=[]), conn) == 0
    assert "title_any is empty" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_ats_skips_bad_board(conn, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    # The example board name must be one ATS_BOARDS really lacks. It was "workday" until
    # 2026-08-20 and then "icims" until later the same day — each time the board got built,
    # this test correctly went red. Keep it pointed at something with no build on the
    # horizon (the census found no SmartRecruiters employer worth a reader).
    cfg = _ats_cfg(companies=[{"slug": "x", "board": "smartrecruiters"}])
    assert fetch.fetch_ats(cfg, conn) == 0
    assert "bad companies entry" in capsys.readouterr().err


def test_fetch_ats_scalar_title_any_is_one_keyword(conn, monkeypatch):
    # A YAML scalar (`title_any: "data analyst"`) must behave as a list-of-one, NOT be
    # iterated per-character (which would match every title and flood the paid eval).
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    cfg = _ats_cfg(title_any="data analyst", location_any="new york")
    fetch.fetch_ats(cfg, conn)
    titles = [r["title"] for r in conn.execute("SELECT title FROM jobs").fetchall()]
    assert "Account Executive" not in titles  # would match 'a'/'n'/... if char-iterated
    assert "Data Analyst" in titles


def test_fetch_ats_string_company_entry_skipped_not_crash(conn, monkeypatch, capsys):
    # `companies: [examplecorp]` (bare string, the natural YAML shorthand) must be skipped
    # with a notice — not raise AttributeError and abort the whole run.
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    cfg = _ats_cfg(companies=["examplecorp",
                              {"slug": "thirdco", "board": "ashby", "name": "Third Co"}])
    assert fetch.fetch_ats(cfg, conn) == 1  # the valid entry still runs
    assert "bad companies entry" in capsys.readouterr().err


def test_fetch_ats_wrong_shape_payload_logs_failed(conn, monkeypatch, capsys):
    # A wrong-shaped 200 response (envelope change / error object) must produce a FAILED
    # line, not read as an empty board; other companies still run.
    def fake(url):
        if "greenhouse" in url:
            return {"error": "gone"}  # no "jobs" key
        return _fake_ats_get(url)
    monkeypatch.setattr(fetch, "_ats_get", fake)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_ats(_ats_cfg(), conn) == 2  # Lever + Ashby survive
    assert "examplecorp (greenhouse) FAILED" in capsys.readouterr().err


def test_fetch_ats_scalar_companies_one_notice(conn, monkeypatch, capsys):
    # `companies: examplecorp` (scalar) must produce ONE bad-entry notice, not iterate
    # the string per-character.
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_ats(_ats_cfg(companies="examplecorp"), conn) == 0
    assert capsys.readouterr().err.count("bad companies entry") == 1


def test_fetch_ats_all_patterns_broken_refuses(conn, monkeypatch, capsys):
    # If every title_any pattern is dropped by sanitization, the source must refuse
    # loudly (like an empty title_any) — not run unfiltered or silently match nothing.
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_ats(_ats_cfg(title_any=["re:(data|product analyst"]), conn) == 0
    err = capsys.readouterr().err
    assert "invalid regex" in err
    assert "skipping" in err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_ats_all_location_patterns_broken_refuses(conn, monkeypatch, capsys):
    # A configured location_any that empties out under sanitization must REFUSE, not fall
    # through to _ats_location_ok's "no filter → accept all" and silently flood.
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_ats(_ats_cfg(location_any=["re:[unterminated"]), conn) == 0
    assert "location_any pattern was unusable" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_ats_row_error_contained_and_rolled_back(conn, monkeypatch, capsys):
    # A row-level failure mid-board must (a) log FAILED for that board only — other boards
    # still run — and (b) roll back the board's ALREADY-INSERTED rows. So greenhouse gets TWO
    # matching rows and boom raises on the SECOND: the first was inserted (uncommitted) before
    # the failure, and must NOT persist. (Asserting only 'other boards survive' would pass even
    # with rollback() deleted, since a single-matching-row board inserts nothing before it fails.)
    gh_two = {"jobs": [
        {"absolute_url": "https://gh/1", "title": "Data Analyst", "company_name": "X",
         "location": {"name": "New York, NY"}, "first_published": "2026-06-01", "content": "a"},
        {"absolute_url": "https://gh/2", "title": "Data Analyst", "company_name": "X",
         "location": {"name": "New York, NY"}, "first_published": "2026-06-02", "content": "b"},
    ]}

    def fake(url):
        return gh_two if "greenhouse" in url else _fake_ats_get(url)

    real = fetch._insert_posting

    def boom(conn_, **kw):
        if kw["source"] == "greenhouse" and kw["url"].endswith("/2"):
            raise RuntimeError("boom")
        return real(conn_, **kw)

    monkeypatch.setattr(fetch, "_insert_posting", boom)
    monkeypatch.setattr(fetch, "_ats_get", fake)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_ats(_ats_cfg(), conn) == 2  # Lever + Ashby survive
    assert "examplecorp (greenhouse) FAILED: boom" in capsys.readouterr().err
    rows = conn.execute("SELECT job_url, source FROM jobs").fetchall()
    assert {r["source"] for r in rows} == {"lever", "ashby"}  # greenhouse fully absent
    # The row inserted BEFORE the failure must have been rolled back, not just uncommitted.
    assert not any(r["job_url"] == "https://gh/1" for r in rows)


def test_fetch_ats_int_slug_coerced(conn, monkeypatch):
    # A digit-only board slug parses as a YAML int; it must be coerced, not crash.
    seen = []

    def fake(url):
        seen.append(url)
        return {"jobs": []}

    monkeypatch.setattr(fetch, "_ats_get", fake)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    cfg = _ats_cfg(companies=[{"slug": 123, "board": "greenhouse"}])
    assert fetch.fetch_ats(cfg, conn) == 0
    assert seen and "123" in seen[0]


def test_fetch_ats_links_repost(conn, monkeypatch):
    # A role already seen (any source) with the same normalized company+location+title
    # makes the ATS insert a repost pointing at the canonical original.
    orig = make_job(conn, job_url="u1", company="ExampleCorp",
                    location="New York, NY", title="Data Analyst")
    monkeypatch.setattr(fetch, "_ats_get", _fake_ats_get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    fetch.fetch_ats(_ats_cfg(), conn)
    gh = conn.execute(
        "SELECT repost_of FROM jobs WHERE job_url=?",
        ("https://boards.greenhouse.io/examplecorp/jobs/1",),
    ).fetchone()
    assert gh["repost_of"] == orig["job_url"]


# ---------------------------------------------------------------- Workday (CXS)
# Shapes captured 2026-08-20 from the LIVE boards wsgr.wd503/WSGR and
# goodwinprocter.wd5/external_careers — the list POST and the per-posting detail GET,
# trimmed to the fields _workday_rows reads. Not invented: an invented fixture is how 38
# green Dice tests once covered a fetcher that extracted nothing from a real page.

WD_LIST_PAGE = {
    "total": 3,
    "jobPostings": [
        {"title": "AI Enablement Specialist",
         "externalPath": "/job/Palo-Alto/AI-Enablement-Specialist_R1696",
         "locationsText": "14 Locations", "postedOn": "Posted 29 Days Ago",
         "bulletFields": ["R1696"]},
        {"title": "Senior Trademark Paralegal",          # no title_any match
         "externalPath": "/job/Seattle/Senior-Trademark-Paralegal_R1730-1",
         "locationsText": "13 Locations", "postedOn": "Posted 27 Days Ago",
         "bulletFields": ["R1730"]},
        {"title": "AI Systems Manager",
         "externalPath": "/job/Palo-Alto/AI-Systems-Manager_R1738-1",
         "locationsText": "14 Locations", "postedOn": "Posted 7 Days Ago",
         "bulletFields": ["R1738"]},
    ],
    "userAuthenticated": False,
}
WD_DETAIL = {
    "/job/Palo-Alto/AI-Enablement-Specialist_R1696": {
        "jobPostingInfo": {
            "title": "AI Enablement Specialist", "jobReqId": "R1696",
            "startDate": "2026-07-22", "location": "Palo Alto", "timeType": "Full time",
            # additionalLocations shape from a live probe (2026-08-20): multi-city law reqs
            # carry a primary plus a city list (WSGR had 13, Holland & Knight 29).
            "additionalLocations": ["Salt Lake City", "San Diego"],
            "jobDescription": "Join the firm's Innovation team. Full virtual opportunity.",
        }},
    "/job/Palo-Alto/AI-Systems-Manager_R1738-1": {
        "jobPostingInfo": {
            "title": "AI Systems Manager", "jobReqId": "R1738",
            "startDate": "2026-08-13", "location": "Palo Alto", "timeType": "Full time",
            "jobDescription": "Lead the AI Systems team.",
        }},
}


def _wd_cfg(**over):
    ats = {"title_any": ["ai enablement", "ai systems"], "delay_between_calls": 0,
           "companies": [{"slug": "wsgr/wd503/WSGR", "board": "workday",
                          "name": "Wilson Sonsini Goodrich & Rosati"}]}
    ats.update(over)
    return {"settings": {"max_description_chars": 12000, "ats": ats}}


def _wd_patch(monkeypatch, detail_log=None):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch, "_workday_post", lambda url, payload: (
        WD_LIST_PAGE if payload["offset"] == 0 else {"total": 3, "jobPostings": []}))

    def fake_get(url):
        path = "/job/" + url.split("/job/", 1)[1]
        if detail_log is not None:
            detail_log.append(path)
        return WD_DETAIL[path]

    monkeypatch.setattr(fetch, "_ats_get", fake_get)


def test_workday_parts_splits_and_rejects():
    assert fetch._workday_parts("wsgr/wd503/WSGR") == ("wsgr", "wd503", "WSGR")
    for bad in ("wsgr", "wsgr/wd503", "wsgr/wd503/WSGR/extra"):
        try:
            fetch._workday_parts(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse as a workday slug")


def test_fetch_ats_workday_inserts_with_employer_date(conn, monkeypatch):
    _wd_patch(monkeypatch)
    assert fetch.fetch_ats(_wd_cfg(), conn) == 2      # paralegal row filtered by title

    rows = {r["job_url"]: r for r in conn.execute("SELECT * FROM jobs").fetchall()}
    url = "https://wsgr.wd503.myworkdayjobs.com/WSGR/job/Palo-Alto/AI-Enablement-Specialist_R1696"
    assert set(rows) == {
        url,
        "https://wsgr.wd503.myworkdayjobs.com/WSGR/job/Palo-Alto/AI-Systems-Manager_R1738-1",
    }
    r = rows[url]
    assert r["source"] == "workday"
    assert r["status"] == "new"
    assert r["company"] == "Wilson Sonsini Goodrich & Rosati"
    assert r["search_name"] == "ats:wsgr/wd503/WSGR"
    # The employer's own startDate, NOT the day the row was fetched. Pinned as a literal:
    # deriving it from _ats_date here would let the same bug pass on both sides.
    assert r["date_posted"] == "2026-07-22"
    assert r["location"] == "Palo Alto"
    assert "Innovation team" in r["description"]
    assert r["salary_min"] is None and r["salary_max"] is None


def test_workday_skips_detail_fetch_for_known_urls(conn, monkeypatch):
    """The load-bearing economics: a url already in `jobs` must never cost a detail GET."""
    log = []
    _wd_patch(monkeypatch, detail_log=log)
    fetch.fetch_ats(_wd_cfg(), conn)
    assert len(log) == 2                              # both new rows paid one detail each

    log.clear()
    assert fetch.fetch_ats(_wd_cfg(), conn) == 0      # idempotent second run
    assert log == []                                  # and it paid for NOTHING


def test_workday_title_filter_runs_before_the_detail_fetch(conn, monkeypatch):
    """A title the filter rejects must not be paid for either — different outcome on each
    side of the filter, so a no-op filter cannot pass this."""
    log = []
    _wd_patch(monkeypatch, detail_log=log)
    fetch.fetch_ats(_wd_cfg(title_any=["ai enablement"]), conn)
    assert log == ["/job/Palo-Alto/AI-Enablement-Specialist_R1696"]
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1


def test_workday_wrong_shaped_payload_fails_the_board(conn, monkeypatch, capsys):
    """A renamed envelope must record FAILED, never success/returned_count=0 — the same rule
    the Adzuna and ATS readers state."""
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch, "_workday_post", lambda url, payload: {"jobs": []})
    assert fetch.fetch_ats(_wd_cfg(), conn) == 0
    err = capsys.readouterr().err
    assert "FAILED" in err and "workday" in err
    # Nothing inserted — the contrast that matters: a silent [] would have inserted nothing
    # too, but quietly, so the stderr FAILED above is the half of this assertion with teeth.
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def _extra_payload(slug):
    # Minimal greenhouse shape (mirrors GH_PAYLOAD's 2026-07-02 probe fields). Same three
    # titles on every board so the ONLY variable between boards is the config's
    # title_any_extra — the point of the tests below.
    def job(n, title):
        return {
            "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/{n}",
            "title": title,
            "company_name": slug.title(),
            "location": {"name": "New York, NY"},
            "first_published": "2026-08-01T08:00:00-04:00",
            "content": "&lt;p&gt;Body&lt;/p&gt;",
        }
    return {"jobs": [job(1, "Data Analyst"),        # shared title_any match
                     job(2, "AI Legal Engineer"),   # extra-only match
                     job(3, "Receptionist")]}       # matches neither


def test_fetch_ats_title_any_extra_widens_only_its_own_board(conn, monkeypatch):
    # The union boundary, asserted on BOTH sides: the same title on two boards must land
    # differently depending only on which board carries the extra vocabulary.
    monkeypatch.setattr(
        fetch, "_ats_get",
        lambda url: _extra_payload("withextra" if "withextra" in url else "noextra"))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    cfg = _ats_cfg(companies=[
        {"slug": "withextra", "board": "greenhouse",
         "title_any_extra": ["legal engineer"]},
        {"slug": "noextra", "board": "greenhouse"},
    ])

    assert fetch.fetch_ats(cfg, conn) == 3

    urls = {r["job_url"] for r in conn.execute("SELECT job_url FROM jobs")}
    assert urls == {
        # extra board: shared pattern still live AND the extra admits its title
        "https://boards.greenhouse.io/withextra/jobs/1",
        "https://boards.greenhouse.io/withextra/jobs/2",
        # no-extra board: the identical title stays out — extra never leaks across boards
        "https://boards.greenhouse.io/noextra/jobs/1",
    }
    # matches-neither stays out everywhere: the union widened, it did not open the gate
    assert not any("jobs/3" in u for u in urls)


def test_fetch_ats_unusable_extra_degrades_to_shared(conn, monkeypatch, capsys):
    # All-unusable extras fall back to shared-only — the safe (narrower) direction — with
    # a per-pattern notice naming the board, not a refusal like location_any's inverted case.
    monkeypatch.setattr(fetch, "_ats_get", lambda url: _extra_payload("withextra"))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    cfg = _ats_cfg(companies=[
        {"slug": "withextra", "board": "greenhouse", "title_any_extra": ["re:", "  "]},
    ])

    assert fetch.fetch_ats(cfg, conn) == 1  # Data Analyst via the shared list only

    urls = {r["job_url"] for r in conn.execute("SELECT job_url FROM jobs")}
    assert urls == {"https://boards.greenhouse.io/withextra/jobs/1"}
    err = capsys.readouterr().err
    assert "title_any_extra [withextra]" in err


def test_workday_location_filter_sees_additional_locations(conn, monkeypatch):
    # Both sides of the boundary in one config: "salt lake" appears ONLY in the Enablement
    # row's additionalLocations (probe-shaped fixture, 2026-08-20), never as a primary.
    # Before the fix the filter saw [primary] alone, so this config inserted zero rows.
    _wd_patch(monkeypatch)
    assert fetch.fetch_ats(_wd_cfg(location_any=["salt lake"]), conn) == 1
    rows = [r["job_url"] for r in conn.execute("SELECT job_url FROM jobs")]
    assert rows == [
        "https://wsgr.wd503.myworkdayjobs.com/WSGR/job/Palo-Alto/AI-Enablement-Specialist_R1696"
    ]  # AI Systems Manager (primary-only "Palo Alto") correctly dropped by the same filter


# --- iCIMS -------------------------------------------------------------------------------
# Fixtures derived from captured probes (2026-08-20, staffcareers-mcguirewoods.icims.com):
# the listing page's job-card <li> structure and the detail page's ld+json block, trimmed to
# what the extractor reads. The fabricated datePosted is KEPT in the detail fixture — it is
# real observed data (render-time minus exactly two years), and the test asserts we ignore it.

ICIMS_CARD_AI = '''
<li class="iCIMS_JobCardItem"> <div class="row"> <div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span > US-NC-Charlotte | US-VA-Richmond</span> </div>
<div class="col-xs-12 title">
<a href="https://staffcareers-mcguirewoods.icims.com/jobs/6861/ai-specialist---legal-document-specialist/job?in_iframe=1"
   class="iCIMS_Anchor" title="6861 - AI Specialist - Legal Document Specialist">
<span class="sr-only field-label">Job Title</span>
<h3 > AI Specialist - Legal Document Specialist</h3> </a> </div> </div> </li>
'''

ICIMS_CARD_OTHER = '''
<li class="iCIMS_JobCardItem"> <div class="row"> <div class="col-xs-6 header left">
<span class="sr-only field-label">Job Locations</span>
<span > US-VA-Richmond</span> </div>
<div class="col-xs-12 title">
<a href="https://staffcareers-mcguirewoods.icims.com/jobs/6900/legal-practice-assistant/job?in_iframe=1"
   class="iCIMS_Anchor" title="6900 - Legal Practice Assistant">
<span class="sr-only field-label">Job Title</span>
<h3 > Legal Practice Assistant</h3> </a> </div> </div> </li>
'''

ICIMS_LIST_PAGE = ('<div class="iCIMS_MainWrapper iCIMS_ListingsPage">'
                   + ICIMS_CARD_AI + ICIMS_CARD_OTHER + "</div>")
ICIMS_EMPTY_PAGE = '<div class="iCIMS_MainWrapper iCIMS_ListingsPage"></div>'

ICIMS_DETAIL_PAGE = '''
<html><head>
<script type="application/ld+json">
{"@context": "http://schema.org/", "@type": "JobPosting",
 "title": "AI Specialist - Legal Document Specialist",
 "datePosted": "2024-08-21T01:48:54.574Z",
 "description": "<p>Overview</p><p>McGuireWoods is seeking a technology-driven BRC AI Specialist to support the firm&rsquo;s investment in artificial intelligence, including Harvey and Jigsaw.</p>",
 "jobLocation": [{"address": {"addressLocality": "Charlotte", "addressRegion": "NC", "@type": "PostalAddress"}}]}
</script>
</head><body><h1>AI Specialist - Legal Document Specialist</h1></body></html>
'''


def _icims_cfg(**over):
    ats = {"title_any": ["ai specialist"], "delay_between_calls": 0,
           "companies": [{"slug": "staffcareers-mcguirewoods", "board": "icims",
                          "name": "McGuireWoods"}]}
    ats.update(over)
    return {"settings": {"max_description_chars": 12000, "ats": ats}}


def _icims_patch(monkeypatch, fetch_log=None):
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    def fake_get(url):
        if fetch_log is not None:
            fetch_log.append(url)
        if "/jobs/search" in url:
            return ICIMS_LIST_PAGE if "pr=0" in url else ICIMS_EMPTY_PAGE
        return ICIMS_DETAIL_PAGE

    monkeypatch.setattr(fetch, "_icims_get", fake_get)


def test_fetch_ats_icims_inserts_row_with_null_date(conn, monkeypatch):
    _icims_patch(monkeypatch)
    assert fetch.fetch_ats(_icims_cfg(), conn) == 1   # Legal Practice Assistant filtered

    r = conn.execute("SELECT * FROM jobs").fetchone()
    # Canonical url: the ?in_iframe=1 query is stripped
    assert r["job_url"] == ("https://staffcareers-mcguirewoods.icims.com/jobs/6861/"
                            "ai-specialist---legal-document-specialist/job")
    assert r["source"] == "icims"
    assert r["status"] == "new"
    assert r["company"] == "McGuireWoods"
    assert r["search_name"] == "ats:staffcareers-mcguirewoods"
    assert r["location"] == "US-NC-Charlotte | US-VA-Richmond"
    # Pinned literal: the fixture's ld+json datePosted is the observed render-time fiction,
    # and the stored value must be NULL — first_seen stands in downstream (mode='seen').
    assert r["date_posted"] is None
    assert "Harvey and Jigsaw" in r["description"]     # ld+json description, HTML stripped
    assert "<p>" not in r["description"]


def test_icims_title_filter_and_known_urls_precede_detail(conn, monkeypatch):
    # Economics boundary, both directions: the non-matching card's detail is never fetched,
    # and a known url costs no detail fetch on the next cycle.
    log = []
    _icims_patch(monkeypatch, fetch_log=log)
    fetch.fetch_ats(_icims_cfg(), conn)
    details = [u for u in log if "/jobs/search" not in u]
    assert details == ["https://staffcareers-mcguirewoods.icims.com/jobs/6861/"
                       "ai-specialist---legal-document-specialist/job?in_iframe=1"]

    log.clear()
    fetch.fetch_ats(_icims_cfg(), conn)               # second cycle: url now known
    assert [u for u in log if "/jobs/search" not in u] == []


def test_icims_pagination_stops_on_repeated_ids(conn, monkeypatch):
    # A portal that serves the same FULL page for every pr= must terminate via the
    # no-new-ids check, not loop to the cap. The page must hold ICIMS_PAGE cards, or the
    # short-page break fires first and this test exercises nothing (its first version did
    # exactly that — 2 cards < 20 stopped after pr=0 with the repeat branch untouched).
    full_page = '<div class="iCIMS_MainWrapper iCIMS_ListingsPage">' + "".join(
        ICIMS_CARD_OTHER.replace("6900", str(7000 + i)) for i in range(fetch.ICIMS_PAGE)
    ) + "</div>"
    log = []
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    def same_page(url):
        log.append(url)
        return full_page if "/jobs/search" in url else ICIMS_DETAIL_PAGE

    monkeypatch.setattr(fetch, "_icims_get", same_page)
    fetch.fetch_ats(_icims_cfg(), conn)
    assert len([u for u in log if "/jobs/search" in u]) == 2  # pr=0 full, pr=1 all-repeats stop


def test_fetch_ats_location_any_extra_widens_only_its_own_board(conn, monkeypatch):
    # Same portal data under two slugs; only one board carries the extra. The shared list
    # matches nothing in "US-NC-Charlotte", so the row lands on exactly one board.
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    def fake_get(url):
        if "/jobs/search" in url:
            host = "withextra" if "withextra" in url else "noextra"
            return (ICIMS_LIST_PAGE.replace("staffcareers-mcguirewoods", host)
                    if "pr=0" in url else ICIMS_EMPTY_PAGE)
        return ICIMS_DETAIL_PAGE

    monkeypatch.setattr(fetch, "_icims_get", fake_get)
    cfg = _icims_cfg(location_any=["new york"], companies=[
        {"slug": "withextra", "board": "icims", "location_any_extra": ["us-"]},
        {"slug": "noextra", "board": "icims"},
    ])
    assert fetch.fetch_ats(cfg, conn) == 1
    urls = [r["job_url"] for r in conn.execute("SELECT job_url FROM jobs")]
    assert urls == ["https://withextra.icims.com/jobs/6861/"
                    "ai-specialist---legal-document-specialist/job"]


def test_location_any_extra_cannot_narrow_an_absent_shared_filter(conn, monkeypatch):
    # Guard boundary: shared location_any ABSENT means accept-everything; a board-local
    # extra must not turn that into "only what the extra matches".
    _icims_patch(monkeypatch)
    cfg = _icims_cfg(companies=[
        {"slug": "staffcareers-mcguirewoods", "board": "icims",
         "location_any_extra": ["nowhere-that-matches"]},
    ])
    cfg["settings"]["ats"].pop("location_any", None)
    assert fetch.fetch_ats(cfg, conn) == 1            # still accepted: extras only widen


def test_icims_cardless_location_falls_back_to_detail_ld_json(conn, monkeypatch):
    # careers-lw's portal skin renders NO location on the job cards (measured 2026-08-20),
    # which silently location-dropped every title match. The detail ld+json address fills
    # in. Boundary both ways: the derived "Charlotte, NC" passes a "charlotte" filter and
    # fails a "new york" one.
    bare_card = ICIMS_CARD_AI.replace(
        '<span class="sr-only field-label">Job Locations</span>', "").replace(
        "<span > US-NC-Charlotte | US-VA-Richmond</span>", "")
    page = '<div class="iCIMS_MainWrapper iCIMS_ListingsPage">' + bare_card + "</div>"
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch, "_icims_get", lambda url: (
        (page if "pr=0" in url else ICIMS_EMPTY_PAGE)
        if "/jobs/search" in url else ICIMS_DETAIL_PAGE))

    assert fetch.fetch_ats(_icims_cfg(location_any=["charlotte"]), conn) == 1
    r = conn.execute("SELECT location FROM jobs").fetchone()
    assert r["location"] == "Charlotte, NC"   # from the fixture's ld+json jobLocation

    conn.execute("DELETE FROM jobs")
    conn.commit()
    assert fetch.fetch_ats(_icims_cfg(location_any=["new york"]), conn) == 0


# The interstitial body is the observed shape (2026-08-20, careers-sidley / jobs-mayerbrown):
# an iCIMS-SERVED page, so it carries the portal branding the empty-board guard looks for.
# That is exactly why the branding guard cannot be the cookie check.
ICIMS_INTERSTITIAL = ('<html><head><title>iCIMS</title></head>'
                      '<body>Please Enable Cookies to Continue</body></html>')


class _FakeResp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_icims_persistent_cookie_wall_raises_instead_of_reading_as_empty(monkeypatch):
    # One retry IS the cookie test. If the interstitial survives it, the wall did not clear,
    # and _icims_get is the last place that is legible: _icims_cards would find no cards, and
    # the caller's branding guard cannot tell this iCIMS-served page from an empty board.
    calls = []

    class _Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp(ICIMS_INTERSTITIAL)

    monkeypatch.setattr(fetch, "_ICIMS_OPENER", _Opener())
    with pytest.raises(ValueError, match="cookie interstitial persisted"):
        fetch._icims_get("https://careers-sidley.icims.com/jobs/search?pr=0")
    assert len(calls) == 2                       # the retry ran; the SECOND one is the failure


def test_icims_cookie_wall_that_clears_on_the_retry_still_succeeds(monkeypatch):
    # The other side of the same boundary: the retry is not a new failure mode. A tenant whose
    # jar is warm on attempt two reads normally.
    bodies = [ICIMS_INTERSTITIAL, ICIMS_LIST_PAGE]

    class _Opener:
        def open(self, req, timeout=None):
            return _FakeResp(bodies.pop(0))

    monkeypatch.setattr(fetch, "_ICIMS_OPENER", _Opener())
    body = fetch._icims_get("https://careers-sidley.icims.com/jobs/search?pr=0")
    assert "iCIMS_JobCardItem" in body
    assert bodies == []                          # both reads consumed


# Verbatim from a captured healthy search page (2026-08-24, careers-cravath, cookieless
# 200, one message occurrence): the portal ships the cookie message on EVERY page as an
# inert display:none template that client JS un-hides only when cookies truly fail. Probed
# the same day, this markup is byte-identical on all 12 configured tenants and on both
# builds in the fleet (183.4.0 and 186.3.1), so it is not one build's quirk — and the
# 2026-08-20 capture of staffcareers-mcguirewoods already carried it on a healthy 20-card
# page, four days before the blackout. The bare substring detector read it as a persisted
# interstitial and failed all 12 boards at once (last success run 87, every board failed
# from run 89 through run 96 that day).
ICIMS_HIDDEN_COOKIE_TEMPLATE = (
    '<div id="iCIMS_NoCookiesMessage" class="iCIMS_ErrorMsg iCIMS_ErrorMessage '
    'iCIMS_NoCookies" style="display: none">\n'
    '<div class="iCIMS_ErrorMsgTitle">Please Enable Cookies to Continue</div>\n'
    'Please enable cookies in your browser to experience all the personalized features '
    'of this site, including the ability to apply for a job.</div>')


def test_icims_hidden_cookie_template_on_healthy_page_is_not_the_wall(monkeypatch):
    # The 2026-08-24 outage shape: a healthy listings page whose only cookie-message
    # occurrence is the inert hidden template. Must read as content on the FIRST attempt —
    # no retry burned, no ValueError, cards still reachable.
    calls = []

    class _Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp(ICIMS_HIDDEN_COOKIE_TEMPLATE + ICIMS_LIST_PAGE)

    monkeypatch.setattr(fetch, "_ICIMS_OPENER", _Opener())
    body = fetch._icims_get("https://careers-cravath.icims.com/jobs/search?pr=0")
    assert "iCIMS_JobCardItem" in body
    assert len(calls) == 1                       # healthy page: the retry stays unspent


def test_icims_cookie_template_rendered_visible_still_raises(monkeypatch):
    # The template shown instead of hidden — the shape a build would serve if it moved the
    # reveal back to the server. Hypothetical: no such page has been captured. Flipping the
    # value rather than deleting the attribute is presentation, not rigor (measured, both
    # constructions land on the same "one message, nothing explains it" input), but it reads
    # as the page a build would emit. Must still be the wall: loud, never success/0.
    visible = ICIMS_HIDDEN_COOKIE_TEMPLATE.replace('style="display: none"',
                                                   'style="display: block"')
    calls = []

    class _Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp(visible)

    monkeypatch.setattr(fetch, "_ICIMS_OPENER", _Opener())
    with pytest.raises(ValueError, match="cookie interstitial persisted"):
        fetch._icims_get("https://careers-cravath.icims.com/jobs/search?pr=0")
    assert len(calls) == 2                       # the retry ran before the failure


def test_icims_wall_beside_the_hidden_template_still_raises(monkeypatch):
    # The other side of the count boundary, and the likelier wall on these builds: the page
    # keeps the boilerplate hidden template AND carries a second, visible copy of the same
    # component. Two occurrences, one hidden template to explain them -> wall. This is the
    # ONLY case that proves `explained` is SUBTRACTED rather than merely checked for zero:
    # mutate the comparison to `_icims_explained_copies(body) == 0` and every other test in
    # this file still passes.
    wall = (ICIMS_HIDDEN_COOKIE_TEMPLATE
            + '<div class="iCIMS_ErrorMsg"><div class="iCIMS_ErrorMsgTitle">'
              'Please Enable Cookies to Continue</div></div>')
    calls = []

    class _Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp(wall)

    monkeypatch.setattr(fetch, "_ICIMS_OPENER", _Opener())
    with pytest.raises(ValueError, match="cookie interstitial persisted"):
        fetch._icims_get("https://careers-cravath.icims.com/jobs/search?pr=0")
    assert len(calls) == 2


def test_icims_hidden_template_markup_variants_are_not_the_wall():
    # NOT invented external evidence: every variant below is the SAME captured template
    # rewritten the ways HTML lets you write it — attribute order, quoting, a trailing
    # semicolon, a second style property. A byte-exact matcher calls each one a wall and
    # takes all 12 boards dark, which is the outage this detector exists to end, so the
    # tolerance is the behaviour under test.
    open_tag = ('<div id="iCIMS_NoCookiesMessage" class="iCIMS_NoCookies" '
                'style="display: none">')
    for variant in (
        open_tag.replace('style="display: none"', "style='display:none'"),
        open_tag.replace('style="display: none"', 'style="display: none;"'),
        open_tag.replace('style="display: none"', 'style="color:red;display:none"'),
        open_tag.replace('style="display: none"', 'style="display:none"'),
        ('<div style="display: none" class="iCIMS_NoCookies" '
         'id="iCIMS_NoCookiesMessage">'),
        "<div id='iCIMS_NoCookiesMessage' style='display: none'>",
    ):
        page = (variant + '<div class="iCIMS_ErrorMsgTitle">'
                'Please Enable Cookies to Continue</div></div>' + ICIMS_LIST_PAGE)
        assert fetch._icims_interstitial(page) is False, variant

    # The hiding MECHANISM is deliberately not generalized: an unobserved way of hiding the
    # template reads as a wall (loud) rather than as content (silent zero).
    unhidden = ('<div id="iCIMS_NoCookiesMessage" class="iCIMS_NoCookies" hidden>'
                '<div class="iCIMS_ErrorMsgTitle">'
                'Please Enable Cookies to Continue</div></div>')
    assert fetch._icims_interstitial(unhidden + ICIMS_LIST_PAGE) is True


# A real wall: the message rendered for real, with no inert template to account for it.
ICIMS_VISIBLE_WALL = ('<div class="iCIMS_ErrorMsg"><div class="iCIMS_ErrorMsgTitle">'
                      'Please Enable Cookies to Continue</div></div>')


def test_icims_lookalike_divs_cannot_absorb_a_wall():
    # Every decoy here earned a "hidden template" credit in this fix's first draft, and ONE
    # unearned credit is all it takes to absorb a real wall's copy and log success/0 — the
    # loosening that bought tolerance for harmless markup variation also bought this, which
    # is why the tolerant direction has to be pinned too. Adversarial inputs, not a claim
    # about captured markup; the id-prefix pair is the one with a real argument behind it,
    # since this platform names its own parts iCIMS_ErrorMsgTitle / iCIMS_JobsTable and a
    # wrapper/title split is therefore the likely next shape.
    #
    # The wall goes INSIDE the decoy element, and that placement is the whole test. With the
    # wall outside, the decoy is an empty element that earns nothing whether or not the
    # regexes match it — the assertions pass identically against maximally-loosened regexes
    # (measured: 0 of 7 guards pinned) and pin nothing. Inside, a decoy that IS mistaken for
    # a template explains the message it now contains and the wall goes quiet, so each
    # assertion fails on exactly one side of its guard (7 of 7).
    for decoy in (
        '<div id="iCIMS_NoCookiesMessageTitle" style="display:none">',       # id is a PREFIX
        '<div id="iCIMS_NoCookiesMessage_v2" style="display:none">',
        '<div data-id="iCIMS_NoCookiesMessage" style="display:none">',       # not the id attr
        '<div id="iCIMS_NoCookiesMessage" data-style="display:none">',       # not style attr
        '<div id="iCIMS_NoCookiesMessage" style="color:red;--display:none">',  # custom prop
        '<div id="iCIMS_NoCookiesMessage" style="display:none-such">',       # not the value
        '<div id="icims_nocookiesmessage" style="display:none">',            # ids are cased
    ):
        assert fetch._icims_interstitial(decoy + ICIMS_VISIBLE_WALL + "</div>") is True, decoy
        # ...and the same element with the REAL id/style does absorb it — the other side of
        # every guard above, so none of these assertions can pass by accident.
    real = '<div id="iCIMS_NoCookiesMessage" style="display:none">'
    assert fetch._icims_interstitial(real + ICIMS_VISIBLE_WALL + "</div>") is False


def test_icims_template_credit_is_earned_by_a_message_not_by_a_tag():
    # A template div containing no message explains no message. Crediting one per matching
    # TAG let an empty template — or one whose </div> never arrives, so its extent is
    # unknown — hand its credit to a wall standing right beside it.
    empty = '<div id="iCIMS_NoCookiesMessage" style="display:none"></div>'
    unclosed = '<div id="iCIMS_NoCookiesMessage" style="display:none">'
    assert fetch._icims_interstitial(empty + ICIMS_VISIBLE_WALL) is True
    assert fetch._icims_interstitial(unclosed + ICIMS_VISIBLE_WALL) is True
    # The captured template does contain one, and explains exactly that one — the other side
    # of the same boundary, on the same page shape.
    assert fetch._icims_interstitial(ICIMS_HIDDEN_COOKIE_TEMPLATE + ICIMS_LIST_PAGE) is False


def test_icims_page_cap_warns_instead_of_silently_truncating(conn, monkeypatch, capsys):
    # No silent caps, the rule _workday_rows already enforces. A portal that keeps serving
    # full pages of NEW ids past ICIMS_MAX_PAGES must say the tail went unread — otherwise a
    # 500-posting ceiling reads as full coverage on exactly the deep boards this lane targets.
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    counter = [0]

    def endless(url):
        if "/jobs/search" not in url:
            return ICIMS_DETAIL_PAGE
        # Every page is full AND entirely new, so neither break can fire.
        cards = []
        for _ in range(fetch.ICIMS_PAGE):
            counter[0] += 1
            cards.append(ICIMS_CARD_OTHER.replace("6900", str(10000 + counter[0])))
        return '<div class="iCIMS_MainWrapper iCIMS_ListingsPage">' + "".join(cards) + "</div>"

    monkeypatch.setattr(fetch, "_icims_get", endless)
    fetch.fetch_ats(_icims_cfg(), conn)
    err = capsys.readouterr().err
    assert "page cap hit" in err
    assert f"listed {fetch.ICIMS_MAX_PAGES * fetch.ICIMS_PAGE} postings" in err
    assert "the tail was NOT read" in err


def test_icims_card_href_must_be_this_tenants_own_host(conn, monkeypatch):
    # The extracted url is BOTH stored as job_url and fetched for the JD, so a link in scraped
    # page HTML must not be able to choose an outbound target. Both sides: the same card shape
    # on the tenant's host is read, on a foreign host it is not.
    foreign = ICIMS_CARD_AI.replace("staffcareers-mcguirewoods.icims.com", "evil.example.com")
    page = ('<div class="iCIMS_MainWrapper iCIMS_ListingsPage">' + foreign
            + ICIMS_CARD_OTHER + "</div>")
    base = "https://staffcareers-mcguirewoods.icims.com"
    assert [c["id"] for c in fetch._icims_cards(page, base)] == ["6900"]
    assert [c["id"] for c in fetch._icims_cards(ICIMS_LIST_PAGE, base)] == ["6861", "6900"]

    log = []
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    def fake_get(url):
        log.append(url)
        if "/jobs/search" in url:
            return page if "pr=0" in url else ICIMS_EMPTY_PAGE
        return ICIMS_DETAIL_PAGE

    monkeypatch.setattr(fetch, "_icims_get", fake_get)
    assert fetch.fetch_ats(_icims_cfg(), conn) == 0      # the AI card was the only title match
    assert not [u for u in log if "evil.example.com" in u]


def test_icims_off_host_cards_are_counted_not_silently_skipped(conn, monkeypatch, capsys):
    # The host check FILTERS rather than anchoring the pattern, so it can report what it
    # dropped. An anchored regex would be silent, and a tenant whose real cards sit on an
    # unpredicted host would read as an empty board -- the failure _icims_get's interstitial
    # guard just closed, reintroduced through the fix for a different one.
    foreign = ICIMS_CARD_AI.replace("staffcareers-mcguirewoods.icims.com", "evil.example.com")
    page = ('<div class="iCIMS_MainWrapper iCIMS_ListingsPage">' + foreign
            + ICIMS_CARD_OTHER + "</div>")
    fetch._icims_cards(page, "https://staffcareers-mcguirewoods.icims.com")
    err = capsys.readouterr().err
    assert "dropped 1 job card(s)" in err
    assert "evil.example.com" in err


def test_icims_unreadable_cards_raise_instead_of_reading_as_empty(conn, monkeypatch):
    # Every card off-host (a wrong slug-to-host assumption, or an iCIMS skin that moved the
    # markup): cards ARE on the page and none parsed. That is a broken reader, not an empty
    # board -- and the branding guard cannot tell them apart, since this page is iCIMS-served.
    all_foreign = ('<div class="iCIMS_MainWrapper iCIMS_ListingsPage">'
                   + ICIMS_CARD_AI.replace("staffcareers-mcguirewoods.icims.com", "elsewhere.example.com")
                   + "</div>")
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fetch, "_icims_get", lambda url: (
        all_foreign if "/jobs/search" in url else ICIMS_DETAIL_PAGE))
    broken = fetch.fetch_ats(_icims_cfg(), conn)
    # The board FAILED; it did not quietly succeed with zero postings. Asserted on the
    # summary's unit outcome, not the inserted count -- both spellings insert 0, which is
    # exactly why "0 rows" cannot be the signal here.
    assert (broken.successes, broken.failures) == (0, 1)

    # ...and a genuinely empty board still reads as SUCCESS: the guard has to separate the
    # two, not just refuse the quiet one.
    monkeypatch.setattr(fetch, "_icims_get", lambda url: (
        ICIMS_EMPTY_PAGE if "/jobs/search" in url else ICIMS_DETAIL_PAGE))
    empty = fetch.fetch_ats(_icims_cfg(), conn)
    assert (empty.successes, empty.failures) == (1, 0)


def test_workday_page_cap_warns_instead_of_silently_truncating(conn, monkeypatch, capsys):
    # No-silent-caps: a board larger than the page bound must SAY the tail went unread
    # (White & Case measured 3,500 roles on 2026-08-20 — the case this exists for).
    monkeypatch.setattr(fetch, "WORKDAY_MAX_PAGES", 1)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    full = {"total": 999, "jobPostings": [
        {"title": f"Role {i}", "externalPath": f"/job/X/Role-{i}",
         "locationsText": "X", "bulletFields": []} for i in range(fetch.WORKDAY_PAGE)]}
    monkeypatch.setattr(fetch, "_workday_post", lambda url, payload: full)
    monkeypatch.setattr(fetch, "_ats_get", lambda url: {"jobPostingInfo": {}})

    fetch.fetch_ats(_wd_cfg(title_any=["nothing matches"]), conn)
    err = capsys.readouterr().err
    assert "page cap hit" in err and "of 999" in err

    # Boundary's other side: a board that fits inside the cap stays silent.
    monkeypatch.setattr(fetch, "_workday_post", lambda url, payload: WD_LIST_PAGE
                        if payload["offset"] == 0 else {"total": 3, "jobPostings": []})
    monkeypatch.setattr(fetch, "WORKDAY_MAX_PAGES", 15)
    monkeypatch.setattr(fetch, "_ats_get", lambda url: WD_DETAIL[
        "/job/" + url.split("/job/", 1)[1]])
    conn.execute("DELETE FROM jobs")
    conn.commit()
    fetch.fetch_ats(_wd_cfg(), conn)
    assert "page cap hit" not in capsys.readouterr().err
