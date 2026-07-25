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
        "work_type": "article",
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


# --- work-type classification: create-only Draft Status/Draft Error ----------------


def test_draft_eligible_work_type_never_touches_draft_status(
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
    pub = _pub(work_type="article")

    upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert "Draft Status" not in created_body["properties"]
    assert "Draft Error" not in created_body["properties"]
    notes = created_body["properties"]["System Notes"]["rich_text"][0]["text"]["content"]
    assert "Work type: article" in notes


def test_zenodo_repository_defaults_to_needs_attention_on_create(
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
    pub = _pub(
        title="Stanford Biodesign Digital Health Group",
        doi="10.5281/zenodo.21358702",
        canonical_key="doi:10.5281/zenodo.21358702",
        work_type=None,
    )

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "created"
    props = created_body["properties"]
    assert props["Draft Status"] == {"select": {"name": "Needs Attention"}}
    error_text = props["Draft Error"]["rich_text"][0]["text"]["content"]
    assert "dataset/repository" in error_text
    notes = props["System Notes"]["rich_text"][0]["text"]["content"]
    assert "Work type: dataset/repository" in notes


def test_osf_registration_defaults_to_needs_attention_on_create(
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
    pub = _pub(
        title="GP AI Skin Cancer Portal Study",
        doi="10.17605/osf.io/evxkq",
        canonical_key="doi:10.17605/osf.io/evxkq",
        work_type=None,
    )

    result = upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert result.action == "created"
    props = created_body["properties"]
    assert props["Draft Status"] == {"select": {"name": "Needs Attention"}}
    error_text = props["Draft Error"]["rich_text"][0]["text"]["content"]
    assert "protocol/registration" in error_text


def test_non_eligible_work_type_never_touches_draft_status_on_update(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """A resync of a non-eligible row must not clobber Draft Status/Draft
    Error a human or the drafting pipeline may have since set (e.g. a human
    manually Approved it, or --force drafted it anyway)."""
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
    pub = _pub(
        title="Stanford Biodesign Digital Health Group",
        doi="10.5281/zenodo.21358702",
        canonical_key="doi:10.5281/zenodo.21358702",
        work_type=None,
    )

    upsert_publication(client, "ds_123", pub, KEEP, detected_date=date(2026, 1, 2))

    assert "Draft Status" not in updated_body["properties"]
    assert "Draft Error" not in updated_body["properties"]


# --- researcher_names: same-run merging without a prior Notion write ---------------


def test_researcher_names_param_creates_page_with_all_names(
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

    result = upsert_publication(
        client,
        "ds_123",
        pub,
        KEEP,
        detected_date=date(2026, 1, 2),
        researcher_names={"Ethan Goh", "Adam Rodman"},
    )

    assert result.action == "created"
    names = {entry["name"] for entry in created_body["properties"]["Researchers"]["multi_select"]}
    assert names == {"Ethan Goh", "Adam Rodman"}


def test_dry_run_create_with_researcher_names_reports_both_in_one_line(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    client = mock_notion_client(handler)
    pub = _pub()

    result = upsert_publication(
        client,
        "ds_123",
        pub,
        KEEP,
        detected_date=date(2026, 1, 2),
        dry_run=True,
        researcher_names={"Ethan Goh", "Adam Rodman"},
    )

    assert result.action == "dry_run_create"
    assert "Ethan Goh" in result.detail
    assert "Adam Rodman" in result.detail


# --- version_duplicate_note: appended to System Notes, canonical key untouched -----


def test_version_duplicate_note_appended_to_system_notes(
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
    note = (
        "Possible version duplicate of: doi:10.20944/preprints202607.0060.v1 "
        "(title similarity 0.96, shared authors: Ethan Goh)"
    )

    upsert_publication(
        client,
        "ds_123",
        pub,
        KEEP,
        detected_date=date(2026, 1, 2),
        version_duplicate_note=note,
    )

    notes_text = created_body["properties"]["System Notes"]["rich_text"][0]["text"]["content"]
    assert note in notes_text
    # Canonical key itself is untouched by the version-duplicate note.
    assert created_body["properties"]["Canonical Key"]["rich_text"][0]["text"]["content"] == (
        pub.canonical_key
    )


def test_no_version_duplicate_note_by_default(
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
    upsert_publication(client, "ds_123", _pub(), KEEP, detected_date=date(2026, 1, 2))

    notes_text = created_body["properties"]["System Notes"]["rich_text"][0]["text"]["content"]
    assert "Possible version duplicate" not in notes_text
