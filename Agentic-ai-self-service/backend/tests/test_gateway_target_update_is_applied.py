"""F-74: READY is not proof that an in-place gateway-target update landed."""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "src")

from app.services import gateway_deployer as gd  # noqa: E402

GW = "gateway-1"
TARGET = "target-1"
NAME = "DynamicTools"
OLD_ARN = "arn:aws:lambda:us-east-1:123456789012:function:old"
NEW_ARN = "arn:aws:lambda:us-east-1:123456789012:function:new"


def _params(
    *,
    arn: str = NEW_ARN,
    tools: list[dict] | None = None,
    metadata: dict | None = None,
    private_endpoint: dict | None = None,
    credentials: list[dict] | None = None,
) -> dict:
    out = {
        "gatewayIdentifier": GW,
        "name": NAME,
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": arn,
                    "toolSchema": {
                        "inlinePayload": tools
                        if tools is not None
                        else [{"name": "lookup", "description": "new schema"}]
                    },
                }
            }
        },
        "credentialProviderConfigurations": (
            credentials if credentials is not None else [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
        ),
    }
    if metadata is not None:
        out["metadataConfiguration"] = metadata
    if private_endpoint is not None:
        out["privateEndpoint"] = private_endpoint
    return out


def _detail(params: dict, *, status: str = "READY", reasons: list[str] | None = None) -> dict:
    detail = {
        "targetId": TARGET,
        "name": NAME,
        "status": status,
    }
    for key in gd._TARGET_REPLACE_KEYS:
        if key in params:
            detail[key] = params[key]
    if reasons is not None:
        detail["statusReasons"] = reasons
    return detail


class _ControlPlane:
    """Return one pre-update detail, then a scripted sequence of read-backs."""

    def __init__(
        self,
        readbacks: list[dict],
        *,
        update_response: dict | None = None,
        before_params: dict | None = None,
    ) -> None:
        self.readbacks = list(readbacks)
        self.update_response = update_response or {}
        self.before_params = before_params or _params(arn=OLD_ARN)
        self.updated = False
        self.update_calls: list[dict] = []
        self.post_update_reads = 0

    def list_gateway_targets(self, **_kwargs):
        return {"items": [{"name": NAME, "targetId": TARGET}]}

    def get_gateway_target(self, **_kwargs):
        if not self.updated:
            # The family-provenance read made before UpdateGatewayTarget.
            return _detail(self.before_params)
        self.post_update_reads += 1
        index = min(self.post_update_reads - 1, len(self.readbacks) - 1)
        return self.readbacks[index]

    def update_gateway_target(self, **kwargs):
        self.update_calls.append(kwargs)
        self.updated = True
        return self.update_response


@pytest.fixture(autouse=True)
def _no_poll_sleep(monkeypatch):
    monkeypatch.setattr(gd.time, "sleep", lambda _seconds: None)


def test_a_stale_pre_update_ready_snapshot_is_not_success():
    desired = _params()
    old_ready = _detail(_params(arn=OLD_ARN))
    applied_ready = _detail(desired)
    ctrl = _ControlPlane([old_ready, applied_ready])

    with gd.collecting_target_records() as records:
        result = gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert result["targetConfiguration"] == desired["targetConfiguration"]
    assert ctrl.post_update_reads == 2, (
        "the first post-update read was already READY but still carried the old Lambda; "
        "returning after one read is the stale-READY defect"
    )
    assert records == [
        {
            "target_id": TARGET,
            "name": NAME,
            "family": "lambda",
            "digest": gd.target_replace_digest(desired),
            # provenance: an in-place update of an existing target is "updated", never "created"
            "arm": "updated",
            "source_runtime_arn": "",
            "source_runtime_id": "",
        }
    ]


