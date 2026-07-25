from datetime import date

from arise_radar.dedupe import group_by_canonical_key
from arise_radar.models import NormalizedPublication
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


def test_single_entry_is_its_own_group() -> None:
    pub = _pub()
    groups = group_by_canonical_key([(pub, KEEP)])

    assert len(groups) == 1
    assert groups[0].publication is pub
    assert groups[0].researcher_names == ["Jane Doe"]
    assert groups[0].decision == KEEP


def test_distinct_canonical_keys_stay_separate() -> None:
    pub_a = _pub(canonical_key="doi:10.1/one")
    pub_b = _pub(canonical_key="doi:10.1/two")

    groups = group_by_canonical_key([(pub_a, KEEP), (pub_b, KEEP)])

    assert [g.publication.canonical_key for g in groups] == ["doi:10.1/one", "doi:10.1/two"]


def test_shared_doi_across_two_researchers_produces_one_group_with_both_names() -> None:
    pub_a = _pub(researcher_id="ethan_goh", researcher_name="Ethan Goh")
    pub_b = _pub(researcher_id="adam_rodman", researcher_name="Adam Rodman")

    groups = group_by_canonical_key([(pub_a, KEEP), (pub_b, KEEP)])

    assert len(groups) == 1
    assert groups[0].researcher_names == ["Adam Rodman", "Ethan Goh"]
    # First-seen record is the representative.
    assert groups[0].publication is pub_a
    assert groups[0].decision == KEEP


def test_same_researcher_appearing_twice_is_not_duplicated() -> None:
    pub = _pub()
    groups = group_by_canonical_key([(pub, KEEP), (pub, KEEP)])

    assert len(groups) == 1
    assert groups[0].researcher_names == ["Jane Doe"]


def test_representative_decision_is_first_seen() -> None:
    pub_a = _pub(researcher_id="ethan_goh", researcher_name="Ethan Goh")
    pub_b = _pub(researcher_id="adam_rodman", researcher_name="Adam Rodman")

    groups = group_by_canonical_key([(pub_a, UNCERTAIN), (pub_b, KEEP)])

    assert groups[0].decision == UNCERTAIN


def test_empty_input_returns_empty_list() -> None:
    assert group_by_canonical_key([]) == []


def test_three_researchers_same_paper_merges_all_three_names() -> None:
    pub_a = _pub(researcher_id="a", researcher_name="Researcher A")
    pub_b = _pub(researcher_id="b", researcher_name="Researcher B")
    pub_c = _pub(researcher_id="c", researcher_name="Researcher C")

    groups = group_by_canonical_key([(pub_a, KEEP), (pub_b, KEEP), (pub_c, KEEP)])

    assert len(groups) == 1
    assert groups[0].researcher_names == ["Researcher A", "Researcher B", "Researcher C"]
