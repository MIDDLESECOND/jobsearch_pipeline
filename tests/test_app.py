"""Flask triage-UI endpoints — the behavior the coming refactors must preserve.

These pin the HTTP contract of app.py: view filtering (today/backlog/applied/passed and the
backlog's chain-decided-relisting drop), decision propagation + the `affected` list the client
uses to update sibling cards, the dupe link/undo/conflict paths, the clip payload, and the
origin guard. Same synthetic-DB discipline as the rest of the suite (temp file via the shared
schema builder + make_job) — never the real jobs.db, never the network.
"""

import sqlite3

import pytest

import app as webapp
import chain
import core
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
                        "feedback_project_url": ""}}

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


# -------------------------------------------------------------- /api/decision

def test_decision_applied_propagates_across_chain(client, seed):
    make_job(seed, job_url="c1", company="Chain Co")
    make_job(seed, job_url="r1", company="Chain Co", repost_of="c1")
    resp = _post(client, "/api/decision", {"job_url": "r1", "action": "applied"}).get_json()
    assert resp["ok"] is True
    assert set(resp["affected"]) == {"c1", "r1"}
    rows = {r["job_url"]: r for r in seed.execute("SELECT * FROM jobs").fetchall()}
    assert rows["c1"]["app_status"] == "applied" and rows["r1"]["app_status"] == "applied"


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