@pytest.mark.parametrize(
    ("family", "before_configuration", "desired_configuration", "credentials"),
    [
        (
            "lambda",
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": OLD_ARN,
                        "toolSchema": {"inlinePayload": [{"name": "before"}]},
                    }
                }
            },
            {
                "mcp": {
                    "lambda": {
                        "lambdaArn": NEW_ARN,
                        "toolSchema": {"inlinePayload": [{"name": "after"}]},
                    }
                }
            },
            [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        ),
        (
            "openApiSchema",
            {"mcp": {"openApiSchema": {"inlinePayload": '{"openapi":"3.0.0","info":{"version":"1"}}'}}},
            {"mcp": {"openApiSchema": {"inlinePayload": '{"openapi":"3.0.0","info":{"version":"2"}}'}}},
            None,
        ),
        (
            "smithyModel",
            {"mcp": {"smithyModel": {"inlinePayload": "namespace before"}}},
            {"mcp": {"smithyModel": {"inlinePayload": "namespace after"}}},
            [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        ),
        (
            "mcpServer",
            {
                "mcp": {
                    "mcpServer": {
                        "endpoint": "https://before.example.com/mcp",
                        "listingMode": "DYNAMIC",
                    }
                }
            },
            {
                "mcp": {
                    "mcpServer": {
                        "endpoint": "https://after.example.com/mcp",
                        "listingMode": "DYNAMIC",
                    }
                }
            },
            [
                {
                    "credentialProviderType": "OAUTH",
                    "credentialProvider": {
                        "oauthCredentialProvider": {
                            "providerArn": (
                                "arn:aws:bedrock-agentcore:us-east-1:123456789012:"
                                "token-vault/default/oauth2credentialprovider/mcp"
                            ),
                            "scopes": ["invoke"],
                        }
                    },
                }
            ],
        ),
    ],
    ids=["lambda", "openapi", "smithy", "mcp-server"],
)
def test_every_platform_target_family_requires_matching_ready_readback(
    family,
    before_configuration,
    desired_configuration,
    credentials,
):
    before = {
        "gatewayIdentifier": GW,
        "name": NAME,
        "targetConfiguration": before_configuration,
    }
    desired = {
        "gatewayIdentifier": GW,
        "name": NAME,
        "targetConfiguration": desired_configuration,
    }
    if credentials is not None:
        before["credentialProviderConfigurations"] = credentials
        desired["credentialProviderConfigurations"] = credentials
    ctrl = _ControlPlane(
        [_detail(before), _detail(desired)],
        before_params=before,
    )

    with gd.collecting_target_records() as records:
        result = gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert result["targetConfiguration"] == desired_configuration
    assert ctrl.post_update_reads == 2
    assert ctrl.update_calls[0]["targetConfiguration"] == desired_configuration
    assert gd.target_replace_digest(before) != gd.target_replace_digest(desired)
    assert records[0]["family"] == family


def test_ready_forever_with_the_old_configuration_is_unproven():
    desired = _params()
    ctrl = _ControlPlane([_detail(_params(arn=OLD_ARN))])

    with gd.collecting_target_records() as records:
        with pytest.raises(gd.GatewayTargetUnproven, match="targetConfiguration"):
            gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert ctrl.post_update_reads == 30
    assert records == [], "desired-state provenance must be recorded only after applied read-back"


def test_the_update_response_itself_cannot_fake_applied_readback():
    desired = _params()
    ctrl = _ControlPlane(
        [_detail(_params(arn=OLD_ARN))],
        update_response=_detail(desired),
    )

    with pytest.raises(gd.GatewayTargetUnproven):
        gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert ctrl.post_update_reads == 30, "the proof must come from GetGatewayTarget after the write"


@pytest.mark.parametrize(
    "terminal",
    ["FAILED", "CREATE_FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL"],
)
def test_a_terminal_update_failure_keeps_the_services_reason(terminal):
    desired = _params()
    ctrl = _ControlPlane(
        [
            _detail(
                desired,
                status=terminal,
                reasons=["the gateway role cannot invoke the requested function"],
            )
        ]
    )

    with pytest.raises(gd._TargetTerminalFailure) as error:
        gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    text = str(error.value)
    assert terminal in text
    assert "gateway role cannot invoke" in text
    assert ctrl.post_update_reads == 1


RESOLVE_REASON = (
    "Please check the OAuth setup. Failed to resolve hostname: "
    "ac-mcp-mcp-server-gateway-clbxt7jc.auth.us-east-1.amazoncognito.com (Service: AgentCredentialProvider, Status Code: 400)"
)


def test_an_unresolved_oauth_hostname_is_retried_and_the_landed_update_is_confirmed():
    """Live (2026-09-28, run 13): the service's resolver had not yet seen the Cognito auth domain
    the MCP step created moments earlier and marked the target UPDATE_UNSUCCESSFUL. The update is
    idempotent; propagation completes within a minute."""
    desired = _params(credentials=_oauth())
    ctrl = _ControlPlane(
        [
            _detail(desired, status="UPDATE_UNSUCCESSFUL", reasons=[RESOLVE_REASON]),
            _detail({**desired, "credentialProviderConfigurations": _oauth(grantType="CLIENT_CREDENTIALS")}),
        ]
    )

    result = gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert result["status"] == "READY"
    assert len(ctrl.update_calls) == 2, "the update must be re-issued after the resolution failure"
    assert ctrl.update_calls[0] == ctrl.update_calls[1]


