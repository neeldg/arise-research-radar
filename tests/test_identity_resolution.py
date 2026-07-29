import json
from collections.abc import Callable
from pathlib import Path

import httpx

from arise_radar.identity_resolution import (
    CSV_FIELDNAMES,
    FixtureSource,
    LiveOpenAlexSource,
    affiliation_match,
    build_author_search_query,
    build_candidate_rows,
    compute_confidence,
    name_similarity,
    resolve_researcher,
    select_researchers_pending_review,
    topic_match,
    write_csv_report,
    write_json_report,
)
from arise_radar.models import Researcher
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError


def _researcher(**overrides: object) -> Researcher:
    defaults: dict[str, object] = {
        "id": "david_wu",
        "name": "David Wu",
        "openalex_id": None,
        "active": False,
        "identity_status": "unverified",
        "aliases": ["David Wu"],
        "institution": "Harvard University",
    }
    defaults.update(overrides)
    return Researcher(**defaults)


def _candidate(**overrides: object) -> dict:
    candidate: dict = {
        "id": "https://openalex.org/A5011111111",
        "display_name": "David Wu",
        "orcid": "https://orcid.org/0000-0001-1111-1111",
        "works_count": 45,
        "cited_by_count": 620,
        "last_known_institutions": [{"display_name": "Harvard University"}],
        "affiliations": [
            {"institution": {"display_name": "Harvard University"}, "years": [2024, 2025]}
        ],
        "topics": [{"display_name": "Clinical decision support"}],
    }
    candidate.update(overrides)
    return candidate


# --- select_researchers_pending_review ---------------------------------------------


def test_select_researchers_pending_review_requires_both_inactive_and_unverified() -> None:
    active_verified = Researcher(id="a", name="A", active=True, identity_status="verified")
    active_ambiguous = Researcher(id="b", name="B", active=True, identity_status="ambiguous")
    inactive_verified = Researcher(id="c", name="C", active=False, identity_status="verified")
    active_unverified = Researcher(id="d", name="D", active=True, identity_status="unverified")
    pending = Researcher(id="e", name="E", active=False, identity_status="unverified")

    result = select_researchers_pending_review(
        [active_verified, active_ambiguous, inactive_verified, active_unverified, pending]
    )

    assert result == [pending]


# --- build_author_search_query ------------------------------------------------------


def test_query_includes_name_institution_and_role_when_present() -> None:
    researcher = _researcher(institution="Harvard University", role="Clinical AI Evaluation Lead")
    query = build_author_search_query(researcher)
    assert query == "David Wu Harvard University Clinical AI Evaluation Lead"


def test_query_omits_role_when_absent() -> None:
    researcher = _researcher(institution="Harvard University", role=None)
    query = build_author_search_query(researcher)
    assert query == "David Wu Harvard University"


def test_query_is_name_only_when_no_institution_or_role() -> None:
    researcher = _researcher(institution=None, role=None)
    assert build_author_search_query(researcher) == "David Wu"


# --- name_similarity / affiliation_match / topic_match / compute_confidence -------


def test_name_similarity_exact_match_is_one() -> None:
    assert name_similarity("David Wu", "David Wu") == 1.0


def test_name_similarity_case_and_whitespace_insensitive() -> None:
    assert name_similarity("David Wu", "  david   wu ") == 1.0


def test_name_similarity_different_names_is_low() -> None:
    assert name_similarity("David Wu", "Samantha Wang") < 0.5


def test_affiliation_match_true_on_substring() -> None:
    researcher = _researcher(institution="Harvard University")
    assert affiliation_match(researcher, ["Harvard University"]) is True


def test_affiliation_match_false_without_roster_institution() -> None:
    researcher = _researcher(institution=None)
    assert affiliation_match(researcher, ["Harvard University"]) is False


def test_affiliation_match_false_on_no_overlap() -> None:
    researcher = _researcher(institution="Harvard University")
    assert affiliation_match(researcher, ["University of Toronto"]) is False


def test_topic_match_true_for_clinical_term() -> None:
    matched, terms = topic_match(["Clinical decision support"])
    assert matched is True
    assert "clinical" in terms


def test_topic_match_false_for_unrelated_topics() -> None:
    matched, terms = topic_match(["Semiconductor physics"])
    assert matched is False
    assert terms == []


