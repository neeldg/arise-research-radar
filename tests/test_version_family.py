from datetime import date

from arise_radar.models import NormalizedPublication
from arise_radar.version_family import (
    TITLE_SIMILARITY_THRESHOLD,
    find_possible_version_duplicates,
    format_version_duplicate_note,
    normalize_title,
    title_similarity,
)


def _pub(**overrides: object) -> NormalizedPublication:
    defaults: dict[str, object] = {
        "researcher_id": "jane_doe",
        "researcher_name": "Jane Doe",
        "title": "A Great Paper",
        "publication_date": date(2026, 1, 1),
        "doi": "10.1000/abc",
        "openalex_id": "W1",
        "canonical_key": "doi:10.1000/abc",
        "authors": ["Jane Doe"],
    }
    defaults.update(overrides)
    return NormalizedPublication(**defaults)


# --- normalize_title / title_similarity --------------------------------------------


def test_normalize_title_strips_punctuation_and_version_markers() -> None:
    assert (
        normalize_title("GP AI Skin Cancer Portal Study (v1)!") == "gp ai skin cancer portal study"
    )


def test_title_similarity_near_duplicate_scores_high() -> None:
    a = "Large Language Models for Clinical Chart Abstraction: Comparative Study"
    b = "Large Language Models for Clinical Chart Abstraction: Comparative Study (Preprint)"
    assert title_similarity(a, b) >= TITLE_SIMILARITY_THRESHOLD


def test_title_similarity_unrelated_titles_scores_low() -> None:
    a = "Clinical decision support with large language models"
    b = "Structure-function modulation of pumpkin seed flour"
    assert title_similarity(a, b) < TITLE_SIMILARITY_THRESHOLD


# --- find_possible_version_duplicates: the two-DOI example from the dry-run -------


def test_same_study_two_preprint_doi_is_flagged() -> None:
    jmir = _pub(
        title="Large Language Models for Clinical Chart Abstraction: Comparative Study",
        doi="10.2196/preprints.105583",
        canonical_key="doi:10.2196/preprints.105583",
        authors=["Jonathan H Chen", "Alice Kim"],
    )
    preprints_org = _pub(
        title="Large Language Models for Clinical Chart Abstraction: Comparative Study",
        doi="10.20944/preprints202607.0060.v1",
        canonical_key="doi:10.20944/preprints202607.0060.v1",
        authors=["Jonathan H Chen", "Alice Kim"],
    )

    matches = find_possible_version_duplicates(jmir, [jmir, preprints_org])

    assert len(matches) == 1
    assert matches[0].canonical_key == "doi:10.20944/preprints202607.0060.v1"
    assert matches[0].shared_authors == ["Alice Kim", "Jonathan H Chen"]
    assert matches[0].title_similarity >= TITLE_SIMILARITY_THRESHOLD


def test_similar_title_and_overlapping_authors_different_doi_is_flagged_not_merged() -> None:
    pub_a = _pub(
        title="A Novel Machine Learning Approach to Sepsis Prediction in the ICU",
        canonical_key="doi:10.1/one",
        authors=["Jane Doe", "Bob Lee"],
    )
    pub_b = _pub(
        title="A novel machine learning approach to sepsis prediction in the ICU",
        canonical_key="doi:10.2/two",
        authors=["Bob Lee", "Carla Reyes"],
    )

    matches = find_possible_version_duplicates(pub_a, [pub_a, pub_b])

    assert len(matches) == 1
    assert matches[0].canonical_key == "doi:10.2/two"
    assert matches[0].shared_authors == ["Bob Lee"]
    # Canonical keys are never merged -- the caller decides. Confirm the
    # inputs are untouched.
    assert pub_a.canonical_key == "doi:10.1/one"
    assert pub_b.canonical_key == "doi:10.2/two"


def test_unrelated_similar_titles_without_author_overlap_are_not_flagged() -> None:
    """Similarly-worded titles about genuinely different papers, by different
    authors, must not be flagged -- title similarity alone is not enough."""
    radiology = _pub(
        title="Large language models for radiology report generation: a randomized trial",
        canonical_key="doi:10.1/radiology",
        authors=["Alice Kim", "Bob Lee"],
    )
    pathology = _pub(
        title="Large language models for pathology report generation: a randomized trial",
        canonical_key="doi:10.1/pathology",
        authors=["Carla Reyes", "Dave Osei"],
    )

    matches = find_possible_version_duplicates(radiology, [radiology, pathology])

    assert matches == []


def test_high_similarity_but_no_authors_at_all_is_not_flagged() -> None:
    pub_a = _pub(title="Some Study", canonical_key="doi:10.1/one", authors=[])
    pub_b = _pub(title="Some Study", canonical_key="doi:10.1/two", authors=[])

    assert find_possible_version_duplicates(pub_a, [pub_a, pub_b]) == []


def test_overlapping_authors_but_low_title_similarity_is_not_flagged() -> None:
    pub_a = _pub(
        title="Clinical decision support with large language models",
        canonical_key="doi:10.1/one",
        authors=["Jane Doe"],
    )
    pub_b = _pub(
        title="Structure-function modulation of pumpkin seed flour",
        canonical_key="doi:10.1/two",
        authors=["Jane Doe"],
    )

    assert find_possible_version_duplicates(pub_a, [pub_a, pub_b]) == []


def test_same_canonical_key_is_never_matched_against_itself() -> None:
    pub = _pub(canonical_key="doi:10.1/one", authors=["Jane Doe"])
    assert find_possible_version_duplicates(pub, [pub]) == []


# --- format_version_duplicate_note --------------------------------------------------


def test_format_version_duplicate_note_empty_for_no_matches() -> None:
    assert format_version_duplicate_note([]) == ""


def test_format_version_duplicate_note_links_probable_versions() -> None:
    pub_a = _pub(
        title="A Great Paper",
        canonical_key="doi:10.1/one",
        authors=["Jane Doe"],
    )
    pub_b = _pub(
        title="A Great Paper",
        canonical_key="doi:10.1/two",
        authors=["Jane Doe"],
    )

    matches = find_possible_version_duplicates(pub_a, [pub_a, pub_b])
    note = format_version_duplicate_note(matches)

    assert "Possible version duplicate of:" in note
    assert "doi:10.1/two" in note
    assert "Jane Doe" in note
