"""Notion sink for citation events — a separate data source
(`NOTION_CITATIONS_DATA_SOURCE_ID`) from the publications data source
(`NOTION_DATA_SOURCE_ID`, see sinks/notion.py). This module never writes to
the publications schema, and reads the publications data source only to
extract the tracked-paper list (see `extract_tracked_works`).

Uses the same injectable `NotionClient` as sinks/notion.py (generic Notion
HTTP client, not data-source-specific) so tests can mock it the same way via
httpx.MockTransport.
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


# --- citation event upsert ----------------------------------------------------------


class CitationUpsertResult(BaseModel):
    citation_key: str
    action: Literal[
        "created", "updated", "skipped_duplicate", "error", "dry_run_create", "dry_run_update"
    ]
    page_id: str | None = None
    detail: str = ""


def upsert_citation_event(
    client: NotionClient,
    data_source_id: str,
    event: CitationEvent,
    *,
    dry_run: bool = False,
) -> CitationUpsertResult:
    """Create or update the Notion row for one citation event, matched by
    Citation Key. Never raises for Notion API failures: those are captured
    in the returned result's action="error" so callers can continue
    processing the rest of a batch — same pattern as upsert_publication in
    sinks/notion.py.
    """
    try:
        matches = client.query_data_source_by_rich_text(
            data_source_id, NotionCitationProperties.CITATION_KEY, event.citation_key
        )
    except NotionError as exc:
        return CitationUpsertResult(
            citation_key=event.citation_key, action="error", detail=f"query failed: {exc}"
        )

    if len(matches) > 1:
        page_ids = ", ".join(match.get("id", "?") for match in matches)
        return CitationUpsertResult(
            citation_key=event.citation_key,
            action="skipped_duplicate",
            detail=(
                f"{len(matches)} existing Notion pages share this citation key "
                f"({page_ids}); needs human repair"
            ),
        )

    if not matches:
        return _create_citation(client, data_source_id, event, dry_run)

    return _update_citation(client, matches[0], event, dry_run)


def _create_citation(
    client: NotionClient, data_source_id: str, event: CitationEvent, dry_run: bool
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
            citation_key=event.citation_key, action="error", detail=f"create failed: {exc}"
        )
    return CitationUpsertResult(
        citation_key=event.citation_key, action="created", page_id=page.get("id")
    )


def _update_citation(
    client: NotionClient, existing_page: dict, event: CitationEvent, dry_run: bool
) -> CitationUpsertResult:
    page_id = existing_page.get("id")
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
            citation_key=event.citation_key, action="error", detail=f"update failed: {exc}"
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
