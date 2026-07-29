"""Slack sink: a small injectable HTTP client for Slack's Web API, mirroring
sinks/notion.py's pattern (injectable httpx.Client, bounded retries, tests
mock it via httpx.MockTransport rather than the official slack_sdk package)
so no test in this suite makes a live network call.

Currently used only by scripts/test_slack_connection.py to verify
SLACK_BOT_TOKEN and post one harmless message to each configured channel.
No other pipeline code sends Slack messages yet — see CLAUDE.md: Slack
delivery is a later phase, and this module implements no automatic-posting
behavior of its own.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping

import httpx
from dotenv import find_dotenv, load_dotenv
from pydantic import BaseModel, SecretStr

DEFAULT_SLACK_BASE_URL = "https://slack.com/api/"
BOT_TOKEN_PREFIX = "xoxb-"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_REQUIRED_ENV_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_PAPERS_CHANNEL_ID",
    "SLACK_SIGNALS_CHANNEL_ID",
)


class SlackConfigError(RuntimeError):
    """Raised when required Slack environment variables are missing or
    obviously malformed (e.g. a bot token that doesn't start with
    "xoxb-")."""


class SlackConfig(BaseModel):
    bot_token: SecretStr
    papers_channel_id: str
    signals_channel_id: str


def load_slack_config(*, env: Mapping[str, str] | None = None) -> SlackConfig:
    """Load SLACK_BOT_TOKEN, SLACK_PAPERS_CHANNEL_ID, SLACK_SIGNALS_CHANNEL_ID
    (.env supported via python-dotenv). Pass `env` explicitly in tests to
    avoid touching the real process environment.

    Every problem found (missing variables, and/or a bot token that doesn't
    look like a bot token) is reported together in one SlackConfigError,
    not just the first one — so a caller sees the whole picture in one run.
    """
    if env is None:
        # find_dotenv(usecwd=True): search for .env from the current working
        # directory, matching sinks/notion.py's require_env — without
        # usecwd, python-dotenv always resolves to this repo's own .env.
        load_dotenv(find_dotenv(usecwd=True))
        env = os.environ

    values = {name: env.get(name) for name in _REQUIRED_ENV_NAMES}
    problems = [f"{name} is missing" for name in _REQUIRED_ENV_NAMES if not values[name]]

    token = values["SLACK_BOT_TOKEN"]
    if token and not token.startswith(BOT_TOKEN_PREFIX):
        problems.append(f"SLACK_BOT_TOKEN must begin with {BOT_TOKEN_PREFIX!r}")

    if problems:
        raise SlackConfigError("; ".join(problems))

    return SlackConfig(
        bot_token=values["SLACK_BOT_TOKEN"],
        papers_channel_id=values["SLACK_PAPERS_CHANNEL_ID"],
        signals_channel_id=values["SLACK_SIGNALS_CHANNEL_ID"],
    )


class SlackError(RuntimeError):
    """Raised when a Slack API request fails at the transport/HTTP level
    after bounded retries (a non-2xx HTTP status, or a connection failure).

    A well-formed Slack response with `"ok": false` — invalid_auth,
    missing_scope, channel_not_found, not_in_channel, etc. — is NOT raised
    as this. Slack's Web API returns HTTP 200 for those; they're routine,
    expected API outcomes for the caller to inspect via the returned dict's
    `ok`/`error` keys, not transport failures.
    """


class SlackClient:
    """Thin, injectable HTTP client for Slack's Web API.

    Pass an `httpx.Client` (e.g. built on `httpx.MockTransport`) to avoid
    live network calls in tests. The token is only ever used to build the
    Authorization header — it is never logged or included in any exception
    text.
    """

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        *,
        token: str = "",
        base_url: str = DEFAULT_SLACK_BASE_URL,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            )
            self._owns_client = True
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SlackClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def auth_test(self) -> dict:
        # No leading slash: base_url has a path component ("/api/"), and a
        # leading-slash path would discard it during httpx's URL joining
        # (RFC 3986 absolute-path reference), hitting slack.com/auth.test
        # instead of slack.com/api/auth.test.
        return self._post("auth.test", json={})

    def post_message(self, channel: str, text: str) -> dict:
        return self._post("chat.postMessage", json={"channel": channel, "text": text})

    def _post(self, path: str, *, json: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.post(path, json=json)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code < 300:
                    return response.json()
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    raise SlackError(
                        f"Slack API returned HTTP {response.status_code} for POST {path}: "
                        f"{response.text}"
                    )
                last_error = httpx.HTTPStatusError(
                    f"Slack API returned HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            if attempt < self._max_retries:
                time.sleep(self._backoff_seconds * (2**attempt))

        raise SlackError(
            f"Slack API request failed after {self._max_retries + 1} attempt(s): {last_error}"
        )
