"""Bounded, read-only differences between stored posting and application-snapshot evidence."""

import difflib
import hashlib
import unicodedata
from datetime import datetime, timezone

from materials import jd_snapshot_records


JD_VERSION_LIMIT = 100
JD_DIFF_MAX_INPUT_CHARS = 50_000
JD_DIFF_MAX_LINES = 2_000
JD_DIFF_MAX_MATRIX = 4_000_000
JD_DIFF_MAX_OPS = 5_000
JD_DIFF_MAX_OUTPUT_CHARS = 100_000
JD_DIFF_MAX_CONTEXT = 10


class JDDiffTooLarge(ValueError):
    """A complete diff would exceed a declared CPU or output boundary."""


class JDEvidenceUnavailable(ValueError):
    """A selected immutable evidence object cannot be read and verified."""


def _time_key(value, *, naive_timezone=None):
    """Order version timestamps on one UTC clock (timeline._time_key's convention).

    Historical producers stored naive machine-local ``datetime.now()`` values, while
    fetch-side rows can carry aware instants; reading a naive value as UTC would misorder
    mixed versions by the machine's UTC offset and attach diffs to the wrong snapshot.
    ``naive_timezone`` exists for deterministic boundary tests.
    """
    value = str(value or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = (parsed.replace(tzinfo=naive_timezone) if naive_timezone is not None
                      else parsed.astimezone())
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OSError, OverflowError):
        return datetime.min


def _token(kind, identity):
    return hashlib.sha256(f"{kind}\0{identity}".encode("utf-8")).hexdigest()


def _normalize(text):
    if not isinstance(text, str):
        raise ValueError("selected JD version has no readable stored text")
    if len(text) > JD_DIFF_MAX_INPUT_CHARS:
        raise JDDiffTooLarge("selected JD text is too large for a complete bounded diff")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    previous_blank = True
    for raw in text.split("\n"):
        line = raw.rstrip()
        blank = not line
        if blank and previous_blank:
            continue
        lines.append(line)
        previous_blank = blank
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > JD_DIFF_MAX_LINES:
        raise JDDiffTooLarge("selected JD text has too many lines for a complete bounded diff")
    return lines


def _public(version):
    return {key: version[key] for key in (
        "id", "kind", "label", "source", "observed_at", "availability",
        "completeness", "title", "location", "possibly_truncated",
    )}


def _text_availability(text, current="available"):
    """Map every per-version comparison bound into listable availability metadata."""
    if current != "available":
        return current
    try:
        _normalize(text)
    except JDDiffTooLarge:
        return "too_large"
    return "available"


def _posting_version(row, description_cap):
    text = row["description"] or ""
    source = row["source"] or "unknown"
    if source == "adzuna":
        completeness = "source snippet (up to 500 characters)"
    elif description_cap and len(text) >= description_cap:
        completeness = "possibly truncated at the configured storage cap"
    else:
        completeness = "first-stored posting text; live-page completeness unknown"
    return {
        "id": _token("posting", row["job_url"]),
        "kind": "posting",
        "label": f"Stored posting · {source} · {row['first_seen'] or 'time unknown'}",
        "source": source,
        "observed_at": row["first_seen"],
        "availability": _text_availability(text),
        "completeness": completeness,
        "title": row["title"],
        "location": row["location"],
        "possibly_truncated": bool(
            source == "adzuna" or (description_cap and len(text) >= description_cap)
        ),
        "_identity": row["job_url"],
        "_tie": row["job_url"],
        "_text": text,
        "_metadata": {
            "title": row["title"], "company": row["company"],
            "location": row["location"], "salary_min": row["salary_min"],
            "salary_max": row["salary_max"],
        },
        "_metadata_available": {"title", "company", "location", "salary_min", "salary_max"},
    }


