"""Terminal-readable formatting for normalized publications and roster warnings."""

from __future__ import annotations

from collections.abc import Sequence

from arise_radar.models import NormalizedPublication, Researcher


def format_skip_warning(researcher: Researcher, reason: str) -> str:
    return f"WARNING: skipping {researcher.name} ({researcher.id}) — {reason}"


def format_publications(publications: Sequence[NormalizedPublication]) -> str:
    if not publications:
        return "No publications found in the requested window."

    blocks = []
    for pub in publications:
        pub_date = pub.publication_date.isoformat() if pub.publication_date else "unknown"
        blocks.append(
            "\n".join(
                [
                    pub.researcher_name,
                    f"  Title:          {pub.title}",
                    f"  Date:           {pub_date}",
                    f"  DOI:            {pub.doi or '—'}",
                    f"  OpenAlex ID:    {pub.openalex_id}",
                    f"  Canonical key:  {pub.canonical_key}",
                ]
            )
        )
    return "\n\n".join(blocks)
