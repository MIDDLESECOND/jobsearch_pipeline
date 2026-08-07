"""Bounded, factual comparisons between stored posting and snapshot evidence."""

import hashlib
import os

import pytest

import materials
from conftest import make_job
from jd_diff import (JD_DIFF_MAX_INPUT_CHARS, JDDiffTooLarge, jd_diff_bundle,
                     jd_versions_bundle)


def _cfg(conn):
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return {"settings": {"db_path": path}}


def _posting(versions, title):
    return next(item for item in versions if item["kind"] == "posting" and item["title"] == title)


def _attach_legacy_snapshot(conn, row, text):
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    relative = materials._store_blob(conn, digest, ".txt", data, _cfg(conn))
    materials._insert_object(
        conn, digest=digest, media_type="text/plain", extension=".txt", size=len(data),
        stored_path=relative, ats_status=None, ats=None,
    )
    materials._attach(conn, row, "jd_snapshot", digest, "legacy snapshot.txt")
    conn.commit()


def test_default_comparison_uses_latest_two_real_instants_and_reports_literal_changes(conn):
    old = make_job(
        conn, job_url="old", title="AI Product Lead", location="New York",
        first_seen="2026-08-02T10:00:00+05:00",
        description="Own roadmap and launch quality. Python required.", salary_min=180000,
    )
    make_job(
        conn, job_url="latest", title="Senior AI Product Lead", location="Remote US",
        first_seen="2026-08-02T08:00:00+00:00", repost_of="old",
        description="Own roadmap, customer discovery, and launch quality. Python preferred.",
        salary_min=190000,
    )

    listed = jd_versions_bundle(conn, old, description_cap=50000)
    posting_titles = [item["title"] for item in listed["versions"] if item["kind"] == "posting"]
    assert posting_titles == ["AI Product Lead", "Senior AI Product Lead"]
    comparison = jd_diff_bundle(conn, old, description_cap=50000)["comparison"]
    assert comparison["left"]["title"] == "AI Product Lead"
    assert comparison["right"]["title"] == "Senior AI Product Lead"
    assert {item["field"] for item in comparison["metadata_changes"]} == {
        "title", "location", "salary_min",
    }
    changed = repr(comparison["hunks"])
    assert "customer discovery" in changed
    assert "required" in changed and "preferred" in changed
    assert "semantic" not in comparison["normalization"]


def test_conservative_normalization_ignores_only_formatting_it_declares(conn):
    old = make_job(conn, job_url="old", description="Own roadmap.  \r\n\r\nShip safely.\r\n")
    make_job(
        conn, job_url="new", repost_of="old", first_seen="2026-06-02T00:00:00",
        description="Own roadmap.\n\n\nShip safely.\n",
    )
    comparison = jd_diff_bundle(conn, old)["comparison"]
    assert comparison["hunks"] == []
    assert comparison["same_after_normalization"] is True

    conn.execute("UPDATE jobs SET description='own roadmap.\n\nShip safely!' WHERE job_url='new'")
    conn.commit()
    assert jd_diff_bundle(conn, old)["comparison"]["same_after_normalization"] is False


def test_explicit_opaque_versions_must_belong_to_current_chain(conn):
    left = make_job(conn, job_url="left", title="Left", description="Left version")
    make_job(conn, job_url="right", title="Right", repost_of="left",
             first_seen="2026-06-02T00:00:00", description="Right version")
    unrelated = make_job(conn, job_url="unrelated", title="Unrelated",
                         description="Private unrelated JD")
    left_versions = jd_versions_bundle(conn, left)["versions"]
    unrelated_id = jd_versions_bundle(conn, unrelated)["versions"][0]["id"]
    with pytest.raises(ValueError, match="current role chain"):
        jd_diff_bundle(
            conn, left, left_id=left_versions[0]["id"], right_id=unrelated_id,
        )
    assert "left" not in left_versions[0]["id"]


def test_merge_and_unlink_derive_version_scope_without_copying_text(conn):
    left = make_job(conn, job_url="left", title="Left", description="Left description")
    right = make_job(conn, job_url="right", title="Right", description="Right description")
    assert len(jd_versions_bundle(conn, left)["versions"]) == 1

    conn.execute("UPDATE jobs SET repost_of='left' WHERE job_url='right'")
    conn.commit()
    current_right = conn.execute("SELECT * FROM jobs WHERE job_url='right'").fetchone()
    assert len(jd_versions_bundle(conn, current_right)["versions"]) == 2

    conn.execute("UPDATE jobs SET repost_of=NULL WHERE job_url='right'")
    conn.commit()
    assert len(jd_versions_bundle(conn, left)["versions"]) == 1
    assert len(jd_versions_bundle(conn, right)["versions"]) == 1


