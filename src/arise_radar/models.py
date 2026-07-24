"""Typed record schemas for the ARISE Research Radar roster pipeline."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class Researcher(BaseModel):
    """A verified ARISE-affiliated researcher, ready to be queried against OpenAlex."""

    id: str
    name: str
    openalex_id: str | None = None
    orcid: str | None = None
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class SeedResearcher(BaseModel):
    """A names-only roster entry pending OpenAlex ID verification."""

    id: str
    name: str
    openalex_id: str | None = None
    verification_status: str = "unverified"


class NormalizedPublication(BaseModel):
    """A publication normalized from a source adapter.

    Independent of any Notion or LLM concerns, per the project's "normalize first" rule.
    """

    researcher_id: str
    researcher_name: str
    title: str
    publication_date: date | None
    doi: str | None
    openalex_id: str
    canonical_key: str


def compute_canonical_key(doi: str | None, openalex_work_id: str) -> str:
    """Prefer DOI as the stable identifier; fall back to the OpenAlex work ID."""
    if doi:
        return f"doi:{doi.lower()}"
    return f"openalex:{openalex_work_id}"
