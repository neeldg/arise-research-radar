import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.test_slack_connection import PAPERS_TEST_MESSAGE, SIGNALS_TEST_MESSAGE, main

from arise_radar.sinks.slack import SlackClient, SlackConfigError, load_slack_config

FAKE_TOKEN = "xoxb-fake-secret-value-12345"
PAPERS_CHANNEL_ID = "C_PAPERS_123"
SIGNALS_CHANNEL_ID = "C_SIGNALS_456"


def _set_env(monkeypatch: pytest.MonkeyPatch, *, token: str = FAKE_TOKEN) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", token)
    monkeypatch.setenv("SLACK_PAPERS_CHANNEL_ID", PAPERS_CHANNEL_ID)
    monkeypatch.setenv("SLACK_SIGNALS_CHANNEL_ID", SIGNALS_CHANNEL_ID)


def _auth_response(*, ok: bool = True, error: str | None = None) -> httpx.Response:
    if ok:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "team": "ARISE AI",
                "team_id": "T123",
                "user": "arise-radar-bot",
                "user_id": "U123",
                "bot_id": "B123",
            },
        )
    return httpx.Response(200, json={"ok": False, "error": error})


def _message_response(
    *, ok: bool = True, ts: str = "1700000000.000100", error: str | None = None
) -> httpx.Response:
    if ok:
        return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": ts})
    return httpx.Response(200, json={"ok": False, "error": error})


# --- load_slack_config: pure validation, no I/O -------------------------------------


def test_load_slack_config_missing_all_reports_all_three() -> None:
    with pytest.raises(SlackConfigError) as exc_info:
        load_slack_config(env={})

    message = str(exc_info.value)
    assert "SLACK_BOT_TOKEN" in message
    assert "SLACK_PAPERS_CHANNEL_ID" in message
    assert "SLACK_SIGNALS_CHANNEL_ID" in message


def test_load_slack_config_missing_one_reports_only_that_one() -> None:
    with pytest.raises(SlackConfigError) as exc_info:
        load_slack_config(
            env={"SLACK_BOT_TOKEN": FAKE_TOKEN, "SLACK_PAPERS_CHANNEL_ID": PAPERS_CHANNEL_ID}
        )

    message = str(exc_info.value)
    assert "SLACK_SIGNALS_CHANNEL_ID" in message
    assert "SLACK_BOT_TOKEN" not in message
    assert "SLACK_PAPERS_CHANNEL_ID" not in message


def test_load_slack_config_token_without_xoxb_prefix_is_rejected() -> None:
    with pytest.raises(SlackConfigError, match="xoxb-"):
        load_slack_config(
            env={
                "SLACK_BOT_TOKEN": "xoxp-wrong-token-type",
                "SLACK_PAPERS_CHANNEL_ID": PAPERS_CHANNEL_ID,
                "SLACK_SIGNALS_CHANNEL_ID": SIGNALS_CHANNEL_ID,
            }
        )


def test_load_slack_config_valid_env() -> None:
    config = load_slack_config(
        env={
            "SLACK_BOT_TOKEN": FAKE_TOKEN,
            "SLACK_PAPERS_CHANNEL_ID": PAPERS_CHANNEL_ID,
            "SLACK_SIGNALS_CHANNEL_ID": SIGNALS_CHANNEL_ID,
        }
    )
    assert config.bot_token.get_secret_value() == FAKE_TOKEN
    assert config.papers_channel_id == PAPERS_CHANNEL_ID
    assert config.signals_channel_id == SIGNALS_CHANNEL_ID


# --- CLI: missing / invalid configuration --------------------------------------------


def test_missing_env_vars_reported_clearly_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_PAPERS_CHANNEL_ID", raising=False)
    monkeypatch.delenv("SLACK_SIGNALS_CHANNEL_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env.
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "SLACK_BOT_TOKEN" in captured.err
    assert "SLACK_PAPERS_CHANNEL_ID" in captured.err
    assert "SLACK_SIGNALS_CHANNEL_ID" in captured.err


def test_invalid_token_prefix_reported_clearly_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch, token="wrong-prefix-token")

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "xoxb-" in captured.err
    assert "wrong-prefix-token" not in captured.err
    assert "wrong-prefix-token" not in captured.out


# --- CLI: successful connection --------------------------------------------------


def test_successful_auth_and_both_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)
    sent_texts: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        if request.url.path == "/api/chat.postMessage":
            payload = json.loads(request.content.decode())
            sent_texts[payload["channel"]] = payload["text"]
            return _message_response(ok=True, ts="1700000000.000100")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Authentication: OK" in captured.out
    assert "ARISE AI" in captured.out
    assert "B123" in captured.out
    assert PAPERS_CHANNEL_ID in captured.out
    assert SIGNALS_CHANNEL_ID in captured.out
    assert "1700000000.000100" in captured.out
    assert sent_texts[PAPERS_CHANNEL_ID] == PAPERS_TEST_MESSAGE
    assert sent_texts[SIGNALS_CHANNEL_ID] == SIGNALS_TEST_MESSAGE


# --- CLI: auth.test failures -----------------------------------------------------


def test_invalid_auth_stops_before_posting_any_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=False, error="invalid_auth")
        raise AssertionError("must not post messages after auth.test fails")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "invalid_auth" in captured.err
    assert "Authentication: OK" not in captured.out


# --- CLI: chat.postMessage failures -----------------------------------------------


def test_missing_scope_on_channel_post_reported_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        if request.url.path == "/api/chat.postMessage":
            return _message_response(ok=False, error="missing_scope")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "missing_scope" in captured.err


def test_channel_not_found_reported_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        if request.url.path == "/api/chat.postMessage":
            return _message_response(ok=False, error="channel_not_found")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "channel_not_found" in captured.err


def test_not_in_channel_reported_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        if request.url.path == "/api/chat.postMessage":
            return _message_response(ok=False, error="not_in_channel")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "not_in_channel" in captured.err


# --- CLI: one channel fails, the other still gets attempted ------------------------


def test_one_channel_succeeds_while_other_fails_still_attempts_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        if request.url.path == "/api/chat.postMessage":
            body = json.loads(request.content.decode())
            if body["channel"] == PAPERS_CHANNEL_ID:
                return _message_response(ok=True, ts="1700000000.000100")
            return _message_response(ok=False, error="channel_not_found")
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_slack_client(handler)
    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 1  # overall failure...
    assert (
        f"Papers channel ({PAPERS_CHANNEL_ID}): message sent" in captured.out
    )  # ...but Papers still succeeded
    assert "channel_not_found" in captured.err
    assert SIGNALS_CHANNEL_ID in captured.err


# --- token never appears in any output, success or failure -------------------------


def test_token_never_appears_in_output_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth.test":
            return _auth_response(ok=True)
        return _message_response(ok=True)

    client = mock_slack_client(handler)
    main([], client=client)
    captured = capsys.readouterr()

    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err


def test_token_never_appears_in_output_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_slack_client: Callable[..., SlackClient],
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return _auth_response(ok=False, error="invalid_auth")

    client = mock_slack_client(handler)
    main([], client=client)
    captured = capsys.readouterr()

    assert FAKE_TOKEN not in captured.out
    assert FAKE_TOKEN not in captured.err
