import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.setup_citations_notion import (
    DATA_SOURCE_NAME,
    DATABASE_TITLE,
    build_repair_properties,
    build_schema_properties,
    diff_schema,
    main,
    update_env_file,
)

from arise_radar.sinks.notion import NotionClient

PARENT_PAGE_ID = "page-123"
DATABASE_ID = "existing-db"
DATA_SOURCE_ID = "ds-456"


def _page_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": PARENT_PAGE_ID,
            "archived": False,
            "properties": {
                "title": {"type": "title", "title": [{"plain_text": "ARISE Research Radar"}]}
            },
        },
    )


def _no_existing_database_response() -> httpx.Response:
    return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})


def _existing_database_block_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "type": "child_database",
                    "id": DATABASE_ID,
                    "child_database": {"title": DATABASE_TITLE},
                }
            ],
            "has_more": False,
            "next_cursor": None,
        },
    )


def _database_response(data_source_name: str = DATA_SOURCE_NAME) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": DATABASE_ID,
            "data_sources": [{"id": DATA_SOURCE_ID, "name": data_source_name}],
        },
    )


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-token-value")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", PARENT_PAGE_ID)


# --- diff_schema / build_repair_properties: pure logic, no I/O ---------------------


def test_diff_schema_all_missing() -> None:
    diff = diff_schema({}, build_schema_properties())
    assert set(diff.missing_properties) == set(build_schema_properties())
    assert diff.type_conflicts == []
    assert diff.missing_select_options == {}
    assert not diff.is_exact_match
    assert diff.is_repairable


def test_diff_schema_exact_match() -> None:
    desired = build_schema_properties()
    existing = {
        name: {"type": next(iter(definition)), **definition} for name, definition in desired.items()
    }
    diff = diff_schema(existing, desired)
    assert diff.is_exact_match
    assert not diff.is_repairable


def test_diff_schema_type_conflict_is_not_repairable() -> None:
    existing = {"Baseline": {"type": "rich_text", "rich_text": {}}}
    diff = diff_schema(existing, {"Baseline": {"checkbox": {}}})
    assert len(diff.type_conflicts) == 1
    assert diff.type_conflicts[0].existing_type == "rich_text"
    assert diff.type_conflicts[0].desired_type == "checkbox"
    assert not diff.is_repairable


def test_diff_schema_missing_select_option_is_repairable() -> None:
    existing = {
        "Review Status": {
            "type": "select",
            "select": {"options": [{"name": "New"}, {"name": "Approved"}]},
        }
    }
    desired = {
        "Review Status": {
            "select": {"options": [{"name": "New"}, {"name": "Approved"}, {"name": "Rejected"}]}
        }
    }
    diff = diff_schema(existing, desired)
    assert diff.missing_properties == []
    assert diff.type_conflicts == []
    assert diff.missing_select_options == {"Review Status": ["Rejected"]}
    assert diff.is_repairable


def test_build_repair_properties_never_drops_existing_options() -> None:
    existing = {
        "Review Status": {
            "type": "select",
            "select": {"options": [{"name": "New"}, {"name": "Approved"}]},
        }
    }
    desired = {
        "Review Status": {
            "select": {"options": [{"name": "New"}, {"name": "Approved"}, {"name": "Rejected"}]}
        },
        "System Notes": {"rich_text": {}},
    }
    diff = diff_schema(existing, desired)
    repair = build_repair_properties(existing, desired, diff)

    assert set(repair) == {"Review Status", "System Notes"}
    repaired_options = {opt["name"] for opt in repair["Review Status"]["select"]["options"]}
    assert repaired_options == {"New", "Approved", "Rejected"}
    assert repair["System Notes"] == {"rich_text": {}}


# --- update_env_file: pure file I/O -------------------------------------------------


