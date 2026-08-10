import contextlib
import io
import json
import sqlite3
from datetime import datetime, timezone
from http.client import HTTPMessage
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import make_job
import pipeline
import outlook_shadow
from outlook_shadow import (
    Candidate,
    OutlookShadowError,
    _NoRedirectHandler,
    _open_graph_no_redirect,
    acquire_access_token,
    classify_candidates,
    fetch_messages,
    parse_message_candidates,
    render_report,
    run_shadow,
    validate_settings,
)


def test_parse_message_candidates_canonicalizes_jobs_and_ignores_navigation():
    message = {
        "id": "private-message-id",
        "subject": "Jobs picked for you",
        "receivedDateTime": "2026-08-10T12:00:00Z",
        "body": {
            "contentType": "html",
            "content": """
                <a href="https://www.indeed.com/rc/clk?jk=abc-123&amp;utm_source=email">
                  Senior Solutions Architect
                </a>
                <a href="https://www.indeed.com/viewjob?vjk=abc-123">View job</a>
                <a href="https://lensa.com/job-v1/acme/solutions-engineer/new-york-ny/role-9?utm_source=alert">
                  Solutions Engineer
                </a>
                <a href="https://subscriptions.indeed.com/preferences">Email preferences</a>
                <a href="https://example.com/job/should-not-pass">Other site</a>
            """,
        },
    }

    candidates = parse_message_candidates(message)

    assert [(c.source, c.title, c.url) for c in candidates] == [
        (
            "indeed",
            "Senior Solutions Architect",
            "https://www.indeed.com/viewjob?jk=abc-123",
        ),
        (
            "lensa",
            "Solutions Engineer",
            "https://lensa.com/job-v1/acme/solutions-engineer/new-york-ny/role-9",
        ),
    ]
    assert all("private-message-id" not in repr(c) for c in candidates)


def test_parse_message_candidates_supports_additional_alert_providers_and_wrappers():
    message = {
        "receivedDateTime": "2026-08-10T12:00:00Z",
        "body": {
            "content": """
                <a href="https://www.adzuna.com/details/1000000001?utm_medium=email">
                  Cloud Solutions Architect
                </a>
                <a href="https://click.alert.invalid/open?url=https%3A%2F%2Fwww.adzuna.com%2Fland%2Fad%2F1000000001%3Fse%3Dprivate-token">
                  View job
                </a>
                <a href="https://www.glassdoor.com/job-listing/solutions-architect-acme-JV_IC1000000_KO0,19_KE20,24.htm?jl=1000000000001&amp;src=EMAIL_JOB_ALERT">
                  Solutions Architect
                </a>
                <a href="https://www.roberthalf.com/us/en/job/example-city/solutions-architect-cloud/00000-0000000001-usen?utm_source=alert">
                  Solutions Architect (Cloud)
                </a>
                <a href="https://www.adzuna.com/search">Adzuna search</a>
                <a href="https://www.glassdoor.com/Job/jobs.htm">Glassdoor jobs</a>
                <a href="https://www.roberthalf.com/us/en/jobs/all/architect">Robert Half jobs</a>
            """,
        },
    }

    candidates = parse_message_candidates(message)

    assert [(c.source, c.title, c.url) for c in candidates] == [
        (
            "adzuna",
            "Cloud Solutions Architect",
            "https://www.adzuna.com/details/1000000001",
        ),
        (
            "glassdoor",
            "Solutions Architect",
            "https://www.glassdoor.com/job-listing/solutions-architect-acme-JV_IC1000000_KO0,19_KE20,24.htm?jl=1000000000001",
        ),
        (
            "robert_half",
            "Solutions Architect (Cloud)",
            "https://www.roberthalf.com/us/en/job/example-city/solutions-architect-cloud/00000-0000000001-usen",
        ),
    ]


