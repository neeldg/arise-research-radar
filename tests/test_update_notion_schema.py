import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.update_notion_schema import (
    DRAFT_SOURCE_BASIS_OPTIONS,
    DRAFT_STATUS_OPTIONS,
    SLACK_STATUS_OPTIONS,
    desired_properties,
    main,
    plan_migration,
)

from arise_radar.sinks.notion import NotionClient

DATA_SOURCE_ID = "ds-123"

EXISTING_ROSTER_PROPERTIES = {
    "Name": {"type": "title", "title": {}},
    "Canonical Key": {"type": "rich_text", "rich_text": {}},
    "Status": {"type": "select", "select": {"options": []}},
}


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "secret-token-value")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", DATA_SOURCE_ID)


# --- plan_migration: pure logic, no I/O -------------------------------------


def test_plan_migration_all_missing_are_added() -> None:
    plan = plan_migration({}, desired_properties())
    assert set(plan.to_add) == set(desired_properties())
    assert plan.already_present == []
    assert plan.conflicts == []


def test_plan_migration_matching_type_is_left_alone() -> None:
    existing = {"Internal Summary": {"type": "rich_text", "rich_text": {}}}
    plan = plan_migration(existing, {"Internal Summary": {"rich_text": {}}})
    assert plan.to_add == []
    assert plan.already_present == ["Internal Summary"]
    assert plan.conflicts == []


def test_plan_migration_type_conflict_is_refused() -> None:
    existing = {"Draft Status": {"type": "rich_text", "rich_text": {}}}
    plan = plan_migration(
        existing, {"Draft Status": {"select": {"options": [{"name": "Drafted"}]}}}
    )
    assert plan.to_add == []
    assert plan.already_present == []
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].property_name == "Draft Status"
    assert plan.conflicts[0].existing_type == "rich_text"
    assert plan.conflicts[0].desired_type == "select"


def test_plan_migration_mixed_case() -> None:
    existing = {
        "Internal Summary": {"type": "rich_text", "rich_text": {}},  # matches -> present
        "Draft Status": {"type": "rich_text", "rich_text": {}},  # conflict
        # Draft Model, Draft Error, etc. absent -> to_add
    }
    plan = plan_migration(existing, desired_properties())
    assert "Internal Summary" in plan.already_present
    assert "Draft Status" not in plan.to_add
    assert any(c.property_name == "Draft Status" for c in plan.conflicts)
    assert "Draft Model" in plan.to_add


def test_desired_properties_have_expected_select_options() -> None:
    desired = desired_properties()
    status_options = {opt["name"] for opt in desired["Draft Status"]["select"]["options"]}
    assert status_options == set(DRAFT_STATUS_OPTIONS)
    basis_options = {opt["name"] for opt in desired["Draft Source Basis"]["select"]["options"]}
    assert basis_options == set(DRAFT_SOURCE_BASIS_OPTIONS)
    slack_status_options = {opt["name"] for opt in desired["Slack Status"]["select"]["options"]}
    assert slack_status_options == set(SLACK_STATUS_OPTIONS)


def test_desired_properties_include_all_four_slack_notification_properties() -> None:
    desired = desired_properties()
    assert desired["Slack Status"]["select"]["options"]
    assert desired["Slack Timestamp"] == {"rich_text": {}}
    assert desired["Slack Error"] == {"rich_text": {}}
    assert desired["Slack Notified Date"] == {"date": {}}


# --- CLI: dry run -------------------------------------------------------------


def test_dry_run_makes_no_write_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": EXISTING_ROSTER_PROPERTIES})
        raise AssertionError(f"dry run must not write: {request.method} {request.url}")

    client = mock_notion_client(handler)

    exit_code = main(["--dry-run"], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would add 13 properties" in captured.out
    assert "Internal Summary" in captured.out
    assert "Dry run: no write requests were made." in captured.out
    assert "secret-token-value" not in captured.out


# --- CLI: real run -------------------------------------------------------------


def test_real_run_adds_only_missing_properties_preserving_existing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": EXISTING_ROSTER_PROPERTIES})
        if request.method == "PATCH" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            nonlocal sent_body
            sent_body = json.loads(request.content)
            return httpx.Response(200, json={"id": DATA_SOURCE_ID})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Added 13 properties." in captured.out
    assert "secret-token-value" not in captured.out

    sent_properties = sent_body["properties"]
    assert set(sent_properties) == set(desired_properties())
    # The pre-existing roster properties must never appear in the write payload.
    assert "Name" not in sent_properties
    assert "Canonical Key" not in sent_properties
    assert "Status" not in sent_properties


def test_real_run_refuses_conflicting_property_and_still_adds_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    sent_body: dict = {}
    existing = {
        **EXISTING_ROSTER_PROPERTIES,
        "Draft Status": {"type": "rich_text", "rich_text": {}},  # wrong type on purpose
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": existing})
        if request.method == "PATCH" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            nonlocal sent_body
            sent_body = json.loads(request.content)
            return httpx.Response(200, json={"id": DATA_SOURCE_ID})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = mock_notion_client(handler)

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "REFUSING 1 conflicting property" in captured.out
    assert "Draft Status" in captured.out
    assert "Added 12 properties." in captured.out
    assert "Draft Status" not in sent_body["properties"]


def test_idempotent_second_run_makes_no_write_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    _set_env(monkeypatch)
    fully_migrated_properties = {
        **EXISTING_ROSTER_PROPERTIES,
        **{
            name: {"type": next(iter(definition)), **definition}
            for name, definition in desired_properties().items()
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/data_sources/{DATA_SOURCE_ID}":
            return httpx.Response(200, json={"properties": fully_migrated_properties})
        raise AssertionError(
            f"already-migrated schema must trigger no write: {request.method} {request.url}"
        )

    client = mock_notion_client(handler)

    exit_code = main([], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No properties to add." in captured.out
    assert "Nothing to migrate — schema is already up to date." in captured.out


# --- credentials ---------------------------------------------------------------


def test_missing_credentials_returns_clear_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env.
    monkeypatch.chdir(tmp_path)

    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_DATA_SOURCE_ID" in captured.err