def test_update_env_file_inserts_new_key_preserving_other_lines(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("NOTION_TOKEN=abc\nNOTION_DATA_SOURCE_ID=xyz\n")

    update_env_file(env_path, "NOTION_CITATIONS_DATA_SOURCE_ID", "new-ds-id")

    content = env_path.read_text()
    assert content == (
        "NOTION_TOKEN=abc\nNOTION_DATA_SOURCE_ID=xyz\nNOTION_CITATIONS_DATA_SOURCE_ID=new-ds-id\n"
    )


def test_update_env_file_replaces_existing_key_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment\nNOTION_TOKEN=abc\nNOTION_CITATIONS_DATA_SOURCE_ID=old-id\n"
        "ANTHROPIC_API_KEY=sk\n"
    )

    update_env_file(env_path, "NOTION_CITATIONS_DATA_SOURCE_ID", "new-ds-id")

    content = env_path.read_text()
    assert content == (
        "# comment\nNOTION_TOKEN=abc\nNOTION_CITATIONS_DATA_SOURCE_ID=new-ds-id\n"
        "ANTHROPIC_API_KEY=sk\n"
    )


def test_update_env_file_creates_missing_file(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    update_env_file(env_path, "NOTION_CITATIONS_DATA_SOURCE_ID", "new-ds-id")

    assert env_path.read_text() == "NOTION_CITATIONS_DATA_SOURCE_ID=new-ds-id\n"


# --- CLI: credentials / access errors ------------------------------------------------


def test_missing_credentials_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_PARENT_PAGE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_PARENT_PAGE_ID" in captured.err


def test_401_gives_clear_token_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Unauthorized"})

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "401" in captured.err
    assert "NOTION_TOKEN" in captured.err
    assert "secret-token-value" not in captured.out
    assert "secret-token-value" not in captured.err


def test_403_gives_clear_permission_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "403" in captured.err
    assert "integration" in captured.err.lower()


def test_404_gives_clear_not_found_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not found"})

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "404" in captured.err
    assert "NOTION_PARENT_PAGE_ID" in captured.err


def test_409_gives_clear_conflict_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        return httpx.Response(409, json={"message": "Conflict"})

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "409" in captured.err
    assert "conflict" in captured.err.lower()


def test_429_retries_then_gives_clear_rate_limit_explanation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(429, json={"message": "rate limited"})

    client = mock_notion_client(handler, max_retries=1)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert attempts["count"] == 2  # confirms the existing retry/backoff path was used, not bypassed
    assert "429" in captured.err
    assert "rate-limited" in captured.err.lower()


def test_archived_parent_page_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": PARENT_PAGE_ID, "archived": True, "properties": {}})

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "archived" in captured.err


# --- CLI: creation path (no existing database) ---------------------------------------


def test_default_run_with_nothing_existing_previews_creation_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _no_existing_database_response()
        raise AssertionError(f"read-only run must not write: {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No existing" in captured.out
    assert "Would create database" in captured.out
    assert "Citation Key" in captured.out
    assert "no create requests were made" in captured.out.lower()


def test_write_notion_creates_database_and_data_source(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    sink: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _no_existing_database_response()
        if request.method == "POST" and request.url.path == "/v1/databases":
            sink["database"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "new-db-id", "url": "https://notion.so/x"})
        if request.method == "POST" and request.url.path == "/v1/data_sources":
            sink["data_source"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "new-ds-id"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Database ID:    new-db-id" in captured.out
    assert "Data source ID: new-ds-id" in captured.out
    assert "NOTION_CITATIONS_DATA_SOURCE_ID=new-ds-id" in captured.out
    assert "secret-token-value" not in captured.out

    assert sink["database"]["parent"] == {"type": "page_id", "page_id": PARENT_PAGE_ID}
    ds_properties = sink["data_source"]["properties"]
    assert set(ds_properties) == set(build_schema_properties())
    relationship_options = {
        opt["name"] for opt in ds_properties["Citation Relationship"]["select"]["options"]
    }
    assert relationship_options == {
        "Unclassified",
        "Applies",
        "Extends",
        "Validates",
        "Critiques",
        "Mentions",
    }


def test_write_notion_with_update_env_writes_env_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    tmp_path: Path,
) -> None:
    _set_env(monkeypatch)
    env_path = tmp_path / ".env"
    env_path.write_text("NOTION_TOKEN=secret-token-value\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _no_existing_database_response()
        if request.method == "POST" and request.url.path == "/v1/databases":
            return httpx.Response(200, json={"id": "new-db-id", "url": "https://notion.so/x"})
        if request.method == "POST" and request.url.path == "/v1/data_sources":
            return httpx.Response(200, json={"id": "new-ds-id"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion", "--update-env", "--env-file", str(env_path)], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Updated {env_path}" in captured.out
    content = env_path.read_text()
    assert "NOTION_CITATIONS_DATA_SOURCE_ID=new-ds-id" in content
    assert "NOTION_TOKEN=secret-token-value" in content


def test_update_env_without_write_notion_does_not_touch_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    tmp_path: Path,
) -> None:
    _set_env(monkeypatch)
    env_path = tmp_path / ".env"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _no_existing_database_response()
        raise AssertionError("must not write anything without --write-notion")

    client = mock_notion_client(handler)
    exit_code = main(["--update-env", "--env-file", str(env_path)], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert not env_path.exists()
    assert "requires --write-notion" in captured.out


# --- CLI: idempotency / existing-database paths ---------------------------------------


def test_running_default_mode_twice_never_creates_a_second_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    desired = build_schema_properties()
    existing_properties = {
        name: {"type": next(iter(definition)), **definition} for name, definition in desired.items()
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response()
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing_properties})
        raise AssertionError(f"must never write: {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Found existing database" in captured.out
    assert "matches exactly" in captured.out


def test_existing_data_source_missing_properties_reports_diff_and_requires_repair_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    existing_properties = {
        "Name": {"type": "title", "title": {}},
        "Citation Key": {"type": "rich_text", "rich_text": {}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response()
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing_properties})
        raise AssertionError(
            f"must not write without --repair-schema: {request.method} {request.url}"
        )

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Missing" in captured.out
    assert "Baseline" in captured.out
    assert "--repair-schema" in captured.out


def test_repair_schema_without_write_notion_previews_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    existing_properties = {
        "Name": {"type": "title", "title": {}},
        "Citation Key": {"type": "rich_text", "rich_text": {}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response()
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing_properties})
        raise AssertionError(f"preview must not write: {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--repair-schema"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would additively repair" in captured.out


def test_repair_schema_with_write_notion_patches_only_missing_and_preserves_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    existing_properties = {
        "Name": {"type": "title", "title": {}},
        "Citation Key": {"type": "rich_text", "rich_text": {}},
        "Review Status": {
            "type": "select",
            "select": {"options": [{"name": "New"}]},  # missing "Approved"/"Rejected"
        },
    }
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response()
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing_properties})
        if request.method == "PATCH" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            nonlocal sent_body
            sent_body = json.loads(request.content)
            return httpx.Response(200, json={"id": DATA_SOURCE_ID})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion", "--repair-schema"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Repaired" in captured.out

    sent_properties = sent_body["properties"]
    assert "Name" not in sent_properties
    assert "Citation Key" not in sent_properties
    review_options = {opt["name"] for opt in sent_properties["Review Status"]["select"]["options"]}
    assert review_options == {"New", "Approved", "Rejected"}


def test_type_conflict_refuses_and_cannot_be_bypassed_by_repair_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    existing_properties = {
        "Name": {"type": "title", "title": {}},
        "Baseline": {"type": "rich_text", "rich_text": {}},  # wrong type on purpose
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response()
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing_properties})
        raise AssertionError(
            f"type conflict must never trigger a write: {request.method} {request.url}"
        )

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion", "--repair-schema"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "REFUSING" in captured.out
    assert "Baseline" in captured.out
    assert "manual" in captured.err.lower()


def test_database_exists_but_no_matching_data_source_is_reported_and_refuses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_block_response()
        if request.method == "GET" and request.url.path == f"/v1/databases/{DATABASE_ID}":
            return _database_response(data_source_name="Some Other Name")
        raise AssertionError(f"must not guess/create a data source: {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no data source named" in captured.err
    assert "Citation Events" in captured.err
