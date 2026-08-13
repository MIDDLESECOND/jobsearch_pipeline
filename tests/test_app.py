"""Flask triage-UI endpoints — the behavior the coming refactors must preserve.

These pin the HTTP contract of app.py: legacy view filtering, bounded/filterable page envelopes,
Action Center queues, the backlog's chain-decided-relisting drop, decision propagation + the
`affected` list the client uses to update sibling cards, the dupe link/undo/conflict paths, the
clip payload, and the origin guard. Same synthetic-DB discipline as the rest of the suite (temp
file via the shared schema builder + make_job) — never the real jobs.db, never the network.
"""

import sqlite3
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest

import app as webapp
import chain
import core
import materials
from conftest import make_job

TODAY_SEEN = "2026-06-01T09:00:00"  # make_job's first_seen date, used as the today-view date
CAP = 60  # small max_description_chars so the truncated flag is testable


@pytest.fixture
def db_path(tmp_path):
    """Schema-initialized temp DB; returns its path (connections are made per request)."""
    path = str(tmp_path / "test.db")
    core.get_db({"settings": {"db_path": path}}).close()
    return path


@pytest.fixture
def seed(db_path):
    """A long-lived connection for seeding rows with make_job."""
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def client(db_path, monkeypatch):
    """Test client with config + DB pointed at the temp file. Routes open (and close) their
    own connection per request, exactly like production."""
    cfg = {"settings": {"db_path": db_path, "max_description_chars": CAP,
                        "feedback_project_url": ""},
           "searches": [{"name": "AI leadership", "tier": "primary",
                         "min_salary": 150000}]}

    def fresh_conn(_cfg=None):
        # The REAL production opener (row factory, busy timeout), so the endpoint tests
        # exercise the same connection configuration the app ships.
        return core.connect_db(cfg)

    monkeypatch.setattr(webapp, "load_config", lambda: cfg)
    monkeypatch.setattr(webapp, "connect_db", fresh_conn)
    # The Werkzeug test client addresses the app as plain "localhost" — allow it alongside
    # the production loopback:port entries (the spoofed-Host test overrides per request).
    monkeypatch.setattr(webapp, "ALLOWED_HOSTS", set(webapp.ALLOWED_HOSTS) | {"localhost"})
    return webapp.app.test_client()


def _post(client, path, body, origin=None):
    headers = {"Origin": origin} if origin else {}
    return client.post(path, json=body, headers=headers)


def test_homepage_exposes_action_center_filters_and_pager(client):
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert 'data-view="action"' in html
    assert 'data-view="funnel"' in html
    assert 'id="query"' in html
    assert 'id="pageLabel"' in html
    assert 'View all' in html
    assert 'filtersEl.classList.add("queue-only")' in html
    assert "Attach resume" in html and "Copy prep context" in html
    assert "Add contact" in html and "Draft outreach" in html
    assert "review before sending" in html
    assert 'id="contactDialog"' in html and 'id="contactForm"' in html
    assert "Add next action" in html and 'id="taskDialog"' in html
    assert "expected_version: task.version" in html
    assert "promptField" not in html
    assert "Basic checks passed" in html
    assert "ATS ✓" not in html
    assert 'input.value = ""' in html
    assert "Possible duplicates" in html
    assert "Not the same role" in html
    assert "Review ignored" in html
    assert "Schedule interview" in html
    assert 'id="interviewDialog"' in html and 'id="interviewForm"' in html
    assert "Save role note" in html
    assert 'href="/api/export/roles.csv"' in html and "Export CSV" in html
    assert "Star role" in html and "Unstar" in html
    assert "Activity ▸" in html and 'fetch("/api/timeline?job_url="' in html
    assert 'data-view="health">Health &amp; yield' in html
    assert 'data-view="prep">Story &amp; answer bank' in html
    assert 'id="prepEntryDialog"' in html and 'id="prepLinkDialog"' in html
    assert 'url = "/api/prep-items?include_archived=1"' in html
    assert 'fetch("/api/prep-links?job_url="' in html
    assert "prepLinkTarget !== target" in html
    assert 'id="jdDiffDialog"' in html and "Compare JD versions" in html
    assert 'fetch("/api/jd-versions?job_url="' in html
    assert 'url = "/api/jd-diff?" + params.toString()' in html
    assert "jdDiffLeft.value !== leftId || jdDiffRight.value !== rightId" in html
    assert "Selections changed; compare again." in html
    assert 'url = "/api/health?" + params' in html
    assert "latestRun.attempts_truncated" in html
    assert "target attempts; older details were omitted" in html
    assert 'id="intakeOpen"' in html and 'id="intakeDialog"' in html
    assert 'id="intakeForm"' in html and 'postJSON("/api/intake"' in html
    assert '<option value="AI leadership">AI leadership</option>' in html
    assert '<option value="manual">Manual intake</option>' in html


# ------------------------------------------------------------- /api/jd-diff

def test_jd_version_api_is_lazy_opaque_and_returns_only_explicit_diff_text(client, seed):
    make_job(
        seed, job_url="jd-old", title="Old", description="<script>old private JD</script>",
    )
    make_job(
        seed, job_url="jd-new", title="New", repost_of="jd-old",
        first_seen="2026-06-02T00:00:00", description="new private JD",
    )
    seed.execute(
        """INSERT INTO job_contacts
           (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
           VALUES ('jd-old','jd-old','Secret Contact',NULL,'other','secret@example.test',
                   NULL,NULL,'2026-06-01T00:00:00')"""
    )
    seed.commit()

    listed = client.get("/api/jd-versions?job_url=jd-old")
    assert listed.status_code == 200
    payload = listed.get_json()
    assert payload["default_left"] and payload["default_right"]
    assert len(payload["versions"]) == 2
    serialized = listed.get_data(as_text=True)
    assert "jd-old" not in serialized and "jd-new" not in serialized
    assert "old private JD" not in serialized and "Secret Contact" not in serialized
    assert set(payload["versions"][0]) == {
        "id", "kind", "label", "source", "observed_at", "availability",
        "completeness", "title", "location", "possibly_truncated",
    }

    diff = client.get(
        "/api/jd-diff", query_string={
            "job_url": "jd-old", "left": payload["default_left"],
            "right": payload["default_right"], "context": 2,
        },
    )
    assert diff.status_code == 200
    body = diff.get_json()["comparison"]
    assert "<script>old private JD</script>" in repr(body["hunks"])
    assert body["complete"] is True
    assert "Secret Contact" not in diff.get_data(as_text=True)


def test_jd_diff_api_rejects_cross_chain_ids_and_resource_overflow(client, seed):
    make_job(seed, job_url="left-old", title="Left old", description="Left")
    make_job(seed, job_url="left-new", title="Left new", repost_of="left-old",
             first_seen="2026-06-02T00:00:00", description="Changed")
    make_job(seed, job_url="other", title="Other", description="Other")
    left = client.get("/api/jd-versions?job_url=left-old").get_json()
    other = client.get("/api/jd-versions?job_url=other").get_json()["versions"][0]["id"]
    refused = client.get("/api/jd-diff", query_string={
        "job_url": "left-old", "left": left["default_left"], "right": other,
    })
    assert refused.status_code == 400
    assert "current role chain" in refused.get_json()["message"]
    assert client.get("/api/jd-diff?job_url=left-old&context=11").status_code == 400

    seed.execute("UPDATE jobs SET description=? WHERE job_url='left-old'", ("x" * 50001,))
    seed.commit()
    versions = client.get("/api/jd-versions?job_url=left-old").get_json()["versions"]
    oversized = next(item for item in versions if item["availability"] == "too_large")
    available = next(item for item in versions if item["availability"] == "available")
    too_large = client.get("/api/jd-diff", query_string={
        "job_url": "left-old", "left": oversized["id"], "right": available["id"],
    })
    assert too_large.status_code == 422
    assert "too large" in too_large.get_json()["message"]


# ----------------------------------------------------------- /api/prep-items

