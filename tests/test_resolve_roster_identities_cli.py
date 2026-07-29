import csv
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.resolve_roster_identities import main

from arise_radar.sources.openalex import OpenAlexClient


def _write_roster(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A123
    active: true
    identity_status: verified
  - id: david_wu
    name: David Wu
    openalex_id: null
    active: false
    identity_status: unverified
    institution: Harvard University
    role: Clinical AI Evaluation Lead
  - id: david_jh_wu
    name: David JH Wu
    openalex_id: null
    active: false
    identity_status: unverified
    institution: Stanford University
  - id: laura_wegner
    name: Laura Wegner
    openalex_id: null
    active: false
    identity_status: unverified
    institution: University of Oxford
"""
    )
    return roster_path


def _write_fixture(tmp_path: Path) -> Path:
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "david_wu": {
                    "candidates": [
                        {
                            "id": "https://openalex.org/A5011111111",
                            "display_name": "David Wu",
                            "orcid": "https://orcid.org/0000-0001-1111-1111",
                            "works_count": 45,
                            "cited_by_count": 620,
                            "last_known_institutions": [{"display_name": "Harvard University"}],
                            "affiliations": [
                                {
                                    "institution": {"display_name": "Harvard University"},
                                    "years": [2024, 2025],
                                }
                            ],
                            "topics": [{"display_name": "Clinical decision support"}],
                            "recent_works": [],
                        }
                    ]
                },
                "david_jh_wu": {
                    "candidates": [
                        {
                            "id": "https://openalex.org/A5033333333",
                            "display_name": "David J.H. Wu",
                            "orcid": None,
                            "works_count": 12,
                            "cited_by_count": 80,
                            "last_known_institutions": [{"display_name": "Stanford University"}],
                            "affiliations": [
                                {
                                    "institution": {"display_name": "Stanford University"},
                                    "years": [2024],
                                }
                            ],
                            "topics": [],
                            "recent_works": [],
                        }
                    ]
                },
                "laura_wegner": {"candidates": []},
            }
        )
    )
    return fixture_path


# --- flag validation ----------------------------------------------------------------


def test_requires_exactly_one_of_fixture_file_or_live_openalex(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    exit_code = main(["--roster", str(roster_path), "--output-dir", str(tmp_path / "out")])
    assert exit_code != 0


def test_fixture_file_and_live_openalex_together_is_rejected(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--fixture-file",
            str(fixture_path),
            "--live-openalex",
        ]
    )
    assert exit_code != 0


# --- intake filter: active:false + identity_status:unverified only ----------------


def test_only_reads_inactive_unverified_researchers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(output_dir),
            "--fixture-file",
            str(fixture_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Resolved 3 researcher(s)" in captured.out

    rows = json.loads((output_dir / "identity_candidates.json").read_text())
    researcher_ids = {row["researcher_id"] for row in rows}
    assert researcher_ids == {"david_wu", "david_jh_wu", "laura_wegner"}
    assert "ethan_goh" not in researcher_ids


def test_researcher_id_targeting_an_active_verified_record_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--fixture-file",
            str(fixture_path),
            "--researcher-id",
            "ethan_goh",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not pending review" in captured.err


def test_unknown_researcher_id_returns_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--fixture-file",
            str(fixture_path),
            "--researcher-id",
            "nonexistent",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no researcher with id" in captured.err


# --- --researcher-id / --limit -------------------------------------------------------


def test_researcher_id_targets_only_david_wu_not_david_jh_wu(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(output_dir),
            "--fixture-file",
            str(fixture_path),
            "--researcher-id",
            "david_wu",
        ]
    )

    assert exit_code == 0
    rows = json.loads((output_dir / "identity_candidates.json").read_text())
    assert {row["researcher_id"] for row in rows} == {"david_wu"}
    assert rows[0]["candidate_openalex_id"] == "A5011111111"


def test_limit_caps_number_of_researchers_resolved(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(output_dir),
            "--fixture-file",
            str(fixture_path),
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    rows = json.loads((output_dir / "identity_candidates.json").read_text())
    assert len({row["researcher_id"] for row in rows}) == 1


# --- output reports -------------------------------------------------------------------


def test_writes_csv_and_json_reports_with_expected_columns(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(output_dir),
            "--fixture-file",
            str(fixture_path),
        ]
    )

    assert exit_code == 0
    csv_path = output_dir / "identity_candidates.csv"
    json_path = output_dir / "identity_candidates.json"
    assert csv_path.exists()
    assert json_path.exists()

    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert "candidate_openalex_id" in reader.fieldnames
    assert "confidence" in reader.fieldnames
    assert "ambiguity_notes" in reader.fieldnames
    assert any(row["researcher_id"] == "david_wu" for row in rows)

    json_rows = json.loads(json_path.read_text())
    laura = next(row for row in json_rows if row["researcher_id"] == "laura_wegner")
    assert laura["confidence"] == "none"
    assert "no candidates found" in laura["ambiguity_notes"][0]


def test_never_modifies_the_roster_file(tmp_path: Path) -> None:
    roster_path = _write_roster(tmp_path)
    fixture_path = _write_fixture(tmp_path)
    before = roster_path.read_text()

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--fixture-file",
            str(fixture_path),
        ]
    )

    assert exit_code == 0
    assert roster_path.read_text() == before


# --- one failed researcher does not stop the batch ----------------------------------


def test_one_failed_researcher_does_not_stop_the_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    roster_path = _write_roster(tmp_path)
    output_dir = tmp_path / "out"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/authors":
            query = request.url.params.get("search", "")
            if query.startswith("David Wu"):
                return httpx.Response(500, json={"message": "internal error"})
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected request to {request.url.path}")

    client = mock_openalex_client(handler, max_retries=0)

    exit_code = main(
        [
            "--roster",
            str(roster_path),
            "--output-dir",
            str(output_dir),
            "--live-openalex",
        ],
        openalex_client=client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "David Wu" in captured.err
    assert "1 researcher(s) failed" in captured.out

    rows = json.loads((output_dir / "identity_candidates.json").read_text())
    researcher_ids = {row["researcher_id"] for row in rows}
    # The other two unverified researchers still got processed and reported.
    assert researcher_ids == {"david_wu", "david_jh_wu", "laura_wegner"}
    david_wu_row = next(row for row in rows if row["researcher_id"] == "david_wu")
    assert david_wu_row["confidence"] == "none"
    assert "query failed" in david_wu_row["ambiguity_notes"][0]