def test_application_snapshot_is_hash_verified_and_preferred_as_default_baseline(conn):
    row = make_job(
        conn, job_url="root", title="Original", description="Original frozen requirement",
        app_status="applied", status_date="2026-08-05",
    )
    materials.snapshot_jd(conn, row, _cfg(conn))
    conn.execute(
        "UPDATE jobs SET title='Updated',description='Updated stored requirement' WHERE job_url='root'"
    )
    conn.commit()

    listed = jd_versions_bundle(conn, row, _cfg(conn))["versions"]
    snapshot = next(item for item in listed if item["kind"] == "snapshot")
    posting = _posting(listed, "Updated")
    assert snapshot["availability"] == "available"
    assert "immutable application-time snapshot" in snapshot["completeness"]
    comparison = jd_diff_bundle(conn, row, cfg=_cfg(conn))["comparison"]
    assert comparison["left"]["id"] == snapshot["id"]
    assert comparison["right"]["id"] == posting["id"]
    assert "Original frozen requirement" in repr(comparison["hunks"])
    assert "Updated stored requirement" in repr(comparison["hunks"])


def test_adzuna_snapshot_retains_source_snippet_provenance(conn):
    row = make_job(
        conn, job_url="adzuna-role", source="adzuna", description="Stored source snippet",
        app_status="applied", status_date="2026-08-05",
    )
    materials.snapshot_jd(conn, row, _cfg(conn))

    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["source"] == "adzuna"
    assert snapshot["possibly_truncated"] is True
    assert "Adzuna source snippet" in snapshot["completeness"]


@pytest.mark.parametrize("field", ["title", "company", "location"])
def test_new_snapshot_header_normalizes_multiline_metadata_without_leaking_it(conn, field):
    values = {"title": "Lead", "company": "Acme", "location": "Remote"}
    values[field] = values[field] + "\nInjected metadata"
    row = make_job(
        conn, job_url="private-url", description="Only JD body", app_status="applied",
        status_date="2026-08-05", title=values["title"], company=values["company"],
        location=values["location"],
    )
    materials.snapshot_jd(conn, row, _cfg(conn))

    record = materials.jd_snapshot_records(conn, row, _cfg(conn))[0]
    assert record["format"] == "snapshot_v1"
    assert record[field] == values[field].replace("\n", " ")
    assert record["_text"] == "Only JD body"
    assert "private-url" not in record["_text"]


@pytest.mark.parametrize("field", ["title", "company", "location"])
def test_multiline_legacy_v1_header_is_stripped_at_exact_interaction_url(conn, field):
    values = {"title": "Lead", "company": "Acme", "location": "Remote"}
    values[field] = values[field] + "\nInjected metadata"
    row = make_job(conn, job_url="private-url", description="Current body")
    legacy = "\n".join([
        "JOB DESCRIPTION SNAPSHOT",
        f"Title: {values['title']}",
        f"Company: {values['company']}",
        f"Location: {values['location']}",
        "URL: private-url",
        "",
        "Only legacy JD body",
    ])
    _attach_legacy_snapshot(conn, row, legacy)

    record = materials.jd_snapshot_records(conn, row, _cfg(conn))[0]
    assert record["format"] == "snapshot_v1"
    assert record[field] == values[field]
    assert record["_text"] == "Only legacy JD body"
    assert "private-url" not in record["_text"]


def test_legacy_v1_body_may_contain_the_interaction_url_without_being_truncated(conn):
    row = make_job(conn, job_url="private-url", description="Current body")
    body = "Intro\nURL: private-url\n\nStill part of the JD body"
    legacy = "\n".join([
        "JOB DESCRIPTION SNAPSHOT", "Title: Lead", "Company: Acme",
        "Location: Remote", "URL: private-url", "", body,
    ])
    _attach_legacy_snapshot(conn, row, legacy)

    record = materials.jd_snapshot_records(conn, row, _cfg(conn))[0]
    assert record["format"] == "snapshot_v1"
    assert record["_text"] == body


def test_missing_snapshot_remains_listed_and_never_falls_back_to_current_description(conn):
    row = make_job(conn, job_url="root", description="Frozen text", app_status="applied",
                   status_date="2026-08-05")
    digest = materials.snapshot_jd(conn, row, _cfg(conn))
    relative = conn.execute(
        "SELECT stored_path FROM material_objects WHERE sha256=?", (digest,)
    ).fetchone()[0]
    (materials.material_root(_cfg(conn)) / relative).unlink()
    listed = jd_versions_bundle(conn, row, _cfg(conn))["versions"]
    snapshot = next(item for item in listed if item["kind"] == "snapshot")
    posting = next(item for item in listed if item["kind"] == "posting")
    assert snapshot["availability"] == "missing"
    with pytest.raises(ValueError, match="missing or corrupt"):
        jd_diff_bundle(
            conn, row, left_id=snapshot["id"], right_id=posting["id"], cfg=_cfg(conn),
        )


