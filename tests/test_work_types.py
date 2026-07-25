from datetime import date

from arise_radar.models import NormalizedPublication
from arise_radar.work_types import DRAFT_ELIGIBLE_CATEGORIES, classify_work_type


def _pub(**overrides: object) -> NormalizedPublication:
    defaults: dict[str, object] = {
        "researcher_id": "jane_doe",
        "researcher_name": "Jane Doe",
        "title": "A Great Paper",
        "publication_date": date(2026, 1, 1),
        "doi": "10.1000/abc",
        "openalex_id": "W1",
        "canonical_key": "doi:10.1000/abc",
        "work_type": "article",
    }
    defaults.update(overrides)
    return NormalizedPublication(**defaults)


# --- the two examples from the roster dry-run findings --------------------------


def test_zenodo_repository_is_retained_but_not_draft_eligible() -> None:
    pub = _pub(
        title="Stanford Biodesign Digital Health Group",
        doi="10.5281/zenodo.21358702",
        work_type=None,
        venue=None,
    )

    result = classify_work_type(pub)

    assert result.category == "dataset/repository"
    assert result.draft_eligible is False
    assert result.reason


def test_osf_registration_is_retained_but_not_draft_eligible() -> None:
    pub = _pub(
        title="GP AI Skin Cancer Portal Study",
        doi="10.17605/osf.io/evxkq",
        work_type=None,
        venue=None,
    )

    result = classify_work_type(pub)

    assert result.category == "protocol/registration"
    assert result.draft_eligible is False
    assert result.reason


# --- draft-eligible categories ----------------------------------------------------


def test_article_is_draft_eligible() -> None:
    result = classify_work_type(_pub(work_type="article"))
    assert result.category == "article"
    assert result.draft_eligible is True


def test_review_maps_to_article() -> None:
    result = classify_work_type(_pub(work_type="review"))
    assert result.category == "article"
    assert result.draft_eligible is True


def test_preprint_is_draft_eligible() -> None:
    result = classify_work_type(_pub(work_type="preprint", doi="10.1101/2026.01.01.12345"))
    assert result.category == "preprint"
    assert result.draft_eligible is True


def test_proceedings_article_maps_to_conference() -> None:
    result = classify_work_type(_pub(work_type="proceedings-article"))
    assert result.category == "conference"
    assert result.draft_eligible is True


def test_editorial_maps_to_editorial_viewpoint() -> None:
    result = classify_work_type(_pub(work_type="editorial"))
    assert result.category == "editorial/viewpoint"
    assert result.draft_eligible is True


def test_letter_maps_to_editorial_viewpoint() -> None:
    result = classify_work_type(_pub(work_type="letter"))
    assert result.category == "editorial/viewpoint"
    assert result.draft_eligible is True


# --- non-eligible categories --------------------------------------------------------


def test_dataset_type_maps_to_dataset_repository() -> None:
    result = classify_work_type(_pub(work_type="dataset", doi="10.1000/some-dataset"))
    assert result.category == "dataset/repository"
    assert result.draft_eligible is False


def test_protocol_keyword_in_title_overrides_preprint_type() -> None:
    result = classify_work_type(
        _pub(title="Study Protocol for the XYZ Randomized Trial", work_type="preprint")
    )
    assert result.category == "protocol/registration"
    assert result.draft_eligible is False


def test_dataset_keyword_in_venue_is_detected() -> None:
    result = classify_work_type(
        _pub(title="Some Archive Entry", venue="Zenodo", work_type=None, doi=None)
    )
    assert result.category == "dataset/repository"
    assert result.draft_eligible is False


def test_unrecognized_type_and_no_hints_is_unknown() -> None:
    result = classify_work_type(
        _pub(title="Something Ambiguous", work_type="paratext", venue=None, doi=None)
    )
    assert result.category == "unknown"
    assert result.draft_eligible is False


def test_missing_type_and_no_hints_is_unknown() -> None:
    result = classify_work_type(_pub(title="Untitled Record", work_type=None, venue=None, doi=None))
    assert result.category == "unknown"
    assert result.draft_eligible is False


# --- fail-open: classification never raises, unknown is still imported ------------


def test_classify_never_raises_on_empty_publication_fields() -> None:
    pub = _pub(title="", work_type=None, venue=None, doi=None)
    result = classify_work_type(pub)
    assert result.category == "unknown"


def test_draft_eligible_categories_matches_documented_set() -> None:
    assert DRAFT_ELIGIBLE_CATEGORIES == {
        "article",
        "preprint",
        "conference",
        "editorial/viewpoint",
    }
