from arise_radar.models import Researcher, compute_canonical_key


def test_canonical_key_prefers_doi() -> None:
    assert compute_canonical_key("10.1000/ABC", "W123") == "doi:10.1000/abc"


def test_canonical_key_falls_back_to_openalex_id() -> None:
    assert compute_canonical_key(None, "W123") == "openalex:W123"


# --- Researcher schema ------------------------------------------------------------


def test_researcher_defaults_to_active_verified_with_no_optional_metadata() -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe")
    assert researcher.active is True
    assert researcher.identity_status == "verified"
    assert researcher.category is None
    assert researcher.institution is None
    assert researcher.role is None


def test_researcher_accepts_unverified_identity_status() -> None:
    researcher = Researcher(
        id="jane_doe",
        name="Jane Doe",
        active=False,
        identity_status="unverified",
        category="researcher_affiliate",
        institution="Stanford University",
    )
    assert researcher.identity_status == "unverified"
    assert researcher.active is False
    assert researcher.category == "researcher_affiliate"
    assert researcher.institution == "Stanford University"
    assert researcher.role is None


def test_researcher_still_accepts_ambiguous_identity_status() -> None:
    researcher = Researcher(
        id="jonathan_h_chen", name="Jonathan H Chen", identity_status="ambiguous"
    )
    assert researcher.identity_status == "ambiguous"
