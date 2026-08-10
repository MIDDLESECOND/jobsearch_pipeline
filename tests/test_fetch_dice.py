"""The Dice source's pure core: flight-chunk reassembly, the search-payload object scanner,
the detail-page JD extraction, the employment filter, and fetch_dice itself with the network
layer (_dice_get) monkeypatched — fixtures mirror the real probed page shapes (2026-08-09:
escaped Next.js flight chunks, jobList data array, entity-bearing strings), so no test ever
touches the network."""

import json
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import fetch
from conftest import make_job
from fetch import _dice_description, _dice_flight, _dice_search_page

# ---------------------------------------------------------------- page fixtures
# A Dice page ships its payload as script chunks of self.__next_f.push([1,"<JSON string>"]);
# the DECODED chunks concatenate into the flight text. Builders below re-create that shape,
# including a payload split mid-object across chunks (observed on real pages).


def _flight_html(payload_text, split_at=None):
    parts = ([payload_text] if split_at is None
             else [payload_text[:split_at], payload_text[split_at:]])
    return "".join(
        "<script>self.__next_f.push([1,%s])</script>" % json.dumps(p) for p in parts
    )


def _job(guid, *, title="Solutions Architect", company="Acme Corp",
         city="New York, New York, USA", posted="2026-08-08T00:32:44Z",
         employment="Full-time", direct_apply=False):
    kind = "direct-apply" if direct_apply else "job-detail"
    return {
        "guid": guid,
        "detailsPageUrl": f"https://www.dice.com/{kind}/{guid}",
        "companyName": company,
        "title": title,
        "jobLocation": {"displayName": city},
        "postedDate": posted,
        "employmentType": employment,
    }


def _search_payload(jobs, total_results=None, total_pages=1):
    # A decoy widget AFTER the job list carries its own (site-wide) totals — the parser
    # must keep the first pair following the jobList anchor, exactly like the real pages.
    total = len(jobs) if total_results is None else total_results
    return ('12:["$","$L2e",null,{"jobList":{"data":%s,'
            '"totalResults":%d,"totalPages":%d}}]\n'
            '13:{"siteWideWidget":{"totalResults":6033,"totalPages":202}}'
            % (json.dumps(jobs), total, total_pages))


_DETAIL_JD = ("<p>Own the pipeline &amp; ship it</p>"
              "<ul><li>SQL</li><li>Python</li></ul>")


def _detail_html(jd=_DETAIL_JD, *, carousel=(), company_profile=None):
    """A detail page. `carousel`/`company_profile` add the OTHER description-bearing objects
    real pages carry — deliberately longer than the JD, so a reader that took "the longest
    description on the page" would return one of them instead of this job's."""
    parts = ['"jobDetail":%s' % json.dumps({"description": jd}),
             '"meta":{"description":"short teaser"}']
    if carousel:
        parts.append('"similarJobs":%s'
                     % json.dumps([{"description": c} for c in carousel]))
    if company_profile:
        parts.append('"companyProfile":%s'
                     % json.dumps({"description": company_profile}))
    return _flight_html("2e:{%s}" % ",".join(parts))


class _Router:
    """Fake _dice_get: serves the search page for /jobs? URLs and per-URL detail pages,
    recording every request so tests can assert what was (not) fetched."""

    def __init__(self, search_html, details=None):
        self.search_html = search_html
        self.details = details or {}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if "dice.com/jobs?" in url:
            return self.search_html
        return self.details[url]  # unexpected detail URL → KeyError, loud


def _dice_cfg(dice_block: str | list = "solutions architect", **settings_over):
    d = {"results_pages": 1, "max_days_old": 7, "delay_between_calls": 0}
    d.update(settings_over)
    return {
        "settings": {"max_description_chars": 12000, "dice": d},
        "searches": [{"name": "sa", "term": "x", "tier": "primary",
                      "dice": dice_block}],
    }


# ------------------------------------------------------------------ pure parsing

def test_dice_flight_reassembles_split_chunks():
    payload = _search_payload([_job("aaa")])
    # Split INSIDE the job object: only correct chunk-joining can parse it.
    html_text = _flight_html(payload, split_at=payload.index("companyName") + 5)
    jobs, total, pages = _dice_search_page(html_text)
    assert [j["url"] for j in jobs] == ["https://www.dice.com/job-detail/aaa"]
    # First totals after the jobList anchor win — never the decoy widget's 6033/202.
    assert total == 1 and pages == 1
    assert _dice_flight(html_text) == payload


