"""Validated, explicit intake of job postings found outside configured fetch sources."""

from datetime import datetime
from urllib.parse import urlsplit

from core import parse_iso
from posting_store import insert_posting


SOURCE_MANUAL = "manual"
_MAX_URL = 4096
_MAX_TITLE = 500
_MAX_COMPANY = 500
_MAX_LOCATION = 500
_MAX_SEARCH_NAME = 500


class PostingAlreadyExists(ValueError):
    """The exact posting URL is already present and was deliberately not overwritten."""


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


def _posting_url(value):
    value = _text(value, "job_url", _MAX_URL, required=True)
    if "\\" in value or any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ValueError("job_url must be a well-formed absolute URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # force validation of an explicitly supplied port
    except ValueError:
        raise ValueError("job_url must be a well-formed absolute URL") from None
    if parsed.scheme.lower() not in ("http", "https") or not hostname:
        raise ValueError("job_url must be an absolute http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("job_url must not contain credentials")
    return value


def _posted_date(value, *, today):
    value = _text(value, "date_posted", 64)
    if not value:
        return ""
    parsed = parse_iso(value)
    if parsed is None or not parsed[1]:
        raise ValueError("date_posted must be an ISO calendar date")
    posted = parsed[0].date()
    if posted > today:
        raise ValueError("date_posted cannot be in the future")
    return posted.isoformat()


def _salary(value, field):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a non-negative number") from None
    if number < 0 or number != number or number == float("inf"):
        raise ValueError(f"{field} must be a non-negative number")
    return number


def _search_track(value, searches):
    name = _text(value, "search_name", _MAX_SEARCH_NAME, required=True)
    if not isinstance(searches, list):
        raise ValueError("configured searches must be a list")
    if not searches:
        if name != "manual intake":
            raise ValueError("ATS-only config requires the manual intake search track")
        return name, "manual"
    matches = [
        item for item in searches
        if (isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item["name"].strip() == name)
    ]
    if not matches:
        raise ValueError("search_name must identify a configured search")
    if len(matches) != 1:
        raise ValueError("configured search_name is ambiguous")
    match = matches[0]
    tier = match.get("tier", "primary")
    if not isinstance(tier, str) or not tier.strip():
        raise ValueError("configured search tier must be a non-empty string")
    return match["name"], tier.strip()


def add_manual_posting(conn, values, *, searches, max_description_chars, now=None):
    """Insert one user-supplied role without fetching, evaluating, or overwriting.

    The new row starts at ``status='new'``.  The next normal pipeline run applies the
    current deterministic filters, repost skips, and evaluation flow in their established
    order. This function owns its transaction so caller work cannot be committed by it.
    """
    if not isinstance(values, dict):
        raise ValueError("intake values must be an object")
    if isinstance(max_description_chars, bool) or not isinstance(max_description_chars, int):
        raise ValueError("max_description_chars must be a positive integer")
    if max_description_chars <= 0:
        raise ValueError("max_description_chars must be a positive integer")
    stamp = now or datetime.now()
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    url = _posting_url(values.get("job_url"))
    title = _text(values.get("title"), "title", _MAX_TITLE, required=True)
    company = _text(values.get("company"), "company", _MAX_COMPANY, required=True)
    search_name, tier = _search_track(values.get("search_name"), searches)
    location = _text(values.get("location"), "location", _MAX_LOCATION)
    date_posted = _posted_date(values.get("date_posted"), today=stamp.date())
    salary_min = _salary(values.get("salary_min"), "salary_min")
    salary_max = _salary(values.get("salary_max"), "salary_max")
    if salary_min is not None and salary_max is not None and salary_min > salary_max:
        raise ValueError("salary_min cannot exceed salary_max")
    description = _text(
        values.get("description"), "description", max_description_chars
    )
    if conn.in_transaction:
        raise RuntimeError("manual intake requires a clean database connection")
    first_seen = stamp.isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM jobs WHERE job_url=?", (url,)).fetchone():
            raise PostingAlreadyExists(f"posting URL already exists: {url}")
        inserted, _ = insert_posting(
            conn,
            url=url,
            title=title,
            company=company,
            location=location,
            search_name=search_name,
            tier=tier,
            date_posted=date_posted,
            first_seen=first_seen,
            salary_min=salary_min,
            salary_max=salary_max,
            description=description,
            source=SOURCE_MANUAL,
        )
        if inserted != 1:
            raise PostingAlreadyExists(f"posting URL already exists: {url}")
        row = conn.execute("SELECT * FROM jobs WHERE job_url=?", (url,)).fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