def test_parse_message_candidates_rejects_lookalikes_and_unsafe_wrapped_values():
    message = {
        "receivedDateTime": "2026-08-10T12:00:00Z",
        "body": {
            "content": """
                <a href="https://glassdoor.com.evil.example/job-listing/x.htm?jl=123456">
                  Fake Glassdoor
                </a>
                <a href="https://adzuna.com.evil.example/details/1000000001">Fake Adzuna</a>
                <a href="https://roberthalf.com.evil.example/us/en/job/x/y/00000-0000000001-usen">
                  Fake Robert Half
                </a>
                <a href="https://click.alert.invalid/open?url=javascript%3Aalert%281%29">
                  Unsafe wrapper
                </a>
                <a href="https://www.glassdoor.com/job-listing/no-id.htm">Missing job ID</a>
                <a href="https://subscriptions.indeed.com/preferences?jk=abc123">
                  Indeed preferences
                </a>
                <a href="https://www.lensa.com/jobs/search">Lensa search</a>
                <a href="https://www.glassdoor.com/v/profile?jl=123456">
                  Glassdoor profile
                </a>
                <a href="https://indeed.com.evil.example/viewjob?jk=abc123">
                  Fake Indeed
                </a>
                <a href="https://notindeed.com/viewjob?jk=abc123">Indeed suffix trick</a>
                <a href="https://lensa.com.evil.example/job/x">Fake Lensa</a>
                <a href="https://mylensa.com/job/x">Lensa suffix trick</a>
            """,
        },
    }

    assert parse_message_candidates(message) == []


def test_classify_candidates_separates_exact_title_only_and_unseen(conn):
    make_job(
        conn,
        job_url="https://www.indeed.com/viewjob?jk=known",
        title="Known Role",
        company="Acme",
    )
    make_job(
        conn,
        job_url="https://employer.example/role/2",
        title="Solutions Architect",
        company="Beta Corp",
    )
    candidates = [
        Candidate(
            source="indeed",
            title="Known Role",
            url="https://www.indeed.com/viewjob?jk=known",
            received_at="2026-08-10T12:00:00Z",
        ),
        Candidate(
            source="lensa",
            title="Solutions Architect",
            url="https://lensa.com/job-v1/acme/solutions-architect/role-2",
            received_at="2026-08-10T12:00:00Z",
        ),
        Candidate(
            source="indeed",
            title="New Role",
            url="https://www.indeed.com/viewjob?jk=new",
            received_at="2026-08-10T12:00:00Z",
        ),
        Candidate(
            source="lensa",
            title=None,
            url="https://lensa.com/job-v1/acme/unknown/role-3",
            received_at="2026-08-10T12:00:00Z",
        ),
    ]

    classified = classify_candidates(conn, candidates)

    assert [item.classification for item in classified] == [
        "known_url",
        "possible_title_match",
        "unseen_link",
        "unseen_link",
    ]
    assert classified[1].possible_matches == (("Solutions Architect", "Beta Corp"),)
    assert classified[2].possible_matches == ()


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        data = json.dumps(self._payload).encode("utf-8")
        return data if size < 0 else data[:size]


def test_fetch_messages_filters_each_exact_sender_and_honors_safe_paging():
    calls = []
    payloads = [
        {
            "value": [
                {
                    "id": "one",
                    "from": {"emailAddress": {"address": "jobalerts-noreply@example.com"}},
                },
                {
                    "id": "must-be-discarded",
                    "from": {"emailAddress": {"address": "private@example.com"}},
                },
            ],
            "@odata.nextLink": "replace-from-request",
        },
        {
            "value": [
                {
                    "id": "two",
                    "from": {"emailAddress": {"address": "JOBALERTS-NOREPLY@EXAMPLE.COM"}},
                }
            ]
        },
    ]

    def opener(request, timeout):
        calls.append((request, timeout))
        payload = payloads.pop(0)
        if payload.get("@odata.nextLink") == "replace-from-request":
            payload["@odata.nextLink"] = request.full_url + "&%24skip=1"
        return _Response(payload)

    messages = list(
        fetch_messages(
            "secret-token",
            folder="inbox",
            senders=["jobalerts-noreply@example.com"],
            since=datetime(2026, 8, 9, tzinfo=timezone.utc),
            max_messages=10,
            opener=opener,
        )
    )

    assert messages == [
        {"receivedDateTime": None, "body": {}},
        {"receivedDateTime": None, "body": {}},
    ]
    assert "must-be-discarded" not in repr(messages)
    first_request, timeout = calls[0]
    assert timeout == 30
    assert first_request.get_header("Authorization") == "Bearer secret-token"
    query = parse_qs(urlparse(first_request.full_url).query)
    assert "from/emailAddress/address eq 'jobalerts-noreply@example.com'" in query["$filter"][0]
    assert "receivedDateTime ge 2026-08-09T00:00:00Z" in query["$filter"][0]
    assert query["$top"] == ["10"]
    # $select is a privacy ALLOWLIST, so what it leaves out is the load-bearing half:
    # asserting only that body/from are present passes just as well after someone widens
    # it to include subject, id, toRecipients, or the whole message.
    assert query["$select"] == ["receivedDateTime,from,body"]


