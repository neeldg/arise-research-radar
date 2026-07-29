import inspect
import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest
from scripts.run_citations import (
    _build_error_report_entries,
    _compute_citation_run_summary,
    main,
)

from arise_radar.citations import CitationDiscoveryResult, CitationEvent
from arise_radar.sinks.notion import NotionClient
from arise_radar.sinks.notion_citations import CitationRowIndex, CitationUpsertResult
from arise_radar.sources.openalex import OpenAlexClient

PUBLICATIONS_DS = "pubs-ds-123"
CITATIONS_DS = "citations-ds-456"
CITATIONS_QUERY_PATH = f"/v1/data_sources/{CITATIONS_DS}/query"
PAGES_PATH = "/v1/pages"


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


def _tracked_work_entry(
    openalex_id: str = "W100", canonical_key: str = "doi:10.1/tracked", title: str = "Tracked"
) -> dict:
    return {
        "openalex_id": openalex_id,
        "canonical_key": canonical_key,
        "title": title,
        "researchers": ["Ethan Goh"],
    }


def _large_fixture(n: int, *, tracked_openalex_id: str = "W100") -> dict:
    """A fixture with one tracked paper and `n` distinct citing works, each
    producing one unique CitationEvent — a fast, offline stand-in for the
    diagnosed 4,491-unique-citation-edge scenario, used to prove the request
    count no longer scales with the number of events."""
    return {
        "tracked_works": [_tracked_work_entry(openalex_id=tracked_openalex_id)],
        "citing_works": [
            _citing_work(
                openalex_id=f"W{2000 + i}",
                referenced_works=[f"https://openalex.org/{tracked_openalex_id}"],
            )
            for i in range(n)
        ],
    }


class _FakeNotionBackend:
    """A minimal stateful fake of the two Notion data sources this CLI
    touches, shared across possibly multiple `main()` calls in one test --
    used for rerun-idempotency and human-edit-preservation tests, where the
    second run must see what the first run actually wrote.

    The citations data source is served as a single bulk list (matching
    NotionClient.iter_data_source_pages, which load_citation_row_index reads
    from) rather than a per-Citation-Key filtered query -- the pipeline no
    longer sends per-key queries at all.
    """

    def __init__(self, publications: list[dict]) -> None:
        self.publications = publications
        self.citations: dict[str, dict] = {}  # citation_key -> page dict
        self._next_id = 1
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        publications_query_path = f"/v1/data_sources/{PUBLICATIONS_DS}/query"

        if request.method == "POST" and request.url.path == publications_query_path:
            return httpx.Response(
                200, json={"results": self.publications, "has_more": False, "next_cursor": None}
            )
        if request.method == "POST" and request.url.path == CITATIONS_QUERY_PATH:
            return httpx.Response(
                200,
                json={
                    "results": list(self.citations.values()),
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.method == "POST" and request.url.path == PAGES_PATH:
            body = json.loads(request.content)
            citation_key = body["properties"]["Citation Key"]["rich_text"][0]["text"]["content"]
            page_id = f"citation-page-{self._next_id}"
            self._next_id += 1
            properties = dict(body["properties"])
            # Stored read-shape, independent of the write-shape payload above
            # -- load_citation_row_index reads plain_text, not text.content.
            properties["Citation Key"] = {
                "type": "rich_text",
                "rich_text": [{"plain_text": citation_key}],
            }
            page = {"id": page_id, "properties": properties}
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

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p in self.requests if m == method and p == path)

    def total(self, method: str) -> int:
        return sum(1 for m, _ in self.requests if m == method)


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


def test_restart_after_partial_completion_is_idempotent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """Simulates an interrupted run: the first invocation only sees citing
    work W900 (as if the process had been killed after that edge was
    written), the second sees W900 and W901 (a rerun that rediscovers
    everything). W900's row must be found via the initial bulk load on the
    second run and updated in place, never recreated."""
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([])  # fixture mode never queries publications

    def fixture_with(citing_ids: list[str]) -> dict:
        return {
            "tracked_works": [_tracked_work_entry()],
            "citing_works": [
                _citing_work(openalex_id=cid, referenced_works=["https://openalex.org/W100"])
                for cid in citing_ids
            ],
        }

    partial_path = tmp_path / "partial.json"
    partial_path.write_text(json.dumps(fixture_with(["W900"])))
    notion_client_1 = mock_notion_client(backend.handler)
    exit_code_1 = main(
        ["--write-notion", "--fixture-file", str(partial_path)], notion_client=notion_client_1
    )
    assert exit_code_1 == 0
    assert len(backend.citations) == 1

    full_path = tmp_path / "full.json"
    full_path.write_text(json.dumps(fixture_with(["W900", "W901"])))
    notion_client_2 = mock_notion_client(backend.handler)
    exit_code_2 = main(
        ["--write-notion", "--fixture-file", str(full_path)], notion_client=notion_client_2
    )

    assert exit_code_2 == 0
    assert len(backend.citations) == 2  # W900's row updated in place, W901's row created


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
    # Errors are broken out by category, never combined into one opaque count.
    assert "OpenAlex batch errors:                 1" in captured.out
    assert "Notion create errors:                  0" in captured.out
    assert "Notion update errors:                  0" in captured.out


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
                "tracked_works": [_tracked_work_entry()],
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


# --- staged, flushed logging ---------------------------------------------------------


def test_stage_messages_print_before_each_phase_in_order(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    backend = _FakeNotionBackend([_publication_page(openalex_id="W100")])
    notion_client = mock_notion_client(backend.handler)
    openalex_client = mock_openalex_client(_openalex_handler_for([_citing_work()]))

    exit_code = main([], notion_client=notion_client, openalex_client=openalex_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    stages = [
        "Stage: reading tracked publications...",
        "Stage: retrieving OpenAlex citations...",
        "Stage: loading existing citation rows...",
        "Stage: processing 1 citation edge(s)...",
    ]
    for stage in stages:
        assert stage in captured.out
    # Each stage message is printed before the next stage begins.
    indices = [captured.out.index(stage) for stage in stages]
    assert indices == sorted(indices)


def test_progress_prints_at_interval_and_flush_is_used_in_source(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_large_fixture(250)))
    backend = _FakeNotionBackend([])
    notion_client = mock_notion_client(backend.handler)

    exit_code = main(["--fixture-file", str(fixture_path)], notion_client=notion_client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Processed 100/250 citation edge(s)..." in captured.out
    assert "Processed 200/250 citation edge(s)..." in captured.out
    assert "Processed 250/250 citation edge(s)..." in captured.out

    import scripts.run_citations as run_citations_module

    source = inspect.getsource(run_citations_module)
    assert "flush=True" in source


# --- request-count guarantees (see the citation-pipeline diagnosis) ----------------


def test_large_event_count_does_not_scale_citation_query_requests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_large_fixture(500)))
    backend = _FakeNotionBackend([])
    notion_client = mock_notion_client(backend.handler)

    exit_code = main(["--fixture-file", str(fixture_path)], notion_client=notion_client)

    assert exit_code == 0
    # One bulk read, never one query per citation key -- the original
    # bottleneck would have produced 500 requests to this same path.
    assert backend.count("POST", CITATIONS_QUERY_PATH) == 1


def test_dry_run_performs_zero_notion_writes_and_zero_per_event_reads(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_large_fixture(500)))
    backend = _FakeNotionBackend([])
    notion_client = mock_notion_client(backend.handler)

    exit_code = main(["--fixture-file", str(fixture_path)], notion_client=notion_client)  # dry run

    assert exit_code == 0
    assert backend.count("POST", CITATIONS_QUERY_PATH) == 1
    assert backend.count("POST", PAGES_PATH) == 0
    assert backend.total("PATCH") == 0


