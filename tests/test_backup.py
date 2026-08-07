"""Evidence-unit backups keep SQLite metadata and immutable material bytes together."""

import hashlib
import json
import sqlite3
import struct
import sys
import zipfile
from datetime import datetime, timezone

import pytest

import backup
import materials
import pipeline
from backup import BackupError, create_backup, verify_backup
from conftest import make_job


NOW = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)


def _snapshot(conn, *, url="https://example.test/role"):
    row = make_job(conn, job_url=url, description="immutable private JD")
    materials.snapshot_jd(conn, row)
    obj = conn.execute(
        "SELECT sha256,size_bytes,stored_path FROM material_objects"
    ).fetchone()
    return obj, materials.material_root(conn=conn) / obj["stored_path"]


def _rewrite_entries(archive, replacements):
    rewritten = archive.with_suffix(".rewritten.zip")
    with zipfile.ZipFile(archive, "r") as source, zipfile.ZipFile(
        rewritten, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            target.writestr(info.filename, replacements.get(info.filename, data))
    rewritten.replace(archive)


def _rewrite_entry(archive, member, replacement):
    _rewrite_entries(archive, {member: replacement})


def test_backup_round_trip_contains_snapshot_database_and_only_catalogued_objects(conn, tmp_path):
    obj, object_path = _snapshot(conn)
    orphan = object_path.parent / "not-in-database.txt"
    orphan.write_text("must not be copied", encoding="utf-8")
    archive = tmp_path / "evidence.zip"

    created = create_backup(conn, archive, created_at=NOW)
    verified = verify_backup(archive)

    assert created == verified
    assert created["created_at"] == "2026-08-07T12:30:00Z"
    assert created["jobs"] == 1
    assert created["material_links"] == 1
    assert created["material_objects"] == 1
    expected_object = f"application_materials/{obj['stored_path']}"
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"manifest.json", "jobs.db", expected_object}
        assert bundle.read(expected_object) == object_path.read_bytes()
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["format"] == "jobsearch_pipeline_evidence_backup"
        assert manifest["format_version"] == 1
        extracted = tmp_path / "restored.db"
        extracted.write_bytes(bundle.read("jobs.db"))
    restored = sqlite3.connect(extracted)
    try:
        assert restored.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert restored.execute(
            "SELECT COUNT(*) FROM application_materials"
        ).fetchone()[0] == 1
    finally:
        restored.close()
    with zipfile.ZipFile(archive) as bundle:
        assert "not-in-database.txt" not in "\n".join(bundle.namelist())


def test_backup_refuses_missing_or_corrupt_material_and_leaves_no_archive(conn, tmp_path):
    _, object_path = _snapshot(conn)
    archive = tmp_path / "evidence.zip"
    object_path.unlink()

    with pytest.raises(BackupError, match="missing"):
        create_backup(conn, archive, created_at=NOW)

    assert not archive.exists()
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(b"wrong bytes")
    with pytest.raises(BackupError, match="size|checksum"):
        create_backup(conn, archive, created_at=NOW)
    assert not archive.exists()
    assert list(tmp_path.glob(".jobsearch-backup-*")) == []


def test_backup_rejects_material_path_escape(conn, tmp_path):
    _snapshot(conn)
    conn.execute("UPDATE material_objects SET stored_path='../outside.txt'")
    conn.commit()

    with pytest.raises(BackupError, match="outside the material store"):
        create_backup(conn, tmp_path / "evidence.zip", created_at=NOW)


@pytest.mark.parametrize("missing_reference", ["owner", "interaction"])
def test_backup_rejects_material_links_hidden_by_missing_postings(
    conn, tmp_path, missing_reference
):
    root = make_job(conn, job_url="root", description="canonical JD")
    interaction = make_job(
        conn,
        job_url="interaction",
        repost_of="root",
        description="interaction JD",
    )
    materials.snapshot_jd(conn, interaction)
    deleted_url = root["job_url"] if missing_reference == "owner" else interaction["job_url"]
    conn.execute("DELETE FROM jobs WHERE job_url=?", (deleted_url,))
    conn.commit()

    with pytest.raises(BackupError, match="missing posting"):
        create_backup(conn, tmp_path / "evidence.zip", created_at=NOW)


