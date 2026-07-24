"""OpenAlex source adapter: fetches works by author ID and normalizes them.

Contains no Notion or LLM-specific logic — only translation from the OpenAlex
API into NormalizedPublication records, per the project's "normalize first" rule.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import date, timedelta

import httpx

from arise_radar.models import NormalizedPublication, Researcher, compute_canonical_key

DEFAULT_BASE_URL = "https://api.openalex.org"
DEFAULT_PER_PAGE = 100
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OpenAlexError(RuntimeError):
    """Raised when the OpenAlex API cannot be reached after bounded retries."""


class OpenAlexClient:
    """Thin, injectable HTTP client for the OpenAlex works endpoint.

    Pass an `httpx.Client` (e.g. built on `httpx.MockTransport`) to avoid live
    network calls in tests.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        mailto: str | None = None,
    ) -> None:
        self._client = http_client or httpx.Client(base_url=base_url, timeout=10.0)
        self._owns_client = http_client is None
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._mailto = mailto

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAlexClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def iter_author_works(self, openalex_author_id: str, since: date) -> Iterator[dict]:
        """Yield raw OpenAlex work records for an author, paginating by cursor."""
        cursor = "*"
        params: dict[str, str | int] = {
            "filter": f"author.id:{openalex_author_id},from_publication_date:{since.isoformat()}",
            "per_page": DEFAULT_PER_PAGE,
        }
        if self._mailto:
            params["mailto"] = self._mailto

        while cursor:
            payload = self._get_page({**params, "cursor": cursor})
            yield from payload.get("results", [])
            cursor = payload.get("meta", {}).get("next_cursor") or ""

    def _get_page(self, params: dict[str, str | int]) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.get("/works", params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                last_error = httpx.HTTPStatusError(
                    f"OpenAlex returned HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_seconds * (2**attempt))

        raise OpenAlexError(
            f"OpenAlex request failed after {self._max_retries + 1} attempt(s): {last_error}"
        ) from last_error


def fetch_researcher_publications(
    client: OpenAlexClient,
    researcher: Researcher,
    days_back: int,
    *,
    today: date | None = None,
) -> list[NormalizedPublication]:
    """Fetch and normalize a verified researcher's recent OpenAlex works."""
    if not researcher.openalex_id:
        raise ValueError(f"Researcher {researcher.id!r} has no verified OpenAlex ID")

    since = (today or date.today()) - timedelta(days=days_back)
    works = client.iter_author_works(researcher.openalex_id, since)
    return [_normalize_work(researcher, work) for work in works]


def _normalize_work(researcher: Researcher, work: dict) -> NormalizedPublication:
    doi = _clean_doi(work.get("doi"))
    openalex_work_id = _short_id(work.get("id", ""))
    return NormalizedPublication(
        researcher_id=researcher.id,
        researcher_name=researcher.name,
        title=work.get("display_name") or work.get("title") or "Untitled",
        publication_date=_parse_date(work.get("publication_date")),
        doi=doi,
        openalex_id=openalex_work_id,
        canonical_key=compute_canonical_key(doi, openalex_work_id),
    )


def _clean_doi(raw_doi: str | None) -> str | None:
    if not raw_doi:
        return None
    return raw_doi.removeprefix("https://doi.org/").lower()


def _short_id(raw_id: str) -> str:
    return raw_id.removeprefix("https://openalex.org/")


def _parse_date(raw_date: str | None) -> date | None:
    if not raw_date:
        return None
    return date.fromisoformat(raw_date)
