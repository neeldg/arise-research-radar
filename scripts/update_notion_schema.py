#!/usr/bin/env python3
"""Safe, idempotent Notion schema migration for the summarization/draft milestone.

Adds the paper-summarization and ARISE-style draft-generation properties to the
existing NOTION_DATA_SOURCE_ID data source if they don't already exist.

    python scripts/update_notion_schema.py --dry-run
    python scripts/update_notion_schema.py

Never touches an existing property. If an existing property's type conflicts
with what this migration would add, that property is skipped and reported —
never overwritten. Running this twice does nothing the second time.
"""

from __future__ import annotations

import argparse
import sys

from pydantic import BaseModel, Field

from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionProperties,
    load_notion_config,
)

DRAFT_STATUS_OPTIONS = ["Not Started", "Drafted", "Needs Attention", "Approved"]
DRAFT_SOURCE_BASIS_OPTIONS = ["OpenAlex Abstract", "PubMed Abstract", "Metadata Only"]


def _select_options(names: list[str]) -> list[dict]:
    return [{"name": name} for name in names]


def desired_properties() -> dict:
    return {
        NotionProperties.INTERNAL_SUMMARY: {"rich_text": {}},
        NotionProperties.KEY_STORY_ANGLE: {"rich_text": {}},
        NotionProperties.WHY_IT_MATTERS: {"rich_text": {}},
        NotionProperties.DRAFT_SOCIAL_POST: {"rich_text": {}},
        NotionProperties.DRAFT_STATUS: {
            "select": {"options": _select_options(DRAFT_STATUS_OPTIONS)}
        },
        NotionProperties.DRAFT_ERROR: {"rich_text": {}},
        NotionProperties.DRAFTED_DATE: {"date": {}},
        NotionProperties.DRAFT_SOURCE_BASIS: {
            "select": {"options": _select_options(DRAFT_SOURCE_BASIS_OPTIONS)}
        },
        NotionProperties.DRAFT_MODEL: {"rich_text": {}},
    }


class SchemaConflict(BaseModel):
    property_name: str
    existing_type: str
    desired_type: str


class SchemaMigrationPlan(BaseModel):
    to_add: list[str] = Field(default_factory=list)
    already_present: list[str] = Field(default_factory=list)
    conflicts: list[SchemaConflict] = Field(default_factory=list)


def plan_migration(existing_properties: dict, desired: dict) -> SchemaMigrationPlan:
    """Pure diff: decide what to add vs. leave alone vs. refuse. No I/O."""
    to_add: list[str] = []
    already_present: list[str] = []
    conflicts: list[SchemaConflict] = []

    for name, definition in desired.items():
        desired_type = next(iter(definition))
        existing = existing_properties.get(name)
        if existing is None:
            to_add.append(name)
        elif existing.get("type") == desired_type:
            already_present.append(name)
        else:
            conflicts.append(
                SchemaConflict(
                    property_name=name,
                    existing_type=existing.get("type", "unknown"),
                    desired_type=desired_type,
                )
            )

    return SchemaMigrationPlan(to_add=to_add, already_present=already_present, conflicts=conflicts)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Add paper-summarization/draft-generation properties to the Notion "
            "data source, without touching anything that already exists."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be added; make no write requests",
    )
    return parser.parse_args(argv)


def _properties_noun(count: int) -> str:
    return "property" if count == 1 else "properties"


def format_plan(plan: SchemaMigrationPlan) -> str:
    lines: list[str] = []
    if plan.to_add:
        lines.append(f"Would add {len(plan.to_add)} {_properties_noun(len(plan.to_add))}:")
        lines.extend(f"  - {name}" for name in plan.to_add)
    else:
        lines.append("No properties to add.")

    if plan.already_present:
        count = len(plan.already_present)
        lines.append(f"Already present, left untouched: {count}")
        lines.extend(f"  - {name}" for name in plan.already_present)

    if plan.conflicts:
        count = len(plan.conflicts)
        lines.append(f"REFUSING {count} conflicting {_properties_noun(count)} (type mismatch):")
        for conflict in plan.conflicts:
            lines.append(
                f"  - {conflict.property_name}: existing type {conflict.existing_type!r} "
                f"conflicts with desired type {conflict.desired_type!r} — left untouched"
            )

    return "\n".join(lines)


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
            data_source = active_client.get_data_source(config.data_source_id)
        except NotionError as exc:
            print(f"ERROR: could not read the data source: {exc}", file=sys.stderr)
            return 1

        existing_properties = data_source.get("properties") or {}
        desired = desired_properties()
        plan = plan_migration(existing_properties, desired)

        print(format_plan(plan))

        if args.dry_run:
            print()
            print("Dry run: no write requests were made.")
            return 0

        if not plan.to_add:
            print()
            print("Nothing to migrate — schema is already up to date.")
            return 0

        properties_to_add = {name: desired[name] for name in plan.to_add}
        try:
            active_client.update_data_source(config.data_source_id, properties_to_add)
        except NotionError as exc:
            print(f"ERROR: schema update failed: {exc}", file=sys.stderr)
            return 1

        print()
        print(f"Added {len(plan.to_add)} {_properties_noun(len(plan.to_add))}.")
        return 0
    finally:
        if owns_client:
            active_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
