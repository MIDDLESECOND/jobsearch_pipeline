"""Verified backups of the SQLite + application-material evidence unit.

The database snapshot is created first with SQLite's online-backup API.  The immutable
material objects are then selected from that snapshot's catalog, so a concurrently-added
attachment can never produce a bundle whose database expects bytes from a different point
in time.  Creation fails closed on missing, corrupt, or path-escaping material metadata.

This module deliberately does not restore archives.  Verification is non-destructive; a
future restore workflow must add an explicit stop/preflight/rollback protocol rather than
turning a validated ZIP into an implicit overwrite operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from materials import material_root


BACKUP_FORMAT = "jobsearch_pipeline_evidence_backup"
BACKUP_FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 50_000
MAX_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_DATABASE_BYTES = 8 * 1024 * 1024 * 1024
MAX_MATERIAL_OBJECT_BYTES = 8 * 1024 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_UNCOMPRESSED_BYTES
MAX_COMPRESSION_RATIO = 1_000
MIN_RATIO_CHECK_BYTES = 1024 * 1024
_DATABASE_MEMBER = "jobs.db"
_MANIFEST_MEMBER = "manifest.json"
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_STRUCT = struct.Struct("<4s4H2LH")
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_EOCD_STRUCT = struct.Struct("<4sQ2H2L4Q")
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_LOCATOR_STRUCT = struct.Struct("<4sLQL")
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_CENTRAL_DIRECTORY_STRUCT = struct.Struct("<4s6H3L5H2L")


class BackupError(ValueError):
    """The evidence unit cannot be backed up or the supplied archive is invalid."""


def _hash_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _hash_path(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        return _hash_stream(handle)


def _database_path(conn) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        # sqlite3.Row and tuples both support positional access.
        if row[1] == "main":
            if not row[2]:
                raise BackupError("evidence backup requires a file-backed SQLite database")
            return Path(row[2]).resolve()
    raise BackupError("SQLite main database path is unavailable")


def _safe_material_path(root: Path, stored_path) -> tuple[Path, str]:
    if not isinstance(stored_path, str) or not stored_path.strip():
        raise BackupError("material object has no stored path")
    root = root.resolve()
    candidate = (root / stored_path).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise BackupError(
            f"material path is outside the material store: {stored_path!r}"
        ) from exc
    normalized = PurePosixPath(*relative.parts).as_posix()
    if normalized in ("", "."):
        raise BackupError(f"material path does not name a file: {stored_path!r}")
    return candidate, normalized


def _check_snapshot(conn) -> None:
    result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        detail = "; ".join(str(item) for item in result[:5])
        raise BackupError(f"SQLite integrity check failed: {detail}")
    missing = conn.execute(
        """SELECT DISTINCT am.object_sha256
             FROM application_materials am
             LEFT JOIN material_objects mo ON mo.sha256=am.object_sha256
            WHERE mo.sha256 IS NULL
            ORDER BY am.object_sha256"""
    ).fetchall()
    if missing:
        raise BackupError(
            "application material link has no object metadata: "
            + ", ".join(str(row[0]) for row in missing[:5])
        )
    orphaned = conn.execute(
        """SELECT am.id,
                  CASE WHEN owner.job_url IS NULL THEN 'owner' ELSE 'interaction' END
             FROM application_materials am
             LEFT JOIN jobs owner ON owner.job_url=am.job_url
             LEFT JOIN jobs interaction ON interaction.job_url=am.interaction_url
            WHERE owner.job_url IS NULL OR interaction.job_url IS NULL
            ORDER BY am.id"""
    ).fetchall()
    if orphaned:
        detail = ", ".join(f"{row[0]} ({row[1]})" for row in orphaned[:5])
        raise BackupError(
            "application material link references a missing posting: " + detail
        )


def _counts(conn) -> dict[str, int]:
    return {
        "jobs": int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
        "material_links": int(
            conn.execute("SELECT COUNT(*) FROM application_materials").fetchone()[0]
        ),
        "material_objects": int(
            conn.execute("SELECT COUNT(*) FROM material_objects").fetchone()[0]
        ),
    }


def _summary(manifest: dict) -> dict[str, object]:
    counts = manifest["counts"]
    return {
        "created_at": manifest["created_at"],
        "jobs": counts["jobs"],
        "material_links": counts["material_links"],
        "material_objects": counts["material_objects"],
        "database_sha256": manifest["database"]["sha256"],
    }


def _preflight_archive(handle) -> int:
    """Bound central-directory work before ZipFile allocates one object per member."""
    archive_size = os.fstat(handle.fileno()).st_size
    if archive_size > MAX_ARCHIVE_BYTES:
        raise BackupError(
            f"backup archive exceeds {MAX_ARCHIVE_BYTES} compressed bytes"
        )
    tail_size = min(
        archive_size,
        _EOCD_STRUCT.size + 65_535 + _ZIP64_LOCATOR_STRUCT.size,
    )
    tail_start = archive_size - tail_size
    handle.seek(tail_start)
    tail = handle.read(tail_size)
    search_end = len(tail)
    end_record = None
    while True:
        index = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            break
        if index + _EOCD_STRUCT.size <= len(tail):
            candidate = _EOCD_STRUCT.unpack_from(tail, index)
            comment_size = candidate[-1]
            if index + _EOCD_STRUCT.size + comment_size == len(tail):
                end_record = candidate
                break
        search_end = index
    if end_record is None:
        raise BackupError("invalid backup ZIP: end-of-central-directory record is missing")
    eocd_offset = tail_start + index
    (_, disk_number, directory_disk, entries_on_disk, entries_total,
     directory_size, directory_offset, _) = end_record
    if disk_number != 0 or directory_disk != 0 or entries_on_disk != entries_total:
        raise BackupError("multi-disk ZIP archives are not supported")
    directory_boundary = eocd_offset
    locator_offset = eocd_offset - _ZIP64_LOCATOR_STRUCT.size
    locator = None
    if locator_offset >= 0:
        handle.seek(locator_offset)
        raw_locator = handle.read(_ZIP64_LOCATOR_STRUCT.size)
        if raw_locator.startswith(_ZIP64_LOCATOR_SIGNATURE):
            if len(raw_locator) != _ZIP64_LOCATOR_STRUCT.size:
                raise BackupError("ZIP64 locator is truncated")
            locator = _ZIP64_LOCATOR_STRUCT.unpack(raw_locator)
    if locator is not None:
        _, zip64_disk, zip64_offset, total_disks = locator
        if zip64_disk != 0 or total_disks != 1:
            raise BackupError("multi-disk ZIP64 archives are not supported")
        if zip64_offset + _ZIP64_EOCD_STRUCT.size > locator_offset:
            raise BackupError("ZIP64 end-of-central-directory offset is invalid")
        handle.seek(zip64_offset)
        raw_zip64 = handle.read(_ZIP64_EOCD_STRUCT.size)
        if len(raw_zip64) != _ZIP64_EOCD_STRUCT.size:
            raise BackupError("ZIP64 end-of-central-directory record is truncated")
        zip64 = _ZIP64_EOCD_STRUCT.unpack(raw_zip64)
        (signature, record_size, _, _, zip64_disk_number, zip64_directory_disk,
         zip64_entries_on_disk, zip64_entries_total, zip64_directory_size,
         zip64_directory_offset) = zip64
        if signature != _ZIP64_EOCD_SIGNATURE or record_size < 44:
            raise BackupError("ZIP64 end-of-central-directory record is invalid")
        if zip64_offset + 12 + record_size != locator_offset:
            raise BackupError("ZIP64 end-of-central-directory extent is invalid")
        if (
            zip64_disk_number != 0
            or zip64_directory_disk != 0
            or zip64_entries_on_disk != zip64_entries_total
        ):
            raise BackupError("multi-disk ZIP64 archives are not supported")
        if entries_total != 0xFFFF and entries_total != zip64_entries_total:
            raise BackupError("ZIP64 member count disagrees with the traditional directory")
        if directory_size != 0xFFFFFFFF and directory_size != zip64_directory_size:
            raise BackupError("ZIP64 directory size disagrees with the traditional directory")
        if directory_offset != 0xFFFFFFFF and directory_offset != zip64_directory_offset:
            raise BackupError("ZIP64 directory offset disagrees with the traditional directory")
        entries_total = zip64_entries_total
        directory_size = zip64_directory_size
        directory_offset = zip64_directory_offset
        directory_boundary = zip64_offset
    elif (
        entries_total == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise BackupError("ZIP64 directory metadata is missing its locator")
    if entries_total > MAX_ARCHIVE_MEMBERS:
        raise BackupError(
            f"backup ZIP has too many members ({entries_total} > {MAX_ARCHIVE_MEMBERS})"
        )
    if directory_size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise BackupError(
            "backup ZIP central directory exceeds the supported size limit"
        )
    if directory_offset + directory_size != directory_boundary:
        raise BackupError("backup ZIP central-directory extent is invalid")
    handle.seek(directory_offset)
    remaining = directory_size
    actual_entries = 0
    while remaining:
        if remaining < _CENTRAL_DIRECTORY_STRUCT.size:
            raise BackupError("backup ZIP central-directory entry is truncated")
        fixed = handle.read(_CENTRAL_DIRECTORY_STRUCT.size)
        if len(fixed) != _CENTRAL_DIRECTORY_STRUCT.size:
            raise BackupError("backup ZIP central-directory entry is truncated")
        fields = _CENTRAL_DIRECTORY_STRUCT.unpack(fixed)
        if fields[0] != _CENTRAL_DIRECTORY_SIGNATURE:
            raise BackupError("backup ZIP central-directory entry is invalid")
        variable_size = fields[10] + fields[11] + fields[12]
        entry_size = _CENTRAL_DIRECTORY_STRUCT.size + variable_size
        if entry_size > remaining:
            raise BackupError("backup ZIP central-directory entry exceeds its boundary")
        actual_entries += 1
        if actual_entries > MAX_ARCHIVE_MEMBERS:
            raise BackupError(
                f"backup ZIP has too many members ({actual_entries} > "
                f"{MAX_ARCHIVE_MEMBERS})"
            )
        handle.seek(variable_size, os.SEEK_CUR)
        remaining -= entry_size
    if actual_entries != entries_total:
        raise BackupError(
            "backup ZIP central-directory entry count does not match its declaration"
        )
    return actual_entries


def _check_zip_resource_limits(infos) -> None:
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise BackupError(
            f"backup ZIP has too many members ({len(infos)} > {MAX_ARCHIVE_MEMBERS})"
        )
    total = 0
    total_compressed = 0
    for info in infos:
        if info.file_size < 0 or info.compress_size < 0:
            raise BackupError(f"backup ZIP member has an invalid size: {info.filename!r}")
        total += info.file_size
        total_compressed += info.compress_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupError("backup ZIP exceeds the total uncompressed-size limit")
        if info.file_size >= MIN_RATIO_CHECK_BYTES:
            ratio = (float("inf") if info.compress_size == 0
                     else info.file_size / info.compress_size)
            if ratio > MAX_COMPRESSION_RATIO:
                raise BackupError(
                    f"backup ZIP member has an unsafe compression ratio: {info.filename!r}"
                )
    if total >= MIN_RATIO_CHECK_BYTES:
        aggregate_ratio = (float("inf") if total_compressed == 0
                           else total / total_compressed)
        if aggregate_ratio > MAX_COMPRESSION_RATIO:
            raise BackupError("backup ZIP has an unsafe aggregate compression ratio")


def create_backup(conn, destination, *, created_at: datetime | None = None):
    """Create an atomic ZIP backup and return its verified summary.

    Existing destinations are never replaced.  The caller must provide a clean connection;
    silently committing or excluding its uncommitted work would make the backup boundary
    unknowable.
    """
    if conn.in_transaction:
        raise BackupError("cannot back up a connection with an uncommitted transaction")
    _database_path(conn)  # validate before creating destination directories
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created_at = created_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        raise BackupError("backup created_at must include a timezone")
    created_iso = created_at.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")

    with TemporaryDirectory(prefix=".jobsearch-backup-", dir=destination.parent) as raw_tmp:
        temp_dir = Path(raw_tmp)
        snapshot_path = temp_dir / _DATABASE_MEMBER
        snapshot_writer = sqlite3.connect(snapshot_path)
        try:
            conn.backup(snapshot_writer)
        finally:
            snapshot_writer.close()

        snapshot = sqlite3.connect(snapshot_path)
        snapshot.row_factory = sqlite3.Row
        try:
            _check_snapshot(snapshot)
            counts = _counts(snapshot)
            catalog = snapshot.execute(
                """SELECT sha256,size_bytes,stored_path
                     FROM material_objects ORDER BY sha256"""
            ).fetchall()
        finally:
            snapshot.close()
        if len(catalog) + 2 > MAX_ARCHIVE_MEMBERS:
            raise BackupError("material catalog exceeds the backup member limit")

        root = material_root(conn=conn)
        material_entries = []
        seen_members = set()
        for row in catalog:
            digest = row["sha256"]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise BackupError(f"material object has an invalid SHA-256: {digest!r}")
            try:
                expected_size = int(row["size_bytes"])
            except (TypeError, ValueError) as exc:
                raise BackupError(f"material {digest} has an invalid size") from exc
            if expected_size < 0:
                raise BackupError(f"material {digest} has a negative size")
            if expected_size > MAX_MATERIAL_OBJECT_BYTES:
                raise BackupError(
                    f"material {digest} exceeds the per-object backup size limit"
                )
            object_path, normalized = _safe_material_path(root, row["stored_path"])
            if not object_path.is_file():
                raise BackupError(f"material object is missing: {normalized}")
            actual_digest, actual_size = _hash_path(object_path)
            if actual_size != expected_size:
                raise BackupError(
                    f"material object size mismatch: {normalized} "
                    f"(expected {expected_size}, got {actual_size})"
                )
            if actual_digest != digest:
                raise BackupError(f"material object checksum mismatch: {normalized}")
            archive_path = f"application_materials/{normalized}"
            if archive_path in seen_members:
                raise BackupError(f"multiple material objects share archive path {archive_path!r}")
            seen_members.add(archive_path)
            material_entries.append(
                {
                    "sha256": digest,
                    "size_bytes": expected_size,
                    "stored_path": normalized,
                    "archive_path": archive_path,
                    "source_path": object_path,
                }
            )

        database_digest, database_size = _hash_path(snapshot_path)
        if database_size > MAX_DATABASE_BYTES:
            raise BackupError("SQLite snapshot exceeds the backup database size limit")
        manifest = {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": created_iso,
            "database": {
                "archive_path": _DATABASE_MEMBER,
                "sha256": database_digest,
                "size_bytes": database_size,
            },
            "materials": [
                {key: value for key, value in item.items() if key != "source_path"}
                for item in material_entries
            ],
            "counts": counts,
        }
        manifest_bytes = (json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n").encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BackupError("backup manifest exceeds the supported size limit")
        total_size = (
            database_size + len(manifest_bytes)
            + sum(item["size_bytes"] for item in material_entries)
        )
        if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise BackupError("evidence unit exceeds the total backup size limit")

        temporary_archive = temp_dir / "bundle.zip"
        with zipfile.ZipFile(
            temporary_archive,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as bundle:
            bundle.write(snapshot_path, _DATABASE_MEMBER)
            for item in material_entries:
                bundle.write(item["source_path"], item["archive_path"])
            bundle.writestr(_MANIFEST_MEMBER, manifest_bytes)
        verified = verify_backup(temporary_archive)
        try:
            # Same parent filesystem (the temporary directory lives beside destination):
            # a hard link publishes the completed archive atomically and fails if another
            # process created the requested name after our initial existence check.
            os.link(temporary_archive, destination)
        except FileExistsError as exc:
            raise BackupError(f"backup destination already exists: {destination}") from exc
    return verified


def _json_no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError(f"manifest contains duplicate key {key!r}")
        result[key] = value
    return result


def _safe_archive_member(name) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise BackupError(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise BackupError(f"unsafe archive member name: {name!r}")
    return path.as_posix()


def _manifest_int(value, label) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupError(f"manifest {label} must be a non-negative integer")
    return value


def _manifest_digest(value, label) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise BackupError(f"manifest {label} is not a SHA-256 digest")
    return value


def _verify_backup_file(source):
    """Validate archive structure, hashes, SQLite integrity, and DB/material agreement."""
    expected_entries = _preflight_archive(source)
    try:
        bundle = zipfile.ZipFile(source, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"invalid backup ZIP: {exc}") from exc

    with bundle:
        infos = bundle.infolist()
        if len(infos) != expected_entries:
            raise BackupError("backup ZIP central-directory entry count is inconsistent")
        _check_zip_resource_limits(infos)
        names = [_safe_archive_member(info.filename) for info in infos]
        if len(names) != len(set(names)):
            raise BackupError("backup ZIP contains duplicate member names")
        if any(info.is_dir() for info in infos):
            raise BackupError("backup ZIP must not contain directory entries")
        info_by_name = dict(zip(names, infos))
        manifest_info = info_by_name.get(_MANIFEST_MEMBER)
        if manifest_info is None:
            raise BackupError("backup manifest.json is missing")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise BackupError("backup manifest exceeds the supported size limit")
        try:
            manifest = json.loads(
                bundle.read(manifest_info).decode("utf-8"),
                object_pairs_hook=_json_no_duplicate_keys,
            )
        except BackupError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"backup manifest is invalid JSON: {exc}") from exc
        if not isinstance(manifest, dict):
            raise BackupError("backup manifest must be a JSON object")
        if manifest.get("format") != BACKUP_FORMAT:
            raise BackupError("backup manifest format is not recognized")
        if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
            raise BackupError(
                f"unsupported backup format version: {manifest.get('format_version')!r}"
            )
        if not isinstance(manifest.get("created_at"), str):
            raise BackupError("manifest created_at must be a string")

        database = manifest.get("database")
        materials = manifest.get("materials")
        counts = manifest.get("counts")
        if not isinstance(database, dict) or not isinstance(materials, list):
            raise BackupError("manifest database/materials shape is invalid")
        if not isinstance(counts, dict):
            raise BackupError("manifest counts must be an object")
        expected_counts = {
            key: _manifest_int(counts.get(key), f"counts.{key}")
            for key in ("jobs", "material_links", "material_objects")
        }
        if database.get("archive_path") != _DATABASE_MEMBER:
            raise BackupError("manifest database archive path must be jobs.db")
        database_size = _manifest_int(database.get("size_bytes"), "database.size_bytes")
        database_digest = _manifest_digest(
            database.get("sha256"), "database.sha256"
        )

        material_catalog = {}
        expected_members = {_MANIFEST_MEMBER, _DATABASE_MEMBER}
        for index, item in enumerate(materials):
            if not isinstance(item, dict):
                raise BackupError(f"manifest materials[{index}] must be an object")
            digest = _manifest_digest(item.get("sha256"), f"materials[{index}].sha256")
            size = _manifest_int(item.get("size_bytes"), f"materials[{index}].size_bytes")
            stored_path = _safe_archive_member(item.get("stored_path"))
            archive_path = _safe_archive_member(item.get("archive_path"))
            if archive_path != f"application_materials/{stored_path}":
                raise BackupError(
                    f"manifest material path does not match stored path: {archive_path!r}"
                )
            if digest in material_catalog:
                raise BackupError(f"manifest repeats material digest {digest}")
            if archive_path in expected_members:
                raise BackupError(f"manifest repeats archive member {archive_path!r}")
            material_catalog[digest] = {
                "sha256": digest,
                "size_bytes": size,
                "stored_path": stored_path,
                "archive_path": archive_path,
            }
            expected_members.add(archive_path)
        if set(names) != expected_members:
            raise BackupError("backup ZIP members do not match the manifest")
        if expected_counts["material_objects"] != len(material_catalog):
            raise BackupError("manifest material count does not match its catalog")

        database_info = info_by_name[_DATABASE_MEMBER]
        if database_info.file_size > MAX_DATABASE_BYTES:
            raise BackupError("backup database exceeds the supported size limit")
        if database_info.file_size != database_size:
            raise BackupError("database size does not match the manifest")
        with bundle.open(database_info) as handle:
            actual_digest, actual_size = _hash_stream(handle)
        if actual_size != database_size or actual_digest != database_digest:
            raise BackupError("database checksum does not match the manifest")
        for item in material_catalog.values():
            info = info_by_name[item["archive_path"]]
            if info.file_size > MAX_MATERIAL_OBJECT_BYTES:
                raise BackupError(
                    f"backup material exceeds the per-object size limit: {item['stored_path']}"
                )
            if info.file_size != item["size_bytes"]:
                raise BackupError(
                    f"material size does not match the manifest: {item['stored_path']}"
                )
            with bundle.open(info) as handle:
                actual_digest, actual_size = _hash_stream(handle)
            if actual_size != item["size_bytes"] or actual_digest != item["sha256"]:
                raise BackupError(
                    f"material checksum does not match the manifest: {item['stored_path']}"
                )

        with TemporaryDirectory(prefix="jobsearch-backup-verify-") as raw_tmp:
            extracted = Path(raw_tmp) / _DATABASE_MEMBER
            with bundle.open(database_info) as source, extracted.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            snapshot = sqlite3.connect(extracted)
            snapshot.row_factory = sqlite3.Row
            try:
                try:
                    _check_snapshot(snapshot)
                    actual_counts = _counts(snapshot)
                    database_catalog = {
                        row["sha256"]: {
                            "sha256": row["sha256"],
                            "size_bytes": int(row["size_bytes"]),
                            "stored_path": PurePosixPath(
                                *Path(row["stored_path"]).parts
                            ).as_posix(),
                            "archive_path": "application_materials/" + PurePosixPath(
                                *Path(row["stored_path"]).parts
                            ).as_posix(),
                        }
                        for row in snapshot.execute(
                            "SELECT sha256,size_bytes,stored_path "
                            "FROM material_objects ORDER BY sha256"
                        )
                    }
                except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
                    raise BackupError(f"backup database catalog is invalid: {exc}") from exc
            finally:
                snapshot.close()
        if actual_counts != expected_counts:
            raise BackupError("manifest counts do not match the backup database")
        if database_catalog != material_catalog:
            raise BackupError("manifest does not match the database catalog")
    return _summary(manifest)


def _verify_backup_unwrapped(archive):
    archive = Path(archive).expanduser().resolve()
    if not archive.is_file():
        raise BackupError(f"backup archive not found: {archive}")
    with archive.open("rb") as source:
        return _verify_backup_file(source)


def verify_backup(archive):
    """Validate a backup and normalize malformed-container failures to BackupError."""
    try:
        return _verify_backup_unwrapped(archive)
    except BackupError:
        raise
    except (
        OSError,
        RuntimeError,
        sqlite3.DatabaseError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise BackupError(f"backup archive cannot be verified: {exc}") from exc