def _snapshot_version(record):
    availability = _text_availability(record["_text"], record["storage_status"])
    source = record["posting_source"] or "unknown"
    if record["format"] != "snapshot_v1":
        completeness = "legacy snapshot format; header parsing and original completeness unknown"
    elif source == "adzuna":
        completeness = (
            "immutable application-time snapshot of the stored Adzuna source snippet "
            "(up to 500 characters)"
        )
    else:
        completeness = (
            "immutable application-time snapshot of the stored JD body; "
            "original website completeness unknown"
        )
    return {
        "id": _token("snapshot", record["attachment_id"]),
        "kind": "snapshot",
        "label": (
            f"Application snapshot · {source} · "
            f"{record['attached_at'] or 'time unknown'}"
        ),
        "source": source,
        "observed_at": record["attached_at"],
        "availability": availability,
        "completeness": completeness,
        "title": record["title"],
        "location": record["location"],
        "possibly_truncated": True if source == "adzuna" else None,
        "_identity": record["attachment_id"],
        "_tie": f"{record['attachment_id']:020d}",
        "_text": record["_text"],
        "_metadata": {
            "title": record["title"], "company": record["company"],
            "location": record["location"], "salary_min": None, "salary_max": None,
        },
        "_metadata_available": (
            {"title", "company", "location"}
            if record["format"] == "snapshot_v1" else set()
        ),
    }


def _enumerate(conn, row, cfg, description_cap):
    if row is None or "job_url" not in row.keys():
        raise ValueError("posting not found")
    current = conn.execute("SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)).fetchone()
    if current is None:
        raise ValueError("posting no longer exists")
    root = current["repost_of"] or current["job_url"]
    member_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_url=? OR repost_of=?", (root, root)
    ).fetchone()[0]
    if member_count > JD_VERSION_LIMIT:
        raise JDDiffTooLarge(f"role chain exceeds the JD version limit ({JD_VERSION_LIMIT})")
    postings = conn.execute(
        """SELECT * FROM jobs WHERE (job_url=? OR repost_of=?)
             AND description IS NOT NULL AND trim(description)<>''
             ORDER BY julianday(first_seen),first_seen,job_url""",
        (root, root),
    ).fetchall()
    posting_versions = [_posting_version(item, description_cap) for item in postings]
    snapshots = jd_snapshot_records(conn, current, cfg, limit=JD_VERSION_LIMIT + 1)
    if len(snapshots) > JD_VERSION_LIMIT:
        raise JDDiffTooLarge(f"role chain exceeds the JD snapshot limit ({JD_VERSION_LIMIT})")
    snapshot_versions = [_snapshot_version(item) for item in snapshots]
    if len(posting_versions) + len(snapshot_versions) > JD_VERSION_LIMIT:
        raise JDDiffTooLarge(f"role chain exceeds the JD version limit ({JD_VERSION_LIMIT})")
    versions = posting_versions + snapshot_versions
    versions.sort(key=lambda item: (
        _time_key(item["observed_at"]), item["kind"], item["_tie"],
    ))
    return root, versions


def _default_pair(versions):
    available_posts = [item for item in versions
                       if item["kind"] == "posting" and item["availability"] == "available"]
    available_snapshots = [item for item in versions
                           if item["kind"] == "snapshot" and item["availability"] == "available"]
    if available_snapshots and available_posts:
        left = available_snapshots[-1]
        right = next((item for item in reversed(available_posts)
                      if _normalize(item["_text"]) != _normalize(left["_text"])),
                     available_posts[-1])
        return left, right
    if len(available_posts) >= 2:
        left = available_posts[0]
        right = next((item for item in reversed(available_posts[1:])
                      if _normalize(item["_text"]) != _normalize(left["_text"])),
                     available_posts[-1])
        return left, right
    if len(available_snapshots) >= 2:
        return available_snapshots[-2], available_snapshots[-1]
    return None


def _metadata_changes(left, right):
    return [
        {"field": field, "before": left["_metadata"][field],
         "after": right["_metadata"][field]}
        for field in ("title", "company", "location", "salary_min", "salary_max")
        if (field in left["_metadata_available"] and field in right["_metadata_available"]
            and left["_metadata"][field] != right["_metadata"][field])
    ]


