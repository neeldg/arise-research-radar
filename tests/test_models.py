from arise_radar.models import compute_canonical_key


def test_canonical_key_prefers_doi() -> None:
    assert compute_canonical_key("10.1000/ABC", "W123") == "doi:10.1000/abc"


def test_canonical_key_falls_back_to_openalex_id() -> None:
    assert compute_canonical_key(None, "W123") == "openalex:W123"
