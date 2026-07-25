import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.drafting import (
    DEFAULT_MODEL,
    AnthropicClient,
    DraftContent,
    DraftGenerationError,
    DraftingConfigError,
    DraftInput,
    build_prompt,
    build_retry_prompt,
    determine_source_basis,
    find_residual_escape_sequences,
    find_residual_escape_sequences_in_content,
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


# --- Unicode fidelity: the SDK parses exactly once via pydantic's real JSON --------
#
# generate_draft_content must never re-parse, regex-extract, or otherwise
# reprocess response.parsed_output — see its docstring. These tests prove the
# ordinary path (response.parsed_output used directly) already round-trips
# real Unicode correctly, and that malformed/double-encoded model output is
# turned into a clean DraftGenerationError rather than corrupted data or an
# uncaught exception.


def _response_with_raw_text(raw_text: str) -> dict:
    """A structured-output response whose message text is exactly `raw_text`
    (not built via _success_response_json's own json.dumps), so tests can
    control precisely what JSON syntax the "model" is claimed to have sent."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": raw_text}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("draft_social_post", "Results improved — significantly, not marginally."),
        ("key_story_angle", "Researchers called it “a turning point” for the field."),
        ("internal_summary", "Key points:\n• Faster\n• Cheaper\n• Safer"),
        ("why_it_matters", "Great result 🎉 for clinicians."),
        ("why_it_matters", "Patients’ outcomes improved across all cohorts."),
        ("internal_summary", "Paragraph one.\n\nParagraph two.\n\nParagraph three."),
    ],
    ids=["em-dash", "curly-quotes", "bullet", "emoji", "apostrophe", "multiline"],
)
def test_generate_draft_content_preserves_unicode(
    mock_anthropic_client: Callable[..., AnthropicClient], field: str, value: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response_json(**{field: value}))

    client = mock_anthropic_client(handler)
    content = client.generate_draft_content("some prompt")

    assert getattr(content, field) == value
    assert "\\u" not in getattr(content, field)


def test_generate_draft_content_decodes_valid_surrogate_pair_emoji(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    """content.text (the raw JSON the SDK parses) contains a real, valid UTF-16
    surrogate pair escape for an astral emoji (👇, U+1F447) — exactly what a
    conformant JSON encoder produces when escaping it. One correct parse must
    reconstruct the single real character."""
    raw_text = (
        '{"internal_summary": "s", "key_story_angle": "a", "why_it_matters": "w", '
        '"draft_social_post": "Tap here \\ud83d\\udc47 to read more.", "limitations": "l"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response_with_raw_text(raw_text))

    client = mock_anthropic_client(handler)
    content = client.generate_draft_content("some prompt")

    assert content.draft_social_post == "Tap here 👇 to read more."
    assert "\\u" not in content.draft_social_post


def test_generate_draft_content_malformed_five_digit_escape_survives_as_literal_text(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    """JSON requires exactly 4 hex digits per \\u escape, so a model-generated
    attempt like "\\u1f447" (5 digits) can never be a valid escape — it's just
    literal text once parsed. Parsing must succeed (it's syntactically valid
    JSON) and must not silently "fix" or further mangle that literal text;
    find_residual_escape_sequences is what catches it downstream."""
    payload = {
        "internal_summary": "s",
        "key_story_angle": "a",
        "why_it_matters": "w",
        "draft_social_post": "Tap here \\u1f447 now.",
        "limitations": "l",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response_json(**payload))

    client = mock_anthropic_client(handler)
    content = client.generate_draft_content("some prompt")

    assert content.draft_social_post == "Tap here \\u1f447 now."
    assert find_residual_escape_sequences(content.draft_social_post) == ["\\u1f447"]


def test_generate_draft_content_double_encoded_json_raises_draft_generation_error(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    """If the model's message text is itself a JSON-encoded string (rather than
    the DraftContent object directly), pydantic's single validate_json call
    correctly rejects it as a type mismatch (string, not object) — this must
    surface as a clean, per-item DraftGenerationError, not an uncaught
    pydantic.ValidationError that would crash the whole run."""
    inner_json = json.dumps(
        {
            "internal_summary": "s",
            "key_story_angle": "a",
            "why_it_matters": "w",
            "draft_social_post": "p",
            "limitations": "l",
        }
    )
    double_encoded_text = json.dumps(inner_json)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_response_with_raw_text(double_encoded_text))

    client = mock_anthropic_client(handler)

    with pytest.raises(DraftGenerationError, match="could not be parsed as structured output"):
        client.generate_draft_content("some prompt")


# --- find_residual_escape_sequences: detector, never a fixer -----------------------


def test_find_residual_escape_sequences_detects_literal_em_dash_escape() -> None:
    assert find_residual_escape_sequences("Results improved \\u2014 significantly.") == ["\\u2014"]


def test_find_residual_escape_sequences_detects_malformed_five_digit_escape() -> None:
    assert find_residual_escape_sequences("Tap here \\u1f447 now.") == ["\\u1f447"]


def test_find_residual_escape_sequences_clean_unicode_text_returns_empty() -> None:
    assert find_residual_escape_sequences("Results improved — significantly. 🎉 •") == []


def test_find_residual_escape_sequences_detects_orphaned_surrogate_code_point() -> None:
    assert find_residual_escape_sequences("broken \ud800 text") == [
        "<orphaned surrogate code point>"
    ]


def test_find_residual_escape_sequences_in_content_reports_only_affected_fields() -> None:
    content = DraftContent(
        internal_summary="Clean summary.",
        key_story_angle="Clean angle.",
        why_it_matters="Broken \\u2022 bullet.",
        draft_social_post="Broken \\u1f447 emoji.",
        limitations="Clean limitations.",
    )

    problems = find_residual_escape_sequences_in_content(content)

    assert set(problems) == {"why_it_matters", "draft_social_post"}
    assert problems["why_it_matters"] == ["\\u2022"]
    assert problems["draft_social_post"] == ["\\u1f447"]


def test_find_residual_escape_sequences_in_content_clean_content_returns_empty() -> None:
    content = DraftContent(
        internal_summary="Clean — summary with “quotes”.",
        key_story_angle="Clean angle.",
        why_it_matters="Clean, with a bullet: • done.",
        draft_social_post="Clean 🎉 post.",
        limitations="None evident.",
    )

    assert find_residual_escape_sequences_in_content(content) == {}


# --- build_retry_prompt -------------------------------------------------------------


def test_build_retry_prompt_names_fields_and_shows_example_escape_and_character() -> None:
    prompt = build_retry_prompt("ORIGINAL PROMPT", {"draft_social_post": ["\\u1f447"]})

    assert prompt.startswith("ORIGINAL PROMPT")
    assert "CORRECTION REQUIRED" in prompt
    assert "draft_social_post" in prompt
    # Clearly states the prior response contained literal escape syntax...
    assert "\\u2014" in prompt or "\\u2022" in prompt
    # ...and requires actual visible Unicode characters.
    assert "—" in prompt
    assert "•" in prompt


# --- generate_draft_for_paper: retry exactly once on residual escape sequences -----


def _corrupted_response_json() -> dict:
    return _success_response_json(draft_social_post="Tap here \\u1f447 now.")


def _clean_response_json() -> dict:
    return _success_response_json(draft_social_post="Tap here 👇 now.")


def test_generate_draft_for_paper_retries_once_when_first_attempt_corrupted(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    call_count = {"n": 0}
    captured_prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        captured_prompts.append(json.loads(request.content)["messages"][0]["content"])
        if call_count["n"] == 1:
            return httpx.Response(200, json=_corrupted_response_json())
        return httpx.Response(200, json=_clean_response_json())

    client = mock_anthropic_client(handler)
    content = generate_draft_for_paper(client, _draft_input(), "STYLE_EXAMPLE", "STYLE_GUIDE")

    assert call_count["n"] == 2
    assert content.draft_social_post == "Tap here 👇 now."
    assert find_residual_escape_sequences_in_content(content) == {}
    # The retry prompt clearly names the affected field and shows a real
    # example escape sequence and a real example character.
    retry_prompt = captured_prompts[1]
    assert "draft_social_post" in retry_prompt
    assert "\\u2014" in retry_prompt or "\\u2022" in retry_prompt
    assert "—" in retry_prompt or "•" in retry_prompt


def test_generate_draft_for_paper_no_retry_when_first_attempt_clean(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_clean_response_json())

    client = mock_anthropic_client(handler)
    content = generate_draft_for_paper(client, _draft_input(), "STYLE_EXAMPLE", "STYLE_GUIDE")

    assert call_count["n"] == 1
    assert content.draft_social_post == "Tap here 👇 now."


def test_generate_draft_for_paper_returns_still_corrupted_content_after_failed_retry(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json=_corrupted_response_json())

    client = mock_anthropic_client(handler)
    content = generate_draft_for_paper(client, _draft_input(), "STYLE_EXAMPLE", "STYLE_GUIDE")

    assert call_count["n"] == 2
    assert find_residual_escape_sequences_in_content(content) != {}


def test_generate_draft_for_paper_propagates_error_raised_during_retry(
    mock_anthropic_client: Callable[..., AnthropicClient],
) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(200, json=_corrupted_response_json())
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "boom"}},
        )

    client = mock_anthropic_client(handler)

    with pytest.raises(DraftGenerationError, match="Anthropic API request failed"):
        generate_draft_for_paper(client, _draft_input(), "STYLE_EXAMPLE", "STYLE_GUIDE")

    assert call_count["n"] == 2
