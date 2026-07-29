"""Citation monitoring, Phase 1: detect citation relationships to tracked
ARISE papers using OpenAlex, and produce one CitationEvent per exact citing
-> cited edge.

Contains no Notion- or Slack-specific logic — only translation from raw
OpenAlex work records (plus the tracked-paper list read from Notion; see
sinks/notion_citations.py) into CitationEvent records, per the project's
"normalize first" rule. Never queries OpenAlex by researcher name — citation
discovery is driven exclusively by tracked OpenAlex work IDs via the `cites`
filter (see sources/openalex.py's `iter_citing_works_batch`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, Field

from arise_radar.sources.openalex import (
    DEFAULT_CITATION_BATCH_SIZE,
    OpenAlexClient,
    OpenAlexError,
    clean_doi,
    extract_author_names,
    parse_openalex_date,
    short_openalex_id,
)

REVIEW_STATUS_NEW = "New"
SLACK_STATUS_PENDING = "Pending"
SLACK_STATUS_SUPPRESSED = "Suppressed"
CITATION_RELATIONSHIP_UNCLASSIFIED = "Unclassified"


def compute_citation_key(citing_openalex_id: str, cited_openalex_id: str) -> str:
    """`citation:<short-citing-openalex-id>:<short-cited-openalex-id>` —
    stable regardless of whether callers pass full openalex.org URLs or
    already-short IDs.
    """
    return (
        f"citation:{short_openalex_id(citing_openalex_id)}:{short_openalex_id(cited_openalex_id)}"
    )


class TrackedWork(BaseModel):
    """One tracked ARISE paper, read from the existing Research Radar Items
    (publications) Notion data source — see
    sinks.notion_citations.extract_tracked_works. Only rows with a valid
    OpenAlex work ID ever become a TrackedWork.
    """

    openalex_id: str
    canonical_key: str
    title: str
    researchers: list[str] = Field(default_factory=list)


class CitationEvent(BaseModel):
    """One exact citing-paper -> tracked-ARISE-paper citation edge.

    `citation_relationship`/`relationship_evidence` are reserved for a later
    classification phase (see CLAUDE.md's "Scholarly citation pipeline") —
    Phase 1 never classifies; they default to "Unclassified"/empty and are
    never touched again once a row exists (see sinks/notion_citations.py).
    """

    citation_key: str
    citing_title: str
    citing_openalex_id: str
    citing_doi: str | None = None
    citing_url: str | None = None
    citing_authors: list[str] = Field(default_factory=list)
    citing_publication_date: date | None = None
    cited_openalex_id: str
    cited_canonical_key: str
    cited_title: str
    arise_researchers: list[str] = Field(default_factory=list)
    detected_date: date
    is_baseline: bool
    review_status: str = REVIEW_STATUS_NEW
    citation_relationship: str = CITATION_RELATIONSHIP_UNCLASSIFIED
    relationship_evidence: str = ""
    slack_status: str
    system_notes: str = ""


def chunk_work_ids(work_ids: Sequence[str], batch_size: int) -> list[list[str]]:
    """Split tracked work IDs into batches no larger than `batch_size` —
    OpenAlex's practical limit on `|`-joined OR filter values (see
    sources.openalex.DEFAULT_CITATION_BATCH_SIZE). The CLI can override the
    size; this never assumes a specific value itself.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    return [list(work_ids[i : i + batch_size]) for i in range(0, len(work_ids), batch_size)]


def _citing_url(citing_openalex_id: str, doi: str | None) -> str | None:
    if doi:
        return f"https://doi.org/{doi}"
    if citing_openalex_id:
        return f"https://openalex.org/{citing_openalex_id}"
    return None


