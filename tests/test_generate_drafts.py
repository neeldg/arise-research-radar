import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import scripts.generate_drafts as generate_drafts_module
from scripts.generate_drafts import main

from arise_radar.drafting import AnthropicClient, DraftContent
from arise_radar.sinks.notion import GENERATED_SECTION_MARKER, NotionClient
from arise_radar.sources.openalex import OpenAlexClient

DATA_SOURCE_ID = "ds-123"


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-notion-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-anthropic-key")


def _notion_page(
    page_id: str,
    *,
    title: str = "A Great Paper",
    canonical_key: str = "doi:10.1/xyz",
    openalex_id: str = "W1",
    draft_status: str | None = None,
    researchers: list[str] | None = None,
) -> dict:
    props: dict = {
        "Name": {"type": "title", "title": [{"plain_text": title}]},
        "Canonical Key": {"type": "rich_text", "rich_text": [{"plain_text": canonical_key}]},
        "OpenAlex ID": {"type": "rich_text", "rich_text": [{"plain_text": openalex_id}]},
        "Researchers": {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in (researchers or ["Ethan Goh"])],
        },
    }
    if draft_status is not None:
        props["Draft Status"] = {"type": "select", "select": {"name": draft_status}}
    else:
        props["Draft Status"] = {"type": "select", "select": None}
    return {"id": page_id, "properties": props}


def _openalex_work_response(has_abstract: bool = True) -> httpx.Response:
    body = {
        "id": "https://openalex.org/W1",
        "display_name": "A Great Paper",
        "publication_date": "2026-01-01",
        "doi": "https://doi.org/10.1/xyz",
        "primary_location": {"source": {"display_name": "Nature Medicine"}},
    }
    if has_abstract:
        body["abstract_inverted_index"] = {"This": [0], "is": [1], "great": [2]}
    return httpx.Response(200, json=body)


def _anthropic_success_response() -> httpx.Response:
    payload = {
        "internal_summary": "Summary.",
        "key_story_angle": "Angle.",
        "why_it_matters": "Matters.",
        "draft_social_post": "Great LinkedIn post.",
        "limitations": "None evident.",
    }
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _is_block_children_path(path: str) -> bool:
    return path.startswith("/v1/blocks/") and path.endswith("/children")


def _block_write_response(request: httpx.Request) -> httpx.Response | None:
    """Handles the GET (list existing)/PATCH (append) block-children calls that
    replace_generated_section makes for any page with no prior generated section,
    reusable by tests that don't care about the block content itself."""
    if request.method == "GET" and _is_block_children_path(request.url.path):
        return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
    if request.method == "PATCH" and _is_block_children_path(request.url.path):
        return httpx.Response(200, json={"results": []})
    return None


