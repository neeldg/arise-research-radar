#!/usr/bin/env python3
"""CLI: fetch recent OpenAlex publications for the verified ARISE roster.

    python scripts/run_roster.py --roster config/roster.yaml --days-back 90

Optionally sync results into a Notion data source (requires NOTION_TOKEN and
NOTION_DATA_SOURCE_ID; see .env.example):

    python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --notion-dry-run
    python scripts/run_roster.py --roster config/roster.yaml --days-back 730 --write-notion
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from arise_radar.models import Researcher
from arise_radar.output import (
    format_exclusion_summary,
    format_notion_summary,
    format_publications,
    format_relevance_summary,
    format_skip_warning,
)
from arise_radar.relevance import apply_relevance_filter
from arise_radar.roster import RosterError, load_roster
from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    load_notion_config,
    upsert_publication,
)
from arise_radar.sources.openalex import (
    OpenAlexClient,
    OpenAlexError,
    fetch_researcher_publications,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch recent publications for verified ARISE roster members via OpenAlex."
    )
    parser.add_argument("--roster", type=Path, required=True, help="Path to roster.yaml")
    parser.add_argument("--days-back", type=int, default=90, help="Lookback window in days")
    parser.add_argument(
        "--show-excluded",
        action="store_true",
        help="Print full details for publications excluded by the relevance filter",
    )
    parser.add_argument(
        "--write-notion",
        action="store_true",
        help="Create/update rows in the configured Notion data source",
    )
    parser.add_argument(
        "--notion-dry-run",
        action="store_true",
        help="Show what would be created/updated in Notion without writing",
    )
    return parser.parse_args(argv)


def _split_queryable(researchers: list[Researcher]) -> tuple[list[Researcher], list[str]]:
    queryable: list[Researcher] = []
    warnings: list[str] = []
    for researcher in researchers:
        if not researcher.active:
            warnings.append(format_skip_warning(researcher, "marked inactive"))
            continue
        if not researcher.openalex_id:
            warnings.append(format_skip_warning(researcher, "no verified OpenAlex ID"))
            continue
        queryable.append(researcher)
    return queryable, warnings


def main(
    argv: list[str] | None = None,
    *,
    client: OpenAlexClient | None = None,
    notion_client: NotionClient | None = None,
) -> int:
    args = parse_args(argv)

    if args.write_notion and args.notion_dry_run:
        print("ERROR: --write-notion and --notion-dry-run cannot be used together", file=sys.stderr)
        return 1

    notion_enabled = args.write_notion or args.notion_dry_run
    active_notion_client: NotionClient | None = None
    notion_data_source_id: str | None = None
    owns_notion_client = False

    if notion_enabled:
        try:
            notion_config = load_notion_config()
        except NotionConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        notion_data_source_id = notion_config.data_source_id
        owns_notion_client = notion_client is None
        active_notion_client = notion_client or NotionClient(
            token=notion_config.token.get_secret_value()
        )

    try:
        researchers = load_roster(args.roster)
    except RosterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    queryable, warnings = _split_queryable(researchers)
    for warning in warnings:
        print(warning, file=sys.stderr)

    if not queryable:
        print("No verified researchers to query.", file=sys.stderr)
        return 0

    owns_client = client is None
    active_client = client or OpenAlexClient()
    run_date = date.today()

    try:
        for researcher in queryable:
            try:
                publications = fetch_researcher_publications(
                    active_client, researcher, args.days_back
                )
            except OpenAlexError as exc:
                print(f"ERROR: {researcher.name} — {exc}", file=sys.stderr)
                continue

            decisions = apply_relevance_filter(researcher, publications)
            visible = [(pub, d) for pub, d in decisions if d.status != "exclude"]
            excluded = [(pub, d) for pub, d in decisions if d.status == "exclude"]
            keep_count = sum(1 for _, d in decisions if d.status == "keep")
            uncertain_count = sum(1 for _, d in decisions if d.status == "uncertain")

            print(format_publications(visible))
            print()
            print(format_relevance_summary(keep_count, uncertain_count, len(excluded)))
            exclusion_text = format_exclusion_summary(excluded, show_details=args.show_excluded)
            if exclusion_text:
                print()
                print(exclusion_text)

            if notion_enabled:
                assert active_notion_client is not None
                assert notion_data_source_id is not None
                notion_results = [
                    upsert_publication(
                        active_notion_client,
                        notion_data_source_id,
                        pub,
                        decision,
                        detected_date=run_date,
                        dry_run=args.notion_dry_run,
                    )
                    for pub, decision in visible
                ]
                print()
                print(format_notion_summary(notion_results))

            print()
    finally:
        if owns_client:
            active_client.close()
        if owns_notion_client and active_notion_client is not None:
            active_notion_client.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
