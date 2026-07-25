from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from scripts.run_roster import main

from arise_radar.sinks.notion import NotionClient
from arise_radar.sources.openalex import OpenAlexClient


def _write_single_researcher_roster(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A123
    active: true
"""
    )
    return roster_path


def _single_publication_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "A Great Paper",
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.1/xyz",
                }
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_default_cli_mode_makes_no_notion_requests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)

    roster_path = _write_single_researcher_roster(tmp_path)
    client = mock_openalex_client(_single_publication_handler)

    exit_code = main(["--roster", str(roster_path)], client=client)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Notion" not in captured.out
    assert "Notion" not in captured.err


def test_write_notion_and_dry_run_together_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roster_path = _write_single_researcher_roster(tmp_path)

    exit_code = main(["--roster", str(roster_path), "--write-notion", "--notion-dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "cannot be used together" in captured.err


def test_write_notion_missing_credentials_returns_clear_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    # Prevent load_dotenv() from picking up the developer's real repo-root .env,
    # which would otherwise silently repopulate these vars and defeat the test.
    monkeypatch.chdir(tmp_path)
    roster_path = _write_single_researcher_roster(tmp_path)

    exit_code = main(["--roster", str(roster_path), "--write-notion"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "NOTION_TOKEN" in captured.err
    assert "NOTION_DATA_SOURCE_ID" in captured.err


def test_notion_dry_run_makes_no_write_requests(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_single_publication_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--notion-dry-run"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Would create" in captured.out


def test_write_notion_creates_page_via_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_single_publication_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out


def test_one_notion_failure_does_not_stop_the_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A123
    active: true
"""
    )

    def openalex_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Paper One",
                        "publication_date": "2026-01-01",
                        "doi": "https://doi.org/10.1/one",
                    },
                    {
                        "id": "https://openalex.org/W2",
                        "display_name": "Paper Two",
                        "publication_date": "2026-01-02",
                        "doi": "https://doi.org/10.1/two",
                    },
                ],
                "meta": {"next_cursor": None},
            },
        )

    openalex_client = mock_openalex_client(openalex_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            body = request.content.decode()
            if "10.1/one" in body:
                return httpx.Response(500, json={"message": "internal error"})
            return httpx.Response(200, json={"id": "page-two"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler, max_retries=0)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out
    assert "Errors: 1" in captured.out


# --- shared DOI across two researchers: one row, not duplicate "Would create" -----


def _two_researcher_roster(tmp_path: Path) -> Path:
    roster_path = tmp_path / "roster.yaml"
    roster_path.write_text(
        """
researchers:
  - id: ethan_goh
    name: Ethan Goh
    openalex_id: A1
    active: true
  - id: adam_rodman
    name: Adam Rodman
    openalex_id: A2
    active: true
"""
    )
    return roster_path


def _shared_work_handler(request: httpx.Request) -> httpx.Response:
    filter_param = request.url.params.get("filter", "")
    if "author.id:A1" in filter_param or "author.id:A2" in filter_param:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "A Shared Paper",
                        "publication_date": "2026-01-01",
                        "doi": "https://doi.org/10.1/shared",
                        "type": "article",
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )
    raise AssertionError(f"unexpected filter {filter_param!r}")


def test_shared_doi_across_two_researchers_produces_one_dry_run_row(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _two_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_shared_work_handler)

    def notion_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        raise AssertionError("dry run must not perform create/update requests")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--notion-dry-run"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    # Exactly one dry-run row for the shared paper -- not two per-author lines
    # that would look like duplicate live writes.
    assert captured.out.count("Would create") == 1
    assert "Ethan Goh" in captured.out
    assert "Adam Rodman" in captured.out
    assert "Proposed new rows:                     1" in captured.out
    assert "Shared-author works:                   1" in captured.out


def test_shared_doi_across_two_researchers_creates_one_page_with_both_researchers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _two_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_shared_work_handler)
    create_calls = {"n": 0}
    created_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            create_calls["n"] += 1
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert create_calls["n"] == 1  # one page, not one per researcher
    names = {entry["name"] for entry in created_body["properties"]["Researchers"]["multi_select"]}
    assert names == {"Ethan Goh", "Adam Rodman"}
    assert "Created: 1" in captured.out


# --- version-family: flagged in System Notes, canonical keys never merged ---------


def _version_duplicate_handler(request: httpx.Request) -> httpx.Response:
    filter_param = request.url.params.get("filter", "")
    shared_authorships = [
        {"author": {"display_name": "Ethan Goh"}},
        {"author": {"display_name": "Adam Rodman"}},
    ]
    title = "Large Language Models for Clinical Chart Abstraction: Comparative Study"
    if "author.id:A1" in filter_param:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": title,
                        "publication_date": "2026-01-01",
                        "doi": "https://doi.org/10.2196/preprints.105583",
                        "type": "preprint",
                        "authorships": shared_authorships,
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )
    if "author.id:A2" in filter_param:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W2",
                        "display_name": title,
                        "publication_date": "2026-01-02",
                        "doi": "https://doi.org/10.20944/preprints202607.0060.v1",
                        "type": "preprint",
                        "authorships": shared_authorships,
                    }
                ],
                "meta": {"next_cursor": None},
            },
        )
    raise AssertionError(f"unexpected filter {filter_param!r}")


