from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.run_roster import main

from arise_radar.sources.openalex import OpenAlexClient


def test_main_skips_unverified_and_inactive_then_prints_verified(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: unverified_person
    name: Unverified Person
    openalex_id: null
    active: true
  - id: inactive_person
    name: Inactive Person
    openalex_id: A999
    active: false
  - id: verified_person
    name: Verified Person
    openalex_id: A123
    active: true
"""
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "A123" in request.url.params.get("filter", "")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W111",
                        "display_name": "A Great Paper",
                        "publication_date": "2026-01-01",
                        "doi": "https://doi.org/10.1/xyz",
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )

    client = mock_openalex_client(handler)

    exit_code = main(["--roster", str(roster_path), "--days-back", "90"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Unverified Person" in captured.err
    assert "no verified OpenAlex ID" in captured.err
    assert "Inactive Person" in captured.err
    assert "marked inactive" in captured.err
    assert "A Great Paper" in captured.out
    assert "Verified Person" in captured.out


def test_main_missing_roster_file_returns_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.yaml"
    exit_code = main(["--roster", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "ERROR" in captured.err


def test_main_no_verified_researchers_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: unverified_person
    name: Unverified Person
    openalex_id: null
    active: true
"""
    )

    exit_code = main(["--roster", str(roster_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No verified researchers to query" in captured.err


def _write_ambiguous_researcher_roster(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: jonathan_h_chen
    name: Jonathan H Chen
    openalex_id: A5046725885
    orcid: "0000-0002-4387-8740"
    active: true
    identity_status: ambiguous
    relevance_filter: healthcare_arise
"""
    )
    return roster_path


def _mixed_relevance_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "Clinical decision support with large language models",
                    "publication_date": "2026-01-01",
                    "doi": None,
                    "type": "article",
                    "primary_topic": {
                        "display_name": "Machine Learning in Healthcare",
                        "field": {"display_name": "Medicine"},
                    },
                    "topics": [],
                    "concepts": [],
                    "primary_location": {"source": {"display_name": "npj Digital Medicine"}},
                },
                {
                    "id": "https://openalex.org/W2",
                    "display_name": "Structure-function modulation of pumpkin seed flour",
                    "publication_date": "2026-01-02",
                    "doi": None,
                    "type": "article",
                    "primary_topic": {
                        "display_name": "Food Science",
                        "field": {"display_name": "Agricultural and Biological Sciences"},
                    },
                    "topics": [],
                    "concepts": [],
                    "primary_location": {"source": {"display_name": "Food Chemistry"}},
                },
                {
                    "id": "https://openalex.org/W3",
                    "display_name": "A novel approach to recommendation systems",
                    "publication_date": "2026-01-03",
                    "doi": None,
                    "type": "article",
                    "primary_topic": None,
                    "topics": [],
                    "concepts": [],
                    "primary_location": None,
                },
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_main_applies_relevance_filter_for_ambiguous_researcher(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    roster_path = _write_ambiguous_researcher_roster(tmp_path)
    client = mock_openalex_client(_mixed_relevance_handler)

    exit_code = main(["--roster", str(roster_path), "--days-back", "90"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "no verified OpenAlex ID" not in captured.err
    assert "marked inactive" not in captured.err
    assert "Clinical decision support" in captured.out
    assert "A novel approach to recommendation systems" in captured.out
    assert "[UNCERTAIN" in captured.out
    assert "pumpkin" not in captured.out.lower()
    assert "Kept: 1" in captured.out
    assert "Uncertain but kept: 1" in captured.out
    assert "Clearly unrelated and excluded: 1" in captured.out
    assert "--show-excluded" in captured.out


def test_main_show_excluded_flag_prints_full_exclusion_details(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    roster_path = _write_ambiguous_researcher_roster(tmp_path)
    client = mock_openalex_client(_mixed_relevance_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--days-back", "90", "--show-excluded"],
        client=client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Structure-function modulation of pumpkin seed flour" in captured.out
    assert "Exclusion reason:" in captured.out
    assert "Matched evidence:" in captured.out
    assert "W2" in captured.out
