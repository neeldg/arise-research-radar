from datetime import date

from arise_radar.models import NormalizedPublication, Researcher
from arise_radar.output import format_publications, format_skip_warning


def test_format_skip_warning_includes_name_id_and_reason() -> None:
    researcher = Researcher(id="jane_doe", name="Jane Doe", openalex_id=None)
    message = format_skip_warning(researcher, "no verified OpenAlex ID")
    assert "Jane Doe" in message
    assert "jane_doe" in message
    assert "no verified OpenAlex ID" in message


def test_format_publications_empty_list() -> None:
    assert "No publications found" in format_publications([])


def test_format_publications_includes_required_fields() -> None:
    pub = NormalizedPublication(
        researcher_id="jane_doe",
        researcher_name="Jane Doe",
        title="A Great Paper",
        publication_date=date(2026, 1, 1),
        doi="10.1000/abc",
        openalex_id="W1",
        canonical_key="doi:10.1000/abc",
    )
    rendered = format_publications([pub])
    assert "Jane Doe" in rendered
    assert "A Great Paper" in rendered
    assert "2026-01-01" in rendered
    assert "10.1000/abc" in rendered
    assert "W1" in rendered
    assert "doi:10.1000/abc" in rendered
