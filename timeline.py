"""Bounded, read-only activity timeline assembled from current role-chain evidence."""

from datetime import datetime, timezone

from chain import effective_decision


DEFAULT_TIMELINE_LIMIT = 200
MAX_TIMELINE_LIMIT = 500
MAX_TIMELINE_CHAIN_MEMBERS = 5_000
_MAX_DETAIL = 2000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MIN_TIME_KEY = -62_135_596_800.0


def _text(value):
    return " ".join(str(value or "").split())


def _detail(value):
    value = _text(value)
    return value if len(value) <= _MAX_DETAIL else value[:_MAX_DETAIL - 1] + "…"


def _item(kind, occurred_at, title, detail="", *, recorded_at="", identity=""):
    return {
        "kind": kind,
        "occurred_at": str(occurred_at or ""),
        "title": str(title),
        "detail": _detail(detail),
        "_recorded_at": str(recorded_at or occurred_at or ""),
        "_identity": str(identity),
    }


def _members(conn, row):
    current = conn.execute(
        "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
    ).fetchone()
    if current is None:
        raise ValueError("posting no longer exists")
    root = current["repost_of"] or current["job_url"]
    member_count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_url=? OR repost_of=?", (root, root)
    ).fetchone()[0]
    if member_count > MAX_TIMELINE_CHAIN_MEMBERS:
        raise ValueError(
            f"role chain exceeds the timeline member limit "
            f"({MAX_TIMELINE_CHAIN_MEMBERS})"
        )
    members = conn.execute(
        "SELECT * FROM jobs WHERE job_url=? OR repost_of=? ORDER BY first_seen,job_url",
        (root, root),
    ).fetchall()
    return current, members, root


def _count(conn, sql, root):
    return int(conn.execute(sql, (root,)).fetchone()[0])


def _collect(conn, current, members, root, limit):
    items = []

    for posting in members:
        if posting["first_seen"]:
            source = posting["source"] or "unknown source"
            summary = f"{source}: {posting['title'] or '(no title)'} — " \
                      f"{posting['company'] or '(no company)'}"
            items.append(_item(
                "posting", posting["first_seen"], "Posting discovered", summary,
                identity=f"posting:{posting['job_url']}",
            ))

    decision = effective_decision(conn, current)
    if decision["app_status"] and decision["status_date"]:
        status = decision["app_status"]
        items.append(_item(
            "decision", decision["status_date"], f"Marked {status}",
            identity=f"decision:{status}",
        ))
    if decision["reject"] and decision["filter_date"]:
        items.append(_item(
            "decision", decision["filter_date"], "Role rejected",
            f"Gate: {decision['filter_gate'] or 'other'}",
            identity="decision:reject",
        ))

    total = len(items)
    total += _count(
        conn,
        """SELECT COUNT(*) FROM app_events e JOIN jobs k ON k.job_url=e.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?""",
        root,
    )
    for event in conn.execute(
        """SELECT e.id,e.event_type,e.event_date,e.note,e.created_at
             FROM app_events e JOIN jobs k ON k.job_url=e.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?
            ORDER BY timeline_instant(e.event_date) DESC,e.event_date DESC,
                     timeline_instant(e.created_at) DESC,e.created_at DESC,e.id DESC LIMIT ?""",
        (root, limit),
    ):
        title = _text(event["event_type"]).replace("_", " ").capitalize()
        items.append(_item(
            "event", event["event_date"], title, event["note"],
            recorded_at=event["created_at"], identity=f"event:{event['id']}",
        ))

    total += _count(
        conn,
        """SELECT COUNT(*) FROM application_materials am
             JOIN jobs k ON k.job_url=am.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?""",
        root,
    )
    for material in conn.execute(
        """SELECT am.id,am.kind,am.original_name,am.attached_at
             FROM application_materials am JOIN jobs k ON k.job_url=am.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?
            ORDER BY timeline_instant(am.attached_at) DESC,am.attached_at DESC,am.id DESC LIMIT ?""",
        (root, limit),
    ):
        label = _text(material["kind"]).replace("_", " ").upper()
        items.append(_item(
            "material", material["attached_at"], f"{label} attached",
            material["original_name"], identity=f"material:{material['id']}",
        ))

    total += _count(
        conn,
        """SELECT COUNT(*) FROM job_contacts c JOIN jobs k ON k.job_url=c.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?""",
        root,
    )
    for contact in conn.execute(
        """SELECT c.id,c.name,c.role,c.kind,c.created_at
             FROM job_contacts c JOIN jobs k ON k.job_url=c.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?
            ORDER BY timeline_instant(c.created_at) DESC,c.created_at DESC,c.id DESC LIMIT ?""",
        (root, limit),
    ):
        detail = " · ".join(
            bit for bit in (_text(contact["name"]), _text(contact["kind"]),
                            _text(contact["role"])) if bit
        )
        items.append(_item(
            "contact", contact["created_at"], "Contact added", detail,
            identity=f"contact:{contact['id']}",
        ))

    total += _count(
        conn,
        """SELECT COUNT(*) FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?""",
        root,
    )
    for task in conn.execute(
        """SELECT t.id,t.title,t.created_at
             FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?
            ORDER BY timeline_instant(t.created_at) DESC,t.created_at DESC,t.id DESC LIMIT ?""",
        (root, limit),
    ):
        items.append(_item(
            "task_created", task["created_at"], "Task added",
            task["title"],
            identity=f"task:{task['id']}:created",
        ))
    total += _count(
        conn,
        """SELECT COUNT(*) FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND t.closed_at IS NOT NULL
              AND t.status IN ('completed','cancelled')""",
        root,
    )
    for task in conn.execute(
        """SELECT t.id,t.title,t.status,t.closed_at
             FROM job_tasks t JOIN jobs k ON k.job_url=t.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND t.closed_at IS NOT NULL
              AND t.status IN ('completed','cancelled')
            ORDER BY timeline_instant(t.closed_at) DESC,t.closed_at DESC,t.id DESC LIMIT ?""",
        (root, limit),
    ):
        items.append(_item(
            "task_closed", task["closed_at"], f"Task {task['status']}",
            task["title"], identity=f"task:{task['id']}:closed",
        ))

    total += _count(
        conn,
        """SELECT COUNT(*) FROM job_interviews i JOIN jobs k ON k.job_url=i.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?""",
        root,
    )
    for interview in conn.execute(
        """SELECT i.id,i.created_at
             FROM job_interviews i JOIN jobs k ON k.job_url=i.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=?
            ORDER BY timeline_instant(i.created_at) DESC,i.created_at DESC,i.id DESC LIMIT ?""",
        (root, limit),
    ):
        items.append(_item(
            "interview_created", interview["created_at"], "Interview scheduled",
            identity=f"interview:{interview['id']}:created",
        ))
    total += _count(
        conn,
        """SELECT COUNT(*) FROM job_interviews i JOIN jobs k ON k.job_url=i.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND i.updated_at<>i.created_at""",
        root,
    )
    for interview in conn.execute(
        """SELECT i.id,i.title,i.starts_at,i.status,i.updated_at
             FROM job_interviews i JOIN jobs k ON k.job_url=i.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND i.updated_at<>i.created_at
            ORDER BY timeline_instant(i.updated_at) DESC,i.updated_at DESC,i.id DESC LIMIT ?""",
        (root, limit),
    ):
        title = ("Interview cancelled" if interview["status"] == "cancelled"
                 else "Interview schedule updated")
        items.append(_item(
            "interview_updated", interview["updated_at"], title,
            f"{interview['title']} · starts {interview['starts_at']}",
            identity=f"interview:{interview['id']}:updated",
        ))

    total += _count(
        conn,
        """SELECT COUNT(*) FROM role_stars s JOIN jobs k ON k.job_url=s.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND s.starred=1""",
        root,
    )
    for star in conn.execute(
        """SELECT s.job_url,s.starred_at FROM role_stars s
             JOIN jobs k ON k.job_url=s.job_url
            WHERE COALESCE(k.repost_of,k.job_url)=? AND s.starred=1
            ORDER BY timeline_instant(s.starred_at) DESC,s.starred_at DESC,s.job_url DESC LIMIT ?""",
        (root, limit),
    ):
        items.append(_item(
            "star", star["starred_at"], "Role starred",
            identity=f"star:{star['job_url']}",
        ))
    return items, total


