"""Notion sink: create-or-update ARISE roster publications in a Notion data source.

Uses a small injectable httpx-based client (mirroring arise_radar.sources.openalex)
rather than the third-party notion-client SDK, so tests can mock it the same way
via httpx.MockTransport, and so this isn't coupled to that SDK's support for the
newer "data source" API surface. No LLM calls — this module only writes what the
roster and relevance pipeline already produced.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date
from typing import Literal

import httpx
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, SecretStr

from arise_radar.drafting import DraftContent
from arise_radar.models import NormalizedPublication
from arise_radar.relevance import RelevanceDecision
from arise_radar.work_types import WorkTypeClassification, classify_work_type

NOTION_APPROVED_DRAFT_STATUS = "Approved"
NOTION_DRAFTED_STATUS = "Drafted"
NOTION_NEEDS_ATTENTION_STATUS = "Needs Attention"

DEFAULT_NOTION_BASE_URL = "https://api.notion.com"
NOTION_API_VERSION = "2025-09-03"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NOTION_SOURCE_VALUE = "OpenAlex"
NOTION_NEW_STATUS = "New"

# Marks the start of the pipeline-owned generated-draft section in a page's body.
# Deliberately long and distinctive so it is never mistaken for human-authored
# content. Everything from this block to the end of the page is treated as
# pipeline-owned and replaced wholesale on rerun; everything before it is human
# content and is never read, queried, or touched.
GENERATED_SECTION_MARKER = (
    "ARISE Research Radar — Generated Draft (auto-managed; do not edit below this line)"
)
MAX_RICH_TEXT_LENGTH = 2000
DRAFT_PREVIEW_MAX_LENGTH = 500
MAX_BLOCKS_PER_APPEND = 100

# Maps internal RelevanceDecision.status ("keep"/"uncertain"/"exclude") to the
# human-readable Notion select option labels defined by scripts/setup_notion.py.
RELEVANCE_STATUS_LABELS: dict[str, str] = {
    "keep": "Kept",
    "uncertain": "Uncertain",
    "exclude": "Excluded",
}


class NotionProperties:
    """Notion data source property names, kept out of the rest of the code."""

    NAME = "Name"
    CANONICAL_KEY = "Canonical Key"
    STATUS = "Status"
    RESEARCHERS = "Researchers"
    PUBLISHED_DATE = "Published Date"
    DETECTED_DATE = "Detected Date"
    DOI = "DOI"
    OPENALEX_ID = "OpenAlex ID"
    SOURCE = "Source"
    URL = "URL"
    RELEVANCE_STATUS = "Relevance Status"
    SYSTEM_NOTES = "System Notes"
    EDITORIAL_NOTES = "Editorial Notes"

    # Paper summarization / draft-generation properties (schema only for now —
    # see scripts/update_notion_schema.py; no code writes these yet).
    INTERNAL_SUMMARY = "Internal Summary"
    KEY_STORY_ANGLE = "Key Story Angle"
    WHY_IT_MATTERS = "Why It Matters"
    DRAFT_SOCIAL_POST = "Draft Social Post"
    DRAFT_STATUS = "Draft Status"
    DRAFT_ERROR = "Draft Error"
    DRAFTED_DATE = "Drafted Date"
    DRAFT_SOURCE_BASIS = "Draft Source Basis"
    DRAFT_MODEL = "Draft Model"


class NotionConfigError(RuntimeError):
    """Raised when required Notion environment variables are missing."""


class NotionConfig(BaseModel):
    token: SecretStr
    data_source_id: str


class NotionSetupConfig(BaseModel):
    token: SecretStr
    parent_page_id: str


def _require_env(names: Sequence[str], env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        # find_dotenv(usecwd=True): search for .env from the current working directory,
        # not from this module's location. Without usecwd, python-dotenv always finds
        # this repo's own .env (since this file lives inside the repo) regardless of
        # where the CLI is invoked from — which also makes chdir-based test isolation
        # a no-op.
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    values = {name: env.get(name) for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise NotionConfigError(
            "Missing required Notion environment variable(s): " + ", ".join(missing)
        )
    return values  # type: ignore[return-value]  # all present and non-empty at this point


def load_notion_config(*, env: Mapping[str, str] | None = None) -> NotionConfig:
    """Load NOTION_TOKEN and NOTION_DATA_SOURCE_ID (.env supported via python-dotenv).

    Pass `env` explicitly in tests to avoid touching the real process environment.
    """
    values = _require_env(("NOTION_TOKEN", "NOTION_DATA_SOURCE_ID"), env)
    return NotionConfig(
        token=values["NOTION_TOKEN"], data_source_id=values["NOTION_DATA_SOURCE_ID"]
    )


def load_notion_setup_config(*, env: Mapping[str, str] | None = None) -> NotionSetupConfig:
    """Load NOTION_TOKEN and NOTION_PARENT_PAGE_ID for the one-time setup script."""
    values = _require_env(("NOTION_TOKEN", "NOTION_PARENT_PAGE_ID"), env)
    return NotionSetupConfig(
        token=values["NOTION_TOKEN"], parent_page_id=values["NOTION_PARENT_PAGE_ID"]
    )


class NotionError(RuntimeError):
    """Raised when the Notion API cannot be reached after bounded retries, or errors."""


class NotionClient:
    """Thin, injectable HTTP client for the Notion API.

    Pass an `httpx.Client` (e.g. built on `httpx.MockTransport`) to avoid live
    network calls in tests. The token is only ever used to build the
    Authorization header — it is never logged or included in any exception text.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        token: str = "",
        base_url: str = DEFAULT_NOTION_BASE_URL,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=base_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_API_VERSION,
                    "Content-Type": "application/json",
                },
                timeout=10.0,
            )
            self._owns_client = True
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NotionClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- read-only -----------------------------------------------------------

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/v1/pages/{page_id}")

    def list_block_children(self, block_id: str) -> Iterator[dict]:
        """Yield a block's direct children, paginating via start_cursor."""
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            payload = self._request("GET", f"/v1/blocks/{block_id}/children", params=params)
            yield from payload.get("results", [])
            if not payload.get("has_more"):
                return
            cursor = payload.get("next_cursor")

    def find_child_database(self, parent_page_id: str, title: str) -> dict | None:
        """Return the child_database block under parent_page_id with an exact title match."""
        for block in self.list_block_children(parent_page_id):
            if block.get("type") != "child_database":
                continue
            if (block.get("child_database") or {}).get("title") == title:
                return block
        return None

    def get_data_source(self, data_source_id: str) -> dict:
        return self._request("GET", f"/v1/data_sources/{data_source_id}")

    def query_data_source_by_canonical_key(
        self, data_source_id: str, canonical_key: str
    ) -> list[dict]:
        body = {
            "filter": {
                "property": NotionProperties.CANONICAL_KEY,
                "rich_text": {"equals": canonical_key},
            }
        }
        payload = self._request("POST", f"/v1/data_sources/{data_source_id}/query", json=body)
        return payload.get("results", [])

    def query_eligible_drafts(
        self, data_source_id: str, *, limit: int, force: bool = False
    ) -> list[dict]:
        """Query New/OpenAlex rows not yet Approved, capped at `limit`.

        Without `force`, only rows with no Draft Status (or "Not Started") are
        returned. With `force`, rows already "Drafted"/"Needs Attention" are
        included too — "Approved" is always excluded, with or without `force`.
        """
        if force:
            draft_status_filter = {
                "property": NotionProperties.DRAFT_STATUS,
                "select": {"does_not_equal": NOTION_APPROVED_DRAFT_STATUS},
            }
        else:
            draft_status_filter = {
                "or": [
                    {"property": NotionProperties.DRAFT_STATUS, "select": {"is_empty": True}},
                    {
                        "property": NotionProperties.DRAFT_STATUS,
                        "select": {"equals": "Not Started"},
                    },
                ]
            }

        body = {
            "filter": {
                "and": [
                    {"property": NotionProperties.STATUS, "select": {"equals": NOTION_NEW_STATUS}},
                    {
                        "property": NotionProperties.SOURCE,
                        "select": {"equals": NOTION_SOURCE_VALUE},
                    },
                    draft_status_filter,
                ]
            },
            "page_size": limit,
        }
        payload = self._request("POST", f"/v1/data_sources/{data_source_id}/query", json=body)
        return payload.get("results", [])[:limit]

    # --- writes ---------------------------------------------------------------

    def create_database(self, parent_page_id: str, title: str) -> dict:
        """Create an (empty-schema) database. Its auto-provisioned default data
        source only ever gets a bare `Name` property — confirmed live that passing
        `properties` in this same call is silently ignored by the API. Use
        create_data_source() afterward to add the real schema.
        """
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
        }
        return self._request("POST", "/v1/databases", json=body)

    def create_data_source(self, database_id: str, name: str, properties: dict) -> dict:
        """Add a new data source with the given schema to an existing database.

        Confirmed live: this is the call that actually applies a full property
        schema (POST /v1/databases does not). The `name` field is accepted by the
        API but not reflected back in the created object — Notion assigns its own
        default display name regardless. The auto-provisioned default data source
        from create_database() is left behind, unused, alongside this one.
        """
        body = {
            "parent": {"type": "database_id", "database_id": database_id},
            "name": name,
            "properties": properties,
        }
        return self._request("POST", "/v1/data_sources", json=body)

    def update_data_source(self, data_source_id: str, properties: dict) -> dict:
        """PATCH only the given properties onto an existing data source's schema.

        Mirrors the classic Notion database-update contract: properties included
        here are added/changed; every property NOT mentioned is left untouched.
        Callers must never include an already-existing property here unless they
        intend to change its definition — this method has no its own safety
        checks against that.
        """
        return self._request(
            "PATCH", f"/v1/data_sources/{data_source_id}", json={"properties": properties}
        )

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._request("POST", "/v1/pages", json=body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/v1/pages/{page_id}", json={"properties": properties})

    def append_block_children(self, block_id: str, children: list[dict]) -> dict:
        """Notion's append-block-children endpoint is a PATCH, not a POST, despite
        the operation being additive rather than a full replace."""
        return self._request(
            "PATCH", f"/v1/blocks/{block_id}/children", json={"children": children}
        )

    def delete_block(self, block_id: str) -> dict:
        """Archives the block (Notion's DELETE semantics — recoverable from trash)."""
        return self._request("DELETE", f"/v1/blocks/{block_id}")

    def _request(self, method: str, path: str, **kwargs: object) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code < 300:
                    return response.json()
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise NotionError(
                        f"Notion API returned HTTP {response.status_code} for "
                        f"{method} {path}: {response.text}"
                    )
                last_error = httpx.HTTPStatusError(
                    f"Notion API returned HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_seconds * (2**attempt))

        raise NotionError(
            f"Notion API request failed after {self._max_retries + 1} attempt(s): {last_error}"
        )


class NotionUpsertResult(BaseModel):
    canonical_key: str
    action: Literal[
        "created", "updated", "skipped_duplicate", "error", "dry_run_create", "dry_run_update"
    ]
    page_id: str | None = None
    detail: str = ""


def upsert_publication(
    client: NotionClient,
    data_source_id: str,
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    *,
    detected_date: date,
    dry_run: bool = False,
    researcher_names: Iterable[str] | None = None,
    version_duplicate_note: str = "",
) -> NotionUpsertResult:
    """Create or update the Notion row for one publication.

    `researcher_names`, when given, is the full set of roster researchers who
    matched this canonical key *in this run* (see arise_radar.dedupe) — used
    instead of just `publication.researcher_name` so shared papers become one
    row with every matching researcher even before anything has been written
    to Notion (dry-run mode never persists between calls, so relying solely
    on querying Notion for this would under-merge). Defaults to
    `{publication.researcher_name}` when omitted, matching prior behavior.

    `version_duplicate_note`, when given, is appended to System Notes (see
    arise_radar.version_family) — advisory only, never merges canonical keys.

    Never called for `exclude` decisions — the caller is expected to filter
    those out first, and this raises if it happens anyway. Never raises for
    Notion API failures: those are captured in the returned result's
    action="error" so callers can continue processing the rest of a batch.
    """
    if decision.status == "exclude":
        raise ValueError("upsert_publication must not be called for excluded publications")

    names = set(researcher_names) if researcher_names is not None else {publication.researcher_name}

    try:
        matches = client.query_data_source_by_canonical_key(
            data_source_id, publication.canonical_key
        )
    except NotionError as exc:
        return NotionUpsertResult(
            canonical_key=publication.canonical_key, action="error", detail=f"query failed: {exc}"
        )

    if len(matches) > 1:
        page_ids = ", ".join(match.get("id", "?") for match in matches)
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="skipped_duplicate",
            detail=(
                f"{len(matches)} existing Notion pages share this canonical key "
                f"({page_ids}); needs human repair"
            ),
        )

    if not matches:
        return _create(
            client,
            data_source_id,
            publication,
            decision,
            detected_date,
            dry_run,
            names,
            version_duplicate_note,
        )

    return _update(
        client, matches[0], publication, decision, dry_run, names, version_duplicate_note
    )


def _create(
    client: NotionClient,
    data_source_id: str,
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    detected_date: date,
    dry_run: bool,
    researcher_names: set[str],
    version_duplicate_note: str,
) -> NotionUpsertResult:
    if dry_run:
        who = ", ".join(repr(name) for name in sorted(researcher_names))
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="dry_run_create",
            detail=f"would create page for {who}: {publication.title!r}",
        )

    properties = _build_create_properties(
        publication, decision, detected_date, researcher_names, version_duplicate_note
    )
    try:
        page = client.create_page(data_source_id, properties)
    except NotionError as exc:
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="error",
            detail=f"create failed: {exc}",
        )
    return NotionUpsertResult(
        canonical_key=publication.canonical_key, action="created", page_id=page.get("id")
    )