def test_prep_library_api_requires_confirmation_and_role_link_before_context(
        client, seed):
    row = make_job(
        seed, job_url="prep-role", app_status="applied", status_date="2026-08-05",
    )
    created = _post(client, "/api/prep-items", {
        "action": "create", "kind": "story", "title": "Launch recovery",
        "prompt": "Tell me about a difficult launch.",
        "response": "I reduced scope and shipped the critical path.",
        "tags": ["delivery"],
    })
    assert created.status_code == 201
    draft = created.get_json()["entry"]
    assert draft["status"] == "draft" and draft["version"] == 1

    before = client.get("/api/prep?job_url=prep-role").get_json()["text"]
    assert "Launch recovery" not in before
    confirmed = _post(client, "/api/prep-items", {
        "action": "confirm", "entry_id": draft["id"], "expected_version": 1,
    }).get_json()["entry"]
    choices = client.get("/api/prep-links?job_url=prep-role").get_json()["entries"]
    choice = next(item for item in choices if item["id"] == draft["id"])
    assert choice["status"] == "confirmed" and choice["link_linked"] is False
    assert set(choice) == {
        "id", "kind", "title", "status", "link_linked", "link_revision", "link_root",
    }

    linked = _post(client, "/api/prep-links", {
        "job_url": row["job_url"], "entry_id": draft["id"], "linked": True,
        "expected_linked": False, "expected_revision": choice["link_revision"],
        "expected_root": choice["link_root"],
    })
    assert linked.status_code == 200 and linked.get_json()["linked"] is True
    after = client.get("/api/prep?job_url=prep-role").get_json()["text"]
    assert "Launch recovery" in after

    edited = _post(client, "/api/prep-items", {
        "action": "update", "entry_id": draft["id"],
        "expected_version": confirmed["version"], "kind": "story",
        "title": "Launch recovery", "prompt": None,
        "response": "Edited details require review.", "tags": [],
    }).get_json()["entry"]
    assert edited["status"] == "draft"
    assert "Launch recovery" not in client.get(
        "/api/prep?job_url=prep-role"
    ).get_json()["text"]


def test_prep_library_api_is_lazy_bounded_and_origin_guarded(client, seed):
    make_job(seed, job_url="prep-role")
    assert client.get("/api/prep-items?limit=0").status_code == 400
    assert client.get("/api/prep-links").status_code == 400
    assert client.get("/api/prep-links?job_url=missing").status_code == 404
    assert client.post("/api/prep-items", json=[]).status_code == 400
    assert client.post("/api/prep-links", json=[]).status_code == 400
    assert _post(client, "/api/prep-links", {
        "job_url": "prep-role", "entry_id": True, "linked": True,
        "expected_linked": False, "expected_revision": 0,
        "expected_root": "prep-role",
    }).status_code == 400
    refused = _post(
        client, "/api/prep-items",
        {"kind": "story", "title": "x", "response": "y", "tags": []},
        origin="http://evil.example",
    )
    assert refused.status_code == 403

    jobs = client.get("/api/jobs?view=today&page=1&page_size=50&date=2026-06-01")
    serialized = jobs.get_data(as_text=True)
    assert "prep_entries" not in serialized and "Launch recovery" not in serialized


# ---------------------------------------------------------------- /api/intake

