#!/usr/bin/env python3
"""CLI: detect citation relationships to tracked ARISE papers (Phase 1 of
citation monitoring) using OpenAlex, and sync them idempotently into a
separate Notion Citation Events data source.

    python scripts/run_citations.py --baseline
    python scripts/run_citations.py --baseline --write-notion
    python scripts/run_citations.py --since-days 30 --write-notion
    python -u scripts/run_citations.py --baseline --error-report logs/errors.json | tee logs/run.log

Reads tracked ARISE papers from the existing publications data source
(NOTION_DATA_SOURCE_ID) — only rows with a valid OpenAlex work ID are used;
this never searches OpenAlex by researcher name. Requires NOTION_TOKEN,
NOTION_DATA_SOURCE_ID, and NOTION_CITATIONS_DATA_SOURCE_ID (see
.env.example). Every mode reads tracked papers and queries OpenAlex (or a
--fixture-file) for real; --write-notion is what gates whether discovered
citation events are persisted — without it, results are previewed only.

--baseline vs incremental:
  --baseline marks every discovered citation Baseline=true, Slack
  Status=Suppressed, and sends no Slack messages (Phase 1 never sends Slack
  messages regardless of this flag — Slack delivery is a later phase). Use
  it once, before turning on the incremental schedule, so historical
  citations don't all look like brand-new events.

  Without --baseline (the normal incremental mode), newly discovered
  citation keys get Baseline=false, Slack Status=Pending. Either way,
  resyncing an *existing* citation key never resets Baseline, Slack Status,
  Slack Timestamp, Review Status, Citation Relationship, or Relationship
  Evidence — those are set once at creation and are otherwise human-owned or
  reserved for a later phase (see sinks/notion_citations.py).

Citation-key matching is a single bulk read, not one Notion query per
citation edge — see sinks/notion_citations.py's module docstring for why
this replaced the original per-event `query_data_source_by_rich_text` call.
A failed citation-data-source read is fatal (the run stops before touching
any event): proceeding with an empty/partial index would risk creating
duplicate rows for citation keys that already exist in Notion.

Errors are reported as separate, named categories (OpenAlex batch errors,
Notion citation-data-source read errors, Notion create errors, Notion update
errors, duplicate stored Citation Keys, malformed stored rows) rather than
one combined count — see arise_radar.output.format_citation_run_summary.
Pass --error-report PATH to also write every individual error as structured
JSON.

Run with `python -u ... | tee logfile` (or set PYTHONUNBUFFERED=1) for a
live, useful log: every stage message and progress line below is printed
with flush=True, but Python still fully block-buffers stdout by default when
it isn't a TTY (i.e. whenever piped or redirected) — `-u`/PYTHONUNBUFFERED
disables that buffering at the interpreter level so a `tee`'d or redirected
run shows output as it happens instead of only at exit (or never, if the
process is interrupted before exit).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel

from arise_radar.citations import (
    DEFAULT_CITATION_BATCH_SIZE,
    CitationDiscoveryResult,
    TrackedWork,
    discover_citation_edges,
    discover_citation_edges_from_fixture,
)
from arise_radar.output import (
    CitationRunSummary,
    format_citation_notion_summary,
    format_citation_run_summary,
)
from arise_radar.sinks.notion import NotionClient, NotionError
from arise_radar.sinks.notion_citations import (
    CitationRowIndex,
    CitationUpsertResult,
    NotionConfigError,
    TrackedWorksResult,
    extract_tracked_works,
    load_citation_row_index,
    load_notion_citations_config,
    upsert_citation_event,
)
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError

_EXISTING_ACTIONS = {"updated", "dry_run_update"}
_NEW_ACTIONS = {"created", "dry_run_create"}

# "Print progress every 100 or 250 citation edges" — 100 gives feedback
# sooner on a long run without being noisy for the common (much smaller)
# incremental run; the final "processed N/N" line always prints regardless
# of this interval (see the processing loop in main()).
PROGRESS_INTERVAL = 100


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Detect citations to tracked ARISE papers via OpenAlex and sync them "
            "idempotently into the Citation Events Notion data source."
        )
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Historical baseline run: Baseline=true, Slack Status=Suppressed, no Slack messages",
    )
    parser.add_argument(
        "--notion-dry-run",
        action="store_true",
        help="Explicit alias for the default: preview only, never write to Notion",
    )
    parser.add_argument(
        "--write-notion", action="store_true", help="Persist discovered citation events to Notion"
    )
    parser.add_argument(
        "--fixture-file",
        type=Path,
        default=None,
        help="Replay tracked + citing works from this JSON fixture (no OpenAlex/Notion reads)",
    )
    parser.add_argument(
        "--limit-tracked-works",
        type=int,
        default=None,
        help="Cap the number of tracked papers used",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="Only consider citing works published in the last N days (omit for full history)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_CITATION_BATCH_SIZE,
        help=f"Tracked work IDs per OR-filter batch (default {DEFAULT_CITATION_BATCH_SIZE})",
    )
    parser.add_argument(
        "--error-report",
        type=Path,
        default=None,
        help="Write every individual error (by category) to this path as structured JSON",
    )
    return parser.parse_args(argv)


def _tracked_works_result_from_fixture(fixture: dict) -> TrackedWorksResult:
    entries = fixture.get("tracked_works", [])
    tracked = [TrackedWork(**entry) for entry in entries]
    return TrackedWorksResult(
        tracked=tracked, total_inspected=len(tracked), skipped_no_openalex_id=0
    )


def _compute_citation_run_summary(
    *,
    tracked_count: int,
    skipped_count: int,
    discovery: CitationDiscoveryResult,
    results: list[CitationUpsertResult],
    index: CitationRowIndex,
    notion_read_errors: int = 0,
) -> CitationRunSummary:
    existing_rows = sum(1 for r in results if r.action in _EXISTING_ACTIONS)
    new_rows = sum(1 for r in results if r.action in _NEW_ACTIONS)
    create_errors = sum(1 for r in results if r.action == "error" and r.stage == "create")
    update_errors = sum(1 for r in results if r.action == "error" and r.stage == "update")
    baseline_suppressed = sum(1 for e in discovery.events if e.slack_status == "Suppressed")
    slack_pending = sum(1 for e in discovery.events if e.slack_status == "Pending")

    return CitationRunSummary(
        tracked_arise_papers=tracked_count,
        skipped_no_openalex_id=skipped_count,
        raw_citing_work_matches=discovery.raw_citing_work_matches,
        unique_citation_edges=len(discovery.events),
        existing_citation_rows=existing_rows,
        proposed_new_citation_rows=new_rows,
        baseline_suppressed_rows=baseline_suppressed,
        new_slack_pending_rows=slack_pending,
        openalex_batch_errors=len(discovery.batch_errors),
        notion_read_errors=notion_read_errors,
        notion_create_errors=create_errors,
        notion_update_errors=update_errors,
        duplicate_stored_citation_keys=len(index.duplicate_keys),
        malformed_stored_rows=index.malformed_rows,
    )


class CitationErrorEntry(BaseModel):
    """One row of the --error-report JSON. citation_key/citing_openalex_id/
    cited_openalex_id are None for errors that aren't tied to a single
    CitationEvent (an OpenAlex batch failure, the bulk Notion read, or the
    aggregate malformed-row count)."""

    citation_key: str | None = None
    citing_openalex_id: str | None = None
    cited_openalex_id: str | None = None
    stage: str
    status_code: int | None = None
    error: str


def _build_error_report_entries(
    *,
    discovery: CitationDiscoveryResult,
    results: list[CitationUpsertResult],
    index: CitationRowIndex,
    notion_read_error: str | None,
) -> list[CitationErrorEntry]:
    """Pure assembly of every individual error into one flat, categorized
    list — no I/O; main() writes it to --error-report. Every category from
    the run summary is represented here at the individual-error level where
    one exists."""
    entries: list[CitationErrorEntry] = []

    for batch_error in discovery.batch_errors:
        entries.append(CitationErrorEntry(stage="openalex_batch", error=batch_error))

    if notion_read_error:
        entries.append(CitationErrorEntry(stage="notion_read", error=notion_read_error))

    for event, result in zip(discovery.events, results, strict=True):
        if result.action != "error":
            continue
        entries.append(
            CitationErrorEntry(
                citation_key=event.citation_key,
                citing_openalex_id=event.citing_openalex_id,
                cited_openalex_id=event.cited_openalex_id,
                stage=f"notion_{result.stage}" if result.stage else "notion_unknown",
                status_code=result.status_code,
                error=result.detail,
            )
        )

    for key, page_ids in index.duplicate_keys.items():
        entries.append(
            CitationErrorEntry(
                citation_key=key,
                stage="duplicate_key",
                error=(
                    f"{len(page_ids)} existing Notion pages share this citation key: "
                    f"{', '.join(page_ids)}"
                ),
            )
        )

    if index.malformed_rows:
        entries.append(
            CitationErrorEntry(
                stage="malformed_row",
                error=(
                    f"{index.malformed_rows} stored citation row(s) have no readable "
                    "Citation Key and/or page id"
                ),
            )
        )

    return entries


def main(
    argv: list[str] | None = None,
    *,
    notion_client: NotionClient | None = None,
    openalex_client: OpenAlexClient | None = None,
) -> int:
    args = parse_args(argv)

    if args.write_notion and args.notion_dry_run:
        print("ERROR: --write-notion and --notion-dry-run cannot be used together", file=sys.stderr)
        return 1

    try:
        notion_config = load_notion_citations_config()
    except NotionConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    owns_notion = notion_client is None
    active_notion = notion_client or NotionClient(token=notion_config.token.get_secret_value())
    owns_openalex = False
    active_openalex: OpenAlexClient | None = None

    detected_date = date.today()
    since = date.today() - timedelta(days=args.since_days) if args.since_days is not None else None

    try:
        fixture: dict | None = None
        if args.fixture_file:
            fixture = json.loads(args.fixture_file.read_text())
            tracked_result = _tracked_works_result_from_fixture(fixture)
        else:
            print("Stage: reading tracked publications...", flush=True)
            try:
                pages = list(
                    active_notion.iter_data_source_pages(notion_config.publications_data_source_id)
                )
            except NotionError as exc:
                print(f"ERROR: could not read tracked publications: {exc}", file=sys.stderr)
                return 1
            tracked_result = extract_tracked_works(pages)

        tracked_works = tracked_result.tracked
        if args.limit_tracked_works is not None:
            tracked_works = tracked_works[: args.limit_tracked_works]

        print(f"Total publication rows inspected: {tracked_result.total_inspected}")
        print(f"Tracked ARISE papers with OpenAlex IDs: {len(tracked_works)}")
        print(f"Skipped (no OpenAlex ID): {tracked_result.skipped_no_openalex_id}")
        print()

        if not tracked_works:
            print("No tracked papers with OpenAlex IDs -- nothing to discover.", file=sys.stderr)
            return 0

        if fixture is not None:
            discovery = discover_citation_edges_from_fixture(
                tracked_works,
                fixture.get("citing_works", []),
                detected_date=detected_date,
                is_baseline=args.baseline,
            )
        else:
            print("Stage: retrieving OpenAlex citations...", flush=True)
            owns_openalex = openalex_client is None
            active_openalex = openalex_client or OpenAlexClient()
            try:
                discovery = discover_citation_edges(
                    active_openalex,
                    tracked_works,
                    detected_date=detected_date,
                    is_baseline=args.baseline,
                    batch_size=args.batch_size,
                    since=since,
                )
            except OpenAlexError as exc:
                print(f"ERROR: OpenAlex request failed: {exc}", file=sys.stderr)
                return 1

        for batch_error in discovery.batch_errors:
            print(f"ERROR: OpenAlex batch failed: {batch_error}", file=sys.stderr)

        print(f"OpenAlex batches queried: {discovery.batches_queried}")
        print(f"Raw citing-work matches: {discovery.raw_citing_work_matches}")
        print(f"Unique citation edges: {len(discovery.events)}")
        print()

        print("Stage: loading existing citation rows...", flush=True)
        notion_read_error: str | None = None
        try:
            citation_pages = list(
                active_notion.iter_data_source_pages(notion_config.citations_data_source_id)
            )
        except NotionError as exc:
            notion_read_error = str(exc)
            print(f"ERROR: could not read existing citation rows: {exc}", file=sys.stderr)
            return 1
        index = load_citation_row_index(citation_pages)

        print(
            f"Existing citation rows loaded: {index.total_rows_loaded} "
            f"({len(index.key_to_page_id)} usable, {len(index.duplicate_keys)} duplicate "
            f"key(s), {index.malformed_rows} malformed)"
        )
        print()

        print(f"Stage: processing {len(discovery.events)} citation edge(s)...", flush=True)
        dry_run = not args.write_notion
        results: list[CitationUpsertResult] = []
        total_events = len(discovery.events)
        for i, event in enumerate(discovery.events, start=1):
            results.append(
                upsert_citation_event(
                    active_notion,
                    notion_config.citations_data_source_id,
                    event,
                    index,
                    dry_run=dry_run,
                )
            )
            if i % PROGRESS_INTERVAL == 0 or i == total_events:
                print(f"  Processed {i}/{total_events} citation edge(s)...", flush=True)

        print(format_citation_notion_summary(results))
        print()

        summary = _compute_citation_run_summary(
            tracked_count=len(tracked_works),
            skipped_count=tracked_result.skipped_no_openalex_id,
            discovery=discovery,
            results=results,
            index=index,
        )
        print(format_citation_run_summary(summary))

        if args.error_report is not None:
            entries = _build_error_report_entries(
                discovery=discovery,
                results=results,
                index=index,
                notion_read_error=notion_read_error,
            )
            args.error_report.write_text(
                json.dumps([entry.model_dump() for entry in entries], indent=2)
            )
            print()
            print(f"Error report written: {args.error_report} ({len(entries)} entries)")

        return 0
    finally:
        if owns_notion:
            active_notion.close()
        if owns_openalex and active_openalex is not None:
            active_openalex.close()


if __name__ == "__main__":
    raise SystemExit(main())
