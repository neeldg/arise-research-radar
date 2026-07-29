from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.citations import (
    CITATION_RELATIONSHIP_UNCLASSIFIED,
    SLACK_STATUS_PENDING,
    SLACK_STATUS_SUPPRESSED,
    TrackedWork,
    build_events_for_citing_work,
    chunk_work_ids,
    compute_citation_key,
    discover_citation_edges,
    discover_citation_edges_from_fixture,
)
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError


def _tracked(**overrides: object) -> TrackedWork:
    defaults: dict[str, object] = {
        "openalex_id": "W100",
        "canonical_key": "doi:10.1/tracked",
        "title": "Tracked Paper",
        "researchers": ["Ethan Goh"],
    }
    defaults.update(overrides)
    return TrackedWork(**defaults)


def _citing_work(**overrides: object) -> dict:
    work: dict = {
        "id": "https://openalex.org/W900",
        "display_name": "A Citing Paper",
        "doi": "https://doi.org/10.2/citing",
        "publication_date": "2026-01-15",
        "referenced_works": ["https://openalex.org/W100"],
        "authorships": [{"author": {"display_name": "Someone Else"}}],
    }
    work.update(overrides)
    return work


# --- compute_citation_key -----------------------------------------------------------


def test_compute_citation_key_exact_format() -> None:
    assert compute_citation_key("W900", "W100") == "citation:W900:W100"


def test_compute_citation_key_strips_full_openalex_urls() -> None:
    assert (
        compute_citation_key("https://openalex.org/W900", "https://openalex.org/W100")
        == "citation:W900:W100"
    )


# --- chunk_work_ids -------------------------------------------------------------------


def test_chunk_work_ids_splits_into_batches() -> None:
    ids = [f"W{i}" for i in range(5)]
    assert chunk_work_ids(ids, 2) == [["W0", "W1"], ["W2", "W3"], ["W4"]]


def test_chunk_work_ids_single_batch_when_under_size() -> None:
    ids = ["W1", "W2"]
    assert chunk_work_ids(ids, 50) == [["W1", "W2"]]


def test_chunk_work_ids_empty_input() -> None:
    assert chunk_work_ids([], 50) == []


def test_chunk_work_ids_rejects_non_positive_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        chunk_work_ids(["W1"], 0)


# --- build_events_for_citing_work: the core edge-recovery logic -------------------


def test_one_citing_paper_cites_one_tracked_paper() -> None:
    tracked = {"W100": _tracked(openalex_id="W100")}
    events = build_events_for_citing_work(
        _citing_work(referenced_works=["https://openalex.org/W100"]),
        tracked,
        detected_date=date(2026, 1, 1),
        is_baseline=False,
        slack_status=SLACK_STATUS_PENDING,
    )

    assert len(events) == 1
    event = events[0]
    assert event.citation_key == "citation:W900:W100"
    assert event.citing_openalex_id == "W900"
    assert event.cited_openalex_id == "W100"
    assert event.cited_canonical_key == "doi:10.1/tracked"
    assert event.cited_title == "Tracked Paper"
    assert event.arise_researchers == ["Ethan Goh"]
    assert event.citing_title == "A Citing Paper"
    assert event.citing_doi == "10.2/citing"
    assert event.citing_url == "https://doi.org/10.2/citing"
    assert event.citing_authors == ["Someone Else"]
    assert event.citing_publication_date == date(2026, 1, 15)
    assert event.detected_date == date(2026, 1, 1)
    assert event.is_baseline is False
    assert event.slack_status == SLACK_STATUS_PENDING
    assert event.review_status == "New"
    assert event.citation_relationship == CITATION_RELATIONSHIP_UNCLASSIFIED
    assert event.relationship_evidence == ""


def test_one_citing_paper_cites_multiple_tracked_papers_produces_one_event_each() -> None:
    tracked = {
        "W100": _tracked(openalex_id="W100", canonical_key="doi:10.1/one", title="Tracked One"),
        "W200": _tracked(openalex_id="W200", canonical_key="doi:10.1/two", title="Tracked Two"),
    }
    citing = _citing_work(
        referenced_works=[
            "https://openalex.org/W100",
            "https://openalex.org/W200",
            "https://openalex.org/W999",  # not tracked -- must not produce an event
        ]
    )

    events = build_events_for_citing_work(
        citing, tracked, detected_date=date(2026, 1, 1), is_baseline=False, slack_status="Pending"
    )

    assert len(events) == 2
    cited_ids = {e.cited_openalex_id for e in events}
    assert cited_ids == {"W100", "W200"}
    citation_keys = {e.citation_key for e in events}
    assert citation_keys == {"citation:W900:W100", "citation:W900:W200"}
    # Both events share the same citing-paper metadata.
    assert all(e.citing_openalex_id == "W900" for e in events)


def test_citing_work_referencing_no_tracked_paper_produces_no_events() -> None:
    tracked = {"W100": _tracked(openalex_id="W100")}
    citing = _citing_work(referenced_works=["https://openalex.org/W999"])

    events = build_events_for_citing_work(
        citing, tracked, detected_date=date(2026, 1, 1), is_baseline=False, slack_status="Pending"
    )

    assert events == []


