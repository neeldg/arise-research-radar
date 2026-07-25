from collections.abc import Callable
from datetime import date

import httpx
import pytest

from arise_radar.models import Researcher
from arise_radar.sources.openalex import (
    OpenAlexClient,
    OpenAlexError,
    fetch_researcher_publications,
    reconstruct_abstract,
)


def test_normalizes_single_page_of_works(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id="A1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "author.id:A1" in request.url.params.get("filter", "")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Paper One",
                        "publication_date": "2026-02-01",
                        "doi": "https://doi.org/10.1000/ABC",
                    },
                    {
                        "id": "https://openalex.org/W2",
                        "display_name": "Paper Two",
                        "publication_date": None,
                        "doi": None,
                    },
                ],
                "meta": {"next_cursor": None},
            },
        )

    client = mock_openalex_client(handler)
    publications = fetch_researcher_publications(
        client, researcher, days_back=90, today=date(2026, 3, 1)
    )

    assert len(publications) == 2
    first, second = publications
    assert first.doi == "10.1000/abc"
    assert first.canonical_key == "doi:10.1000/abc"
    assert first.openalex_id == "W1"
    assert second.doi is None
    assert second.canonical_key == "openalex:W2"
    assert second.publication_date is None


def test_paginates_with_cursor(mock_openalex_client: Callable[..., OpenAlexClient]) -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id="A1")
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        seen_cursors.append(cursor)
        if cursor == "*":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "display_name": "P1",
                            "publication_date": "2026-01-01",
                            "doi": None,
                        }
                    ],
                    "meta": {"next_cursor": "page2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W2",
                        "display_name": "P2",
                        "publication_date": "2026-01-02",
                        "doi": None,
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )

    client = mock_openalex_client(handler)
    publications = fetch_researcher_publications(
        client, researcher, days_back=90, today=date(2026, 3, 1)
    )

    assert seen_cursors == ["*", "page2"]
    assert [p.openalex_id for p in publications] == ["W1", "W2"]


def test_retries_on_transient_failure_then_succeeds(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id="A1")
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(503, json={"error": "try again"})
        return httpx.Response(200, json={"results": [], "meta": {"next_cursor": None}})

    client = mock_openalex_client(handler, max_retries=3)
    publications = fetch_researcher_publications(
        client, researcher, days_back=90, today=date(2026, 3, 1)
    )

    assert publications == []
    assert attempts["count"] == 3


def test_raises_after_exhausting_retries(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id="A1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    client = mock_openalex_client(handler, max_retries=2)

    with pytest.raises(OpenAlexError):
        fetch_researcher_publications(client, researcher, days_back=90, today=date(2026, 3, 1))


def test_missing_openalex_id_raises_without_request(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    researcher = Researcher(id="no_id", name="No Id", openalex_id=None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called when openalex_id is missing")

    client = mock_openalex_client(handler)

    with pytest.raises(ValueError):
        fetch_researcher_publications(client, researcher, days_back=90)


# --- get_work / reconstruct_abstract --------------------------------------------


def test_get_work_fetches_single_work_by_id(
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/works/W123"
        return httpx.Response(
            200, json={"id": "https://openalex.org/W123", "display_name": "A Paper"}
        )

    client = mock_openalex_client(handler)
    work = client.get_work("W123")

    assert work["display_name"] == "A Paper"


def test_reconstruct_abstract_decodes_inverted_index() -> None:
    work = {
        "abstract_inverted_index": {
            "This": [0],
            "is": [1],
            "a": [2],
            "test": [3],
        }
    }
    assert reconstruct_abstract(work) == "This is a test"


def test_reconstruct_abstract_handles_repeated_words() -> None:
    work = {"abstract_inverted_index": {"the": [0, 3], "cat": [1], "sat": [2], "mat": [4]}}
    assert reconstruct_abstract(work) == "the cat sat the mat"


def test_reconstruct_abstract_missing_returns_none() -> None:
    assert reconstruct_abstract({}) is None
    assert reconstruct_abstract({"abstract_inverted_index": None}) is None
