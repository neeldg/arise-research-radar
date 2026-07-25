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

from arise_radar.models import NormalizedPublication
from arise_radar.relevance import RelevanceDecision

DEFAULT_NOTION_BASE_URL = "https://api.notion.com"
NOTION_API_VERSION = "2025-09-03"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
NOTION_SOURCE_VALUE = "OpenAlex"
NOTION_NEW_STATUS = "New"

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

    def create_page(self, data_source_id: str, properties: dict) -> dict:
        body = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        return self._request("POST", "/v1/pages", json=body)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._request("PATCH", f"/v1/pages/{page_id}", json={"properties": properties})

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
) -> NotionUpsertResult:
    """Create or update the Notion row for one publication.

    Never called for `exclude` decisions — the caller is expected to filter
    those out first, and this raises if it happens anyway. Never raises for
    Notion API failures: those are captured in the returned result's
    action="error" so callers can continue processing the rest of a batch.
    """
    if decision.status == "exclude":
        raise ValueError("upsert_publication must not be called for excluded publications")

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
        return _create(client, data_source_id, publication, decision, detected_date, dry_run)

    return _update(client, matches[0], publication, decision, dry_run)


def _create(
    client: NotionClient,
    data_source_id: str,
    publication: NormalizedPublication,
    decision: RelevanceDecision,
    detected_date: date,
    dry_run: bool,
) -> NotionUpsertResult:
    if dry_run:
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="dry_run_create",
            detail=f"would create page for {publication.researcher_name!r}: {publication.title!r}",
        )

    properties = _build_create_properties(publication, decision, detected_date)
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
) -> NotionUpsertResult:
    page_id = existing_page.get("id")
    existing_researchers = _read_multi_select(existing_page, NotionProperties.RESEARCHERS)
    merged_researchers = existing_researchers | {publication.researcher_name}

    if dry_run:
        adds = (
            "no new researcher"
            if publication.researcher_name in existing_researchers
            else f"adds {publication.researcher_name!r}"
        )
        return NotionUpsertResult(
            canonical_key=publication.canonical_key,
            action="dry_run_update",
            page_id=page_id,
            detail=f"would update page ({adds}; researchers -> {sorted(merged_researchers)})",
        )

    properties = _build_update_properties(publication, decision, merged_researchers)
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
    publication: NormalizedPublication, decision: RelevanceDecision, detected_date: date
) -> dict:
    properties = _shared_properties(publication, decision)
    properties[NotionProperties.STATUS] = {"select": {"name": NOTION_NEW_STATUS}}
    properties[NotionProperties.DETECTED_DATE] = _date_value(detected_date)
    properties[NotionProperties.RESEARCHERS] = _multi_select_value({publication.researcher_name})
    return properties


def _build_update_properties(
    publication: NormalizedPublication, decision: RelevanceDecision, researchers: set[str]
) -> dict:
    # Status and Detected Date are intentionally omitted here: editorial status and
    # the original detection date are preserved rather than overwritten on every sync.
    # Editorial Notes is never included anywhere in this module — it is human-owned.
    properties = _shared_properties(publication, decision)
    properties[NotionProperties.RESEARCHERS] = _multi_select_value(researchers)
    return properties


def _shared_properties(publication: NormalizedPublication, decision: RelevanceDecision) -> dict:
    properties: dict[str, object] = {
        NotionProperties.NAME: _title_value(publication.title),
        NotionProperties.CANONICAL_KEY: _rich_text_value(publication.canonical_key),
        NotionProperties.SOURCE: {"select": {"name": NOTION_SOURCE_VALUE}},
        NotionProperties.RELEVANCE_STATUS: {
            "select": {"name": RELEVANCE_STATUS_LABELS.get(decision.status, decision.status)}
        },
        NotionProperties.SYSTEM_NOTES: _rich_text_value(_notes_text(decision)),
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


def _notes_text(decision: RelevanceDecision) -> str:
    if decision.matched_terms:
        return f"{decision.reason} (matched: {', '.join(decision.matched_terms)})"
    return decision.reason


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