def _update(
    client: NotionClient,
    existing_page: dict,
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    dry_run: bool,
    researcher_names: set[str],
    version_duplicate_note: str,
) -> NotionUpsertResult:
    page_id = existing_page.get("id")
    existing_researchers = _read_multi_select(existing_page, NotionProperties.RESEARCHERS)
    merged_researchers = existing_researchers | researcher_names

    if dry_run:
        new_names = researcher_names - existing_researchers
        adds = f"adds {sorted(new_names)}" if new_names else "no new researcher"
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="dry_run_update",
            page_id=page_id,
            detail=f"would update page ({adds}; researchers -> {sorted(merged_researchers)})",
        )

    properties = _build_update_properties(
        publication, decision, merged_researchers, version_duplicate_note
    )
    try:
        client.update_page(page_id, properties)
    except NotionError as exc:
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="error",
            detail=f"update failed: {exc}",
        )
    return NotionUpsertResult(
        canonical_key=publication.canonical_key, action="updated", page_id=page_id
    )


def _build_create_properties(
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    detected_date: date,
    researcher_names: set[str],
    version_duplicate_note: str = "",
) -> dict:
    work_type = classify_work_type(publication)
    properties = _shared_properties(publication, decision, work_type, version_duplicate_note)
    properties[NotionProperties.STATUS] = {"select": {"name": NOTION_NEW_STATUS}}
    properties[NotionProperties.DETECTED_DATE] = _date_value(detected_date)
    properties[NotionProperties.RESEARCHERS] = _multi_select_value(researcher_names)

    # Fail-open: the record is always imported. Rows with no clear "finding"
    # to draft about automatically (a non-standard work type, or a probable
    # preprint/published-version or repository-version duplicate of another
    # row) default to Needs Attention with a clear reason instead of sitting
    # silently eligible for scheduled drafting. Create-only, like
    # Status/Detected Date above — never overwritten on a later resync, so a
    # human's or the drafting pipeline's own progress on this row is
    # preserved (see _build_update_properties). A human can still draft it
    # manually via `--canonical-key ... --force`.
    hold_reasons = _draft_hold_reasons(work_type, version_duplicate_note)
    if hold_reasons:
        properties[NotionProperties.DRAFT_STATUS] = {
            "select": {"name": NOTION_NEEDS_ATTENTION_STATUS}
        }
        properties[NotionProperties.DRAFT_ERROR] = _rich_text_value(
            "Not eligible for automatic scheduled drafting — requires human review: "
            + "; ".join(hold_reasons)
        )
    return properties


