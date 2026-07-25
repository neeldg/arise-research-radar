"""Deterministic version-family (possible-duplicate) detection.

The same underlying study can appear under multiple DOIs — most commonly a
preprint cross-posted to two different preprint/repository services (e.g.
JMIR Preprints and Preprints.org). This module never merges canonical keys:
DOI canonical keys stay exactly as computed by
`arise_radar.models.compute_canonical_key`. It only flags PROBABLE
version-family pairs — via strongly normalized title similarity plus
overlapping paper authors — so a human can decide in Notion whether to merge
or reject one. Purely advisory; always a two-signal AND gate (title alone is
not enough — see the module's own validation notes: superficially similar
but unrelated titles score just as high on title similarity alone).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from pydantic import BaseModel

from arise_radar.models import NormalizedPublication

TITLE_SIMILARITY_THRESHOLD = 0.90

_VERSION_SUFFIX_PATTERN = re.compile(r"\bv\d+\b")
_NON_ALNUM_PATTERN = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Strongly normalize a title for cross-DOI comparison: lowercase, strip
    version markers (e.g. "v1") and punctuation, collapse whitespace."""
    text = title.lower()
    text = _VERSION_SUFFIX_PATTERN.sub(" ", text)
    text = _NON_ALNUM_PATTERN.sub(" ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    return text


def title_similarity(title_a: str, title_b: str) -> float:
    """A deterministic 0.0-1.0 similarity ratio between two strongly
    normalized titles."""
    return SequenceMatcher(None, normalize_title(title_a), normalize_title(title_b)).ratio()


def authors_overlap(authors_a: list[str], authors_b: list[str]) -> list[str]:
    """Author names (in `authors_a`'s original casing) present in both lists,
    compared case-insensitively, sorted for determinism."""
    normalized_b = {name.strip().lower() for name in authors_b if name.strip()}
    shared = {name for name in authors_a if name.strip() and name.strip().lower() in normalized_b}
    return sorted(shared)


class VersionFamilyMatch(BaseModel):
    canonical_key: str
    title_similarity: float
    shared_authors: list[str]


def find_possible_version_duplicates(
    publication: NormalizedPublication,
    candidates: list[NormalizedPublication],
    *,
    title_threshold: float = TITLE_SIMILARITY_THRESHOLD,
) -> list[VersionFamilyMatch]:
    """Compare `publication` against `candidates` (typically: every other
    distinct-canonical-key publication seen in the same run) and return every
    candidate whose canonical key differs but whose title is strongly similar
    AND that shares at least one paper author. Never merges or mutates
    anything — purely advisory.
    """
    matches: list[VersionFamilyMatch] = []
    for candidate in candidates:
        if candidate.canonical_key == publication.canonical_key:
            continue
        similarity = title_similarity(publication.title, candidate.title)
        if similarity < title_threshold:
            continue
        shared = authors_overlap(publication.authors, candidate.authors)
        if not shared:
            continue
        matches.append(
            VersionFamilyMatch(
                canonical_key=candidate.canonical_key,
                title_similarity=round(similarity, 3),
                shared_authors=shared,
            )
        )
    return matches


def format_version_duplicate_note(matches: list[VersionFamilyMatch]) -> str:
    """A human-readable System Notes addendum linking probable versions so a
    human can decide whether to merge or reject one. Empty when there are no
    matches — never writes a note for a clean record."""
    if not matches:
        return ""
    parts = [
        f"{match.canonical_key} (title similarity {match.title_similarity:.2f}, "
        f"shared authors: {', '.join(match.shared_authors)})"
        for match in matches
    ]
    return "Possible version duplicate of: " + "; ".join(parts)
