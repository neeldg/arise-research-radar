from datetime import date

import pytest

from arise_radar.models import NormalizedPublication, Researcher
from arise_radar.relevance import apply_relevance_filter, evaluate_relevance


def _pub(**overrides: object) -> NormalizedPublication:
    defaults: dict[str, object] = {
        "researcher_id": "jonathan_h_chen",
        "researcher_name": "Jonathan H Chen",
        "title": "Untitled",
        "publication_date": date(2026, 1, 1),
        "doi": None,
        "openalex_id": "W1",
        "canonical_key": "openalex:W1",
        "topics": [],
        "concepts": [],
        "venue": None,
        "work_type": "article",
    }
    defaults.update(overrides)
    return NormalizedPublication(**defaults)


def test_clear_clinical_ai_paper_is_kept() -> None:
    pub = _pub(
        title="Clinical decision support with large language models",
        topics=["Machine Learning in Healthcare", "Medicine"],
        venue="npj Digital Medicine",
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "keep"
    assert "clinical decision support" in decision.matched_terms


def test_healthcare_policy_paper_is_kept() -> None:
    pub = _pub(
        title="Reforming health policy for value-based care delivery",
        topics=["Health Policy"],
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "keep"
    assert "health policy" in decision.matched_terms


def test_medical_education_paper_is_kept() -> None:
    pub = _pub(
        title="Using generative AI in medical education curricula",
        topics=["Medical Education"],
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "keep"
    assert "medical education" in decision.matched_terms


def test_food_science_paper_is_excluded() -> None:
    pub = _pub(
        title="Structure-function modulation of protein-rich pumpkin seed flour via "
        "microfluidization processing for plant-based applications",
        topics=["Food Science", "Agricultural and Biological Sciences"],
        venue="Food Chemistry",
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "exclude"
    assert "pumpkin" in decision.matched_terms
    assert "food science" in decision.matched_terms


def test_telecommunications_paper_is_excluded() -> None:
    pub = _pub(
        title="5G network optimization for telecommunications providers",
        topics=["Telecommunications"],
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "exclude"
    assert "telecommunications" in decision.matched_terms


def test_ambiguous_interdisciplinary_paper_is_uncertain_and_kept() -> None:
    pub = _pub(
        title="A novel approach to recommendation systems",
        topics=["Machine Learning"],
    )
    decision = evaluate_relevance(pub)
    assert decision.status == "uncertain"
    assert decision.matched_terms == []


def test_missing_topic_metadata_is_uncertain_and_kept() -> None:
    pub = _pub(title="Untitled", topics=[], concepts=[], venue=None, work_type=None)
    decision = evaluate_relevance(pub)
    assert decision.status == "uncertain"


def test_filter_failure_is_kept_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _raise(_publication: NormalizedPublication) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("arise_radar.relevance._classify", _raise)

    pub = _pub(title="Clinical decision support with large language models")
    decision = evaluate_relevance(pub)

    assert decision.status == "uncertain"
    assert "boom" in decision.reason
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "boom" in captured.err


def test_relevance_filter_none_bypasses_filtering_completely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_publication: NormalizedPublication) -> None:
        raise AssertionError("classify should never be called when relevance_filter is 'none'")

    monkeypatch.setattr("arise_radar.relevance._classify", _raise)

    researcher = Researcher(id="ethan_goh", name="Ethan Goh", openalex_id="A1")
    pub = _pub(researcher_id="ethan_goh", researcher_name="Ethan Goh", title="Pumpkin seed flour")

    results = apply_relevance_filter(researcher, [pub])

    assert len(results) == 1
    kept_pub, decision = results[0]
    assert kept_pub is pub
    assert decision.status == "keep"
    assert "not enabled" in decision.reason


def test_relevance_filter_applied_when_enabled() -> None:
    researcher = Researcher(
        id="jonathan_h_chen",
        name="Jonathan H Chen",
        openalex_id="A5046725885",
        relevance_filter="healthcare_arise",
    )
    healthy = _pub(openalex_id="W1", title="Clinical decision support tool", topics=["Medicine"])
    food = _pub(
        openalex_id="W2",
        title="Pumpkin seed flour microfluidization",
        topics=["Food Science"],
    )

    results = apply_relevance_filter(researcher, [healthy, food])
    statuses = {pub.openalex_id: decision.status for pub, decision in results}

    assert statuses == {"W1": "keep", "W2": "exclude"}
