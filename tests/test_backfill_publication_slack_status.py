import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.backfill_publication_slack_status import main, plan_backfill

from arise_radar.sinks.notion import NotionClient

DATA_SOURCE_ID = "ds-123"
QUERY_PATH = f"/v1/data_sources/{DATA_SOURCE_ID}/query"


def _publication_page(
    *, page_id: str = "page-1", title: str = "A Paper", slack_status: str | None = None
) -> dict:
    props: dict = {"Name": {"type": "title", "title": [{"plain_text": title}]}}
    if slack_status is not None:
        props["Slack Status"] = {"type": "select", "select": {"name": slack_status}}
    else:
        props["Slack Status"] = {"type": "select", "select": None}
    return {"id": page_id, "properties": props}


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-token-value")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)


# --- plan_backfill: pure logic, no I/O ----------------------------------------------


def test_plan_backfill_selects_only_empty_slack_status_rows() -> None:
    pages = [
        _publication_page(page_id="a", slack_status=None),
        _publication_page(page_id="b", slack_status="Pending"),
        _publication_page(page_id="c", slack_status="Suppressed"),
        _publication_page(page_id="d", slack_status="Sent"),
        _publication_page(page_id="e", slack_status="Failed"),
    ]
    plan = plan_backfill(pages)

    assert plan.rows_scanned == 5
    assert plan.already_set == 4
    assert [row.page_id for row in plan.to_backfill] == ["a"]


def test_plan_backfill_empty_input() -> None:
    plan = plan_backfill([])
    assert plan.rows_scanned == 0
    assert plan.to_backfill == []


def test_plan_backfill_counts_malformed_rows_without_guessing() -> None:
    page = _publication_page(slack_status=None)
    del page["id"]
    plan = plan_backfill([page])
    assert plan.malformed_rows == 1
    assert plan.to_backfill == []


# --- CLI: dry run (default) -----------------------------------------------------


def test_dry_run_makes_no_write_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    pages = [
        _publication_page(page_id="a", title="Needs Backfill", slack_status=None),
        _publication_page(page_id="b", title="Already Pending", slack_status="Pending"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        raise AssertionError(f"dry run must not write: {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would backfill to Suppressed: 1" in captured.out
    assert "Needs Backfill" in captured.out
    assert "Already Pending" not in captured.out
    assert "Dry run: no write requests were made." in captured.out
    assert "secret-token-value" not in captured.out


def test_dry_run_nothing_to_backfill(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    pages = [_publication_page(slack_status="Suppressed")]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        raise AssertionError("must not write")

    client = mock_notion_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Nothing to backfill." in captured.out


# --- CLI: live write --------------------------------------------------------------


def test_live_backfill_changes_only_empty_slack_status_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    pages = [
        _publication_page(page_id="a", slack_status=None),
        _publication_page(page_id="b", slack_status="Pending"),
        _publication_page(page_id="c", slack_status="Sent"),
    ]
    patched_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        if request.method == "PATCH" and request.url.path.startswith("/v1/pages/"):
            page_id = request.url.path.rsplit("/", 1)[-1]
            patched_pages.append(page_id)
            body = json.loads(request.content)
            assert body["properties"] == {"Slack Status": {"select": {"name": "Suppressed"}}}
            return httpx.Response(200, json={"id": page_id})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert patched_pages == ["a"]  # only the empty-status row was ever touched
    assert "Backfilled: 1" in captured.out


def test_live_backfill_never_overwrites_existing_statuses(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    pages = [
        _publication_page(page_id="a", slack_status="Pending"),
        _publication_page(page_id="b", slack_status="Suppressed"),
        _publication_page(page_id="c", slack_status="Sent"),
        _publication_page(page_id="d", slack_status="Failed"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        raise AssertionError("must never PATCH a row that already has a Slack Status")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)

    assert exit_code == 0


def test_rerun_after_backfill_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """A second run against rows the first run just backfilled must do
    nothing -- proves rerunning this script is safe."""
    _set_env(monkeypatch)
    pages = [_publication_page(page_id="a", slack_status="Suppressed")]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        raise AssertionError("already-backfilled rows must trigger no write")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Nothing to backfill." in captured.out


# --- bulk read, not one query per row ---------------------------------------------


def test_bulk_read_not_one_query_per_row(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    pages = [_publication_page(page_id=f"p{i}", slack_status=None) for i in range(50)]
    query_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == QUERY_PATH:
            query_calls["count"] += 1
            return httpx.Response(
                200, json={"results": pages, "has_more": False, "next_cursor": None}
            )
        if request.method == "PATCH" and request.url.path.startswith("/v1/pages/"):
            page_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": page_id})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    exit_code = main(["--write-notion"], client=client)

    assert exit_code == 0
    assert query_calls["count"] == 1  # one bulk read, not 50 per-row lookups


# --- credentials ---------------------------------------------------------------


def test_missing_credentials_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_DATA_SOURCE_ID" in captured.err
