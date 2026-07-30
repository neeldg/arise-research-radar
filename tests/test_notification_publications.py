import json
from collections.abc import Callable

import httpx
import pytest

from arise_radar.notifications.publications import (
    PublicationNotificationConfigError,
    deliver_publication_notification,
    load_publication_notification_config,
    read_publication_row,
    render_slack_blocks,
    render_slack_text,
    scan_publication_rows,
    select_candidates,
)
from arise_radar.sinks.notion import NotionClient
from arise_radar.sinks.slack import SlackClient

FAKE_NOTION_TOKEN = "secret-notion-token"
FAKE_SLACK_TOKEN = "xoxb-fake-secret-value-12345"
DATA_SOURCE_ID = "ds-123"


def _publication_page(
    *,
    page_id: str | None = "page-1",
    page_url: str | None = "https://notion.so/page-1",
    canonical_key: str | None = "doi:10.1000/abc",
    title: str = "Clinical decision support with large language models",
    doi: str | None = "10.1000/abc",
    url: str | None = "https://doi.org/10.1000/abc",
    researchers: list[str] | None = None,
    source_label: str | None = "OpenAlex",
    published_date: str | None = "2026-01-15",
    why_it_matters: str | None = None,
    internal_summary: str | None = None,
    key_story_angle: str | None = None,
    draft_social_post: str | None = None,
    slack_status: str | None = "Pending",
) -> dict:
    properties: dict = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "Researchers": {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in (researchers or ["Ethan Goh"])],
        },
    }
    if canonical_key is not None:
        properties["Canonical Key"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": canonical_key}],
        }
    if doi is not None:
        properties["DOI"] = {"type": "rich_text", "rich_text": [{"plain_text": doi}]}
    if url is not None:
        properties["URL"] = {"type": "url", "url": url}
    if source_label is not None:
        properties["Source"] = {"type": "select", "select": {"name": source_label}}
    if published_date is not None:
        properties["Published Date"] = {"type": "date", "date": {"start": published_date}}
    if why_it_matters is not None:
        properties["Why It Matters"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": why_it_matters}],
        }
    if internal_summary is not None:
        properties["Internal Summary"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": internal_summary}],
        }
    if key_story_angle is not None:
        properties["Key Story Angle"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": key_story_angle}],
        }
    if draft_social_post is not None:
        properties["Draft Social Post"] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": draft_social_post}],
        }
    if slack_status is not None:
        properties["Slack Status"] = {"type": "select", "select": {"name": slack_status}}
    else:
        properties["Slack Status"] = {"type": "select", "select": None}

    page: dict = {"properties": properties}
    if page_id is not None:
        page["id"] = page_id
    if page_url is not None:
        page["url"] = page_url
    return page


# --- load_publication_notification_config -------------------------------------------


def test_load_config_missing_all_reports_all_four() -> None:
    with pytest.raises(PublicationNotificationConfigError) as exc_info:
        load_publication_notification_config(env={})
    message = str(exc_info.value)
    assert "NOTION_TOKEN" in message
    assert "NOTION_DATA_SOURCE_ID" in message
    assert "SLACK_BOT_TOKEN" in message
    assert "SLACK_PAPERS_CHANNEL_ID" in message
    assert "NOTION_CITATIONS_DATA_SOURCE_ID" not in message
    assert "SLACK_SIGNALS_CHANNEL_ID" not in message


def test_load_config_bad_token_prefix_rejected() -> None:
    with pytest.raises(PublicationNotificationConfigError, match="xoxb-"):
        load_publication_notification_config(
            env={
                "NOTION_TOKEN": FAKE_NOTION_TOKEN,
                "NOTION_DATA_SOURCE_ID": DATA_SOURCE_ID,
                "SLACK_BOT_TOKEN": "xoxp-wrong-type",
                "SLACK_PAPERS_CHANNEL_ID": "C_PAPERS",
            }
        )