def test_fetch_messages_rejects_untrusted_graph_next_link():
    def opener(_request, timeout):
        assert timeout == 30
        return _Response(
            {
                "value": [],
                "@odata.nextLink": "https://evil.example/collect?token=please",
            }
        )

    with pytest.raises(OutlookShadowError, match="unsafe pagination"):
        list(
            fetch_messages(
                "secret-token",
                folder="inbox",
                senders=["alerts@example.com"],
                since=datetime(2026, 8, 9, tzinfo=timezone.utc),
                max_messages=10,
                opener=opener,
            )
        )


def test_fetch_messages_rejects_same_origin_pagination_scope_widening():
    calls = 0

    def opener(_request, timeout):
        nonlocal calls
        assert timeout == 30
        calls += 1
        if calls > 1:
            pytest.fail("scope-widened Graph nextLink must not be requested")
        return _Response(
            {
                "value": [],
                "@odata.nextLink": (
                    "https://graph.microsoft.com/v1.0/me/messages?"
                    "$select=receivedDateTime,from,body&$top=10"
                ),
            }
        )

    with pytest.raises(OutlookShadowError, match="pagination scope"):
        list(
            fetch_messages(
                "secret-token",
                folder="inbox",
                senders=["alerts@example.com"],
                since=datetime(2026, 8, 9, tzinfo=timezone.utc),
                max_messages=10,
                opener=opener,
            )
        )
    assert calls == 1


def test_fetch_messages_rejects_next_link_with_changed_sender_filter():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        assert timeout == 30
        calls += 1
        if calls > 1:
            pytest.fail("Graph nextLink with a changed sender must not be requested")
        widened = request.full_url.replace(
            "alerts%40example.com",
            "private%40example.com",
        ) + "&%24skip=1"
        return _Response({"value": [], "@odata.nextLink": widened})

    with pytest.raises(OutlookShadowError, match="pagination scope"):
        list(
            fetch_messages(
                "secret-token",
                folder="inbox",
                senders=["alerts@example.com"],
                since=datetime(2026, 8, 9, tzinfo=timezone.utc),
                max_messages=10,
                opener=opener,
            )
        )
    assert calls == 1


def test_fetch_messages_rejects_pagination_cycles():
    same_page = None

    def opener(request, timeout):
        nonlocal same_page
        assert timeout == 30
        if same_page is None:
            same_page = request.full_url + "&%24skip=1"
        return _Response({"value": [], "@odata.nextLink": same_page})

    with pytest.raises(OutlookShadowError, match="pagination cycle"):
        list(
            fetch_messages(
                "secret-token",
                folder="inbox",
                senders=["alerts@example.com"],
                since=datetime(2026, 8, 9, tzinfo=timezone.utc),
                max_messages=10,
                opener=opener,
            )
        )


def test_default_graph_opener_disables_all_redirects():
    request = outlook_shadow.Request(
        "https://graph.microsoft.com/v1.0/me/messages",
        headers={"Authorization": "Bearer secret"},
    )
    handler = _NoRedirectHandler()

    redirected = handler.redirect_request(
        request,
        io.BytesIO(b""),
        302,
        "Found",
        HTTPMessage(),
        "https://evil.example/collect",
    )

    assert redirected is None
    kwdefaults = fetch_messages.__kwdefaults__
    assert kwdefaults is not None
    assert kwdefaults["opener"] is _open_graph_no_redirect


def test_fetch_messages_enforces_cumulative_response_budget(monkeypatch):
    next_page = None

    def opener(request, timeout):
        nonlocal next_page
        assert timeout == 30
        if next_page is None:
            next_page = request.full_url + "&%24skip=1"
        payload = {"value": [], "padding": "x" * 80}
        if "%24skip=1" not in request.full_url:
            payload["@odata.nextLink"] = next_page
        return _Response(payload)

    monkeypatch.setattr(outlook_shadow, "_MAX_GRAPH_TOTAL_BYTES", 100)
    with pytest.raises(OutlookShadowError, match="cumulative size"):
        list(
            fetch_messages(
                "secret-token",
                folder="inbox",
                senders=["alerts@example.com"],
                since=datetime(2026, 8, 9, tzinfo=timezone.utc),
                max_messages=10,
                opener=opener,
            )
        )


