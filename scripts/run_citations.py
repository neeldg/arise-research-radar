#!/usr/bin/env python3
"""CLI: detect citation relationships to tracked ARISE papers (Phase 1 of
citation monitoring) using OpenAlex, and sync them idempotently into a
separate Notion Citation Events data source.

    python scripts/run_citations.py --baseline
    python scripts/run_citations.py --baseline --write-notion
    python scripts/run_citations.py --since-days 30 --write-notion

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
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

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
    CitationUpsertResult,
    NotionConfigError,
    TrackedWorksResult,
    extract_tracked_works,
    load_notion_citations_config,
    upsert_citation_event,
)
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError

_EXISTING_ACTIONS = {"updated", "dry_run_update"}
_NEW_ACTIONS = {"created", "dry_run_create"}


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
) -> CitationRunSummary:
    existing_rows = sum(1 for r in results if r.action in _EXISTING_ACTIONS)
    new_rows = sum(1 for r in results if r.action in _NEW_ACTIONS)
    write_errors = sum(1 for r in results if r.action == "error")
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
        errors=write_errors + len(discovery.batch_errors),
    )


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

        dry_run = not args.write_notion
        results = [
            upsert_citation_event(
                active_notion, notion_config.citations_data_source_id, event, dry_run=dry_run
            )
            for event in discovery.events
        ]
        print(format_citation_notion_summary(results))
        print()

        summary = _compute_citation_run_summary(
            tracked_count=len(tracked_works),
            skipped_count=tracked_result.skipped_no_openalex_id,
            discovery=discovery,
            results=results,
        )
        print(format_citation_run_summary(summary))

        return 0
    finally:
        if owns_notion:
            active_notion.close()
        if owns_openalex and active_openalex is not None:
            active_openalex.close()


if __name__ == "__main__":
    raise SystemExit(main())
