import json
from collections.abc import Callable

import httpx
import pytest
from scripts.run_citations import main

from arise_radar.sinks.notion import NotionClient
from arise_radar.sources.openalex import OpenAlexClient

PUBLICATIONS_DS = "pubs-ds-123"
CITATIONS_DS = "citations-ds-456"


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", PUBLICATIONS_DS)
    monkeypatch.setenv("NOTION_CITATIONS_DATA_SOURCE_ID", CITATIONS_DS)


def _publication_page(
    *,
    openalex_id: str | None,
    title: str = "Tracked Paper",
    canonical_key: str = "doi:10.1/tracked",
    researchers: list[str] | None = None,
) -> dict:
    props: dict = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "Canonical Key": {"type": "rich_text", "rich_text": [{"plain_text": canonical_key}]},
        "Researchers": {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in (researchers or ["Ethan Goh"])],
        },
    }
    if openalex_id is not None:
        props["OpenAlex ID"] = {"type": "rich_text", "rich_text": [{"plain_text": openalex_id}]}
    return {"id": f"pub-{title}", "properties": props}


def _citing_work(
    *,
    openalex_id: str = "W900",
    referenced_works: list[str] | None = None,
    display_name: str = "A Citing Paper",
) -> dict:
    return {
        "id": f"https://openalex.org/{openalex_id}",
        "display_name": display_name,
        "doi": None,
        "publication_date": "2026-01-15",
        "referenced_works": referenced_works or ["https://openalex.org/W100"],
        "authorships": [{"author": {"display_name": "Someone Else"}}],
    }