def test_dice_direct_apply_url_keeps_identity_but_details_via_job_detail():
    jobs, _, _ = _dice_search_page(_flight_html(_search_payload([
        _job("dd", direct_apply=True)])))
    # The stored identity is what search results keep returning (the direct-apply link);
    # the JD fetch goes to job-detail/<guid>, which serves it for every posting kind.
    assert jobs[0]["url"] == "https://www.dice.com/direct-apply/dd"
    assert jobs[0]["detail_url"] == "https://www.dice.com/job-detail/dd"


def test_dice_search_page_fields_and_entities():
    jobs, _, _ = _dice_search_page(_flight_html(_search_payload([
        _job("aaa", company="Acme &amp; Partners", title="Solutions Architect {AI}"),
    ])))
    j = jobs[0]
    # Entities decode before the normalized key is derived; braces in titles survive the
    # object scanner (it is string-aware, not a bare brace counter).
    assert j["company"] == "Acme & Partners"
    assert j["title"] == "Solutions Architect {AI}"
    assert j["location"] == "New York, New York, USA"
    assert j["date_posted"] == "2026-08-08T00:32:44Z"
    assert j["employment"] == "Full-time"


def test_dice_detail_reads_the_anchored_job_detail_not_the_longest_string():
    """The JD comes from the jobDetail object, NOT from "the longest description on the
    page". A detail page also carries a meta description, the company profile, and a
    similar-jobs carousel with its own descriptions — longest-wins demonstrably returns one
    of those. A substituted JD is undetectable downstream: it reaches the paid eval, the
    verdict caches onto the chain, and applying freezes it as immutable packet evidence."""
    out = _dice_description(
        _detail_html(carousel=["Unrelated recommended role. " * 40],
                     company_profile="Company boilerplate about our mission. " * 40),
        12000)
    assert "Own the pipeline & ship it" in out
    assert "SQL" in out and "Python" in out
    assert "<" not in out
    assert "short teaser" not in out
    assert "Unrelated recommended role" not in out
    assert "Company boilerplate" not in out


def test_dice_detail_without_the_anchor_yields_no_jd():
    """A changed page shape must produce no JD rather than a guess: the caller then skips
    the insert and retries the still-unseen URL next run, instead of storing another job's
    text as this role's evidence."""
    assert _dice_description(
        _flight_html('2e:{"otherShape":{"description":"%s"}}' % ("x" * 900)), 12000) == ""


def test_dice_detail_cap_respected():
    assert len(_dice_description(_detail_html("<p>%s</p>" % ("x" * 500)), 100)) == 100


# -------------------------------------------------------------------- fetch_dice

