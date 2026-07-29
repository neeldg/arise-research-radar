"""Notion sink for citation events — a separate data source
(`NOTION_CITATIONS_DATA_SOURCE_ID`) from the publications data source
(`NOTION_DATA_SOURCE_ID`, see sinks/notion.py). This module never writes to
the publications schema, and reads the publications data source only to
extract the tracked-paper list (see `extract_tracked_works`).

Uses the same injectable `NotionClient` as sinks/notion.py (generic Notion
HTTP client, not data-source-specific) so tests can mock it the same way via
httpx.MockTransport.

Citation-key matching is a single bulk read, not one query per event. An
earlier version of upsert_citation_event called
`query_data_source_by_rich_text` once per CitationEvent — with thousands of
unique edges in a single run, that meant thousands of individual Notion
requests (in dry-run mode too, since only the write, not the lookup, was
gated by `dry_run`), and a chunk of them would fail once Notion's rate limit
kicked in. `load_citation_row_index` now reads the whole Citation Events data
source once via `NotionClient.iter_data_source_pages` (paginated, O(rows/100)
requests) into an in-memory `citation_key -> page_id` mapping that
`upsert_citation_event` consults and mutates in place — see that function's
docstring for the full contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from arise_radar.citations import CitationEvent, TrackedWork
from arise_radar.models import compute_canonical_key
from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionProperties,
    read_multi_select_names,
    read_rich_text,
    read_title,
    require_env,
)


class NotionCitationProperties:
    """Citation Events data source property names, kept out of the rest of
    the code — mirrors NotionProperties in sinks/notion.py, but for the
    separate citations data source."""

    NAME = "Name"
    CITATION_KEY = "Citation Key"
    CITING_OPENALEX_ID = "Citing OpenAlex ID"
    CITING_DOI = "Citing DOI"
    CITING_URL = "Citing URL"
    CITED_ARISE_PAPER = "Cited ARISE Paper"
    ARISE_RESEARCHERS = "ARISE Researchers"
    PUBLISHED_DATE = "Published Date"
    DETECTED_DATE = "Detected Date"
    BASELINE = "Baseline"
    REVIEW_STATUS = "Review Status"
    CITATION_RELATIONSHIP = "Citation Relationship"
    RELATIONSHIP_EVIDENCE = "Relationship Evidence"
    SLACK_STATUS = "Slack Status"
    SLACK_TIMESTAMP = "Slack Timestamp"
    SYSTEM_NOTES = "System Notes"


class NotionCitationsConfig(BaseModel):
    token: SecretStr
    publications_data_source_id: str
    citations_data_source_id: str


def load_notion_citations_config(*, env: Mapping[str, str] | None = None) -> NotionCitationsConfig:
    """Load NOTION_TOKEN, NOTION_DATA_SOURCE_ID (to read tracked papers from),
    and NOTION_CITATIONS_DATA_SOURCE_ID (to write citation events to).

    Pass `env` explicitly in tests to avoid touching the real process environment.
    """
    values = require_env(
        ("NOTION_TOKEN", "NOTION_DATA_SOURCE_ID", "NOTION_CITATIONS_DATA_SOURCE_ID"), env
    )
    return NotionCitationsConfig(
        token=values["NOTION_TOKEN"],
        publications_data_source_id=values["NOTION_DATA_SOURCE_ID"],
        citations_data_source_id=values["NOTION_CITATIONS_DATA_SOURCE_ID"],
    )


__all__ = [
    "NotionCitationProperties",
    "NotionCitationsConfig",
    "NotionConfigError",  # re-exported for convenience; identical to sinks.notion's
    "load_notion_citations_config",
]


# --- tracked-paper input: read from the existing publications data source ---------


class TrackedWorksResult(BaseModel):
    """Kept distinct from arise_radar.citations.TrackedWork: this pairs the
    extracted tracked works with reporting counts (requirement 3's "report
    clearly" numbers), which is a concern of reading from Notion, not of the
    TrackedWork record itself."""

    tracked: list[TrackedWork] = Field(default_factory=list)
    total_inspected: int
    skipped_no_openalex_id: int


def extract_tracked_works(pages: Iterable[dict]) -> TrackedWorksResult:
    """From publications-data-source page dicts, build the tracked-paper
    list for citation discovery. Only rows with a non-empty OpenAlex ID
    become a TrackedWork — this is the only identifier citation discovery
    ever uses (see arise_radar.citations: never queries by researcher name).
    Rows without one are counted, never silently dropped from the report.
    """
    tracked: list[TrackedWork] = []
    total = 0
    skipped = 0
    for page in pages:
        total += 1
        openalex_id = read_rich_text(page, NotionProperties.OPENALEX_ID)
        if not openalex_id:
            skipped += 1
            continue
        canonical_key = read_rich_text(
            page, NotionProperties.CANONICAL_KEY
        ) or compute_canonical_key(None, openalex_id)
        title = read_title(page) or "Untitled"
        researchers = read_multi_select_names(page, NotionProperties.RESEARCHERS)
        tracked.append(
            TrackedWork(
                openalex_id=openalex_id,
                canonical_key=canonical_key,
                title=title,
                researchers=researchers,
            )
        )
    return TrackedWorksResult(
        tracked=tracked, total_inspected=total, skipped_no_openalex_id=skipped
    )


# --- bulk citation-row index: one paginated read, not one query per event ---------


class CitationRowIndex(BaseModel):
    """In-memory index of the Citation Events data source, built once per run
    by `load_citation_row_index` from a single paginated read
    (`NotionClient.iter_data_source_pages`) instead of one
    `query_data_source_by_rich_text` call per citation key.

    Mutated in place by `upsert_citation_event` as new pages are created
    within the same run: a citation key this run itself just created is
    added to `key_to_page_id` immediately, so a later CitationEvent sharing
    that key in the *same* run is matched against it instead of creating a
    second row. On the *next* run, that same row is found by the initial
    bulk load instead — so an interrupted run is safely restartable: rows
    already written are never recreated.
    """

    key_to_page_id: dict[str, str] = Field(default_factory=dict)
    # A Citation Key stored on more than one existing page — a pre-existing
    # data-integrity problem, not something this pipeline resolves on its
    # own. Kept out of key_to_page_id entirely; matching events are reported
    # as skipped_duplicate (see upsert_citation_event) without ever being
    # written to any of the ambiguous pages.
    duplicate_keys: dict[str, list[str]] = Field(default_factory=dict)
    total_rows_loaded: int = 0
    # A stored row with no readable Citation Key and/or no page id. Excluded
    # from the index (never guessed at), counted so it's visible instead of
    # silently dropped.
    malformed_rows: int = 0


def load_citation_row_index(pages: Iterable[dict]) -> CitationRowIndex:
    """Pure indexing over already-fetched Citation Events page dicts (see
    `NotionClient.iter_data_source_pages`) — no I/O here; the paginated read
    itself is the caller's responsibility (see scripts/run_citations.py),
    matching the extract_tracked_works split above.
    """
    seen: dict[str, list[str]] = {}
    total = 0
    malformed = 0
    for page in pages:
        total += 1
        citation_key = read_rich_text(page, NotionCitationProperties.CITATION_KEY)
        page_id = page.get("id")
        if not citation_key or not page_id:
            malformed += 1
            continue
        seen.setdefault(citation_key, []).append(page_id)

    key_to_page_id: dict[str, str] = {}
    duplicate_keys: dict[str, list[str]] = {}
    for key, page_ids in seen.items():
        if len(page_ids) > 1:
            duplicate_keys[key] = page_ids
        else:
            key_to_page_id[key] = page_ids[0]

    return CitationRowIndex(
        key_to_page_id=key_to_page_id,
        duplicate_keys=duplicate_keys,
        total_rows_loaded=total,
        malformed_rows=malformed,
    )


# --- citation event upsert ----------------------------------------------------------


class CitationUpsertResult(BaseModel):
    citation_key: str
    action: Literal[
        "created", "updated", "skipped_duplicate", "error", "dry_run_create", "dry_run_update"
    ]
    page_id: str | None = None
    detail: str = ""
    # Only set when action == "error" — which Notion write failed, and the
    # HTTP status Notion returned (see NotionError.status_code), so callers
    # can report create vs. update failures separately instead of one
    # unexplained count (see scripts/run_citations.py's --error-report).
    stage: Literal["create", "update"] | None = None
    status_code: int | None = None


def upsert_citation_event(
    client: NotionClient,
    data_source_id: str,
    event: CitationEvent,
    index: CitationRowIndex,
    *,
    dry_run: bool = False,
) -> CitationUpsertResult:
    """Create or update the Notion row for one citation event, matched
    against the in-memory `index` (see load_citation_row_index) — never a
    per-event Notion query, in dry-run or live mode alike. Never raises for
    Notion API failures: those are captured in the returned result's
    action="error" so callers can continue processing the rest of a batch —
    same pattern as upsert_publication in sinks/notion.py.
    """
    if event.citation_key in index.duplicate_keys:
        page_ids = index.duplicate_keys[event.citation_key]
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="skipped_duplicate",
            detail=(
                f"{len(page_ids)} existing Notion pages share this citation key "
                f"({', '.join(page_ids)}); needs human repair"
            ),
        )

    existing_page_id = index.key_to_page_id.get(event.citation_key)
    if existing_page_id is None:
        return _create_citation(client, data_source_id, event, index, dry_run)

    return _update_citation(client, existing_page_id, event, dry_run)


def _create_citation(
    client: NotionClient,
    data_source_id: str,
    event: CitationEvent,
    index: CitationRowIndex,
    dry_run: bool,
) -> CitationUpsertResult:
    if dry_run:
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="dry_run_create",
            detail=(
                f"would create citation event: {event.citing_title!r} cites "
                f"{event.cited_title!r} (baseline={event.is_baseline}, "
                f"slack_status={event.slack_status!r})"
            ),
        )

    properties = _build_citation_create_properties(event)
    try:
        page = client.create_page(data_source_id, properties)
    except NotionError as exc:
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="error",
            stage="create",
            status_code=exc.status_code,
            detail=f"create failed: {exc}",
        )
    page_id = page.get("id")
    if page_id:
        # Same-run idempotency: a later event sharing this key (or a rerun
        # of discover_citation_edges within the same process) finds this
        # page via the index instead of creating a duplicate.
        index.key_to_page_id[event.citation_key] = page_id
    return CitationUpsertResult(citation_key=event.citation_key, action="created", page_id=page_id)


def _update_citation(
    client: NotionClient, page_id: str, event: CitationEvent, dry_run: bool
) -> CitationUpsertResult:
    if dry_run:
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="dry_run_update",
            page_id=page_id,
            detail=(
                "would refresh citation metadata (Baseline/Review Status/Slack fields preserved)"
            ),
        )

    properties = _build_citation_update_properties(event)
    try:
        client.update_page(page_id, properties)
    except NotionError as exc:
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="error",
            stage="update",
            status_code=exc.status_code,
            detail=f"update failed: {exc}",
        )
    return CitationUpsertResult(citation_key=event.citation_key, action="updated", page_id=page_id)


def _build_citation_create_properties(event: CitationEvent) -> dict:
    properties = _shared_citation_properties(event)
    properties[NotionCitationProperties.DETECTED_DATE] = _date_value(event.detected_date)
    properties[NotionCitationProperties.BASELINE] = {"checkbox": event.is_baseline}
    properties[NotionCitationProperties.REVIEW_STATUS] = {"select": {"name": event.review_status}}
    properties[NotionCitationProperties.CITATION_RELATIONSHIP] = {
        "select": {"name": event.citation_relationship}
    }
    properties[NotionCitationProperties.RELATIONSHIP_EVIDENCE] = _rich_text_value(
        event.relationship_evidence
    )
    properties[NotionCitationProperties.SLACK_STATUS] = {"select": {"name": event.slack_status}}
    return properties


def _build_citation_update_properties(event: CitationEvent) -> dict:
    # Detected Date, Baseline, Review Status, Citation Relationship,
    # Relationship Evidence, Slack Status, and Slack Timestamp are
    # intentionally omitted here. Detected Date is the original detection
    # date, preserved like publications' Detected Date. The rest are either
    # human-owned (Review Status) or reserved for a later phase (Citation
    # Relationship/Relationship Evidence/Slack *) and must never be reset by
    # a routine resync -- this is what guarantees a baseline rerun can never
    # flip an existing non-baseline row back to Baseline=true, and that
    # incremental reruns never reset human review progress.
    return _shared_citation_properties(event)


def _shared_citation_properties(event: CitationEvent) -> dict:
    properties: dict[str, object] = {
        NotionCitationProperties.NAME: _title_value(
            f"{event.citing_title} cites {event.cited_title}"
        ),
        NotionCitationProperties.CITATION_KEY: _rich_text_value(event.citation_key),
        NotionCitationProperties.CITING_OPENALEX_ID: _rich_text_value(event.citing_openalex_id),
        NotionCitationProperties.CITED_ARISE_PAPER: _rich_text_value(
            f"{event.cited_title} ({event.cited_canonical_key})"
        ),
        NotionCitationProperties.ARISE_RESEARCHERS: _multi_select_value(event.arise_researchers),
        NotionCitationProperties.SYSTEM_NOTES: _rich_text_value(event.system_notes),
    }
    if event.citing_doi:
        properties[NotionCitationProperties.CITING_DOI] = _rich_text_value(event.citing_doi)
    if event.citing_url:
        properties[NotionCitationProperties.CITING_URL] = {"url": event.citing_url}
    if event.citing_publication_date:
        properties[NotionCitationProperties.PUBLISHED_DATE] = _date_value(
            event.citing_publication_date
        )
    return properties


def _title_value(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def _rich_text_value(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _date_value(value: date) -> dict:
    return {"date": {"start": value.isoformat()}}


def _multi_select_value(names: Iterable[str]) -> dict:
    return {"multi_select": [{"name": name} for name in sorted(names)]}