def _time_key(value, *, naive_timezone=None):
    """Map stored timestamps to one UTC number for SQL and Python ordering.

    Historical producers stored naive ``datetime.now()`` values, while newer concerns use
    aware UTC values. A naive value therefore means the machine's local wall clock at write
    time; treating it as UTC would reverse cross-concern events in non-UTC timezones.
    ``naive_timezone`` exists for deterministic boundary tests.
    """
    value = str(value or "")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = (parsed.replace(tzinfo=naive_timezone) if naive_timezone is not None
                      else parsed.astimezone())
        return (parsed.astimezone(timezone.utc) - _EPOCH).total_seconds()
    except (ValueError, OSError, OverflowError):
        return _MIN_TIME_KEY


def role_timeline(conn, row, *, limit=DEFAULT_TIMELINE_LIMIT):
    """Return newest-first activity for ``row``'s current chain.

    Only timestamped facts retained by the existing tables are emitted. Mutable tables do
    not become invented append-only history: an open task has no close item, an unstarred
    tombstone has no fabricated unstar time, and interview updates are represented only by
    the one ``updated_at`` value the schema actually retains.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TIMELINE_LIMIT:
        raise ValueError(f"timeline limit must be 1..{MAX_TIMELINE_LIMIT}")
    # Per-category LIMIT must use exactly the same instant semantics as the final Python sort;
    # otherwise a mixed legacy-local/aware-UTC category can discard its newest row too early.
    conn.create_function("timeline_instant", 1, _time_key, deterministic=True)
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    try:
        current, members, root = _members(conn, row)
        items, total = _collect(conn, current, members, root, limit)
        items.sort(
            key=lambda item: (
                _time_key(item["occurred_at"]), _time_key(item["_recorded_at"]),
                item["_identity"],
            ),
            reverse=True,
        )
        visible = items[:limit]
        for item in visible:
            item.pop("_recorded_at", None)
            item.pop("_identity", None)
        result = {"items": visible, "total": total, "truncated": total > limit}
        if owns_snapshot:
            conn.commit()
        return result
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise
