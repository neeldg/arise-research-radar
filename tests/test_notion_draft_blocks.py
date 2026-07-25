"""Tests for the page-body (block) storage of generated drafts.

The full draft (social post, internal summary, key story angle, why it
matters, generation metadata) is written as Notion blocks in the page body,
not as a single rich_text property — see arise_radar.sinks.notion.
build_generated_section_blocks / replace_generated_section.
"""

import json
from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.drafting import DraftContent
from arise_radar.sinks.notion import (
    GENERATED_SECTION_MARKER,
    MAX_RICH_TEXT_LENGTH,
    NotionClient,
    NotionError,
    build_generated_section_blocks,
    replace_generated_section,
)


def _draft_content(**overrides: object) -> DraftContent:
    defaults: dict[str, object] = {
        "internal_summary": "Summary.",
        "key_story_angle": "Angle.",
        "why_it_matters": "Matters.",
        "draft_social_post": "Post.",
        "limitations": "Some limitation.",
    }
    defaults.update(overrides)
    return DraftContent(**defaults)


def _block_text(block: dict) -> str:
    block_type = block["type"]
    return "".join(part["text"]["content"] for part in block[block_type]["rich_text"])


def _existing_block(block_id: str, *, text: str, block_type: str = "paragraph") -> dict:
    """A block shaped like Notion's actual API response (includes plain_text)."""
    rich_text = [{"type": "text", "text": {"content": text}, "plain_text": text}]
    return {"id": block_id, "type": block_type, block_type: {"rich_text": rich_text}}


def _heading_index(blocks: list[dict], text: str) -> int:
    return next(
        i
        for i, b in enumerate(blocks)
        if b["type"].startswith("heading") and _block_text(b) == text
    )


# --- build_generated_section_blocks ----------------------------------------------


def test_marker_is_the_first_block() -> None:
    blocks = build_generated_section_blocks(
        _draft_content(),
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 1),
    )
    assert blocks[0]["type"] == "heading_3"
    assert _block_text(blocks[0]) == GENERATED_SECTION_MARKER


def test_section_headings_appear_in_order() -> None:
    blocks = build_generated_section_blocks(
        _draft_content(),
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 1),
    )
    headings = [_block_text(b) for b in blocks if b["type"].startswith("heading")]
    assert headings == [
        GENERATED_SECTION_MARKER,
        "Draft Social Post",
        "Internal Summary",
        "Key Story Angle",
        "Why It Matters",
        "Generation Information",
    ]


def test_generation_information_bullets() -> None:
    blocks = build_generated_section_blocks(
        _draft_content(),
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 3, 4),
    )
    bullets = [_block_text(b) for b in blocks if b["type"] == "bulleted_list_item"]
    assert bullets == [
        "Source basis: OpenAlex Abstract",
        "Model: claude-opus-5",
        "Drafted date: 2026-03-04",
    ]


def test_limitations_folded_into_internal_summary_section() -> None:
    content = _draft_content(internal_summary="Main summary.", limitations="Small sample size.")
    blocks = build_generated_section_blocks(
        content, source_basis="Metadata Only", model_name="m", drafted_date=date(2026, 1, 1)
    )
    start = _heading_index(blocks, "Internal Summary") + 1
    end = _heading_index(blocks, "Key Story Angle")
    section_text = "".join(_block_text(b) for b in blocks[start:end])
    assert "Main summary." in section_text
    assert "Small sample size." in section_text


# --- long content / no truncation -------------------------------------------------


def test_long_draft_splits_across_blocks_without_truncation() -> None:
    long_post = "A" * 5000
    blocks = build_generated_section_blocks(
        _draft_content(draft_social_post=long_post),
        source_basis="Metadata Only",
        model_name="m",
        drafted_date=date(2026, 1, 1),
    )
    start = _heading_index(blocks, "Draft Social Post") + 1
    end = _heading_index(blocks, "Internal Summary")
    post_blocks = blocks[start:end]

    assert len(post_blocks) > 1
    for block in post_blocks:
        for part in block["paragraph"]["rich_text"]:
            assert len(part["text"]["content"]) <= MAX_RICH_TEXT_LENGTH
    assert "".join(_block_text(b) for b in post_blocks) == long_post


# --- paragraph breaks preserved ----------------------------------------------------


