import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.setup_notion import DATABASE_TITLE, main

from arise_radar.sinks.notion import NotionClient

PARENT_PAGE_ID = "page-123"


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


def _existing_database_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "type": "child_database",
                    "id": "existing-db",
                    "child_database": {"title": DATABASE_TITLE},
                }
            ],
            "has_more": False,
            "next_cursor": None,
        },
    )


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-token-value")
    monkeypatch.setenv("NOTION_PARENT_PAGE_ID", PARENT_PAGE_ID)


def test_missing_credentials_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_PARENT_PAGE_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env,
    # which would otherwise silently repopulate these vars and defeat the test.
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_PARENT_PAGE_ID" in captured.err


def test_parent_page_not_accessible_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not found", "code": "object_not_found"})

    client = mock_notion_client(handler)

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not accessible" in captured.err
    assert "secret-token-value" not in captured.out
    assert "secret-token-value" not in captured.err


def test_refuses_to_create_duplicate_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _existing_database_response()
        raise AssertionError("must not create anything when a duplicate database exists")

    client = mock_notion_client(handler)

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "already exists" in captured.err
    assert "existing-db" in captured.err


def test_dry_run_makes_no_write_requests(
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
        raise AssertionError(f"dry run must not write: {request.method} {request.url}")

    client = mock_notion_client(handler)

    exit_code = main(["--dry-run"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would create database" in captured.out
    assert "Would create its data source" in captured.out
    assert "Canonical Key" in captured.out
    assert "Would create one test row" in captured.out
    assert "no create, update, or delete requests were made" in captured.out


def test_dry_run_with_skip_test_row_omits_test_row_line(
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
        raise AssertionError("dry run must not write")

    client = mock_notion_client(handler)

    exit_code = main(["--dry-run", "--skip-test-row"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "test row" not in captured.out.lower()


def _happy_path_handler(create_body_sink: dict) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/pages/{PARENT_PAGE_ID}":
            return _page_response()
        if request.method == "GET" and request.url.path.endswith("/children"):
            return _no_existing_database_response()
        if request.method == "POST" and request.url.path == "/v1/databases":
            create_body_sink["database"] = json.loads(request.content)
            return httpx.Response(
                200, json={"id": "new-db-id", "url": "https://notion.so/new-db-id"}
            )
        if request.method == "POST" and request.url.path == "/v1/data_sources":
            create_body_sink["data_source"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "new-ds-id"})
        if request.method == "POST" and request.url.path == "/v1/pages":
            create_body_sink["test_row"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "new-row-id"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return handler


def test_real_run_creates_database_data_source_and_test_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    sink: dict = {}
    client = mock_notion_client(_happy_path_handler(sink))

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Database ID:    new-db-id" in captured.out
    assert "Data source ID: new-ds-id" in captured.out
    assert "Database URL:   https://notion.so/new-db-id" in captured.out
    assert "NOTION_DATA_SOURCE_ID=new-ds-id" in captured.out
    assert "Test row created: new-row-id" in captured.out
    assert "secret-token-value" not in captured.out

    assert sink["database"]["parent"] == {"type": "page_id", "page_id": PARENT_PAGE_ID}
    assert "properties" not in sink["database"]
    ds_properties = sink["data_source"]["properties"]
    assert set(ds_properties) == {
        "Name",
        "Canonical Key",
        "Status",
        "Researchers",
        "Published Date",
        "Detected Date",
        "DOI",
        "OpenAlex ID",
        "Source",
        "URL",
        "Relevance Status",
        "System Notes",
        "Editorial Notes",
    }
    status_options = {opt["name"] for opt in ds_properties["Status"]["select"]["options"]}
    assert status_options == {"New", "Approved", "Rejected", "Published", "Error"}
    source_options = {opt["name"] for opt in ds_properties["Source"]["select"]["options"]}
    assert source_options == {"OpenAlex", "PubMed", "medRxiv", "arXiv", "Media", "Manual"}
    relevance_options = {
        opt["name"] for opt in ds_properties["Relevance Status"]["select"]["options"]
    }
    assert relevance_options == {"Kept", "Uncertain", "Excluded"}

    test_row_properties = sink["test_row"]["properties"]
    assert "Editorial Notes" not in test_row_properties
    assert test_row_properties["Researchers"] == {"multi_select": [{"name": "Ethan Goh"}]}


def test_real_run_with_skip_test_row_does_not_create_row(
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
        if request.method == "POST" and request.url.path == "/v1/databases":
            return httpx.Response(200, json={"id": "new-db-id", "url": "https://notion.so/x"})
        if request.method == "POST" and request.url.path == "/v1/data_sources":
            return httpx.Response(200, json={"id": "new-ds-id"})
        if request.method == "POST" and request.url.path == "/v1/pages":
            raise AssertionError("must not create a test row when --skip-test-row is passed")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)

    exit_code = main(["--skip-test-row"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Test row created" not in captured.out