def test_possible_version_duplicate_is_flagged_but_both_rows_kept_separate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _two_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_version_duplicate_handler)
    created_bodies: list[dict] = []

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            created_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"page-{len(created_bodies)}"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    # Two distinct DOIs -> two separate pages, never auto-merged.
    assert len(created_bodies) == 2
    canonical_keys = {
        body["properties"]["Canonical Key"]["rich_text"][0]["text"]["content"]
        for body in created_bodies
    }
    assert canonical_keys == {
        "doi:10.2196/preprints.105583",
        "doi:10.20944/preprints202607.0060.v1",
    }
    for body in created_bodies:
        notes = body["properties"]["System Notes"]["rich_text"][0]["text"]["content"]
        assert "Possible version duplicate of:" in notes
        # Held for review, not silently left eligible for automatic drafting.
        assert body["properties"]["Draft Status"] == {"select": {"name": "Needs Attention"}}
        error_text = body["properties"]["Draft Error"]["rich_text"][0]["text"]["content"]
        assert "preprint/published-version or repository-version duplicate" in error_text
        assert "human review" in error_text
    assert "Duplicate-flagged (held for review):   2" in captured.out


# --- non-standard research objects: retained, but not draft eligible --------------


def _zenodo_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "Stanford Biodesign Digital Health Group",
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.5281/zenodo.21358702",
                }
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_zenodo_repository_is_retained_but_not_drafted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_zenodo_handler)
    created_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out  # retained -- imported, not discarded
    props = created_body["properties"]
    assert props["Draft Status"] == {"select": {"name": "Needs Attention"}}
    assert "dataset/repository" in props["Draft Error"]["rich_text"][0]["text"]["content"]
    assert "Non-standard (held for review):        1" in captured.out


def _overlapping_non_standard_and_duplicate_handler(request: httpx.Request) -> httpx.Response:
    title = "Stanford Biodesign Digital Health Group"
    authors = [{"author": {"display_name": "Ethan Goh"}}]
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": title,
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.5281/zenodo.1111",
                    "authorships": authors,
                },
                {
                    "id": "https://openalex.org/W2",
                    "display_name": title,
                    "publication_date": "2026-01-02",
                    "doi": "https://doi.org/10.1/normal-paper",
                    "type": "article",
                    "authorships": authors,
                },
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_non_standard_and_duplicate_flags_overlap_in_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """One row (the Zenodo record) is both a non-standard work type and a
    probable version duplicate of the other row at once -- both aggregate
    counters must include it, and neither silently drops the other reason."""
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_overlapping_non_standard_and_duplicate_handler)
    created_bodies: list[dict] = []

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            created_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"page-{len(created_bodies)}"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(created_bodies) == 2
    by_key = {
        body["properties"]["Canonical Key"]["rich_text"][0]["text"]["content"]: body
        for body in created_bodies
    }

    zenodo_error = by_key["doi:10.5281/zenodo.1111"]["properties"]["Draft Error"]["rich_text"][0][
        "text"
    ]["content"]
    assert "dataset/repository" in zenodo_error
    assert "preprint/published-version or repository-version duplicate" in zenodo_error

    normal_error = by_key["doi:10.1/normal-paper"]["properties"]["Draft Error"]["rich_text"][0][
        "text"
    ]["content"]
    assert "dataset/repository" not in normal_error
    assert "preprint/published-version or repository-version duplicate" in normal_error

    for body in created_bodies:
        assert body["properties"]["Draft Status"] == {"select": {"name": "Needs Attention"}}

    # Overlap: both counters include the Zenodo row, so they needn't be disjoint.
    assert "Duplicate-flagged (held for review):   2" in captured.out
    assert "Non-standard (held for review):        1" in captured.out
    assert "Standard draft-eligible works:         0" in captured.out


def _osf_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "GP AI Skin Cancer Portal Study",
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.17605/osf.io/evxkq",
                }
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_osf_registration_is_retained_but_not_drafted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_osf_handler)
    created_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out  # retained -- imported, not discarded
    props = created_body["properties"]
    assert props["Draft Status"] == {"select": {"name": "Needs Attention"}}
    assert "protocol/registration" in props["Draft Error"]["rich_text"][0]["text"]["content"]


def _normal_article_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "A Great Paper",
                    "publication_date": "2026-01-01",
                    "doi": "https://doi.org/10.1/xyz",
                    "type": "article",
                }
            ],
            "meta": {"next_cursor": None},
        },
    )


def test_normal_article_remains_draft_eligible_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mock_openalex_client: Callable[..., OpenAlexClient],
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds_123")

    roster_path = _write_single_researcher_roster(tmp_path)
    openalex_client = mock_openalex_client(_normal_article_handler)
    created_body: dict = {}

    def notion_handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.method == "POST" and request.url.path == "/v1/data_sources/ds_123/query":
            return httpx.Response(200, json={"results": []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            nonlocal created_body
            created_body = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    notion_client = mock_notion_client(notion_handler)

    exit_code = main(
        ["--roster", str(roster_path), "--write-notion"],
        client=openalex_client,
        notion_client=notion_client,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Created: 1" in captured.out
    assert "Draft Status" not in created_body["properties"]
    assert "Non-standard (held for review):        0" in captured.out
