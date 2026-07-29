#!/usr/bin/env python3
"""CLI: post pending ARISE citation events to the Slack signals channel and
record the delivery result back onto the Citation Events Notion row.

    python scripts/send_citation_notifications.py
    python scripts/send_citation_notifications.py --send
    python scripts/send_citation_notifications.py --send --retry-failed
    python scripts/send_citation_notifications.py --send --citation-key citation:W900:W100
    python scripts/send_citation_notifications.py --send --error-report logs/citation_errors.json

Requires NOTION_TOKEN, NOTION_CITATIONS_DATA_SOURCE_ID, SLACK_BOT_TOKEN, and
SLACK_SIGNALS_CHANNEL_ID (see .env.example). Never reads the publications
data source and never posts to the papers channel.

Candidate selection (see arise_radar.notifications.citations.scan_citation_rows):
a row is only ever a candidate when Baseline = false AND Slack Status is
Pending (or, with --retry-failed, also Failed). Suppressed and Sent rows are
never candidates, regardless of flags. A Citation Key shared by more than
one stored row is treated as a data-integrity problem: every copy is
skipped and reported, never posted.

Safe default: without --send, this is a dry run — it reads Notion and
prints exactly what it would post (rendered message previews), but makes no
Slack calls and no Notion writes. --send is required for live delivery.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from arise_radar.notifications.citations import (
    CitationDeliveryResult,
    CitationNotificationConfigError,
    CitationScanResult,
    deliver_citation_notification,
    load_citation_notification_config,
    scan_citation_rows,
    select_candidates,
)
from arise_radar.sinks.notion import NotionClient, NotionError
from arise_radar.sinks.slack import SlackClient


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Post pending ARISE citation events to Slack and record the delivery "
            "result on the Citation Events Notion row."
        )
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually post to Slack and write to Notion; default is dry-run preview only",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Cap the number of candidates processed"
    )
    parser.add_argument(
        "--citation-key", type=str, default=None, help="Only process this one Citation Key"
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also process rows with Slack Status = Failed (in addition to Pending)",
    )
    parser.add_argument(
        "--error-report",
        type=Path,
        default=None,
        help="Write every delivery/duplicate-key problem to this path as structured JSON",
    )
    return parser.parse_args(argv)


class CitationNotificationErrorEntry(BaseModel):
    citation_key: str | None = None
    notion_page_id: str | None = None
    stage: str
    slack_error_code: str | None = None
    status_code: int | None = None
    error: str


def _build_error_report_entries(
    scan: CitationScanResult, results: list[CitationDeliveryResult]
) -> list[CitationNotificationErrorEntry]:
    """Pure assembly of every individual problem into one flat, categorized
    list — no I/O; main() writes it to --error-report."""
    entries: list[CitationNotificationErrorEntry] = []

    for result in results:
        if result.action != "failed":
            continue
        entries.append(
            CitationNotificationErrorEntry(
                citation_key=result.citation_key,
                notion_page_id=result.page_id,
                stage=result.stage or "unknown",
                slack_error_code=result.slack_error_code,
                status_code=result.status_code,
                error=result.detail,
            )
        )

    for key, page_ids in scan.duplicate_keys.items():
        entries.append(
            CitationNotificationErrorEntry(
                citation_key=key,
                stage="duplicate_key",
                error=(
                    f"{len(page_ids)} existing Notion pages share this citation key: "
                    f"{', '.join(page_ids)}"
                ),
            )
        )

    return entries


def main(
    argv: list[str] | None = None,
    *,
    notion_client: NotionClient | None = None,
    slack_client: SlackClient | None = None,
) -> int:
    args = parse_args(argv)

    try:
        config = load_citation_notification_config()
    except CitationNotificationConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    owns_notion = notion_client is None
    active_notion = notion_client or NotionClient(token=config.notion_token.get_secret_value())
    owns_slack = slack_client is None
    active_slack = slack_client or SlackClient(token=config.slack_bot_token.get_secret_value())

    try:
        print("Stage: reading citation rows...", flush=True)
        try:
            pages = list(active_notion.iter_data_source_pages(config.citations_data_source_id))
        except NotionError as exc:
            print(f"ERROR: could not read citation rows: {exc}", file=sys.stderr)
            return 1

        scan = scan_citation_rows(pages)
        candidates = select_candidates(
            scan,
            retry_failed=args.retry_failed,
            citation_key=args.citation_key,
            limit=args.limit,
        )

        print(f"Rows scanned: {scan.rows_scanned}")
        print(f"Pending candidates: {len(scan.pending)}")
        print(
            "Failed candidates selected for retry: "
            f"{len(scan.failed) if args.retry_failed else 0} (of {len(scan.failed)} Failed)"
        )
        print(f"Suppressed/Sent rows skipped: {scan.suppressed_or_sent_skipped}")
        print(f"Baseline rows skipped: {scan.baseline_skipped}")
        print(f"Duplicate Citation Keys (all copies skipped): {len(scan.duplicate_keys)}")
        print(f"Malformed rows (no Citation Key/page id): {scan.malformed_rows}")
        print(f"Selected this run: {len(candidates)}")
        print()

        dry_run = not args.send
        print(
            f"Stage: {'previewing' if dry_run else 'delivering'} {len(candidates)} row(s)...",
            flush=True,
        )

        results: list[CitationDeliveryResult] = []
        sent = 0
        failed = 0
        previewed = 0
        for row in candidates:
            result = deliver_citation_notification(
                active_slack,
                active_notion,
                config.citations_data_source_id,
                row,
                channel_id=config.signals_channel_id,
                dry_run=dry_run,
            )
            results.append(result)
            if result.action == "sent":
                sent += 1
                print(f"[sent] {row.citation_key} -> ts={result.ts}")
            elif result.action == "failed":
                failed += 1
                print(
                    f"[FAILED] {row.citation_key} ({result.stage}): {result.detail}",
                    file=sys.stderr,
                )
            else:
                previewed += 1
                print(f"--- DRY RUN PREVIEW: {row.citation_key} ---")
                print(result.detail)
                print()

        print()
        print(f"Sent: {sent}")
        print(f"Failed: {failed}")
        print(f"Dry-run previews shown: {previewed}")
        if dry_run:
            print("Dry run: no Slack messages were sent and no Notion writes were made.")

        if args.error_report is not None:
            entries = _build_error_report_entries(scan, results)
            args.error_report.write_text(
                json.dumps([entry.model_dump() for entry in entries], indent=2)
            )
            print()
            print(f"Error report written: {args.error_report} ({len(entries)} entries)")

        return 1 if failed else 0
    finally:
        if owns_notion:
            active_notion.close()
        if owns_slack:
            active_slack.close()


if __name__ == "__main__":
    raise SystemExit(main())