def test_a_persisting_oauth_hostname_failure_is_still_terminal_after_the_bounded_retries():
    desired = _params(credentials=_oauth())
    ctrl = _ControlPlane([_detail(desired, status="UPDATE_UNSUCCESSFUL", reasons=[RESOLVE_REASON])])

    with pytest.raises(gd._TargetTerminalFailure) as error:
        gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert "Failed to resolve hostname" in str(error.value)
    assert len(ctrl.update_calls) == 1 + gd._OAUTH_RESOLVE_RETRIES


def test_any_other_terminal_reason_is_not_retried():
    desired = _params(credentials=_oauth())
    ctrl = _ControlPlane(
        [_detail(desired, status="UPDATE_UNSUCCESSFUL", reasons=["the gateway role cannot invoke it"])]
    )

    with pytest.raises(gd._TargetTerminalFailure):
        gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert len(ctrl.update_calls) == 1


def test_readback_normalization_accepts_reordered_set_like_collections():
    credentials = [
        {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {
                    "providerArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/p",
                    "scopes": ["write", "read"],
                }
            },
        }
    ]
    metadata = {
        "allowedRequestHeaders": ["x-b", "x-a"],
        "allowedQueryParameters": ["second", "first"],
    }
    private_endpoint = {
        "managedVpcResource": {
            "vpcIdentifier": "vpc-0123456789abcdef0",
            "subnetIds": ["subnet-22222222222222222", "subnet-11111111111111111"],
            "securityGroupIds": ["sg-22222222222222222", "sg-11111111111111111"],
            "endpointIpAddressType": "IPV4",
        }
    }
    desired = _params(
        tools=[{"name": "z"}, {"name": "a"}],
        metadata=metadata,
        private_endpoint=private_endpoint,
        credentials=credentials,
    )
    observed = _params(
        tools=[{"name": "a"}, {"name": "z"}],
        metadata={
            "allowedRequestHeaders": list(reversed(metadata["allowedRequestHeaders"])),
            "allowedQueryParameters": list(reversed(metadata["allowedQueryParameters"])),
        },
        private_endpoint={
            "managedVpcResource": {
                **private_endpoint["managedVpcResource"],
                "subnetIds": list(reversed(private_endpoint["managedVpcResource"]["subnetIds"])),
                "securityGroupIds": list(reversed(private_endpoint["managedVpcResource"]["securityGroupIds"])),
            }
        },
        credentials=[
            {
                **credentials[0],
                "credentialProvider": {
                    "oauthCredentialProvider": {
                        **credentials[0]["credentialProvider"]["oauthCredentialProvider"],
                        "scopes": ["read", "write"],
                    }
                },
            }
        ],
    )
    ctrl = _ControlPlane([_detail(observed)])

    result = gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert result["status"] == "READY"
    assert ctrl.post_update_reads == 1
    assert gd.target_replace_digest(desired) == gd.target_replace_digest(observed)


def test_json_schema_required_property_order_is_set_like():
    desired = _params(
        tools=[
            {
                "name": "lookup",
                "description": "look up one tenant record",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "tenant": {"type": "string"},
                        "query": {"type": "string"},
                    },
                    "required": ["tenant", "query"],
                },
            }
        ]
    )
    observed = _params(
        tools=[
            {
                "name": "lookup",
                "description": "look up one tenant record",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tenant": {"type": "string"},
                    },
                    "required": ["query", "tenant"],
                },
            }
        ]
    )

    assert gd._target_replace_mismatches(desired, observed) == []
    assert gd.target_replace_digest(desired) == gd.target_replace_digest(observed)


@pytest.mark.parametrize(
    "changed",
    [
        {"metadataConfiguration": {"allowedRequestHeaders": ["x-tenant"]}},
        {
            "privateEndpoint": {
                "selfManagedLatticeResource": {"resourceConfigurationIdentifier": "rcfg-0123456789abcdef0"}
            }
        },
    ],
    ids=["metadata", "private-endpoint"],
)
def test_every_updateable_configuration_family_participates_in_the_digest(changed):
    baseline = _params()
    assert gd.target_replace_digest(baseline) != gd.target_replace_digest(
        {
            **baseline,
            **changed,
        }
    )


