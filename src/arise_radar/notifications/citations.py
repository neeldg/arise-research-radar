"""Citation Slack notification pipeline: read pending citation events from
the Citation Events Notion data source (see sinks/notion_citations.py),
post each to the ARISE signals Slack channel, and record the delivery
result back onto the same row.

Mirrors sinks/notion_citations.py's bulk-read-then-index pattern (see
load_citation_row_index) rather than querying Notion once per row: the
whole Citation Events data source is read in one paginated pass
(scan_citation_rows), candidates are selected in memory (select_candidates),
and every Notion write goes straight to an already-known page ID — never a
per-row lookup.

Only ever writes: Slack Status, Slack Timestamp, and System Notes (appended
to, never replaced). Review Status, Citation Relationship, Relationship
Evidence, Baseline, and everything else on the row is left exactly as it
is — those are human-owned or set once at citation-discovery time (see
sinks/notion_citations.py's _build_citation_update_properties for the same
convention on that pipeline).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, SecretStr

from arise_radar.sinks.notion import (
    NotionClient,
    NotionError,
    read_checkbox,
    read_date,
    read_multi_select_names,
    read_rich_text,
    read_select,
    read_title,
    read_url,
)
from arise_radar.sinks.notion_citations import NotionCitationProperties
from arise_radar.sinks.slack import BOT_TOKEN_PREFIX, SlackClient, SlackError

SLACK_STATUS_PENDING = "Pending"
SLACK_STATUS_SENT = "Sent"
SLACK_STATUS_FAILED = "Failed"
SLACK_STATUS_SUPPRESSED = "Suppressed"

FAILURE_NOTE_PREFIX = "[Slack delivery]"

_REQUIRED_ENV_NAMES = (
    "NOTION_TOKEN",
    "NOTION_CITATIONS_DATA_SOURCE_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNALS_CHANNEL_ID",
)


# --- configuration ------------------------------------------------------------------


class CitationNotificationConfigError(RuntimeError):
    """Raised when required environment variables for this pipeline are
    missing or obviously malformed."""


class CitationNotificationConfig(BaseModel):
    notion_token: SecretStr
    citations_data_source_id: str
    slack_bot_token: SecretStr
    signals_channel_id: str


def load_citation_notification_config(
    *, env: Mapping[str, str] | None = None
) -> CitationNotificationConfig:
    """Load NOTION_TOKEN, NOTION_CITATIONS_DATA_SOURCE_ID, SLACK_BOT_TOKEN,
    and SLACK_SIGNALS_CHANNEL_ID (.env supported via python-dotenv).

    Deliberately does NOT require NOTION_DATA_SOURCE_ID (the publications
    data source) or SLACK_PAPERS_CHANNEL_ID — this pipeline never reads the
    publications data source and never posts to the papers channel, so
    reusing sinks.notion_citations.load_notion_citations_config here would
    demand an unrelated variable this script has no use for.

    Pass `env` explicitly in tests to avoid touching the real process
    environment. Every problem found is reported together, not just the
    first one.
    """
    if env is None:
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    values = {name: env.get(name) for name in _REQUIRED_ENV_NAMES}
    problems = [f"{name} is missing" for name in _REQUIRED_ENV_NAMES if not values[name]]

    token = values["SLACK_BOT_TOKEN"]
    if token and not token.startswith(BOT_TOKEN_PREFIX):
        problems.append(f"SLACK_BOT_TOKEN must begin with {BOT_TOKEN_PREFIX!r}")

    if problems:
        raise CitationNotificationConfigError("; ".join(problems))

    return CitationNotificationConfig(
        notion_token=values["NOTION_TOKEN"],
        citations_data_source_id=values["NOTION_CITATIONS_DATA_SOURCE_ID"],
        slack_bot_token=values["SLACK_BOT_TOKEN"],
        signals_channel_id=values["SLACK_SIGNALS_CHANNEL_ID"],
    )


# --- reading a Citation Events page into a domain record ---------------------------


class CitationRow(BaseModel):
    """Every field this pipeline needs from one Citation Events Notion page,
    read once during the bulk scan regardless of whether the row ends up
    selected as a candidate."""

    page_id: str
    page_url: str | None = None
    citation_key: str | None = None
    citing_title: str = ""
    citing_doi: str | None = None
    citing_url: str | None = None
    citing_openalex_id: str | None = None
    cited_arise_paper: str = ""
    arise_researchers: list[str] = Field(default_factory=list)
    published_date: str | None = None
    citation_relationship: str = "Unclassified"
    slack_status: str | None = None
    is_baseline: bool = False
    system_notes: str = ""


def _split_citing_title(name: str) -> str:
    """`Name` is always written by the citation-discovery pipeline as
    "<citing_title> cites <cited_title>" (see
    sinks/notion_citations.py's _shared_citation_properties, used on both
    create and update) — the only way a row exists in this data source is
    through that pipeline, so splitting on the first " cites " is a safe,
    deterministic reversal, not a guess. Falls back to the full title
    (never crashes) if the marker is somehow missing.
    """
    marker = " cites "
    if marker in name:
        return name.split(marker, 1)[0]
    return name


def read_citation_row(page: dict) -> CitationRow:
    name = read_title(page)
    return CitationRow(
        page_id=page.get("id", ""),
        page_url=page.get("url"),
        citation_key=read_rich_text(page, NotionCitationProperties.CITATION_KEY),
        citing_title=_split_citing_title(name) if name else "",
        citing_doi=read_rich_text(page, NotionCitationProperties.CITING_DOI),
        citing_url=read_url(page, NotionCitationProperties.CITING_URL),
        citing_openalex_id=read_rich_text(page, NotionCitationProperties.CITING_OPENALEX_ID),
        cited_arise_paper=read_rich_text(page, NotionCitationProperties.CITED_ARISE_PAPER) or "",
        arise_researchers=read_multi_select_names(page, NotionCitationProperties.ARISE_RESEARCHERS),
        published_date=read_date(page, NotionCitationProperties.PUBLISHED_DATE),
        citation_relationship=(
            read_select(page, NotionCitationProperties.CITATION_RELATIONSHIP) or "Unclassified"
        ),
        slack_status=read_select(page, NotionCitationProperties.SLACK_STATUS),
        is_baseline=read_checkbox(page, NotionCitationProperties.BASELINE),
        system_notes=read_rich_text(page, NotionCitationProperties.SYSTEM_NOTES) or "",
    )


# --- bulk scan + candidate selection ------------------------------------------------


class CitationScanResult(BaseModel):
    """Partitions every row in the data source by eligibility. Does not
    decide what to actually notify this run — see select_candidates, which
    also applies --limit/--citation-key/--retry-failed on top of this."""

    rows_scanned: int = 0
    duplicate_keys: dict[str, list[str]] = Field(default_factory=dict)
    malformed_rows: int = 0
    pending: list[CitationRow] = Field(default_factory=list)
    failed: list[CitationRow] = Field(default_factory=list)
    baseline_skipped: int = 0
    suppressed_or_sent_skipped: int = 0


def scan_citation_rows(pages: Iterable[dict]) -> CitationScanResult:
    """Pure scan over already-fetched Citation Events pages — no I/O.

    A Citation Key shared by more than one stored page is a data-integrity
    problem, not something safe to guess a winner for: every row sharing
    that key is excluded from every other bucket and reported in
    `duplicate_keys` instead, so none of the ambiguous copies is ever
    notified.
    """
    seen: dict[str, list[CitationRow]] = {}
    total = 0
    malformed = 0
    for page in pages:
        total += 1
        row = read_citation_row(page)
        if not row.citation_key or not row.page_id:
            malformed += 1
            continue
        seen.setdefault(row.citation_key, []).append(row)

    duplicate_keys: dict[str, list[str]] = {}
    pending: list[CitationRow] = []
    failed: list[CitationRow] = []
    baseline_skipped = 0
    suppressed_or_sent_skipped = 0

    for key, rows in seen.items():
        if len(rows) > 1:
            duplicate_keys[key] = [row.page_id for row in rows]
            continue

        row = rows[0]
        if row.is_baseline:
            # Hard safety net, independent of Slack Status: a baseline row
            # must never be notified even if something upstream mislabeled
            # it Pending/Failed.
            baseline_skipped += 1
            continue

        if row.slack_status == SLACK_STATUS_PENDING:
            pending.append(row)
        elif row.slack_status == SLACK_STATUS_FAILED:
            failed.append(row)
        else:
            suppressed_or_sent_skipped += 1

    return CitationScanResult(
        rows_scanned=total,
        duplicate_keys=duplicate_keys,
        malformed_rows=malformed,
        pending=pending,
        failed=failed,
        baseline_skipped=baseline_skipped,
        suppressed_or_sent_skipped=suppressed_or_sent_skipped,
    )


def select_candidates(
    scan: CitationScanResult,
    *,
    retry_failed: bool = False,
    citation_key: str | None = None,
    limit: int | None = None,
) -> list[CitationRow]:
    """Pending rows are always candidates; Failed rows only join them with
    --retry-failed. A Sent or Suppressed row is never in `scan.pending` or
    `scan.failed` in the first place (see scan_citation_rows), so it can
    never be selected here regardless of flags.
    """
    candidates = list(scan.pending)
    if retry_failed:
        candidates.extend(scan.failed)

    if citation_key is not None:
        candidates = [row for row in candidates if row.citation_key == citation_key]

    if limit is not None:
        candidates = candidates[:limit]

    return candidates


# --- Slack message rendering ---------------------------------------------------


def _joined_researchers(row: CitationRow) -> str:
    return ", ".join(row.arise_researchers) if row.arise_researchers else "(none recorded)"


def _citing_link(row: CitationRow) -> str | None:
    if row.citing_url:
        return row.citing_url
    if row.citing_doi:
        return f"https://doi.org/{row.citing_doi}"
    return None


def render_slack_text(row: CitationRow) -> str:
    """Complete, standalone plain-text fallback — must be fully readable on
    its own, since Slack shows this (not `blocks`) in notifications and on
    surfaces that don't render Block Kit."""
    lines = [
        "📚 NEW ARISE CITATION",
        "",
        "Citing paper:",
        row.citing_title or "(untitled)",
        "",
        "Cites:",
        row.cited_arise_paper or "(unknown ARISE paper)",
        "",
        "ARISE researcher(s):",
        _joined_researchers(row),
        "",
        "Published:",
        row.published_date or "(unknown)",
        "",
        "Relationship:",
        row.citation_relationship or "Unclassified",
    ]

    citing_link = _citing_link(row)
    if citing_link:
        lines.extend(["", f"Citing paper link: {citing_link}"])
    if row.page_url:
        lines.extend(["", f"Notion page: {row.page_url}"])
    lines.extend(["", f"Citation Key: {row.citation_key}"])

    return "\n".join(lines)


