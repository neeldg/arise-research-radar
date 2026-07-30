"""New-publication Slack notification pipeline: read pending publication
rows from the main publications Notion data source (see sinks/notion.py),
post each to the ARISE papers Slack channel, and record the delivery result
back onto the same row.

Mirrors notifications/citations.py's bulk-read-then-index pattern rather
than querying Notion once per row: the whole publications data source is
read in one paginated pass (scan_publication_rows), candidates are selected
in memory (select_candidates), and every Notion write goes straight to an
already-known page ID — never a per-row lookup.

Only ever writes: Slack Status, Slack Timestamp, Slack Notified Date, and
Slack Error. Status, Relevance Status, Editorial Notes, Draft Status, and
every generated drafting field are left exactly as they are — this module
never mentions them in a write payload (see sinks/notion.py's
upsert_publication for the equivalent convention on the roster-sync side,
which is the only other code that ever sets Slack Status, and only at
create time).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, Field, SecretStr

from arise_radar.sinks.notion import (
    NotionClient,
    NotionError,
    NotionProperties,
    read_date,
    read_multi_select_names,
    read_rich_text,
    read_select,
    read_title,
    read_url,
)
from arise_radar.sinks.slack import BOT_TOKEN_PREFIX, SlackClient, SlackError

SLACK_STATUS_PENDING = "Pending"
SLACK_STATUS_SENT = "Sent"
SLACK_STATUS_FAILED = "Failed"
SLACK_STATUS_SUPPRESSED = "Suppressed"

# A Draft Social Post preview is only included in the Slack message when
# it's short enough to be a quick, useful preview rather than a wall of text
# competing with the "why it matters" summary above it -- roughly
# tweet-length, the same intuition as sinks/notion.py's own
# DRAFT_PREVIEW_MAX_LENGTH (which caps the property-level preview, a
# different and much longer limit for a different purpose).
DRAFT_SOCIAL_POST_INCLUDE_MAX_LENGTH = 280

# Slack Error is meant to be a concise, at-a-glance reason, not a full stack
# trace -- long messages are truncated (never silently dropped: the full
# detail is still in this run's own stdout/--error-report).
SLACK_ERROR_MAX_LENGTH = 300

_REQUIRED_ENV_NAMES = (
    "NOTION_TOKEN",
    "NOTION_DATA_SOURCE_ID",
    "SLACK_BOT_TOKEN",
    "SLACK_PAPERS_CHANNEL_ID",
)


# --- configuration ------------------------------------------------------------------


class PublicationNotificationConfigError(RuntimeError):
    """Raised when required environment variables for this pipeline are
    missing or obviously malformed."""


class PublicationNotificationConfig(BaseModel):
    notion_token: SecretStr
    data_source_id: str
    slack_bot_token: SecretStr
    papers_channel_id: str


def load_publication_notification_config(
    *, env: Mapping[str, str] | None = None
) -> PublicationNotificationConfig:
    """Load NOTION_TOKEN, NOTION_DATA_SOURCE_ID, SLACK_BOT_TOKEN, and
    SLACK_PAPERS_CHANNEL_ID (.env supported via python-dotenv).

    Deliberately does NOT require NOTION_CITATIONS_DATA_SOURCE_ID or
    SLACK_SIGNALS_CHANNEL_ID -- this pipeline never touches the Citation
    Events data source and never posts to the signals channel.

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
        raise PublicationNotificationConfigError("; ".join(problems))

    return PublicationNotificationConfig(
        notion_token=values["NOTION_TOKEN"],
        data_source_id=values["NOTION_DATA_SOURCE_ID"],
        slack_bot_token=values["SLACK_BOT_TOKEN"],
        papers_channel_id=values["SLACK_PAPERS_CHANNEL_ID"],
    )


# --- reading a publication page into a domain record --------------------------------


class PublicationRow(BaseModel):
    """Every field this pipeline needs from one publication Notion page,
    read once during the bulk scan regardless of whether the row ends up
    selected as a candidate."""

    page_id: str
    page_url: str | None = None
    canonical_key: str | None = None
    title: str = ""
    doi: str | None = None
    url: str | None = None
    researchers: list[str] = Field(default_factory=list)
    # The "venue/source" display line: this data source has no dedicated
    # Venue property (only Source, e.g. "OpenAlex" -- which pipeline
    # discovered it, not which journal published it), so that's the only
    # stored "origin" signal available to show here.
    source_label: str | None = None
    published_date: str | None = None
    why_it_matters: str | None = None
    internal_summary: str | None = None
    key_story_angle: str | None = None
    draft_social_post: str | None = None
    slack_status: str | None = None


