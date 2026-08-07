"""Explicit, local interview stories and application answers.

Entries are reusable candidate-authored notes.  A separate versioned link marks relevance to a
current duplicate chain.  Only confirmed, linked entries are eligible for interview-prep context;
this module never generates claims or sends content externally.
"""

import hashlib
import json
from datetime import datetime, timezone


ENTRY_KINDS = ("story", "qa")
ENTRY_STATUSES = ("draft", "confirmed", "archived")
MAX_TITLE_CHARS = 240
MAX_PROMPT_CHARS = 4000
MAX_RESPONSE_CHARS = 12000
MAX_TAGS = 20
MAX_TAG_CHARS = 60
MAX_LIBRARY_ENTRIES = 500
MAX_CONTEXT_ENTRIES = 20
MAX_CONTEXT_CHARS = 50000


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, field, *, required=False, maximum):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return value or None


def _normalize(kind, title, prompt, response, tags):
    if kind not in ENTRY_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(ENTRY_KINDS)}")
    title = _text(title, "title", required=True, maximum=MAX_TITLE_CHARS)
    prompt = _text(prompt, "prompt", maximum=MAX_PROMPT_CHARS)
    response = _text(response, "response", required=True, maximum=MAX_RESPONSE_CHARS)
    if kind == "qa" and not prompt:
        raise ValueError("prompt is required for a qa entry")
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    if len(tags) > MAX_TAGS:
        raise ValueError(f"tags may contain at most {MAX_TAGS} items")
    clean = []
    seen = set()
    for tag in tags:
        normalized = _text(tag, "tag", required=True, maximum=MAX_TAG_CHARS)
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            clean.append(normalized)
    return kind, title, prompt, response, clean


