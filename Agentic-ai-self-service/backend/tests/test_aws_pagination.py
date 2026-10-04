from unittest.mock import MagicMock, call

import pytest
from app.services import gateway_deployer
from app.services.aws_pagination import list_all
from app.step_handlers import memory_step, policy_step


def test_list_all_follows_every_real_token():
    client = MagicMock()
    client.list_things.side_effect = [
        {"items": [{"id": "a"}], "nextToken": "page-2"},
        {"items": [{"id": "b"}]},
    ]

    assert list_all(
        client,
        "list_things",
        item_keys=("items",),
        request={"owner": "x", "maxResults": 100},
    ) == [{"id": "a"}, {"id": "b"}]
    assert client.list_things.call_args_list == [
        call(owner="x", maxResults=100),
        call(owner="x", maxResults=100, nextToken="page-2"),
    ]


def test_list_all_accepts_alternate_result_and_token_casing():
    client = MagicMock()
    client.list_things.side_effect = [
        {"Summaries": [{"id": "a"}], "NextToken": "next"},
        {"Summaries": [{"id": "b"}]},
    ]

    assert list_all(
        client,
        "list_things",
        item_keys=("items", "Summaries"),
        request={"MaxResults": 60},
        request_token="NextToken",
        response_token="NextToken",
    ) == [{"id": "a"}, {"id": "b"}]


def test_list_all_does_not_follow_a_mock_token_forever():
    client = MagicMock()
    client.list_things.return_value = {"items": []}

    assert (
        list_all(
            client,
            "list_things",
            item_keys=("items",),
            request={},
        )
        == []
    )
    client.list_things.assert_called_once_with()


def test_list_all_rejects_a_repeated_real_token():
    client = MagicMock()
    client.list_things.side_effect = [
        {"items": [{"id": "a"}], "nextToken": "same"},
        {"items": [{"id": "b"}], "nextToken": "same"},
    ]

    with pytest.raises(RuntimeError, match="repeated pagination token"):
        list_all(
            client,
            "list_things",
            item_keys=("items",),
            request={},
        )


def test_list_all_honours_iam_truncation_and_marker_fields():
    client = MagicMock()
    client.list_things.side_effect = [
        {
            "Things": ["a"],
            "IsTruncated": True,
            "Marker": "page-2",
        },
        {
            "Things": ["b"],
            "IsTruncated": False,
        },
    ]

    assert list_all(
        client,
        "list_things",
        item_keys=("Things",),
        request={"RoleName": "r"},
        request_token="Marker",
        response_token="Marker",
        continuation_flag="IsTruncated",
    ) == ["a", "b"]
    assert client.list_things.call_args_list == [
        call(RoleName="r"),
        call(RoleName="r", Marker="page-2"),
    ]


def test_list_all_rejects_truncated_iam_page_without_a_marker():
    client = MagicMock()
    client.list_things.return_value = {
        "Things": ["a"],
        "IsTruncated": True,
    }

    with pytest.raises(RuntimeError, match="without a usable Marker"):
        list_all(
            client,
            "list_things",
            item_keys=("Things",),
            request={},
            request_token="Marker",
            response_token="Marker",
            continuation_flag="IsTruncated",
        )


def test_gateway_name_lookup_finds_a_gateway_on_page_two():
    client = MagicMock()
    client.list_gateways.side_effect = [
        {
            "items": [{"name": "other", "gatewayId": "gw-1"}],
            "nextToken": "page-2",
        },
        {"gatewaySummaries": [{"name": "wanted", "gatewayId": "gw-2"}]},
    ]

    gateways = gateway_deployer._list_all_gateways(client)

    assert [gateway["gatewayId"] for gateway in gateways] == ["gw-1", "gw-2"]
    assert client.list_gateways.call_args_list == [
        call(),
        call(nextToken="page-2"),
    ]


def test_gateway_target_conflict_reuses_the_target_on_page_two():
    client = MagicMock()
    client.create_gateway_target.side_effect = Exception("target already exists")
    client.list_gateway_targets.side_effect = [
        {
            "items": [{"name": "other", "targetId": "target-1"}],
            "nextToken": "page-2",
        },
        {"items": [{"name": "wanted", "targetId": "target-2"}]},
    ]

    target = gateway_deployer._create_gateway_target_with_retry(
        client,
        "gw-1",
        "wanted",
        {"gatewayIdentifier": "gw-1", "name": "wanted"},
    )

    assert target == {"name": "wanted", "targetId": "target-2"}
    assert client.list_gateway_targets.call_args_list == [
        call(gatewayIdentifier="gw-1", maxResults=50),
        call(
            gatewayIdentifier="gw-1",
            maxResults=50,
            nextToken="page-2",
        ),
    ]


def test_gateway_tool_action_resolution_includes_page_two_targets():
    client = MagicMock()
    page_one = {
        "items": [{"name": "First", "targetId": "target-1"}],
        "nextToken": "page-2",
    }
    page_two = {"gatewayTargetSummaries": [{"name": "Second", "gatewayTargetId": "target-2"}]}
    client.list_gateway_targets.side_effect = [
        page_one,
        page_two,
        page_one,
        page_two,
    ]

    def _detail(tool_name: str) -> dict:
        return {
            "status": "READY",
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "toolSchema": {
                            "inlinePayload": [{"name": tool_name}],
                        }
                    }
                }
            },
        }

    client.get_gateway_target.side_effect = [
        _detail("one"),
        _detail("two"),
        _detail("one"),
        _detail("two"),
    ]

    actions, expected = gateway_deployer._resolve_gateway_tool_actions(
        client,
        "gw-1",
        timeout=1,
    )

    assert actions == ["First___one", "Second___two"]
    assert expected == 2
    assert client.list_gateway_targets.call_args_list == [
        call(gatewayIdentifier="gw-1", maxResults=50),
        call(gatewayIdentifier="gw-1", maxResults=50, nextToken="page-2"),
        call(gatewayIdentifier="gw-1", maxResults=50),
        call(gatewayIdentifier="gw-1", maxResults=50, nextToken="page-2"),
    ]


def test_policy_tool_manifest_includes_a_target_on_page_two():
    client = MagicMock()
    client.list_gateway_targets.side_effect = [
        {
            "items": [{"name": "First", "targetId": "target-1"}],
            "nextToken": "page-2",
        },
        {
            "gatewayTargetSummaries": [
                {"name": "Second", "gatewayTargetId": "target-2"},
            ]
        },
    ]
    client.get_gateway_target.side_effect = [
        {
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "toolSchema": {"inlinePayload": [{"name": "one"}]},
                    }
                }
            }
        },
        {
            "targetConfiguration": {
                "mcp": {
                    "lambda": {
                        "toolSchema": {"inlinePayload": [{"name": "two"}]},
                    }
                }
            }
        },
    ]

    assert policy_step._read_gateway_tool_actions(client, "gw-1") == [
        "First___one",
        "Second___two",
    ]


def test_memory_conflict_lookup_finds_the_memory_on_page_two():
    client = MagicMock()
    client.list_memories.side_effect = [
        {
            "memories": [{"id": "other-memory-1234567890"}],
            "nextToken": "page-2",
        },
        {
            "memorySummaries": [
                {"id": "wanted_memory-1234567890", "status": "ACTIVE"},
            ]
        },
    ]

    assert (
        memory_step._find_memory_by_name(
            client,
            "wanted_memory",
            retries=0,
        )
        == "wanted_memory-1234567890"
    )
    assert client.list_memories.call_args_list == [
        call(maxResults=100),
        call(maxResults=100, nextToken="page-2"),
    ]
