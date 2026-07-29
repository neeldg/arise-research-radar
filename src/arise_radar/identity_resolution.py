"""Scholarly identity resolution: candidate-report generation for the 49
inactive, unverified people on the ARISE master roster (config/roster.yaml).

This module never touches the roster file and never activates anyone. It
only searches OpenAlex for plausible author matches and scores them for a
human reviewer — see scripts/resolve_roster_identities.py for the CLI, and
docs/identity_resolution_workflow.md-equivalent guidance in that script's
module docstring for the human verification workflow.

Two interchangeable candidate sources (`LiveOpenAlexSource`,
`FixtureSource`) feed the same scoring/report-building code, so live and
fixture-driven runs are scored identically — see `candidates_for`.
"""

from __future__ import annotations

import csv
import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from arise_radar.models import Researcher
from arise_radar.relevance import INCLUDE_TERMS
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError

# A returned candidate whose name similarity clears this bar is considered a
# serious contender worth counting toward the "common name" ambiguity flag.
COMMON_NAME_SIMILARITY_THRESHOLD = 0.85
RECENT_WORKS_LIMIT = 3

Confidence = Literal["high", "medium", "low", "none"]


def select_researchers_pending_review(researchers: list[Researcher]) -> list[Researcher]:
    """Exactly the intake filter this workflow requires: `active: false` AND
    `identity_status: unverified`. Written explicitly (rather than relying on
    the two conditions always coinciding) so this script never silently
    processes an active or already-verified/ambiguous record, even if the
    roster's invariants ever drift.
    """
    return [r for r in researchers if not r.active and r.identity_status == "unverified"]


def build_author_search_query(researcher: Researcher) -> str:
    """The free-text query sent to OpenAlex's /authors search: the exact full
    name plus, where known, institution and role. OpenAlex's author search
    indexes primarily on name, so institution/role bias the ranking rather
    than guarantee precision — affiliation_match/topic_match below
    independently verify each returned candidate against the same fields.
    """
    parts = [researcher.name]
    if researcher.institution:
        parts.append(researcher.institution)
    if researcher.role:
        parts.append(researcher.role)
    return " ".join(parts)


class CandidateSource(Protocol):
    def candidates_for(self, researcher: Researcher) -> list[tuple[dict, list[dict]]]:
        """Return (raw OpenAlex author dict, raw recent-works list) pairs."""
        ...


class LiveOpenAlexSource:
    """Queries the real OpenAlex API. Only ever constructed when
    `--live-openalex` is explicitly passed — see the CLI."""

    def __init__(self, client: OpenAlexClient) -> None:
        self._client = client

    def candidates_for(self, researcher: Researcher) -> list[tuple[dict, list[dict]]]:
        query = build_author_search_query(researcher)
        candidates = self._client.search_authors(query)
        pairs: list[tuple[dict, list[dict]]] = []
        for candidate in candidates:
            author_id = _short_openalex_id(candidate.get("id", ""))
            recent_works = (
                self._client.get_recent_works(author_id, limit=RECENT_WORKS_LIMIT)
                if author_id
                else []
            )
            pairs.append((candidate, recent_works))
        return pairs


class FixtureSource:
    """Replays a static JSON fixture instead of calling OpenAlex — the only
    source used in tests (see tests/test_identity_resolution.py), and the
    intended way to review candidates offline.

    Fixture shape (keyed by roster researcher id, native OpenAlex JSON
    shapes throughout — see config/identity_resolution_fixture.example.json):

        {
          "<researcher_id>": {
            "candidates": [
              {..raw OpenAlex author fields.., "recent_works": [..raw work dicts..]}
            ]
          }
        }
    """

    def __init__(self, data: dict) -> None:
        self._data = data

    def candidates_for(self, researcher: Researcher) -> list[tuple[dict, list[dict]]]:
        entry = self._data.get(researcher.id) or {}
        candidates = entry.get("candidates") or []
        return [(candidate, candidate.get("recent_works") or []) for candidate in candidates]

    @classmethod
    def from_file(cls, path: Path) -> FixtureSource:
        return cls(json.loads(path.read_text()))


