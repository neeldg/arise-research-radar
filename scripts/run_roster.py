#!/usr/bin/env python3
"""CLI: fetch recent OpenAlex publications for the verified ARISE roster.

python scripts/run_roster.py --roster config/roster.yaml --days-back 90
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arise_radar.models import Researcher
from arise_radar.output import format_publications, format_skip_warning
from arise_radar.roster import RosterError, load_roster
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


def main(argv: list[str] | None = None, *, client: OpenAlexClient | None = None) -> int:
    args = parse_args(argv)

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

    all_publications = []
    try:
        for researcher in queryable:
            try:
                publications = fetch_researcher_publications(
                    active_client, researcher, args.days_back
                )
            except OpenAlexError as exc:
                print(f"ERROR: {researcher.name} — {exc}", file=sys.stderr)
                continue
            all_publications.extend(publications)
    finally:
        if owns_client:
            active_client.close()

    print(format_publications(all_publications))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