def render_slack_blocks(row: CitationRow) -> list[dict]:
    body = "\n".join(
        [
            "*Citing paper:*",
            row.citing_title or "(untitled)",
            "",
            "*Cites:*",
            row.cited_arise_paper or "(unknown ARISE paper)",
            "",
            "*ARISE researcher(s):*",
            _joined_researchers(row),
            "",
            "*Published:*",
            row.published_date or "(unknown)",
            "",
            "*Relationship:*",
            row.citation_relationship or "Unclassified",
        ]
    )

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📚 NEW ARISE CITATION", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]

    link_lines = []
    citing_link = _citing_link(row)
    if citing_link:
        link_lines.append(f"<{citing_link}|Citing paper link>")
    if row.page_url:
        link_lines.append(f"<{row.page_url}|View in Notion>")
    if link_lines:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(link_lines)}}
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Citation Key: `{row.citation_key}`"}],
        }
    )
    return blocks


# --- delivery: post to Slack, then record the result by known page ID -------------


class CitationDeliveryResult(BaseModel):
    citation_key: str
    page_id: str
    action: Literal["sent", "failed", "dry_run_preview"]
    ts: str | None = None
    detail: str = ""
    # Only set on action == "failed". "slack_post": chat.postMessage itself
    # failed (transport error, or a well-formed ok=false response).
    # "notion_update_after_send": Slack accepted the message but the
    # follow-up Notion write failed -- see _record_success's except branch
    # for why Slack Status is deliberately left untouched in that case.
    stage: Literal["slack_post", "notion_update_after_send"] | None = None
    slack_error_code: str | None = None
    status_code: int | None = None


