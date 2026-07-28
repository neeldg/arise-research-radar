"""Typed record schemas for the ARISE Research Radar roster pipeline."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Researcher(BaseModel):
    """One person on the ARISE master roster (`config/roster.yaml`).

    Covers both ends of the roster: publication/citation-active researchers
    with a verified OpenAlex identity (`active: true`, `identity_status:
    "verified"` or `"ambiguous"`), and media-monitoring-only names carried
    for network completeness that don't yet have a confirmed scholarly ID
    (`active: false`, `identity_status: "unverified"`, `openalex_id: null`).
    Only the first group is ever queried against OpenAlex — see
    `arise_radar.roster.active_verified_researchers` /
    `unverified_researchers`, and `scripts/run_roster.py`'s
    `_split_queryable`, which skips inactive and ID-less entries alike.
    """

    id: str
    name: str
    openalex_id: str | None = None
    orcid: str | None = None
    aliases: list[str] = Field(default_factory=list)
    active: bool = True
    identity_status: Literal["verified", "ambiguous", "unverified"] = "verified"
    relevance_filter: Literal["none", "healthcare_arise"] = "none"
    # Optional network metadata, populated only where actually known (e.g.
    # from https://arise-ai.org/team) -- never invented for a person it
    # wasn't given for.
    category: str | None = None
    institution: str | None = None
    role: str | None = None


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
    topics: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    venue: str | None = None
    work_type: str | None = None
    authors: list[str] = Field(default_factory=list)


def compute_canonical_key(doi: str | None, openalex_work_id: str) -> str:
    """Prefer DOI as the stable identifier; fall back to the OpenAlex work ID."""
    if doi:
        return f"doi:{doi.lower()}"
    return f"openalex:{openalex_work_id}"