def _draft_hold_reasons(
    work_type: WorkTypeClassification, version_duplicate_note: str
) -> list[str]:
    """Reasons a newly-created row should default to Needs Attention instead
    of sitting eligible for automatic scheduled drafting. Empty when the row
    is a standard, draft-eligible, non-duplicate work."""
    reasons: list[str] = []
    if not work_type.draft_eligible:
        reasons.append(f"classified as {work_type.category!r}: {work_type.reason}")
    if version_duplicate_note:
        reasons.append(
            "probable preprint/published-version or repository-version duplicate — "
            f"{version_duplicate_note}"
        )
    return reasons


def _build_update_properties(
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    researchers: set[str],
    version_duplicate_note: str = "",
) -> dict:
    # Status, Detected Date, Draft Status, and Draft Error are intentionally
    # omitted here: editorial status, the original detection date, and
    # drafting-pipeline progress are preserved rather than overwritten on
    # every sync. Editorial Notes is never included anywhere in this module
    # — it is human-owned.
    work_type = classify_work_type(publication)
    properties = _shared_properties(publication, decision, work_type, version_duplicate_note)
    properties[NotionProperties.RESEARCHERS] = _multi_select_value(researchers)
    return properties


def _shared_properties(
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    work_type: WorkTypeClassification,
    version_duplicate_note: str = "",
) -> dict:
    properties: dict[str, object] = {
        NotionProperties.NAME: _title_value(publication.title),
        NotionProperties.CANONICAL_KEY: _rich_text_value(publication.canonical_key),
        NotionProperties.SOURCE: {"select": {"name": NOTION_SOURCE_VALUE}},
        NotionProperties.RELEVANCE_STATUS: {
            "select": {"name": RELEVANCE_STATUS_LABELS.get(decision.status, decision.status)}
        },
        NotionProperties.SYSTEM_NOTES: _rich_text_value(
            _notes_text(decision, work_type, version_duplicate_note)
        ),
    }
    if publication.publication_date:
        properties[NotionProperties.PUBLISHED_DATE] = _date_value(publication.publication_date)
    if publication.doi:
        properties[NotionProperties.DOI] = _rich_text_value(publication.doi)
    if publication.openalex_id:
        properties[NotionProperties.OPENALEX_ID] = _rich_text_value(publication.openalex_id)
    url = _publication_url(publication)
    if url:
        properties[NotionProperties.URL] = {"url": url}
    return properties