def test_multiple_paragraphs_become_separate_blocks() -> None:
    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    blocks = build_generated_section_blocks(
        _draft_content(why_it_matters=text),
        source_basis="Metadata Only",
        model_name="m",
        drafted_date=date(2026, 1, 1),
    )
    start = _heading_index(blocks, "Why It Matters") + 1
    end = _heading_index(blocks, "Generation Information")
    paragraphs = blocks[start:end]

    assert [_block_text(b) for b in paragraphs] == [
        "First paragraph.",
        "Second paragraph.",
        "Third paragraph.",
    ]


# --- unicode / emoji ----------------------------------------------------------------


def test_unicode_and_emoji_preserved_exactly() -> None:
    text = "Great result 🎉 — clínicians report é, ñ, 中文, 🚀🔥"
    blocks = build_generated_section_blocks(
        _draft_content(key_story_angle=text),
        source_basis="Metadata Only",
        model_name="m",
        drafted_date=date(2026, 1, 1),
    )
    start = _heading_index(blocks, "Key Story Angle") + 1
    end = _heading_index(blocks, "Why It Matters")
    assert "".join(_block_text(b) for b in blocks[start:end]) == text


# --- replace_generated_section: rerun replaces, never duplicates -------------------


def test_rerun_deletes_old_generated_section_and_appends_new(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    human_block = _existing_block("human-1", text="Editor's note: looks good.")
    old_marker = _existing_block(
        "marker-old", text=GENERATED_SECTION_MARKER, block_type="heading_3"
    )
    old_content = _existing_block("old-content-1", text="stale draft text")

    deleted_ids: list[str] = []
    appended: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(
                200,
                json={
                    "results": [human_block, old_marker, old_content],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            appended.extend(json.loads(request.content)["children"])
            return httpx.Response(200, json={"results": []})
        if request.method == "DELETE" and request.url.path.startswith("/v1/blocks/"):
            deleted_ids.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"archived": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    new_blocks = build_generated_section_blocks(
        _draft_content(draft_social_post="Fresh draft."),
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 2),
    )

    replace_generated_section(client, "page-1", new_blocks)

    assert deleted_ids == ["marker-old", "old-content-1"]
    assert appended == new_blocks


def test_human_content_before_marker_is_never_deleted(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    human_block_1 = _existing_block("human-1", text="Human intro.")
    human_block_2 = _existing_block("human-2", text="Editorial Notes: check facts.")
    marker = _existing_block("marker-old", text=GENERATED_SECTION_MARKER, block_type="heading_3")
    old_content = _existing_block("old-content-1", text="stale")
    new_blocks = [{"type": "paragraph", "paragraph": {"rich_text": []}}]

    deleted_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(
                200,
                json={
                    "results": [human_block_1, human_block_2, marker, old_content],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(200, json={"results": []})
        if request.method == "DELETE" and request.url.path.startswith("/v1/blocks/"):
            deleted_ids.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"archived": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    replace_generated_section(client, "page-1", new_blocks)

    assert "human-1" not in deleted_ids
    assert "human-2" not in deleted_ids


def test_first_run_with_no_prior_marker_only_appends(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    human_block = _existing_block("human-1", text="Some human content.")
    deleted_ids: list[str] = []
    new_blocks = [{"type": "paragraph", "paragraph": {"rich_text": []}}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(
                200, json={"results": [human_block], "has_more": False, "next_cursor": None}
            )
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(200, json={"results": []})
        if request.method == "DELETE":
            deleted_ids.append(request.url.path)
            return httpx.Response(200, json={"archived": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)
    replace_generated_section(client, "page-1", new_blocks)

    assert deleted_ids == []


# --- partial-write failure handling -------------------------------------------------


def test_append_failure_raises_and_leaves_old_content_undeleted(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    marker = _existing_block("marker-old", text=GENERATED_SECTION_MARKER, block_type="heading_3")
    old_content = _existing_block("old-content-1", text="stale draft")
    delete_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(
                200,
                json={"results": [marker, old_content], "has_more": False, "next_cursor": None},
            )
        if request.method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            return httpx.Response(500, json={"message": "internal error"})
        if request.method == "DELETE":
            delete_calls["n"] += 1
            return httpx.Response(200, json={"archived": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler, max_retries=0)

    with pytest.raises(NotionError):
        replace_generated_section(
            client, "page-1", [{"type": "paragraph", "paragraph": {"rich_text": []}}]
        )

    assert delete_calls["n"] == 0