def test_fetch_messages_fairly_quotas_senders_and_discloses_truncation():
    calls = []
    stats = {}

    def opener(request, timeout):
        assert timeout == 30
        query = parse_qs(urlparse(request.full_url).query)
        sender = query["$filter"][0].split("address eq '", 1)[1].split("'", 1)[0]
        calls.append(sender)
        payload: dict[str, Any] = {
            "value": [
                {
                    "receivedDateTime": "2026-08-10T12:00:00Z",
                    "from": {"emailAddress": {"address": sender}},
                    "body": {"content": "<p>alert</p>"},
                }
            ]
        }
        if sender == "first@example.com":
            payload["@odata.nextLink"] = (
                "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skip=1"
            )
        return _Response(payload)

    messages = list(
        fetch_messages(
            "secret-token",
            folder="inbox",
            senders=["first@example.com", "second@example.com"],
            since=datetime(2026, 8, 9, tzinfo=timezone.utc),
            max_messages=2,
            opener=opener,
            stats=stats,
        )
    )

    assert calls == ["first@example.com", "second@example.com"]
    assert len(messages) == 2
    assert stats["truncated_senders"] == 1


def test_fetch_messages_discloses_overflow_inside_last_page_without_next_link():
    stats = {}
    page = {"number": 0}

    def opener(_request, timeout):
        assert timeout == 30
        page["number"] += 1
        payload = {
            "value": [
                {
                    "receivedDateTime": "2026-08-10T12:00:00Z",
                    "from": {"emailAddress": {"address": "alerts@example.com"}},
                    "body": {"content": f"<p>alert {page['number']}-{i}</p>"},
                }
                for i in range(100)
            ]
        }
        if page["number"] == 1:
            payload["@odata.nextLink"] = _request.full_url + "&%24skip=100"
        return _Response(payload)

    messages = list(
        fetch_messages(
            "secret-token",
            folder="inbox",
            senders=["alerts@example.com"],
            since=datetime(2026, 8, 9, tzinfo=timezone.utc),
            max_messages=150,
            opener=opener,
            stats=stats,
        )
    )

    assert len(messages) == 150
    assert page["number"] == 2
    assert stats["truncated_senders"] == 1


def test_message_parser_stops_at_anchor_cap_and_rejects_oversized_body(monkeypatch):
    anchors = "".join(
        f'<a href="https://www.indeed.com/viewjob?jk=role-{i}">Role {i}</a>'
        for i in range(150)
    )
    message = {
        "receivedDateTime": "2026-08-10T12:00:00Z",
        "body": {"content": anchors},
    }
    assert len(parse_message_candidates(message)) == 100

    monkeypatch.setattr(outlook_shadow, "_MAX_MESSAGE_BODY_CHARS", 10)
    with pytest.raises(OutlookShadowError, match="body size"):
        parse_message_candidates(message)

def test_acquire_access_token_is_silent_unless_login_was_requested():
    class FakeApp:
        CONSOLE_WINDOW_HANDLE = 123

        def __init__(self):
            self.interactive_calls = []

        def get_accounts(self, username=None):
            return [{"username": username or "me@example.com"}]

        def acquire_token_silent(self, scopes, account):
            assert scopes == ["Mail.Read"]
            return None

        def acquire_token_interactive(self, scopes, **kwargs):
            self.interactive_calls.append((scopes, kwargs))
            return {"access_token": "interactive-token"}

    app = FakeApp()
    factory_calls = []

    def factory(client_id, **kwargs):
        factory_calls.append((client_id, kwargs))
        return app

    settings = {
        "client_id": "00000000-0000-0000-0000-000000000000",
        "tenant": "common",
        "login_hint": "me@example.com",
    }

    with pytest.raises(OutlookShadowError, match="--login"):
        acquire_access_token(settings, interactive=False, app_factory=factory)
    assert app.interactive_calls == []

    assert acquire_access_token(settings, interactive=True, app_factory=factory) == "interactive-token"
    assert factory_calls[-1][1]["enable_broker_on_windows"] is True
    assert app.interactive_calls[-1][1]["parent_window_handle"] == 123


