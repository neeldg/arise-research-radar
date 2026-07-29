import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.citations import CitationEvent
from arise_radar.sinks.notion import NotionClient, NotionConfigError
from arise_radar.sinks.notion_citations import (
    CitationRowIndex,
    extract_tracked_works,
    load_citation_row_index,
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


# --- load_citation_row_index: pure indexing over already-fetched pages -------------


def _citation_page(citation_key: str | None, page_id: str | None = "page-1") -> dict:
    props: dict = {}
    if citation_key is not None:
        props["Citation Key"] = {"type": "rich_text", "rich_text": [{"plain_text": citation_key}]}
    page: dict = {"properties": props}
    if page_id is not None:
        page["id"] = page_id
    return page


def test_load_citation_row_index_empty_input() -> None:
    index = load_citation_row_index([])
    assert index.key_to_page_id == {}
    assert index.duplicate_keys == {}
    assert index.total_rows_loaded == 0
    assert index.malformed_rows == 0


def test_load_citation_row_index_single_unique_key() -> None:
    index = load_citation_row_index([_citation_page("citation:W900:W100", "page-1")])
    assert index.key_to_page_id == {"citation:W900:W100": "page-1"}
    assert index.duplicate_keys == {}
    assert index.total_rows_loaded == 1
    assert index.malformed_rows == 0


def test_load_citation_row_index_flags_duplicate_key_and_excludes_it_from_lookup_map() -> None:
    pages = [
        _citation_page("citation:W900:W100", "page-a"),
        _citation_page("citation:W900:W100", "page-b"),
    ]
    index = load_citation_row_index(pages)

    assert index.key_to_page_id == {}  # ambiguous key never usable for direct lookup
    assert index.duplicate_keys == {"citation:W900:W100": ["page-a", "page-b"]}
    assert index.total_rows_loaded == 2
    assert index.malformed_rows == 0


def test_load_citation_row_index_counts_malformed_rows_without_guessing() -> None:
    pages = [
        _citation_page(None, "page-1"),  # no Citation Key
        _citation_page("citation:W900:W100", None),  # no page id
        _citation_page("citation:W901:W100", "page-2"),  # fine
    ]
    index = load_citation_row_index(pages)

    assert index.malformed_rows == 2
    assert index.total_rows_loaded == 3
    assert index.key_to_page_id == {"citation:W901:W100": "page-2"}
    assert index.duplicate_keys == {}


# --- upsert_citation_event: create, matched against the in-memory index only ------


def test_new_citation_event_creates_page_with_no_pre_create_lookup(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    created_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    event = _event()
    index = CitationRowIndex()

    result = upsert_citation_event(client, DATA_SOURCE_ID, event, index)

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
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    event = _event(is_baseline=True, slack_status="Suppressed")

    result = upsert_citation_event(client, DATA_SOURCE_ID, event, CitationRowIndex())

    assert result.action == "created"
    props = created_body["properties"]
    assert props["Baseline"] == {"checkbox": True}
    assert props["Slack Status"] == {"select": {"name": "Suppressed"}}


def test_successful_create_adds_key_to_index_so_a_later_event_updates_not_creates(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    create_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/pages":
            create_calls["count"] += 1
            return httpx.Response(200, json={"id": "citation-page-1"})
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    index = CitationRowIndex()
    event = _event()

    first = upsert_citation_event(client, DATA_SOURCE_ID, event, index)
    second = upsert_citation_event(client, DATA_SOURCE_ID, event, index)

    assert first.action == "created"
    assert second.action == "updated"
    assert second.page_id == "citation-page-1"
    assert create_calls["count"] == 1  # only one row was ever created for the repeated event
    assert index.key_to_page_id[event.citation_key] == "citation-page-1"


# --- upsert_citation_event: update, using the known page id directly --------------


def test_existing_citation_key_updates_using_known_page_id_no_query(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    index = CitationRowIndex(key_to_page_id={"citation:W900:W100": "citation-page-1"})

    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), index)

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
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    index = CitationRowIndex(key_to_page_id={"citation:W900:W100": "citation-page-1"})
    # Even a baseline=True incoming event must not overwrite the existing
    # (already-reviewed, already-non-baseline-in-spirit) row's fields.
    upsert_citation_event(
        client, DATA_SOURCE_ID, _event(is_baseline=True, slack_status="Suppressed"), index
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


def test_duplicate_stored_key_is_skipped_without_any_notion_call(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not create, update, or query when the citation key is ambiguous")

    client = mock_notion_client(handler)
    index = CitationRowIndex(duplicate_keys={"citation:W900:W100": ["page-a", "page-b"]})

    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), index)

    assert result.action == "skipped_duplicate"
    assert "page-a" in result.detail
    assert "page-b" in result.detail


def test_create_failure_is_captured_as_error_with_stage_and_status_code(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/pages":
            return httpx.Response(500, json={"message": "internal error"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler, max_retries=0)
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), CitationRowIndex())

    assert result.action == "error"
    assert result.stage == "create"
    assert result.status_code == 500
    assert "create failed" in result.detail


def test_update_failure_is_captured_as_error_with_stage_and_status_code(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH" and request.url.path == "/v1/pages/citation-page-1":
            return httpx.Response(503, json={"message": "unavailable"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler, max_retries=0)
    index = CitationRowIndex(key_to_page_id={"citation:W900:W100": "citation-page-1"})
    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), index)

    assert result.action == "error"
    assert result.stage == "update"
    assert result.status_code == 503
    assert "update failed" in result.detail


# --- dry run: zero Notion calls at all, not just zero writes -----------------------


def test_dry_run_create_makes_no_notion_call(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not perform any Notion request")

    client = mock_notion_client(handler)
    result = upsert_citation_event(
        client, DATA_SOURCE_ID, _event(), CitationRowIndex(), dry_run=True
    )

    assert result.action == "dry_run_create"


def test_dry_run_update_makes_no_notion_call(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("dry run must not perform any Notion request")

    client = mock_notion_client(handler)
    index = CitationRowIndex(key_to_page_id={"citation:W900:W100": "citation-page-1"})

    result = upsert_citation_event(client, DATA_SOURCE_ID, _event(), index, dry_run=True)

    assert result.action == "dry_run_update"
    assert result.page_id == "citation-page-1"