def test_fetch_dice_inserts_new_filters_c2c_skips_known(conn, monkeypatch):
    known_url = "https://www.dice.com/job-detail/known"
    make_job(conn, job_url=known_url, title="Old Role", company="Seen Co",
             status="evaluated", source="dice")
    router = _Router(
        _flight_html(_search_payload([
            _job("aaa", direct_apply=True),  # JD must still come via job-detail/<guid>
            _job("c2c", company="Bodyshop LLC", employment="Contract, Third Party"),
            _job("known"),
        ])),
        details={"https://www.dice.com/job-detail/aaa": _detail_html()},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 1

    row = conn.execute("SELECT * FROM jobs WHERE job_url LIKE '%/aaa'").fetchone()
    assert row["job_url"] == "https://www.dice.com/direct-apply/aaa"
    assert row["status"] == "new"
    assert row["source"] == "dice"
    assert row["search_name"] == "sa" and row["tier"] == "primary"
    assert row["salary_min"] is None and row["salary_max"] is None
    assert "Own the pipeline & ship it" in row["description"]
    # The precise UTC timestamp lands local-naive to the second (the _ats_date/first_seen
    # convention) — asserted TZ-agnostically so the test passes on any machine.
    expected = (datetime.fromisoformat("2026-08-08T00:32:44+00:00")
                .astimezone().replace(tzinfo=None).isoformat(timespec="seconds"))
    assert row["date_posted"] == expected

    # The C2C row never entered; the known URL cost no detail fetch (one search page +
    # exactly one detail request in total).
    assert conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE company LIKE 'Bodyshop%'").fetchone()["c"] == 0
    assert [u for u in router.calls if "job-detail" in u] == [
        "https://www.dice.com/job-detail/aaa"]

    # Idempotency: a re-crawl fetches no details and inserts nothing.
    router.calls.clear()
    assert fetch.fetch_dice(_dice_cfg(), conn) == 0
    assert [u for u in router.calls if "job-detail" in u] == []


def test_fetch_dice_links_repost_chain(conn, monkeypatch):
    # Same company+location+exact title already in the DB under a LinkedIn URL → the Dice
    # row must join that chain via the shared posting-store path, not start a fresh one.
    make_job(conn, job_url="https://linkedin.example/1", title="Solutions Architect",
             company="Acme Corp", location="New York, New York, USA", status="evaluated")
    router = _Router(
        _flight_html(_search_payload([_job("aaa")])),
        details={"https://www.dice.com/job-detail/aaa": _detail_html()},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    assert fetch.fetch_dice(_dice_cfg(), conn) == 1
    row = conn.execute("SELECT repost_of FROM jobs WHERE job_url LIKE '%/aaa'").fetchone()
    assert row["repost_of"] == "https://linkedin.example/1"


def test_fetch_dice_missing_jd_skips_row_not_query(conn, monkeypatch):
    router = _Router(
        _flight_html(_search_payload([_job("aaa"), _job("bbb")])),
        details={
            # aaa's detail page has no parseable description; bbb's is fine.
            "https://www.dice.com/job-detail/aaa": "<html>nothing here</html>",
            "https://www.dice.com/job-detail/bbb": _detail_html(),
        },
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 1  # bbb inserted; aaa skipped (retries as still-unseen next run)
    assert summary.successes == 1 and summary.failures == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM jobs WHERE job_url LIKE '%/aaa'").fetchone()["c"] == 0


def test_fetch_dice_noop_without_config(conn, capsys):
    cfg = {"settings": {"max_description_chars": 12000},
           "searches": [{"name": "sa", "term": "x"}]}
    assert fetch.fetch_dice(cfg, conn) == 0
    assert "skipping Dice source" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_dice_empty_exclude_admits_everything(conn, monkeypatch):
    router = _Router(
        _flight_html(_search_payload([
            _job("c2c", employment="Contract, Third Party")])),
        details={"https://www.dice.com/job-detail/c2c": _detail_html()},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_dice(_dice_cfg(employment_exclude=[]), conn) == 1


def test_fetch_dice_all_exclude_patterns_broken_refuses(conn, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert fetch.fetch_dice(_dice_cfg(employment_exclude=["re:(third"]), conn) == 0
    assert "employment_exclude" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_dice_unusable_phrase_block_fails_that_unit(conn, monkeypatch, capsys):
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: (_ for _ in ()).throw(AssertionError("no fetch")))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(dice_block=[{"phrase": "oops"}]), conn)
    assert summary == 0
    assert summary.failures == 1 and summary.successes == 0
    assert "no usable phrases" in capsys.readouterr().err


def test_fetch_dice_invalid_window_falls_back(conn, monkeypatch, capsys):
    router = _Router(_flight_html(_search_payload([])))
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_dice(_dice_cfg(max_days_old=5), conn) == 0
    assert "must be one of" in capsys.readouterr().err
    assert all("SEVEN" in u for u in router.calls if "dice.com/jobs?" in u)


# ------------------------------------------------- broken pages must not read as healthy

def test_fetch_dice_unparseable_search_page_fails_instead_of_reading_empty(
        conn, monkeypatch, capsys):
    """A layout change or a 200-status block page yields no jobList envelope. Recorded as a
    success it is byte-identical to a healthy zero-result sweep — the source would print
    "0 returned" forever, stay green in the health table, and still satisfy the cooldown's
    "at least one target succeeded" stamp."""
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: "<html><body>Checking your browser…</body></html>")
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 0
    assert summary.failures == 1 and summary.successes == 0
    assert "no jobList envelope" in capsys.readouterr().err


def test_dice_search_page_tolerates_envelope_key_order():
    """Everything is read from inside the sliced jobList object, so Dice adding a key ahead
    of "data" is survivable. Anchoring on `"jobList":{"data":[` made key order load-bearing:
    one reordering stopped every row parsing."""
    reordered = _flight_html(
        '12:["$","$L2e",null,{"jobList":{"totalResults":1,"data":%s,"totalPages":1}}]'
        % json.dumps([_job("aaa")]))
    jobs, total, pages = _dice_search_page(reordered)
    assert [j["url"] for j in jobs] == ["https://www.dice.com/job-detail/aaa"]
    assert total == 1 and pages == 1


def test_dice_search_page_totals_come_from_inside_the_envelope():
    """jobList's OWN totals, not "the first totalResults after the anchor". A page whose
    jobList carries no count would otherwise inherit an unrelated downstream widget's
    site-wide figure — which makes a page that parsed ZERO rows report a healthy total."""
    payload = ('12:["$","$L2e",null,{"jobList":{"data":[]}}]\n'
               '13:{"siteWideWidget":{"totalResults":6033,"totalPages":202}}')
    jobs, total, pages = _dice_search_page(_flight_html(payload))
    assert jobs == [] and total == 0 and pages is None


def test_fetch_dice_envelope_claiming_unparsed_rows_is_a_failure(conn, monkeypatch):
    """An envelope that says it holds results while none of them parse is a shape change,
    not an empty window — and it must not read as a healthy zero-result sweep. The row
    objects here are deliberately UNPARSEABLE JSON (the dropped-chunk signature): an object
    that parses but merely lacks fields would trip a different guard, leaving this one
    untested."""
    payload = ('12:["$","$L2e",null,{"jobList":{"data":[{"title":}],'
               '"totalResults":30,"totalPages":1}}]')
    monkeypatch.setattr(fetch, "_dice_get", lambda url: _flight_html(payload))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary.failures == 1 and summary.successes == 0


def test_fetch_dice_genuine_zero_results_stays_a_success(conn, monkeypatch):
    """The discriminator for the two tests above: a real "nothing posted this window" page
    still ships the envelope (data [], totalResults 0), and must stay a success."""
    router = _Router(_flight_html(_search_payload([])))
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 0 and summary.successes == 1 and summary.failures == 0


def test_fetch_dice_all_details_missing_jd_fails_the_query(conn, monkeypatch, capsys):
    """One dead detail page is ordinary attrition. An all-fail sweep is a changed
    detail-page shape — and left as a success it looks exactly like a healthy re-crawl
    where everything was already known (returned/eligible high, inserted 0)."""
    router = _Router(
        _flight_html(_search_payload([_job(f"g{i}") for i in range(3)])),
        details={f"https://www.dice.com/job-detail/g{i}": "<html>no anchor</html>"
                 for i in range(3)},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 0 and summary.failures == 1 and summary.successes == 0
    assert "yielded no job description" in capsys.readouterr().err


def test_fetch_dice_detail_fetch_exception_spares_the_other_rows(
        conn, monkeypatch, capsys):
    """A dead detail page (404, reset) must not kill the query's other rows."""
    def get(url):
        if url.endswith("/aaa"):
            raise OSError("connection reset")
        if "dice.com/jobs?" in url:
            return _flight_html(_search_payload([_job("aaa"), _job("bbb")]))
        return _detail_html()

    monkeypatch.setattr(fetch, "_dice_get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 1 and summary.successes == 1 and summary.failures == 0
    assert "detail fetch failed" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 1


def test_fetch_dice_row_error_rolls_back_the_whole_query(conn, monkeypatch, capsys):
    """Two insertable rows and the SECOND insert raises: the first is already inserted
    (uncommitted) and must NOT persist. Asserting only "the query failed" would still pass
    with the rollback deleted, since the next query's commit would ship the orphan row."""
    router = _Router(
        _flight_html(_search_payload([_job("aaa"), _job("bbb")])),
        details={"https://www.dice.com/job-detail/aaa": _detail_html(),
                 "https://www.dice.com/job-detail/bbb": _detail_html()},
    )
    real = fetch._insert_posting

    def boom(conn_, **kw):
        if kw["url"].endswith("/bbb"):
            raise RuntimeError("boom")
        return real(conn_, **kw)

    monkeypatch.setattr(fetch, "_insert_posting", boom)
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary.failures == 1
    assert "FAILED: boom" in capsys.readouterr().err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_dice_never_requests_inside_a_write_transaction(conn, monkeypatch):
    """Dice fetches a detail page PER ROW, so inserting as it goes would hold the WAL
    writer lock across every remaining request and politeness sleep — minutes on a first
    crawl, against core.py's 30s busy_timeout. The local UI and an overlapping scheduled
    run both write to this database."""
    router = _Router(
        _flight_html(_search_payload([_job(f"g{i}") for i in range(4)])),
        details={f"https://www.dice.com/job-detail/g{i}": _detail_html()
                 for i in range(4)},
    )
    in_transaction = []

    def probe(url):
        in_transaction.append(conn.in_transaction)
        return router(url)

    monkeypatch.setattr(fetch, "_dice_get", probe)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    assert fetch.fetch_dice(_dice_cfg(), conn) == 4
    assert len(in_transaction) == 5           # 1 search page + 4 detail pages
    assert in_transaction == [False] * 5


def test_fetch_dice_bad_later_page_keeps_the_pages_already_paid_for(conn, monkeypatch,
                                                                    capsys):
    """Page 1 parsed and its detail pages are already bought. If page 2 comes back a block
    page, failing the whole query throws that work away — and because the rows are never
    inserted they stay unseen, so the next run buys them again, forever. results_pages
    defaults to 2, so this is the shipped configuration."""
    page1 = _flight_html(_search_payload([_job("a"), _job("b")],
                                         total_results=4, total_pages=2))

    def get(url):
        if "dice.com/jobs?" not in url:
            return _detail_html()
        return page1 if "page=1" in url else "<html>Checking your browser…</html>"

    monkeypatch.setattr(fetch, "_dice_get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    summary = fetch.fetch_dice(_dice_cfg(results_pages=2), conn)
    assert summary == 2 and summary.successes == 1 and summary.failures == 0
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 2
    assert "page 2" in capsys.readouterr().err     # the truncation is still disclosed


def test_fetch_dice_rows_without_a_url_fail_the_query(conn, monkeypatch):
    """detailsPageUrl AND guid renamed: every row is skipped before it counts as eligible,
    so as a success this is indistinguishable from "the filter excluded everything"."""
    jobs = [{"title": "Role", "companyName": "Co", "postedDate": "2026-08-09T12:00:00Z",
             "employmentType": "Full-time"} for _ in range(3)]
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: _flight_html(_search_payload(jobs)))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary.failures == 1 and summary.successes == 0


def test_fetch_dice_rows_without_employment_type_fail_the_query(conn, monkeypatch):
    """employmentType is this source's flood guard. A rename silently admits the whole C2C
    staffing population — into the DB and into the paid eval — so it refuses, exactly like
    the config-side version of the same filter emptying out."""
    jobs = [_job(f"g{i}") for i in range(3)]
    for j in jobs:
        del j["employmentType"]
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: _flight_html(_search_payload(jobs)))
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary.failures == 1 and summary.successes == 0
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0


def test_fetch_dice_dead_detail_links_do_not_fail_the_query(conn, monkeypatch):
    """Three dead links are ordinary attrition, NOT a broken parser: a delisted posting
    stays in Dice's posted window and is deliberately never inserted, so failing on network
    errors would re-fire every run for a week and blame the parser for a timeout."""
    def get(url):
        if "dice.com/jobs?" in url:
            return _flight_html(_search_payload([_job(f"g{i}") for i in range(3)]))
        raise OSError("connection reset")

    monkeypatch.setattr(fetch, "_dice_get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(), conn)
    assert summary == 0 and summary.successes == 1 and summary.failures == 0


def test_dice_detail_ambiguous_anchor_yields_no_jd():
    """Two jobDetail objects make "the first match" a coin flip, and picking wrong stores
    another role's JD as this role's evidence. Refuse instead."""
    payload = ('2e:{"recommendations":[{"jobDetail":{"description":"WRONG ROLE"}}],'
               '"jobDetail":{"description":"the real one"}}')
    assert _dice_description(_flight_html(payload), 12000) == ""


# ------------------------------------------------------------- query shape and knobs

def test_fetch_dice_walks_pages_and_stops_at_total_pages(conn, monkeypatch):
    """Page depth inside the window is the completeness bound, so results_pages must
    actually paginate — and totalPages must stop it (a third request would KeyError)."""
    pages = {
        1: _flight_html(_search_payload([_job("p1")], total_results=2, total_pages=2)),
        2: _flight_html(_search_payload([_job("p2")], total_results=2, total_pages=2)),
    }
    calls = []

    def get(url):
        calls.append(url)
        if "dice.com/jobs?" in url:
            return pages[int(parse_qs(urlsplit(url).query)["page"][0])]
        return _detail_html()

    monkeypatch.setattr(fetch, "_dice_get", get)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)

    assert fetch.fetch_dice(_dice_cfg(results_pages=5), conn) == 2
    assert len([u for u in calls if "dice.com/jobs?" in u]) == 2


def test_fetch_dice_sends_the_phrase_quoted(conn, monkeypatch):
    """Quoting IS the Dice query dialect (it cannot parse LinkedIn boolean syntax).
    Unquoted, the phrase degrades to loose keywords and a far broader relevance set flows
    into the paid eval."""
    router = _Router(_flight_html(_search_payload([])))
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    fetch.fetch_dice(_dice_cfg(), conn)
    assert parse_qs(urlsplit(router.calls[0]).query)["q"] == ['"solutions architect"']


def test_fetch_dice_one_bad_phrase_spares_the_others(conn, monkeypatch, capsys):
    """_dice_phrases drops unusable entries individually — one typo'd phrase must not take
    the search's remaining phrases down with it."""
    router = _Router(
        _flight_html(_search_payload([_job("aaa")])),
        details={"https://www.dice.com/job-detail/aaa": _detail_html()},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    summary = fetch.fetch_dice(_dice_cfg(dice_block=["", "solutions architect"]), conn)
    assert summary == 1 and summary.successes == 1 and summary.failures == 0
    assert "ignoring phrase" in capsys.readouterr().err


def test_fetch_dice_results_pages_out_of_range_falls_back(conn, monkeypatch, capsys):
    """results_pages: -1 made range(1, 0) empty — zero HTTP requests, yet recorded as a
    healthy success that satisfied the cooldown's "some target succeeded" stamp."""
    router = _Router(_flight_html(_search_payload([])))
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    assert fetch.fetch_dice(_dice_cfg(results_pages=-1), conn) == 0
    assert "results_pages must be a whole number 1..20" in capsys.readouterr().err
    assert len([u for u in router.calls if "dice.com/jobs?" in u]) == 1


def test_fetch_dice_non_numeric_knobs_warn_instead_of_silently_defaulting(
        conn, monkeypatch, capsys):
    """`_num` alone turns a YAML bool into 1.0 and 2.9 into 2, both silently — so
    `results_pages: yes` would quietly fetch ONE page while still recording success."""
    router = _Router(_flight_html(_search_payload([])))
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda *_: None)
    for bad in (True, "two", 2.9):
        fetch.fetch_dice(_dice_cfg(results_pages=bad), conn)
        assert "results_pages must be a whole number" in capsys.readouterr().err
    for bad in ("seven", False):
        fetch.fetch_dice(_dice_cfg(max_days_old=bad), conn)
        assert "max_days_old must be" in capsys.readouterr().err
    fetch.fetch_dice(_dice_cfg(delay_between_calls="fast"), conn)
    assert "delay_between_calls must be a number" in capsys.readouterr().err


def test_fetch_dice_negative_delay_falls_back(conn, monkeypatch, capsys):
    """time.sleep raises on a negative delay — mid-query AND again from inside the except
    handler, escaping fetch_dice entirely and skipping every remaining search."""
    slept = []
    router = _Router(
        _flight_html(_search_payload([_job("aaa")])),
        details={"https://www.dice.com/job-detail/aaa": _detail_html()},
    )
    monkeypatch.setattr(fetch, "_dice_get", router)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))
    # A row is present so both sleep sites (per detail fetch, end of page) actually fire.
    assert fetch.fetch_dice(_dice_cfg(delay_between_calls=-1), conn) == 1
    assert "delay_between_calls must be a number >= 0" in capsys.readouterr().err
    assert slept and all(s >= 0 for s in slept)


def test_fetch_dice_blank_employment_exclude_refuses(conn, monkeypatch, capsys):
    """`employment_exclude: ""` expresses restrict-intent but sanitizes to nothing. A
    truthiness check missed it (an empty string is falsy), so the guard passed and the
    whole C2C staffing population went straight into the DB and the paid eval."""
    monkeypatch.setattr(fetch, "_dice_get",
                        lambda url: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert fetch.fetch_dice(_dice_cfg(employment_exclude=""), conn) == 0
    err = capsys.readouterr().err
    assert "every employment_exclude pattern was unusable" in err
    # The sanitizer notice names Dice — a scheduled log must not blame the ATS boards.
    assert "[dice] ignoring employment_exclude" in err
    assert conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0
