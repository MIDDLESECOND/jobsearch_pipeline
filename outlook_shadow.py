"""Read-only Outlook job-alert discovery through Microsoft Graph.

This concern deliberately stops at a local, disposable report. It never changes mailbox
state, follows job links, inserts into ``jobs``, or invokes the evaluator. A later intake
decision therefore remains explicit and goes through the existing posting-store path.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from chain import _norm_title


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]
_MAX_LINKS_PER_MESSAGE = 100
_MAX_REPORT_ITEMS = 500
_MAX_SENDERS = 20
_MAX_GRAPH_PAGES = 100
_MAX_GRAPH_BYTES = 16 * 1024 * 1024
_MAX_GRAPH_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_MESSAGE_BODY_CHARS = 2 * 1024 * 1024
_MAX_CANDIDATES = 5000
_BATCH = 300
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SAFE_TENANT_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_SAFE_INDEED_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_SAFE_NUMERIC_JOB_ID_RE = re.compile(r"^[0-9]{3,30}$")
_SAFE_ROBERT_HALF_ID_RE = re.compile(r"^[0-9]{5}-[0-9]{10}-[A-Za-z]{4}$")
_ADZUNA_SUFFIXES = (
    "com",
    "co.uk",
    "com.au",
    "ca",
    "de",
    "fr",
    "nl",
    "pl",
    "it",
    "es",
    "at",
    "be",
    "ch",
    "in",
    "co.nz",
    "co.za",
    "sg",
    "com.br",
)
_WRAPPED_URL_KEYS = {
    "dest",
    "destination",
    "redirect",
    "redirect_url",
    "target",
    "u",
    "url",
}
_MAX_WRAPPER_DEPTH = 3
_GENERIC_ANCHOR_TEXT = {
    "apply",
    "apply now",
    "learn more",
    "see job",
    "view",
    "view job",
    "view jobs",
}
_NON_JOB_TEXT = (
    "email preference",
    "privacy",
    "sign in",
    "terms",
    "unsubscribe",
)


class OutlookShadowError(RuntimeError):
    """A safe, user-facing failure at the Outlook/Graph boundary."""


@dataclass(frozen=True)
class Candidate:
    source: str
    title: str | None
    url: str
    received_at: str
    classification: str = "unclassified"
    possible_matches: tuple[tuple[str, str], ...] = ()


class _AnchorParser(HTMLParser):
    def __init__(self, max_anchors=_MAX_LINKS_PER_MESSAGE):
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._max_anchors = max_anchors
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if (tag.lower() != "a" or self._href is not None
                or len(self.anchors) >= self._max_anchors):
            return
        values = dict(attrs)
        href = values.get("href")
        if isinstance(href, str):
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join(self._text)))
            self._href = None
            self._text = []


def validate_settings(raw):
    """Validate the optional ``settings.outlook_email`` block and apply bounded defaults."""
    if not isinstance(raw, dict):
        raise ValueError("settings.outlook_email must be a mapping")
    settings = dict(raw)
    client_id = settings.get("client_id")
    if (not isinstance(client_id, str) or not client_id.strip()
            or len(client_id.strip()) > 200):
        raise ValueError("settings.outlook_email.client_id is missing or empty")
    settings["client_id"] = client_id.strip()

    senders = settings.get("senders")
    if not isinstance(senders, list) or not senders:
        raise ValueError("settings.outlook_email.senders must be a non-empty list")
    if len(senders) > _MAX_SENDERS:
        raise ValueError(
            f"settings.outlook_email.senders accepts at most {_MAX_SENDERS} addresses"
        )
    clean_senders = []
    for sender in senders:
        if (not isinstance(sender, str) or len(sender.strip()) > 320
                or not _EMAIL_RE.fullmatch(sender.strip())):
            raise ValueError(
                "settings.outlook_email.senders entries must be exact email addresses"
            )
        address = sender.strip()
        if not any(existing.casefold() == address.casefold() for existing in clean_senders):
            clean_senders.append(address)
    settings["senders"] = clean_senders

    tenant = settings.get("tenant", "common")
    if (not isinstance(tenant, str) or len(tenant.strip()) > 255
            or not _SAFE_TENANT_RE.fullmatch(tenant.strip())):
        raise ValueError("settings.outlook_email.tenant is invalid")
    settings["tenant"] = tenant.strip()

    folder = settings.get("folder", "inbox")
    if (not isinstance(folder, str) or not folder.strip() or len(folder) > 512
            or any(ord(ch) < 32 for ch in folder)):
        raise ValueError("settings.outlook_email.folder is invalid")
    settings["folder"] = folder.strip()

    login_hint = settings.get("login_hint")
    if login_hint is not None:
        if (not isinstance(login_hint, str) or len(login_hint.strip()) > 320
                or not _EMAIL_RE.fullmatch(login_hint.strip())):
            raise ValueError("settings.outlook_email.login_hint must be an email address")
        settings["login_hint"] = login_hint.strip()

    for key, default, low, high in (
        ("days", 7, 1, 30),
        ("max_messages", 200, 1, 500),
    ):
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(
                f"settings.outlook_email.{key} must be an integer from {low} to {high}"
            )
        settings[key] = value
    if settings["max_messages"] < len(clean_senders):
        raise ValueError(
            "settings.outlook_email.max_messages must be at least the number of senders"
        )
    return settings


def _default_app_factory(client_id, **kwargs):
    try:
        import msal
    except ImportError as exc:
        raise OutlookShadowError(
            "Outlook authentication dependency is missing; run pip install -r requirements.txt"
        ) from exc
    try:
        return msal.PublicClientApplication(client_id, **kwargs)
    except ImportError as exc:
        raise OutlookShadowError(
            "Windows authentication broker is missing; run pip install -r requirements.txt"
        ) from exc


def acquire_access_token(settings, *, interactive=False, app_factory=None):
    """Use the Windows broker cache silently; show UI only for an explicit ``--login``."""
    tenant = settings.get("tenant", "common")
    if not isinstance(tenant, str) or not _SAFE_TENANT_RE.fullmatch(tenant):
        raise OutlookShadowError("Outlook tenant setting is invalid")
    if sys.platform != "win32" and app_factory is None:
        raise OutlookShadowError("Outlook shadow authentication currently requires Windows")
    factory = app_factory or _default_app_factory
    try:
        app = factory(
            settings["client_id"],
            authority=f"https://login.microsoftonline.com/{tenant}",
            enable_broker_on_windows=True,
        )
        login_hint = settings.get("login_hint")
        accounts = app.get_accounts(username=login_hint) if login_hint else app.get_accounts()
        if len(accounts) > 1 and not login_hint:
            raise OutlookShadowError(
                "multiple Microsoft accounts are available; set outlook_email.login_hint"
            )
        account = accounts[0] if accounts else None
        result = app.acquire_token_silent(SCOPES, account=account) if account else None
        if result and result.get("access_token"):
            return result["access_token"]
        if not interactive:
            raise OutlookShadowError(
                "no reusable Outlook login is available; run email-shadow once with --login"
            )
        kwargs = {"parent_window_handle": app.CONSOLE_WINDOW_HANDLE}
        if login_hint:
            kwargs["login_hint"] = login_hint
        result = app.acquire_token_interactive(SCOPES, **kwargs)
        if result and result.get("access_token"):
            return result["access_token"]
        error = result.get("error") if isinstance(result, dict) else None
        if not isinstance(error, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error):
            error = None
        suffix = f" ({error})" if error else ""
        raise OutlookShadowError(f"Outlook login did not complete{suffix}")
    except OutlookShadowError:
        raise
    except Exception as exc:
        # Broker exceptions can carry account identifiers or tenant details. Keep the raw
        # exception chained for a debugger, but never copy it into scheduled/user output.
        raise OutlookShadowError("Windows authentication broker failed") from exc


def _graph_page_is_safe(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "graph.microsoft.com"
        and port in (None, 443)
        and parsed.path.startswith("/v1.0/")
        and parsed.username is None
        and parsed.password is None
    )


def _graph_pagination_is_bound(url, *, expected_path, expected_query):
    """Keep an untrusted nextLink inside the original exact-sender mail query."""
    if not _graph_page_is_safe(url):
        return False
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (TypeError, ValueError):
        return False
    allowed = set(expected_query) | {"$skip", "$skiptoken"}
    if parsed.path != expected_path or not set(query).issubset(allowed):
        return False
    for key, expected in expected_query.items():
        if query.get(key) != [expected]:
            return False
    page_values = [
        value
        for key in ("$skip", "$skiptoken")
        for value in query.get(key, [])
    ]
    return bool(page_values) and all(page_values) and all(
        len(query.get(key, [])) <= 1 for key in ("$skip", "$skiptoken")
    )


def _message_from_address(message):
    from_value = message.get("from") if isinstance(message, dict) else None
    email = from_value.get("emailAddress") if isinstance(from_value, dict) else None
    address = email.get("address") if isinstance(email, dict) else None
    return address.strip().casefold() if isinstance(address, str) else ""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never forward Graph's bearer token through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_GRAPH_OPENER = build_opener(_NoRedirectHandler())


def _open_graph_no_redirect(request, timeout):
    return _GRAPH_OPENER.open(request, timeout=timeout)


def _read_graph_page(url, token, opener):
    if not _graph_page_is_safe(url):
        raise OutlookShadowError("Graph returned an unsafe pagination link")
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="html"',
        },
    )
    try:
        with opener(request, timeout=30) as response:
            raw = response.read(_MAX_GRAPH_BYTES + 1)
            if len(raw) > _MAX_GRAPH_BYTES:
                raise OutlookShadowError("Microsoft Graph mail response exceeded the size limit")
            payload = json.loads(raw)
    except OutlookShadowError:
        raise
    except HTTPError as exc:
        raise OutlookShadowError(f"Microsoft Graph mail read failed (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OutlookShadowError("Microsoft Graph mail read failed (network error)") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise OutlookShadowError("Microsoft Graph returned an invalid mail response") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
        raise OutlookShadowError("Microsoft Graph returned an invalid mail response")
    return payload, len(raw)


def fetch_messages(token, *, folder, senders, since, max_messages,
                   opener=_open_graph_no_redirect, stats=None):
    """Yield minimal exact-sender messages page-by-page, with bounded Graph pagination."""
    if since.tzinfo is None:
        raise ValueError("since must be timezone-aware")
    since_utc = since.astimezone(timezone.utc).replace(microsecond=0)
    since_text = since_utc.isoformat().replace("+00:00", "Z")
    if stats is None:
        stats = {}
    stats["truncated_senders"] = 0
    seen_pages = set()
    pages_read = 0
    total_bytes = 0
    folder_path = quote(folder, safe="")
    base_quota, quota_extra = divmod(max_messages, len(senders))
    for sender_index, sender in enumerate(senders):
        sender_limit = base_quota + (1 if sender_index < quota_extra else 0)
        sender_count = 0
        sender_truncated = False
        odata_filter = (
            f"receivedDateTime ge {since_text} and "
            f"from/emailAddress/address eq '{sender.replace(chr(39), chr(39) * 2)}'"
        )
        # ONE dict, used both to build the request and to bound every nextLink Graph hands
        # back. Writing it twice would let the two drift on any edit, and the drift is
        # silent in both directions: a stricter expectation rejects Graph's own valid
        # pagination, a looser one widens the scope this guard exists to pin.
        expected_query = {
            # The exact-sender filter is the privacy boundary; within those messages,
            # request only the timestamp and HTML needed to find job anchors. Graph may
            # still return its mandatory id, but this module never retains or reports it.
            "$select": "receivedDateTime,from,body",
            "$filter": odata_filter,
            "$orderby": "receivedDateTime desc",
            "$top": str(min(sender_limit, 100)),
        }
        query = urlencode(expected_query)
        next_url = f"{GRAPH_ROOT}/me/mailFolders/{folder_path}/messages?{query}"
        expected_path = urlsplit(next_url).path
        initial_page = True
        while next_url and sender_count < sender_limit:
            if not initial_page and not _graph_pagination_is_bound(
                next_url,
                expected_path=expected_path,
                expected_query=expected_query,
            ):
                raise OutlookShadowError("Graph returned an unsafe pagination scope")
            if next_url in seen_pages:
                raise OutlookShadowError("Microsoft Graph pagination cycle detected")
            if pages_read >= _MAX_GRAPH_PAGES:
                raise OutlookShadowError("Microsoft Graph pagination exceeded the page limit")
            seen_pages.add(next_url)
            pages_read += 1
            initial_page = False
            payload, page_bytes = _read_graph_page(next_url, token, opener)
            total_bytes += page_bytes
            if total_bytes > _MAX_GRAPH_TOTAL_BYTES:
                raise OutlookShadowError(
                    "Microsoft Graph mail responses exceeded the cumulative size limit"
                )
            for message in payload["value"]:
                # Defense in depth: the OData query is exact-sender scoped, and the response
                # must independently agree before its untrusted body enters the parser.
                if _message_from_address(message) == sender.casefold():
                    if sender_count >= sender_limit:
                        # The server can return a full final page without nextLink even when
                        # our fair sender quota ends mid-page. Scan the rest of this bounded
                        # page so the report still discloses that unseen messages existed.
                        sender_truncated = True
                        continue
                    body = message.get("body") if isinstance(message, dict) else None
                    content = body.get("content") if isinstance(body, dict) else None
                    yield {
                        "receivedDateTime": message.get("receivedDateTime"),
                        "body": {"content": content} if isinstance(content, str) else {},
                    }
                    sender_count += 1
            next_url = payload.get("@odata.nextLink")
            if next_url is not None and not isinstance(next_url, str):
                raise OutlookShadowError("Microsoft Graph returned an invalid pagination link")
        if sender_truncated or (sender_count >= sender_limit and next_url):
            stats["truncated_senders"] += 1


def _anchor_title(value):
    title = re.sub(r"\s+", " ", unescape(value or "")).strip(" \t\r\n-|•")
    if not title or len(title) > 200 or title.casefold() in _GENERIC_ANCHOR_TEXT:
        return None
    if any(marker in title.casefold() for marker in _NON_JOB_TEXT):
        return None
    return title


def _safe_received_at(value):
    if not isinstance(value, str) or len(value) > 64:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _adzuna_canonical_host(host):
    for suffix in _ADZUNA_SUFFIXES:
        if host in (f"adzuna.{suffix}", f"www.adzuna.{suffix}"):
            return f"www.adzuna.{suffix}"
    return None


def _direct_job_url(parsed):
    """Return ``(provider, stable URL)`` for one allowlisted job-detail route."""
    host = parsed.hostname.lower().rstrip(".")
    query = parse_qs(parsed.query, keep_blank_values=False)
    folded_query = {key.casefold(): values for key, values in query.items()}

    if host == "indeed.com" or host.endswith(".indeed.com"):
        if parsed.path.casefold().rstrip("/") not in ("/viewjob", "/rc/clk"):
            return None
        job_id = (folded_query.get("jk") or folded_query.get("vjk") or [None])[0]
        if not isinstance(job_id, str) or not _SAFE_INDEED_ID_RE.fullmatch(job_id):
            return None
        return "indeed", f"https://www.indeed.com/viewjob?jk={job_id}"

    if host == "lensa.com" or host.endswith(".lensa.com"):
        if not re.match(r"^/job(?:-v[0-9]+)?/", parsed.path, re.I):
            return None
        path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        return "lensa", urlunsplit(
            ("https", "lensa.com", path.rstrip("/") or "/", "", "")
        )

    adzuna_host = _adzuna_canonical_host(host)
    if adzuna_host:
        match = re.fullmatch(r"/(?:details|land/ad)/([0-9]{3,30})/?", parsed.path, re.I)
        if not match:
            return None
        return "adzuna", f"https://{adzuna_host}/details/{match.group(1)}"

    if host == "glassdoor.com" or host.endswith(".glassdoor.com"):
        path_lower = parsed.path.casefold()
        job_id = (
            folded_query.get("jl") or folded_query.get("joblistingid") or [None]
        )[0]
        if (not isinstance(job_id, str)
                or not _SAFE_NUMERIC_JOB_ID_RE.fullmatch(job_id)):
            return None
        if not (
            (path_lower.startswith("/job-listing/") and path_lower.endswith(".htm"))
            or path_lower == "/partner/joblisting.htm"
        ):
            return None
        path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
        return "glassdoor", urlunsplit(
            ("https", "www.glassdoor.com", path, urlencode({"jl": job_id}), "")
        )

    if host == "roberthalf.com" or host.endswith(".roberthalf.com"):
        parts = [part for part in parsed.path.split("/") if part]
        if (len(parts) < 6 or parts[2].casefold() != "job"
                or not all(re.fullmatch(r"[A-Za-z]{2}", part) for part in parts[:2])
                or not _SAFE_ROBERT_HALF_ID_RE.fullmatch(parts[-1])):
            return None
        path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
        return "robert_half", urlunsplit(
            ("https", "www.roberthalf.com", path.rstrip("/"), "", "")
        )
    return None


def _candidate_job_url(value, *, _depth=0, _seen=None):
    """Resolve a direct job URL or a bounded, locally encoded tracking destination.

    No URL is requested here. Opaque tracking tokens deliberately remain unsupported: only an
    explicit nested http(s) destination can cross the allowlisted provider/route boundary.
    """
    href = unescape(value or "").strip()
    if not href or len(href) > 4096 or any(ch in href for ch in "\r\n<>"):
        return None
    try:
        parsed = urlsplit(href)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    direct = _direct_job_url(parsed)
    if direct is not None:
        return direct
    if _depth >= _MAX_WRAPPER_DEPTH:
        return None
    if _seen is None:
        _seen = set()
    if href in _seen:
        return None
    _seen.add(href)
    query = parse_qs(parsed.query, keep_blank_values=False)
    for key, values in query.items():
        if key.casefold() not in _WRAPPED_URL_KEYS:
            continue
        for nested in values[:3]:
            resolved = _candidate_job_url(
                nested,
                _depth=_depth + 1,
                _seen=_seen,
            )
            if resolved is not None:
                return resolved
    return None


def parse_message_candidates(message):
    """Extract bounded allowlisted job anchors without retaining message metadata/body."""
    body = message.get("body") if isinstance(message, dict) else None
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str):
        return []
    if len(content) > _MAX_MESSAGE_BODY_CHARS:
        raise OutlookShadowError("Outlook job-alert message exceeded the body size limit")
    parser = _AnchorParser()
    try:
        parser.feed(content)
        parser.close()
    except (ValueError, AssertionError):
        return []
    received_at = _safe_received_at(message.get("receivedDateTime"))
    found = {}
    for href, text in parser.anchors:
        resolved = _candidate_job_url(href)
        if not resolved:
            continue
        source, url = resolved
        item = Candidate(
            source=source,
            title=_anchor_title(text),
            url=url,
            received_at=received_at,
        )
        previous = found.get(url)
        if previous is None or (previous.title is None and item.title is not None):
            found[url] = item
    return list(found.values())


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), _BATCH):
        yield values[start:start + _BATCH]


def classify_candidates(conn, candidates):
    """Compare with jobs.db read-only; title-only hits remain explicitly uncertain."""
    candidates = list(candidates)
    known_urls = set()
    for batch in _chunks({item.url for item in candidates}):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT job_url FROM jobs WHERE job_url IN ({placeholders})", batch
        ).fetchall()
        known_urls.update(row["job_url"] for row in rows)

    normalized = {_norm_title(item.title) for item in candidates if item.title}
    normalized.discard("")
    matches = {}
    for batch in _chunks(normalized):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT norm_title,title,company FROM jobs "
            f"WHERE norm_title IN ({placeholders}) "
            f"ORDER BY company,title,job_url",
            batch,
        ).fetchall()
        for row in rows:
            pair = (row["title"] or "Title unavailable", row["company"] or "Unknown company")
            bucket = matches.setdefault(row["norm_title"], [])
            if pair not in bucket:
                bucket.append(pair)

    classified = []
    for item in candidates:
        if item.url in known_urls:
            classified.append(replace(item, classification="known_url"))
            continue
        title_matches = matches.get(_norm_title(item.title), []) if item.title else []
        if title_matches:
            classified.append(
                replace(
                    item,
                    classification="possible_title_match",
                    possible_matches=tuple(title_matches[:10]),
                )
            )
        else:
            classified.append(replace(item, classification="unseen_link"))
    return classified


def _md_text(value):
    value = re.sub(r"\s+", " ", value or "Title unavailable").strip()
    value = value or "Title unavailable"
    value = escape(value, quote=False)
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _source_stats(items, source_email_counts=None):
    source_email_counts = source_email_counts or {}
    sources = sorted({item.source for item in items} | set(source_email_counts))
    result = {}
    for source in sources:
        source_items = [item for item in items if item.source == source]
        result[source] = {
            "emails": source_email_counts.get(source, 0),
            "candidates": len(source_items),
            "unseen_links": sum(
                item.classification == "unseen_link" for item in source_items
            ),
            "possible_title_matches": sum(
                item.classification == "possible_title_match" for item in source_items
            ),
            "known_urls": sum(item.classification == "known_url" for item in source_items),
        }
    return result


def render_report(items, *, report_date, days, emails_scanned, links_found,
                  truncated_senders=0, source_email_counts=None):
    items = list(items)
    counts = {
        key: sum(1 for item in items if item.classification == key)
        for key in ("unseen_link", "possible_title_match", "known_url")
    }
    lines = [
        f"# Outlook job-alert shadow report — {report_date}",
        "",
        f"Window: last {days} day(s)",
        f"Emails scanned: {emails_scanned}",
        f"Unique candidate links found: {links_found}",
        f"Unseen links: {counts['unseen_link']}",
        f"Possible title matches: {counts['possible_title_match']}",
        f"Exact URLs already in jobs.db: {counts['known_url']}",
        f"Sender quotas reached: {truncated_senders}",
        "",
        "> Read-only shadow evidence: this scan did not change Outlook or jobs.db, fetch live job ",
        "> pages, insert postings, or call an LLM. “Unseen” means the URL was not present; it is ",
        "> not proof that the underlying role is new. A title-only match is only a review hint.",
        f"> Counts are bounded by max_messages and the first {_MAX_LINKS_PER_MESSAGE} links per email.",
    ]
    if truncated_senders:
        lines.append(
            f"> Coverage is truncated for {truncated_senders} configured sender(s); "
            "the report is not a complete window."
        )
    source_stats = _source_stats(items, source_email_counts)
    lines.extend(
        [
            "",
            "## Historical comparison by source",
            "",
            "| Source | Emails with candidates | Unique candidate links | Unseen | Possible title match | Known URL |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    if source_stats:
        for source, stats in source_stats.items():
            lines.append(
                f"| {_md_text(source.replace('_', ' '))} | {stats['emails']} | "
                f"{stats['candidates']} | "
                f"{stats['unseen_links']} | {stats['possible_title_matches']} | "
                f"{stats['known_urls']} |"
            )
    else:
        lines.append("| None | 0 | 0 | 0 | 0 | 0 |")
    lines.extend(
        [
            "",
            "> This is a bounded historical comparison. It does not prove that an alert source "
            "found the role first; URL variants and title-only matches keep that attribution uncertain.",
        ]
    )
    sections = (
        ("unseen_link", "Unseen links"),
        ("possible_title_match", "Possible title matches"),
        ("known_url", "Exact URLs already known"),
    )
    shown = 0
    for classification, heading in sections:
        section_items = [item for item in items if item.classification == classification]
        lines.extend(["", f"## {heading}", ""])
        if not section_items:
            lines.append("None.")
            continue
        listed = 0
        for item in section_items:
            if shown >= _MAX_REPORT_ITEMS:
                break
            suffix = ""
            if item.possible_matches:
                matches = "; ".join(
                    f"{_md_text(title)} — {_md_text(company)}"
                    for title, company in item.possible_matches
                )
                suffix = f" — possible jobs.db match: {matches}"
            lines.append(
                f"- {_md_text(item.title)} — {item.source} — "
                f"{_safe_received_at(item.received_at) or 'date unavailable'}"
                f"{suffix} — <{item.url}>"
            )
            shown += 1
            listed += 1
        if listed < len(section_items):
            # The shared item budget can be exhausted by an earlier section. Say so HERE:
            # a heading followed by nothing reads as "this category is empty", which is the
            # opposite of the truth, and the global footer below does not say which section
            # lost its rows. The counts in the summary and the per-source table stay whole.
            lines.append(
                f"_{len(section_items) - listed} of {len(section_items)} not listed — "
                f"report item limit ({_MAX_REPORT_ITEMS}) reached._"
            )
    if len(items) > shown:
        lines.extend(["", f"Report limit reached; {len(items) - shown} additional link(s) omitted."])
    lines.append("")
    return "\n".join(lines)


def _open_jobs_read_only(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise OutlookShadowError(f"jobs database not found: {path}")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise OutlookShadowError("could not open jobs database read-only") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        conn.close()
        raise OutlookShadowError("could not open jobs database read-only") from exc
    return conn


def _write_report(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(content)
        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise OutlookShadowError("could not write Outlook shadow report") from exc


def run_shadow(cfg, base_dir, *, interactive=False, days_override=None, now=None,
               token_getter=acquire_access_token, message_fetcher=fetch_messages):
    """Run one report-only scan and return its bounded summary."""
    raw = cfg.get("settings", {}).get("outlook_email")
    if raw is None:
        raise ValueError("settings.outlook_email is missing from config.yaml")
    settings = validate_settings(raw)
    days = days_override if days_override is not None else settings["days"]
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 30:
        raise ValueError("email-shadow --days must be an integer from 1 to 30")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    token = token_getter(settings, interactive=interactive)
    fetch_stats = {}
    messages = message_fetcher(
        token,
        folder=settings["folder"],
        senders=settings["senders"],
        since=now - timedelta(days=days),
        max_messages=settings["max_messages"],
        stats=fetch_stats,
    )
    by_url = {}
    source_email_counts = {}
    emails_scanned = 0
    for message in messages:
        emails_scanned += 1
        message_candidates = parse_message_candidates(message)
        for source in {item.source for item in message_candidates}:
            source_email_counts[source] = source_email_counts.get(source, 0) + 1
        for item in message_candidates:
            previous = by_url.get(item.url)
            if previous is None or (previous.title is None and item.title is not None):
                if previous is None and len(by_url) >= _MAX_CANDIDATES:
                    raise OutlookShadowError(
                        "Outlook shadow candidate limit reached; narrow the mail window"
                    )
                by_url[item.url] = item
    db_path = Path(base_dir) / cfg["settings"]["db_path"]
    try:
        with _open_jobs_read_only(db_path) as conn:
            classified = classify_candidates(conn, by_url.values())
    except sqlite3.Error as exc:
        raise OutlookShadowError("could not compare Outlook links with jobs database") from exc
    report_date = now.astimezone().date().isoformat()
    content = render_report(
        classified,
        report_date=report_date,
        days=days,
        emails_scanned=emails_scanned,
        links_found=len(by_url),
        truncated_senders=fetch_stats.get("truncated_senders", 0),
        source_email_counts=source_email_counts,
    )
    report_path = Path(base_dir) / cfg["settings"]["reports_dir"] / (
        f"outlook-shadow-{report_date}.md"
    )
    _write_report(report_path, content)
    source_stats = _source_stats(classified, source_email_counts)
    return {
        "path": report_path,
        "emails_scanned": emails_scanned,
        "links_found": len(by_url),
        "unseen_links": sum(
            1 for item in classified if item.classification == "unseen_link"
        ),
        "possible_title_matches": sum(
            1 for item in classified if item.classification == "possible_title_match"
        ),
        "known_urls": sum(
            1 for item in classified if item.classification == "known_url"
        ),
        "truncated_senders": fetch_stats.get("truncated_senders", 0),
        "source_stats": source_stats,
    }