def read_publication_row(page: dict) -> PublicationRow:
    return PublicationRow(
        page_id=page.get("id", ""),
        page_url=page.get("url"),
        canonical_key=read_rich_text(page, NotionProperties.CANONICAL_KEY),
        title=read_title(page) or "",
        doi=read_rich_text(page, NotionProperties.DOI),
        url=read_url(page, NotionProperties.URL),
        researchers=read_multi_select_names(page, NotionProperties.RESEARCHERS),
        source_label=read_select(page, NotionProperties.SOURCE),
        published_date=read_date(page, NotionProperties.PUBLISHED_DATE),
        why_it_matters=read_rich_text(page, NotionProperties.WHY_IT_MATTERS),
        internal_summary=read_rich_text(page, NotionProperties.INTERNAL_SUMMARY),
        key_story_angle=read_rich_text(page, NotionProperties.KEY_STORY_ANGLE),
        draft_social_post=read_rich_text(page, NotionProperties.DRAFT_SOCIAL_POST),
        slack_status=read_select(page, NotionProperties.SLACK_STATUS),
    )


# --- bulk scan + candidate selection ------------------------------------------------


class PublicationScanResult(BaseModel):
    """Partitions every row in the data source by eligibility. Does not
    decide what to actually notify this run — see select_candidates, which
    also applies --limit/--canonical-key/--retry-failed on top of this."""

    rows_scanned: int = 0
    duplicate_keys: dict[str, list[str]] = Field(default_factory=dict)
    malformed_rows: int = 0
    pending: list[PublicationRow] = Field(default_factory=list)
    failed: list[PublicationRow] = Field(default_factory=list)
    # Suppressed rows AND rows with no Slack Status at all (historical rows
    # not yet backfilled -- see scripts/backfill_publication_slack_status.py)
    # are reported together: both are simply "not eligible for Slack".
    historical_or_suppressed_skipped: int = 0
    sent_skipped: int = 0


def scan_publication_rows(pages: Iterable[dict]) -> PublicationScanResult:
    """Pure scan over already-fetched publication pages — no I/O.

    A Canonical Key shared by more than one stored page is a data-integrity
    problem, not something safe to guess a winner for: every row sharing
    that key is excluded from every other bucket and reported in
    `duplicate_keys` instead, so none of the ambiguous copies is ever
    notified.
    """
    seen: dict[str, list[PublicationRow]] = {}
    total = 0
    malformed = 0
    for page in pages:
        total += 1
        row = read_publication_row(page)
        if not row.canonical_key or not row.page_id:
            malformed += 1
            continue
        seen.setdefault(row.canonical_key, []).append(row)

    duplicate_keys: dict[str, list[str]] = {}
    pending: list[PublicationRow] = []
    failed: list[PublicationRow] = []
    historical_or_suppressed_skipped = 0
    sent_skipped = 0

    for key, rows in seen.items():
        if len(rows) > 1:
            duplicate_keys[key] = [row.page_id for row in rows]
            continue

        row = rows[0]
        if row.slack_status == SLACK_STATUS_PENDING:
            pending.append(row)
        elif row.slack_status == SLACK_STATUS_FAILED:
            failed.append(row)
        elif row.slack_status == SLACK_STATUS_SENT:
            sent_skipped += 1
        else:
            # Suppressed, or empty (not yet backfilled) -- see the class
            # docstring above.
            historical_or_suppressed_skipped += 1

    return PublicationScanResult(
        rows_scanned=total,
        duplicate_keys=duplicate_keys,
        malformed_rows=malformed,
        pending=pending,
        failed=failed,
        historical_or_suppressed_skipped=historical_or_suppressed_skipped,
        sent_skipped=sent_skipped,
    )


def select_candidates(
    scan: PublicationScanResult,
    *,
    retry_failed: bool = False,
    canonical_key: str | None = None,
    limit: int | None = None,
) -> list[PublicationRow]:
    """Pending rows are always candidates; Failed rows only join them with
    --retry-failed. A Sent or Suppressed/historical row is never in
    `scan.pending` or `scan.failed` in the first place (see
    scan_publication_rows), so it can never be selected here regardless of
    flags.
    """
    candidates = list(scan.pending)
    if retry_failed:
        candidates.extend(scan.failed)

    if canonical_key is not None:
        candidates = [row for row in candidates if row.canonical_key == canonical_key]

    if limit is not None:
        candidates = candidates[:limit]

    return candidates


# --- Slack message rendering ---------------------------------------------------


def _joined_researchers(row: PublicationRow) -> str:
    return ", ".join(row.researchers) if row.researchers else "(none recorded)"


def _why_it_matters(row: PublicationRow) -> str:
    if row.why_it_matters:
        return row.why_it_matters
    if row.internal_summary:
        return row.internal_summary
    return "(no summary available yet — open the Notion record for details)"


def _publication_link(row: PublicationRow) -> str | None:
    if row.url:
        return row.url
    if row.doi:
        return f"https://doi.org/{row.doi}"
    return None


def _draft_social_post_preview(row: PublicationRow) -> str | None:
    text = row.draft_social_post
    if not text or len(text) > DRAFT_SOCIAL_POST_INCLUDE_MAX_LENGTH:
        return None
    return text


