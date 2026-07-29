import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.citations import CitationEvent
from arise_radar.sinks.notion import NotionClient, NotionConfigError
from arise_radar.sinks.notion_citations import (
    extract_tracked_works,
    load_notion_citations_config,
    upsert_citation_event,
)

DATA_SOURCE_ID = "citations-ds-123"


def _event(**overrides: object) -> CitationEvent:
    defaults: dict[str, object] = {
        "citation_key": "citation:W900:W100",
        "citing_title": "A Citing Paper",
        "citing_openalex_id": "W900",
        "citing_doi": "10.2/citing",
        "citing_url": "https://doi.org/10.2/citing",
        "citing_authors": ["Someone Else"],
        "citing_publication_date": date(2026, 1, 15),
        "cited_openalex_id": "W100",
        "cited_canonical_key": "doi:10.1/tracked",
        "cited_title": "Tracked Paper",
        "arise_researchers": ["Ethan Goh"],
        "detected_date": date(2026, 1, 20),
        "is_baseline": False,
        "slack_status": "Pending",
    }
    defaults.update(overrides)
    return CitationEvent(**defaults)


# --- load_notion_citations_config --------------------------------------------------


def test_load_notion_citations_config_requires_all_three_vars() -> None:
    with pytest.raises(NotionConfigError, match="NOTION_CITATIONS_DATA_SOURCE_ID"):
        load_notion_citations_config(
            env={"NOTION_TOKEN": "secret", "NOTION_DATA_SOURCE_ID": "pubs-ds"}
        )


def test_load_notion_citations_config_valid_env() -> None:
    config = load_notion_citations_config(
        env={
            "NOTION_TOKEN": "secret",
            "NOTION_DATA_SOURCE_ID": "pubs-ds",
            "NOTION_CITATIONS_DATA_SOURCE_ID": "citations-ds",
        }
    )
    assert config.publications_data_source_id == "pubs-ds"
    assert config.citations_data_source_id == "citations-ds"
    assert config.token.get_secret_value() == "secret"


# --- extract_tracked_works: missing OpenAlex IDs skipped clearly -------------------


def _publication_page(
    *, openalex_id: str | None, title: str = "A Paper", canonical_key: str = "doi:10.1/x"
) -> dict:
    props: dict = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "Canonical Key": {"type": "rich_text", "rich_text": [{"plain_text": canonical_key}]},
        "Researchers": {"type": "multi_select", "multi_select": [{"name": "Ethan Goh"}]},
    }
    if openalex_id is not None:
        props["OpenAlex ID"] = {"type": "rich_text", "rich_text": [{"plain_text": openalex_id}]}
    return {"id": "page-1", "properties": props}


def test_extract_tracked_works_keeps_rows_with_openalex_id() -> None:
    pages = [_publication_page(openalex_id="W100")]
    result = extract_tracked_works(pages)

    assert result.total_inspected == 1
    assert result.skipped_no_openalex_id == 0
    assert len(result.tracked) == 1
    assert result.tracked[0].openalex_id == "W100"
    assert result.tracked[0].canonical_key == "doi:10.1/x"
    assert result.tracked[0].researchers == ["Ethan Goh"]


def test_extract_tracked_works_skips_rows_without_openalex_id_clearly() -> None:
    pages = [
        _publication_page(openalex_id="W100"),
        _publication_page(openalex_id=None, title="No OpenAlex ID Paper"),
    ]
    result = extract_tracked_works(pages)

    assert result.total_inspected == 2
    assert result.skipped_no_openalex_id == 1
    assert len(result.tracked) == 1
    assert result.tracked[0].openalex_id == "W100"


def test_extract_tracked_works_all_missing_reports_all_skipped() -> None:
    pages = [_publication_page(openalex_id=None), _publication_page(openalex_id=None)]
    result = extract_tracked_works(pages)

    assert result.total_inspected == 2
    assert result.skipped_no_openalex_id == 2
    assert result.tracked == []


def test_extract_tracked_works_empty_input() -> None:
    result = extract_tracked_works([])
    assert result.total_inspected == 0
    assert result.skipped_no_openalex_id == 0
    assert result.tracked == []


# --- upsert_citation_event: create ---------------------------------------------------