def test_load_config_valid_env() -> None:
    config = load_publication_notification_config(
        env={
            "NOTION_TOKEN": FAKE_NOTION_TOKEN,
            "NOTION_DATA_SOURCE_ID": DATA_SOURCE_ID,
            "SLACK_BOT_TOKEN": FAKE_SLACK_TOKEN,
            "SLACK_PAPERS_CHANNEL_ID": "C_PAPERS",
        }
    )
    assert config.notion_token.get_secret_value() == FAKE_NOTION_TOKEN
    assert config.data_source_id == DATA_SOURCE_ID
    assert config.slack_bot_token.get_secret_value() == FAKE_SLACK_TOKEN
    assert config.papers_channel_id == "C_PAPERS"


# --- read_publication_row -------------------------------------------------------------


def test_read_publication_row_reads_all_fields() -> None:
    row = read_publication_row(
        _publication_page(
            page_id="page-42",
            page_url="https://notion.so/page-42",
            canonical_key="doi:10.1/x",
            title="A Great Paper",
            doi="10.1/x",
            url="https://doi.org/10.1/x",
            researchers=["Ethan Goh", "Jane Doe"],
            source_label="OpenAlex",
            published_date="2026-02-01",
            why_it_matters="It matters because X",
            internal_summary="Summary text",
            key_story_angle="Angle text",
            draft_social_post="Short draft",
            slack_status="Pending",
        )
    )
    assert row.page_id == "page-42"
    assert row.page_url == "https://notion.so/page-42"
    assert row.canonical_key == "doi:10.1/x"
    assert row.title == "A Great Paper"
    assert row.doi == "10.1/x"
    assert row.url == "https://doi.org/10.1/x"
    assert row.researchers == ["Ethan Goh", "Jane Doe"]
    assert row.source_label == "OpenAlex"
    assert row.published_date == "2026-02-01"
    assert row.why_it_matters == "It matters because X"
    assert row.internal_summary == "Summary text"
    assert row.key_story_angle == "Angle text"
    assert row.draft_social_post == "Short draft"
    assert row.slack_status == "Pending"


# --- scan_publication_rows -----------------------------------------------------------


def test_scan_pending_rows_are_bucketed() -> None:
    scan = scan_publication_rows([_publication_page(slack_status="Pending")])
    assert len(scan.pending) == 1
    assert scan.failed == []


def test_scan_failed_rows_are_bucketed_separately() -> None:
    scan = scan_publication_rows([_publication_page(slack_status="Failed")])
    assert scan.pending == []
    assert len(scan.failed) == 1


def test_scan_suppressed_rows_never_selected() -> None:
    scan = scan_publication_rows([_publication_page(slack_status="Suppressed")])
    assert scan.pending == []
    assert scan.failed == []
    assert scan.historical_or_suppressed_skipped == 1


def test_scan_empty_slack_status_rows_treated_as_historical_never_selected() -> None:
    """A row with no Slack Status at all (pre-Slack-activation, not yet
    backfilled) must never be selected -- it is not "Pending" just because
    the property is empty."""
    scan = scan_publication_rows([_publication_page(slack_status=None)])
    assert scan.pending == []
    assert scan.failed == []
    assert scan.historical_or_suppressed_skipped == 1


def test_scan_sent_rows_never_selected() -> None:
    scan = scan_publication_rows([_publication_page(slack_status="Sent")])
    assert scan.pending == []
    assert scan.failed == []
    assert scan.sent_skipped == 1


def test_scan_duplicate_canonical_keys_excluded_from_every_bucket() -> None:
    pages = [
        _publication_page(page_id="page-a", canonical_key="doi:10.1/dup", slack_status="Pending"),
        _publication_page(page_id="page-b", canonical_key="doi:10.1/dup", slack_status="Pending"),
    ]
    scan = scan_publication_rows(pages)
    assert scan.pending == []
    assert scan.duplicate_keys == {"doi:10.1/dup": ["page-a", "page-b"]}


def test_scan_malformed_rows_counted_and_excluded() -> None:
    pages = [
        _publication_page(canonical_key=None),
        _publication_page(page_id=None),
    ]
    scan = scan_publication_rows(pages)
    assert scan.malformed_rows == 2
    assert scan.pending == []