def render_slack_text(row: PublicationRow) -> str:
    """Complete, standalone plain-text fallback — must be fully readable on
    its own, since Slack shows this (not `blocks`) in notifications and on
    surfaces that don't render Block Kit."""
    lines = [
        "📄 NEW ARISE RESEARCH",
        "",
        row.title or "(untitled)",
        "",
        "ARISE researcher(s):",
        _joined_researchers(row),
        "",
        "Published in:",
        row.source_label or "(unknown)",
        "",
        "Published:",
        row.published_date or "(unknown)",
        "",
        "Why it matters:",
        _why_it_matters(row),
    ]

    if row.key_story_angle:
        lines.extend(["", "Story angle:", row.key_story_angle])

    draft_preview = _draft_social_post_preview(row)
    if draft_preview:
        lines.extend(["", "Draft social post preview:", draft_preview])

    link = _publication_link(row)
    if link:
        lines.extend(["", f"Publication link: {link}"])
    if row.page_url:
        lines.extend(["", f"Notion page: {row.page_url}"])
    lines.extend(["", f"Canonical Key: {row.canonical_key}"])

    return "\n".join(lines)


def render_slack_blocks(row: PublicationRow) -> list[dict]:
    body_lines = [
        f"*{row.title or '(untitled)'}*",
        "",
        "*ARISE researcher(s):*",
        _joined_researchers(row),
        "",
        "*Published in:*",
        row.source_label or "(unknown)",
        "",
        "*Published:*",
        row.published_date or "(unknown)",
        "",
        "*Why it matters:*",
        _why_it_matters(row),
    ]
    if row.key_story_angle:
        body_lines.extend(["", "*Story angle:*", row.key_story_angle])

    draft_preview = _draft_social_post_preview(row)
    if draft_preview:
        body_lines.extend(["", "*Draft social post preview:*", draft_preview])

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📄 NEW ARISE RESEARCH", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(body_lines)}},
    ]

    link_lines = []
    link = _publication_link(row)
    if link:
        link_lines.append(f"<{link}|Publication link>")
    if row.page_url:
        link_lines.append(f"<{row.page_url}|View in Notion>")
    if link_lines:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(link_lines)}}
        )

    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"Canonical Key: `{row.canonical_key}`"}],
        }
    )
    return blocks


# --- delivery: post to Slack, then record the result by known page ID -------------


class PublicationDeliveryResult(BaseModel):
    canonical_key: str
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


def _current_utc_datetime_value() -> dict:
    return {"date": {"start": datetime.now(UTC).isoformat()}}


def _safe_error_text(text: str) -> str:
    if len(text) <= SLACK_ERROR_MAX_LENGTH:
        return text
    return text[: SLACK_ERROR_MAX_LENGTH - 1].rstrip() + "…"


def deliver_publication_notification(
    slack_client: SlackClient,
    notion_client: NotionClient,
    data_source_id: str,
    row: PublicationRow,
    *,
    channel_id: str,
    dry_run: bool = False,
) -> PublicationDeliveryResult:
    """Post one PublicationRow to Slack and record the result on its Notion
    page (by the page ID already known from the bulk scan — never a
    lookup). Never raises for Slack/Notion failures: captured in the
    returned result's action="failed" so callers can continue processing
    the rest of the batch.
    """
    assert row.canonical_key is not None  # guaranteed by scan_publication_rows

    if dry_run:
        return PublicationDeliveryResult(
            canonical_key=row.canonical_key,
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


def _record_success(
    client: NotionClient, row: PublicationRow, *, ts: str
) -> PublicationDeliveryResult:
    assert row.canonical_key is not None
    properties = {
        NotionProperties.SLACK_STATUS: {"select": {"name": SLACK_STATUS_SENT}},
        NotionProperties.SLACK_TIMESTAMP: _rich_text_value(ts),
        NotionProperties.SLACK_NOTIFIED_DATE: _current_utc_datetime_value(),
        NotionProperties.SLACK_ERROR: _rich_text_value(""),
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
        return PublicationDeliveryResult(
            canonical_key=row.canonical_key,
            page_id=row.page_id,
            action="failed",
            stage="notion_update_after_send",
            status_code=exc.status_code,
            detail=(
                f"Slack message sent (ts={ts}) but the Notion update failed: {exc} -- "
                "Slack Status was NOT changed; needs manual reconciliation"
            ),
        )
    return PublicationDeliveryResult(
        canonical_key=row.canonical_key, page_id=row.page_id, action="sent", ts=ts
    )


def _record_failure(
    client: NotionClient,
    row: PublicationRow,
    *,
    error: str,
    slack_error_code: str | None,
    status_code: int | None,
) -> PublicationDeliveryResult:
    assert row.canonical_key is not None
    safe_error = _safe_error_text(error)
    properties = {
        NotionProperties.SLACK_STATUS: {"select": {"name": SLACK_STATUS_FAILED}},
        NotionProperties.SLACK_ERROR: _rich_text_value(safe_error),
    }
    try:
        client.update_page(row.page_id, properties)
        detail = f"Slack post failed: {safe_error}"
    except NotionError as exc:
        detail = f"Slack post failed ({safe_error}); Notion update also failed: {exc}"

    return PublicationDeliveryResult(
        canonical_key=row.canonical_key,
        page_id=row.page_id,
        action="failed",
        stage="slack_post",
        detail=detail,
        slack_error_code=slack_error_code,
        status_code=status_code,
    )
