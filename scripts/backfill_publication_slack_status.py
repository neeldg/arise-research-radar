#!/usr/bin/env python3
"""One-time (but safely re-runnable) backfill: mark every historical
publication row's Slack Status as Suppressed, so the new-publication Slack
notifier (scripts/send_publication_notifications.py) never treats the
existing backlog as eligible for delivery.

    python scripts/backfill_publication_slack_status.py
    python scripts/backfill_publication_slack_status.py --write-notion

Requires NOTION_TOKEN and NOTION_DATA_SOURCE_ID (see .env.example). Run
scripts/update_notion_schema.py first if Slack Status doesn't exist on the
data source yet.

Only ever changes a row whose Slack Status is currently empty (i.e. it
predates the Slack Status property entirely, or predates this backfill).
A row that already has ANY value -- Pending, Suppressed, Sent, or Failed --
is left completely untouched; this is what makes rerunning safe and
idempotent, and what guarantees this script can never revert a live row a
human or the notifier has already acted on back to Suppressed.

Bulk-reads the whole data source once (NotionClient.iter_data_source_pages)
rather than querying per row, then writes only the rows that actually need
it, straight to each row's already-known page ID.

Default: dry-run -- reads every row and prints exactly which ones would
change, but makes no write requests. --write-notion is required to apply it.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import BaseModel, Field

from arise_radar.sinks.notion import (
    NOTION_SLACK_STATUS_SUPPRESSED,
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionProperties,
    load_notion_config,
    read_select,
    read_title,
)


class BackfillRow(BaseModel):
    page_id: str
    title: str


class BackfillPlan(BaseModel):
    rows_scanned: int = 0
    already_set: int = 0
    to_backfill: list[BackfillRow] = Field(default_factory=list)
    malformed_rows: int = 0


def plan_backfill(pages: list[dict]) -> BackfillPlan:
    """Pure diff over already-fetched publication pages: which rows have no
    Slack Status yet (candidates for backfill) vs. already have one (left
    alone, whatever it is). No I/O."""
    rows_scanned = 0
    already_set = 0
    malformed = 0
    to_backfill: list[BackfillRow] = []

    for page in pages:
        rows_scanned += 1
        current_status = read_select(page, NotionProperties.SLACK_STATUS)
        if current_status:
            already_set += 1
            continue
        page_id = page.get("id")
        if not page_id:
            malformed += 1
            continue
        to_backfill.append(BackfillRow(page_id=page_id, title=read_title(page) or "(untitled)"))

    return BackfillPlan(
        rows_scanned=rows_scanned,
        already_set=already_set,
        to_backfill=to_backfill,
        malformed_rows=malformed,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill Slack Status=Suppressed on every historical publication row "
            "that doesn't have a Slack Status yet -- never touches a row that already "
            "has one, of any value."
        )
    )
    parser.add_argument(
        "--write-notion",
        action="store_true",
        help="Actually write the backfill; default is a dry-run preview only",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, client: NotionClient | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_notion_config()
    except NotionConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    owns_client = client is None
    active_client = client or NotionClient(token=config.token.get_secret_value())

    try:
        try:
            pages = list(active_client.iter_data_source_pages(config.data_source_id))
        except NotionError as exc:
            print(f"ERROR: could not read publication rows: {exc}", file=sys.stderr)
            return 1

        plan = plan_backfill(pages)

        print(f"Rows scanned: {plan.rows_scanned}")
        print(f"Already have a Slack Status (left untouched): {plan.already_set}")
        print(f"Malformed rows (no page id, skipped): {plan.malformed_rows}")
        print(f"Would backfill to Suppressed: {len(plan.to_backfill)}")
        print()

        if not plan.to_backfill:
            print("Nothing to backfill.")
            return 0

        for row in plan.to_backfill:
            print(f"  - {row.title} ({row.page_id})")
        print()

        if not args.write_notion:
            print("Dry run: no write requests were made. Pass --write-notion to apply.")
            return 0

        errors = 0
        for row in plan.to_backfill:
            try:
                active_client.update_page(
                    row.page_id,
                    {
                        NotionProperties.SLACK_STATUS: {
                            "select": {"name": NOTION_SLACK_STATUS_SUPPRESSED}
                        }
                    },
                )
            except NotionError as exc:
                errors += 1
                print(f"ERROR: failed to backfill {row.page_id}: {exc}", file=sys.stderr)

        print(f"Backfilled: {len(plan.to_backfill) - errors}")
        print(f"Errors: {errors}")
        return 1 if errors else 0
    finally:
        if owns_client:
            active_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
