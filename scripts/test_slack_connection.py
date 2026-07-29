#!/usr/bin/env python3
"""Reusable connection test: verify SLACK_BOT_TOKEN via auth.test, then post
one harmless test message to each configured channel.

    python scripts/test_slack_connection.py

Requires SLACK_BOT_TOKEN, SLACK_PAPERS_CHANNEL_ID, SLACK_SIGNALS_CHANNEL_ID
(see .env.example). There is no dry-run mode — a connection test that
doesn't attempt the thing it's testing wouldn't prove anything — so running
this for real always sends two real Slack messages. Every scenario in
tests/test_slack_connection.py exercises this same code path against a fake
Slack API instead, so no live Slack call happens except a deliberate manual
run of this script.

Only prints: authentication success/failure, workspace name, bot ID,
destination channel ID, the returned Slack message timestamp, and any Slack
error code/message. SLACK_BOT_TOKEN itself is never printed.

Exits non-zero if authentication fails, or if either channel's message
fails to send — but a failure on one channel never stops the attempt on the
other, matching the project's general "one failure must not stop the rest"
convention (see e.g. sinks/notion_citations.py's upsert_citation_event).
"""

from __future__ import annotations

import sys

from arise_radar.sinks.slack import SlackClient, SlackConfigError, SlackError, load_slack_config

PAPERS_TEST_MESSAGE = "🧪 ARISE Research Radar test: new-paper notifications are connected."
SIGNALS_TEST_MESSAGE = (
    "📡 ARISE Research Radar test: citation and media notifications are connected."
)


def _send_test_message(client: SlackClient, label: str, channel_id: str, text: str) -> bool:
    try:
        result = client.post_message(channel_id, text)
    except SlackError as exc:
        print(
            f"ERROR: {label} channel ({channel_id}) message request failed: {exc}",
            file=sys.stderr,
        )
        return False

    if not result.get("ok"):
        print(
            f"ERROR: {label} channel ({channel_id}) message failed: "
            f"{result.get('error', 'unknown_error')}",
            file=sys.stderr,
        )
        return False

    print(f"{label} channel ({channel_id}): message sent, ts={result.get('ts')}")
    return True


def main(argv: list[str] | None = None, *, client: SlackClient | None = None) -> int:
    del argv  # no flags yet; accepted for interface parity with the other scripts

    try:
        config = load_slack_config()
    except SlackConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    owns_client = client is None
    active_client = client or SlackClient(token=config.bot_token.get_secret_value())

    try:
        try:
            auth = active_client.auth_test()
        except SlackError as exc:
            print(f"ERROR: auth.test request failed: {exc}", file=sys.stderr)
            return 1

        if not auth.get("ok"):
            print(
                f"ERROR: Slack authentication failed: {auth.get('error', 'unknown_error')}",
                file=sys.stderr,
            )
            return 1

        print("Authentication: OK")
        print(f"Workspace: {auth.get('team', '(unknown)')}")
        print(f"Bot ID: {auth.get('bot_id', '(unknown)')}")
        print()

        papers_ok = _send_test_message(
            active_client, "Papers", config.papers_channel_id, PAPERS_TEST_MESSAGE
        )
        signals_ok = _send_test_message(
            active_client, "Signals", config.signals_channel_id, SIGNALS_TEST_MESSAGE
        )

        return 0 if (papers_ok and signals_ok) else 1
    finally:
        if owns_client:
            active_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