def test_order_is_preserved_for_an_ordered_target_configuration_list():
    first = {
        "gatewayIdentifier": GW,
        "name": NAME,
        "targetConfiguration": {
            "http": {
                "passthrough": {
                    "endpoint": "https://example.com/mcp",
                    "protocolType": "MCP",
                    "stickinessConfiguration": {
                        "identifier": "tenant",
                        "compositeIdentifier": ["tenant", "session"],
                    },
                }
            }
        },
    }
    reversed_identifiers = {
        **first,
        "targetConfiguration": {
            "http": {
                "passthrough": {
                    **first["targetConfiguration"]["http"]["passthrough"],
                    "stickinessConfiguration": {
                        "identifier": "tenant",
                        "compositeIdentifier": ["session", "tenant"],
                    },
                }
            }
        },
    }

    assert gd.target_replace_digest(first) != gd.target_replace_digest(reversed_identifiers)
    assert gd._target_replace_mismatches(first, reversed_identifiers) == ["targetConfiguration"]


def test_nested_empty_union_members_are_not_erased_from_target_identity():
    openapi = {
        "targetConfiguration": {"mcp": {"openApiSchema": {}}},
    }
    smithy = {
        "targetConfiguration": {"mcp": {"smithyModel": {}}},
    }

    assert gd.target_replace_digest(openapi) != gd.target_replace_digest(smithy)


def _oauth(
    provider_arn: str = "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/mcp-cred",
    **extra,
) -> list[dict]:
    return [
        {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {
                "oauthCredentialProvider": {
                    "providerArn": provider_arn,
                    "scopes": ["agentcore-mcp-gw/invoke"],
                    **extra,
                }
            },
        }
    ]


def test_the_services_default_grant_type_echoed_on_an_omitted_request_field_is_not_a_mismatch():
    """Live (2026-09-28, run 11): the MCP server target's OAuth block is requested without
    ``grantType``; the read-back carried ``grantType: CLIENT_CREDENTIALS``. The update HAD
    landed (the provider and the runtime's client were both the new ones), yet equality could
    never confirm it and every redeploy of an OAuth target was refused as unproven. The create
    path never compares, which is why the first deployment of the same template passed."""
    desired = _params(credentials=_oauth())
    observed = {**desired, "credentialProviderConfigurations": _oauth(grantType="CLIENT_CREDENTIALS")}

    assert gd._target_replace_mismatches(desired, observed) == []
    # And the digest, which only ever sees requests, is unaffected by the echo.
    assert gd.target_replace_digest(desired) == gd.target_replace_digest(_params(credentials=_oauth()))


def test_a_grant_type_the_service_did_not_apply_is_still_a_mismatch():
    desired = _params(credentials=_oauth(grantType="JWT_BEARER"))
    observed = {**desired, "credentialProviderConfigurations": _oauth(grantType="CLIENT_CREDENTIALS")}

    assert gd._target_replace_mismatches(desired, observed) == ["credentialProviderConfigurations"]


def test_a_repointed_provider_that_did_not_land_is_still_a_mismatch():
    desired = _params(
        credentials=_oauth(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/new"
        )
    )
    observed = {
        **desired,
        "credentialProviderConfigurations": _oauth(
            "arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/oauth2credentialprovider/old",
            grantType="CLIENT_CREDENTIALS",
        ),
    }

    assert gd._target_replace_mismatches(desired, observed) == ["credentialProviderConfigurations"]


def test_an_omitted_optional_top_level_field_matches_an_empty_service_echo():
    desired = _params()
    desired.pop("credentialProviderConfigurations")
    observed = {
        **desired,
        "credentialProviderConfigurations": [],
        "metadataConfiguration": {},
    }

    assert gd._target_replace_mismatches(desired, observed) == []


def test_an_unconfirmed_sensitive_schema_is_not_echoed_in_the_error():
    marker = "TOP-SECRET-SCHEMA-MARKER"
    desired = _params(tools=[{"name": "lookup", "description": marker}])
    ctrl = _ControlPlane([_detail(_params(arn=OLD_ARN))])

    with pytest.raises(gd.GatewayTargetUnproven) as error:
        gd._update_existing_gateway_target(ctrl, GW, NAME, desired)

    assert marker not in str(error.value)