def test_multiple_citing_papers_citing_the_same_tracked_paper() -> None:
    tracked = {"W100": _tracked(openalex_id="W100")}
    citing_a = _citing_work(id="https://openalex.org/W901", display_name="Citing A")
    citing_b = _citing_work(id="https://openalex.org/W902", display_name="Citing B")

    events_a = build_events_for_citing_work(
        citing_a, tracked, detected_date=date(2026, 1, 1), is_baseline=False, slack_status="Pending"
    )
    events_b = build_events_for_citing_work(
        citing_b, tracked, detected_date=date(2026, 1, 1), is_baseline=False, slack_status="Pending"
    )

    assert len(events_a) == 1 and len(events_b) == 1
    assert events_a[0].citation_key == "citation:W901:W100"
    assert events_b[0].citation_key == "citation:W902:W100"
    assert events_a[0].cited_openalex_id == events_b[0].cited_openalex_id == "W100"


def test_citing_work_with_no_doi_falls_back_to_openalex_url() -> None:
    tracked = {"W100": _tracked(openalex_id="W100")}
    citing = _citing_work(doi=None)

    events = build_events_for_citing_work(
        citing, tracked, detected_date=date(2026, 1, 1), is_baseline=False, slack_status="Pending"
    )

    assert events[0].citing_doi is None
    assert events[0].citing_url == "https://openalex.org/W900"


def test_baseline_run_sets_suppressed_and_baseline_true() -> None:
    tracked = {"W100": _tracked(openalex_id="W100")}
    events = build_events_for_citing_work(
        _citing_work(),
        tracked,
        detected_date=date(2026, 1, 1),
        is_baseline=True,
        slack_status=SLACK_STATUS_SUPPRESSED,
    )

    assert events[0].is_baseline is True
    assert events[0].slack_status == SLACK_STATUS_SUPPRESSED


# --- discover_citation_edges_from_fixture -------------------------------------------


def test_discover_from_fixture_dedupes_across_repeated_citing_works() -> None:
    tracked = [_tracked(openalex_id="W100")]
    citing_works = [_citing_work(), _citing_work()]  # same citing work appears twice

    result = discover_citation_edges_from_fixture(
        tracked, citing_works, detected_date=date(2026, 1, 1), is_baseline=False
    )

    assert result.raw_citing_work_matches == 2
    assert len(result.events) == 1  # deduped by citation_key
    assert result.batch_errors == []


def test_discover_from_fixture_empty_citing_works() -> None:
    result = discover_citation_edges_from_fixture(
        [_tracked()], [], detected_date=date(2026, 1, 1), is_baseline=False
    )
    assert result.events == []
    assert result.raw_citing_work_matches == 0
    assert result.batches_queried == 0


# --- discover_citation_edges: batching, pagination, per-batch error isolation -----


def test_discover_citation_edges_batches_by_configured_size(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    seen_filters: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_filters.append(request.url.params["filter"])
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    client = mock_openalex_client(handler)
    tracked = [_tracked(openalex_id=f"W{i}") for i in range(5)]

    result = discover_citation_edges(
        client, tracked, detected_date=date(2026, 1, 1), is_baseline=False, batch_size=2
    )

    assert result.batches_queried == 3
    assert len(seen_filters) == 3
    assert "cites:W0|W1" in seen_filters[0]
    assert "cites:W2|W3" in seen_filters[1]
    assert "cites:W4" in seen_filters[2]


def test_discover_citation_edges_paginates_within_a_batch(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        if cursor == "*":
            return httpx.Response(
                200,
                json={
                    "results": [_citing_work(id="https://openalex.org/W901")],
                    "meta": {"next_cursor": "page2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [_citing_work(id="https://openalex.org/W902")],
                "meta": {"next_cursor": None},
            },
        )

    client = mock_openalex_client(handler)
    tracked = [_tracked(openalex_id="W100")]

    result = discover_citation_edges(
        client, tracked, detected_date=date(2026, 1, 1), is_baseline=False, batch_size=50
    )

    assert seen_cursors == ["*", "page2"]
    assert result.raw_citing_work_matches == 2
    assert len(result.events) == 2


def test_one_failed_batch_does_not_stop_other_batches(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filter_value = request.url.params["filter"]
        if "W0" in filter_value:
            return httpx.Response(500, json={"message": "internal error"})
        return httpx.Response(
            200, json={"results": [_citing_work()], "meta": {"next_cursor": None}}
        )

    client = mock_openalex_client(handler, max_retries=0)
    tracked = [_tracked(openalex_id="W0"), _tracked(openalex_id="W100")]

    result = discover_citation_edges(
        client, tracked, detected_date=date(2026, 1, 1), is_baseline=False, batch_size=1
    )

    assert result.batches_queried == 2
    assert len(result.batch_errors) == 1
    assert len(result.events) == 1  # the surviving batch's edge was still recovered


def test_openalex_error_class_is_the_one_caught_per_batch(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "down"})

    client = mock_openalex_client(handler, max_retries=0)
    tracked = [_tracked(openalex_id="W100")]

    result = discover_citation_edges(
        client, tracked, detected_date=date(2026, 1, 1), is_baseline=False
    )

    assert result.events == []
    assert len(result.batch_errors) == 1
    # confirm this really is the OpenAlexError path, not something silently swallowed elsewhere
    with pytest.raises(OpenAlexError):
        list(client.iter_citing_works_batch(["W100"]))


def test_discover_citation_edges_passes_since_filter(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "from_publication_date:2026-01-01" in request.url.params["filter"]
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    client = mock_openalex_client(handler)
    discover_citation_edges(
        client,
        [_tracked()],
        detected_date=date(2026, 2, 1),
        is_baseline=False,
        since=date(2026, 1, 1),
    )
