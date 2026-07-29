#!/usr/bin/env python3
"""One-time (but safely re-runnable) setup: create or validate the ARISE
Citation Events Notion database used by scripts/run_citations.py.

    python scripts/setup_citations_notion.py
    python scripts/setup_citations_notion.py --write-notion
    python scripts/setup_citations_notion.py --write-notion --repair-schema
    python scripts/setup_citations_notion.py --write-notion --repair-schema --update-env

Requires NOTION_TOKEN and NOTION_PARENT_PAGE_ID (see .env.example) — the same
parent page as scripts/setup_notion.py, but this script only ever looks for
and creates a database titled "ARISE Citation Events" under it, and never
touches the existing "ARISE Research Radar — Test" database or
NOTION_DATA_SOURCE_ID.

Default (no --write-notion): read-only. Looks for an existing database/data
source, reports what exists, what schema differences (if any) were found, and
what would be created or repaired. Makes no create/update requests.

--write-notion: actually create the database + data source if neither exists.
If the "Citation Events" data source already exists, its schema is compared
against the desired schema; a difference is reported but left untouched
unless --repair-schema is also given.

--repair-schema: when the existing data source is missing properties, or an
existing select/multi_select property is missing some of the documented
options, additively add them — never deletes a property, never removes an
existing select/multi_select option, and never changes an existing property's
type. A genuine type conflict (existing property, wrong type) can never be
auto-repaired by this flag; it always requires manual resolution in the
Notion UI. Without --write-notion, this only previews the repair.

--update-env: on success, write NOTION_CITATIONS_DATA_SOURCE_ID=<id> into
.env (default path, override with --env-file), replacing that one line if
present and preserving every other line exactly. Never touched unless the
data source is known to be a full schema match (freshly created, or an
existing one that matched/was just repaired) with --write-notion.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    NotionError,
    load_notion_setup_config,
)
from arise_radar.sinks.notion_citations import NotionCitationProperties

DATABASE_TITLE = "ARISE Citation Events"
DATA_SOURCE_NAME = "Citation Events"

REVIEW_STATUS_OPTIONS = ["New", "Approved", "Rejected"]
CITATION_RELATIONSHIP_OPTIONS = [
    "Unclassified",
    "Applies",
    "Extends",
    "Validates",
    "Critiques",
    "Mentions",
]
SLACK_STATUS_OPTIONS = ["Pending", "Suppressed", "Sent", "Failed"]

DEFAULT_ENV_FILE = Path(".env")
ENV_KEY = "NOTION_CITATIONS_DATA_SOURCE_ID"


def _select_options(names: list[str]) -> list[dict]:
    return [{"name": name} for name in names]


def build_schema_properties() -> dict:
    return {
        NotionCitationProperties.NAME: {"title": {}},
        NotionCitationProperties.CITATION_KEY: {"rich_text": {}},
        NotionCitationProperties.CITING_OPENALEX_ID: {"rich_text": {}},
        NotionCitationProperties.CITING_DOI: {"rich_text": {}},
        NotionCitationProperties.CITING_URL: {"url": {}},
        NotionCitationProperties.CITED_ARISE_PAPER: {"rich_text": {}},
        NotionCitationProperties.ARISE_RESEARCHERS: {"multi_select": {"options": []}},
        NotionCitationProperties.PUBLISHED_DATE: {"date": {}},
        NotionCitationProperties.DETECTED_DATE: {"date": {}},
        NotionCitationProperties.BASELINE: {"checkbox": {}},
        NotionCitationProperties.REVIEW_STATUS: {
            "select": {"options": _select_options(REVIEW_STATUS_OPTIONS)}
        },
        NotionCitationProperties.CITATION_RELATIONSHIP: {
            "select": {"options": _select_options(CITATION_RELATIONSHIP_OPTIONS)}
        },
        NotionCitationProperties.RELATIONSHIP_EVIDENCE: {"rich_text": {}},
        NotionCitationProperties.SLACK_STATUS: {
            "select": {"options": _select_options(SLACK_STATUS_OPTIONS)}
        },
        NotionCitationProperties.SLACK_TIMESTAMP: {"rich_text": {}},
        NotionCitationProperties.SYSTEM_NOTES: {"rich_text": {}},
    }


# --- schema diff: pure logic, no I/O --------------------------------------------


class SchemaPropertyConflict(BaseModel):
    property_name: str
    existing_type: str
    desired_type: str


class SchemaDiff(BaseModel):
    missing_properties: list[str] = Field(default_factory=list)
    type_conflicts: list[SchemaPropertyConflict] = Field(default_factory=list)
    missing_select_options: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def is_exact_match(self) -> bool:
        return not (self.missing_properties or self.type_conflicts or self.missing_select_options)

    @property
    def is_repairable(self) -> bool:
        """True when everything wrong can be fixed additively — adding a
        missing property outright, or adding missing options to an existing
        select/multi_select — without touching an existing property's type
        or removing anything. False whenever there is at least one genuine
        type conflict, which this script can never auto-repair."""
        return not self.type_conflicts and bool(
            self.missing_properties or self.missing_select_options
        )


def diff_schema(existing_properties: dict, desired: dict) -> SchemaDiff:
    missing_properties: list[str] = []
    type_conflicts: list[SchemaPropertyConflict] = []
    missing_select_options: dict[str, list[str]] = {}

    for name, definition in desired.items():
        desired_type = next(iter(definition))
        existing = existing_properties.get(name)
        if existing is None:
            missing_properties.append(name)
            continue

        existing_type = existing.get("type")
        if existing_type != desired_type:
            type_conflicts.append(
                SchemaPropertyConflict(
                    property_name=name,
                    existing_type=existing_type or "unknown",
                    desired_type=desired_type,
                )
            )
            continue

        if desired_type in ("select", "multi_select"):
            desired_options = {opt["name"] for opt in definition[desired_type].get("options", [])}
            existing_options = {
                opt["name"] for opt in (existing.get(desired_type) or {}).get("options", [])
            }
            missing = sorted(desired_options - existing_options)
            if missing:
                missing_select_options[name] = missing

    return SchemaDiff(
        missing_properties=missing_properties,
        type_conflicts=type_conflicts,
        missing_select_options=missing_select_options,
    )


def build_repair_properties(existing_properties: dict, desired: dict, diff: SchemaDiff) -> dict:
    """Only ever adds — a missing property gets its full desired definition;
    an existing select/multi_select property missing some options gets a
    combined option list (existing options ∪ missing ones), never dropping an
    option that is already there. Never includes a type-conflicted property."""
    repair: dict[str, dict] = {}
    for name in diff.missing_properties:
        repair[name] = desired[name]
    for name, missing_options in diff.missing_select_options.items():
        desired_type = next(iter(desired[name]))
        existing_options = {
            opt["name"]
            for opt in (existing_properties[name].get(desired_type) or {}).get("options", [])
        }
        combined = sorted(existing_options | set(missing_options))
        repair[name] = {desired_type: {"options": _select_options(combined)}}
    return repair


def format_diff(diff: SchemaDiff) -> str:
    if diff.is_exact_match:
        return "Existing data source schema matches exactly."

    lines: list[str] = []
    if diff.missing_properties:
        lines.append(f"Missing {len(diff.missing_properties)} propert(y/ies):")
        lines.extend(f"  - {name}" for name in diff.missing_properties)
    if diff.missing_select_options:
        lines.append("Missing select option(s):")
        for name, options in diff.missing_select_options.items():
            lines.append(f"  - {name}: {', '.join(options)}")
    if diff.type_conflicts:
        lines.append(
            f"REFUSING {len(diff.type_conflicts)} conflicting propert(y/ies) (type mismatch):"
        )
        for conflict in diff.type_conflicts:
            lines.append(
                f"  - {conflict.property_name}: existing type {conflict.existing_type!r} "
                f"conflicts with desired type {conflict.desired_type!r} — needs manual repair "
                "in the Notion UI, can never be auto-repaired"
            )
    return "\n".join(lines)


# --- .env update ------------------------------------------------------------------


def update_env_file(env_path: Path, key: str, value: str) -> None:
    """Insert or replace exactly one `KEY=value` line, preserving every other
    line in the file exactly as-is (order, comments, blank lines). Creates
    the file if it doesn't exist yet."""
    prefix = f"{key}="
    new_line = f"{key}={value}\n"

    if env_path.exists():
        lines = env_path.read_text().splitlines(keepends=True)
    else:
        lines = []

    output_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            output_lines.append(new_line)
            replaced = True
        else:
            output_lines.append(line)

    if not replaced:
        if output_lines and not output_lines[-1].endswith("\n"):
            output_lines[-1] += "\n"
        output_lines.append(new_line)

    env_path.write_text("".join(output_lines))