def test_live_create_run_has_no_pre_create_lookup_per_event(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    n = 500
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_large_fixture(n)))
    backend = _FakeNotionBackend([])
    notion_client = mock_notion_client(backend.handler)

    exit_code = main(
        ["--write-notion", "--fixture-file", str(fixture_path)], notion_client=notion_client
    )

    assert exit_code == 0
    assert backend.count("POST", CITATIONS_QUERY_PATH) == 1  # still just the one bulk read
    assert backend.count("POST", PAGES_PATH) == n  # one create per new event, no lookups
    assert backend.total("PATCH") == 0


def test_live_update_run_uses_known_page_ids_no_pre_update_lookup(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    n = 500
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(_large_fixture(n)))
    backend = _FakeNotionBackend([])

    notion_client_1 = mock_notion_client(backend.handler)
    main(["--write-notion", "--fixture-file", str(fixture_path)], notion_client=notion_client_1)
    assert backend.count("POST", PAGES_PATH) == n

    backend.requests.clear()  # only care about the SECOND run's requests now
    notion_client_2 = mock_notion_client(backend.handler)
    exit_code = main(
        ["--write-notion", "--fixture-file", str(fixture_path)], notion_client=notion_client_2
    )

    assert exit_code == 0
    assert backend.count("POST", CITATIONS_QUERY_PATH) == 1
    assert backend.count("POST", PAGES_PATH) == 0  # no re-creates
    assert backend.total("PATCH") == n  # every existing row updated directly by known page id


# --- separate, categorized error reporting + --error-report JSON ------------------


