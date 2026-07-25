#!/usr/bin/env python3
"""Generate ARISE-style internal summaries and LinkedIn drafts for eligible
New/OpenAlex Notion rows.

    python scripts/generate_drafts.py
    python scripts/generate_drafts.py --dry-run
    python scripts/generate_drafts.py --write-notion
    python scripts/generate_drafts.py --write-notion --limit 5
    python scripts/generate_drafts.py --write-notion --canonical-key "doi:10.xxxx/yyyy"
    python scripts/generate_drafts.py --write-notion --force

Requires NOTION_TOKEN, NOTION_DATA_SOURCE_ID, and ANTHROPIC_API_KEY (see
.env.example). Every mode actually calls the Anthropic API and generates real
draft content — `--write-notion` is what gates whether that content is
persisted to Notion; without it, drafts are generated and printed only.

Never overwrites a Notion row whose Draft Status is "Approved", with or
without --force. Never touches Status, Researchers, Editorial Notes, or
publication metadata — only draft-* properties are ever written.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from pydantic import BaseModel

from arise_radar.drafting import (
    AnthropicClient,
    DraftGenerationError,
    DraftingConfigError,
    DraftInput,
    determine_source_basis,
    generate_draft_for_paper,
    load_anthropic_config,
    load_style_files,
)
from arise_radar.sinks.notion import (
    NOTION_APPROVED_DRAFT_STATUS,
    NotionClient,
    NotionConfigError,
    NotionError,
    NotionProperties,
    build_draft_error_properties,
    build_draft_success_properties,
    build_generated_section_blocks,
    load_notion_config,
    read_multi_select_names,
    read_rich_text,
    read_select,
    read_title,
    replace_generated_section,
)
from arise_radar.sources.openalex import OpenAlexClient, OpenAlexError, reconstruct_abstract

DEFAULT_LIMIT = 3
IN_PROGRESS_DRAFT_STATUSES = {"Drafted", "Needs Attention"}


class DraftRunResult(BaseModel):
    canonical_key: str
    title: str
    action: str  # "drafted" | "preview" | "error" | "skipped"
    detail: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate internal summaries and ARISE-style LinkedIn drafts for eligible "
            "New/OpenAlex Notion rows."
        )
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="Max rows to process (default 3)"
    )
    parser.add_argument(
        "--canonical-key", type=str, default=None, help="Draft only the row with this canonical key"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit alias for the default: generate and preview, never write to Notion",
    )
    parser.add_argument(
        "--write-notion", action="store_true", help="Persist generated drafts to Notion"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate rows already Drafted/Needs Attention (never overwrites Approved)",
    )
    return parser.parse_args(argv)


def _eligibility_block_reason(page: dict, *, force: bool) -> str | None:
    """Return None if eligible, else a human-readable reason it was skipped."""
    draft_status = read_select(page, NotionProperties.DRAFT_STATUS)
    if draft_status == NOTION_APPROVED_DRAFT_STATUS:
        return "Draft Status is Approved — never overwritten"
    if draft_status in IN_PROGRESS_DRAFT_STATUSES and not force:
        return f"Draft Status is {draft_status!r} — use --force to regenerate"
    return None


def _parse_date(raw_date: str | None) -> date | None:
    return date.fromisoformat(raw_date) if raw_date else None


def _build_draft_input(
    page: dict, work: dict, openalex_id: str, abstract: str | None
) -> DraftInput:
    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    doi = work.get("doi")
    return DraftInput(
        title=work.get("display_name") or read_title(page),
        researcher_names=read_multi_select_names(page, NotionProperties.RESEARCHERS),
        publication_date=_parse_date(work.get("publication_date")),
        venue=source.get("display_name"),
        doi=doi.removeprefix("https://doi.org/") if doi else None,
        openalex_id=openalex_id,
        abstract=abstract,
    )


def _record_error(
    notion_client: NotionClient, page_id: str, message: str, drafted_date: date
) -> None:
    try:
        notion_client.update_page(
            page_id, build_draft_error_properties(error_message=message, drafted_date=drafted_date)
        )
    except NotionError as exc:
        print(f"WARNING: could not record draft error in Notion: {exc}", file=sys.stderr)


def main(
    argv: list[str] | None = None,
    *,
    notion_client: NotionClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    anthropic_client: AnthropicClient | None = None,
) -> int:
    args = parse_args(argv)

    if args.write_notion and args.dry_run:
        print("ERROR: --write-notion and --dry-run cannot be used together", file=sys.stderr)
        return 1

    try:
        notion_config = load_notion_config()
    except NotionConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        anthropic_config = load_anthropic_config()
    except DraftingConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        style_example, style_guide = load_style_files()
    except OSError as exc:
        print(f"ERROR: could not load style prompt files: {exc}", file=sys.stderr)
        return 1

    owns_notion = notion_client is None
    owns_openalex = openalex_client is None
    owns_anthropic = anthropic_client is None
    active_notion = notion_client or NotionClient(token=notion_config.token.get_secret_value())
    active_openalex = openalex_client or OpenAlexClient()
    active_anthropic = anthropic_client or AnthropicClient(
        api_key=anthropic_config.api_key, model=anthropic_config.model
    )

    results: list[DraftRunResult] = []
    run_date = date.today()

    try:
        if args.canonical_key:
            try:
                pages = active_notion.query_data_source_by_canonical_key(
                    notion_config.data_source_id, args.canonical_key
                )
            except NotionError as exc:
                print(f"ERROR: could not query Notion: {exc}", file=sys.stderr)
                return 1
            if not pages:
                print(
                    f"ERROR: no Notion row found for canonical key {args.canonical_key!r}",
                    file=sys.stderr,
                )
                return 1
            if len(pages) > 1:
                print(
                    f"ERROR: {len(pages)} Notion rows share canonical key "
                    f"{args.canonical_key!r}; needs human repair",
                    file=sys.stderr,
                )
                return 1
        else:
            try:
                pages = active_notion.query_eligible_drafts(
                    notion_config.data_source_id, limit=args.limit, force=args.force
                )
            except NotionError as exc:
                print(f"ERROR: could not query Notion: {exc}", file=sys.stderr)
                return 1

        if not pages:
            print("No eligible rows found.")
            return 0

        for page in pages:
            title = read_title(page)
            canonical_key = read_rich_text(page, NotionProperties.CANONICAL_KEY) or "(unknown)"
            openalex_id = read_rich_text(page, NotionProperties.OPENALEX_ID)

            block_reason = _eligibility_block_reason(page, force=args.force)
            if block_reason is not None:
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key,
                        title=title,
                        action="skipped",
                        detail=block_reason,
                    )
                )
                continue

            if not openalex_id:
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key,
                        title=title,
                        action="skipped",
                        detail="no OpenAlex ID on this row",
                    )
                )
                continue

            try:
                work = active_openalex.get_work(openalex_id)
            except OpenAlexError as exc:
                detail = f"OpenAlex fetch failed: {exc}"
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key, title=title, action="error", detail=detail
                    )
                )
                if args.write_notion:
                    _record_error(active_notion, page["id"], detail, run_date)
                continue

            abstract = reconstruct_abstract(work)
            draft_input = _build_draft_input(page, work, openalex_id, abstract)

            try:
                content = generate_draft_for_paper(
                    active_anthropic, draft_input, style_example, style_guide
                )
            except DraftGenerationError as exc:
                detail = str(exc)
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key, title=title, action="error", detail=detail
                    )
                )
                if args.write_notion:
                    _record_error(active_notion, page["id"], detail, run_date)
                continue

            source_basis = determine_source_basis(abstract)

            if not args.write_notion:
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key,
                        title=title,
                        action="preview",
                        detail=f"[{source_basis}] {content.draft_social_post}",
                    )
                )
                continue

            blocks = build_generated_section_blocks(
                content,
                source_basis=source_basis,
                model_name=active_anthropic.model,
                drafted_date=run_date,
            )
            try:
                replace_generated_section(active_notion, page["id"], blocks)
            except NotionError as exc:
                detail = f"Notion page content write failed: {exc}"
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key, title=title, action="error", detail=detail
                    )
                )
                # Page content wasn't confirmed written — never mark this row Drafted.
                _record_error(active_notion, page["id"], detail, run_date)
                continue

            try:
                active_notion.update_page(
                    page["id"],
                    build_draft_success_properties(
                        content,
                        source_basis=source_basis,
                        model_name=active_anthropic.model,
                        drafted_date=run_date,
                    ),
                )
            except NotionError as exc:
                results.append(
                    DraftRunResult(
                        canonical_key=canonical_key,
                        title=title,
                        action="error",
                        detail=f"Notion property write failed: {exc}",
                    )
                )
                continue

            results.append(
                DraftRunResult(
                    canonical_key=canonical_key,
                    title=title,
                    action="drafted",
                    detail=f"source: {source_basis}",
                )
            )

        for result in results:
            print(f"[{result.action}] {result.title} ({result.canonical_key})")
            if result.detail:
                print(f"  {result.detail}")

        counts: dict[str, int] = {}
        for result in results:
            counts[result.action] = counts.get(result.action, 0) + 1
        print()
        print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

        return 0
    finally:
        if owns_notion:
            active_notion.close()
        if owns_openalex:
            active_openalex.close()
        if owns_anthropic:
            active_anthropic.close()


if __name__ == "__main__":
    raise SystemExit(main())
