import json
from collections.abc import Callable

import httpx
import pytest

from arise_radar.notifications.citations import (
    CitationNotificationConfigError,
    deliver_citation_notification,
    load_citation_notification_config,
    read_citation_row,
    render_slack_blocks,
    render_slack_text,
    scan_citation_rows,
    select_candidates,
)
from arise_radar.sinks.notion import NotionClient
from arise_radar.sinks.slack import SlackClient

FAKE_NOTION_TOKEN = "secret-notion-token"
FAKE_SLACK_TOKEN = "xoxb-fake-secret-value-12345"
CITATIONS_DS = "citations-ds-123"


def _citation_page(
    *,
    page_id: str = "page-1",
    page_url: str | None = "https://notion.so/page-1",
    citation_key: str | None = "citation:W900:W100",
    citing_title: str = "A Citing Paper",
    citing_doi: str | None = "10.2/citing",
    citing_url: str | None = "https://doi.org/10.2/citing",
    citing_openalex_id: str | None = "W900",
    cited_title: str = "Tracked Paper",
    cited_canonical_key: str = "doi:10.1/tracked",
    arise_researchers: list[str] | None = None,
    published_date: str | None = "2026-01-15",
    citation_relationship: str = "Unclassified",
    slack_status: str | None = "Pending",
    is_baseline: bool = False,
    system_notes: str = "",
    name_override: str | None = None,
) -> dict:
    name = name_override if name_override is not None else f"{citing_title} cites {cited_title}"
    properties: dict = {
        "Name": {"type": "title", "title": [{"plain_text": name}]},
        "ARISE Researchers": {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in (arise_researchers or ["Ethan Goh"])],
        },
        "Citation Relationship": {"type": "select", "select": {"name": citation_relationship}},
        "Baseline": {"type": "checkbox", "checkbox": is_baseline},
        "System Notes": {
            "type": "rich_text",
            "rich_text": [{"plain_text": system_notes}] if system_notes else [],
        },
        "Cited ARISE Paper": {
            "type": "rich_text",
            "rich_text": [{"plain_text": f"{cited_title} ({cited_canonical_key})"}],
        },
    }
    if citation_key is not None:
        properties["Citation Key"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": citation_key}],
        }
    if citing_doi is not None:
        properties["Citing DOI"] = {"type": "rich_text", "rich_text": [{"plain_text": citing_doi}]}
    if citing_url is not None:
        properties["Citing URL"] = {"type": "url", "url": citing_url}
    if citing_openalex_id is not None:
        properties["Citing OpenAlex ID"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": citing_openalex_id}],
        }
    if published_date is not None:
        properties["Published Date"] = {"type": "date", "date": {"start": published_date}}
    if slack_status is not None:
        properties["Slack Status"] = {"type": "select", "select": {"name": slack_status}}

    page: dict = {"properties": properties}
    if page_id is not None:
        page["id"] = page_id
    if page_url is not None:
        page["url"] = page_url
    return page


# --- load_citation_notification_config -----------------------------------------------


def test_load_config_missing_all_reports_all_four() -> None:
    with pytest.raises(CitationNotificationConfigError) as exc_info:
        load_citation_notification_config(env={})
    message = str(exc_info.value)
    assert "NOTION_TOKEN" in message
    assert "NOTION_CITATIONS_DATA_SOURCE_ID" in message
    assert "SLACK_BOT_TOKEN" in message
    assert "SLACK_SIGNALS_CHANNEL_ID" in message
    # This pipeline never touches publications or the papers channel.
    assert "NOTION_DATA_SOURCE_ID" not in message
    assert "SLACK_PAPERS_CHANNEL_ID" not in message


def test_load_config_bad_token_prefix_rejected() -> None:
    with pytest.raises(CitationNotificationConfigError, match="xoxb-"):
        load_citation_notification_config(
            env={
                "NOTION_TOKEN": FAKE_NOTION_TOKEN,
                "NOTION_CITATIONS_DATA_SOURCE_ID": CITATIONS_DS,
                "SLACK_BOT_TOKEN": "xoxp-wrong-type",
                "SLACK_SIGNALS_CHANNEL_ID": "C_SIGNALS",
            }
        )