def test_backup_rejects_orphaned_pipeline_fetch_attempt(conn, tmp_path):
    conn.execute(
        """INSERT INTO pipeline_fetch_attempts
           (run_id,source_family,target_kind,target_label,definition_hash,started_at,ended_at,
            status,skip_reason,error_kind,returned_count,eligible_count,inserted_count,repost_count)
           VALUES (999,'linkedin','search','track',NULL,'2026-08-07T00:00:00+00:00',
                   '2026-08-07T00:01:00+00:00','success',NULL,NULL,0,0,0,0)"""
    )
    conn.commit()

    with pytest.raises(BackupError, match="missing pipeline run"):
        create_backup(conn, tmp_path / "orphan-attempt.zip", created_at=NOW)


@pytest.mark.parametrize("missing", ["entry", "owner", "interaction"])
def test_backup_rejects_orphaned_prep_library_links(conn, tmp_path, missing):
    make_job(conn, job_url="owner")
    make_job(conn, job_url="interaction", repost_of="owner")
    conn.execute(
        """INSERT INTO prep_entries
           (kind,title,prompt,response,tags_json,status,created_at,updated_at,confirmed_at,version)
           VALUES ('story','Evidence',NULL,'Truth','[]','confirmed',
                   '2026-08-07T00:00:00+00:00','2026-08-07T00:00:00+00:00',
                   '2026-08-07T00:00:00+00:00',1)"""
    )
    entry_id = conn.execute("SELECT id FROM prep_entries").fetchone()[0]
    conn.execute(
        """INSERT INTO prep_entry_roles
           (entry_id,job_url,interaction_url,linked,linked_at,version)
           VALUES (?,?,?,1,'2026-08-07T00:00:00+00:00',1)""",
        (entry_id, "owner", "interaction"),
    )
    if missing == "entry":
        conn.execute("DELETE FROM prep_entries WHERE id=?", (entry_id,))
    else:
        conn.execute("DELETE FROM jobs WHERE job_url=?", (missing,))
    conn.commit()

    with pytest.raises(BackupError, match="prep library link references missing data"):
        create_backup(conn, tmp_path / "orphan-prep.zip", created_at=NOW)


def test_backup_refuses_existing_destination_and_uncommitted_source(conn, tmp_path):
    archive = tmp_path / "evidence.zip"
    archive.write_bytes(b"keep me")

    with pytest.raises(BackupError, match="already exists"):
        create_backup(conn, archive, created_at=NOW)
    assert archive.read_bytes() == b"keep me"

    conn.execute("INSERT INTO meta(key,value) VALUES ('pending','change')")
    with pytest.raises(BackupError, match="uncommitted transaction"):
        create_backup(conn, tmp_path / "other.zip", created_at=NOW)
    conn.rollback()


def test_backup_does_not_overwrite_destination_created_during_publish(
    conn, tmp_path, monkeypatch
):
    archive = tmp_path / "evidence.zip"
    original_link = backup.os.link

    def competing_publish(source, destination):
        destination = type(archive)(destination)
        destination.write_bytes(b"created by another process")
        return original_link(source, destination)

    monkeypatch.setattr(backup.os, "link", competing_publish)

    with pytest.raises(BackupError, match="already exists"):
        create_backup(conn, archive, created_at=NOW)

    assert archive.read_bytes() == b"created by another process"


def test_backup_verifies_temporary_archive_before_publishing(conn, tmp_path, monkeypatch):
    archive = tmp_path / "evidence.zip"

    def interrupted_verification(_archive):
        raise KeyboardInterrupt()

    monkeypatch.setattr(backup, "verify_backup", interrupted_verification)

    with pytest.raises(KeyboardInterrupt):
        create_backup(conn, archive, created_at=NOW)

    assert not archive.exists()
    assert list(tmp_path.glob(".jobsearch-backup-*")) == []


