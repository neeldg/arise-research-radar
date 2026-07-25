"""Deterministic work-type classification (the "Classify" pipeline stage).

Not every OpenAlex work behind a DOI is a conventional research paper: some
are preprints, some are conference papers, some are trial/study protocol
registrations, and some are dataset or general-purpose repository records
(e.g. archived on Zenodo or OSF Registries). All of them are retained under
the project's fail-open principle (see CLAUDE.md) — this module only decides
which of a fixed set of categories a record falls into, and whether that
category defaults to being eligible for scheduled drafting. No record is
ever discarded here, and "unknown" is itself a valid, imported category.

Classification never relies solely on the source repository (Zenodo/OSF):
OpenAlex type, venue, DOI prefix, and title are all considered together, since
both Zenodo and OSF host a genuine mix of conventional papers and other
research objects.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from arise_radar.models import NormalizedPublication

WorkTypeCategory = Literal[
    "article",
    "preprint",
    "conference",
    "editorial/viewpoint",
    "protocol/registration",
    "dataset/repository",
    "unknown",
]

# Article, preprint, conference, and editorial/viewpoint content has findings
# worth summarizing and drafting about. Protocol/registration and
# dataset/repository records are legitimate ARISE-affiliated research
# objects worth tracking, but rarely have a "finding" for a social post, and
# "unknown" means we're not confident enough to draft automatically at all.
DRAFT_ELIGIBLE_CATEGORIES: frozenset[WorkTypeCategory] = frozenset(
    {"article", "preprint", "conference", "editorial/viewpoint"}
)

# DOI prefixes for general-purpose repositories whose records span many kinds
# of research objects. The prefix alone is a strong hint for these two
# specific, curated namespaces (Zenodo's general archive; OSF's Registries
# product specifically, as opposed to other OSF-hosted preprint servers like
# PsyArXiv/SocArXiv, which use their own DOI prefixes and are conventional
# preprints) — title/venue terms below can still override it.
_ZENODO_DOI_PREFIX = "10.5281/"
_OSF_REGISTRIES_DOI_PREFIX = "10.17605/osf.io/"

_PROTOCOL_TERMS: tuple[str, ...] = (
    "protocol",
    "registration",
    "preregistration",
    "pre-registration",
    "trial registration",
    "study registration",
    "registry",
)
_DATASET_TERMS: tuple[str, ...] = (
    "dataset",
    "data set",
    "repository",
    "code repository",
    "software",
    "archive",
)

# OpenAlex's own `type` field, when present and unambiguous, is the primary
# signal once the repository/keyword hints above don't already apply.
_OPENALEX_TYPE_MAP: dict[str, WorkTypeCategory] = {
    "article": "article",
    "review": "article",
    "preprint": "preprint",
    "proceedings-article": "conference",
    "editorial": "editorial/viewpoint",
    "letter": "editorial/viewpoint",
    "dataset": "dataset/repository",
}


class WorkTypeClassification(BaseModel):
    category: WorkTypeCategory
    draft_eligible: bool
    reason: str


def classify_work_type(publication: NormalizedPublication) -> WorkTypeClassification:
    """Classify one publication deterministically. Always returns a category
    — never raises, never a basis for excluding the record from import.
    """
    title = (publication.title or "").lower()
    venue = (publication.venue or "").lower()
    doi = (publication.doi or "").lower()
    haystack = f"{title} {venue}"

    looks_like_osf_registration = doi.startswith(_OSF_REGISTRIES_DOI_PREFIX)
    if _matches_any(_PROTOCOL_TERMS, haystack) or looks_like_osf_registration:
        return WorkTypeClassification(
            category="protocol/registration",
            draft_eligible=False,
            reason=(
                "looks like a study protocol or registration record, not a "
                "conventional research paper"
            ),
        )

    looks_like_zenodo = doi.startswith(_ZENODO_DOI_PREFIX)
    if _matches_any(_DATASET_TERMS, haystack) or looks_like_zenodo:
        return WorkTypeClassification(
            category="dataset/repository",
            draft_eligible=False,
            reason=(
                "looks like a dataset, software, or general-purpose repository "
                "record, not a conventional research paper"
            ),
        )

    mapped = _OPENALEX_TYPE_MAP.get((publication.work_type or "").lower())
    if mapped is not None:
        return WorkTypeClassification(
            category=mapped,
            draft_eligible=mapped in DRAFT_ELIGIBLE_CATEGORIES,
            reason=f"OpenAlex type {publication.work_type!r} maps to {mapped!r}",
        )

    return WorkTypeClassification(
        category="unknown",
        draft_eligible=False,
        reason=(
            f"no confident classification from OpenAlex type {publication.work_type!r}, "
            "venue, DOI, or title"
        ),
    )


def _matches_any(terms: tuple[str, ...], haystack: str) -> bool:
    return any(term in haystack for term in terms)
