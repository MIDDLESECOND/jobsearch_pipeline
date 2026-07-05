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
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

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
