from collections.abc import Callable

import httpx

from arise_radar.sinks.notion import NotionClient


def test_get_page_returns_page_json(mock_notion_client: Callable[..., NotionClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/pages/page-123"
        return httpx.Response(200, json={"id": "page-123", "archived": False})

    client = mock_notion_client(handler)
    page = client.get_page("page-123")

    assert page == {"id": "page-123", "archived": False}


def test_list_block_children_paginates(mock_notion_client: Callable[..., NotionClient]) -> None:
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("start_cursor")
        seen_cursors.append(cursor)
        if cursor is None:
            return httpx.Response(
                200,
                json={"results": [{"id": "block-1"}], "has_more": True, "next_cursor": "page2"},
            )
        return httpx.Response(
            200, json={"results": [{"id": "block-2"}], "has_more": False, "next_cursor": None}
        )

    client = mock_notion_client(handler)
    blocks = list(client.list_block_children("page-123"))

    assert [b["id"] for b in blocks] == ["block-1", "block-2"]
    assert seen_cursors == [None, "page2"]


def test_find_child_database_matches_exact_title(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"type": "paragraph"},
                    {
                        "type": "child_database",
                        "id": "db-1",
                        "child_database": {"title": "Some Other Database"},
                    },
                    {
                        "type": "child_database",
                        "id": "db-2",
                        "child_database": {"title": "ARISE Research Radar — Test"},
                    },
                ],
                "has_more": False,
                "next_cursor": None,
            },
        )

    client = mock_notion_client(handler)
    found = client.find_child_database("page-123", "ARISE Research Radar — Test")

    assert found is not None
    assert found["id"] == "db-2"


def test_find_child_database_returns_none_when_absent(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})

    client = mock_notion_client(handler)
    found = client.find_child_database("page-123", "ARISE Research Radar — Test")

    assert found is None


def test_create_database_sends_expected_body(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body
        sent_body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/v1/databases"
        return httpx.Response(200, json={"id": "db-new", "url": "https://notion.so/db-new"})

    client = mock_notion_client(handler)
    result = client.create_database("page-123", "My Database")

    assert result["id"] == "db-new"
    assert sent_body["parent"] == {"type": "page_id", "page_id": "page-123"}
    assert sent_body["title"][0]["text"]["content"] == "My Database"
    assert "properties" not in sent_body


def test_create_data_source_sends_expected_body(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body
        sent_body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/v1/data_sources"
        return httpx.Response(200, json={"id": "ds-new"})

    client = mock_notion_client(handler)
    properties = {"Name": {"title": {}}}
    result = client.create_data_source("db-new", "My Database", properties)

    assert result["id"] == "ds-new"
    assert sent_body["parent"] == {"type": "database_id", "database_id": "db-new"}
    assert sent_body["properties"] == properties
    assert sent_body["properties"] == properties