def test_acquire_access_token_hides_broker_exception_details():
    class BrokenApp:
        def get_accounts(self):
            raise RuntimeError("private-person@example.com broker detail")

    with pytest.raises(OutlookShadowError, match="Windows authentication broker") as caught:
        acquire_access_token(
            {"client_id": "client", "tenant": "common"},
            app_factory=lambda *_args, **_kwargs: BrokenApp(),
        )
    assert "private-person" not in str(caught.value)


@pytest.mark.parametrize(
    "settings, message",
    [
        ({}, "client_id"),
        ({"client_id": "abc", "senders": []}, "senders"),
        ({"client_id": "abc", "senders": ["not-an-address"]}, "email address"),
        ({"client_id": "abc", "senders": ["a" * 400 + "@example.com"]}, "email address"),
        (
            {
                "client_id": "abc",
                "senders": ["one@example.com", "two@example.com"],
                "max_messages": 1,
            },
            "at least",
        ),
        (
            {"client_id": "abc", "senders": [f"jobs-{i}@example.com" for i in range(21)]},
            "at most",
        ),
        ({"client_id": "abc", "senders": ["jobs@example.com"], "days": 0}, "days"),
    ],
)
def test_validate_settings_fails_closed(settings, message):
    with pytest.raises(ValueError, match=message):
        validate_settings(settings)


def test_render_report_is_bounded_summary_without_mail_body_or_identifiers():
    items = [
        Candidate(
            source="indeed",
            title="Role [One]",
            url="https://www.indeed.com/viewjob?jk=one",
            received_at="2026-08-10T12:00:00Z",
            classification="unseen_link",
        ),
        Candidate(
            source="lensa",
            title="<img src=x onerror=alert(1)>",
            url="https://lensa.com/job-v1/acme/unknown/role-3",
            received_at="bad\n## injected heading",
            classification="unseen_link",
            possible_matches=(("Stored Role", "Acme\n## DB injected heading"),),
        ),
    ]

    report = render_report(
        items,
        report_date="2026-08-10",
        days=7,
        emails_scanned=3,
        links_found=2,
    )

    assert "Emails scanned: 3" in report
    assert "Unseen links: 2" in report
    assert "Role \\[One\\]" in report
    assert "&lt;img src=x onerror=alert(1)&gt;" in report
    assert "<img src=x" not in report
    assert "## injected heading" not in report
    assert "DB injected heading" in report
    assert "\n## DB injected heading" not in report
    assert "date unavailable" in report
    assert "mail body" not in report.lower()
    assert "private-message-id" not in report
    assert "LLM" in report


def test_render_report_starved_section_says_so_instead_of_looking_empty():
    """The item budget is shared across sections, so an earlier section can exhaust it. A
    heading followed by nothing reads as "this category is empty" — the opposite of the
    truth — and the global footer does not say WHICH section lost rows."""
    items = [
        Candidate(source="indeed", title=f"Role {i}",
                  url=f"https://www.indeed.com/viewjob?jk=a{i}",
                  received_at="2026-08-10T12:00:00Z", classification="unseen_link")
        for i in range(outlook_shadow._MAX_REPORT_ITEMS)
    ]
    items.append(Candidate(source="indeed", title="Known Role",
                           url="https://www.indeed.com/viewjob?jk=zzz",
                           received_at="2026-08-10T12:00:00Z",
                           classification="known_url"))

    report = render_report(items, report_date="2026-08-10", days=7,
                           emails_scanned=3, links_found=len(items))
    lines = report.splitlines()
    section = lines[lines.index("## Exact URLs already known") + 1:]
    body = [line for line in section[:3] if line.strip()]

    assert body, "starved section rendered as a bare heading with nothing under it"
    assert "not listed" in body[0] and "report item limit" in body[0]
    # The summary counts and the per-source table stay whole regardless of the display cap.
    assert "Exact URLs already in jobs.db: 1" in report