# --- CLI --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or validate the ARISE Citation Events Notion database."
    )
    parser.add_argument(
        "--write-notion",
        action="store_true",
        help="Actually create/repair objects in Notion; default is read-only",
    )
    parser.add_argument(
        "--repair-schema",
        action="store_true",
        help=(
            "Additively add missing properties/select options to an existing data "
            "source (with --write-notion), or preview that repair (without it). "
            "Never touches an existing property's type or removes anything."
        ),
    )
    parser.add_argument(
        "--update-env",
        action="store_true",
        help=f"On success, write {ENV_KEY} into the env file (requires --write-notion)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Path to update with --update-env (default: {DEFAULT_ENV_FILE})",
    )
    return parser.parse_args(argv)


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return "(untitled)"


def _explain_notion_error(exc: NotionError, *, context: str) -> str:
    if exc.status_code == 401:
        return (
            f"ERROR: Notion rejected the token (401 Unauthorized) while {context} — "
            "check NOTION_TOKEN."
        )
    if exc.status_code == 403:
        return (
            f"ERROR: Notion denied permission (403 Forbidden) while {context} — the integration "
            "needs access to the parent page (Notion page -> ... -> Connections -> add the "
            "integration)."
        )
    if exc.status_code == 404:
        return (
            f"ERROR: Notion could not find the object (404 Not Found) while {context} — check "
            "NOTION_PARENT_PAGE_ID and that the page is shared with this integration."
        )
    if exc.status_code == 409:
        return (
            f"ERROR: Notion reported a conflict (409) while {context} — this can happen if "
            "another process just created or modified the same object; re-run the command "
            "to re-check current state before trying again."
        )
    if exc.status_code == 429:
        return (
            f"ERROR: Notion rate-limited this request (429) while {context}, and retries "
            f"were exhausted: {exc}"
        )
    return f"ERROR: {context} failed: {exc}"


