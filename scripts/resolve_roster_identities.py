#!/usr/bin/env python3
"""CLI: generate reviewable OpenAlex identity-candidate reports for the
inactive, unverified people on the ARISE master roster (config/roster.yaml).

    python scripts/resolve_roster_identities.py \\
        --fixture-file config/identity_resolution_fixture.example.json \\
        --output-dir out/identity_review

    python scripts/resolve_roster_identities.py \\
        --live-openalex --output-dir out/identity_review

    python scripts/resolve_roster_identities.py \\
        --live-openalex --researcher-id david_wu --output-dir out/identity_review

Reads only researchers where `active: false` and `identity_status:
unverified` (see arise_radar.identity_resolution.select_researchers_pending_review)
— every other roster record, including any already-verified/active one, is
never touched, queried, or reported on.

This script never writes to config/roster.yaml and never sets a researcher's
`active` or `identity_status` — it only produces a CSV and a JSON report of
OpenAlex candidates for a human to review. Promoting a researcher (setting a
verified `openalex_id` and `active: true` in config/roster.yaml) is always a
separate, manual, human-reviewed edit.

Human verification workflow:

1. Run this script (fixture or --live-openalex) to produce
   identity_candidates.csv / .json in --output-dir.
2. Open the CSV. For each researcher_id, review candidate rows sorted by
   confidence: "high" first, then "medium", "low", "none".
3. Read the ambiguity_notes column first -- "common name" and "conflicting
   affiliation" rows need extra scrutiny (e.g. cross-checking the
   candidate's recent_works and topics against what's actually known about
   the person) before being trusted even at "high" confidence.
4. Once a human is confident a candidate is the right person, manually edit
   config/roster.yaml for that researcher: set `openalex_id` to the
   candidate's OpenAlex ID (short form, e.g. "A5012345678"), set `orcid` if
   known, set `active: true`, and set `identity_status: verified` (or
   `"ambiguous"` if the identity is a known merged/shared node, as already
   done for Jonathan H Chen -- see the comment above that entry).
5. Re-run `python scripts/run_roster.py --roster config/roster.yaml
   --notion-dry-run` to confirm the newly-promoted researcher now returns
   sensible publications before ever using `--write-notion`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from arise_radar.identity_resolution import (
    CandidateSource,
    FixtureSource,
    IdentityCandidateRow,
    LiveOpenAlexSource,
    resolve_researcher,
    select_researchers_pending_review,
    write_csv_report,
    write_json_report,
)
from arise_radar.models import Researcher
from arise_radar.roster import RosterError, load_roster
from arise_radar.sources.openalex import OpenAlexClient

DEFAULT_ROSTER_PATH = Path("config/roster.yaml")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate reviewable OpenAlex candidate reports for the ARISE roster's "
            "inactive, unverified people. Never writes to the roster; never activates anyone."
        )
    )
    parser.add_argument(
        "--roster", type=Path, default=DEFAULT_ROSTER_PATH, help="Path to roster.yaml"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Max unverified researchers to resolve"
    )
    parser.add_argument(
        "--researcher-id",
        type=str,
        default=None,
        help="Resolve only the unverified researcher with this roster id",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write identity_candidates.csv/.json into",
    )
    parser.add_argument(
        "--fixture-file",
        type=Path,
        default=None,
        help="Replay OpenAlex author-search results from this JSON fixture (no live calls)",
    )
    parser.add_argument(
        "--live-openalex",
        action="store_true",
        help="Query the real OpenAlex API",
    )
    return parser.parse_args(argv)


def _select_targets(
    researchers: list[Researcher], *, researcher_id: str | None, limit: int | None
) -> tuple[list[Researcher], str | None]:
    """Returns (targets, error). Never silently includes a researcher outside
    the active:false/identity_status:unverified filter, even when
    --researcher-id names one directly."""
    pending = select_researchers_pending_review(researchers)

    if researcher_id is not None:
        matches = [r for r in pending if r.id == researcher_id]
        if matches:
            return matches, None
        exists_but_ineligible = any(r.id == researcher_id for r in researchers)
        if exists_but_ineligible:
            return [], (
                f"researcher {researcher_id!r} exists but is not pending review "
                "(must be active: false and identity_status: unverified)"
            )
        return [], f"no researcher with id {researcher_id!r} found in the roster"

    if limit is not None:
        pending = pending[:limit]
    return pending, None


def main(
    argv: list[str] | None = None,
    *,
    openalex_client: OpenAlexClient | None = None,
) -> int:
    args = parse_args(argv)

    if bool(args.fixture_file) and args.live_openalex:
        print("ERROR: --fixture-file and --live-openalex cannot be used together", file=sys.stderr)
        return 1
    if not args.fixture_file and not args.live_openalex:
        print("ERROR: specify exactly one of --fixture-file or --live-openalex", file=sys.stderr)
        return 1

    try:
        researchers = load_roster(args.roster)
    except RosterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    targets, selection_error = _select_targets(
        researchers, researcher_id=args.researcher_id, limit=args.limit
    )
    if selection_error is not None:
        print(f"ERROR: {selection_error}", file=sys.stderr)
        return 1

    if not targets:
        print("No unverified researchers to resolve.", file=sys.stderr)
        return 0

    source: CandidateSource
    owns_client = False
    active_client: OpenAlexClient | None = None
    if args.fixture_file:
        source = FixtureSource.from_file(args.fixture_file)
    else:
        owns_client = openalex_client is None
        active_client = openalex_client or OpenAlexClient()
        source = LiveOpenAlexSource(active_client)

    rows: list[IdentityCandidateRow] = []
    errored: list[str] = []
    try:
        for researcher in targets:
            researcher_rows, error = resolve_researcher(researcher, source)
            rows.extend(researcher_rows)
            if error is not None:
                errored.append(researcher.name)
                print(f"ERROR: {researcher.name} — {error}", file=sys.stderr)
    finally:
        if owns_client and active_client is not None:
            active_client.close()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "identity_candidates.csv"
    json_path = args.output_dir / "identity_candidates.json"
    write_csv_report(rows, csv_path)
    write_json_report(rows, json_path)

    print(f"Resolved {len(targets)} researcher(s); wrote {len(rows)} candidate row(s).")
    if errored:
        print(f"  {len(errored)} researcher(s) failed and were recorded with an error note.")
    print(f"CSV:  {csv_path}")
    print(f"JSON: {json_path}")
    print(
        "Review only -- config/roster.yaml was not modified. "
        "See this script's module docstring for the verification workflow."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
