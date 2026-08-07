"""Chain-scoped interview schedules and local iCalendar export.

Schedules are explicit user-entered facts.  They are keyed to the role canonical at
write time and mapped through that posting's current chain root on read, matching the
ownership model used by tasks, contacts, packets, and application events.  This module
does not write an external calendar or infer that an interview actually occurred.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


INTERVIEW_SCHEDULED = "scheduled"
INTERVIEW_CANCELLED = "cancelled"
INTERVIEW_MODES = ("video", "phone", "onsite", "other")

_MAX_TITLE = 240
_MAX_LOCATION = 500
_MAX_URL = 2048
_MAX_NOTE = 4000


def _root_url(row):
    return row["repost_of"] or row["job_url"]


def _chain_urls(conn, row):
    root = _root_url(row)
    return [item[0] for item in conn.execute(
        "SELECT job_url FROM jobs WHERE job_url=? OR repost_of=? ORDER BY job_url",
        (root, root),
    )]


def _text(value, field, limit, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _starts_at(value):
    raw = _text(value, "starts_at", 64, required=True)
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        raise ValueError("starts_at must be ISO 8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("starts_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _aware_now(value):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("now must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("duration_minutes must be an integer")
    if not 15 <= value <= 480:
        raise ValueError("duration_minutes must be from 15 to 480")
    return value


def _mode(value):
    mode = _text(value, "mode", 20, required=True).lower()
    if mode not in INTERVIEW_MODES:
        raise ValueError(f"mode must be one of {list(INTERVIEW_MODES)}")
    return mode


def _meeting_url(value):
    value = _text(value, "meeting_url", _MAX_URL)
    if not value:
        return ""
    parsed = urlparse(value)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or any(char.isspace() for char in value)):
        raise ValueError("meeting_url must be an http(s) URL")
    return value


def _fields(*, title, starts_at, duration_minutes, mode, location, meeting_url, note):
    return {
        "title": _text(title, "title", _MAX_TITLE, required=True),
        "starts_at": _starts_at(starts_at),
        "duration_minutes": _duration(duration_minutes),
        "mode": _mode(mode),
        "location": _text(location, "location", _MAX_LOCATION),
        "meeting_url": _meeting_url(meeting_url),
        "note": _text(note, "note", _MAX_NOTE),
    }


def _as_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "starts_at": row["starts_at"],
        "duration_minutes": row["duration_minutes"],
        "mode": row["mode"],
        "location": row["location"],
        "meeting_url": row["meeting_url"],
        "note": row["note"],
        "status": row["status"],
        "interaction_url": row["interaction_url"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row["version"],
    }


def _begin_write(conn):
    if conn.in_transaction:
        raise RuntimeError("interview mutation requires a clean database connection")
    conn.execute("BEGIN IMMEDIATE")


def _require_applied_chain(conn, row):
    urls = _chain_urls(conn, row)
    qs = ",".join("?" * len(urls))
    applied = conn.execute(
        f"SELECT 1 FROM jobs WHERE job_url IN ({qs}) AND app_status='applied' LIMIT 1",
        tuple(urls),
    ).fetchone()
    if applied is None:
        raise ValueError("interviews require an applied chain")


def add_interview(conn, row, *, title: object, starts_at: object,
                  duration_minutes: object, mode: object, location: object = "",
                  meeting_url: object = "", note: object = ""):
    """Add an interview and return a transaction-consistent chain snapshot."""
    values = _fields(
        title=title, starts_at=starts_at, duration_minutes=duration_minutes,
        mode=mode, location=location, meeting_url=meeting_url, note=note,
    )
    now = datetime.now(timezone.utc).isoformat()
    _begin_write(conn)
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            raise ValueError("posting no longer exists")
        _require_applied_chain(conn, current)
        cur = conn.execute(
            """INSERT INTO job_interviews
               (job_url,interaction_url,title,starts_at,duration_minutes,mode,location,
                meeting_url,note,status,created_at,updated_at,version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (_root_url(current), current["job_url"], values["title"], values["starts_at"],
             values["duration_minutes"], values["mode"], values["location"] or None,
             values["meeting_url"] or None, values["note"] or None,
             INTERVIEW_SCHEDULED, now, now),
        )
        stored = conn.execute(
            "SELECT * FROM job_interviews WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        result = {
            "interview": _as_dict(stored),
            "interviews": chain_interviews(conn, current),
        }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def change_interview(conn, row, interview_id, action, *, expected_version,
                     title: object = None, starts_at: object = None,
                     duration_minutes: object = None, mode: object = None,
                     location: object = "", meeting_url: object = "", note: object = ""):
    """Update or cancel one current-chain interview with optimistic concurrency."""
    if isinstance(interview_id, bool) or not isinstance(interview_id, int):
        raise ValueError("interview_id must be an integer")
    if action not in ("update", "cancel"):
        raise ValueError("interview action must be update or cancel")
    if (isinstance(expected_version, bool) or not isinstance(expected_version, int)
            or expected_version < 0):
        raise ValueError("expected_version must be a non-negative integer")
    values = None
    if action == "update":
        values = _fields(
            title=title, starts_at=starts_at, duration_minutes=duration_minutes,
            mode=mode, location=location, meeting_url=meeting_url, note=note,
        )

    _begin_write(conn)
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            raise ValueError("posting no longer exists")
        _require_applied_chain(conn, current)
        urls = _chain_urls(conn, current)
        qs = ",".join("?" * len(urls))
        stored = conn.execute(
            f"SELECT * FROM job_interviews WHERE id=? AND job_url IN ({qs})",
            (interview_id, *urls),
        ).fetchone()
        if stored is None:
            conn.rollback()
            return None
        if stored["version"] != expected_version:
            raise ValueError("interview changed; refresh and retry")
        if stored["status"] != INTERVIEW_SCHEDULED:
            raise ValueError("interview is not scheduled")
        now = datetime.now(timezone.utc).isoformat()
        if action == "cancel":
            changed = conn.execute(
                """UPDATE job_interviews SET status=?,updated_at=?,version=version+1
                   WHERE id=? AND version=?""",
                (INTERVIEW_CANCELLED, now, interview_id, expected_version),
            )
        else:
            assert values is not None
            changed = conn.execute(
                """UPDATE job_interviews
                   SET title=?,starts_at=?,duration_minutes=?,mode=?,location=?,meeting_url=?,
                       note=?,updated_at=?,version=version+1
                   WHERE id=? AND version=?""",
                (values["title"], values["starts_at"], values["duration_minutes"],
                 values["mode"], values["location"] or None,
                 values["meeting_url"] or None, values["note"] or None, now,
                 interview_id, expected_version),
            )
        if changed.rowcount != 1:
            raise RuntimeError("interview version changed while holding the write transaction")
        updated = conn.execute(
            "SELECT * FROM job_interviews WHERE id=?", (interview_id,)
        ).fetchone()
        result = {
            "interview": _as_dict(updated),
            "interviews": chain_interviews(conn, current),
        }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def interview_summaries(conn, rows, *, now=None):
    """Future schedules keyed by input posting URL, resolved in one current-chain snapshot."""
    posting_urls = {row["job_url"] for row in rows}
    out = {url: [] for url in posting_urls}
    if not posting_urls:
        return out
    cutoff = _aware_now(now).isoformat(timespec="seconds")
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        current_roots = {}
        url_list = list(posting_urls)
        for start in range(0, len(url_list), 800):
            chunk = url_list[start:start + 800]
            qs = ",".join("?" * len(chunk))
            current = conn.execute(
                f"SELECT job_url,COALESCE(repost_of,job_url) AS root "
                f"FROM jobs WHERE job_url IN ({qs})",
                tuple(chunk),
            ).fetchall()
            current_roots.update({item["job_url"]: item["root"] for item in current})
        schedules_by_root = {root: [] for root in set(current_roots.values())}
        root_list = list(schedules_by_root)
        for start in range(0, len(root_list), 800):
            chunk = root_list[start:start + 800]
            qs = ",".join("?" * len(chunk))
            found = conn.execute(
                f"""SELECT COALESCE(k.repost_of,k.job_url) AS current_root,i.*
                    FROM job_interviews i JOIN jobs k ON k.job_url=i.job_url
                    WHERE COALESCE(k.repost_of,k.job_url) IN ({qs})
                      AND i.status=? AND i.starts_at>=?
                    ORDER BY i.starts_at ASC,i.id ASC""",
                (*chunk, INTERVIEW_SCHEDULED, cutoff),
            ).fetchall()
            for item in found:
                schedules_by_root[item["current_root"]].append(_as_dict(item))
        for url, root in current_roots.items():
            out[url] = schedules_by_root[root]
        if owns_snapshot:
            conn.commit()
        return out
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise


