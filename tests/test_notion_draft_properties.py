from datetime import date

from arise_radar.drafting import DraftContent
from arise_radar.sinks.notion import (
    build_draft_error_properties,
    build_draft_success_properties,
    read_multi_select_names,
    read_rich_text,
    read_select,
    read_title,
)


def _page(**properties: dict) -> dict:
    return {"id": "page-1", "properties": properties}


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


# --- readers --------------------------------------------------------------------


def test_read_title() -> None:
    page = _page(Name={"type": "title", "title": [{"plain_text": "A Great Paper"}]})
    assert read_title(page) == "A Great Paper"


def test_read_title_empty() -> None:
    page = _page(Name={"type": "title", "title": []})
    assert read_title(page) == ""


def test_read_rich_text_present() -> None:
    page = _page(
        **{"Canonical Key": {"type": "rich_text", "rich_text": [{"plain_text": "doi:10.1/x"}]}}
    )
    assert read_rich_text(page, "Canonical Key") == "doi:10.1/x"


def test_read_rich_text_absent_returns_none() -> None:
    page = _page()
    assert read_rich_text(page, "OpenAlex ID") is None


def test_read_select_present() -> None:
    page = _page(Status={"type": "select", "select": {"name": "New"}})
    assert read_select(page, "Status") == "New"


def test_read_select_empty_returns_none() -> None:
    page = _page(Status={"type": "select", "select": None})
    assert read_select(page, "Status") is None


def test_read_multi_select_names_sorted() -> None:
    page = _page(
        Researchers={
            "type": "multi_select",
            "multi_select": [{"name": "Ethan Goh"}, {"name": "Adam Rodman"}],
        }
    )
    assert read_multi_select_names(page, "Researchers") == ["Adam Rodman", "Ethan Goh"]


# --- draft property builders -----------------------------------------------------
#
# The complete draft (internal summary, key story angle, why it matters, full
# social post) now lives in the page body — see test_notion_draft_blocks.py.
# Only a short Draft Social Post preview plus pipeline-fact properties are
# written here.


def test_build_draft_success_properties_sets_short_preview_and_pipeline_facts() -> None:
    content = _draft_content(draft_social_post="A short post.")
    props = build_draft_success_properties(
        content,
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 1),
    )

    assert props["Draft Social Post"]["rich_text"][0]["text"]["content"] == "A short post."
    assert props["Draft Status"] == {"select": {"name": "Drafted"}}
    assert props["Draft Source Basis"] == {"select": {"name": "OpenAlex Abstract"}}
    assert props["Draft Model"]["rich_text"][0]["text"]["content"] == "claude-opus-5"
    assert props["Drafted Date"] == {"date": {"start": "2026-01-01"}}


def test_build_draft_success_properties_caps_long_post_at_approximately_500_chars() -> None:
    long_post = "x" * 3000
    content = _draft_content(draft_social_post=long_post)
    props = build_draft_success_properties(
        content,
        source_basis="OpenAlex Abstract",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 1),
    )

    preview = props["Draft Social Post"]["rich_text"][0]["text"]["content"]
    assert len(preview) <= 500
    assert preview != long_post


def test_build_draft_success_properties_never_touches_preserved_fields() -> None:
    content = _draft_content()
    props = build_draft_success_properties(
        content,
        source_basis="Metadata Only",
        model_name="claude-opus-5",
        drafted_date=date(2026, 1, 1),
    )

    for preserved in (
        "Status",
        "Researchers",
        "Editorial Notes",
        "Published Date",
        "DOI",
        "OpenAlex ID",
        "URL",
        "Internal Summary",
        "Key Story Angle",
        "Why It Matters",
    ):
        assert preserved not in props


def test_build_draft_error_properties() -> None:
    props = build_draft_error_properties(
        error_message="Anthropic timed out", drafted_date=date(2026, 1, 2)
    )

    assert props["Draft Status"] == {"select": {"name": "Needs Attention"}}
    assert props["Draft Error"]["rich_text"][0]["text"]["content"] == "Anthropic timed out"
    assert props["Drafted Date"] == {"date": {"start": "2026-01-02"}}
    for content_field in (
        "Internal Summary",
        "Key Story Angle",
        "Why It Matters",
        "Draft Social Post",
    ):
        assert content_field not in props