def test_manual_intake_api_creates_a_new_unevaluated_source_row(client, db_path):
    response = _post(client, "/api/intake", {
        "job_url": "https://careers.example.test/roles/42",
        "title": "AI Platform Lead",
        "company": "Example Co",
        "location": "Remote",
        "date_posted": "2026-08-06",
        "salary_min": 180000,
        "salary_max": 220000,
        "description": "Own the AI platform and its reliable delivery.",
        "search_name": "AI leadership",
    })

    assert response.status_code == 201
    payload = response.get_json()
    assert payload == {
        "ok": True,
        "job_url": "https://careers.example.test/roles/42",
        "repost_of": None,
        "message": "Role added; the next pipeline run will process it through current rules.",
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (payload["job_url"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["source"] == "manual"
    assert row["status"] == "new"
    assert row["verdict"] is None and row["eval_json"] is None
    assert row["description"] == "Own the AI platform and its reliable delivery."


def test_manual_intake_api_rejects_duplicates_bad_json_and_cross_origin(
    client, seed
):
    make_job(seed, job_url="https://careers.example.test/roles/42",
             title="Existing", description="keep")
    duplicate = _post(client, "/api/intake", {
        "job_url": "https://careers.example.test/roles/42",
        "title": "Replacement",
        "company": "Example Co",
        "search_name": "AI leadership",
    })
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.get_json()["message"]
    assert seed.execute(
        "SELECT title FROM jobs WHERE job_url='https://careers.example.test/roles/42'"
    ).fetchone()[0] == "Existing"

    malformed = client.post("/api/intake", json=["not", "an", "object"])
    assert malformed.status_code == 400
    invalid = _post(client, "/api/intake", {
        "job_url": "javascript:alert(1)", "title": "Bad", "company": "Bad",
        "search_name": "AI leadership",
    })
    assert invalid.status_code == 400
    assert "http" in invalid.get_json()["message"]
    refused = _post(
        client,
        "/api/intake",
        {"job_url": "https://example.test/2", "title": "Role", "company": "Co",
         "search_name": "AI leadership"},
        origin="https://evil.example",
    )
    assert refused.status_code == 403


def test_funnel_api_returns_chain_scoped_snapshot_and_validates_range(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="root", company="Funnel Co", app_status="applied",
             status_date=today, channel="direct")
    make_job(seed, job_url="relist", company="Funnel Co", repost_of="root",
             app_status="applied", status_date=today, channel="direct")
    got = client.get("/api/funnel?days=30").get_json()
    assert got["range"]["days"] == 30
    assert got["stages"][0]["id"] == "applied"
    assert got["stages"][0]["count"] == 1
    assert got["by_channel"][0]["id"] == "direct"

    assert client.get("/api/funnel?days=all").status_code == 200
    assert client.get("/api/funnel?days=0").status_code == 400
    assert client.get("/api/funnel?days=recent").status_code == 400


def test_funnel_config_error_is_not_misreported_as_bad_query(client, monkeypatch):
    monkeypatch.setitem(webapp.app.config, "PROPAGATE_EXCEPTIONS", False)

    def bad_config():
        raise ValueError("invalid local config")

    monkeypatch.setattr(webapp, "load_config", bad_config)
    response = client.get("/api/funnel?days=90")
    assert response.status_code == 500


def test_role_csv_export_is_downloadable_and_chain_deduped(client, seed):
    make_job(seed, job_url="root", app_status="applied", status_date=date.today().isoformat())
    make_job(seed, job_url="relist", repost_of="root", app_status="applied",
             status_date=date.today().isoformat())

    response = client.get("/api/export/roles.csv")

    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert "attachment;" in response.headers["Content-Disposition"]
    assert response.get_data(as_text=True).count("\n") == 2


def test_star_api_updates_chain_cards_and_action_queue(client, seed):
    make_job(seed, job_url="root")
    make_job(seed, job_url="relist", repost_of="root")

    response = _post(client, "/api/star", {
        "job_url": "relist", "starred": True, "expected_starred": False,
        "expected_star_version": 0,
    })
    assert response.status_code == 200 and response.get_json()["starred"] is True
    starred_version = response.get_json()["star_version"]
    card = client.get("/api/jobs?view=today&date=2026-06-01").get_json()[0]
    assert card["starred"] is True and card["starred_at"]
    queue = client.get("/api/actions/starred_roles?page=1&page_size=20").get_json()
    assert queue["total"] == 1 and queue["items"][0]["job_url"] == "root"

    stale = _post(client, "/api/star", {
        "job_url": "root", "starred": False, "expected_starred": False,
        "expected_star_version": 0,
    })
    assert stale.status_code == 409 and "refresh" in stale.get_json()["message"]
    cleared = _post(client, "/api/star", {
        "job_url": "root", "starred": False, "expected_starred": True,
        "expected_star_version": starred_version,
    })
    assert cleared.status_code == 200 and cleared.get_json()["starred"] is False


def test_star_api_validates_origin_and_boolean_state(client, seed):
    make_job(seed, job_url="root")
    assert _post(client, "/api/star", ["bad"]).status_code == 400
    assert _post(client, "/api/star", {
        "job_url": "root", "starred": True, "expected_starred": False,
    }).status_code == 400
    assert _post(client, "/api/star", {
        "job_url": "root", "starred": "yes", "expected_starred": False,
        "expected_star_version": 0,
    }).status_code == 400
    assert _post(client, "/api/star", {
        "job_url": "root", "starred": True, "expected_starred": False,
        "expected_star_version": 0,
    }, origin="http://evil.example").status_code == 403


# ------------------------------------------------------------------ /api/jobs

def test_today_view_returns_rows_seen_that_day(client, seed):
    make_job(seed, job_url="u1", first_seen=TODAY_SEEN)
    make_job(seed, job_url="u2", first_seen="2026-05-20T09:00:00")
    got = client.get("/api/jobs?view=today&date=2026-06-01").get_json()
    assert [j["job_url"] for j in got] == ["u1"]
    j = got[0]
    # The flattened fields the cards render.
    assert j["band"] == "acceptable" and j["bucket_label"] and "age_label" in j


def test_job_payload_carries_chain_verdict(client, seed):
    # The exact row class chain_verdict exists for: an eval-skipped relisting
    # (status='repost_evaluated', own verdict/fit_score NULL by design) must expose the
    # chain's PASS through /api/jobs so the UI can badge it.
    make_job(seed, job_url="canon", verdict="PASS", first_seen="2026-05-20T09:00:00")
    make_job(seed, job_url="relist", repost_of="canon", status="repost_evaluated",
             verdict=None, fit_score=None, bucket=None, first_seen=TODAY_SEEN)
    got = client.get("/api/jobs?view=today&date=2026-06-01").get_json()
    j = next(x for x in got if x["job_url"] == "relist")
    assert j["verdict"] is None             # own verdict stays NULL — never copied
    assert j["band"] is None                # NULL-score path renders without crashing
    assert j["chain_verdict"] == "PASS"     # the chain's most favorable, read through
    assert j["chain_fit_score"] == 12       # the winning member's fit (badge + sort fallback)


def test_today_sort_promotes_eval_skipped_rows_but_not_other_fit_null_rows(client, seed):
    # The chain-fit sort fallback is gated on the two eval-skip statuses: a PASS/14-chain
    # relisting sorts with fit 14 (above a scored fit-11 card), while a salary_filtered or
    # needs_manual relisting of the same chain keeps fit 0 (bottom band) — a rejected or
    # description-less row must never outrank genuinely scored cards.
    make_job(seed, job_url="chain-c", company="SortCo", verdict="PASS", fit_score=14,
             first_seen="2026-05-20T09:00:00")
    make_job(seed, job_url="skip-r", company="SortCo", title="Skipped Role",
             repost_of="chain-c", status="repost_evaluated", verdict=None, fit_score=None,
             bucket=None, first_seen=TODAY_SEEN)
    make_job(seed, job_url="sal-r", company="SortCo", title="Salary Role",
             repost_of="chain-c", status="salary_filtered", verdict=None, fit_score=None,
             bucket=None, first_seen=TODAY_SEEN)
    make_job(seed, job_url="scored11", company="Mid Co", verdict="PASS", fit_score=11,
             first_seen=TODAY_SEEN)
    got = client.get("/api/jobs?view=today&date=2026-06-01").get_json()
    order = [j["job_url"] for j in got]
    assert order.index("skip-r") < order.index("scored11")   # promoted by chain fit
    assert order.index("sal-r") > order.index("scored11")    # NOT promoted


def test_chain_verdict_takes_most_favorable_member(client, seed):
    # Noisy repeat evals: a GATE_FAIL sample on one member never outranks a PASS on another.
    make_job(seed, job_url="canon2", verdict="PASS", first_seen="2026-05-20T09:00:00")
    make_job(seed, job_url="relist2", repost_of="canon2", status="evaluated",
             verdict="GATE_FAIL", first_seen=TODAY_SEEN)
    got = client.get("/api/jobs?view=today&date=2026-06-01").get_json()
    j = next(x for x in got if x["job_url"] == "relist2")
    assert j["verdict"] == "GATE_FAIL"      # the row's own (noisy) sample, unchanged
    assert j["chain_verdict"] == "PASS"


def test_backlog_only_undecided_gates_passed(client, seed):
    make_job(seed, job_url="pass1")                                   # PASS, undecided -> in
    make_job(seed, job_url="rec1", verdict="RECRUITER_ONLY")          # in
    make_job(seed, job_url="fail1", verdict="GATE_FAIL", fit_score=None, bucket=None)
    make_job(seed, job_url="done1", app_status="applied", status_date="2026-06-02")
    make_job(seed, job_url="rej1", filter_source="manual", filter_gate="other")
    make_job(seed, job_url="new1", status="new", verdict=None, fit_score=None)
    urls = {j["job_url"] for j in client.get("/api/jobs?view=backlog").get_json()}
    assert urls == {"pass1", "rec1"}


def test_backlog_drops_relisting_of_decided_chain(client, seed):
    make_job(seed, job_url="canon", company="Chain Co", app_status="applied",
             status_date="2026-06-02")
    make_job(seed, job_url="relist", company="Chain Co", repost_of="canon")
    urls = {j["job_url"] for j in client.get("/api/jobs?view=backlog").get_json()}
    assert "relist" not in urls
    # ...but an undecided chain's relisting stays, carrying the chain fields.
    make_job(seed, job_url="canon2", company="Other Co")
    make_job(seed, job_url="relist2", company="Other Co", repost_of="canon2")
    got = {j["job_url"]: j for j in client.get("/api/jobs?view=backlog").get_json()}
    assert got["relist2"]["is_repost"] is True
    assert got["relist2"]["chain_app_status"] is None


def test_applied_view_orders_by_status_date(client, seed):
    make_job(seed, job_url="a_old", app_status="applied", status_date="2026-05-01")
    make_job(seed, job_url="a_new", app_status="applied", status_date="2026-06-01")
    make_job(seed, job_url="p1", app_status="passed", status_date="2026-06-01")
    got = [j["job_url"] for j in client.get("/api/jobs?view=applied").get_json()]
    assert got == ["a_new", "a_old"]


def test_paged_jobs_api_returns_envelope_and_filters(client, seed):
    make_job(seed, job_url="u1", title="AI Architect", fit_score=17, source="ashby")
    make_job(seed, job_url="u2", title="Data Analyst", fit_score=12, source="linkedin")
    make_job(seed, job_url="u3", title="AI Engineer", fit_score=16, source="ashby")
    got = client.get(
        "/api/jobs?view=backlog&page=1&page_size=1&q=ai&source=ashby&min_score=14"
    ).get_json()
    assert set(got) == {"items", "total", "page", "page_size", "pages"}
    assert got["total"] == 2 and got["page_size"] == 1 and got["pages"] == 2
    assert got["items"][0]["job_url"] == "u1"


def test_paged_jobs_api_rejects_bad_query_values(client, seed):
    assert client.get("/api/jobs?view=unknown&page=1").status_code == 400
    assert client.get("/api/jobs?view=backlog&page=zero").status_code == 400
    assert client.get("/api/jobs?view=backlog&page=1&page_size=5000").status_code == 400
    assert client.get("/api/jobs?view=backlog&page=1&min_score=high").status_code == 400


def test_action_center_api_returns_card_payloads(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="cold", fit_score=17, first_seen=f"{today}T09:00:00")
    make_job(seed, job_url="route", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen=f"{today}T10:00:00")
    got = client.get("/api/actions").get_json()
    by_id = {s["id"]: s for s in got["sections"]}
    assert by_id["fresh_strong"]["items"][0]["job_url"] == "cold"
    assert by_id["recruiter_route"]["items"][0]["job_url"] == "route"
    assert "age_label" in by_id["fresh_strong"]["items"][0]


def test_action_section_api_is_paged(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="cold-a", fit_score=17, first_seen=f"{today}T09:00:00")
    make_job(seed, job_url="cold-b", fit_score=16, first_seen=f"{today}T08:00:00")
    got = client.get("/api/actions/fresh_strong?page=1&page_size=1").get_json()
    assert got["title"] == "Fresh strong matches"
    assert got["total"] == 2 and got["pages"] == 2
    assert len(got["items"]) == 1


def test_recruiter_route_exits_after_contact_recorded(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="route-me", verdict="RECRUITER_ONLY", fit_score=15,
             first_seen=f"{today}T09:00:00")

    before = client.get("/api/actions/recruiter_route").get_json()
    assert [i["job_url"] for i in before["items"]] == ["route-me"]

    response = _post(client, "/api/contacts", {
        "job_url": "route-me", "name": "Rex Cruiter", "kind": "recruiter",
    })
    assert response.status_code == 200 and response.get_json()["ok"] is True

    after = client.get("/api/actions/recruiter_route").get_json()
    assert after["total"] == 0 and after["items"] == []
    # Leaving the queue is not a hidden decision: the chain stays in the backlog.
    backlog = client.get("/api/jobs?view=backlog&page=1&page_size=50").get_json()
    assert "route-me" in [i["job_url"] for i in backlog["items"]]


def test_interview_schedule_api_card_queue_and_ics(client, seed):
    today = date.today()
    row = make_job(seed, job_url="applied", title="AI Lead", company="Acme",
                   app_status="applied", status_date=today.isoformat())
    starts = datetime.now().astimezone() + timedelta(days=2)
    response = _post(client, "/api/interviews", {
        "job_url": row["job_url"], "action": "add", "title": "Technical round",
        "starts_at": starts.isoformat(), "duration_minutes": 60, "mode": "video",
        "meeting_url": "https://meet.example.com/room", "location": "", "note": "Prepare",
    })
    assert response.status_code == 200
    item = response.get_json()["interview"]
    assert item["version"] == 0

    card = client.get("/api/jobs?view=applied&page=1&page_size=20").get_json()["items"][0]
    assert card["interviews"][0]["id"] == item["id"]
    queue = client.get("/api/actions/upcoming_interviews?page=1&page_size=20").get_json()
    assert queue["total"] == 1 and queue["items"][0]["job_url"] == "applied"

    calendar = client.get(
        f"/api/interviews/{item['id']}.ics?job_url=applied"
    )
    assert calendar.status_code == 200
    assert calendar.mimetype == "text/calendar"
    assert "BEGIN:VCALENDAR" in calendar.get_data(as_text=True)

    updated = _post(client, "/api/interviews", {
        "job_url": "applied", "action": "update", "interview_id": item["id"],
        "expected_version": item["version"], "title": "Final round",
        "starts_at": (starts + timedelta(days=1)).isoformat(),
        "duration_minutes": 75, "mode": "onsite", "location": "HQ",
        "meeting_url": "", "note": "Bring ID",
    }).get_json()["interview"]
    assert updated["version"] == 1 and updated["title"] == "Final round"

    cancelled = _post(client, "/api/interviews", {
        "job_url": "applied", "action": "cancel", "interview_id": item["id"],
        "expected_version": updated["version"],
    }).get_json()["interview"]
    assert cancelled["status"] == "cancelled"
    assert client.get("/api/actions/upcoming_interviews?page=1&page_size=20").get_json()["total"] == 0


def test_interview_api_validates_body_origin_and_applied_state(client, seed):
    make_job(seed, job_url="passed", app_status="passed", status_date=date.today().isoformat())
    assert _post(client, "/api/interviews", ["not", "an", "object"]).status_code == 400
    assert _post(client, "/api/interviews", {
        "job_url": "passed", "action": "add", "title": "Round",
        "starts_at": "2026-08-10T15:00:00+00:00", "duration_minutes": 60,
        "mode": "video",
    }, origin="http://evil.example").status_code == 403
    refused = _post(client, "/api/interviews", {
        "job_url": "passed", "action": "add", "title": "Round",
        "starts_at": "2026-08-10T15:00:00+00:00", "duration_minutes": 60,
        "mode": "video",
    })
    assert refused.status_code == 400 and "applied chain" in refused.get_json()["message"]


def test_possible_duplicates_api_compares_confirms_dismisses_and_restores(
        client, seed, monkeypatch):
    today = date.today().isoformat()
    make_job(seed, job_url="li", company="Acme Inc", title="Sr Data Analyst",
             source="linkedin", location="New York, NY",
             description="LinkedIn version of the job", first_seen=f"{today}T09:00:00")
    make_job(seed, job_url="adz", company="Acme", title="Senior Data Analyst",
             source="adzuna", location="Grand Central, Manhattan",
             description="Adzuna version of the job", first_seen=f"{today}T10:00:00")

    def refuse_full_card_serialization(*args, **kwargs):
        raise AssertionError("duplicate suggestions must not load full role cards")

    monkeypatch.setattr(webapp, "rows_to_dicts", refuse_full_card_serialization)

    section = client.get(
        "/api/actions/possible_duplicates?page=1&page_size=20"
    ).get_json()
    assert section["total"] == 1
    pair = section["items"][0]
    expected_side_keys = {
        "job_url", "title", "company", "location", "source",
        "first_seen", "date_posted", "description_preview",
    }
    assert set(pair["left"]) == expected_side_keys
    assert set(pair["right"]) == expected_side_keys
    assert {pair["left"]["job_url"], pair["right"]["job_url"]} == {"li", "adz"}
    assert pair["left"]["description_preview"]
    assert pair["same_location"] is False

    dismissed = _post(client, "/api/dupe-candidate", {
        "left_url": "li", "right_url": "adz", "dismissed": True,
        "expected_roots": ["adz", "li"], "expected_dismissed": False,
        "expected_review_version": 0,
    })
    assert dismissed.status_code == 200 and dismissed.get_json()["dismissed"] is True
    active = client.get("/api/actions/possible_duplicates?page=1&page_size=20").get_json()
    ignored = client.get(
        "/api/actions/possible_duplicates?page=1&page_size=20&dismissed=1"
    ).get_json()
    assert active["total"] == 0 and active["dismissed_total"] == 1
    assert ignored["total"] == 1 and ignored["items"][0]["dismissed_at"]

    stale_restore = _post(client, "/api/dupe-candidate", {
        "left_url": "li", "right_url": "adz", "dismissed": False,
        "expected_roots": ["adz", "li"], "expected_dismissed": False,
        "expected_review_version": 0,
    })
    assert stale_restore.status_code == 409
    assert "review changed" in stale_restore.get_json()["message"]
    assert client.get(
        "/api/actions/possible_duplicates?page=1&page_size=20&dismissed=1"
    ).get_json()["total"] == 1

    restored = _post(client, "/api/dupe-candidate", {
        "left_url": "li", "right_url": "adz", "dismissed": False,
        "expected_roots": ["adz", "li"], "expected_dismissed": True,
        "expected_review_version": 1,
    })
    assert restored.status_code == 200 and restored.get_json()["dismissed"] is False
    active_again = client.get(
        "/api/actions/possible_duplicates?page=1&page_size=20"
    ).get_json()["items"][0]
    assert active_again["review_version"] == 2
    assert active_again["dismissed_at"] is None

    confirmed = _post(client, "/api/dupe", {
        "job_url": "adz", "of": "li", "expected_roots": ["adz", "li"],
    })
    assert confirmed.get_json()["ok"] is True
    assert client.get(
        "/api/actions/possible_duplicates?page=1&page_size=20"
    ).get_json()["total"] == 0


def test_dupe_candidate_confirmation_refuses_changed_preview_roots(client, seed):
    make_job(seed, job_url="left", company="Acme", source="linkedin")
    make_job(seed, job_url="right", company="Acme", source="adzuna")
    make_job(seed, job_url="new-root", company="Acme", source="ashby",
             first_seen="2026-05-01T09:00:00")
    seed.execute("UPDATE jobs SET repost_of='new-root' WHERE job_url='right'")
    seed.commit()

    response = _post(client, "/api/dupe", {
        "job_url": "right", "of": "left",
        "expected_roots": ["left", "right"],
    }).get_json()

    assert response["ok"] is False
    assert "changed since preview" in response["message"]
    assert seed.execute(
        "SELECT repost_of FROM jobs WHERE job_url='left'"
    ).fetchone()["repost_of"] is None


def test_dupe_candidate_confirmation_cannot_override_newer_dismissal(client, seed):
    make_job(seed, job_url="left", company="Acme", title="Analyst",
             source="linkedin", first_seen=date.today().isoformat() + "T09:00:00")
    make_job(seed, job_url="right", company="Acme", title="Analyst",
             source="adzuna", first_seen=date.today().isoformat() + "T10:00:00")
    assert _post(client, "/api/dupe-candidate", {
        "left_url": "left", "right_url": "right", "dismissed": True,
        "expected_roots": ["left", "right"], "expected_dismissed": False,
        "expected_review_version": 0,
    }).get_json()["ok"] is True

    response = _post(client, "/api/dupe", {
        "job_url": "right", "of": "left",
        "expected_roots": ["left", "right"],
    }).get_json()

    assert response["ok"] is False
    assert "reviewed as different roles" in response["message"]
    assert seed.execute(
        "SELECT repost_of FROM jobs WHERE job_url='right'"
    ).fetchone()["repost_of"] is None


def test_dupe_candidate_mutation_validates_body_and_origin(client, seed):
    malformed = _post(client, "/api/dupe-candidate", ["not", "an", "object"])
    assert malformed.status_code == 400
    assert malformed.is_json and "object" in malformed.get_json()["message"]
    assert _post(client, "/api/dupe-candidate", {
        "left_url": "a", "right_url": "b", "dismissed": True,
    }).status_code == 400
    assert _post(client, "/api/dupe-candidate", {
        "left_url": "a", "right_url": "b", "dismissed": "yes",
        "expected_roots": ["a", "b"], "expected_dismissed": False,
        "expected_review_version": 0,
    }).status_code == 400
    assert _post(client, "/api/dupe-candidate", {
        "left_url": "a", "right_url": "b", "dismissed": True,
        "expected_roots": ["a", "b"], "expected_dismissed": False,
        "expected_review_version": 0,
    }, origin="http://evil.example").status_code == 403


def test_followup_sent_api_advances_queue_without_setting_outcome(client, seed):
    applied = (date.today() - timedelta(days=14)).isoformat()
    make_job(seed, job_url="due", app_status="applied", status_date=applied)
    before = client.get("/api/actions/followups_due?page=1&page_size=20").get_json()
    assert [j["job_url"] for j in before["items"]] == ["due"]

    recorded = _post(client, "/api/event", {"job_url": "due", "type": "followup_sent"})
    assert recorded.get_json()["ok"] is True
    assert recorded.get_json()["outcome_status"] is None
    row = seed.execute(
        "SELECT event_type FROM app_events WHERE job_url='due'"
    ).fetchone()
    assert row["event_type"] == "followup_sent"

    after = client.get("/api/actions/followups_due?page=1&page_size=20").get_json()
    assert after["items"] == [] and after["total"] == 0


# -------------------------------------------------------------- /api/decision

def test_decision_applied_propagates_across_chain(client, seed):
    make_job(seed, job_url="c1", company="Chain Co", description="older canonical JD")
    make_job(seed, job_url="r1", company="Chain Co", repost_of="c1",
             description="current relisting JD")
    resp = _post(client, "/api/decision", {"job_url": "r1", "action": "applied"}).get_json()
    assert resp["ok"] is True
    assert set(resp["affected"]) == {"c1", "r1"}
    assert resp["materials"]["jd_snapshot"] is not None
    rows = {r["job_url"]: r for r in seed.execute("SELECT * FROM jobs").fetchall()}
    assert rows["c1"]["app_status"] == "applied" and rows["r1"]["app_status"] == "applied"
    assert seed.execute(
        "SELECT COUNT(*) FROM application_materials WHERE kind='jd_snapshot'"
    ).fetchone()[0] == 1
    prep = client.get("/api/prep?job_url=r1").get_json()["text"]
    assert "Posting applied through: r1" in prep
    assert "current relisting JD" in prep and "older canonical JD" not in prep


def test_decision_undo_app_clears_chain(client, seed):
    make_job(seed, job_url="c1", app_status="applied", status_date="2026-06-02")
    resp = _post(client, "/api/decision", {"job_url": "c1", "action": "undo_app"}).get_json()
    assert resp["ok"] is True
    row = seed.execute("SELECT app_status, status_date FROM jobs").fetchone()
    assert row["app_status"] is None and row["status_date"] is None


def test_decision_reject_lifts_new_row_out_of_eval(client, seed):
    make_job(seed, job_url="n1", status="new", verdict=None, fit_score=None, bucket=None)
    resp = _post(client, "/api/decision",
                 {"job_url": "n1", "action": "reject", "gate": "work_auth"}).get_json()
    assert resp["ok"] is True
    row = seed.execute("SELECT status, filter_source, filter_gate FROM jobs").fetchone()
    assert row["status"] == "rule_filtered"
    assert row["filter_source"] == "manual" and row["filter_gate"] == "work_auth"


def test_decision_undo_reject_clears_manual_only(client, seed):
    make_job(seed, job_url="m1", company="Chain Co", filter_source="manual",
             filter_gate="other", filter_date="2026-06-02")
    make_job(seed, job_url="ruley", company="Chain Co", repost_of="m1",
             filter_source="rule:clearance", filter_gate="work_auth")
    resp = _post(client, "/api/decision", {"job_url": "m1", "action": "undo_reject"}).get_json()
    assert resp["ok"] is True
    rows = {r["job_url"]: r for r in seed.execute("SELECT * FROM jobs").fetchall()}
    assert rows["m1"]["filter_source"] is None
    assert rows["ruley"]["filter_source"] == "rule:clearance"  # rule attribution survives


def test_decision_expired_marks_chain_and_writes_marker(client, seed):
    make_job(seed, job_url="c1", company="Chain Co")
    make_job(seed, job_url="r1", company="Chain Co", repost_of="c1")
    resp = _post(client, "/api/decision", {"job_url": "r1", "action": "expired"}).get_json()
    assert resp["ok"] is True
    assert set(resp["affected"]) == {"c1", "r1"}
    assert set(resp["exempt"]) == {"c1", "r1"}  # chain was undecided → whole chain exempt
    rows = {r["job_url"]: r for r in seed.execute("SELECT * FROM jobs").fetchall()}
    assert rows["c1"]["app_status"] == "passed" and rows["r1"]["app_status"] == "passed"
    events = seed.execute("SELECT job_url, event_type, note FROM app_events").fetchall()
    assert [(e["job_url"], e["event_type"], e["note"]) for e in events] == \
        [("c1", "note", chain.EXPIRED_NOTE)]
    # undo_expired through the same endpoint reverses both halves.
    resp = _post(client, "/api/decision", {"job_url": "r1", "action": "undo_expired"}).get_json()
    assert resp["ok"] is True
    rows = {r["job_url"]: r for r in seed.execute("SELECT * FROM jobs").fetchall()}
    assert rows["c1"]["app_status"] is None and rows["r1"]["app_status"] is None
    assert seed.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0


def test_decision_expired_refused_on_applied_chain(client, seed):
    make_job(seed, job_url="c1", app_status="applied", status_date="2026-06-01")
    resp = _post(client, "/api/decision", {"job_url": "c1", "action": "expired"}).get_json()
    assert resp["ok"] is False and "applied" in resp["message"]
    assert seed.execute("SELECT app_status FROM jobs").fetchone()["app_status"] == "applied"
    assert seed.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0


def test_decision_bad_request(client, seed):
    assert _post(client, "/api/decision", {"action": "applied"}).status_code == 400
    assert _post(client, "/api/decision",
                 {"job_url": "x", "action": "explode"}).status_code == 400


def test_decision_unknown_url_reports_failure(client, seed):
    resp = _post(client, "/api/decision", {"job_url": "nope", "action": "applied"})
    assert resp.get_json()["ok"] is False


def test_cross_origin_post_refused(client, seed):
    make_job(seed, job_url="c1")
    resp = _post(client, "/api/decision", {"job_url": "c1", "action": "applied"},
                 origin="http://evil.example")
    assert resp.status_code == 403
    assert seed.execute("SELECT app_status FROM jobs").fetchone()["app_status"] is None


def test_unrecognized_host_refused(client, seed):
    # DNS rebinding sends the attacker's domain as Host (and Origin — which would then
    # "match" host_url); the Host pin refuses it before any route runs.
    make_job(seed, job_url="c1")
    resp = client.post("/api/decision", json={"job_url": "c1", "action": "applied"},
                       base_url="http://evil.example")
    assert resp.status_code == 403
    assert seed.execute("SELECT app_status FROM jobs").fetchone()["app_status"] is None


def test_decision_applied_with_resume_lands_chainwide(client, seed):
    make_job(seed, job_url="c1", company="Chain Co")
    make_job(seed, job_url="r1", company="Chain Co", repost_of="c1")
    resp = _post(client, "/api/decision",
                 {"job_url": "r1", "action": "applied", "resume": "variant-B"}).get_json()
    assert resp["ok"] is True
    got = {r["job_url"]: r["resume_variant"]
           for r in seed.execute("SELECT job_url, resume_variant FROM jobs")}
    assert got == {"c1": "variant-B", "r1": "variant-B"}
    # set_resume edits it after the fact through the same endpoint.
    resp = _post(client, "/api/decision",
                 {"job_url": "c1", "action": "set_resume", "resume": "variant-C"}).get_json()
    assert resp["ok"] is True
    row = seed.execute("SELECT resume_variant FROM jobs WHERE job_url='r1'").fetchone()
    assert row["resume_variant"] == "variant-C"


# --------------------------------------------------------------- /api/materials

def test_material_upload_packet_download_and_prep_context(client, seed):
    make_job(seed, job_url="c1", title="AI PM", company="Acme",
             description="Own the exact production AI roadmap.",
             app_status="applied", status_date="2026-08-01")
    # Existing applied rows can predate the feature; explicitly applying again backfills the
    # frozen JD without duplicating the decision semantics.
    _post(client, "/api/decision", {"job_url": "c1", "action": "applied"})
    payload = (b"Actual submitted resume actual@example.com 212-555-0100\n"
               + b"Production systems evidence. " * 10)
    response = client.post(
        "/api/materials",
        data={"job_url": "c1", "kind": "resume",
              "file": (BytesIO(payload), "actual-resume.txt")},
        content_type="multipart/form-data",
    )
    got = response.get_json()
    assert response.status_code == 200 and got["ok"] is True
    assert got["item"]["ats_status"] == "ok"
    assert got["materials"]["resume"]["name"] == "actual-resume.txt"

    card = client.get("/api/jobs?view=applied").get_json()[0]
    assert card["materials"]["jd_snapshot"] is not None
    assert card["materials"]["resume"]["sha256"] == got["item"]["sha256"]
    download = client.get(
        f"/api/materials/{got['item']['id']}/download?job_url=c1"
    )
    assert download.status_code == 200 and download.data == payload
    download.close()

    _post(client, "/api/event",
          {"job_url": "c1", "type": "interview", "note": "prepare roadmap story"})
    prep = client.get("/api/prep?job_url=c1").get_json()
    assert prep["ok"] is True
    assert prep["partial"] is False
    assert "Own the exact production AI roadmap." in prep["text"]
    assert "Actual submitted resume" in prep["text"]
    assert "prepare roadmap story" in prep["text"]

    stored_path = seed.execute(
        "SELECT stored_path FROM material_objects WHERE sha256=?",
        (got["item"]["sha256"],),
    ).fetchone()[0]
    material_cfg = {"settings": {
        "db_path": seed.execute("PRAGMA database_list").fetchone()[2],
    }}
    (materials.material_root(material_cfg) / stored_path).unlink()
    card = client.get("/api/jobs?view=applied").get_json()[0]
    assert card["materials"]["resume"]["storage_status"] == "missing"
    partial = client.get("/api/prep?job_url=c1").get_json()
    assert partial["partial"] is True
    assert any("resume file is missing" in warning for warning in partial["warnings"])


def test_material_upload_requires_applied_chain_and_same_origin(client, seed):
    make_job(seed, job_url="c1")
    data = {"job_url": "c1", "kind": "resume",
            "file": (BytesIO(b"text"), "resume.txt")}
    response = client.post("/api/materials", data=data, content_type="multipart/form-data")
    assert response.status_code == 400 and "applied" in response.get_json()["message"]

    response = client.post(
        "/api/materials",
        data={"job_url": "c1", "kind": "resume",
              "file": (BytesIO(b"text"), "resume.txt")},
        content_type="multipart/form-data", headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------- /api/contacts

def test_contact_crud_card_projection_and_outreach_brief(client, seed):
    make_job(seed, job_url="c1", title="AI PM", company="Acme",
             description="Own production AI evaluation.", app_status="applied",
             status_date="2026-08-01")
    make_job(seed, job_url="r1", title="AI Product Lead", company="Acme",
             description="Current relisting JD.", repost_of="c1", app_status="applied",
             status_date="2026-08-01")
    _post(client, "/api/decision", {"job_url": "r1", "action": "applied"})
    response = _post(client, "/api/contacts", {
        "job_url": "r1", "name": "Alex Rivera", "role": "Senior Recruiter",
        "kind": "recruiter", "email": "alex@example.com",
        "profile_url": "https://www.linkedin.com/in/alex", "note": "Met at meetup",
    })
    got = response.get_json()
    assert response.status_code == 200 and got["ok"] is True
    assert got["contact"]["interaction_url"] == "r1"

    listed = client.get("/api/contacts?job_url=c1").get_json()["contacts"]
    assert [c["name"] for c in listed] == ["Alex Rivera"]
    cards = client.get("/api/jobs?view=applied").get_json()
    assert all(card["contacts"][0]["id"] == got["contact"]["id"] for card in cards)

    brief = client.get(
        f"/api/outreach?job_url=r1&contact_id={got['contact']['id']}"
        "&purpose=application_follow_up"
    ).get_json()
    assert brief["ok"] is True
    assert brief["contact"]["email"] == "alex@example.com"
    assert "Do not send anything" in brief["text"]
    assert "Current relisting JD." in brief["text"]

    removed = _post(client, "/api/contacts", {
        "job_url": "c1", "action": "delete", "contact_id": got["contact"]["id"],
    })
    assert removed.get_json()["contacts"] == []


def test_contact_and_outreach_api_validation_and_origin_guard(client, seed):
    make_job(seed, job_url="c1")
    bad = _post(client, "/api/contacts", {
        "job_url": "c1", "name": "Alex", "email": "not-an-email",
    })
    assert bad.status_code == 400 and "email" in bad.get_json()["message"]
    crossed = _post(
        client, "/api/contacts", {"job_url": "c1", "name": "Alex"},
        origin="http://evil.example",
    )
    assert crossed.status_code == 403
    assert seed.execute("SELECT COUNT(*) FROM job_contacts").fetchone()[0] == 0
    assert client.post("/api/contacts", json=["not", "an", "object"]).status_code == 400
    assert client.get("/api/outreach?job_url=c1&contact_id=nope").status_code == 400
    assert client.get("/api/contacts?job_url=missing").status_code == 404


# ------------------------------------------------------------------ /api/tasks

def test_task_crud_card_projection_and_due_queue(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="c1", title="AI PM", company="Acme")
    make_job(seed, job_url="r1", title="AI PM relist", company="Acme", repost_of="c1")
    created = _post(client, "/api/tasks", {
        "job_url": "r1", "title": "Prepare portfolio", "due_date": today,
        "note": "Choose two production AI examples",
    })
    got = created.get_json()
    assert created.status_code == 200 and got["ok"] is True
    assert got["task"]["interaction_url"] == "r1"

    listed = client.get("/api/tasks?job_url=c1").get_json()["tasks"]
    assert [task["title"] for task in listed] == ["Prepare portfolio"]
    cards = client.get("/api/jobs?view=backlog&page=1&page_size=20").get_json()["items"]
    assert len(cards) == 2
    assert {card["chain_root"] for card in cards} == {"c1"}
    assert all(card["tasks"][0]["id"] == got["task"]["id"] for card in cards)
    assert all(card["task_count"] == 1 for card in cards)
    due = client.get("/api/actions/tasks_due?page=1&page_size=20").get_json()
    assert [item["job_url"] for item in due["items"]] == ["c1"]
    assert due["items"][0]["next_task_due"] == today

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    snoozed = _post(client, "/api/tasks", {
        "job_url": "c1", "action": "snooze", "task_id": got["task"]["id"],
        "expected_version": got["task"]["version"], "due_date": tomorrow,
    }).get_json()
    assert snoozed["task"]["due_date"] == tomorrow
    assert client.get("/api/actions/tasks_due").get_json()["items"] == []

    completed = _post(client, "/api/tasks", {
        "job_url": "r1", "action": "complete", "task_id": got["task"]["id"],
        "expected_version": snoozed["task"]["version"],
    }).get_json()
    assert completed["task"]["status"] == "completed"
    assert completed["tasks"] == []
    history = client.get("/api/tasks?job_url=c1&include_closed=1").get_json()["tasks"]
    assert history[0]["status"] == "completed"
    reopened = _post(client, "/api/tasks", {
        "job_url": "c1", "action": "reopen", "task_id": got["task"]["id"],
        "expected_version": completed["task"]["version"],
    }).get_json()
    assert reopened["task"]["status"] == "open"


def test_task_api_validation_cross_chain_and_origin_guard(client, seed):
    make_job(seed, job_url="left")
    make_job(seed, job_url="right")
    bad = _post(client, "/api/tasks", {
        "job_url": "left", "title": "Task", "due_date": "tomorrow",
    })
    assert bad.status_code == 400 and "YYYY-MM-DD" in bad.get_json()["message"]
    crossed = _post(
        client, "/api/tasks",
        {"job_url": "left", "title": "Task", "due_date": "2026-08-08"},
        origin="http://evil.example",
    )
    assert crossed.status_code == 403
    assert client.post("/api/tasks", json=["bad"]).status_code == 400

    task = _post(client, "/api/tasks", {
        "job_url": "left", "title": "Left task", "due_date": "2026-08-08",
    }).get_json()["task"]
    missing = _post(client, "/api/tasks", {
        "job_url": "right", "action": "complete", "task_id": task["id"],
        "expected_version": task["version"],
    })
    assert missing.status_code == 404
    assert client.get("/api/tasks?job_url=missing").status_code == 404


def test_task_api_rejects_a_stale_cross_tab_update(client, seed):
    make_job(seed, job_url="root")
    task = _post(client, "/api/tasks", {
        "job_url": "root", "title": "Follow up", "due_date": "2026-08-08",
    }).get_json()["task"]
    first = _post(client, "/api/tasks", {
        "job_url": "root", "action": "snooze", "task_id": task["id"],
        "expected_version": task["version"], "due_date": "2026-08-09",
    })
    assert first.status_code == 200

    stale = _post(client, "/api/tasks", {
        "job_url": "root", "action": "snooze", "task_id": task["id"],
        "expected_version": task["version"], "due_date": "2026-08-15",
    })
    assert stale.status_code == 400
    assert stale.get_json()["message"] == "task changed; refresh and retry"
    current = client.get("/api/tasks?job_url=root").get_json()["tasks"][0]
    assert current["due_date"] == "2026-08-09"


# ----------------------------------------------------------------- /api/event

def test_event_records_and_returns_chain_outcome(client, seed):
    make_job(seed, job_url="c1", company="Chain Co", app_status="applied",
             status_date="2026-06-01")
    make_job(seed, job_url="r1", company="Chain Co", repost_of="c1",
             app_status="applied", status_date="2026-06-01")
    resp = _post(client, "/api/event",
                 {"job_url": "r1", "type": "interview", "date": "2026-06-12",
                  "note": "panel round"}).get_json()
    assert resp["ok"] is True
    assert set(resp["affected"]) == {"c1", "r1"} and resp["exempt"] == ["r1"]
    # The card patches its tag from the response (chain-wide cache, one truth source).
    assert resp["outcome_status"] == "interview" and resp["outcome_date"] == "2026-06-12"
    rows = {r["job_url"]: r["outcome_status"]
            for r in seed.execute("SELECT job_url, outcome_status FROM jobs")}
    assert rows == {"c1": "interview", "r1": "interview"}
    # ...and /api/jobs exposes the chain fields the Applied view renders.
    j = next(x for x in client.get("/api/jobs?view=applied").get_json()
             if x["job_url"] == "r1")
    assert j["chain_outcome_status"] == "interview"
    assert j["chain_outcome_date"] == "2026-06-12"

    # Undo removes the last event and the response reflects the stepped-back cache.
    resp = _post(client, "/api/event", {"job_url": "r1", "undo": True}).get_json()
    assert resp["ok"] is True and resp["outcome_status"] is None


def test_decision_response_carries_post_mutation_outcome_truth(client, seed):
    # The client patches outcome/resume from the response instead of mirroring rules: a
    # re-apply RESTORES the outcome from kept event history server-side, which no client
    # mirror can derive — without these fields the card showed "no response" over a DB
    # that said "interview", inviting a duplicate event record.
    make_job(seed, job_url="c1", app_status="applied", status_date="2026-06-01",
             resume_variant="variant-B")
    _post(client, "/api/event", {"job_url": "c1", "type": "interview", "date": "2026-06-12"})
    resp = _post(client, "/api/decision", {"job_url": "c1", "action": "undo_app"}).get_json()
    assert resp["outcome_status"] is None and resp["resume_variant"] is None
    resp = _post(client, "/api/decision", {"job_url": "c1", "action": "applied"}).get_json()
    assert resp["outcome_status"] == "interview"    # restored from kept history
    assert resp["outcome_date"] == "2026-06-12"


def test_non_string_body_values_get_json_error_not_500(client, seed):
    # The cores call .strip() on these — without the endpoint guard a number/list would
    # AttributeError into a Flask HTML 500 instead of the routes' JSON error contract.
    make_job(seed, job_url="c1", app_status="applied", status_date="2026-06-01")
    r = _post(client, "/api/decision",
              {"job_url": "c1", "action": "set_resume", "resume": 5})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    r = _post(client, "/api/event",
              {"job_url": "c1", "type": "interview", "note": {"text": "x"}})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    r = _post(client, "/api/event",
              {"job_url": "c1", "type": "interview", "date": 20260612})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    assert seed.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0


def test_event_refused_on_unapplied_chain(client, seed):
    make_job(seed, job_url="u1")
    resp = _post(client, "/api/event", {"job_url": "u1", "type": "offer"}).get_json()
    assert resp["ok"] is False and "applied" in resp["message"]
    assert seed.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 0


def test_role_note_is_available_before_application_and_reads_chain_wide(client, seed):
    make_job(seed, job_url="root")
    make_job(seed, job_url="relist", repost_of="root")

    response = _post(client, "/api/event", {
        "job_url": "relist", "type": "note", "note": "Verify the team charter",
    })
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert seed.execute(
        "SELECT app_status FROM jobs WHERE job_url='root'"
    ).fetchone()[0] is None
    assert client.get("/api/events?job_url=root").get_json()[0]["note"] == \
        "Verify the team charter"


def test_events_timeline_and_guards(client, seed):
    make_job(seed, job_url="c1", app_status="applied", status_date="2026-06-01")
    _post(client, "/api/event", {"job_url": "c1", "type": "recruiter_screen",
                                 "date": "2026-06-05"})
    _post(client, "/api/event", {"job_url": "c1", "type": "note", "note": "pinged them"})
    got = client.get("/api/events?job_url=c1").get_json()
    assert [(e["event_type"], e["note"]) for e in got] == \
        [("recruiter_screen", None), ("note", "pinged them")]
    assert client.get("/api/events").status_code == 400
    assert client.get("/api/events?job_url=ghost").status_code == 404
    # State-changing route carries the same origin guard as /api/decision.
    resp = _post(client, "/api/event", {"job_url": "c1", "type": "offer"},
                 origin="http://evil.example")
    assert resp.status_code == 403


def test_event_api_rejects_non_object_json(client, seed):
    make_job(seed, job_url="c1")
    response = _post(client, "/api/event", ["not", "an", "object"])
    assert response.status_code == 400 and response.get_json()["ok"] is False


# --------------------------------------------------------------- /api/timeline

def test_timeline_api_returns_bounded_chain_activity_without_contact_secrets(
    client, seed
):
    make_job(seed, job_url="root", first_seen="2026-08-01T09:00:00")
    seed.execute(
        """INSERT INTO app_events(job_url,event_type,event_date,note,created_at)
           VALUES ('root','note','2026-08-02','reviewed role',
                   '2026-08-02T10:00:00+00:00')"""
    )
    seed.execute(
        """INSERT INTO job_contacts
           (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
           VALUES ('root','root','Jane','Recruiter','recruiter','private@example.test',
                   'https://profile.test/jane','private note','2026-08-03T10:00:00+00:00')"""
    )
    seed.commit()

    response = client.get("/api/timeline?job_url=root&limit=2")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 3 and payload["truncated"] is True
    assert len(payload["items"]) == 2
    assert payload["items"][0]["kind"] == "contact"
    serialized = repr(payload)
    assert "private@example.test" not in serialized
    assert "profile.test" not in serialized and "private note" not in serialized


def test_timeline_api_validates_posting_and_limit(client):
    assert client.get("/api/timeline").status_code == 400
    assert client.get("/api/timeline?job_url=missing").status_code == 404
    invalid = client.get("/api/timeline?job_url=missing&limit=all")
    assert invalid.status_code == 400


# ------------------------------------------------------------------- /api/health

def test_health_api_returns_aggregates_without_private_posting_or_error_text(
    client, seed, monkeypatch
):
    from health import (finish_pipeline_run, record_fetch_attempt, start_pipeline_run)

    make_job(
        seed, job_url="health-role", source="linkedin", search_name="AI leadership",
        title="Private title", company="Private company",
        description="Private description", first_seen=date.today().isoformat() + "T09:00:00",
    )
    run_id = start_pipeline_run(
        seed, trigger="manual", run_date=date.today().isoformat()
    )
    record_fetch_attempt(
        seed, run_id=run_id, source_family="linkedin", target_kind="search",
        target_label="AI leadership", definition_hash="a" * 64, status="failed",
        error_kind="timeout",
    )
    finish_pipeline_run(seed, run_id, status="degraded")
    cfg = webapp.load_config()
    monkeypatch.setattr(
        webapp, "load_config",
        lambda: {**cfg, "searches": [{
            "name": "AI leadership", "term": "SECRET BOOLEAN QUERY MUST NOT LEAK"
        }]},
    )

    response = client.get("/api/health?days=30&run_limit=5")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["runs"][0]["status"] == "degraded"
    assert payload["runs"][0]["attempts_total"] == 1
    assert payload["runs"][0]["attempts_truncated"] is False
    assert payload["search_effectiveness"]["total"] >= 1
    serialized = repr(payload)
    assert "Private title" not in serialized
    assert "Private company" not in serialized
    assert "Private description" not in serialized
    assert "SECRET BOOLEAN QUERY" not in serialized
    assert "raw_error" not in serialized and "error_message" not in serialized


def test_health_api_validates_bounds(client):
    assert client.get("/api/health?days=0").status_code == 400
    assert client.get("/api/health?run_limit=all").status_code == 400


# ------------------------------------------------------------------ /api/dupe

def test_dupe_links_earliest_as_canonical_and_undo_splits(client, seed):
    make_job(seed, job_url="early", company="A Co", first_seen="2026-05-01T09:00:00")
    make_job(seed, job_url="late", company="B Co", first_seen="2026-06-01T09:00:00")
    resp = _post(client, "/api/dupe", {"job_url": "late", "of": "early"}).get_json()
    assert resp["ok"] is True
    row = seed.execute("SELECT repost_of, repost_source FROM jobs WHERE job_url='late'").fetchone()
    assert row["repost_of"] == "early" and row["repost_source"] == "manual"

    resp = _post(client, "/api/dupe", {"job_url": "late", "undo": True}).get_json()
    assert resp["ok"] is True
    row = seed.execute("SELECT repost_of, repost_source FROM jobs WHERE job_url='late'").fetchone()
    assert row["repost_of"] is None and row["repost_source"] is None


def test_dupe_conflicting_decisions_refused(client, seed):
    make_job(seed, job_url="ap", company="A Co", app_status="applied", status_date="2026-06-02")
    make_job(seed, job_url="pa", company="B Co", app_status="passed", status_date="2026-06-02")
    resp = _post(client, "/api/dupe", {"job_url": "ap", "of": "pa"}).get_json()
    assert resp["ok"] is False
    assert "decided differently" in resp["message"]


def test_dupe_bad_request(client, seed):
    assert _post(client, "/api/dupe", {"job_url": "x"}).status_code == 400
    malformed = _post(client, "/api/dupe", ["not", "an", "object"])
    assert malformed.status_code == 400 and malformed.is_json


# ------------------------------------------------------------------ /api/clip

def test_clip_returns_header_and_description(client, seed):
    make_job(seed, job_url="u1", title="Analyst", company="Acme Corp",
             description="short body")
    data = client.get("/api/clip?job_url=u1").get_json()
    assert data["text"].startswith("Analyst — Acme Corp\n")
    assert data["text"].endswith("short body")
    assert data["truncated"] is False


def test_clip_flags_truncated_description(client, seed):
    make_job(seed, job_url="u1", description="x" * CAP)
    assert client.get("/api/clip?job_url=u1").get_json()["truncated"] is True


def test_clip_missing_or_empty(client, seed):
    assert client.get("/api/clip").status_code == 400
    make_job(seed, job_url="empty", description="")
    assert client.get("/api/clip?job_url=empty").status_code == 404
    assert client.get("/api/clip?job_url=ghost").status_code == 404


# ------------------------------------------- second-opinion visibility (the UI razor)
#
# app._visible_opinion decides which second_judge.opinion_summaries entries reach the
# card: disagreement always (a warning is load-bearing at any age), a done agreement
# only while it can still change an action — undecided chain, effective posted-at
# (core.recency_dt) within AGREEMENT_FRESH_DAYS calendar days. Everything else is None.

UNDECIDED = {"app_status": None, "reject": False}


def _op(direction=None, status="done"):
    return {"direction": direction, "status": status, "verdict": "PASS", "fit_score": 16}


def test_visible_opinion_disagreement_survives_age_and_decisions():
    stale = {"date_posted": "", "first_seen": "2026-01-05T09:00:00"}
    applied = {"app_status": "applied", "reject": False}
    op = _op(direction="demote")
    assert webapp._visible_opinion(op, applied, stale, today=date(2026, 6, 10)) is op


def test_visible_opinion_agreement_needs_fresh_and_undecided():
    today = date(2026, 6, 10)
    fresh = {"date_posted": "2026-06-09T09:00:00", "first_seen": "2026-06-09T10:00:00"}
    op = _op()
    assert webapp._visible_opinion(op, UNDECIDED, fresh, today=today) is op
    # any chain decision hides it — the chip's job (confidence at apply time) is over
    assert webapp._visible_opinion(
        op, {"app_status": "applied", "reject": False}, fresh, today=today) is None
    assert webapp._visible_opinion(
        op, {"app_status": None, "reject": True}, fresh, today=today) is None
    # pending/errored never render, whatever the row looks like
    assert webapp._visible_opinion(_op(status="pending"), UNDECIDED, fresh, today=today) is None
    assert webapp._visible_opinion(_op(status="error"), UNDECIDED, fresh, today=today) is None
    assert webapp._visible_opinion(None, UNDECIDED, fresh, today=today) is None


def test_visible_opinion_agreement_window_is_calendar_days():
    today = date(2026, 6, 10)
    # AGREEMENT_FRESH_DAYS=3 mirrors fresh_strong: today plus the two preceding dates
    edge = {"date_posted": "2026-06-08T09:00:00", "first_seen": "2026-06-01T00:00:00"}
    out = {"date_posted": "2026-06-07T23:00:00", "first_seen": "2026-06-01T00:00:00"}
    dateless = {"date_posted": "", "first_seen": ""}
    assert webapp._visible_opinion(_op(), UNDECIDED, edge, today=today) is not None
    assert webapp._visible_opinion(_op(), UNDECIDED, out, today=today) is None
    assert webapp._visible_opinion(_op(), UNDECIDED, dateless, today=today) is None


def _seed_opinion(seed, url, verdict, fit, cid, collected_at=None, status="done"):
    seed.execute(
        "INSERT INTO second_opinions (job_url, custom_id, model, status, submitted_at,"
        " collected_at, verdict, fit_score, gate_notes)"
        " VALUES (?, ?, 'test-model', ?, 'now', ?, ?, ?, '')",
        (url, cid, status, collected_at, verdict, fit))
    seed.commit()


def test_jobs_api_serializes_agreement_only_inside_the_window(client, seed):
    today = date.today().isoformat()
    make_job(seed, job_url="fresh_agree", fit_score=16,
             first_seen=f"{today}T09:00:00")
    make_job(seed, job_url="stale_agree", fit_score=16)  # first_seen 2026-06-01, long past
    make_job(seed, job_url="stale_flag", fit_score=16)
    _seed_opinion(seed, "fresh_agree", "PASS", 17, cid="c1")
    _seed_opinion(seed, "stale_agree", "PASS", 17, cid="c2")
    _seed_opinion(seed, "stale_flag", "GATE_FAIL", None, cid="c3")
    got = {j["job_url"]: j for j in client.get("/api/jobs?view=backlog").get_json()}
    agree = got["fresh_agree"]["second_opinion"]
    assert agree is not None and agree["direction"] is None
    assert agree["verdict"] == "PASS" and agree["fit_score"] == 17
    # stale agreement spends zero pixels; a stale DISAGREEMENT still warns
    assert got["stale_agree"]["second_opinion"] is None
    assert got["stale_flag"]["second_opinion"]["direction"] == "demote"


# ------------------------------------------------- the freshness poll (/api/freshness)
#
# The UI never refetches on its own, so opinions collected behind an open tab stay
# invisible. The poll counts what the tab is MISSING — through the same razor, so an
# all-agreement batch on decided rows raises no banner — and is bounded on both axes.


def test_freshness_counts_only_opinions_the_card_would_render(client, seed):
    now = datetime.now()
    since = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    after = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    before = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    today = date.today().isoformat()
    make_job(seed, job_url="fresh_agree", fit_score=16, first_seen=f"{today}T09:00:00")
    for url in ("stale_agree", "stale_flag", "old_flag", "pending_flag"):
        make_job(seed, job_url=url, fit_score=16)  # first_seen 2026-06-01, long past
    _seed_opinion(seed, "fresh_agree", "PASS", 17, cid="c1", collected_at=after)
    _seed_opinion(seed, "stale_agree", "PASS", 17, cid="c2", collected_at=after)
    _seed_opinion(seed, "stale_flag", "GATE_FAIL", None, cid="c3", collected_at=after)
    _seed_opinion(seed, "old_flag", "GATE_FAIL", None, cid="c4", collected_at=before)
    _seed_opinion(seed, "pending_flag", None, None, cid="c5", collected_at=after,
                  status="pending")
    got = client.get("/api/freshness?since=" + since).get_json()
    # The fresh agreement and the stale DISAGREEMENT would change pixels. The stale
    # agreement, the pre-`since` arrival, and the pending row would not — a banner that
    # fires on those trains the user to ignore it.
    assert got["opinions"] == 2
    assert got["truncated"] is False and got["now"]


def test_freshness_bounds_the_scan_and_ignores_a_garbage_since(client, seed):
    ancient = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    make_job(seed, job_url="ancient_flag", fit_score=16)
    _seed_opinion(seed, "ancient_flag", "GATE_FAIL", None, cid="c9", collected_at=ancient)
    # A `since` older than the lookback is clamped to it: a tab open for a month asks
    # about the last week, not the whole table (the row cap bounds the rest).
    assert client.get("/api/freshness?since=1970-01-01T00:00:00").get_json()["opinions"] == 0
    # No/garbage baseline is a pure baseline read — the caller gets `now`, counts nothing.
    baseline = client.get("/api/freshness").get_json()
    assert baseline["opinions"] == 0 and baseline["now"]
    assert client.get("/api/freshness?since=not-a-time").get_json()["opinions"] == 0


def test_freshness_accepts_an_offset_carrying_since(client, seed):
    """collected_at is machine-local and naive; an aware `since` must be converted to that
    clock, not compared against it (that comparison is a TypeError -> 500)."""
    now = datetime.now()
    make_job(seed, job_url="tz_flag", fit_score=16)
    _seed_opinion(seed, "tz_flag", "GATE_FAIL", None, cid="c8",
                  collected_at=(now - timedelta(hours=1)).isoformat(timespec="seconds"))
    aware = (now - timedelta(hours=2)).astimezone().isoformat(timespec="seconds")
    got = client.get("/api/freshness?since=" + aware)
    assert got.status_code == 200
    assert got.get_json()["opinions"] == 1