def build_events_for_citing_work(
    citing_work: dict,
    tracked_by_id: dict[str, TrackedWork],
    *,
    detected_date: date,
    is_baseline: bool,
    slack_status: str,
) -> list[CitationEvent]:
    """One citing paper that references multiple tracked ARISE papers
    produces one CitationEvent per edge — recovered by intersecting this
    citing work's own `referenced_works` against every tracked ID we know
    about (not just the batch that happened to surface it), so a citing
    paper's edges are always complete regardless of batch boundaries.
    """
    referenced = {short_openalex_id(rid) for rid in citing_work.get("referenced_works") or []}
    cited_ids = sorted(referenced & tracked_by_id.keys())
    if not cited_ids:
        return []

    citing_openalex_id = short_openalex_id(citing_work.get("id", ""))
    citing_doi = clean_doi(citing_work.get("doi"))
    citing_title = citing_work.get("display_name") or citing_work.get("title") or "Untitled"
    citing_authors = extract_author_names(citing_work)
    citing_publication_date = parse_openalex_date(citing_work.get("publication_date"))
    citing_url = _citing_url(citing_openalex_id, citing_doi)

    events: list[CitationEvent] = []
    for cited_id in cited_ids:
        tracked = tracked_by_id[cited_id]
        events.append(
            CitationEvent(
                citation_key=compute_citation_key(citing_openalex_id, cited_id),
                citing_title=citing_title,
                citing_openalex_id=citing_openalex_id,
                citing_doi=citing_doi,
                citing_url=citing_url,
                citing_authors=citing_authors,
                citing_publication_date=citing_publication_date,
                cited_openalex_id=cited_id,
                cited_canonical_key=tracked.canonical_key,
                cited_title=tracked.title,
                arise_researchers=tracked.researchers,
                detected_date=detected_date,
                is_baseline=is_baseline,
                slack_status=slack_status,
            )
        )
    return events


class CitationDiscoveryResult(BaseModel):
    events: list[CitationEvent]
    raw_citing_work_matches: int
    batches_queried: int
    batch_errors: list[str] = Field(default_factory=list)


def discover_citation_edges(
    client: OpenAlexClient,
    tracked_works: list[TrackedWork],
    *,
    detected_date: date,
    is_baseline: bool,
    batch_size: int = DEFAULT_CITATION_BATCH_SIZE,
    since: date | None = None,
) -> CitationDiscoveryResult:
    """Query OpenAlex in batches of tracked work IDs, recover every exact
    citation edge, and return one CitationEvent per unique (citing, cited)
    pair. One failed batch is recorded in `batch_errors` and never stops the
    remaining batches (see requirement: "one failed batch must not stop
    other batches").

    `raw_citing_work_matches` counts every citing-work record seen across
    all batches, including a citing work that legitimately reappears in more
    than one batch (because it cites tracked papers split across batches) —
    `len(events)` after dedup is the unique-edge count.
    """
    tracked_by_id = {work.openalex_id: work for work in tracked_works}
    batches = chunk_work_ids(list(tracked_by_id), batch_size)
    slack_status = SLACK_STATUS_SUPPRESSED if is_baseline else SLACK_STATUS_PENDING

    raw_matches = 0
    batch_errors: list[str] = []
    deduped_events: dict[str, CitationEvent] = {}

    for batch in batches:
        try:
            citing_works = list(client.iter_citing_works_batch(batch, since=since))
        except OpenAlexError as exc:
            batch_errors.append(str(exc))
            continue

        for citing_work in citing_works:
            raw_matches += 1
            for event in build_events_for_citing_work(
                citing_work,
                tracked_by_id,
                detected_date=detected_date,
                is_baseline=is_baseline,
                slack_status=slack_status,
            ):
                deduped_events.setdefault(event.citation_key, event)

    return CitationDiscoveryResult(
        events=list(deduped_events.values()),
        raw_citing_work_matches=raw_matches,
        batches_queried=len(batches),
        batch_errors=batch_errors,
    )


def discover_citation_edges_from_fixture(
    tracked_works: list[TrackedWork],
    citing_works: list[dict],
    *,
    detected_date: date,
    is_baseline: bool,
) -> CitationDiscoveryResult:
    """Same edge-recovery logic as discover_citation_edges (reuses
    build_events_for_citing_work directly, so fixture-driven and live runs
    behave identically) but over a pre-supplied flat list of raw citing-work
    dicts instead of live, batched OpenAlex queries — used by
    scripts/run_citations.py's --fixture-file for fully offline runs.
    """
    tracked_by_id = {work.openalex_id: work for work in tracked_works}
    slack_status = SLACK_STATUS_SUPPRESSED if is_baseline else SLACK_STATUS_PENDING

    deduped_events: dict[str, CitationEvent] = {}
    for citing_work in citing_works:
        for event in build_events_for_citing_work(
            citing_work,
            tracked_by_id,
            detected_date=detected_date,
            is_baseline=is_baseline,
            slack_status=slack_status,
        ):
            deduped_events.setdefault(event.citation_key, event)

    return CitationDiscoveryResult(
        events=list(deduped_events.values()),
        raw_citing_work_matches=len(citing_works),
        batches_queried=1 if citing_works else 0,
        batch_errors=[],
    )
