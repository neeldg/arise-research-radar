from datetime import date

from arise_radar.models import NormalizedPublication, Researcher
from arise_radar.output import (
    RunSummary,
    format_exclusion_summary,
    format_publications,
    format_relevance_summary,
    format_run_summary,
    format_skip_warning,
)
from arise_radar.relevance import RelevanceDecision

KEEP = RelevanceDecision(status="keep", reason="matched a healthcare/clinical-AI signal")
UNCERTAIN = RelevanceDecision(status="uncertain", reason="no clear signal found")


def _pub(**overrides: object) -> NormalizedPublication:
    defaults: dict[str, object] = {
        "researcher_id": "jane_doe",
        "researcher_name": "Jane Doe",
        "title": "A Great Paper",
        "publication_date": date(2026, 1, 1),
        "doi": "10.1000/abc",
        "openalex_id": "W1",
        "canonical_key": "doi:10.1000/abc",
    }
    defaults.update(overrides)
    return NormalizedPublication(**defaults)


def test_format_skip_warning_includes_name_id_and_reason() -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id=None)
    message = format_skip_warning(researcher, "no verified OpenAlex ID")
    assert "Jane Doe" in message
    assert "jane_doe" in message
    assert "no verified OpenAlex ID" in message


def test_format_publications_empty_list() -> None:
    assert "No publications found" in format_publications([])


def test_format_publications_includes_required_fields() -> None:
    rendered = format_publications([(_pub(), KEEP)])
    assert "Jane Doe" in rendered
    assert "A Great Paper" in rendered
    assert "2026-01-01" in rendered
    assert "10.1000/abc" in rendered
    assert "W1" in rendered
    assert "doi:10.1000/abc" in rendered
    assert "UNCERTAIN" not in rendered


def test_format_publications_marks_uncertain_entries() -> None:
    rendered = format_publications([(_pub(), UNCERTAIN)])
    assert "[UNCERTAIN" in rendered
    assert "no clear signal found" in rendered


def test_format_relevance_summary() -> None:
    summary = format_relevance_summary(3, 1, 2)
    assert "Kept: 3" in summary
    assert "Uncertain but kept: 1" in summary
    assert "Clearly unrelated and excluded: 2" in summary


def test_format_exclusion_summary_empty() -> None:
    assert format_exclusion_summary([], show_details=False) == ""
    assert format_exclusion_summary([], show_details=True) == ""


def test_format_exclusion_summary_short_form_hides_titles() -> None:
    decision = RelevanceDecision(
        status="exclude", reason="food science", matched_terms=["food science"]
    )
    summary = format_exclusion_summary(
        [(_pub(title="Pumpkin seed flour study"), decision)], show_details=False
    )
    assert "Excluded (1)" in summary
    assert "--show-excluded" in summary
    assert "Pumpkin seed flour study" not in summary


def test_format_exclusion_summary_full_details() -> None:
    decision = RelevanceDecision(
        status="exclude", reason="food science", matched_terms=["food science", "pumpkin"]
    )
    summary = format_exclusion_summary(
        [(_pub(title="Pumpkin seed flour study"), decision)], show_details=True
    )
    assert "Pumpkin seed flour study" in summary
    assert "food science" in summary
    assert "pumpkin" in summary
    assert "W1" in summary
    assert "2026-01-01" in summary


# --- format_run_summary -----------------------------------------------------------


def test_format_run_summary_includes_all_counts() -> None:
    summary = RunSummary(
        raw_author_work_matches=12,
        unique_canonical_keys=10,
        existing_rows=2,
        proposed_new_rows=8,
        shared_author_works=3,
        standard_draft_eligible_works=6,
        duplicate_flagged_held_for_review=1,
        non_standard_held_for_review=2,
    )
    rendered = format_run_summary(summary)

    assert "Raw author-work matches:" in rendered
    assert "12" in rendered
    assert "Unique canonical keys:" in rendered
    assert "10" in rendered
    assert "Existing rows:" in rendered
    assert "2" in rendered
    assert "Proposed new rows:" in rendered
    assert "8" in rendered
    assert "Shared-author works:" in rendered
    assert "3" in rendered
    assert "Standard draft-eligible works:" in rendered
    assert "6" in rendered
    assert "Duplicate-flagged (held for review):" in rendered
    assert "1" in rendered
    assert "Non-standard (held for review):" in rendered


def test_format_run_summary_omits_notion_specific_counts_when_none() -> None:
    summary = RunSummary(
        raw_author_work_matches=5,
        unique_canonical_keys=5,
        shared_author_works=0,
        standard_draft_eligible_works=5,
        duplicate_flagged_held_for_review=0,
        non_standard_held_for_review=0,
    )
    rendered = format_run_summary(summary)

    assert "Existing rows:" not in rendered
    assert "Proposed new rows:" not in rendered
