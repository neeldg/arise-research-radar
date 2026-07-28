from pathlib import Path

import pytest

from arise_radar.roster import (
    RosterError,
    active_verified_researchers,
    load_roster,
    load_seed_roster,
    unverified_researchers,
)

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


# --- master roster (config/roster.yaml): 15 active verified + 49 unverified -------

MASTER_ROSTER_PATH = REPO_ROOT / "config" / "roster.yaml"

# The 15 pre-existing active/verified records and their OpenAlex IDs, exactly
# as they were before the 49 new network-wide entries were added. Used to
# assert nothing about them changed.
EXPECTED_ACTIVE_VERIFIED_OPENALEX_IDS: dict[str, str] = {
    "ethan_goh": "A5103153068",
    "jonathan_h_chen": "A5046725885",
    "adam_rodman": "A5045324950",
    "arjun_manrai": "A5036448016",
    "eric_horvitz": "A5043228682",
    "neera_ahuja": "A5041327310",
    "vishnu_ravi": "A5077342977",
    "jason_hom": "A5061093158",
    "chase_walton": "A5073943724",
    "fateme_nateghi": "A5056466429",
    "peter_brodeur": "A5015726003",
    "kathleen_lacar": "A5093379071",
    "emily_tat": "A5102895001",
    "liam_mccoy": "A5082108649",
    "laura_zwaan": "A5041919544",
}


def test_master_roster_has_64_unique_people() -> None:
    researchers = load_roster(MASTER_ROSTER_PATH)
    ids = [r.id for r in researchers]
    names = [r.name for r in researchers]

    assert len(researchers) == 64
    assert len(ids) == len(set(ids)), "duplicate ids in master roster"
    assert len(names) == len(set(names)), "duplicate names in master roster"


def test_master_roster_has_15_active_verified_people() -> None:
    researchers = load_roster(MASTER_ROSTER_PATH)
    active_verified = active_verified_researchers(researchers)

    assert len(active_verified) == 15
    assert {r.id for r in active_verified} == set(EXPECTED_ACTIVE_VERIFIED_OPENALEX_IDS)
    assert all(r.active for r in active_verified)
    assert all(r.identity_status != "unverified" for r in active_verified)


def test_master_roster_has_49_inactive_unverified_people() -> None:
    researchers = load_roster(MASTER_ROSTER_PATH)
    unverified = unverified_researchers(researchers)

    assert len(unverified) == 49
    assert all(not r.active for r in unverified)
    assert all(r.identity_status == "unverified" for r in unverified)
    assert all(r.openalex_id is None for r in unverified)
    assert all(r.orcid is None for r in unverified)
    # No alias, affiliation, or filter invented beyond the exact full name.
    assert all(r.aliases == [r.name] for r in unverified)
    assert all(r.relevance_filter == "none" for r in unverified)


def test_active_verified_and_unverified_partition_the_master_roster() -> None:
    researchers = load_roster(MASTER_ROSTER_PATH)
    active_verified = active_verified_researchers(researchers)
    unverified = unverified_researchers(researchers)

    active_ids = {r.id for r in active_verified}
    unverified_ids = {r.id for r in unverified}
    assert active_ids.isdisjoint(unverified_ids)
    assert len(active_verified) + len(unverified) == len(researchers)


def test_david_wu_and_david_jh_wu_remain_distinct() -> None:
    by_name = {r.name: r for r in load_roster(MASTER_ROSTER_PATH)}
    david_wu = by_name["David Wu"]
    david_jh_wu = by_name["David JH Wu"]

    assert david_wu.id == "david_wu"
    assert david_jh_wu.id == "david_jh_wu"
    assert david_wu.id != david_jh_wu.id
    assert not david_wu.active
    assert not david_jh_wu.active


def test_laura_wegner_and_laura_zwaan_remain_distinct() -> None:
    by_name = {r.name: r for r in load_roster(MASTER_ROSTER_PATH)}
    laura_wegner = by_name["Laura Wegner"]
    laura_zwaan = by_name["Laura Zwaan"]

    assert laura_wegner.id != laura_zwaan.id
    assert laura_zwaan.active and laura_zwaan.identity_status == "verified"
    assert not laura_wegner.active and laura_wegner.identity_status == "unverified"


def test_jason_hom_and_jason_freed_remain_distinct() -> None:
    by_name = {r.name: r for r in load_roster(MASTER_ROSTER_PATH)}
    jason_hom = by_name["Jason Hom"]
    jason_freed = by_name["Jason Freed"]

    assert jason_hom.id != jason_freed.id
    assert jason_hom.active and jason_hom.identity_status == "verified"
    assert not jason_freed.active and jason_freed.identity_status == "unverified"


def test_no_existing_verified_record_changed() -> None:
    by_id = {r.id: r for r in load_roster(MASTER_ROSTER_PATH)}

    for researcher_id, expected_openalex_id in EXPECTED_ACTIVE_VERIFIED_OPENALEX_IDS.items():
        record = by_id[researcher_id]
        assert record.openalex_id == expected_openalex_id
        assert record.active is True

    # Jonathan H Chen is the one pre-existing "ambiguous" identity with a
    # non-default relevance filter and a second alias -- confirm none of
    # that was disturbed by the batch of new entries.
    jonathan = by_id["jonathan_h_chen"]
    assert jonathan.orcid == "0000-0002-4387-8740"
    assert jonathan.identity_status == "ambiguous"
    assert jonathan.relevance_filter == "healthcare_arise"
    assert jonathan.aliases == ["Jonathan H Chen", "Jonathan Chen"]