def _compare(left, right, context):
    unavailable = {left["availability"], right["availability"]} - {"available"}
    if "too_large" in unavailable:
        raise JDDiffTooLarge("selected JD text is too large for a complete bounded diff")
    if unavailable:
        raise JDEvidenceUnavailable("selected JD snapshot is missing or corrupt")
    before = _normalize(left["_text"])
    after = _normalize(right["_text"])
    if len(before) * len(after) >= JD_DIFF_MAX_MATRIX:
        raise JDDiffTooLarge("selected JD diff would exceed the comparison matrix limit")
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    opcodes = [item for item in matcher.get_opcodes() if item[0] != "equal"]
    if len(opcodes) > JD_DIFF_MAX_OPS:
        raise JDDiffTooLarge("selected JD diff would exceed the operation limit")
    hunks = []
    output_chars = 0
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in opcodes:
        old_lines = before[i1:i2]
        new_lines = after[j1:j2]
        if tag in ("replace", "delete"):
            removed += len(old_lines)
        if tag in ("replace", "insert"):
            added += len(new_lines)
        hunk = {
            "op": tag,
            "context_before": before[max(0, i1 - context):i1],
            "removed": old_lines,
            "added": new_lines,
            "context_after": before[i2:i2 + context],
        }
        output_chars += sum(len(line) for key in (
            "context_before", "removed", "added", "context_after"
        ) for line in hunk[key])
        if output_chars > JD_DIFF_MAX_OUTPUT_CHARS:
            raise JDDiffTooLarge("selected JD diff would exceed the output limit")
        hunks.append(hunk)
    return {
        "left": _public(left), "right": _public(right),
        "metadata_changes": _metadata_changes(left, right),
        "hunks": hunks,
        "stats": {"added_lines": added, "removed_lines": removed},
        "normalization": "Unicode NFC; normalized newlines; trailing whitespace removed; repeated blank lines collapsed",
        "same_after_normalization": not hunks,
        "complete": True,
    }


def _snapshot(conn, callback):
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        result = callback()
        if owns:
            conn.commit()
        return result
    except Exception:
        if owns:
            conn.rollback()
        raise


def jd_versions_bundle(conn, row, cfg=None, *, description_cap=None):
    def build():
        _, versions = _enumerate(conn, row, cfg, description_cap)
        default = _default_pair(versions)
        return {
            "ok": True,
            "versions": [_public(item) for item in versions],
            "default_left": default[0]["id"] if default else None,
            "default_right": default[1]["id"] if default else None,
            "definition": (
                "Stored posting observations and application snapshots; not a live-page check "
                "or semantic interpretation of employer intent."
            ),
        }
    return _snapshot(conn, build)


def jd_diff_bundle(conn, row, *, left_id=None, right_id=None, context=3,
                   cfg=None, description_cap=None):
    if (isinstance(context, bool) or not isinstance(context, int)
            or not 0 <= context <= JD_DIFF_MAX_CONTEXT):
        raise ValueError(f"context must be 0..{JD_DIFF_MAX_CONTEXT}")

    def build():
        _, versions = _enumerate(conn, row, cfg, description_cap)
        by_id = {item["id"]: item for item in versions}
        if left_id is None and right_id is None:
            pair = _default_pair(versions)
            if pair is None:
                return {"ok": True, "comparison": None}
            left, right = pair
        elif not isinstance(left_id, str) or not isinstance(right_id, str):
            raise ValueError("left and right version ids are required together")
        else:
            left = by_id.get(left_id)
            right = by_id.get(right_id)
            if left is None or right is None:
                raise ValueError("selected version is not in the current role chain")
            if left_id == right_id:
                raise ValueError("choose two different JD versions")
        return {"ok": True, "comparison": _compare(left, right, context)}
    return _snapshot(conn, build)