def _entry(row):
    if row is None:
        return None
    value = dict(row)
    try:
        tags = json.loads(value.pop("tags_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        tags = []
    value["tags"] = tags if isinstance(tags, list) else []
    return value


def _clean_write(conn):
    if conn.in_transaction:
        raise RuntimeError("prep-library mutation requires a clean database connection")
    conn.execute("BEGIN IMMEDIATE")


def create_entry(conn, *, kind, title, prompt, response, tags):
    kind, title, prompt, response, tags = _normalize(
        kind, title, prompt, response, tags,
    )
    _clean_write(conn)
    try:
        count = conn.execute("SELECT COUNT(*) FROM prep_entries").fetchone()[0]
        if count >= MAX_LIBRARY_ENTRIES:
            raise ValueError(
                f"prep library is limited to {MAX_LIBRARY_ENTRIES} retained entries"
            )
        now = _now()
        cur = conn.execute(
            """INSERT INTO prep_entries
               (kind,title,prompt,response,tags_json,status,created_at,updated_at,
                confirmed_at,version)
               VALUES (?,?,?,?,?,'draft',?,?,NULL,1)""",
            (kind, title, prompt, response, json.dumps(tags, ensure_ascii=False), now, now),
        )
        row = conn.execute("SELECT * FROM prep_entries WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.commit()
        return _entry(row)
    except Exception:
        conn.rollback()
        raise


def list_entries(conn, *, include_archived=False, limit=200):
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIBRARY_ENTRIES:
        raise ValueError(f"limit must be from 1 to {MAX_LIBRARY_ENTRIES}")
    where = "" if include_archived else "WHERE status<>'archived'"
    rows = conn.execute(
        f"""SELECT * FROM prep_entries {where}
             ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                      updated_at DESC,id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_entry(row) for row in rows]


def _load_for_write(conn, entry_id, expected_version):
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise ValueError("entry_id must be an integer")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise ValueError("expected_version must be an integer")
    row = conn.execute("SELECT * FROM prep_entries WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        raise ValueError("prep entry not found")
    if row["version"] != expected_version:
        raise ValueError("prep entry changed since this view loaded; refresh and retry")
    return row


def update_entry(conn, entry_id, *, expected_version, kind, title, prompt, response, tags):
    kind, title, prompt, response, tags = _normalize(
        kind, title, prompt, response, tags,
    )
    _clean_write(conn)
    try:
        current = _load_for_write(conn, entry_id, expected_version)
        if current["status"] == "archived":
            raise ValueError("restore an archived prep entry before editing it")
        conn.execute(
            """UPDATE prep_entries
                  SET kind=?,title=?,prompt=?,response=?,tags_json=?,status='draft',
                      updated_at=?,confirmed_at=NULL,version=version+1
                WHERE id=? AND version=?""",
            (kind, title, prompt, response, json.dumps(tags, ensure_ascii=False), _now(),
             entry_id, expected_version),
        )
        row = conn.execute("SELECT * FROM prep_entries WHERE id=?", (entry_id,)).fetchone()
        conn.commit()
        return _entry(row)
    except Exception:
        conn.rollback()
        raise


def _transition(conn, entry_id, expected_version, *, source, target):
    _clean_write(conn)
    try:
        current = _load_for_write(conn, entry_id, expected_version)
        if current["status"] != source:
            raise ValueError(f"prep entry must be {source} before it can become {target}")
        now = _now()
        confirmed_at = now if target == "confirmed" else None
        conn.execute(
            """UPDATE prep_entries SET status=?,updated_at=?,confirmed_at=?,version=version+1
                WHERE id=? AND version=?""",
            (target, now, confirmed_at, entry_id, expected_version),
        )
        row = conn.execute("SELECT * FROM prep_entries WHERE id=?", (entry_id,)).fetchone()
        conn.commit()
        return _entry(row)
    except Exception:
        conn.rollback()
        raise


def confirm_entry(conn, entry_id, *, expected_version):
    return _transition(
        conn, entry_id, expected_version, source="draft", target="confirmed",
    )


def archive_entry(conn, entry_id, *, expected_version):
    _clean_write(conn)
    try:
        current = _load_for_write(conn, entry_id, expected_version)
        if current["status"] == "archived":
            raise ValueError("prep entry is already archived")
        conn.execute(
            """UPDATE prep_entries SET status='archived',updated_at=?,confirmed_at=NULL,
                       version=version+1 WHERE id=? AND version=?""",
            (_now(), entry_id, expected_version),
        )
        row = conn.execute("SELECT * FROM prep_entries WHERE id=?", (entry_id,)).fetchone()
        conn.commit()
        return _entry(row)
    except Exception:
        conn.rollback()
        raise


def restore_entry(conn, entry_id, *, expected_version):
    return _transition(
        conn, entry_id, expected_version, source="archived", target="draft",
    )


def _current(conn, row):
    if row is None or "job_url" not in row.keys():
        raise ValueError("posting not found")
    current = conn.execute("SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)).fetchone()
    if current is None:
        raise ValueError("posting no longer exists")
    return current, current["repost_of"] or current["job_url"]


def _link_rows(conn, root, entry_id):
    return conn.execute(
        """SELECT pr.job_url,pr.linked,pr.version
             FROM prep_entry_roles pr JOIN jobs owner ON owner.job_url=pr.job_url
            WHERE pr.entry_id=? AND COALESCE(owner.repost_of,owner.job_url)=?
            ORDER BY pr.job_url""",
        (entry_id, root),
    ).fetchall()


def _link_state(conn, root, entry_id):
    members = [row[0] for row in conn.execute(
        """SELECT job_url FROM jobs WHERE job_url=? OR repost_of=?
             ORDER BY job_url""",
        (root, root),
    ).fetchall()]
    rows = _link_rows(conn, root, entry_id)
    linked = any(bool(row["linked"]) for row in rows)
    # Membership is part of the token even when this entry has no link row. Otherwise a stale
    # page for the retained canonical can survive a merge/split and disclose the entry to a
    # newly joined role without the user reviewing that changed scope.
    encoded = json.dumps({
        "members": members,
        "links": [(row["job_url"], row["linked"], row["version"]) for row in rows],
    }, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    revision = hashlib.sha256(encoded).hexdigest()[:20]
    return {"linked": linked, "revision": revision, "root": root}


def role_entries(conn, row, *, include_unconfirmed=False):
    _current_row, root = _current(conn, row)
    status = "" if include_unconfirmed else "AND pe.status='confirmed'"
    rows = conn.execute(
        f"""SELECT DISTINCT pe.* FROM prep_entries pe
              JOIN prep_entry_roles pr ON pr.entry_id=pe.id AND pr.linked=1
              JOIN jobs owner ON owner.job_url=pr.job_url
             WHERE COALESCE(owner.repost_of,owner.job_url)=? {status}
             ORDER BY pe.updated_at DESC,pe.id DESC LIMIT ?""",
        (root, MAX_LIBRARY_ENTRIES),
    ).fetchall()
    return [_entry(item) for item in rows]


def role_entry_choices(conn, row, *, include_archived=True, limit=MAX_LIBRARY_ENTRIES):
    _current_row, root = _current(conn, row)
    if not isinstance(limit, int) or not 1 <= limit <= MAX_LIBRARY_ENTRIES:
        raise ValueError(f"limit must be from 1 to {MAX_LIBRARY_ENTRIES}")
    where = "" if include_archived else "WHERE status<>'archived'"
    # The role-link picker needs identity and state, not private prompts/responses/tags.
    entries = [dict(item) for item in conn.execute(
        f"""SELECT id,kind,title,status FROM prep_entries {where}
             ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                      updated_at DESC,id DESC LIMIT ?""",
        (limit,),
    ).fetchall()]
    for entry in entries:
        entry.update({f"link_{key}": value for key, value in
                      _link_state(conn, root, entry["id"]).items()})
    return entries


def set_role_link(conn, row, entry_id, *, linked, expected_linked,
                  expected_revision, expected_root):
    if not isinstance(entry_id, int) or isinstance(entry_id, bool):
        raise ValueError("entry_id must be an integer")
    if not isinstance(linked, bool) or not isinstance(expected_linked, bool):
        raise ValueError("linked and expected_linked must be booleans")
    _clean_write(conn)
    try:
        current, root = _current(conn, row)
        if root != expected_root:
            raise ValueError("role chain changed since this view loaded; refresh and retry")
        entry = conn.execute("SELECT status FROM prep_entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            raise ValueError("prep entry not found")
        if entry["status"] == "archived" and linked:
            raise ValueError("restore the prep entry before linking it")
        state = _link_state(conn, root, entry_id)
        if state["linked"] != expected_linked or state["revision"] != expected_revision:
            raise ValueError("prep link changed since this view loaded; refresh and retry")
        now = _now()
        if linked != state["linked"]:
            if linked:
                conn.execute(
                    """INSERT INTO prep_entry_roles
                       (entry_id,job_url,interaction_url,linked,linked_at,version)
                       VALUES (?,?,?,?,?,1)
                       ON CONFLICT(entry_id,job_url) DO UPDATE SET
                         interaction_url=excluded.interaction_url,linked=1,
                         linked_at=excluded.linked_at,version=prep_entry_roles.version+1""",
                    (entry_id, root, current["job_url"], 1, now),
                )
            else:
                conn.execute(
                    """UPDATE prep_entry_roles SET linked=0,version=version+1
                        WHERE entry_id=? AND linked=1 AND job_url IN
                              (SELECT job_url FROM jobs
                                WHERE job_url=? OR repost_of=?)""",
                    (entry_id, root, root),
                )
        result = _link_state(conn, root, entry_id)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def confirmed_context(conn, row):
    entries = role_entries(conn, row)
    included = []
    total = 0
    omitted = 0
    for entry in entries:
        size = (len(entry["title"]) + len(entry["prompt"] or "")
                + len(entry["response"]) + sum(len(tag) for tag in entry["tags"]))
        if len(included) >= MAX_CONTEXT_ENTRIES or total + size > MAX_CONTEXT_CHARS:
            omitted += 1
            continue
        included.append(entry)
        total += size
    warning = None
    if omitted:
        warning = f"{omitted} linked prep library entr{'y was' if omitted == 1 else 'ies were'} omitted by context limits"
    return {"entries": included, "warning": warning}