def test_load_config_valid_env() -> None:
    config = load_citation_notification_config(
        env={
            "NOTION_TOKEN": FAKE_NOTION_TOKEN,
            "NOTION_CITATIONS_DATA_SOURCE_ID": CITATIONS_DS,
            "SLACK_BOT_TOKEN": FAKE_SLACK_TOKEN,
            "SLACK_SIGNALS_CHANNEL_ID": "C_SIGNALS",
        }
    )
    assert config.notion_token.get_secret_value() == FAKE_NOTION_TOKEN
    assert config.citations_data_source_id == CITATIONS_DS
    assert config.slack_bot_token.get_secret_value() == FAKE_SLACK_TOKEN
    assert config.signals_channel_id == "C_SIGNALS"


# --- read_citation_row ----------------------------------------------------------------


def test_read_citation_row_splits_citing_title_from_name() -> None:
    row = read_citation_row(_citation_page(citing_title="Some Paper", cited_title="ARISE Study"))
    assert row.citing_title == "Some Paper"
    assert row.cited_arise_paper == "ARISE Study (doi:10.1/tracked)"


def test_read_citation_row_falls_back_to_full_name_without_marker() -> None:
    row = read_citation_row(_citation_page(name_override="A weird title with no marker"))
    assert row.citing_title == "A weird title with no marker"


def test_read_citation_row_reads_all_fields() -> None:
    row = read_citation_row(
        _citation_page(
            page_id="page-42",
            page_url="https://notion.so/page-42",
            citation_key="citation:W1:W2",
            citing_doi="10.1/citing",
            citing_url="https://doi.org/10.1/citing",
            citing_openalex_id="W1",
            arise_researchers=["Ethan Goh", "Jane Doe"],
            published_date="2026-02-01",
            citation_relationship="Extends",
            slack_status="Pending",
            is_baseline=False,
            system_notes="existing note",
        )
    )
    assert row.page_id == "page-42"
    assert row.page_url == "https://notion.so/page-42"
    assert row.citation_key == "citation:W1:W2"
    assert row.citing_doi == "10.1/citing"
    assert row.citing_url == "https://doi.org/10.1/citing"
    assert row.citing_openalex_id == "W1"
    assert row.arise_researchers == ["Ethan Goh", "Jane Doe"]
    assert row.published_date == "2026-02-01"
    assert row.citation_relationship == "Extends"
    assert row.slack_status == "Pending"
    assert row.is_baseline is False
    assert row.system_notes == "existing note"


def test_read_citation_row_defaults_relationship_to_unclassified() -> None:
    page = _citation_page()
    page["properties"]["Citation Relationship"] = {"type": "select", "select": None}
    row = read_citation_row(page)
    assert row.citation_relationship == "Unclassified"


# --- scan_citation_rows -----------------------------------------------------------


def test_scan_baseline_rows_never_selected() -> None:
    scan = scan_citation_rows([_citation_page(is_baseline=True, slack_status="Pending")])
    assert scan.pending == []
    assert scan.failed == []
    assert scan.baseline_skipped == 1


def test_scan_suppressed_rows_never_selected() -> None:
    scan = scan_citation_rows([_citation_page(slack_status="Suppressed", is_baseline=True)])
    assert scan.pending == []
    assert scan.failed == []


def test_scan_sent_rows_never_selected() -> None:
    scan = scan_citation_rows([_citation_page(slack_status="Sent")])
    assert scan.pending == []
    assert scan.failed == []
    assert scan.suppressed_or_sent_skipped == 1


def test_scan_pending_rows_are_bucketed() -> None:
    scan = scan_citation_rows([_citation_page(slack_status="Pending")])
    assert len(scan.pending) == 1
    assert scan.failed == []


def test_scan_failed_rows_are_bucketed_separately() -> None:
    scan = scan_citation_rows([_citation_page(slack_status="Failed")])
    assert scan.pending == []
    assert len(scan.failed) == 1