def main(argv: list[str] | None = None, *, client: NotionClient | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_notion_setup_config()
    except NotionConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    owns_client = client is None
    active_client = client or NotionClient(token=config.token.get_secret_value())

    try:
        try:
            page = active_client.get_page(config.parent_page_id)
        except NotionError as exc:
            print(_explain_notion_error(exc, context="reading the parent page"), file=sys.stderr)
            return 1

        if page.get("archived"):
            print("ERROR: parent page is archived", file=sys.stderr)
            return 1

        print(f"Parent page accessible: {_page_title(page)!r}")

        try:
            existing_database_block = active_client.find_child_database(
                config.parent_page_id, DATABASE_TITLE
            )
        except NotionError as exc:
            print(
                _explain_notion_error(exc, context="checking for an existing database"),
                file=sys.stderr,
            )
            return 1

        desired = build_schema_properties()

        if existing_database_block is None:
            return _handle_create(active_client, config.parent_page_id, desired, args)

        return _handle_existing(active_client, existing_database_block, desired, args)
    finally:
        if owns_client:
            active_client.close()


def _handle_create(
    client: NotionClient, parent_page_id: str, desired: dict, args: argparse.Namespace
) -> int:
    if not args.write_notion:
        print(f"No existing {DATABASE_TITLE!r} database found.")
        print(f"Would create database: {DATABASE_TITLE!r}")
        print(f"Would create its {DATA_SOURCE_NAME!r} data source with {len(desired)} properties:")
        for name in desired:
            print(f"  - {name}")
        print("Read-only run: no create requests were made. Pass --write-notion to create.")
        _maybe_update_env(args, "")
        return 0

    database = client.create_database(parent_page_id, DATABASE_TITLE)
    database_id = database.get("id")
    database_url = database.get("url")

    try:
        data_source = client.create_data_source(database_id, DATA_SOURCE_NAME, desired)
    except NotionError as exc:
        print(
            f"ERROR: database was created (id={database_id}) but its data source failed to "
            f"create: {exc}",
            file=sys.stderr,
        )
        return 1
    data_source_id = data_source.get("id")

    print(f"Database ID:    {database_id}")
    print(f"Data source ID: {data_source_id}")
    print(f"Database URL:   {database_url}")
    print("Properties:")
    for name in desired:
        print(f"  - {name}")
    print()
    print("Add this to .env:")
    print(f"{ENV_KEY}={data_source_id}")

    _maybe_update_env(args, data_source_id)
    return 0


def _handle_existing(
    client: NotionClient, database_block: dict, desired: dict, args: argparse.Namespace
) -> int:
    database_id = database_block.get("id")
    print(f"Found existing database {DATABASE_TITLE!r} (id={database_id}).")

    try:
        database = client.get_database(database_id)
    except NotionError as exc:
        print(_explain_notion_error(exc, context="reading the existing database"), file=sys.stderr)
        return 1

    data_sources = database.get("data_sources") or []
    matching = next((ds for ds in data_sources if ds.get("name") == DATA_SOURCE_NAME), None)
    if matching is None:
        print(
            f"ERROR: database {database_id!r} exists but has no data source named "
            f"{DATA_SOURCE_NAME!r} (found: {[ds.get('name') for ds in data_sources]!r}) — "
            "this needs manual review; refusing to guess or create an additional data source.",
            file=sys.stderr,
        )
        return 1
    data_source_id = matching["id"]

    try:
        data_source = client.get_data_source(data_source_id)
    except NotionError as exc:
        print(
            _explain_notion_error(exc, context="reading the existing data source"),
            file=sys.stderr,
        )
        return 1

    existing_properties = data_source.get("properties") or {}
    diff = diff_schema(existing_properties, desired)
    print(f"Data source ID: {data_source_id}")
    print(format_diff(diff))

    if diff.is_exact_match:
        _maybe_update_env(args, data_source_id)
        return 0

    if diff.type_conflicts:
        print()
        print(
            "Cannot proceed automatically — the conflicting propert(y/ies) above need manual "
            "repair in the Notion UI, then re-run.",
            file=sys.stderr,
        )
        return 1

    # Only additive gaps (missing properties / missing select options) remain.
    if not args.repair_schema:
        print()
        print(
            "Re-run with --repair-schema to add the above, or --write-notion --repair-schema "
            "to actually apply it."
        )
        return 1

    repair_properties = build_repair_properties(existing_properties, desired, diff)

    if not args.write_notion:
        print()
        print(f"Would additively repair {len(repair_properties)} propert(y/ies):")
        for name in repair_properties:
            print(f"  - {name}")
        print("Read-only run: no write requests were made. Pass --write-notion to apply.")
        _maybe_update_env(args, "")
        return 0

    try:
        client.update_data_source(data_source_id, repair_properties)
    except NotionError as exc:
        print(
            _explain_notion_error(exc, context="repairing the data source schema"),
            file=sys.stderr,
        )
        return 1

    print()
    print(f"Repaired {len(repair_properties)} propert(y/ies).")
    _maybe_update_env(args, data_source_id)
    return 0


def _maybe_update_env(args: argparse.Namespace, data_source_id: str) -> None:
    if not args.update_env:
        return
    if not args.write_notion:
        print(
            f"NOTE: --update-env requires --write-notion; not writing {args.env_file} "
            "against an unconfirmed/read-only result."
        )
        return
    update_env_file(args.env_file, ENV_KEY, data_source_id)
    print(f"Updated {args.env_file} with {ENV_KEY}.")


if __name__ == "__main__":
    raise SystemExit(main())
