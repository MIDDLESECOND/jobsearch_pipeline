"""Explicit starred-role state, independent of model scoring and workflow decisions."""

from datetime import datetime, timedelta, timezone


def _root_url(row):
    return row["repost_of"] or row["job_url"]


def _chain_urls(conn, row):
    root = _root_url(row)
    return [item[0] for item in conn.execute(
        "SELECT job_url FROM jobs WHERE job_url=? OR repost_of=? ORDER BY job_url",
        (root, root),
    )]


def _begin_write(conn):
    if conn.in_transaction:
        raise RuntimeError("star mutation requires a clean database connection")
    conn.execute("BEGIN IMMEDIATE")


def _next_starred_at(conn):
    """Return a strictly increasing UTC timestamp for deterministic recency order."""
    now = datetime.now(timezone.utc)
    latest = conn.execute(
        "SELECT MAX(starred_at) FROM role_stars WHERE starred=1"
    ).fetchone()[0]
    if latest:
        try:
            previous = datetime.fromisoformat(latest)
            if previous.tzinfo is not None and now <= previous:
                now = previous + timedelta(microseconds=1)
        except (TypeError, ValueError):
            pass
    return now.isoformat()


def _next_version(conn):
    """Allocate a repository-wide monotonic mutation version under the write lock."""
    latest = conn.execute("SELECT COALESCE(MAX(version),0) FROM role_stars").fetchone()[0]
    return latest + 1


def star_summaries(conn, rows):
    """Return current-chain star state keyed by each input posting URL in one snapshot."""
    posting_urls = {row["job_url"] for row in rows}
    out = {
        url: {"starred": False, "starred_at": None, "star_version": 0}
        for url in posting_urls
    }
    if not posting_urls:
        return out
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
        by_root = {
            root: {"starred_at": None, "star_version": 0}
            for root in set(current_roots.values())
        }
        root_list = list(by_root)
        for start in range(0, len(root_list), 800):
            chunk = root_list[start:start + 800]
            qs = ",".join("?" * len(chunk))
            found = conn.execute(
                f"""SELECT COALESCE(k.repost_of,k.job_url) AS current_root,
                           MAX(CASE WHEN s.starred=1 THEN s.starred_at END) AS starred_at,
                           MAX(s.version) AS star_version
                    FROM role_stars s JOIN jobs k ON k.job_url=s.job_url
                    WHERE COALESCE(k.repost_of,k.job_url) IN ({qs})
                    GROUP BY COALESCE(k.repost_of,k.job_url)""",
                tuple(chunk),
            ).fetchall()
            for item in found:
                by_root[item["current_root"]] = {
                    "starred_at": item["starred_at"],
                    "star_version": item["star_version"],
                }
        for url, root in current_roots.items():
            summary = by_root[root]
            out[url] = {
                "starred": summary["starred_at"] is not None,
                **summary,
            }
        if owns_snapshot:
            conn.commit()
        return out
    except Exception:
        if owns_snapshot:
            conn.rollback()
        raise


def set_starred(conn, row, starred, *, expected_starred, expected_version):
    """Set absolute current-chain star state with stale-tab and merge/unlink protection."""
    if not isinstance(starred, bool):
        raise ValueError("starred must be a boolean")
    if not isinstance(expected_starred, bool):
        raise ValueError("expected_starred must be a boolean")
    if (isinstance(expected_version, bool) or not isinstance(expected_version, int)
            or expected_version < 0):
        raise ValueError("expected_version must be a non-negative integer")
    _begin_write(conn)
    try:
        current = conn.execute(
            "SELECT * FROM jobs WHERE job_url=?", (row["job_url"],)
        ).fetchone()
        if current is None:
            raise ValueError("posting no longer exists")
        urls = _chain_urls(conn, current)
        qs = ",".join("?" * len(urls))
        existing = conn.execute(
            f"""SELECT MAX(CASE WHEN starred=1 THEN starred_at END) AS starred_at,
                       COALESCE(MAX(version),0) AS star_version
                FROM role_stars WHERE job_url IN ({qs})""",
            tuple(urls),
        ).fetchone()
        current_starred = existing["starred_at"] is not None
        current_version = existing["star_version"]
        if current_starred != expected_starred or current_version != expected_version:
            raise ValueError("star changed; refresh and retry")
        if starred and not current_starred:
            version = _next_version(conn)
            conn.execute(
                """INSERT INTO role_stars(job_url,starred_at,starred,version)
                   VALUES (?,?,1,?)
                   ON CONFLICT(job_url) DO UPDATE SET
                     starred_at=excluded.starred_at,starred=1,version=excluded.version""",
                (_root_url(current), _next_starred_at(conn), version),
            )
        elif not starred and current_starred:
            version = _next_version(conn)
            conn.execute(
                f"UPDATE role_stars SET starred=0,version=? "
                f"WHERE job_url IN ({qs}) AND starred=1",
                (version, *urls),
            )
        summary = star_summaries(conn, [current])[current["job_url"]]
        result = {**summary, "affected": sorted(urls)}
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