def test_scan_rows_scanned_counts_everything() -> None:
    pages = [
        _publication_page(page_id="a", canonical_key="doi:10.1/a", slack_status="Pending"),
        _publication_page(page_id="b", canonical_key="doi:10.1/b", slack_status="Sent"),
        _publication_page(page_id="c", canonical_key="doi:10.1/c", slack_status="Suppressed"),
    ]
    scan = scan_publication_rows(pages)
    assert scan.rows_scanned == 3


# --- select_candidates -----------------------------------------------------------


def test_select_candidates_default_only_pending() -> None:
    pages = [
        _publication_page(page_id="a", canonical_key="doi:10.1/a", slack_status="Pending"),
        _publication_page(page_id="b", canonical_key="doi:10.1/b", slack_status="Failed"),
    ]
    scan = scan_publication_rows(pages)
    candidates = select_candidates(scan)
    assert [c.page_id for c in candidates] == ["a"]


def test_select_candidates_retry_failed_includes_failed_rows() -> None:
    pages = [
        _publication_page(page_id="a", canonical_key="doi:10.1/a", slack_status="Pending"),
        _publication_page(page_id="b", canonical_key="doi:10.1/b", slack_status="Failed"),
    ]
    scan = scan_publication_rows(pages)
    candidates = select_candidates(scan, retry_failed=True)
    assert {c.page_id for c in candidates} == {"a", "b"}


def test_select_candidates_canonical_key_filters() -> None:
    pages = [
        _publication_page(page_id="a", canonical_key="doi:10.1/a", slack_status="Pending"),
        _publication_page(page_id="b", canonical_key="doi:10.1/b", slack_status="Pending"),
    ]
    scan = scan_publication_rows(pages)
    candidates = select_candidates(scan, canonical_key="doi:10.1/b")
    assert [c.page_id for c in candidates] == ["b"]


def test_select_candidates_limit_caps_results() -> None:
    pages = [
        _publication_page(page_id=f"p{i}", canonical_key=f"doi:10.1/{i}", slack_status="Pending")
        for i in range(5)
    ]
    scan = scan_publication_rows(pages)
    candidates = select_candidates(scan, limit=2)
    assert len(candidates) == 2


# --- rendering -----------------------------------------------------------------


def test_render_slack_text_is_complete_and_readable() -> None:
    row = read_publication_row(
        _publication_page(
            title="Some Paper",
            researchers=["Ethan Goh"],
            published_date="2026-01-15",
            why_it_matters="Because it changes practice",
            key_story_angle="A fresh angle",
        )
    )
    text = render_slack_text(row)
    assert "📄 NEW ARISE RESEARCH" in text
    assert "Some Paper" in text
    assert "ARISE researcher(s):" in text and "Ethan Goh" in text
    assert "Published in:" in text and "OpenAlex" in text
    assert "Published:" in text and "2026-01-15" in text
    assert "Why it matters:" in text and "Because it changes practice" in text
    assert "Story angle:" in text and "A fresh angle" in text
    assert "Publication link: https://doi.org/10.1000/abc" in text
    assert "Notion page: https://notion.so/page-1" in text
    assert "Canonical Key: doi:10.1000/abc" in text


def test_render_slack_text_falls_back_from_why_it_matters_to_internal_summary() -> None:
    row = read_publication_row(
        _publication_page(why_it_matters=None, internal_summary="Fallback summary")
    )
    text = render_slack_text(row)
    assert "Fallback summary" in text


def test_render_slack_text_has_safe_fallback_when_no_summary_available() -> None:
    row = read_publication_row(_publication_page(why_it_matters=None, internal_summary=None))
    text = render_slack_text(row)
    assert "open the Notion record" in text


def test_render_slack_text_omits_story_angle_when_absent() -> None:
    row = read_publication_row(_publication_page(key_story_angle=None))
    text = render_slack_text(row)
    assert "Story angle:" not in text


def test_render_slack_text_includes_short_draft_social_post() -> None:
    row = read_publication_row(_publication_page(draft_social_post="A short, punchy draft post."))
    text = render_slack_text(row)
    assert "Draft social post preview:" in text
    assert "A short, punchy draft post." in text