def test_scan_duplicate_citation_keys_excluded_from_every_bucket() -> None:
    pages = [
        _citation_page(page_id="page-a", citation_key="citation:DUP:DUP", slack_status="Pending"),
        _citation_page(page_id="page-b", citation_key="citation:DUP:DUP", slack_status="Pending"),
    ]
    scan = scan_citation_rows(pages)
    assert scan.pending == []
    assert scan.duplicate_keys == {"citation:DUP:DUP": ["page-a", "page-b"]}


def test_scan_malformed_rows_counted_and_excluded() -> None:
    pages = [
        _citation_page(citation_key=None),
        _citation_page(page_id=None),
    ]
    scan = scan_citation_rows(pages)
    assert scan.malformed_rows == 2
    assert scan.pending == []


def test_scan_rows_scanned_counts_everything() -> None:
    pages = [
        _citation_page(page_id="a", citation_key="citation:A:B", slack_status="Pending"),
        _citation_page(page_id="b", citation_key="citation:C:D", slack_status="Sent"),
        _citation_page(page_id="c", citation_key="citation:E:F", is_baseline=True),
    ]
    scan = scan_citation_rows(pages)
    assert scan.rows_scanned == 3


# --- select_candidates -----------------------------------------------------------


def test_select_candidates_default_only_pending() -> None:
    pages = [
        _citation_page(page_id="a", citation_key="citation:A:B", slack_status="Pending"),
        _citation_page(page_id="b", citation_key="citation:C:D", slack_status="Failed"),
    ]
    scan = scan_citation_rows(pages)
    candidates = select_candidates(scan)
    assert [c.page_id for c in candidates] == ["a"]


def test_select_candidates_retry_failed_includes_failed_rows() -> None:
    pages = [
        _citation_page(page_id="a", citation_key="citation:A:B", slack_status="Pending"),
        _citation_page(page_id="b", citation_key="citation:C:D", slack_status="Failed"),
    ]
    scan = scan_citation_rows(pages)
    candidates = select_candidates(scan, retry_failed=True)
    assert {c.page_id for c in candidates} == {"a", "b"}


def test_select_candidates_citation_key_filters() -> None:
    pages = [
        _citation_page(page_id="a", citation_key="citation:A:B", slack_status="Pending"),
        _citation_page(page_id="b", citation_key="citation:C:D", slack_status="Pending"),
    ]
    scan = scan_citation_rows(pages)
    candidates = select_candidates(scan, citation_key="citation:C:D")
    assert [c.page_id for c in candidates] == ["b"]


def test_select_candidates_limit_caps_results() -> None:
    pages = [
        _citation_page(page_id=f"p{i}", citation_key=f"citation:{i}:X", slack_status="Pending")
        for i in range(5)
    ]
    scan = scan_citation_rows(pages)
    candidates = select_candidates(scan, limit=2)
    assert len(candidates) == 2


# --- rendering -----------------------------------------------------------------


def test_render_slack_text_is_complete_and_readable() -> None:
    row = read_citation_row(
        _citation_page(
            citing_title="Some Paper",
            cited_title="ARISE Study",
            arise_researchers=["Ethan Goh"],
            published_date="2026-01-15",
            citation_relationship="Extends",
        )
    )
    text = render_slack_text(row)
    assert "📚 NEW ARISE CITATION" in text
    assert "Citing paper:" in text and "Some Paper" in text
    assert "Cites:" in text and "ARISE Study (doi:10.1/tracked)" in text
    assert "ARISE researcher(s):" in text and "Ethan Goh" in text
    assert "Published:" in text and "2026-01-15" in text
    assert "Relationship:" in text and "Extends" in text
    assert "Citing paper link: https://doi.org/10.2/citing" in text
    assert "Notion page: https://notion.so/page-1" in text
    assert "Citation Key: citation:W900:W100" in text


