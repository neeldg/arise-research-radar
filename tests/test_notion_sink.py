import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.models import NormalizedPublication
from arise_radar.relevance import RelevanceDecision
from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    load_notion_config,
    load_notion_setup_config,
    upsert_publication,
)


def _pub(**overrides: object) -> NormalizedPublication:
    defaults: dict[str, object] = {
        "researcher_id": "ethan_goh",
        "researcher_name": "Ethan Goh",
        "title": "Clinical decision support with large language models",
        "publication_date": date(2026, 1, 1),
        "doi": "10.1000/abc",
        "openalex_id": "W1",
        "canonical_key": "doi:10.1000/abc",
    }
    defaults.update(overrides)
    return NormalizedPublication(**defaults)


KEEP = RelevanceDecision(status="keep", reason="matched a healthcare/clinical-AI signal")


# --- config -----------------------------------------------------------------


def test_missing_token_raises_clear_error() -> None:
    with pytest.raises(NotionConfigError, match="NOTION_TOKEN"):
        load_notion_config(env={"NOTION_DATA_SOURCE_ID": "ds_123"})


def test_missing_data_source_id_raises_clear_error() -> None:
    with pytest.raises(NotionConfigError, match="NOTION_DATA_SOURCE_ID"):
        load_notion_config(env={"NOTION_TOKEN": "secret"})


def test_missing_both_raises_clear_error() -> None:
    with pytest.raises(NotionConfigError, match="NOTION_TOKEN"):
        load_notion_config(env={})


def test_valid_env_loads_config() -> None:
    config = load_notion_config(env={"NOTION_TOKEN": "secret", "NOTION_DATA_SOURCE_ID": "ds_123"})
    assert config.data_source_id == "ds_123"
    assert config.token.get_secret_value() == "secret"
    assert "secret" not in repr(config)  # SecretStr must never leak into repr/str


def test_setup_config_missing_parent_page_id_raises() -> None:
    with pytest.raises(NotionConfigError, match="NOTION_PARENT_PAGE_ID"):
        load_notion_setup_config(env={"NOTION_TOKEN": "secret"})


def test_setup_config_valid_env_loads() -> None:
    config = load_notion_setup_config(
        env={"NOTION_TOKEN": "secret", "NOTION_PARENT_PAGE_ID": "page-123"}
    )
    assert config.parent_page_id == "page-123"
    assert config.token.get_secret_value() == "secret"


# --- upsert: create -----------------------------------------------------------


def test_new_publication_creates_page(mock_notion_client: Callable[..., NotionClient]) -> None:
    created_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-new-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    pub = _pub()

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "created"
    assert result.page_id == "page-new-1"
    assert created_body["parent"] == {"type": "data_source_id", "data_source_id": "ds_123"}
    props = created_body["properties"]
    assert props["Status"] == {"select": {"name": "New"}}
    assert props["Researchers"] == {"multi_select": [{"name": "Ethan Goh"}]}
    assert props["Canonical Key"]["rich_text"][0]["text"]["content"] == "doi:10.1000/abc"
    assert props["Detected Date"] == {"date": {"start": "2026-01-02"}}
    assert props["Relevance Status"] == {"select": {"name": "Kept"}}
    system_notes_text = props["System Notes"]["rich_text"][0]["text"]["content"]
    assert "matched a healthcare/clinical-AI signal" in system_notes_text
    assert "Editorial Notes" not in props


# --- upsert: update -----------------------------------------------------------


def _existing_page(page_id: str, status: str, researchers: list[str]) -> dict:
    return {
        "id": page_id,
        "properties": {
            "Status": {"select": {"name": status}},
            "Researchers": {"multi_select": [{"name": name} for name in researchers]},
        },
    }


def test_existing_canonical_key_updates_page_not_creates(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(
                200, json={"results": [_existing_page("page-1", "Reviewed", ["Ethan Goh"])]}
            )
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    pub = _pub()

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "updated"
    assert result.page_id == "page-1"


def test_existing_editorial_status_is_preserved(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    updated_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(
                200, json={"results": [_existing_page("page-1", "Reviewed", ["Ethan Goh"])]}
            )
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    pub = _pub()

    upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert "Status" not in updated_body["properties"]
    assert "Detected Date" not in updated_body["properties"]
    assert "Editorial Notes" not in updated_body["properties"]
    assert "System Notes" in updated_body["properties"]


def test_uncertain_decision_maps_to_uncertain_relevance_status(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    created_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-new-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    pub = _pub()
    uncertain = RelevanceDecision(status="uncertain", reason="no clear signal found")

    upsert_publication(client, "ds_123", pub, uncertain, detected_date=date(2026, 1, 2))

    assert created_body["properties"]["Relevance Status"] == {"select": {"name": "Uncertain"}}


def test_shared_publication_merges_researcher_names(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    updated_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(
                200, json={"results": [_existing_page("page-1", "New", ["Ethan Goh"])]}
            )
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    pub = _pub(researcher_id="adam_rodman", researcher_name="Adam Rodman")

    upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    names = {entry["name"] for entry in updated_body["properties"]["Researchers"]["multi_select"]}
    assert names == {"Ethan Goh", "Adam Rodman"}


# --- exclusion, duplicates, failures ------------------------------------------


def test_excluded_publication_raises_and_is_never_sent(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Notion must never be called for an excluded publication")

    client = mock_notion_client(handler)
    pub = _pub()
    exclude_decision = RelevanceDecision(status="exclude", reason="unrelated domain")

    with pytest.raises(ValueError, match="excluded"):
        upsert_publication(client, "ds_123", pub, exclude_decision, detected_date=date(2026, 1, 2))


def test_multiple_pages_same_canonical_key_is_flagged_and_not_written(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(
                200,
                json={
                    "results": [
                        _existing_page("page-a", "New", []),
                        _existing_page("page-b", "New", []),
                    ]
                },
            )
        raise AssertionError("must not create or update when the canonical key is ambiguous")

    client = mock_notion_client(handler)
    pub = _pub()

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "skipped_duplicate"
    assert "page-a" in result.detail
    assert "page-b" in result.detail


def test_notion_failure_is_captured_as_error_not_raised(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            return httpx.Response(500, json={"message": "internal error"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler, max_retries=0)
    pub = _pub()

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "error"
    assert "create failed" in result.detail


# --- dry run -------------------------------------------------------------------


def test_dry_run_create_makes_no_write_request(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    client = mock_notion_client(handler)
    pub = _pub()

    result = upsert_publication(
        client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2), dry_run=True
    )

    assert result.action == "dry_run_create"


def test_dry_run_update_makes_no_write_request(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(
                200, json={"results": [_existing_page("page-1", "New", ["Ethan Goh"])]}
            )
        raise AssertionError("dry run must not perform create/update requests")

    client = mock_notion_client(handler)
    pub = _pub(researcher_id="adam_rodman", researcher_name="Adam Rodman")

    result = upsert_publication(
        client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2), dry_run=True
    )

    assert result.action == "dry_run_update"
    assert "Adam Rodman" in result.detail
