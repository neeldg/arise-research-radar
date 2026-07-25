import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.drafting import (
    DEFAULT_MODEL,
    AnthropicClient,
    DraftGenerationError,
    DraftingConfigError,
    DraftInput,
    build_prompt,
    determine_source_basis,
    generate_draft_for_paper,
    load_anthropic_config,
    load_style_files,
)


def _draft_input(**overrides: object) -> DraftInput:
    defaults: dict[str, object] = {
        "title": "A Great Paper",
        "researcher_names": ["Ethan Goh"],
        "publication_date": date(2026, 1, 1),
        "venue": "Nature Medicine",
        "doi": "10.1000/abc",
        "openalex_id": "W1",
        "abstract": "This study evaluated a large language model on clinical cases.",
    }
    defaults.update(overrides)
    return DraftInput(**defaults)


def _success_response_json(**overrides: object) -> dict:
    payload = {
        "internal_summary": "Summary text.",
        "key_story_angle": "Angle text.",
        "why_it_matters": "Matters text.",
        "draft_social_post": "Post text.",
        "limitations": "Limitations text.",
    }
    payload.update(overrides)
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


# --- determine_source_basis ---------------------------------------------------


def test_source_basis_with_abstract() -> None:
    assert determine_source_basis("some abstract text") == "OpenAlex Abstract"


def test_source_basis_without_abstract() -> None:
    assert determine_source_basis(None) == "Metadata Only"


# --- build_prompt --------------------------------------------------------------


def test_build_prompt_includes_paper_facts_and_style_material() -> None:
    prompt = build_prompt(_draft_input(), style_example="EXAMPLE_TEXT", style_guide="GUIDE_TEXT")
    assert "A Great Paper" in prompt
    assert "Ethan Goh" in prompt
    assert "Nature Medicine" in prompt
    assert "10.1000/abc" in prompt
    assert "This study evaluated a large language model" in prompt
    assert "EXAMPLE_TEXT" in prompt
    assert "GUIDE_TEXT" in prompt
    assert "never reuse its facts, numbers, model names, or claims" in prompt.lower()


def test_build_prompt_warns_when_no_abstract() -> None:
    prompt = build_prompt(_draft_input(abstract=None), style_example="EXAMPLE", style_guide="GUIDE")
    assert "No abstract is available" in prompt
    assert "Do not invent findings" in prompt


# --- load_style_files (integration: real committed files) ----------------------


def test_load_style_files_reads_real_committed_files() -> None:
    example, guide = load_style_files()
    assert "Nature Medicine" in example
    assert "Eric Horvitz" in example
    assert "style reference, not a factual template" in guide.lower()


# --- load_anthropic_config -------------------------------------------------------


def test_load_anthropic_config_missing_api_key_raises() -> None:
    with pytest.raises(DraftingConfigError, match="ANTHROPIC_API_KEY"):
        load_anthropic_config(env={})


def test_load_anthropic_config_missing_api_key_raises_even_with_model_set() -> None:
    with pytest.raises(DraftingConfigError, match="ANTHROPIC_API_KEY"):
        load_anthropic_config(env={"ANTHROPIC_MODEL": "claude-sonnet-5"})


def test_load_anthropic_config_defaults_model_when_absent() -> None:
    config = load_anthropic_config(env={"ANTHROPIC_API_KEY": "secret-key"})
    assert config.api_key == "secret-key"
    assert config.model == DEFAULT_MODEL
    assert DEFAULT_MODEL == "claude-opus-5"


def test_load_anthropic_config_defaults_model_when_empty_string() -> None:
    config = load_anthropic_config(env={"ANTHROPIC_API_KEY": "secret-key", "ANTHROPIC_MODEL": ""})
    assert config.model == DEFAULT_MODEL


def test_load_anthropic_config_honors_explicit_model() -> None:
    config = load_anthropic_config(
        env={"ANTHROPIC_API_KEY": "secret-key", "ANTHROPIC_MODEL": "claude-sonnet-5"}
    )
    assert config.model == "claude-sonnet-5"


# --- AnthropicClient.generate_draft_content ------------------------------------


def test_generate_draft_content_success(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/messages"
        return httpx.Response(200, json=_success_response_json())

    client = mock_anthropic_client(handler)
    content = client.generate_draft_content("some prompt")

    assert content.internal_summary == "Summary text."
    assert content.draft_social_post == "Post text."


def test_generate_draft_content_refusal_raises(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5",
                "stop_reason": "refusal",
                "content": [],
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        )

    client = mock_anthropic_client(handler)

    with pytest.raises(DraftGenerationError, match="declined"):
        client.generate_draft_content("some prompt")


def test_generate_draft_content_api_error_raises(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad request"},
            },
        )

    client = mock_anthropic_client(handler)

    with pytest.raises(DraftGenerationError, match="Anthropic API request failed"):
        client.generate_draft_content("some prompt")


def test_generate_draft_for_paper_builds_prompt_and_calls_client(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    captured_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(200, json=_success_response_json())

    client = mock_anthropic_client(handler)
    content = generate_draft_for_paper(
        client, _draft_input(), "STYLE_EXAMPLE_TEXT", "STYLE_GUIDE_TEXT"
    )

    assert content.key_story_angle == "Angle text."
    sent_prompt = captured_body["messages"][0]["content"]
    assert "STYLE_EXAMPLE_TEXT" in sent_prompt
    assert "STYLE_GUIDE_TEXT" in sent_prompt
    assert "A Great Paper" in sent_prompt