class _FakeNotionBackend:
    """A minimal stateful fake of the two Notion data sources this CLI
    touches, shared across possibly multiple `main()` calls in one test --
    used for rerun-idempotency and human-edit-preservation tests, where the
    second run must see what the first run actually wrote."""

    def __init__(self, publications: list[dict]) -> None:
        self.publications = publications
        self.citations: dict[str, dict] = {}  # citation_key -> page dict
        self._next_id = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        publications_query_path = f"/v1/data_sources/{PUBLICATIONS_DS}/query"
        citations_query_path = f"/v1/data_sources/{CITATIONS_DS}/query"

        if request.method == "POST" and request.url.path == publications_query_path:
            return httpx.Response(
                200, json={"results": self.publications, "has_more": False, "next_cursor": None}
            )
        if request.method == "POST" and request.url.path == citations_query_path:
            body = json.loads(request.content)
            key = body["filter"]["rich_text"]["equals"]
            existing = self.citations.get(key)
            return httpx.Response(200, json={"results": [existing] if existing else []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            body = json.loads(request.content)
            citation_key = body["properties"]["Citation Key"]["rich_text"][0]["text"]["content"]
            page_id = f"citation-page-{self._next_id}"
            self._next_id += 1
            page = {"id": page_id, "properties": dict(body["properties"])}
            self.citations[citation_key] = page
            return httpx.Response(200, json={"id": page_id})
        if request.method == "PATCH" and request.url.path.startswith("/v1/pages/"):
            page_id = request.url.path.rsplit("/", 1)[-1]
            body = json.loads(request.content)
            for page in self.citations.values():
                if page["id"] == page_id:
                    page["properties"].update(body["properties"])
                    return httpx.Response(200, json={"id": page_id})
            raise AssertionError(f"PATCH to unknown page id {page_id}")
        raise AssertionError(f"unexpected Notion request {request.method} {request.url}")


def _openalex_handler_for(citing_works: list[dict]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/works"
        return httpx.Response(200, json={"results": citing_works, "meta": {"next_cursor": None}})

    return handler


# --- flag validation / safe default -------------------------------------------------


def test_write_notion_and_notion_dry_run_together_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch)
    exit_code = main(["--write-notion", "--notion-dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot be used together" in captured.err


def test_default_is_dry_run_makes_no_write_requests(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])

    def guarded_handler(request: httpx.Request) -> httpx.Response:
        if request.method in ("POST",) and request.url.path == "/v1/pages":
            raise AssertionError("default mode must not create pages")
        if request.method == "PATCH":
            raise AssertionError("default mode must not update pages")
        return backend.handler(request)

    notion_client = mock_notion_client(guarded_handler)
    openalex_client = mock_openalex_client(_openalex_handler_for([_citing_work()]))

    exit_code = main([], notion_client=notion_client, openalex_client=openalex_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would create" in captured.out
    assert backend.citations == {}


# --- missing OpenAlex IDs skipped clearly -------------------------------------------


def test_missing_openalex_ids_are_skipped_clearly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend(
        [
            _publication_page(openalex_id="W100", title="Has ID"),
            _publication_page(openalex_id=None, title="No ID One"),
            _publication_page(openalex_id=None, title="No ID Two"),
        ]
    )
    notion_client = mock_notion_client(backend.handler)
    openalex_client = mock_openalex_client(_openalex_handler_for([]))

    exit_code = main([], notion_client=notion_client, openalex_client=openalex_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Total publication rows inspected: 3" in captured.out
    assert "Tracked ARISE papers with OpenAlex IDs: 1" in captured.out
    assert "Skipped (no OpenAlex ID): 2" in captured.out


def test_all_missing_openalex_ids_reports_and_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id=None)])
    notion_client = mock_notion_client(backend.handler)

    exit_code = main([], notion_client=notion_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "nothing to discover" in captured.err


# --- baseline vs incremental ---------------------------------------------------------


def test_baseline_run_produces_suppressed_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])
    notion_client = mock_notion_client(backend.handler)
    openalex_client = mock_openalex_client(_openalex_handler_for([_citing_work()]))

    exit_code = main(
        ["--baseline", "--write-notion"],
        notion_client=notion_client,
        openalex_client=openalex_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(backend.citations) == 1
    page = next(iter(backend.citations.values()))
    assert page["properties"]["Baseline"] == {"checkbox": True}
    assert page["properties"]["Slack Status"] == {"select": {"name": "Suppressed"}}
    assert "Baseline-suppressed rows:                1" in captured.out
    assert "New Slack-pending rows:                  0" in captured.out


def test_incremental_run_produces_pending_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])
    notion_client = mock_notion_client(backend.handler)
    openalex_client = mock_openalex_client(_openalex_handler_for([_citing_work()]))

    exit_code = main(
        ["--write-notion"], notion_client=notion_client, openalex_client=openalex_client
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    page = next(iter(backend.citations.values()))
    assert page["properties"]["Baseline"] == {"checkbox": False}
    assert page["properties"]["Slack Status"] == {"select": {"name": "Pending"}}
    assert "Baseline-suppressed rows:                0" in captured.out
    assert "New Slack-pending rows:                  1" in captured.out


# --- rerun idempotency: no duplicate rows -------------------------------------------


def test_rerun_creates_no_duplicate_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])
    citing = [_citing_work()]

    notion_client_1 = mock_notion_client(backend.handler)
    openalex_client_1 = mock_openalex_client(_openalex_handler_for(citing))
    exit_code_1 = main(
        ["--baseline", "--write-notion"],
        notion_client=notion_client_1,
        openalex_client=openalex_client_1,
    )
    captured_1 = capsys.readouterr()
    assert exit_code_1 == 0
    assert "Created: 1" in captured_1.out
    assert len(backend.citations) == 1

    # Rerun against the SAME backend (same citations already persisted).
    notion_client_2 = mock_notion_client(backend.handler)
    openalex_client_2 = mock_openalex_client(_openalex_handler_for(citing))
    exit_code_2 = main(
        ["--write-notion"], notion_client=notion_client_2, openalex_client=openalex_client_2
    )
    captured_2 = capsys.readouterr()

    assert exit_code_2 == 0
    assert "Updated: 1" in captured_2.out
    assert "Created:" not in captured_2.out
    assert len(backend.citations) == 1  # still exactly one row -- no duplicate


# --- existing human-edited fields preserved end-to-end ------------------------------


