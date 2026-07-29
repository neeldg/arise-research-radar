import json
from collections.abc import Callable

import httpx
import pytest
from scripts.send_citation_notifications import main

from arise_radar.sinks.notion import NotionClient
from arise_radar.sinks.slack import SlackClient

FAKE_NOTION_TOKEN = "secret-notion-token"
FAKE_SLACK_TOKEN = "xoxb-fake-secret-value-12345"
CITATIONS_DS = "citations-ds-456"
SIGNALS_CHANNEL_ID = "C_SIGNALS_789"
CITATIONS_QUERY_PATH = f"/v1/data_sources/{CITATIONS_DS}/query"


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", FAKE_NOTION_TOKEN)
    monkeypatch.setenv("NOTION_CITATIONS_DATA_SOURCE_ID", CITATIONS_DS)
    monkeypatch.setenv("SLACK_BOT_TOKEN", FAKE_SLACK_TOKEN)
    monkeypatch.setenv("SLACK_SIGNALS_CHANNEL_ID", SIGNALS_CHANNEL_ID)


def _citation_page(
    *,
    page_id: str = "page-1",
    citation_key: str = "citation:W900:W100",
    citing_title: str = "A Citing Paper",
    cited_title: str = "Tracked Paper",
    slack_status: str | None = "Pending",
    is_baseline: bool = False,
    review_status: str = "New",
    citation_relationship: str = "Unclassified",
    relationship_evidence: str = "",
    system_notes: str = "",
) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": f"{citing_title} cites {cited_title}"}],
            },
            "Citation Key": {"type": "rich_text", "rich_text": [{"plain_text": citation_key}]},
            "Cited ARISE Paper": {
                "type": "rich_text",
                "rich_text": [{"plain_text": f"{cited_title} (doi:10.1/tracked)"}],
            },
            "ARISE Researchers": {
                "type": "multi_select",
                "multi_select": [{"name": "Ethan Goh"}],
            },
            "Citing OpenAlex ID": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "W900"}],
            },
            "Citing URL": {"type": "url", "url": "https://doi.org/10.2/citing"},
            "Published Date": {"type": "date", "date": {"start": "2026-01-15"}},
            "Citation Relationship": {"type": "select", "select": {"name": citation_relationship}},
            "Relationship Evidence": {
                "type": "rich_text",
                "rich_text": [{"plain_text": relationship_evidence}]
                if relationship_evidence
                else [],
            },
            "Review Status": {"type": "select", "select": {"name": review_status}},
            "Baseline": {"type": "checkbox", "checkbox": is_baseline},
            "Slack Status": (
                {"type": "select", "select": {"name": slack_status}}
                if slack_status is not None
                else {"type": "select", "select": None}
            ),
            "System Notes": {
                "type": "rich_text",
                "rich_text": [{"plain_text": system_notes}] if system_notes else [],
            },
        },
    }


class _FakeCitationsBackend:
    """Stateful fake of the Citation Events data source: bulk-served via a
    single query response, updated in place by page id on PATCH -- mirrors
    the real pipeline's bulk-read-then-update-by-id contract."""

    def __init__(self, pages: list[dict]) -> None:
        self.pages: dict[str, dict] = {page["id"]: page for page in pages}
        self.requests: list[tuple[str, str]] = []

    def notion_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == CITATIONS_QUERY_PATH:
            return httpx.Response(
                200,
                json={
                    "results": list(self.pages.values()),
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.method == "PATCH" and request.url.path.startswith("/v1/pages/"):
            page_id = request.url.path.rsplit("/", 1)[-1]
            if page_id not in self.pages:
                raise AssertionError(f"PATCH to unknown page id {page_id}")
            body = json.loads(request.content)
            for name, value in body["properties"].items():
                self.pages[page_id]["properties"][name] = _to_read_shape(name, value)
            return httpx.Response(200, json={"id": page_id})
        raise AssertionError(f"unexpected Notion request {request.method} {request.url}")

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.requests if m == method and p == path)

    def total(self, method: str) -> int:
        return sum(1 for m, _ in self.requests if m == method)


def _to_read_shape(name: str, value: dict) -> dict:
    """Convert a write-shape property payload (as sent by chat.postMessage's
    Notion update) into the read-shape a later bulk scan would see -- only
    the properties this pipeline ever writes need conversion."""
    if "select" in value:
        return {"type": "select", "select": value["select"]}
    if "rich_text" in value:
        parts = value["rich_text"]
        return {
            "type": "rich_text",
            "rich_text": [{"plain_text": p["text"]["content"]} for p in parts],
        }
    return value


def _default_slack_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"})


# --- missing configuration ------------------------------------------------------------


def test_missing_env_vars_reported_clearly(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_CITATIONS_DATA_SOURCE_ID", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_SIGNALS_CHANNEL_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env.
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "SLACK_BOT_TOKEN" in captured.err


# --- safe default: dry run ------------------------------------------------------------


def test_dry_run_performs_no_slack_or_notion_writes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page()])

    def guarded_slack(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not call Slack")

    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(guarded_slack)

    exit_code = main([], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "DRY RUN PREVIEW" in captured.out
    assert "no Slack messages were sent and no Notion writes were made" in captured.out
    assert backend.total("PATCH") == 0
    assert backend.pages["page-1"]["properties"]["Slack Status"]["select"]["name"] == "Pending"


# --- selection rules -------------------------------------------------------------


def test_baseline_rows_never_selected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page(is_baseline=True, slack_status="Pending")])
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(lambda r: (_ for _ in ()).throw(AssertionError("no Slack")))

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Baseline rows skipped: 1" in captured.out
    assert "Sent: 0" in captured.out