def test_compute_confidence_high_requires_similarity_and_affiliation() -> None:
    assert compute_confidence(0.95, True, False) == "high"
    assert compute_confidence(0.95, False, False) == "medium"


def test_compute_confidence_medium_on_moderate_similarity_with_affiliation() -> None:
    assert compute_confidence(0.65, True, False) == "medium"


def test_compute_confidence_low_otherwise() -> None:
    assert compute_confidence(0.4, False, False) == "low"


# --- build_candidate_rows -----------------------------------------------------------


def test_no_candidates_produces_one_none_confidence_row() -> None:
    rows = build_candidate_rows(_researcher(), [])
    assert len(rows) == 1
    row = rows[0]
    assert row.confidence == "none"
    assert row.researcher_id == "david_wu"
    assert row.researcher_institution == "Harvard University"
    assert "no candidates found" in row.ambiguity_notes[0]


def test_clean_high_confidence_candidate_row_fields() -> None:
    candidate = _candidate()
    recent_works = [
        {"display_name": "Evaluating LLMs for Clinical AI", "publication_date": "2026-01-01"}
    ]

    rows = build_candidate_rows(_researcher(), [(candidate, recent_works)])

    assert len(rows) == 1
    row = rows[0]
    assert row.candidate_display_name == "David Wu"
    assert row.candidate_openalex_id == "A5011111111"
    assert row.candidate_orcid == "https://orcid.org/0000-0001-1111-1111"
    assert row.current_affiliation == "Harvard University"
    assert row.recent_affiliations == ["Harvard University"]
    assert row.works_count == 45
    assert row.cited_by_count == 620
    assert row.recent_works == ["Evaluating LLMs for Clinical AI (2026)"]
    assert row.topics == ["Clinical decision support"]
    assert row.name_similarity == 1.0
    assert row.affiliation_match is True
    assert row.topic_match is True
    assert row.confidence == "high"
    assert row.ambiguity_notes == []


def test_common_name_flag_applied_to_all_strongly_matching_candidates() -> None:
    same_name_a = _candidate(id="https://openalex.org/A1", display_name="David Wu")
    same_name_b = _candidate(id="https://openalex.org/A2", display_name="David Wu")

    rows = build_candidate_rows(_researcher(), [(same_name_a, []), (same_name_b, [])])

    assert len(rows) == 2
    assert all(any("common name" in note for note in row.ambiguity_notes) for row in rows)


def test_conflicting_affiliation_flag_when_no_overlap() -> None:
    candidate = _candidate(
        last_known_institutions=[{"display_name": "University of Toronto"}],
        affiliations=[{"institution": {"display_name": "University of Toronto"}, "years": [2010]}],
    )

    rows = build_candidate_rows(_researcher(institution="Harvard University"), [(candidate, [])])

    assert len(rows) == 1
    row = rows[0]
    assert row.affiliation_match is False
    assert any("conflicting affiliation" in note for note in row.ambiguity_notes)
    assert "University of Toronto" in row.ambiguity_notes[0]
    assert "Harvard University" in row.ambiguity_notes[0]


def test_no_conflicting_affiliation_note_when_candidate_has_no_affiliation_data() -> None:
    candidate = _candidate(last_known_institutions=[], affiliations=[])
    rows = build_candidate_rows(_researcher(), [(candidate, [])])
    assert rows[0].ambiguity_notes == []


# --- David Wu vs David JH Wu stay separate ------------------------------------------


def test_david_wu_and_david_jh_wu_use_independent_fixture_candidates() -> None:
    fixture = {
        "david_wu": {"candidates": [_candidate(display_name="David Wu")]},
        "david_jh_wu": {
            "candidates": [
                _candidate(id="https://openalex.org/A5033333333", display_name="David J.H. Wu")
            ]
        },
    }
    source = FixtureSource(fixture)

    david_wu_pairs = source.candidates_for(_researcher(id="david_wu", name="David Wu"))
    david_jh_wu_pairs = source.candidates_for(
        _researcher(id="david_jh_wu", name="David JH Wu", institution="Stanford University")
    )

    assert len(david_wu_pairs) == 1
    assert len(david_jh_wu_pairs) == 1
    assert david_wu_pairs[0][0]["display_name"] == "David Wu"
    assert david_jh_wu_pairs[0][0]["display_name"] == "David J.H. Wu"
    assert david_wu_pairs[0][0]["id"] != david_jh_wu_pairs[0][0]["id"]