def test_existing_human_edited_fields_preserved_across_rerun(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])
    citing = [_citing_work()]

    # First run: baseline, creates the row.
    notion_client_1 = mock_notion_client(backend.handler)
    openalex_client_1 = mock_openalex_client(_openalex_handler_for(citing))
    main(
        ["--baseline", "--write-notion"],
        notion_client=notion_client_1,
        openalex_client=openalex_client_1,
    )

    # A human reviews it in Notion: approves it and marks the Slack message sent.
    page = next(iter(backend.citations.values()))
    page["properties"]["Review Status"] = {"select": {"name": "Approved"}}
    page["properties"]["Slack Status"] = {"select": {"name": "Sent"}}
    page["properties"]["Citation Relationship"] = {"select": {"name": "Extends"}}

    # Second run (incremental) rediscovers the same edge.
    notion_client_2 = mock_notion_client(backend.handler)
    openalex_client_2 = mock_openalex_client(_openalex_handler_for(citing))
    exit_code = main(
        ["--write-notion"], notion_client=notion_client_2, openalex_client=openalex_client_2
    )

    assert exit_code == 0
    page_after = next(iter(backend.citations.values()))
    assert page_after["properties"]["Review Status"] == {"select": {"name": "Approved"}}
    assert page_after["properties"]["Slack Status"] == {"select": {"name": "Sent"}}
    assert page_after["properties"]["Citation Relationship"] == {"select": {"name": "Extends"}}
    # Baseline=true from the first (baseline) run must also survive untouched.
    assert page_after["properties"]["Baseline"] == {"checkbox": True}


# --- one failed OpenAlex batch does not stop later batches -------------------------


def test_one_failed_batch_does_not_stop_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend(
        [
            _publication_page(openalex_id="W100", title="One", canonical_key="doi:10.1/one"),
            _publication_page(openalex_id="W200", title="Two", canonical_key="doi:10.1/two"),
        ]
    )

    def openalex_handler(request: httpx.Request) -> httpx.Response:
        filter_value = request.url.params["filter"]
        if "W100" in filter_value:
            return httpx.Response(500, json={"message": "internal error"})
        return httpx.Response(
            200,
            json={
                "results": [_citing_work(referenced_works=["https://openalex.org/W200"])],
                "meta": {"next_cursor": None},
            },
        )

    notion_client = mock_notion_client(backend.handler)
    openalex_client = mock_openalex_client(openalex_handler, max_retries=0)

    exit_code = main(
        ["--write-notion", "--batch-size", "1"],
        notion_client=notion_client,
        openalex_client=openalex_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "ERROR: OpenAlex batch failed" in captured.err
    assert len(backend.citations) == 1  # the surviving batch's edge still got written
    assert "Errors:                                  1" in captured.out


# --- --fixture-file: offline discovery, still writes via Notion client -------------


def test_fixture_file_offline_discovery(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "tracked_works": [
                    {
                        "openalex_id": "W100",
                        "canonical_key": "doi:10.1/tracked",
                        "title": "Tracked Paper",
                        "researchers": ["Ethan Goh"],
                    }
                ],
                "citing_works": [_citing_work()],
            }
        )
    )
    backend = _FakeNotionBackend([])  # publications DS never queried in fixture mode
    notion_client = mock_notion_client(backend.handler)

    exit_code = main(
        ["--baseline", "--write-notion", "--fixture-file", str(fixture_path)],
        notion_client=notion_client,
    )

    assert exit_code == 0
    assert len(backend.citations) == 1


# --- no Slack calls occur in this phase ---------------------------------------------


def test_no_slack_api_calls_exist_in_citation_monitoring_modules() -> None:
    """Phase 1 never sends Slack messages. "Slack Status"/"Slack Timestamp"
    are legitimate Notion property names reserved for a later phase (see
    sinks/notion_citations.py) -- what must not exist is any actual Slack
    API call: an HTTP client pointed at slack.com, the slack_sdk package, or
    a chat.postMessage-style call.
    """
    import inspect

    import scripts.run_citations as run_citations_module

    from arise_radar import citations as citations_module
    from arise_radar.sinks import notion_citations as notion_citations_module

    forbidden = ("slack.com", "slack_sdk", "WebClient", "chat.postMessage", "chat_postMessage")
    for module in (run_citations_module, citations_module, notion_citations_module):
        source = inspect.getsource(module)
        for pattern in forbidden:
            assert pattern not in source, f"{module.__name__} references {pattern!r}"