def test_suppressed_rows_never_selected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page(slack_status="Suppressed", is_baseline=True)])
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(lambda r: (_ for _ in ()).throw(AssertionError("no Slack")))

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sent: 0" in captured.out


def test_sent_rows_never_reselected(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page(slack_status="Sent")])
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(lambda r: (_ for _ in ()).throw(AssertionError("no Slack")))

    exit_code = main(
        ["--send", "--retry-failed"], notion_client=notion_client, slack_client=slack_client
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sent: 0" in captured.out


# --- successful delivery ---------------------------------------------------------


def test_pending_row_posts_and_becomes_sent_with_timestamp(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page(slack_status="Pending")])
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sent: 1" in captured.out
    page = backend.pages["page-1"]
    assert page["properties"]["Slack Status"]["select"]["name"] == "Sent"
    assert (
        page["properties"]["Slack Timestamp"]["rich_text"][0]["plain_text"] == "1700000000.000100"
    )


# --- failed delivery ---------------------------------------------------------------


def test_slack_failure_becomes_failed_and_continues_to_next_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(page_id="page-1", citation_key="citation:A:B", slack_status="Pending"),
            _citation_page(page_id="page-2", citation_key="citation:C:D", slack_status="Pending"),
        ]
    )

    call_count = {"n": 0}

    def alternating_slack_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000200"})

    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(alternating_slack_handler)

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 1  # overall failure reported...
    assert "Sent: 1" in captured.out  # ...but the second row still got processed
    assert "Failed: 1" in captured.out
    assert backend.pages["page-1"]["properties"]["Slack Status"]["select"]["name"] == "Failed"
    assert backend.pages["page-2"]["properties"]["Slack Status"]["select"]["name"] == "Sent"
    notes = backend.pages["page-1"]["properties"]["System Notes"]["rich_text"]
    assert notes and "channel_not_found" in notes[0]["plain_text"]
    assert "channel_not_found" in captured.err


def test_system_notes_append_preserves_unrelated_existing_notes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [_citation_page(slack_status="Pending", system_notes="unrelated human note")]
    )

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})

    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(slack_handler)

    main(["--send"], notion_client=notion_client, slack_client=slack_client)

    notes = backend.pages["page-1"]["properties"]["System Notes"]["rich_text"][0]["plain_text"]
    assert "unrelated human note" in notes
    assert "not_in_channel" in notes


# --- --retry-failed -----------------------------------------------------------------


def test_retry_failed_flag_processes_failed_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend([_citation_page(slack_status="Failed")])
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Sent: 0" in captured.out  # normal run never touches Failed rows

    exit_code = main(
        ["--send", "--retry-failed"], notion_client=notion_client, slack_client=slack_client
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Sent: 1" in captured.out
    assert backend.pages["page-1"]["properties"]["Slack Status"]["select"]["name"] == "Sent"


# --- duplicate citation keys ------------------------------------------------------


def test_duplicate_citation_keys_are_reported_and_skipped(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(
                page_id="page-a", citation_key="citation:DUP:DUP", slack_status="Pending"
            ),
            _citation_page(
                page_id="page-b", citation_key="citation:DUP:DUP", slack_status="Pending"
            ),
        ]
    )
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(lambda r: (_ for _ in ()).throw(AssertionError("no Slack")))

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Duplicate Citation Keys (all copies skipped): 1" in captured.out
    assert "Sent: 0" in captured.out
    assert backend.total("PATCH") == 0