def chain_interviews(conn, row, *, include_cancelled=False):
    """Read a refreshed chain under one snapshot; optionally include cancelled history."""
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            result = []
        else:
            urls = _chain_urls(conn, current)
            qs = ",".join("?" * len(urls))
            status_sql = "" if include_cancelled else "AND status=?"
            params = [*urls] + ([] if include_cancelled else [INTERVIEW_SCHEDULED])
            found = conn.execute(
                f"""SELECT * FROM job_interviews WHERE job_url IN ({qs}) {status_sql}
                    ORDER BY starts_at ASC,id ASC""",
                tuple(params),
            ).fetchall()
            result = [_as_dict(item) for item in found]
        if owns_snapshot:
            conn.commit()
        return result
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise


def _ics_text(value):
    value = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def _ics_time(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fold_ics_line(line):
    """Fold one RFC 5545 content line without splitting a UTF-8 code point."""
    parts = []
    current = []
    octets = 0
    limit = 75
    for char in line:
        width = len(char.encode("utf-8"))
        if current and octets + width > limit:
            parts.append("".join(current))
            current = [char]
            octets = width
            # A continuation line begins with one space, leaving 74 content octets.
            limit = 74
        else:
            current.append(char)
            octets += width
    parts.append("".join(current))
    return "\r\n ".join(parts)


def interview_ics(conn, row, interview_id, *, now=None):
    """Render one scheduled interview as a local-download iCalendar document."""
    if isinstance(interview_id, bool) or not isinstance(interview_id, int):
        raise ValueError("interview_id must be an integer")
    stamp = _aware_now(now)
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            result = None
        else:
            urls = _chain_urls(conn, current)
            qs = ",".join("?" * len(urls))
            stored = conn.execute(
                f"SELECT * FROM job_interviews WHERE id=? AND job_url IN ({qs})",
                (interview_id, *urls),
            ).fetchone()
            if stored is None:
                result = None
            else:
                if stored["status"] != INTERVIEW_SCHEDULED:
                    raise ValueError("calendar export requires a scheduled interview")
                starts = datetime.fromisoformat(stored["starts_at"])
                ends = starts + timedelta(minutes=stored["duration_minutes"])
                summary = f'{stored["title"]} — {current["title"]} at {current["company"]}'
                description = [
                    f'Mode: {stored["mode"]}', f'Posting: {stored["interaction_url"]}',
                ]
                if stored["meeting_url"]:
                    description.append(f'Meeting: {stored["meeting_url"]}')
                if stored["note"]:
                    description.append(stored["note"])
                location = stored["location"] or stored["meeting_url"] or ""
                lines = [
                    "BEGIN:VCALENDAR", "VERSION:2.0",
                    "PRODID:-//Job Search Pipeline//Interview Schedule//EN",
                    "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
                    f'UID:jobsearch-pipeline-interview-{stored["id"]}@local',
                    f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}",
                    f"DTSTART:{_ics_time(starts.isoformat())}",
                    f"DTEND:{_ics_time(ends.isoformat())}",
                    f"SUMMARY:{_ics_text(summary)}",
                    f"DESCRIPTION:{_ics_text(chr(10).join(description))}",
                ]
                if location:
                    lines.append(f"LOCATION:{_ics_text(location)}")
                lines.extend(["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"])
                result = "\r\n".join(_fold_ics_line(line) for line in lines) + "\r\n"
        if owns_snapshot:
            conn.commit()
        return result
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise
