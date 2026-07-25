"""Deterministic, fail-open relevance filter for ambiguous researcher identities.

Applied only to researchers whose roster `relevance_filter` is not "none" (currently
Jonathan H Chen, whose OpenAlex profile is a merged/ambiguous author node — see
config/roster.yaml). Uses only OpenAlex metadata already captured on
NormalizedPublication (title, topics, concepts, venue, work type) and deterministic
term matching. No LLM calls.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

from arise_radar.models import NormalizedPublication, Researcher

# Healthcare / clinical / biomedical-informatics signals. Presence of any of these
# always wins over an exclude signal, so interdisciplinary papers are never excluded
# merely for touching a non-health field too.
INCLUDE_TERMS: tuple[str, ...] = (
    "healthcare",
    "health care",
    "health system",
    "health systems",
    "health policy",
    "public health",
    "population health",
    "health informatics",
    "biomedical informatics",
    "medical informatics",
    "clinical informatics",
    "electronic health record",
    "electronic health records",
    "ehr",
    "clinical decision support",
    "clinical ai",
    "clinical artificial intelligence",
    "medicine",
    "medical",
    "clinical",
    "clinician",
    "physician",
    "nurse",
    "nursing",
    "patient",
    "patients",
    "hospital",
    "hospitals",
    "diagnosis",
    "diagnostic",
    "diagnostics",
    "treatment",
    "therapy",
    "therapeutic",
    "disease",
    "oncology",
    "cardiology",
    "radiology",
    "pathology",
    "psychiatry",
    "pediatric",
    "pediatrics",
    "pharmacology",
    "pharmacy",
    "medical education",
    "residency",
    "patient safety",
    "clinical trial",
    "clinical trials",
    "clinical reasoning",
    "epidemiology",
    "telehealth",
    "telemedicine",
    "biomedical",
    "health information",
    "ai in healthcare",
    "ai in medicine",
    "healthcare ai",
    "medical ai",
)

# Domains with no plausible ARISE connection. Only fires when no INCLUDE_TERMS
# match, keeping exclusion high-precision.
EXCLUDE_TERMS: tuple[str, ...] = (
    "food science",
    "food chemistry",
    "food industry",
    "agricultural",
    "agriculture",
    "pumpkin",
    "microfluidization",
    "telecommunications",
    "telecom",
    "wireless network",
    "cellular network",
    "image compression",
    "signal processing",
    "quantization",
    "econometrics",
    "econometric",
    "instrumental variable",
    "binary instrument",
    "industrial engineering",
    "materials science",
    "semiconductor",
    "polymer",
    "metallurgy",
)


class RelevanceDecision(BaseModel):
    status: Literal["keep", "exclude", "uncertain"]
    reason: str
    matched_terms: list[str] = Field(default_factory=list)


def apply_relevance_filter(
    researcher: Researcher, publications: Sequence[NormalizedPublication]
) -> list[tuple[NormalizedPublication, RelevanceDecision]]:
    """Apply the relevance filter, or bypass it entirely when relevance_filter is 'none'."""
    if researcher.relevance_filter == "none":
        return [
            (
                pub,
                RelevanceDecision(
                    status="keep",
                    reason="relevance filtering not enabled for this researcher",
                    matched_terms=[],
                ),
            )
            for pub in publications
        ]
    return [(pub, evaluate_relevance(pub)) for pub in publications]


def evaluate_relevance(publication: NormalizedPublication) -> RelevanceDecision:
    """Classify a single publication. Never raises: on internal error, keeps the paper."""
    try:
        return _classify(publication)
    except Exception as exc:  # the filter must never take down the pipeline
        print(
            f"WARNING: relevance filter failed for {publication.openalex_id} "
            f"({publication.title!r}): {exc} — keeping by default",
            file=sys.stderr,
        )
        return RelevanceDecision(
            status="uncertain",
            reason=f"relevance filter raised an error ({exc}); keeping by default",
            matched_terms=[],
        )


def _classify(publication: NormalizedPublication) -> RelevanceDecision:
    haystack = _build_haystack(publication)
    if not haystack.strip():
        return RelevanceDecision(
            status="uncertain",
            reason="no usable metadata (title, topics, concepts, and venue all empty)",
            matched_terms=[],
        )

    include_matches = _matches(INCLUDE_TERMS, haystack)
    if include_matches:
        return RelevanceDecision(
            status="keep",
            reason="matched a healthcare/clinical-AI signal",
            matched_terms=include_matches,
        )

    exclude_matches = _matches(EXCLUDE_TERMS, haystack)
    if exclude_matches:
        return RelevanceDecision(
            status="exclude",
            reason="matched a clearly unrelated-domain signal with no offsetting healthcare signal",
            matched_terms=exclude_matches,
        )

    return RelevanceDecision(
        status="uncertain",
        reason="no clear healthcare or exclusion signal found in available metadata",
        matched_terms=[],
    )


def _build_haystack(publication: NormalizedPublication) -> str:
    parts = [publication.title or ""]
    parts.extend(publication.topics)
    parts.extend(publication.concepts)
    if publication.venue:
        parts.append(publication.venue)
    if publication.work_type:
        parts.append(publication.work_type)
    return " ".join(parts).lower()


def _matches(terms: tuple[str, ...], haystack: str) -> list[str]:
    return sorted({term for term in terms if _term_in(term, haystack)})


def _term_in(term: str, haystack: str) -> bool:
    pattern = r"\b" + re.escape(term) + r"\b"
    return re.search(pattern, haystack) is not None
