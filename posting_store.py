"""One normalized insertion path for every discovered or manually entered posting."""

from chain import _fingerprint, _find_repost, _norm_company, _norm_title
from states import STATUS_NEW


def insert_posting(conn, *, url, title, company, location, search_name, tier,
                   date_posted, first_seen, salary_min, salary_max, description, source):
    """Normalize, fingerprint, repost-link, and insert one unseen posting.

    ``ON CONFLICT(job_url) DO NOTHING`` ignores only the primary-key duplicate.  It is
    deliberately not ``INSERT OR IGNORE``: other constraint failures must stay visible.
    Returns ``(inserted, repost_of)`` where inserted is 0 or 1.
    """
    norm_company = _norm_company(company)
    norm_title = _norm_title(title)
    fingerprint = _fingerprint(company, location)
    repost_of = _find_repost(conn, fingerprint, norm_title, exclude_url=url)
    cur = conn.execute(
        """INSERT INTO jobs
           (job_url, title, company, location, search_name, tier, date_posted,
            first_seen, salary_min, salary_max, description, status,
            norm_company, norm_title, fingerprint, repost_of, source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(job_url) DO NOTHING""",
        (url, title, company, location, search_name, tier, date_posted, first_seen,
         salary_min, salary_max, description, STATUS_NEW, norm_company, norm_title,
         fingerprint, repost_of, source),
    )
    return cur.rowcount, repost_of
