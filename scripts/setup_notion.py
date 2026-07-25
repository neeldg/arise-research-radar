#!/usr/bin/env python3
"""One-time setup: create the ARISE Research Radar test Notion database.

    python scripts/setup_notion.py --dry-run
    python scripts/setup_notion.py
    python scripts/setup_notion.py --skip-test-row

Requires NOTION_TOKEN and NOTION_PARENT_PAGE_ID (see .env.example). Refuses to
run if a database with the target name already exists under the parent page.
"""

from __future__ import annotations

import argparse
import sys

from arise_radar.sinks.notion import (
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionProperties,
    load_notion_setup_config,
)

DATABASE_TITLE = "ARISE Research Radar — Test"

STATUS_OPTIONS = ["New", "Approved", "Rejected", "Published", "Error"]
SOURCE_OPTIONS = ["OpenAlex", "PubMed", "medRxiv", "arXiv", "Media", "Manual"]
RELEVANCE_STATUS_OPTIONS = ["Kept", "Uncertain", "Excluded"]

TEST_ROW_NAME = "TEST — ARISE Research Radar"
TEST_ROW_CANONICAL_KEY = "test:manual:001"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the ARISE Research Radar test Notion database."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate access and print the proposed schema; make no write requests",
    )
    parser.add_argument(
        "--skip-test-row",
        action="store_true",
        help="Do not create the optional test row after the database is created",
    )
    return parser.parse_args(argv)


def _select_options(names: list[str]) -> list[dict]:
    return [{"name": name} for name in names]


def build_schema_properties() -> dict:
    return {
        NotionProperties.NAME: {"title": {}},
        NotionProperties.CANONICAL_KEY: {"rich_text": {}},
        NotionProperties.STATUS: {"select": {"options": _select_options(STATUS_OPTIONS)}},
        NotionProperties.RESEARCHERS: {"multi_select": {"options": []}},
        NotionProperties.PUBLISHED_DATE: {"date": {}},
        NotionProperties.DETECTED_DATE: {"date": {}},
        NotionProperties.DOI: {"rich_text": {}},
        NotionProperties.OPENALEX_ID: {"rich_text": {}},
        NotionProperties.SOURCE: {"select": {"options": _select_options(SOURCE_OPTIONS)}},
        NotionProperties.URL: {"url": {}},
        NotionProperties.RELEVANCE_STATUS: {
            "select": {"options": _select_options(RELEVANCE_STATUS_OPTIONS)}
        },
        NotionProperties.SYSTEM_NOTES: {"rich_text": {}},
        NotionProperties.EDITORIAL_NOTES: {"rich_text": {}},
    }


def build_test_row_properties() -> dict:
    # Editorial Notes is intentionally omitted so it starts empty — it is human-owned
    # and the roster sync must never write to it (see sinks/notion.py).
    return {
        NotionProperties.NAME: {"title": [{"type": "text", "text": {"content": TEST_ROW_NAME}}]},
        NotionProperties.CANONICAL_KEY: {
            "rich_text": [{"type": "text", "text": {"content": TEST_ROW_CANONICAL_KEY}}]
        },
        NotionProperties.STATUS: {"select": {"name": "New"}},
        NotionProperties.RESEARCHERS: {"multi_select": [{"name": "Ethan Goh"}]},
        NotionProperties.SOURCE: {"select": {"name": "Manual"}},
        NotionProperties.RELEVANCE_STATUS: {"select": {"name": "Kept"}},
        NotionProperties.SYSTEM_NOTES: {
            "rich_text": [{"type": "text", "text": {"content": "Created by setup script"}}]
        },
    }


def _page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return "(untitled)"


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
            print(f"ERROR: parent page not accessible: {exc}", file=sys.stderr)
            return 1

        if page.get("archived"):
            print("ERROR: parent page is archived", file=sys.stderr)
            return 1

        print(f"Parent page accessible: {_page_title(page)!r}")

        try:
            existing = active_client.find_child_database(config.parent_page_id, DATABASE_TITLE)
        except NotionError as exc:
            print(f"ERROR: could not check for an existing database: {exc}", file=sys.stderr)
            return 1

        if existing is not None:
            print(
                f"ERROR: a database named {DATABASE_TITLE!r} already exists under the parent "
                f"page (id={existing.get('id')}); refusing to create a duplicate.",
                file=sys.stderr,
            )
            return 1

        schema = build_schema_properties()

        if args.dry_run:
            print(f"Would create database: {DATABASE_TITLE!r}")
            print(f"Would create its data source with {len(schema)} properties:")
            for name in schema:
                print(f"  - {name}")
            if not args.skip_test_row:
                print(f"Would create one test row: {TEST_ROW_NAME!r}")
            print("Dry run: no create, update, or delete requests were made.")
            return 0

        database = active_client.create_database(config.parent_page_id, DATABASE_TITLE)
        database_id = database.get("id")
        database_url = database.get("url")

        try:
            data_source = active_client.create_data_source(database_id, DATABASE_TITLE, schema)
        except NotionError as exc:
            print(
                f"ERROR: database was created (id={database_id}) but its data source "
                f"failed to create: {exc}",
                file=sys.stderr,
            )
            return 1
        data_source_id = data_source.get("id")

        print(f"Database ID:    {database_id}")
        print(f"Data source ID: {data_source_id}")
        print(f"Database URL:   {database_url}")
        print("Properties:")
        for name in schema:
            print(f"  - {name}")
        print()
        print("Add this to .env:")
        print(f"NOTION_DATA_SOURCE_ID={data_source_id}")

        if not args.skip_test_row:
            try:
                row = active_client.create_page(data_source_id, build_test_row_properties())
            except NotionError as exc:
                print(f"ERROR: failed to create test row: {exc}", file=sys.stderr)
                return 1
            print()
            print(f"Test row created: {row.get('id')}")

        return 0
    finally:
        if owns_client:
            active_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
