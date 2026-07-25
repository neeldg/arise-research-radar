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


def test_get_data_source_returns_json(mock_notion_client: Callable[..., NotionClient]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/data_sources/ds-123"
        return httpx.Response(200, json={"id": "ds-123", "properties": {"Name": {"type": "title"}}})

    client = mock_notion_client(handler)
    data_source = client.get_data_source("ds-123")

    assert data_source["id"] == "ds-123"
    assert data_source["properties"]["Name"]["type"] == "title"


def test_update_data_source_sends_only_given_properties(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body
        sent_body = json.loads(request.content)
        assert request.method == "PATCH"
        assert request.url.path == "/v1/data_sources/ds-123"
        return httpx.Response(200, json={"id": "ds-123"})

    client = mock_notion_client(handler)
    new_properties = {"Internal Summary": {"rich_text": {}}}
    result = client.update_data_source("ds-123", new_properties)

    assert result["id"] == "ds-123"
    assert sent_body == {"properties": new_properties}


def test_query_eligible_drafts_default_filters_new_openalex_not_started(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body
        sent_body = json.loads(request.content)
        assert request.method == "POST"
        assert request.url.path == "/v1/data_sources/ds-123/query"
        return httpx.Response(200, json={"results": [{"id": "page-1"}]})

    client = mock_notion_client(handler)
    results = client.query_eligible_drafts("ds-123", limit=3)

    assert [r["id"] for r in results] == ["page-1"]
    assert sent_body["page_size"] == 3
    conditions = sent_body["filter"]["and"]
    assert {"property": "Status", "select": {"equals": "New"}} in conditions
    assert {"property": "Source", "select": {"equals": "OpenAlex"}} in conditions
    draft_status_condition = next(c for c in conditions if "or" in c)
    assert {"property": "Draft Status", "select": {"is_empty": True}} in draft_status_condition[
        "or"
    ]
    assert {
        "property": "Draft Status",
        "select": {"equals": "Not Started"},
    } in draft_status_condition["or"]


def test_query_eligible_drafts_force_excludes_only_approved(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    sent_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body
        sent_body = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    client = mock_notion_client(handler)
    client.query_eligible_drafts("ds-123", limit=5, force=True)

    conditions = sent_body["filter"]["and"]
    assert {
        "property": "Draft Status",
        "select": {"does_not_equal": "Approved"},
    } in conditions


def test_query_eligible_drafts_caps_results_at_limit(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"id": "page-1"}, {"id": "page-2"}, {"id": "page-3"}]}
        )

    client = mock_notion_client(handler)
    results = client.query_eligible_drafts("ds-123", limit=2)

    assert len(results) == 2


def test_append_block_children_uses_patch(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    """Regression test: Notion's append-block-children endpoint is a PATCH, not
    a POST — a live write against a real page 404/405s with the wrong verb even
    though mocked tests using the wrong verb would still pass."""
    sent_body: dict = {}
    sent_method: str = ""
    sent_path: str = ""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        nonlocal sent_body, sent_method, sent_path
        sent_body = json.loads(request.content)
        sent_method = request.method
        sent_path = request.url.path
        return httpx.Response(200, json={"results": [{"id": "block-new"}]})

    client = mock_notion_client(handler)
    children = [{"type": "paragraph", "paragraph": {"rich_text": []}}]
    result = client.append_block_children("page-123", children)

    assert sent_method == "PATCH"
    assert sent_path == "/v1/blocks/page-123/children"
    assert sent_body == {"children": children}
    assert result["results"][0]["id"] == "block-new"


def test_delete_block_sends_delete_request(
    mock_notion_client: Callable[..., NotionClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/blocks/block-123"
        return httpx.Response(200, json={"id": "block-123", "archived": True})

    client = mock_notion_client(handler)
    result = client.delete_block("block-123")

    assert result == {"id": "block-123", "archived": True}
