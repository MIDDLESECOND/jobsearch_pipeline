"""The ATS source's pure core: HTML→text, date normalization, the per-board payload
extractors, the title/location filters, and fetch_ats itself with the network layer
(_ats_get) monkeypatched — payload fixtures mirror the real probed API shapes, so no
test ever touches the network."""

import re

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

def test_ats_date_iso_sliced():
    assert _ats_date("2026-06-01T08:00:00-04:00") == "2026-06-01"


def test_ats_date_epoch_ms():
    # Mid-day UTC so any local timezone lands on the same calendar day.
    assert re.fullmatch(r"2026-06-1[56]", _ats_date(1781524800000))


def test_ats_date_unparseable():
    assert _ats_date(None) == ""
    assert _ats_date("junk") == ""
    assert _ats_date(True) == ""  # bool passes isinstance(int) — must not become 1969/1970
    assert _ats_date("Posted 3 weeks ago") == ""  # >=10 chars but not a date — no blind slice


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
    assert r["date_posted"] == "2026-06-01"
    assert r["description"] == "Build & ship dashboards\nSQL\nPython"
    assert r["remote"] is False


def test_lever_rows_full_description():
    rows = _ats_rows_lever(LEVER_PAYLOAD, "Another Co")
    assert len(rows) == 1
    r = rows[0]
    assert r["company"] == "Another Co"  # no company in Lever payloads → config name
    assert r["locations"] == ["New York, NY", "Toronto, ON"]  # allLocations, not just primary
    assert r["remote"] is False  # workplaceType "hybrid" — no location-substring heuristic
    assert re.fullmatch(r"2026-06-1[56]", r["date_posted"])
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
    assert r["date_posted"] == "2026-06-11"
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
    cfg = _ats_cfg(companies=[{"slug": "x", "board": "workday"}])
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