def _rich_text_value(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _append_system_note(existing: str, note: str) -> str:
    if not existing:
        return note
    return f"{existing}\n\n{note}"


def deliver_citation_notification(
    slack_client: SlackClient,
    notion_client: NotionClient,
    citations_data_source_id: str,
    row: CitationRow,
    *,
    channel_id: str,
    dry_run: bool = False,
) -> CitationDeliveryResult:
    """Post one CitationRow to Slack and record the result on its Notion
    page (by the page ID already known from the bulk scan — never a
    lookup). Never raises for Slack/Notion failures: captured in the
    returned result's action="failed" so callers can continue processing
    the rest of the batch.
    """
    assert row.citation_key is not None  # guaranteed by scan_citation_rows

    if dry_run:
        return CitationDeliveryResult(
            citation_key=row.citation_key,
            page_id=row.page_id,
            action="dry_run_preview",
            detail=render_slack_text(row),
        )

    text = render_slack_text(row)
    blocks = render_slack_blocks(row)

    try:
        result = slack_client.post_message(channel_id, text, blocks=blocks)
    except SlackError as exc:
        return _record_failure(
            notion_client,
            row,
            error=str(exc),
            slack_error_code=None,
            status_code=exc.status_code,
        )

    if not result.get("ok") or not result.get("ts"):
        return _record_failure(
            notion_client,
            row,
            error=result.get("error") or "unknown_error",
            slack_error_code=result.get("error") or "unknown_error",
            status_code=None,
        )

    return _record_success(notion_client, row, ts=result["ts"])


def _record_success(client: NotionClient, row: CitationRow, *, ts: str) -> CitationDeliveryResult:
    assert row.citation_key is not None
    properties = {
        NotionCitationProperties.SLACK_STATUS: {"select": {"name": SLACK_STATUS_SENT}},
        NotionCitationProperties.SLACK_TIMESTAMP: _rich_text_value(ts),
    }
    try:
        client.update_page(row.page_id, properties)
    except NotionError as exc:
        # The Slack message genuinely was delivered -- marking this row
        # Failed would be false (and a later --retry-failed run would
        # double-post). Leaving Slack Status untouched risks a normal run
        # re-selecting it as Pending and also double-posting. Neither
        # automatic choice is safe, so this is surfaced loudly as its own
        # error category for a human to reconcile, and Slack Status is left
        # exactly as it was.
        return CitationDeliveryResult(
            citation_key=row.citation_key,
            page_id=row.page_id,
            action="failed",
            stage="notion_update_after_send",
            status_code=exc.status_code,
            detail=(
                f"Slack message sent (ts={ts}) but the Notion update failed: {exc} -- "
                "Slack Status was NOT changed; needs manual reconciliation"
            ),
        )
    return CitationDeliveryResult(
        citation_key=row.citation_key, page_id=row.page_id, action="sent", ts=ts
    )


def _record_failure(
    client: NotionClient,
    row: CitationRow,
    *,
    error: str,
    slack_error_code: str | None,
    status_code: int | None,
) -> CitationDeliveryResult:
    assert row.citation_key is not None
    note = f"{FAILURE_NOTE_PREFIX} chat.postMessage failed: {error}"
    properties = {
        NotionCitationProperties.SLACK_STATUS: {"select": {"name": SLACK_STATUS_FAILED}},
        NotionCitationProperties.SYSTEM_NOTES: _rich_text_value(
            _append_system_note(row.system_notes, note)
        ),
    }
    try:
        client.update_page(row.page_id, properties)
        detail = f"Slack post failed: {error}"
    except NotionError as exc:
        detail = f"Slack post failed ({error}); Notion update also failed: {exc}"

    return CitationDeliveryResult(
        citation_key=row.citation_key,
        page_id=row.page_id,
        action="failed",
        stage="slack_post",
        detail=detail,
        slack_error_code=slack_error_code,
        status_code=status_code,
    )