def test_render_slack_text_omits_long_draft_social_post() -> None:
    long_draft = "x" * 500
    row = read_publication_row(_publication_page(draft_social_post=long_draft))
    text = render_slack_text(row)
    assert "Draft social post preview:" not in text
    assert long_draft not in text


def test_render_slack_text_falls_back_to_doi_link_without_url() -> None:
    row = read_publication_row(_publication_page(url=None, doi="10.9/x"))
    text = render_slack_text(row)
    assert "https://doi.org/10.9/x" in text


def test_render_slack_blocks_has_header_and_canonical_key_context() -> None:
    row = read_publication_row(_publication_page())
    blocks = render_slack_blocks(row)
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"]["text"] == "📄 NEW ARISE RESEARCH"
    context_block = blocks[-1]
    assert context_block["type"] == "context"
    assert "doi:10.1000/abc" in context_block["elements"][0]["text"]


# --- deliver_publication_notification: dry run --------------------------------------


def test_deliver_dry_run_makes_no_calls_and_previews(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not call Slack")

    def notion_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not call Notion")

    row = read_publication_row(_publication_page())
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=True,
    )

    assert result.action == "dry_run_preview"
    assert "NEW ARISE RESEARCH" in result.detail


# --- deliver_publication_notification: success --------------------------------------


def test_deliver_success_sets_sent_saves_ts_and_notified_date_clears_error(
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

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=False,
    )

    assert result.action == "sent"
    assert result.ts == "1700000000.000100"
    props = patched_body["properties"]
    assert props["Slack Status"] == {"select": {"name": "Sent"}}
    assert props["Slack Timestamp"]["rich_text"][0]["text"]["content"] == "1700000000.000100"
    assert props["Slack Error"]["rich_text"][0]["text"]["content"] == ""
    notified_start = props["Slack Notified Date"]["date"]["start"]
    assert notified_start.startswith("20")  # a real ISO datetime was written
    assert "Status" not in props
    assert "Relevance Status" not in props
    assert "Editorial Notes" not in props
    assert "Draft Status" not in props
    assert "Internal Summary" not in props


# --- deliver_publication_notification: failure --------------------------------------


def test_deliver_slack_ok_false_sets_failed_and_saves_error(
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

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=False,
    )

    assert result.action == "failed"
    assert result.stage == "slack_post"
    assert result.slack_error_code == "channel_not_found"
    props = patched_body["properties"]
    assert props["Slack Status"] == {"select": {"name": "Failed"}}
    assert "channel_not_found" in props["Slack Error"]["rich_text"][0]["text"]["content"]
    assert "Status" not in props
    assert "Relevance Status" not in props


def test_deliver_slack_transport_failure_propagates_status_code(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal_error"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "page-1"})

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler, max_retries=0),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
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

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
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

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler, max_retries=0),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=False,
    )

    assert result.action == "failed"
    assert result.stage == "notion_update_after_send"
    assert "1700000000.000100" in result.detail


# --- retry: successful retry clears error --------------------------------------


def test_successful_retry_clears_previous_error(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    patched_body: dict = {}

    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "ts": "1700000000.000900"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        nonlocal patched_body
        patched_body = json.loads(request.content)
        return httpx.Response(200, json={"id": "page-1"})

    row = read_publication_row(_publication_page(page_id="page-1", slack_status="Failed"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=False,
    )

    assert result.action == "sent"
    assert patched_body["properties"]["Slack Error"]["rich_text"][0]["text"]["content"] == ""


# --- token never appears in any raised error text -----------------------------------


def test_slack_error_never_contains_the_bot_token(
    mock_slack_client: Callable[..., SlackClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def slack_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "page-1"})

    row = read_publication_row(_publication_page(page_id="page-1"))
    result = deliver_publication_notification(
        mock_slack_client(slack_handler, max_retries=0),
        mock_notion_client(notion_handler),
        DATA_SOURCE_ID,
        row,
        channel_id="C_PAPERS",
        dry_run=False,
    )

    assert FAKE_SLACK_TOKEN not in result.detail
    assert FAKE_SLACK_TOKEN not in (result.slack_error_code or "")