def test_render_slack_text_falls_back_to_doi_link_without_url() -> None:
    row = read_citation_row(_citation_page(citing_url=None, citing_doi="10.9/x"))
    text = render_slack_text(row)
    assert "https://doi.org/10.9/x" in text


def test_render_slack_blocks_has_header_and_citation_key_context() -> None:
    row = read_citation_row(_citation_page())
    blocks = render_slack_blocks(row)
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "📚 NEW ARISE CITATION"
    context_block = blocks[-1]
    assert context_block["type"] == "context"
    assert "citation:W900:W100" in context_block["elements"][0]["text"]


# --- deliver_citation_notification: dry run -----------------------------------------


def test_deliver_dry_run_makes_no_calls_and_previews(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not call Slack")

    def notion_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not call Notion")

    row = read_citation_row(_citation_page())
    result = deliver_citation_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=True,
    )

    assert result.action == "dry_run_preview"
    assert "Citing paper:" in result.detail


# --- deliver_citation_notification: success -----------------------------------------


def test_deliver_success_sets_sent_and_saves_timestamp(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    patched_body: dict = {}

    def slack_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat.postMessage"
        return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1700000000.000100"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH" and request.url.path == "/v1/pages/page-1"
        nonlocal patched_body
        patched_body = json.loads(request.content)
        return httpx.Response(200, json={"id": "page-1"})

    row = read_citation_row(_citation_page(page_id="page-1"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert result.action == "sent"
    assert result.ts == "1700000000.000100"
    props = patched_body["properties"]
    assert props["Slack Status"] == {"select": {"name": "Sent"}}
    assert props["Slack Timestamp"]["rich_text"][0]["text"]["content"] == "1700000000.000100"
    assert "Review Status" not in props
    assert "Citation Relationship" not in props
    assert "Relationship Evidence" not in props
    assert "System Notes" not in props


# --- deliver_citation_notification: failure -----------------------------------------


def test_deliver_slack_ok_false_sets_failed_and_appends_system_note(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    patched_body: dict = {}

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        nonlocal patched_body
        patched_body = json.loads(request.content)
        return httpx.Response(200, json={"id": "page-1"})

    row = read_citation_row(_citation_page(page_id="page-1", system_notes="existing human note"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert result.action == "failed"
    assert result.stage == "slack_post"
    assert result.slack_error_code == "channel_not_found"
    props = patched_body["properties"]
    assert props["Slack Status"] == {"select": {"name": "Failed"}}
    notes = props["System Notes"]["rich_text"][0]["text"]["content"]
    assert "existing human note" in notes  # unrelated existing notes preserved
    assert "channel_not_found" in notes
    assert "Review Status" not in props
    assert "Citation Relationship" not in props


def test_deliver_slack_transport_failure_propagates_status_code(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal_error"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "page-1"})

    row = read_citation_row(_citation_page(page_id="page-1"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler, max_retries=0),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert result.action == "failed"
    assert result.stage == "slack_post"
    assert result.status_code == 500


def test_deliver_missing_ts_is_not_marked_sent(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})  # no ts

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "page-1"})

    row = read_citation_row(_citation_page(page_id="page-1"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert result.action == "failed"


def test_deliver_sent_but_notion_update_fails_is_reported_distinctly(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000100"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "internal error"})

    row = read_citation_row(_citation_page(page_id="page-1"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler, max_retries=0),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert result.action == "failed"
    assert result.stage == "notion_update_after_send"
    assert "1700000000.000100" in result.detail


# --- token never appears in any raised error text -----------------------------------


def test_slack_error_never_contains_the_bot_token(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "page-1"})

    row = read_citation_row(_citation_page(page_id="page-1"))
    result = deliver_citation_notification(
        mock_slack_client(slack_handler, max_retries=0),
        mock_notion_client(notion_handler),
        CITATIONS_DS,
        row,
        channel_id="C_SIGNALS",
        dry_run=False,
    )

    assert FAKE_SLACK_TOKEN not in result.detail
    assert FAKE_SLACK_TOKEN not in (result.slack_error_code or "")
