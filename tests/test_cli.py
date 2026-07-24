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