def _notion_query_handler(pages: list[dict]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": pages})
        raise AssertionError(f"unexpected Notion request {request.method} {request.url}")

    return handler


def _openalex_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "GET" and request.url.path == "/works/W1":
        return _openalex_work_response()
    raise AssertionError(f"unexpected OpenAlex request {request.method} {request.url}")


def _anthropic_handler(request: httpx.Request) -> httpx.Response:
    if request.method == "POST" and request.url.path == "/v1/messages":
        return _anthropic_success_response()
    raise AssertionError(f"unexpected Anthropic request {request.method} {request.url}")


# --- credentials / flag validation ----------------------------------------------


def test_missing_notion_credentials_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err


def test_missing_anthropic_key_returns_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-notion-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ANTHROPIC_API_KEY" in captured.err


def test_write_notion_and_dry_run_together_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_env(monkeypatch)

    exit_code = main(["--write-notion", "--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot be used together" in captured.err


# --- default / preview mode ------------------------------------------------------


def test_default_mode_generates_draft_but_writes_nothing_to_notion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1")

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        raise AssertionError("must not write to Notion without --write-notion")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(_openalex_handler)
    anthropic_client = mock_anthropic_client(_anthropic_handler)

    exit_code = main(
        [],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "preview" in captured.out
    assert "Great LinkedIn post." in captured.out
    assert "secret-notion-token" not in captured.out
    assert "secret-anthropic-key" not in captured.out


# --- --write-notion ---------------------------------------------------------------


def test_write_notion_persists_draft_properties(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1")
    updated_body: dict = {}
    appended_children: list[dict] = []

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            appended_children.extend(json.loads(request.content)["children"])
            return httpx.Response(200, json={"results": []})
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(_openalex_handler)
    anthropic_client = mock_anthropic_client(_anthropic_handler)

    exit_code = main(
        ["--write-notion"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "drafted" in captured.out

    # Full content was written as page-body blocks, marker first.
    assert appended_children[0]["type"] == "heading_3"
    assert appended_children[0]["heading_3"]["rich_text"][0]["text"]["content"] == (
        GENERATED_SECTION_MARKER
    )
    full_post_text = "".join(
        part["text"]["content"]
        for block in appended_children
        if block["type"] == "paragraph"
        for part in block["paragraph"]["rich_text"]
    )
    assert "Great LinkedIn post." in full_post_text

    props = updated_body["properties"]
    assert props["Draft Status"] == {"select": {"name": "Drafted"}}
    assert props["Draft Social Post"]["rich_text"][0]["text"]["content"] == "Great LinkedIn post."
    assert props["Draft Source Basis"] == {"select": {"name": "OpenAlex Abstract"}}
    assert "Status" not in props
    assert "Researchers" not in props
    assert "Editorial Notes" not in props


# --- eligibility: never overwrite Approved, --force behavior --------------------


def test_approved_row_is_skipped_even_with_force(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1", draft_status="Approved")

    notion_client = mock_notion_client(_notion_query_handler([page]))

    def fail_openalex(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch OpenAlex for an Approved row")

    def fail_anthropic(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call Anthropic for an Approved row")

    openalex_client = mock_openalex_client(fail_openalex)
    anthropic_client = mock_anthropic_client(fail_anthropic)

    exit_code = main(
        ["--write-notion", "--force"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "skipped" in captured.out
    assert "Approved" in captured.out


def test_already_drafted_row_skipped_without_force_but_processed_with_force(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1", draft_status="Drafted")

    # Without --force: skipped, no OpenAlex/Anthropic calls.
    notion_client = mock_notion_client(_notion_query_handler([page]))

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            "must not call external services for an already-drafted row without --force"
        )

    openalex_client = mock_openalex_client(fail)
    anthropic_client = mock_anthropic_client(fail)

    exit_code = main(
        [],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "skipped" in captured.out
    assert "--force" in captured.out

    # With --force: processed normally.
    notion_client2 = mock_notion_client(_notion_query_handler([page]))
    openalex_client2 = mock_openalex_client(_openalex_handler)
    anthropic_client2 = mock_anthropic_client(_anthropic_handler)

    exit_code2 = main(
        ["--force"],
        notion_client=notion_client2,
        openalex_client=openalex_client2,
        anthropic_client=anthropic_client2,
    )
    captured2 = capsys.readouterr()
    assert exit_code2 == 0
    assert "preview" in captured2.out


# --- --limit default and explicit ------------------------------------------------


def test_default_limit_is_three(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    captured_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(
        lambda r: (_ for _ in ()).throw(AssertionError("no calls expected"))
    )
    anthropic_client = mock_anthropic_client(
        lambda r: (_ for _ in ()).throw(AssertionError("no calls expected"))
    )

    main(
        [],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )

    assert captured_body["page_size"] == 3


# --- --canonical-key ---------------------------------------------------------------


def test_canonical_key_targets_single_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1", canonical_key="doi:10.1/target")
    captured_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            nonlocal captured_body
            captured_body = json.loads(request.content)
            return httpx.Response(200, json={"results": [page]})
        raise AssertionError("unexpected request")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(_openalex_handler)
    anthropic_client = mock_anthropic_client(_anthropic_handler)

    exit_code = main(
        ["--canonical-key", "doi:10.1/target"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured_body["filter"]["rich_text"]["equals"] == "doi:10.1/target"
    assert "preview" in captured.out


def test_canonical_key_not_found_returns_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(["--canonical-key", "doi:10.1/missing"], notion_client=notion_client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no Notion row found" in captured.err


# --- error handling continues the run --------------------------------------------


def test_anthropic_failure_is_recorded_and_run_continues(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page1 = _notion_page("page-1", canonical_key="doi:10.1/one")
    page2 = _notion_page("page-2", canonical_key="doi:10.1/two")
    error_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page1, page2]})
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal error_body
            error_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-2":
            return httpx.Response(200, json={"id": "page-2"})
        block_response = _block_write_response(request)
        if block_response is not None:
            return block_response
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def openalex_handler(request: httpx.Request) -> httpx.Response:
        return _openalex_work_response()

    call_count = {"n": 0}

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg_refused",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "stop_reason": "refusal",
                    "content": [],
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                },
            )
        return _anthropic_success_response()

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(openalex_handler)
    anthropic_client = mock_anthropic_client(anthropic_handler)

    exit_code = main(
        ["--write-notion"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "error" in captured.out
    assert "drafted" in captured.out
    assert error_body["properties"]["Draft Status"] == {"select": {"name": "Needs Attention"}}
    assert "declined" in error_body["properties"]["Draft Error"]["rich_text"][0]["text"]["content"]


# --- ANTHROPIC_MODEL configurability ---------------------------------------------


class _RecordingAnthropicClient:
    """Stand-in for AnthropicClient that records its constructor args instead of
    making any real (or even mocked-transport) HTTP call."""

    captured_kwargs: dict[str, object] | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        _RecordingAnthropicClient.captured_kwargs = kwargs
        self.model = kwargs.get("model")

    def close(self) -> None:
        pass

    def generate_draft_content(self, prompt: str) -> DraftContent:
        return DraftContent(
            internal_summary="s",
            key_story_angle="a",
            why_it_matters="w",
            draft_social_post="Recorded post.",
            limitations="l",
        )


def test_anthropic_model_defaults_to_claude_opus_5_when_env_absent(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setattr(generate_drafts_module, "AnthropicClient", _RecordingAnthropicClient)
    _RecordingAnthropicClient.captured_kwargs = None

    page = _notion_page("page-1")
    notion_client = mock_notion_client(_notion_query_handler([page]))
    openalex_client = mock_openalex_client(_openalex_handler)

    main([], notion_client=notion_client, openalex_client=openalex_client)

    assert _RecordingAnthropicClient.captured_kwargs is not None
    assert _RecordingAnthropicClient.captured_kwargs["model"] == "claude-opus-5"
    assert _RecordingAnthropicClient.captured_kwargs["api_key"] == "secret-anthropic-key"


def test_anthropic_model_honors_env_override_and_is_preserved_in_draft_model(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    _set_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(generate_drafts_module, "AnthropicClient", _RecordingAnthropicClient)
    _RecordingAnthropicClient.captured_kwargs = None

    page = _notion_page("page-1")
    updated_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal updated_body
            updated_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        block_response = _block_write_response(request)
        if block_response is not None:
            return block_response
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(_openalex_handler)

    exit_code = main(
        ["--write-notion"], notion_client=notion_client, openalex_client=openalex_client
    )

    assert exit_code == 0
    assert _RecordingAnthropicClient.captured_kwargs["model"] == "claude-sonnet-5"
    assert (
        updated_body["properties"]["Draft Model"]["rich_text"][0]["text"]["content"]
        == "claude-sonnet-5"
    )


# --- page-content write failure: never mark Drafted, always record an error ------


def test_page_content_write_failure_is_recorded_and_never_marks_drafted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    """If replace_generated_section fails (e.g. the append-block-children call
    errors), the row must not be marked Drafted — the success properties PATCH
    must never be sent, and the failure must be recorded via Draft Status =
    Needs Attention instead, per the project's failure-behavior rule."""
    _set_env(monkeypatch)
    page = _notion_page("page-1")
    error_body: dict = {}
    success_patch_sent = {"value": False}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(500, json={"message": "internal error"})
        if request.method == "DELETE" and request.url.path.startswith("/v1/blocks/"):
            raise AssertionError("must not delete any blocks when the append itself failed")
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal error_body
            body = json.loads(request.content)
            if body["properties"].get("Draft Status") == {"select": {"name": "Drafted"}}:
                success_patch_sent["value"] = True
            error_body = body
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler, max_retries=0)
    openalex_client = mock_openalex_client(_openalex_handler)
    anthropic_client = mock_anthropic_client(_anthropic_handler)

    exit_code = main(
        ["--write-notion"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "error" in captured.out
    assert "drafted" not in captured.out
    assert not success_patch_sent["value"]
    assert error_body["properties"]["Draft Status"] == {"select": {"name": "Needs Attention"}}
    assert (
        "Notion page content write failed"
        in (error_body["properties"]["Draft Error"]["rich_text"][0]["text"]["content"])
    )


# --- residual literal escape sequences: never mark Drafted, never touch page body --


def test_residual_escape_sequences_block_drafted_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    """If the model emits literal Unicode-escape-looking text (e.g. a literal
    "\\u1f447" instead of an emoji), find_residual_escape_sequences_in_content
    must catch it before any Notion write — the row must not be marked
    Drafted, no page-body blocks may be written, and Draft Error must clearly
    explain why."""
    _set_env(monkeypatch)
    page = _notion_page("page-1")
    error_body: dict = {}

    def corrupted_anthropic_handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "internal_summary": "Summary.",
            "key_story_angle": "Angle.",
            "why_it_matters": "Matters.",
            "draft_social_post": "Tap here \\u1f447 now.",
            "limitations": "None.",
        }
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        if request.method == "PATCH" and request.url.path == "/v1/pages/page-1":
            nonlocal error_body
            error_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        if request.url.path.startswith("/v1/blocks/"):
            raise AssertionError("must not touch the page body for corrupted draft content")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(_openalex_handler)
    anthropic_client = mock_anthropic_client(corrupted_anthropic_handler)

    exit_code = main(
        ["--write-notion"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "error" in captured.out
    assert "drafted" not in captured.out
    assert error_body["properties"]["Draft Status"] == {"select": {"name": "Needs Attention"}}
    error_text = error_body["properties"]["Draft Error"]["rich_text"][0]["text"]["content"]
    assert "escape" in error_text.lower()
    assert "draft_social_post" in error_text


# --- Approved rows: page body is never touched -------------------------------------


def test_approved_row_page_content_is_never_touched(
    monkeypatch: pytest.MonkeyPatch,
    mock_notion_client: Callable[..., NotionClient],
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    _set_env(monkeypatch)
    page = _notion_page("page-1", draft_status="Approved")

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "POST"
            and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}/query"
        ):
            return httpx.Response(200, json={"results": [page]})
        raise AssertionError(
            f"must not touch page content or properties for an Approved row: "
            f"{request.method} {request.url}"
        )

    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call external services for an Approved row")

    notion_client = mock_notion_client(notion_handler)
    openalex_client = mock_openalex_client(fail)
    anthropic_client = mock_anthropic_client(fail)

    exit_code = main(
        ["--write-notion", "--force"],
        notion_client=notion_client,
        openalex_client=openalex_client,
        anthropic_client=anthropic_client,
    )

    assert exit_code == 0