def test_verify_detects_tampered_material_without_extracting_it(conn, tmp_path):
    obj, _ = _snapshot(conn)
    archive = tmp_path / "evidence.zip"
    create_backup(conn, archive, created_at=NOW)
    member = f"application_materials/{obj['stored_path']}"
    _rewrite_entry(archive, member, b"tampered")

    with pytest.raises(BackupError, match="size|checksum"):
        verify_backup(archive)


def test_verify_cross_checks_manifest_against_database_catalog(conn, tmp_path):
    obj, _ = _snapshot(conn)
    archive = tmp_path / "evidence.zip"
    create_backup(conn, archive, created_at=NOW)
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        database_bytes = bundle.read("jobs.db")
    altered_db = tmp_path / "altered.db"
    altered_db.write_bytes(database_bytes)
    altered = sqlite3.connect(altered_db)
    try:
        altered.execute(
            "UPDATE material_objects SET stored_path=? WHERE sha256=?",
            (f"objects/ff/{obj['sha256']}.txt", obj["sha256"]),
        )
        altered.commit()
    finally:
        altered.close()
    database_bytes = altered_db.read_bytes()
    manifest["database"]["sha256"] = hashlib.sha256(database_bytes).hexdigest()
    manifest["database"]["size_bytes"] = len(database_bytes)
    _rewrite_entries(
        archive,
        {
            "jobs.db": database_bytes,
            "manifest.json": json.dumps(manifest, sort_keys=True).encode("utf-8"),
        },
    )

    with pytest.raises(BackupError, match="does not match the database catalog"):
        verify_backup(archive)


def test_cli_verify_is_read_only_and_does_not_load_private_config(
    conn, tmp_path, monkeypatch, capsys
):
    archive = tmp_path / "evidence.zip"
    create_backup(conn, archive, created_at=NOW)

    def unexpected_config_read():
        pytest.fail("backup --verify must not load config.yaml or open the live database")

    monkeypatch.setattr(pipeline, "load_config", unexpected_config_read)
    monkeypatch.setattr(sys, "argv", ["pipeline.py", "backup", "--verify", str(archive)])

    pipeline.main()

    output = capsys.readouterr()
    assert "[backup] verified" in output.out
    assert "0 material object(s)" in output.out
    assert output.err == ""


def test_cli_create_uses_live_database_and_produces_verified_archive(
    conn, tmp_path, monkeypatch, capsys
):
    _snapshot(conn)
    archive = tmp_path / "from-cli.zip"
    monkeypatch.setattr(pipeline, "load_config", lambda: {"settings": {}})
    monkeypatch.setattr(pipeline, "get_db", lambda _cfg: conn)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pipeline.py", "backup", "--output", str(archive)],
    )

    pipeline.main()

    output = capsys.readouterr()
    assert "[backup] verified" in output.out
    assert "1 material object(s)" in output.out
    assert verify_backup(archive)["jobs"] == 1


def test_verify_normalizes_a_malformed_zip_to_backup_error(tmp_path):
    archive = tmp_path / "not-a-backup.zip"
    archive.write_bytes(b"not a zip")

    with pytest.raises(BackupError, match="invalid backup ZIP|cannot be verified"):
        verify_backup(archive)


def test_verify_rejects_duplicate_zip_member_names(conn, tmp_path):
    archive = tmp_path / "evidence.zip"
    create_backup(conn, archive, created_at=NOW)
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(archive, "a") as bundle:
            bundle.writestr("manifest.json", b"{}")

    with pytest.raises(BackupError, match="duplicate member names"):
        verify_backup(archive)


def test_verify_preflights_zip64_directory_counts_before_zipfile_allocation(
    tmp_path, monkeypatch
):
    archive = tmp_path / "zip64-count.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one.txt", b"one")
    raw = archive.read_bytes()
    eocd_offset = raw.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    eocd = list(struct.unpack_from("<4s4H2LH", raw, eocd_offset))
    directory_size = eocd[5]
    directory_offset = eocd[6]
    eocd[3] = 0xFFFF
    eocd[4] = 0xFFFF
    fake_count = 3
    zip64_record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        fake_count,
        fake_count,
        directory_size,
        directory_offset,
    )
    zip64_locator = struct.pack(
        "<4sLQL", b"PK\x06\x07", 0, eocd_offset, 1
    )
    archive.write_bytes(
        raw[:eocd_offset]
        + zip64_record
        + zip64_locator
        + struct.pack("<4s4H2LH", *eocd)
    )
    monkeypatch.setattr(backup, "MAX_ARCHIVE_MEMBERS", 2)

    with pytest.raises(BackupError, match="too many members"):
        verify_backup(archive)