class IdentityCandidateRow(BaseModel):
    """One (researcher, candidate) pair in the reviewable report. A
    researcher with zero candidates still gets exactly one row (confidence
    "none") — never silently omitted."""

    researcher_id: str
    researcher_name: str
    researcher_institution: str | None = None
    candidate_display_name: str = ""
    candidate_openalex_id: str = ""
    candidate_orcid: str | None = None
    current_affiliation: str | None = None
    recent_affiliations: list[str] = Field(default_factory=list)
    works_count: int = 0
    cited_by_count: int = 0
    recent_works: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    name_similarity: float = 0.0
    affiliation_match: bool = False
    topic_match: bool = False
    confidence: Confidence = "none"
    ambiguity_notes: list[str] = Field(default_factory=list)


def _normalize_name(name: str) -> str:
    return " ".join(name.lower().split())


def name_similarity(researcher_name: str, candidate_name: str) -> float:
    return SequenceMatcher(
        None, _normalize_name(researcher_name), _normalize_name(candidate_name)
    ).ratio()


def _short_openalex_id(raw_id: str) -> str:
    return raw_id.removeprefix("https://openalex.org/")


def _extract_current_affiliation(candidate: dict) -> str | None:
    last_known = candidate.get("last_known_institutions") or []
    if last_known and last_known[0].get("display_name"):
        return last_known[0]["display_name"]
    affiliations = candidate.get("affiliations") or []
    if affiliations:
        institution = affiliations[0].get("institution") or {}
        return institution.get("display_name")
    return None


def _extract_recent_affiliations(candidate: dict) -> list[str]:
    names: list[str] = []
    for entry in candidate.get("last_known_institutions") or []:
        if entry.get("display_name") and entry["display_name"] not in names:
            names.append(entry["display_name"])
    for affiliation in candidate.get("affiliations") or []:
        institution = affiliation.get("institution") or {}
        name = institution.get("display_name")
        if name and name not in names:
            names.append(name)
    return names


def _extract_candidate_topics(candidate: dict) -> list[str]:
    names: set[str] = set()
    for entry in candidate.get("topics") or []:
        if entry.get("display_name"):
            names.add(entry["display_name"])
    for entry in candidate.get("x_concepts") or []:
        if entry.get("display_name"):
            names.add(entry["display_name"])
    return sorted(names)


def _extract_recent_work_titles(recent_works: list[dict]) -> list[str]:
    titles: list[str] = []
    for work in recent_works:
        title = work.get("display_name") or work.get("title")
        if not title:
            continue
        year = (work.get("publication_date") or "")[:4]
        titles.append(f"{title} ({year})" if year else title)
    return titles


def affiliation_match(researcher: Researcher, recent_affiliations: list[str]) -> bool:
    """Never invents a match: a researcher with no known roster institution
    always scores False, since there is nothing to compare against."""
    if not researcher.institution:
        return False
    target = researcher.institution.strip().lower()
    return any(
        target in affiliation.lower() or affiliation.lower() in target
        for affiliation in recent_affiliations
    )


def topic_match(topics: list[str]) -> tuple[bool, list[str]]:
    """Whether any of a candidate's topics/concepts land in ARISE's
    healthcare/clinical-AI domain (reusing the same term list the relevance
    filter uses for publications — see arise_radar.relevance)."""
    haystack = " ".join(topics).lower()
    matched = sorted({term for term in INCLUDE_TERMS if term in haystack})
    return bool(matched), matched


def compute_confidence(
    similarity: float, has_affiliation_match: bool, has_topic_match: bool
) -> Confidence:
    """Deterministic, documented thresholds — never a judgment call made
    silently. A human reviews every row regardless of confidence; this only
    orders the review queue.
    """
    if similarity >= 0.9 and has_affiliation_match:
        return "high"
    if similarity >= 0.9 or (similarity >= 0.6 and has_affiliation_match):
        return "medium"
    if has_topic_match and similarity >= 0.6:
        return "medium"
    return "low"


def _no_candidates_row(researcher: Researcher, *, note: str) -> IdentityCandidateRow:
    return IdentityCandidateRow(
        researcher_id=researcher.id,
        researcher_name=researcher.name,
        researcher_institution=researcher.institution,
        confidence="none",
        ambiguity_notes=[note],
    )