def test_error_categories_are_reported_separately_and_in_error_report_json(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "tracked_works": [
                    _tracked_work_entry(
                        openalex_id="W100", canonical_key="doi:10.1/one", title="One"
                    ),
                    _tracked_work_entry(
                        openalex_id="W200", canonical_key="doi:10.1/two", title="Two"
                    ),
                ],
                "citing_works": [
                    _citing_work(
                        openalex_id="W900", referenced_works=["https://openalex.org/W100"]
                    ),
                    _citing_work(
                        openalex_id="W901", referenced_works=["https://openalex.org/W200"]
                    ),
                ],
            }
        )
    )

    duplicate_pages = [
        {
            "id": "dup-a",
            "properties": {
                "Citation Key": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": "citation:DUPTEST:DUPKEY"}],
                }
            },
        },
        {
            "id": "dup-b",
            "properties": {
                "Citation Key": {
                    "type": "rich_text",
                    "rich_text": [{"plain_text": "citation:DUPTEST:DUPKEY"}],
                }
            },
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == CITATIONS_QUERY_PATH:
            return httpx.Response(
                200,
                json={"results": duplicate_pages, "has_more": False, "next_cursor": None},
            )
        if request.method == "POST" and request.url.path == PAGES_PATH:
            body = json.loads(request.content)
            key = body["properties"]["Citation Key"]["rich_text"][0]["text"]["content"]
            if key == "citation:W901:W200":
                return httpx.Response(500, json={"message": "boom"})
            return httpx.Response(200, json={"id": "citation-page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(handler, max_retries=0)
    report_path = tmp_path / "errors.json"

    exit_code = main(
        [
            "--write-notion",
            "--fixture-file",
            str(fixture_path),
            "--error-report",
            str(report_path),
        ],
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "OpenAlex batch errors:                 0" in captured.out
    assert "Notion create errors:                  1" in captured.out
    assert "Notion update errors:                  0" in captured.out
    assert "Duplicate stored Citation Keys:        1" in captured.out

    entries = json.loads(report_path.read_text())
    stages = {entry["stage"] for entry in entries}
    assert "notion_create" in stages
    assert "duplicate_key" in stages

    create_entry = next(e for e in entries if e["stage"] == "notion_create")
    assert create_entry["citation_key"] == "citation:W901:W200"
    assert create_entry["citing_openalex_id"] == "W901"
    assert create_entry["cited_openalex_id"] == "W200"
    assert create_entry["status_code"] == 500

    dup_entry = next(e for e in entries if e["stage"] == "duplicate_key")
    assert dup_entry["citation_key"] == "citation:DUPTEST:DUPKEY"


# --- pure functions: category separation without any I/O --------------------------


def _minimal_event(citation_key: str, citing_id: str, cited_id: str) -> CitationEvent:
    return CitationEvent(
        citation_key=citation_key,
        citing_title="Citing",
        citing_openalex_id=citing_id,
        cited_openalex_id=cited_id,
        cited_canonical_key="doi:10.1/x",
        cited_title="Cited",
        detected_date=date(2026, 1, 1),
        is_baseline=False,
        slack_status="Pending",
    )


def test_build_error_report_entries_keeps_every_category_distinct() -> None:
    event_ok = _minimal_event("citation:A:B", "A", "B")
    event_bad = _minimal_event("citation:C:D", "C", "D")
    discovery = CitationDiscoveryResult(
        events=[event_ok, event_bad],
        raw_citing_work_matches=2,
        batches_queried=1,
        batch_errors=["batch 1 failed: timeout"],
    )
    results = [
        CitationUpsertResult(citation_key="citation:A:B", action="created", page_id="p1"),
        CitationUpsertResult(
            citation_key="citation:C:D",
            action="error",
            stage="update",
            status_code=429,
            detail="update failed: rate limited",
        ),
    ]
    index = CitationRowIndex(
        duplicate_keys={"citation:E:F": ["page-x", "page-y"]}, malformed_rows=3
    )

    entries = _build_error_report_entries(
        discovery=discovery, results=results, index=index, notion_read_error=None
    )

    stages = [entry.stage for entry in entries]
    assert stages.count("openalex_batch") == 1
    assert stages.count("notion_update") == 1
    assert stages.count("duplicate_key") == 1
    assert stages.count("malformed_row") == 1
    assert len(entries) == 4  # nothing combined, nothing dropped

    update_entry = next(e for e in entries if e.stage == "notion_update")
    assert update_entry.citation_key == "citation:C:D"
    assert update_entry.citing_openalex_id == "C"
    assert update_entry.cited_openalex_id == "D"
    assert update_entry.status_code == 429


def test_compute_citation_run_summary_reports_categories_independently() -> None:
    discovery = CitationDiscoveryResult(
        events=[
            _minimal_event("citation:A:B", "A", "B"),
            _minimal_event("citation:C:D", "C", "D"),
        ],
        raw_citing_work_matches=2,
        batches_queried=1,
        batch_errors=["one batch failed"],
    )
    results = [
        CitationUpsertResult(citation_key="citation:A:B", action="created", page_id="p1"),
        CitationUpsertResult(
            citation_key="citation:C:D", action="error", stage="create", status_code=500
        ),
    ]
    index = CitationRowIndex(duplicate_keys={"citation:E:F": ["page-x", "page-y"]})

    summary = _compute_citation_run_summary(
        tracked_count=1, skipped_count=0, discovery=discovery, results=results, index=index
    )

    assert summary.openalex_batch_errors == 1
    assert summary.notion_create_errors == 1
    assert summary.notion_update_errors == 0
    assert summary.duplicate_stored_citation_keys == 1
    assert summary.notion_read_errors == 0


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
