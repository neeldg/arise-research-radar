"""Terminal-readable formatting for normalized publications, relevance decisions,
and roster warnings.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from arise_radar.models import NormalizedPublication, Researcher
from arise_radar.relevance import RelevanceDecision
from arise_radar.sinks.notion import NotionUpsertResult
from arise_radar.sinks.notion_citations import CitationUpsertResult

NOTION_ACTION_LABELS: dict[str, str] = {
    "created": "Created",
    "dry_run_create": "Would create",
    "updated": "Updated",
    "dry_run_update": "Would update",
    "skipped_duplicate": "Skipped (duplicate canonical key)",
    "error": "Errors",
}

_NOTION_DETAIL_ACTIONS = {"dry_run_create", "dry_run_update", "skipped_duplicate", "error"}


def format_skip_warning(researcher: Researcher, reason: str) -> str:
    return f"WARNING: skipping {researcher.name} ({researcher.id}) — {reason}"


def format_publications(entries: Sequence[tuple[NormalizedPublication, RelevanceDecision]]) -> str:
    if not entries:
        return "No publications found in the requested window."

    blocks = []
    for pub, decision in entries:
        pub_date = pub.publication_date.isoformat() if pub.publication_date else "unknown"
        header = pub.researcher_name
        if decision.status == "uncertain":
            header = f"{header}  [UNCERTAIN — kept: {decision.reason}]"
        blocks.append(
            "\n".join(
                [
                    header,
                    f"  Title:          {pub.title}",
                    f"  Date:           {pub_date}",
                    f"  DOI:            {pub.doi or '—'}",
                    f"  OpenAlex ID:    {pub.openalex_id}",
                    f"  Canonical key:  {pub.canonical_key}",
                ]
            )
        )
    return "\n\n".join(blocks)


def format_relevance_summary(kept: int, uncertain: int, excluded: int) -> str:
    return (
        f"Kept: {kept}\nUncertain but kept: {uncertain}\nClearly unrelated and excluded: {excluded}"
    )


def format_exclusion_summary(
    excluded: Sequence[tuple[NormalizedPublication, RelevanceDecision]],
    *,
    show_details: bool,
) -> str:
    if not excluded:
        return ""

    if not show_details:
        return (
            f"Excluded ({len(excluded)}) — rerun with --show-excluded "
            "to see titles and exclusion reasons."
        )

    blocks = [f"Excluded ({len(excluded)}):"]
    for pub, decision in excluded:
        pub_date = pub.publication_date.isoformat() if pub.publication_date else "unknown"
        blocks.append(
            "\n".join(
                [
                    f"  {pub.researcher_name}",
                    f"    Title:            {pub.title}",
                    f"    Date:             {pub_date}",
                    f"    OpenAlex ID:      {pub.openalex_id}",
                    f"    Exclusion reason: {decision.reason}",
                    f"    Matched evidence: {', '.join(decision.matched_terms) or '—'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def format_notion_summary(results: Sequence[NotionUpsertResult]) -> str:
    if not results:
        return "Notion: nothing to write."

    counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1

    lines = ["Notion:"]
    for action in (
        "created",
        "dry_run_create",
        "updated",
        "dry_run_update",
        "skipped_duplicate",
        "error",
    ):
        if counts.get(action):
            lines.append(f"  {NOTION_ACTION_LABELS[action]}: {counts[action]}")

    for result in results:
        if result.action in _NOTION_DETAIL_ACTIONS:
            lines.append(f"    [{result.action}] {result.canonical_key}: {result.detail}")

    return "\n".join(lines)


class RunSummary(BaseModel):
    """One final, run-wide aggregate — printed once, after every researcher
    has been processed and the run has been deduplicated. Distinct from the
    per-researcher "Would create"/"Would update" lines above: those exist per
    canonical key *after* same-run dedup (see arise_radar.dedupe), so this
    summary never makes repeated per-author lines for a shared paper look
    like duplicate live writes.
    """

    raw_author_work_matches: int
    unique_canonical_keys: int
    existing_rows: int | None = None
    proposed_new_rows: int | None = None
    shared_author_works: int
    # These three partition every canonical row by automatic-drafting
    # eligibility. `duplicate_flagged_held_for_review` and
    # `non_standard_held_for_review` are not mutually exclusive — a row can
    # be both a non-standard work type *and* a probable version duplicate at
    # once — so they may overlap and need not sum with
    # `standard_draft_eligible_works` to `unique_canonical_keys`.
    standard_draft_eligible_works: int
    duplicate_flagged_held_for_review: int
    non_standard_held_for_review: int


def format_run_summary(summary: RunSummary) -> str:
    lines = [
        "Run summary:",
        f"  Raw author-work matches:              {summary.raw_author_work_matches}",
        f"  Unique canonical keys:                {summary.unique_canonical_keys}",
    ]
    if summary.existing_rows is not None:
        lines.append(f"  Existing rows:                         {summary.existing_rows}")
    if summary.proposed_new_rows is not None:
        lines.append(f"  Proposed new rows:                     {summary.proposed_new_rows}")
    lines.extend(
        [
            f"  Shared-author works:                   {summary.shared_author_works}",
            f"  Standard draft-eligible works:         {summary.standard_draft_eligible_works}",
            f"  Duplicate-flagged (held for review):   {summary.duplicate_flagged_held_for_review}",
            f"  Non-standard (held for review):        {summary.non_standard_held_for_review}",
        ]
    )
    return "\n".join(lines)


_CITATION_ACTION_LABELS: dict[str, str] = {
    "created": "Created",
    "dry_run_create": "Would create",
    "updated": "Updated",
    "dry_run_update": "Would update",
    "skipped_duplicate": "Skipped (duplicate citation key)",
    "error": "Errors",
}
_CITATION_DETAIL_ACTIONS = {"dry_run_create", "dry_run_update", "skipped_duplicate", "error"}


def format_citation_notion_summary(results: Sequence[CitationUpsertResult]) -> str:
    """The citation-events equivalent of format_notion_summary above, for
    the separate Citation Events data source (see sinks/notion_citations.py)."""
    if not results:
        return "Citations: nothing to write."

    counts: dict[str, int] = {}
    for result in results:
        counts[result.action] = counts.get(result.action, 0) + 1

    lines = ["Citations:"]
    for action in (
        "created",
        "dry_run_create",
        "updated",
        "dry_run_update",
        "skipped_duplicate",
        "error",
    ):
        if counts.get(action):
            lines.append(f"  {_CITATION_ACTION_LABELS[action]}: {counts[action]}")

    for result in results:
        if result.action in _CITATION_DETAIL_ACTIONS:
            lines.append(f"    [{result.action}] {result.citation_key}: {result.detail}")

    return "\n".join(lines)


class CitationRunSummary(BaseModel):
    """One final, run-wide aggregate for scripts/run_citations.py — printed
    once, after tracked-paper reading, OpenAlex discovery, and the Notion
    citations sync are all complete."""

    tracked_arise_papers: int
    skipped_no_openalex_id: int
    raw_citing_work_matches: int
    unique_citation_edges: int
    existing_citation_rows: int
    proposed_new_citation_rows: int
    baseline_suppressed_rows: int
    new_slack_pending_rows: int
    errors: int


def format_citation_run_summary(summary: CitationRunSummary) -> str:
    return "\n".join(
        [
            "Citation run summary:",
            f"  Tracked ARISE papers:                  {summary.tracked_arise_papers}",
            f"  Skipped (no OpenAlex ID):               {summary.skipped_no_openalex_id}",
            f"  Raw citing-work matches:                {summary.raw_citing_work_matches}",
            f"  Unique citation edges:                  {summary.unique_citation_edges}",
            f"  Existing citation rows:                 {summary.existing_citation_rows}",
            f"  Proposed new citation rows:              {summary.proposed_new_citation_rows}",
            f"  Baseline-suppressed rows:                {summary.baseline_suppressed_rows}",
            f"  New Slack-pending rows:                  {summary.new_slack_pending_rows}",
            f"  Errors:                                  {summary.errors}",
        ]
    )
