from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.run_roster import main

from arise_radar.sinks.notion import NotionClient
from arise_radar.sources.openalex import OpenAlexClient


def _write_single_researcher_roster(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A123
    active: true
"""
    )
    return roster_path


def _single_publication_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "A Great Paper",
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.1/xyz",
                }
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_default_cli_mode_makes_no_notion_requests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)

    roster_path = _write_single_researcher_roster(tmp_path)
    client = mock_openalex_client(_single_publication_handler)

    exit_code = main(["--roster", str(roster_path)], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Notion" not in captured.out
    assert "Notion" not in captured.err


def test_write_notion_and_dry_run_together_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = _write_single_researcher_roster(tmp_path)

    exit_code = main(["--roster", str(roster_path), "--write-notion", "--notion-dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot be used together" in captured.err


def test_write_notion_missing_credentials_returns_clear_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env,
    # which would otherwise silently repopulate these vars and defeat the test.
    monkeypatch.chdir(tmp_path)
    roster_path = _write_single_researcher_roster(tmp_path)

    exit_code = main(["--roster", str(roster_path), "--write-notion"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_DATA_SOURCE_ID" in captured.err


def test_notion_dry_run_makes_no_write_requests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_single_publication_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--notion-dry-run"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would create" in captured.out


def test_write_notion_creates_page_via_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_single_publication_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out


def test_one_notion_failure_does_not_stop_the_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A123
    active: true
"""
    )

    def openalex_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Paper One",
                        "publication_date": "2026-01-01",
                        "doi": "https://doi.org/10.1/one",
                    },
                    {
                        "id": "https://openalex.org/W2",
                        "display_name": "Paper Two",
                        "publication_date": "2026-01-02",
                        "doi": "https://doi.org/10.1/two",
                    },
                ],
                "meta": {"next_cursor": None},
            },
        )

    openalex_client = mock_openalex_client(openalex_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            body = request.content.decode()
            if "10.1/one" in body:
                return httpx.Response(500, json={"message": "internal error"})
            return httpx.Response(200, json={"id": "page-two"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler, max_retries=0)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out
    assert "Errors: 1" in captured.out