# --- --limit / --citation-key ------------------------------------------------------


def test_limit_caps_processed_rows(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(
                page_id=f"page-{i}", citation_key=f"citation:{i}:X", slack_status="Pending"
            )
            for i in range(5)
        ]
    )
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    exit_code = main(
        ["--send", "--limit", "2"], notion_client=notion_client, slack_client=slack_client
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sent: 2" in captured.out


def test_citation_key_targets_a_single_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(page_id="page-1", citation_key="citation:A:B", slack_status="Pending"),
            _citation_page(page_id="page-2", citation_key="citation:C:D", slack_status="Pending"),
        ]
    )
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    exit_code = main(
        ["--send", "--citation-key", "citation:C:D"],
        notion_client=notion_client,
        slack_client=slack_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sent: 1" in captured.out
    assert backend.pages["page-1"]["properties"]["Slack Status"]["select"]["name"] == "Pending"
    assert backend.pages["page-2"]["properties"]["Slack Status"]["select"]["name"] == "Sent"


# --- human-edited fields preserved -------------------------------------------------


def test_human_edited_fields_preserved_on_success(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(
                slack_status="Pending",
                review_status="Approved",
                citation_relationship="Extends",
                relationship_evidence="human-written evidence",
            )
        ]
    )
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    main(["--send"], notion_client=notion_client, slack_client=slack_client)

    props = backend.pages["page-1"]["properties"]
    assert props["Review Status"]["select"]["name"] == "Approved"
    assert props["Citation Relationship"]["select"]["name"] == "Extends"
    assert props["Relationship Evidence"]["rich_text"][0]["plain_text"] == "human-written evidence"


# --- token never appears in output --------------------------------------------------


def test_token_never_appears_in_output_success_and_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(page_id="page-1", citation_key="citation:A:B", slack_status="Pending"),
            _citation_page(page_id="page-2", citation_key="citation:C:D", slack_status="Pending"),
        ]
    )

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000300"})

    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(handler)

    main(["--send"], notion_client=notion_client, slack_client=slack_client)
    captured = capsys.readouterr()

    assert FAKE_NOTION_TOKEN not in captured.out
    assert FAKE_NOTION_TOKEN not in captured.err
    assert FAKE_SLACK_TOKEN not in captured.out
    assert FAKE_SLACK_TOKEN not in captured.err


# --- bulk read, no per-row lookup --------------------------------------------------


def test_bulk_notion_read_with_no_per_row_lookup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    n = 50
    backend = _FakeCitationsBackend(
        [
            _citation_page(
                page_id=f"page-{i}", citation_key=f"citation:{i}:X", slack_status="Pending"
            )
            for i in range(n)
        ]
    )
    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(_default_slack_handler)

    exit_code = main(["--send"], notion_client=notion_client, slack_client=slack_client)

    assert exit_code == 0
    assert backend.count("POST", CITATIONS_QUERY_PATH) == 1  # one bulk read, not N lookups
    assert backend.total("PATCH") == n  # one update per row, straight to its known page id


# --- --error-report -----------------------------------------------------------------


def test_error_report_json_contains_failure_and_duplicate_entries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    backend = _FakeCitationsBackend(
        [
            _citation_page(page_id="page-1", citation_key="citation:A:B", slack_status="Pending"),
            _citation_page(
                page_id="dup-a", citation_key="citation:DUP:DUP", slack_status="Pending"
            ),
            _citation_page(
                page_id="dup-b", citation_key="citation:DUP:DUP", slack_status="Pending"
            ),
        ]
    )

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "missing_scope"})

    notion_client = mock_notion_client(backend.notion_handler)
    slack_client = mock_slack_client(slack_handler)
    report_path = tmp_path / "errors.json"

    main(
        ["--send", "--error-report", str(report_path)],
        notion_client=notion_client,
        slack_client=slack_client,
    )

    entries = json.loads(report_path.read_text())
    stages = {e["stage"] for e in entries}
    assert "slack_post" in stages
    assert "duplicate_key" in stages

    failure_entry = next(e for e in entries if e["stage"] == "slack_post")
    assert failure_entry["citation_key"] == "citation:A:B"
    assert failure_entry["notion_page_id"] == "page-1"
    assert failure_entry["slack_error_code"] == "missing_scope"

    dup_entry = next(e for e in entries if e["stage"] == "duplicate_key")
    assert dup_entry["citation_key"] == "citation:DUP:DUP"