def build_candidate_rows(
    researcher: Researcher, candidate_pairs: list[tuple[dict, list[dict]]]
) -> list[IdentityCandidateRow]:
    """Score every candidate returned for one researcher. Ambiguity flags
    that depend on the *set* of candidates (common-name detection) are
    computed here, across all of them together, before building rows.
    """
    if not candidate_pairs:
        return [_no_candidates_row(researcher, note="no candidates found")]

    similarities = [
        name_similarity(researcher.name, candidate.get("display_name", ""))
        for candidate, _ in candidate_pairs
    ]
    strong_match_count = sum(1 for s in similarities if s >= COMMON_NAME_SIMILARITY_THRESHOLD)

    rows: list[IdentityCandidateRow] = []
    for (candidate, recent_works), similarity in zip(candidate_pairs, similarities, strict=True):
        recent_affiliations = _extract_recent_affiliations(candidate)
        current_affiliation = _extract_current_affiliation(candidate)
        topics = _extract_candidate_topics(candidate)
        has_affiliation_match = affiliation_match(researcher, recent_affiliations)
        has_topic_match, matched_topics = topic_match(topics)

        notes: list[str] = []
        if strong_match_count > 1:
            notes.append(
                f"common name: {strong_match_count} candidates returned with "
                "similarly matching names"
            )
        if recent_affiliations and not has_affiliation_match:
            known = ", ".join(recent_affiliations)
            expected = researcher.institution or "unknown"
            notes.append(
                f"conflicting affiliation: candidate's known affiliation(s) [{known}] do not "
                f"include the roster institution ({expected}) — verify this is the same person"
            )

        rows.append(
            IdentityCandidateRow(
                researcher_id=researcher.id,
                researcher_name=researcher.name,
                researcher_institution=researcher.institution,
                candidate_display_name=candidate.get("display_name", ""),
                candidate_openalex_id=_short_openalex_id(candidate.get("id", "")),
                candidate_orcid=candidate.get("orcid"),
                current_affiliation=current_affiliation,
                recent_affiliations=recent_affiliations,
                works_count=candidate.get("works_count") or 0,
                cited_by_count=candidate.get("cited_by_count") or 0,
                recent_works=_extract_recent_work_titles(recent_works),
                topics=topics,
                name_similarity=round(similarity, 3),
                affiliation_match=has_affiliation_match,
                topic_match=has_topic_match,
                confidence=compute_confidence(similarity, has_affiliation_match, has_topic_match),
                ambiguity_notes=notes,
            )
        )
    return rows


def resolve_researcher(
    researcher: Researcher, source: CandidateSource
) -> tuple[list[IdentityCandidateRow], str | None]:
    """Resolve one researcher's candidates. Returns (rows, error) — error is
    only set when the source itself failed (e.g. a live OpenAlex request
    error); a clean zero-candidate result is not an error and returns rows
    with `error=None`. Callers must continue the batch on error, never stop.
    """
    try:
        candidate_pairs = source.candidates_for(researcher)
    except OpenAlexError as exc:
        return [_no_candidates_row(researcher, note=f"query failed: {exc}")], str(exc)
    return build_candidate_rows(researcher, candidate_pairs), None


CSV_FIELDNAMES: list[str] = [
    "researcher_id",
    "researcher_name",
    "researcher_institution",
    "candidate_display_name",
    "candidate_openalex_id",
    "candidate_orcid",
    "current_affiliation",
    "recent_affiliations",
    "works_count",
    "cited_by_count",
    "recent_works",
    "topics",
    "name_similarity",
    "affiliation_match",
    "topic_match",
    "confidence",
    "ambiguity_notes",
]


def _row_to_csv_dict(row: IdentityCandidateRow) -> dict[str, str]:
    return {
        "researcher_id": row.researcher_id,
        "researcher_name": row.researcher_name,
        "researcher_institution": row.researcher_institution or "",
        "candidate_display_name": row.candidate_display_name,
        "candidate_openalex_id": row.candidate_openalex_id,
        "candidate_orcid": row.candidate_orcid or "",
        "current_affiliation": row.current_affiliation or "",
        "recent_affiliations": "; ".join(row.recent_affiliations),
        "works_count": str(row.works_count),
        "cited_by_count": str(row.cited_by_count),
        "recent_works": "; ".join(row.recent_works),
        "topics": "; ".join(row.topics),
        "name_similarity": f"{row.name_similarity:.3f}",
        "affiliation_match": str(row.affiliation_match),
        "topic_match": str(row.topic_match),
        "confidence": row.confidence,
        "ambiguity_notes": "; ".join(row.ambiguity_notes),
    }


def write_csv_report(rows: list[IdentityCandidateRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(_row_to_csv_dict(row))


def write_json_report(rows: list[IdentityCandidateRow], path: Path) -> None:
    path.write_text(
        json.dumps([row.model_dump() for row in rows], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