# --- resolve_researcher: errors never crash, one failure is isolated --------------


class _FailingSource:
    def candidates_for(self, researcher: Researcher) -> list[tuple[dict, list[dict]]]:
        raise OpenAlexError("boom")


def test_resolve_researcher_reports_source_failure_without_raising() -> None:
    rows, error = resolve_researcher(_researcher(), _FailingSource())

    assert error == "boom"
    assert len(rows) == 1
    assert rows[0].confidence == "none"
    assert "query failed" in rows[0].ambiguity_notes[0]


def test_resolve_researcher_success_returns_no_error() -> None:
    source = FixtureSource({"david_wu": {"candidates": [_candidate()]}})
    rows, error = resolve_researcher(_researcher(), source)

    assert error is None
    assert len(rows) == 1


# --- FixtureSource.from_file --------------------------------------------------------


def test_fixture_source_from_file_loads_json(tmp_path: Path) -> None:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({"david_wu": {"candidates": [_candidate()]}}))

    source = FixtureSource.from_file(fixture_path)
    pairs = source.candidates_for(_researcher())

    assert len(pairs) == 1
    assert pairs[0][0]["display_name"] == "David Wu"


def test_example_fixture_file_is_valid_and_keeps_david_wus_separate() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    example_path = repo_root / "config" / "identity_resolution_fixture.example.json"
    source = FixtureSource.from_file(example_path)

    david_wu_pairs = source.candidates_for(_researcher(id="david_wu", name="David Wu"))
    david_jh_wu_pairs = source.candidates_for(
        _researcher(id="david_jh_wu", name="David JH Wu", institution="Stanford University")
    )

    assert len(david_wu_pairs) == 2
    assert len(david_jh_wu_pairs) == 1
    assert {pair[0]["id"] for pair in david_wu_pairs}.isdisjoint(
        {pair[0]["id"] for pair in david_jh_wu_pairs}
    )


# --- LiveOpenAlexSource: mocked transport only, no live calls -----------------------


def test_live_source_searches_then_fetches_recent_works_per_candidate(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        if request.url.path == "/authors":
            assert request.url.params.get("search") == "David Wu Harvard University"
            return httpx.Response(200, json={"results": [_candidate()]})
        if request.url.path == "/works":
            assert request.url.params.get("filter") == "author.id:A5011111111"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"display_name": "A Paper", "publication_date": "2026-01-01"},
                    ]
                },
            )
        raise AssertionError(f"unexpected request to {request.url.path}")

    client = mock_openalex_client(handler)
    source = LiveOpenAlexSource(client)

    pairs = source.candidates_for(_researcher())

    assert requests_seen == ["/authors", "/works"]
    assert len(pairs) == 1
    candidate, recent_works = pairs[0]
    assert candidate["display_name"] == "David Wu"
    assert recent_works[0]["display_name"] == "A Paper"


def test_live_source_handles_zero_candidates(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authors":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("must not fetch recent works when there are no candidates")

    client = mock_openalex_client(handler)
    source = LiveOpenAlexSource(client)

    assert source.candidates_for(_researcher()) == []


# --- CSV / JSON report writers -------------------------------------------------------


def test_write_csv_report_includes_all_required_columns(tmp_path: Path) -> None:
    rows = build_candidate_rows(_researcher(), [(_candidate(), [])])
    csv_path = tmp_path / "identity_candidates.csv"

    write_csv_report(rows, csv_path)

    content = csv_path.read_text()
    header = content.splitlines()[0]
    for field in CSV_FIELDNAMES:
        assert field in header
    assert "David Wu" in content
    assert "A5011111111" in content


def test_write_json_report_round_trips_all_fields(tmp_path: Path) -> None:
    rows = build_candidate_rows(_researcher(), [(_candidate(), [])])
    json_path = tmp_path / "identity_candidates.json"

    write_json_report(rows, json_path)

    loaded = json.loads(json_path.read_text())
    assert len(loaded) == 1
    assert loaded[0]["candidate_openalex_id"] == "A5011111111"
    assert loaded[0]["confidence"] == "high"
    assert loaded[0]["recent_affiliations"] == ["Harvard University"]