def test_verify_counts_actual_central_directory_entries_before_zipfile_allocation(
    tmp_path, monkeypatch
):
    archive = tmp_path / "forged-count.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("one.txt", b"one")
        bundle.writestr("two.txt", b"two")
    raw = bytearray(archive.read_bytes())
    eocd_offset = raw.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    struct.pack_into("<H", raw, eocd_offset + 8, 1)
    struct.pack_into("<H", raw, eocd_offset + 10, 1)
    archive.write_bytes(raw)

    def must_not_allocate(*_args, **_kwargs):
        pytest.fail("ZipFile must not run before the actual directory count is bounded")

    monkeypatch.setattr(backup.zipfile, "ZipFile", must_not_allocate)

    with pytest.raises(BackupError, match="entry count"):
        verify_backup(archive)


def test_verify_rejects_unsafe_aggregate_ratio_split_across_small_members(
    tmp_path, monkeypatch
):
    archive = tmp_path / "split-compression-bomb.zip"
    with zipfile.ZipFile(
        archive, "w", compression=zipfile.ZIP_DEFLATED
    ) as bundle:
        for index in range(8):
            bundle.writestr(f"part-{index}.bin", b"\0" * 1024)
    monkeypatch.setattr(backup, "MIN_RATIO_CHECK_BYTES", 4096)
    monkeypatch.setattr(backup, "MAX_COMPRESSION_RATIO", 2)

    with pytest.raises(BackupError, match="aggregate compression ratio"):
        verify_backup(archive)


def test_verify_uses_one_open_file_for_preflight_and_zipfile(
    conn, tmp_path, monkeypatch
):
    archive = tmp_path / "single-handle.zip"
    create_backup(conn, archive, created_at=NOW)
    original_preflight = backup._preflight_archive
    original_zipfile = backup.zipfile.ZipFile
    seen = {}

    def recording_preflight(source):
        seen["preflight"] = source
        return original_preflight(source)

    def recording_zipfile(source, *args, **kwargs):
        seen["zipfile"] = source
        return original_zipfile(source, *args, **kwargs)

    monkeypatch.setattr(backup, "_preflight_archive", recording_preflight)
    monkeypatch.setattr(backup.zipfile, "ZipFile", recording_zipfile)

    verify_backup(archive)

    assert seen["preflight"] is seen["zipfile"]


@pytest.mark.parametrize(
    ("constant", "limit", "message", "with_material"),
    [
        ("MAX_ARCHIVE_MEMBERS", 1, "too many members", False),
        ("MAX_CENTRAL_DIRECTORY_BYTES", 1, "central directory", False),
        ("MAX_DATABASE_BYTES", 1, "database exceeds", False),
        ("MAX_MATERIAL_OBJECT_BYTES", 1, "material exceeds", True),
        ("MAX_TOTAL_UNCOMPRESSED_BYTES", 1, "uncompressed-size", False),
        ("MAX_COMPRESSION_RATIO", 0, "compression ratio", False),
    ],
)
def test_verify_enforces_resource_limits_before_extraction(
    conn, tmp_path, monkeypatch, constant, limit, message, with_material
):
    if with_material:
        _snapshot(conn)
    archive = tmp_path / f"{constant}.zip"
    create_backup(conn, archive, created_at=NOW)
    if constant == "MAX_COMPRESSION_RATIO":
        monkeypatch.setattr(backup, "MIN_RATIO_CHECK_BYTES", 1)
    monkeypatch.setattr(backup, constant, limit)

    with pytest.raises(BackupError, match=message):
        verify_backup(archive)
