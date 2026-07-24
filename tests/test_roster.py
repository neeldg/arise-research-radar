from pathlib import Path

import pytest

from arise_radar.roster import RosterError, load_roster, load_seed_roster

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_load_roster_parses_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "roster.yaml"
    path.write_text(
        """
researchers:
  - id: jane_doe
    name: Jane Doe
    openalex_id: A123
    orcid: null
    aliases: ["Jane Doe", "J. Doe"]
    active: true
"""
    )
    researchers = load_roster(path)
    assert len(researchers) == 1
    assert researchers[0].id == "jane_doe"
    assert researchers[0].aliases == ["Jane Doe", "J. Doe"]


def test_load_roster_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RosterError):
        load_roster(tmp_path / "missing.yaml")


def test_load_roster_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "roster.yaml"
    path.write_text("researchers: [invalid: [")
    with pytest.raises(RosterError):
        load_roster(path)


def test_load_roster_validation_error_raises(tmp_path: Path) -> None:
    path = tmp_path / "roster.yaml"
    path.write_text(
        """
researchers:
  - name: Missing Id
"""
    )
    with pytest.raises(RosterError):
        load_roster(path)


def test_load_seed_roster_parses_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "seed.yaml"
    path.write_text(
        """
researchers:
  - id: jane_doe
    name: Jane Doe
    openalex_id: null
    verification_status: unverified
"""
    )
    researchers = load_seed_roster(path)
    assert researchers[0].verification_status == "unverified"


def test_example_roster_config_is_valid() -> None:
    example = load_roster(REPO_ROOT / "config" / "roster.example.yaml")
    assert len(example) == 1
    assert example[0].openalex_id == "A0000000000"


def test_seed_roster_config_is_valid_and_keeps_both_david_wus() -> None:
    seed = load_seed_roster(REPO_ROOT / "config" / "roster_seed.yaml")
    ids = [r.id for r in seed]

    assert len(ids) == len(set(ids)), "seed roster ids must be unique"
    assert "david_wu" in ids
    assert "david_jh_wu" in ids
    assert all(r.openalex_id is None for r in seed)
    assert all(r.verification_status == "unverified" for r in seed)