def test_render_report_includes_per_source_historical_backtest_counts():
    items = [
        Candidate(
            source="adzuna",
            title="Known Role",
            url="https://www.adzuna.com/details/1",
            received_at="2026-08-10T12:00:00Z",
            classification="known_url",
        ),
        Candidate(
            source="adzuna",
            title="New Role",
            url="https://www.adzuna.com/details/2",
            received_at="2026-08-10T12:00:00Z",
            classification="unseen_link",
        ),
        Candidate(
            source="glassdoor",
            title="Possible Role",
            url="https://www.glassdoor.com/job-listing/x.htm?jl=1000001",
            received_at="2026-08-10T12:00:00Z",
            classification="possible_title_match",
        ),
    ]

    report = render_report(
        items,
        report_date="2026-08-10",
        days=30,
        emails_scanned=4,
        links_found=3,
        source_email_counts={"adzuna": 2, "glassdoor": 1},
    )

    assert "## Historical comparison by source" in report
    assert "| adzuna | 2 | 2 | 1 | 0 | 1 |" in report
    assert "| glassdoor | 1 | 1 | 0 | 1 | 0 |" in report
    assert "does not prove that an alert source found the role first" in report


def test_email_shadow_cli_uses_read_only_path_before_normal_db_open(monkeypatch, capsys):
    cfg = {
        "settings": {
            "db_path": "jobs.db",
            "reports_dir": "reports",
            "outlook_email": {},
        },
        "searches": [],
    }
    seen = {}

    monkeypatch.setattr(pipeline, "load_config", lambda: cfg)
    monkeypatch.setattr(pipeline, "run_log", lambda _label: contextlib.nullcontext())
    monkeypatch.setattr(
        pipeline,
        "get_db",
        lambda _cfg: pytest.fail("email-shadow must not use the schema/migration DB opener"),
    )

    def fake_run(config, base_dir, *, interactive, days_override):
        seen.update(
            config=config,
            base_dir=base_dir,
            interactive=interactive,
            days_override=days_override,
        )
        return {
            "path": base_dir / "reports" / "outlook-shadow-2026-08-10.md",
            "emails_scanned": 4,
            "links_found": 3,
            "unseen_links": 2,
            "possible_title_matches": 1,
            "known_urls": 0,
        }

    # No raising=False: with it, monkeypatch would happily CREATE the attribute, so this
    # test would keep passing even if pipeline stopped importing run_outlook_shadow.
    monkeypatch.setattr(pipeline, "run_outlook_shadow", fake_run)
    monkeypatch.setattr(
        "sys.argv", ["pipeline.py", "email-shadow", "--login", "--days", "3"]
    )

    pipeline.main()

    assert seen["config"] is cfg
    assert seen["interactive"] is True
    assert seen["days_override"] == 3
    assert "4 email(s)" in capsys.readouterr().out


def test_email_shadow_cli_closes_log_cleanly_before_safe_exit(monkeypatch, capsys):
    cfg = {
        "settings": {"db_path": "jobs.db", "reports_dir": "reports"},
        "searches": [],
    }
    log_exit = {}

    class RecordingLog:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, _exc, _tb):
            log_exit["exc_type"] = exc_type
            log_exit["exc"] = _exc
            return False

    def fail_safely(*_args, **_kwargs):
        try:
            raise RuntimeError("private-person@example.com broker detail")
        except RuntimeError as exc:
            raise OutlookShadowError("Windows authentication broker failed") from exc

    monkeypatch.setattr(pipeline, "load_config", lambda: cfg)
    monkeypatch.setattr(pipeline, "run_log", lambda _label: RecordingLog())
    monkeypatch.setattr(pipeline, "run_outlook_shadow", fail_safely)
    monkeypatch.setattr("sys.argv", ["pipeline.py", "email-shadow"])

    with pytest.raises(SystemExit) as caught:
        pipeline.main()

    assert caught.value.code == 2
    assert log_exit["exc_type"] is OutlookShadowError
    assert str(log_exit["exc"]) == "email-shadow failed"
    assert log_exit["exc"].__cause__ is None
    assert log_exit["exc"].__context__ is None
    output = capsys.readouterr()
    assert "Windows authentication broker failed" in output.err
    assert "private-person" not in output.err