def _title_value(text: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": text}}]}


def _rich_text_value(text: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": text}}]}


def _date_value(value: date) -> dict:
    return {"date": {"start": value.isoformat()}}


def _multi_select_value(names: Iterable[str]) -> dict:
    return {"multi_select": [{"name": name} for name in sorted(names)]}


def _notes_text(
    decision: RelevanceDecision,
    work_type: WorkTypeClassification,
    version_duplicate_note: str = "",
) -> str:
    if decision.matched_terms:
        relevance_text = f"{decision.reason} (matched: {', '.join(decision.matched_terms)})"
    else:
        relevance_text = decision.reason

    parts = [relevance_text, f"Work type: {work_type.category} ({work_type.reason})"]
    if version_duplicate_note:
        parts.append(version_duplicate_note)
    return "\n\n".join(parts)


def _publication_url(publication: NormalizedPublication) -> str | None:
    if publication.doi:
        return f"https://doi.org/{publication.doi}"
    if publication.openalex_id:
        return f"https://openalex.org/{publication.openalex_id}"
    return None


def _read_multi_select(page: dict, property_name: str) -> set[str]:
    prop = (page.get("properties") or {}).get(property_name) or {}
    values = prop.get("multi_select") or []
    return {value["name"] for value in values if value.get("name")}


# --- generic page-property readers (used by the drafting pipeline) -------------


def read_title(page: dict, property_name: str = NotionProperties.NAME) -> str:
    prop = (page.get("properties") or {}).get(property_name) or {}
    return "".join(part.get("plain_text", "") for part in prop.get("title", []))


def read_rich_text(page: dict, property_name: str) -> str | None:
    prop = (page.get("properties") or {}).get(property_name) or {}
    parts = prop.get("rich_text") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text or None


def read_select(page: dict, property_name: str) -> str | None:
    prop = (page.get("properties") or {}).get(property_name) or {}
    select = prop.get("select")
    return select.get("name") if select else None


def read_multi_select_names(page: dict, property_name: str) -> list[str]:
    return sorted(_read_multi_select(page, property_name))


# --- draft property builders ----------------------------------------------------


def _draft_preview(text: str, max_length: int = DRAFT_PREVIEW_MAX_LENGTH) -> str:
    """A short property-level preview of the draft post. The complete draft lives
    in the page body (see build_generated_section_blocks) — this exists only so
    the row is scannable from the database table view."""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def build_draft_success_properties(
    content: DraftContent, *, source_basis: str, model_name: str, drafted_date: date
) -> dict:
    """Properties for a successful draft generation.

    The complete internal summary, key story angle, why-it-matters, and social
    post text are written to the page body (build_generated_section_blocks),
    not here — only a short Draft Social Post preview plus the pipeline-facts
    properties are set. Status, Researchers, Editorial Notes, and publication
    metadata are never mentioned here — they are preserved by construction, the
    same pattern used by the roster upsert above.
    """
    return {
        NotionProperties.DRAFT_SOCIAL_POST: _rich_text_value(
            _draft_preview(content.draft_social_post)
        ),
        NotionProperties.DRAFT_STATUS: {"select": {"name": NOTION_DRAFTED_STATUS}},
        NotionProperties.DRAFT_ERROR: _rich_text_value(""),
        NotionProperties.DRAFTED_DATE: _date_value(drafted_date),
        NotionProperties.DRAFT_SOURCE_BASIS: {"select": {"name": source_basis}},
        NotionProperties.DRAFT_MODEL: _rich_text_value(model_name),
    }


def build_draft_error_properties(*, error_message: str, drafted_date: date) -> dict:
    """Properties for a failed draft generation.

    Per the project's failure-behavior rule: retain the item, leave the
    generated content fields untouched, store the error, and mark it as
    needing attention — never silently discard.
    """
    return {
        NotionProperties.DRAFT_STATUS: {"select": {"name": NOTION_NEEDS_ATTENTION_STATUS}},
        NotionProperties.DRAFT_ERROR: _rich_text_value(error_message),
        NotionProperties.DRAFTED_DATE: _date_value(drafted_date),
    }


# --- generated-draft page body (blocks) -----------------------------------------
#
# The full draft (social post, internal summary, key story angle, why-it-matters,
# generation metadata) lives in the page body as blocks, not in properties — a
# single rich_text property value is capped by Notion well below what a real
# draft needs, and truncating it silently would violate the project's
# never-silently-discard rule. Every block below the marker heading is
# pipeline-owned; every block above it (including Editorial Notes, which is a
# property, not a block, and is never touched by this module) is human content.


def _text_object(content: str) -> dict:
    return {"type": "text", "text": {"content": content}}


def _chunk_text(text: str, max_length: int = MAX_RICH_TEXT_LENGTH) -> list[str]:
    """Split text into pieces no longer than max_length, without truncating
    anything — every character of the input appears in the output."""
    if not text:
        return [""]
    return [text[i : i + max_length] for i in range(0, len(text), max_length)]


def _paragraph_block(text: str) -> dict:
    rich_text = [_text_object(text)] if text else []
    return {"type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _paragraph_blocks(text: str) -> list[dict]:
    """One paragraph block per blank-line-delimited paragraph (preserving
    paragraph breaks), with any paragraph longer than MAX_RICH_TEXT_LENGTH split
    across additional paragraph blocks so no single rich-text `text.content`
    value ever exceeds the limit. Never truncates."""
    if not text:
        return [_paragraph_block("")]
    blocks: list[dict] = []
    for paragraph in text.split("\n\n"):
        blocks.extend(_paragraph_block(chunk) for chunk in _chunk_text(paragraph))
    return blocks


def _heading_block(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"type": key, key: {"rich_text": [_text_object(text)]}}


def _bulleted_list_item(text: str) -> dict:
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [_text_object(text)]}}


def build_generated_section_blocks(
    content: DraftContent, *, source_basis: str, model_name: str, drafted_date: date
) -> list[dict]:
    """The complete pipeline-owned page-body section for a successful draft.

    Starts with the marker block used by replace_generated_section to find and
    remove this same section on rerun.
    """
    internal_summary = content.internal_summary
    if content.limitations:
        internal_summary = f"{internal_summary}\n\nLimitations: {content.limitations}"

    blocks: list[dict] = [
        _heading_block(3, GENERATED_SECTION_MARKER),
        {"type": "divider", "divider": {}},
        _heading_block(1, "Draft Social Post"),
        *_paragraph_blocks(content.draft_social_post),
        _heading_block(1, "Internal Summary"),
        *_paragraph_blocks(internal_summary),
        _heading_block(1, "Key Story Angle"),
        *_paragraph_blocks(content.key_story_angle),
        _heading_block(1, "Why It Matters"),
        *_paragraph_blocks(content.why_it_matters),
        _heading_block(1, "Generation Information"),
        _bulleted_list_item(f"Source basis: {source_basis}"),
        _bulleted_list_item(f"Model: {model_name}"),
        _bulleted_list_item(f"Drafted date: {drafted_date.isoformat()}"),
    ]
    return blocks


def _block_plain_text(block: dict) -> str:
    block_type = block.get("type")
    payload = block.get(block_type) if block_type else None
    if not isinstance(payload, dict):
        return ""
    return "".join(part.get("plain_text", "") for part in payload.get("rich_text") or [])


def _is_marker_block(block: dict) -> bool:
    return _block_plain_text(block) == GENERATED_SECTION_MARKER


def _generated_block_ids(existing_blocks: list[dict]) -> list[str]:
    """IDs of the previous generated section: the marker block and everything
    after it. Blocks before the marker are human content and are never
    returned. Generated content is always appended at the end of the page, so
    "the marker and everything after" is exactly the previous generated
    section, even if it was itself left partially duplicated by an earlier
    partial failure.
    """
    for index, block in enumerate(existing_blocks):
        if _is_marker_block(block):
            return [block["id"] for block in existing_blocks[index:] if block.get("id")]
    return []


def _append_blocks_batched(
    client: NotionClient, page_id: str, blocks: list[dict], batch_size: int = MAX_BLOCKS_PER_APPEND
) -> None:
    for i in range(0, len(blocks), batch_size):
        client.append_block_children(page_id, blocks[i : i + batch_size])


def replace_generated_section(client: NotionClient, page_id: str, blocks: list[dict]) -> None:
    """Replace the pipeline-owned generated section in a page's body with `blocks`.

    Appends the new section before deleting the old one. If the append fails
    partway (raises NotionError), the old generated section — and all human
    content — is left exactly as it was: nothing is deleted until the new
    content is confirmed written. The worst case on a later failure (deleting
    the old section) is old and new generated content coexisting, which is
    visible on the page and recoverable by rerunning, never silent data loss.
    Raises NotionError on any failure; callers must not mark the row Drafted
    unless this returns successfully.
    """
    existing_blocks = list(client.list_block_children(page_id))
    stale_block_ids = _generated_block_ids(existing_blocks)

    _append_blocks_batched(client, page_id, blocks)

    for block_id in stale_block_ids:
        client.delete_block(block_id)