def test_new_citation_event_creates_page(mock_notion_client: Callable[..., NotionClient]) -> None:
    created_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    event = _event()

    result = upsert_citation_event(client, DATA_SOURCE_ID, event)

    assert result.action == "created"
    assert result.page_id == "citation-page-1"
    props = created_body["properties"]
    assert props["Citation Key"]["rich_text"][0]["text"]["content"] == "citation:W900:W100"
    assert props["Citing OpenAlex ID"]["rich_text"][0]["text"]["content"] == "W900"
    assert props["Citing DOI"]["rich_text"][0]["text"]["content"] == "10.2/citing"
    assert props["Citing URL"] == {"url": "https://doi.org/10.2/citing"}
    assert props["Cited ARISE Paper"]["rich_text"][0]["text"]["content"] == (
        "Tracked Paper (doi:10.1/tracked)"
    )
    assert props["ARISE Researchers"] == {"multi_select": [{"name": "Ethan Goh"}]}
    assert props["Published Date"] == {"date": {"start": "2026-01-15"}}
    assert props["Detected Date"] == {"date": {"start": "2026-01-20"}}
    assert props["Baseline"] == {"checkbox": False}
    assert props["Review Status"] == {"select": {"name": "New"}}
    assert props["Citation Relationship"] == {"select": {"name": "Unclassified"}}
    assert props["Relationship Evidence"]["rich_text"][0]["text"]["content"] == ""
    assert props["Slack Status"] == {"select": {"name": "Pending"}}
    assert "Slack Timestamp" not in props


def test_baseline_citation_event_creates_with_suppressed_status(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    created_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    event = _event(is_baseline=True, slack_status="Suppressed")

    result = upsert_citation_event(client, DATA_SOURCE_ID, event)

    assert result.action == "created"
    props = created_body["properties"]
    assert props["Baseline"] == {"checkbox": True}
    assert props["Slack Status"] == {"select": {"name": "Suppressed"}}


# --- upsert_citation_event: update, preserving human-edited fields ----------------


def _existing_citation_page(page_id: str = "citation-page-1") -> dict:
    return {
        "id": page_id,
        "properties": {
            "Citation Key": {
                "type": "rich_text",
                "rich_text": [{"plain_text": "citation:W900:W100"}],
            },
            "Review Status": {"type": "select", "select": {"name": "Approved"}},
            "Baseline": {"type": "checkbox", "checkbox": True},
            "Slack Status": {"type": "select", "select": {"name": "Sent"}},
            "Citation Relationship": {"type": "select", "select": {"name": "Extends"}},
        },
    }


def test_existing_citation_key_updates_page_not_creates(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [_existing_citation_page()]})
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event())

    assert result.action == "updated"
    assert result.page_id == "citation-page-1"


def test_update_never_touches_baseline_review_status_or_slack_fields(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """A rerun (baseline or incremental) that finds an already-existing
    citation key must never reset Baseline, Review Status, Slack Status,
    Slack Timestamp, Citation Relationship, or Relationship Evidence --
    those are human-owned or reserved for a later phase."""
    updated_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [_existing_citation_page()]})
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    # Even a baseline=True incoming event must not overwrite the existing
    # (already-reviewed, already-non-baseline-in-spirit) row's fields.
    upsert_citation_event(
        client, DATA_SOURCE_ID, _event(is_baseline=True, slack_status="Suppressed")
    )

    props = updated_body["properties"]
    assert "Baseline" not in props
    assert "Review Status" not in props
    assert "Slack Status" not in props
    assert "Slack Timestamp" not in props
    assert "Citation Relationship" not in props
    assert "Relationship Evidence" not in props
    assert "Detected Date" not in props
    # Shared/pipeline-owned fields are still refreshed.
    assert "Citation Key" in props
    assert "Citing OpenAlex ID" in props
    assert "ARISE Researchers" in props


# --- exclusion / duplicate / failure handling ---------------------------------------


def test_multiple_pages_same_citation_key_is_flagged_and_not_written(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(
                200,
                json={
                    "results": [
                        _existing_citation_page("page-a"),
                        _existing_citation_page("page-b"),
                    ]
                },
            )
        raise AssertionError("must not create or update when the citation key is ambiguous")

    client = mock_notion_client(handler)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event())

    assert result.action == "skipped_duplicate"
    assert "page-a" in result.detail
    assert "page-b" in result.detail


def test_notion_failure_is_captured_as_error_not_raised(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            return httpx.Response(500, json={"message": "internal error"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler, max_retries=0)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event())

    assert result.action == "error"
    assert "create failed" in result.detail


# --- dry run -------------------------------------------------------------------------


def test_dry_run_create_makes_no_write_request(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    client = mock_notion_client(handler)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), dry_run=True)

    assert result.action == "dry_run_create"


def test_dry_run_update_makes_no_write_request(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [_existing_citation_page()]})
        raise AssertionError("dry run must not perform create/update requests")

    client = mock_notion_client(handler)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), dry_run=True)

    assert result.action == "dry_run_update"
    assert result.page_id == "citation-page-1"