def test_jobs_connection_physically_refuses_writes(tmp_path):
    """The module's headline claim is that it CANNOT write jobs.db, not merely that it
    happens not to. Observing unchanged bytes after a read-only workload passes just as
    well with a plain read-write connection, so assert the refusal itself."""
    db_path = tmp_path / "jobs.db"
    with contextlib.closing(sqlite3.connect(db_path)) as setup:
        setup.execute("CREATE TABLE jobs (job_url TEXT PRIMARY KEY)")
        setup.commit()

    writes = (
        "INSERT INTO jobs VALUES ('https://example.invalid/1')",
        "DELETE FROM jobs",
        "CREATE TABLE sneaky (x TEXT)",
    )
    with contextlib.closing(outlook_shadow._open_jobs_read_only(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        # Both guards, asserted separately. query_only is the one that still protects a
        # database opened read-WRITE by mistake, and mode=ro alone would hide its removal.
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        for statement in writes:
            with pytest.raises(sqlite3.Error):
                conn.execute(statement)
        # The two guards are not redundant, and `PRAGMA query_only=OFF` is accepted without
        # error — so query_only alone would be revocable by any later code on this handle.
        # The URI's mode=ro is what actually makes the file unwritable; both stay.
        conn.execute("PRAGMA query_only=OFF")
        for statement in writes:
            with pytest.raises(sqlite3.Error):
                conn.execute(statement)


def test_run_shadow_end_to_end_leaves_database_bytes_unchanged(tmp_path):
    db_path = tmp_path / "jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE jobs (job_url TEXT PRIMARY KEY, title TEXT, company TEXT, "
            "norm_title TEXT)"
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?)",
            (
                "https://www.indeed.com/viewjob?jk=known",
                "Known Role",
                "Acme",
                "known role",
            ),
        )
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?)",
            (
                "https://www.adzuna.com/details/1000000001",
                "Cloud Solutions Architect",
                "Beta",
                "cloud solutions architect",
            ),
        )
    before = db_path.read_bytes()
    cfg = {
        "settings": {
            "db_path": "jobs.db",
            "reports_dir": "reports",
            "outlook_email": {
                "client_id": "client-id",
                "senders": ["alerts@example.com"],
                "days": 7,
            },
        }
    }

    def token_getter(settings, *, interactive):
        assert settings["senders"] == ["alerts@example.com"]
        assert interactive is False
        return "token"

    def message_fetcher(token, **kwargs):
        assert token == "token"
        assert kwargs["max_messages"] == 200
        return [
            {
                "id": "must-not-be-reported",
                "receivedDateTime": "2026-08-10T12:00:00Z",
                "body": {
                    "content": (
                        '<a href="https://www.indeed.com/viewjob?jk=known">Known Role</a>'
                        '<a href="https://www.indeed.com/viewjob?jk=new">New Role</a>'
                        '<a href="https://www.adzuna.com/land/ad/1000000001?se=private">'
                        'Cloud Solutions Architect</a>'
                        '<a href="https://www.glassdoor.com/job-listing/new-role-acme-JV_KO0,8_KE9,13.htm?jl=1000000000001">'
                        'Glassdoor New Role</a>'
                    )
                },
            }
        ]

    summary = run_shadow(
        cfg,
        tmp_path,
        now=datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
        token_getter=token_getter,
        message_fetcher=message_fetcher,
    )

    assert summary["known_urls"] == 2
    assert summary["unseen_links"] == 2
    assert summary["source_stats"] == {
        "adzuna": {
            "emails": 1,
            "candidates": 1,
            "unseen_links": 0,
            "possible_title_matches": 0,
            "known_urls": 1,
        },
        "glassdoor": {
            "emails": 1,
            "candidates": 1,
            "unseen_links": 1,
            "possible_title_matches": 0,
            "known_urls": 0,
        },
        "indeed": {
            "emails": 1,
            "candidates": 2,
            "unseen_links": 1,
            "possible_title_matches": 0,
            "known_urls": 1,
        },
    }
    # The report is dated in LOCAL time (run_shadow uses now.astimezone().date()), so derive
    # the expected name the same way. Hard-coding 2026-08-10 passes only where the runner's
    # offset keeps 18:00Z on the same calendar day — true for CI (UTC), false at UTC+6 east.
    expected_date = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc).astimezone().date()
    assert summary["path"] == (
        tmp_path / "reports" / f"outlook-shadow-{expected_date.isoformat()}.md")
    report = summary["path"].read_text(encoding="utf-8")
    assert "must-not-be-reported" not in report
    assert db_path.read_bytes() == before
    assert not (tmp_path / "jobs.db-wal").exists()