def test_snapshot_hash_is_rechecked_even_when_size_and_mtime_are_unchanged(conn):
    row = make_job(conn, job_url="root", description="Frozen text", app_status="applied",
                   status_date="2026-08-05")
    digest = materials.snapshot_jd(conn, row, _cfg(conn))
    relative = conn.execute(
        "SELECT stored_path FROM material_objects WHERE sha256=?", (digest,)
    ).fetchone()[0]
    path = materials.material_root(_cfg(conn)) / relative
    before = path.stat()
    path.write_bytes(b"X" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["availability"] == "corrupt"


def test_snapshot_reader_bounds_actual_file_even_when_catalog_size_is_small(conn):
    row = make_job(conn, job_url="root", description="Frozen text", app_status="applied",
                   status_date="2026-08-05")
    digest = materials.snapshot_jd(conn, row, _cfg(conn))
    relative = conn.execute(
        "SELECT stored_path FROM material_objects WHERE sha256=?", (digest,)
    ).fetchone()[0]
    path = materials.material_root(_cfg(conn)) / relative
    path.write_bytes(b"X" * (materials.MAX_JD_SNAPSHOT_READ_BYTES + 1))

    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["availability"] == "too_large"


def test_snapshot_reader_does_not_let_oversized_catalog_hide_corrupt_or_missing_bytes(conn):
    row = make_job(conn, job_url="root", description="Frozen text", app_status="applied",
                   status_date="2026-08-05")
    digest = materials.snapshot_jd(conn, row, _cfg(conn))
    relative = conn.execute(
        "SELECT stored_path FROM material_objects WHERE sha256=?", (digest,)
    ).fetchone()[0]
    path = materials.material_root(_cfg(conn)) / relative
    conn.execute(
        "UPDATE material_objects SET size_bytes=? WHERE sha256=?",
        (materials.MAX_JD_SNAPSHOT_READ_BYTES + 1, digest),
    )
    conn.commit()

    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["availability"] == "corrupt"
    path.unlink()
    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["availability"] == "missing"


def test_unsafe_snapshot_catalog_path_is_corrupt_not_missing(conn):
    row = make_job(conn, job_url="root", description="Frozen text", app_status="applied",
                   status_date="2026-08-05")
    digest = materials.snapshot_jd(conn, row, _cfg(conn))
    conn.execute(
        "UPDATE material_objects SET stored_path='../outside.txt' WHERE sha256=?", (digest,)
    )
    conn.commit()

    snapshot = next(item for item in jd_versions_bundle(conn, row, _cfg(conn))["versions"]
                    if item["kind"] == "snapshot")
    assert snapshot["availability"] == "corrupt"


def test_two_application_snapshots_can_be_compared_explicitly(conn):
    row = make_job(conn, job_url="root", title="Original", description="First frozen text",
                   app_status="applied", status_date="2026-08-05")
    materials.snapshot_jd(conn, row, _cfg(conn))
    conn.execute("UPDATE jobs SET title='Revised',description='Second frozen text' WHERE job_url='root'")
    conn.commit()
    current = conn.execute("SELECT * FROM jobs WHERE job_url='root'").fetchone()
    materials.snapshot_jd(conn, current, _cfg(conn))
    snapshots = [item for item in jd_versions_bundle(conn, current, _cfg(conn))["versions"]
                 if item["kind"] == "snapshot"]
    assert len(snapshots) == 2
    comparison = jd_diff_bundle(
        conn, current, left_id=snapshots[0]["id"], right_id=snapshots[1]["id"],
        cfg=_cfg(conn),
    )["comparison"]
    assert "First frozen text" in repr(comparison["hunks"])
    assert "Second frozen text" in repr(comparison["hunks"])


def test_snapshot_owner_follows_merge_and_unlink_not_historical_interaction(conn):
    left = make_job(conn, job_url="left", description="Left")
    right = make_job(
        conn, job_url="right", repost_of="left", description="Right applied text",
        app_status="applied", status_date="2026-08-05",
    )
    materials.snapshot_jd(conn, right, _cfg(conn))
    assert any(item["kind"] == "snapshot" for item in
               jd_versions_bundle(conn, right, _cfg(conn))["versions"])

    conn.execute("UPDATE jobs SET repost_of=NULL WHERE job_url='right'")
    conn.commit()
    assert any(item["kind"] == "snapshot" for item in
               jd_versions_bundle(conn, left, _cfg(conn))["versions"])
    assert not any(item["kind"] == "snapshot" for item in
                   jd_versions_bundle(conn, right, _cfg(conn))["versions"])


def test_resource_guards_fail_closed_instead_of_returning_partial_diff(conn):
    old = make_job(conn, job_url="old", description="a" * (JD_DIFF_MAX_INPUT_CHARS + 1))
    make_job(conn, job_url="new", repost_of="old", first_seen="2026-06-02T00:00:00",
             description="short")
    listed = jd_versions_bundle(conn, old)["versions"]
    too_large = _posting(listed, "Data Analyst")
    available = next(item for item in listed
                     if item["kind"] == "posting" and item["availability"] == "available")
    assert too_large["availability"] == "too_large"
    with pytest.raises(JDDiffTooLarge, match="too large"):
        jd_diff_bundle(conn, old, left_id=too_large["id"], right_id=available["id"])

    conn.execute("UPDATE jobs SET description=? WHERE job_url='old'",
                 ("\n".join(f"left {i}" for i in range(2000)),))
    conn.execute("UPDATE jobs SET description=? WHERE job_url='new'",
                 ("\n".join(f"right {i}" for i in range(2000)),))
    conn.commit()
    with pytest.raises(JDDiffTooLarge, match="matrix"):
        jd_diff_bundle(conn, old)


def test_line_limit_is_listed_as_too_large_and_excluded_from_defaults(conn):
    old = make_job(conn, job_url="old", title="Oversized", description="\n".join(
        f"x{i % 10}" for i in range(2001)
    ))
    make_job(conn, job_url="middle", title="Middle", repost_of="old",
             first_seen="2026-06-02T00:00:00", description="middle")
    make_job(conn, job_url="latest", title="Latest", repost_of="old",
             first_seen="2026-06-03T00:00:00", description="latest")

    listed = jd_versions_bundle(conn, old)
    oversized = _posting(listed["versions"], "Oversized")
    assert oversized["availability"] == "too_large"
    assert oversized["id"] not in {listed["default_left"], listed["default_right"]}
    with pytest.raises(JDDiffTooLarge, match="too large"):
        jd_diff_bundle(
            conn, old, left_id=oversized["id"], right_id=listed["default_right"],
        )


def test_snapshot_line_limit_is_listed_as_too_large_without_breaking_defaults(conn):
    row = make_job(
        conn, job_url="old", title="Original", description="\n".join(
            f"x{i % 10}" for i in range(2001)
        ), app_status="applied", status_date="2026-08-05",
    )
    materials.snapshot_jd(conn, row, _cfg(conn))
    conn.execute("UPDATE jobs SET description='middle' WHERE job_url='old'")
    conn.commit()
    make_job(conn, job_url="latest", title="Latest", repost_of="old",
             first_seen="2026-06-03T00:00:00", description="latest")

    listed = jd_versions_bundle(conn, row, _cfg(conn))
    snapshot = next(item for item in listed["versions"] if item["kind"] == "snapshot")
    assert snapshot["availability"] == "too_large"
    assert snapshot["id"] not in {listed["default_left"], listed["default_right"]}
    with pytest.raises(JDDiffTooLarge, match="too large"):
        jd_diff_bundle(
            conn, row, left_id=snapshot["id"], right_id=listed["default_right"],
            cfg=_cfg(conn),
        )


def test_version_list_does_not_serialize_bodies_or_other_private_evidence(conn):
    secret_text = "<script>private JD body</script>"
    old = make_job(conn, job_url="old", title="Old", description=secret_text)
    make_job(conn, job_url="new", title="New", repost_of="old",
             first_seen="2026-06-02T00:00:00", description="New body")
    conn.execute(
        """INSERT INTO job_contacts
           (job_url,interaction_url,name,role,kind,email,profile_url,note,created_at)
           VALUES ('old','old','Secret Person',NULL,'other','secret@example.test',NULL,
                   'private note','2026-06-01T00:00:00')"""
    )
    conn.commit()
    serialized = repr(jd_versions_bundle(conn, old))
    assert secret_text not in serialized
    assert "Secret Person" not in serialized and "secret@example.test" not in serialized
    assert "job_url" not in serialized
    assert secret_text in repr(jd_diff_bundle(conn, old)["comparison"]["hunks"])


def test_read_joins_caller_snapshot_without_committing_it(conn):
    old = make_job(conn, job_url="old", title="Old", description="Old")
    make_job(conn, job_url="new", title="New", repost_of="old",
             first_seen="2026-06-02T00:00:00", description="New")
    conn.execute("UPDATE jobs SET title='uncommitted title' WHERE job_url='new'")
    bundle = jd_versions_bundle(conn, old)
    assert conn.in_transaction
    assert any(item["title"] == "uncommitted title" for item in bundle["versions"])
    conn.rollback()
