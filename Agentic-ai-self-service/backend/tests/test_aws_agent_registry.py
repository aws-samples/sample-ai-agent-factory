"""Phase 6: AWS Agent Registry adapter — GA API surface.

Pure helpers (descriptor builders, ARN parsing, name/type normalization) plus
adapter behavior against a fake control/data client that captures kwargs. No
real AWS.

These tests deliberately pin the GA wire shape, because the preview -> GA change
is a SILENT one: the deprecated `bedrock-agentcore-control` model still exposes
CreateRegistryRecord with the old `descriptorType` parameter, so a regression
back to preview spellings would not raise UnknownOperation locally.
"""

from __future__ import annotations

import json

import pytest
from app.services import aws_agent_registry as ar
from app.services.aws_agent_registry import (
    AwsAgentRegistry,
    build_a2a_descriptor,
    build_custom_descriptor,
    build_mcp_descriptor,
    normalize_record_type,
    sanitize_record_name,
)

# -- GA namespace ------------------------------------------------------------


def test_ga_service_names():
    """Registry is its own service at GA — not bedrock-agentcore."""
    assert ar.CONTROL_SERVICE == "agent-registry-control"
    assert ar.DATA_SERVICE == "agent-registry"
    assert ar.MIN_BOTO3 >= (1, 43, 66)


def test_ga_record_type_enum():
    """recordType replaced descriptorType; A2A/AGENT_SKILLS were renamed."""
    assert set(ar.RECORD_TYPES) == {"MCP", "AGENT", "CUSTOM", "SKILL"}
    assert "A2A" not in ar.RECORD_TYPES
    assert "AGENT_SKILLS" not in ar.RECORD_TYPES


# -- pure helpers ------------------------------------------------------------


def test_record_id_from_arn():
    # GA ARNs use the agent-registry service namespace.
    arn = "arn:aws:agent-registry:us-east-1:123456789012:registry/abc123def456/record/rec4567890ab"
    assert ar._record_id_from_arn(arn) == "rec4567890ab"
    assert ar._record_id_from_arn("") == ""


def test_a2a_descriptor_shape():
    d = build_a2a_descriptor("bot", "does things", "https://x/invoke", skills=[{"id": "s1", "name": "search"}])
    # GA: a flat `a2aAgentCard` descriptor holding {data, dataSchemaVersion} —
    # preview nested this as a2a.agentCard.inlineContent.
    assert set(d.keys()) == {"a2aAgentCard"}
    ac = d["a2aAgentCard"]
    assert set(ac.keys()) == {"data", "dataSchemaVersion"}
    assert ac["dataSchemaVersion"] == "0.3"
    card = json.loads(ac["data"])
    assert card["name"] == "bot" and card["url"] == "https://x/invoke"
    assert card["protocolVersion"] == "0.3"
    # skills are normalized up to the required id/name/description/tags set — the
    # live service rejects the entire card if any of the four is missing.
    assert card["skills"] == [{"id": "s1", "name": "search", "description": "search", "tags": []}]


def test_a2a_descriptor_has_no_preview_keys():
    d = build_a2a_descriptor("bot", "d", "https://x")
    assert "a2a" not in d
    assert "inlineContent" not in d["a2aAgentCard"]
    assert "schemaVersion" not in d["a2aAgentCard"]


def test_a2a_description_capped_at_100():
    d = build_a2a_descriptor("bot", "x" * 200, "https://x")
    card = json.loads(d["a2aAgentCard"]["data"])
    assert len(card["description"]) == 100


def test_custom_descriptor_roundtrips():
    d = build_custom_descriptor({"framework": "strands", "model": "claude"})
    assert set(d.keys()) == {"custom"}
    # `custom` is the one descriptor with no dataSchemaVersion member.
    assert set(d["custom"].keys()) == {"data"}
    assert json.loads(d["custom"]["data"])["framework"] == "strands"


def test_mcp_descriptor_nests_tools_under_additional_data():
    d = build_mcp_descriptor({"name": "io.github.acme/srv", "version": "1.0.0"}, tools=[{"name": "search"}])
    srv = d["mcpServer"]
    assert srv["dataSchemaVersion"] == ar.MCP_SERVER_SCHEMA_VERSION
    tools = srv["additionalData"]["tools"]
    # `inputSchema` is required per tool by the live schema and is filled in.
    assert json.loads(tools["data"]) == {
        "tools": [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]
    }
    assert tools["dataSchemaVersion"] == ar.MCP_TOOLS_SCHEMA_VERSION


def test_descriptor_data_over_the_size_cap_fails_locally():
    """`data` is capped at 102400 bytes. AWS's ValidationException doesn't say
    WHICH descriptor overflowed, and on the deploy path it lands in a best-effort
    handler — so the builders name the culprit themselves."""
    huge = {"blob": "x" * (ar._DATA_MAX + 10)}
    with pytest.raises(ValueError, match="custom"):
        build_custom_descriptor(huge)
    with pytest.raises(ValueError, match="mcpServer"):
        build_mcp_descriptor(huge)
    with pytest.raises(ValueError, match="a2aAgentCard"):
        build_a2a_descriptor("bot", "d", "https://x", skills=[huge])
    with pytest.raises(ValueError, match=r"additionalData\.tools"):
        build_mcp_descriptor({"name": "srv"}, tools=[dict(huge, name="t")])


def test_descriptor_size_is_measured_in_bytes_not_characters():
    """The cap is bytes; multi-byte characters must not slip past a len() check."""
    # 4-byte emoji: well under _DATA_MAX characters, well over it in bytes.
    payload = {"blob": "🚀" * (ar._DATA_MAX // 3)}
    with pytest.raises(ValueError, match="custom"):
        build_custom_descriptor(payload)


def test_descriptor_just_under_the_cap_is_accepted():
    d = build_custom_descriptor({"b": "x" * (ar._DATA_MAX - 100)})
    assert len(d["custom"]["data"].encode("utf-8")) <= ar._DATA_MAX


def test_mcp_descriptor_omits_additional_data_when_no_tools():
    d = build_mcp_descriptor({"name": "srv"})
    assert "additionalData" not in d["mcpServer"]


# -- live-verified descriptor content contracts -------------------------------
#
# Every assertion below was established by submitting candidates to the real GA
# service and bisecting the failures. The service rejects a bad `data` document
# with one message — "content is not in compliance with schema version 'X' for
# descriptor type 'T'" — naming neither the field nor even which sub-document, so
# none of this is discoverable from the SDK model or an error string.


def test_mcp_server_name_must_be_namespaced():
    """A bare "my-server" is REJECTED live; the name is `<namespace>/<server>`."""
    d = build_mcp_descriptor({"name": "my-server", "description": "d", "version": "1.0.0"})
    doc = json.loads(d["mcpServer"]["data"])
    assert doc["name"] == f"{ar.MCP_DEFAULT_NAMESPACE}/my-server"


def test_mcp_server_name_already_namespaced_is_left_alone():
    d = build_mcp_descriptor({"name": "io.github.acme/srv", "description": "d", "version": "2.0.0"})
    assert json.loads(d["mcpServer"]["data"])["name"] == "io.github.acme/srv"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # exactly one "/" — a second slash is rejected by the service
        ("io.github.acme/srv/extra", "io.github.acme/srv-extra"),
        # an empty half is rejected; a good namespace is kept and the server half filled
        ("io.github.acme/", "io.github.acme/server"),
        ("/srv", f"{ar.MCP_DEFAULT_NAMESPACE}/srv"),
        # "_" is legal in the server half but NOT in the namespace
        ("io_github/srv", f"{ar.MCP_DEFAULT_NAMESPACE}/io_github-srv"),
        ("io.github.acme/my-srv_1", "io.github.acme/my-srv_1"),
        # nothing usable at all still yields a valid name
        ("", f"{ar.MCP_DEFAULT_NAMESPACE}/server"),
        ("///", f"{ar.MCP_DEFAULT_NAMESPACE}/server"),
    ],
)
def test_sanitize_mcp_server_name(given, expected):
    got = ar.sanitize_mcp_server_name(given)
    assert got == expected
    # whatever we produce must satisfy the pattern the service enforces
    assert ar._MCP_NAME_RE.match(got), got


def test_mcp_server_description_and_version_are_required_and_filled():
    """Live: dropping either `description` or `version` fails validation. The old
    builder emitted only {name, version}, so every MCP record was rejected."""
    d = build_mcp_descriptor({"name": "io.github.acme/srv"})
    doc = json.loads(d["mcpServer"]["data"])
    assert doc["description"] == "io.github.acme/srv"
    assert doc["version"] == "1.0.0"


def test_mcp_server_passes_through_extra_server_json_fields():
    """The schema is open: unknown keys are accepted. Normalization must be additive
    so legitimate server.json members survive."""
    d = build_mcp_descriptor(
        {
            "name": "io.github.acme/srv",
            "description": "d",
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "https://x/mcp"}],
            "repository": {"url": "https://github.com/a/b", "source": "github"},
        }
    )
    doc = json.loads(d["mcpServer"]["data"])
    assert doc["remotes"] == [{"type": "streamable-http", "url": "https://x/mcp"}]
    assert doc["repository"]["source"] == "github"


def test_mcp_tool_without_a_name_raises():
    """`name` is required per tool and cannot be invented — a synthesized name would
    publish a tool nothing can invoke. This builder has no best-effort caller, so
    raising beats repairing."""
    with pytest.raises(ValueError, match="no 'name'"):
        build_mcp_descriptor({"name": "io.github.acme/srv"}, tools=[{"description": "d"}])


def test_mcp_tool_keeps_a_caller_supplied_input_schema():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    d = build_mcp_descriptor({"name": "io.github.acme/srv"}, tools=[{"name": "search", "inputSchema": schema}])
    got = json.loads(d["mcpServer"]["additionalData"]["tools"]["data"])["tools"][0]
    assert got["inputSchema"] == schema


def test_a2a_skills_are_normalized_to_the_required_field_set():
    """Live: a skill entry needs ALL of id/name/description/tags or the whole card
    is rejected. `tags: []` is accepted, an ABSENT `tags` is not."""
    d = build_a2a_descriptor("bot", "d", "https://x", skills=[{"name": "search"}])
    skill = json.loads(d["a2aAgentCard"]["data"])["skills"][0]
    assert skill == {"id": "search", "name": "search", "description": "search", "tags": []}


def test_a2a_skill_with_nothing_usable_still_yields_a_valid_entry():
    """Repair, not raise: this runs on the best-effort auto-register-on-deploy path,
    where dropping the whole governance record over one thin skill is worse."""
    d = build_a2a_descriptor("bot", "d", "https://x", skills=[{}, {"tags": "not-a-list"}])
    skills = json.loads(d["a2aAgentCard"]["data"])["skills"]
    assert [s["id"] for s in skills] == ["skill-1", "skill-2"]
    assert all(s["tags"] == [] for s in skills)


def test_a2a_skill_preserves_extra_keys_and_real_tags():
    d = build_a2a_descriptor(
        "bot",
        "d",
        "https://x",
        skills=[{"id": "s1", "name": "search", "description": "finds", "tags": ["web"], "examples": ["e"]}],
    )
    skill = json.loads(d["a2aAgentCard"]["data"])["skills"][0]
    assert skill["tags"] == ["web"]
    assert skill["examples"] == ["e"]


def test_agent_skills_descriptor_omits_data_schema_version():
    """Live: `agentSkillsDefinition` is the one descriptor that accepts NO
    dataSchemaVersion — every candidate value comes back "not supported for
    descriptor type 'agent_skills'". It is also the one that must wrap its list in
    a {"skills": [...]} object; a bare array is rejected."""
    d = ar.build_agent_skills_descriptor([{"name": "search"}])
    assert set(d["agentSkillsDefinition"].keys()) == {"data"}
    payload = json.loads(d["agentSkillsDefinition"]["data"])
    assert payload == {"skills": [{"id": "search", "name": "search", "description": "search", "tags": []}]}


def test_agent_skills_descriptor_with_no_skills():
    d = ar.build_agent_skills_descriptor()
    assert json.loads(d["agentSkillsDefinition"]["data"]) == {"skills": []}


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("AGENT", "AGENT"),
        ("MCP", "MCP"),
        ("SKILL", "SKILL"),
        ("CUSTOM", "CUSTOM"),
        # preview spellings
        ("A2A", "AGENT"),
        ("a2a", "AGENT"),
        ("AGENT_SKILLS", "SKILL"),
        ("custom", "CUSTOM"),
        ("mcp", "MCP"),
        # descriptor keys
        ("a2aAgentCard", "AGENT"),
        ("mcpServer", "MCP"),
        # unknown / empty degrade to CUSTOM rather than failing a deploy
        ("", "CUSTOM"),
        (None, "CUSTOM"),
        ("nonsense", "CUSTOM"),
    ],
)
def test_normalize_record_type(given, expected):
    assert normalize_record_type(given) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("my-agent", "my-agent"),
        ("my agent", "my_agent"),
        ("_leading", "leading"),
        ("travel.agent/v1", "travel.agent/v1"),
        ("!!!", "agent"),
        ("", "agent"),
    ],
)
def test_sanitize_record_name(given, expected):
    """Service pattern is [a-zA-Z0-9][a-zA-Z0-9_\\-./]* (1-255)."""
    out = sanitize_record_name(given)
    assert out == expected
    assert out[0].isalnum()
    assert 1 <= len(out) <= 255


def test_sanitize_record_name_truncates_to_255():
    assert len(sanitize_record_name("a" * 400)) == 255


# -- adapter (fake client) ---------------------------------------------------


class _Conflict(Exception):
    """Stands in for botocore's ConflictException — same duck type _is_conflict reads."""

    def __init__(self, message="A record with name 'bot' and version '1.0' already exists"):
        super().__init__(message)
        self.response = {"Error": {"Code": "ConflictException", "Message": message}}


class _FakeControl:
    def __init__(self, pages=None, registry_status="READY", conflict_on_create=False):
        self.calls = []
        # list_registry_records pages, each (records, nextToken)
        self._pages = pages or [([], None)]
        self._page_idx = 0
        self._registry_status = registry_status
        self._conflict_on_create = conflict_on_create

    def get_registry(self, **kw):
        self.calls.append(("get_registry", kw))
        # GetRegistry always returns `status`; a fake that omitted it was modelling
        # a response the service never sends, which is what let the CREATING race
        # hide (see test_available_is_false_while_the_registry_is_still_creating).
        return {"registryId": kw["registryId"], "status": self._registry_status}

    def create_registry_record(self, **kw):
        self.calls.append(("create_registry_record", kw))
        if self._conflict_on_create:
            raise _Conflict()
        return {
            "recordArn": ("arn:aws:agent-registry:us-east-1:123456789012:registry/reg1/record/rec4567890ab"),
            "status": "CREATING",
        }

    def update_registry_record(self, **kw):
        self.calls.append(("update_registry_record", kw))
        return {
            "recordId": kw["recordId"],
            "recordArn": (f"arn:aws:agent-registry:us-east-1:123456789012:registry/reg1/record/{kw['recordId']}"),
            "name": "bot",
            "recordType": "CUSTOM",
            # The service demotes an edited record out of APPROVED — verified live.
            "status": "DRAFT",
        }

    def submit_registry_record_for_approval(self, **kw):
        self.calls.append(("submit", kw))

    def update_registry_record_status(self, **kw):
        self.calls.append(("update_status", kw))

    def list_registry_records(self, **kw):
        self.calls.append(("list_registry_records", kw))
        records, token = self._pages[self._page_idx]
        self._page_idx = min(self._page_idx + 1, len(self._pages) - 1)
        return {"registryRecords": records, **({"nextToken": token} if token else {})}

    def delete_registry_record(self, **kw):
        self.calls.append(("delete_registry_record", kw))
        return {}


class _FakeData:
    def __init__(self, results):
        self._results = results
        self.calls = []

    def search_discoverable_registry_records(self, **kw):
        self.calls.append(("search", kw))
        return {"registryRecords": self._results}


def _adapter(results=None, pages=None, registry_status="READY", conflict_on_create=False):
    a = AwsAgentRegistry.__new__(AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = _FakeControl(pages=pages, registry_status=registry_status, conflict_on_create=conflict_on_create)
    a.data = _FakeData(results or [])
    return a


def test_available_true_when_get_registry_ok():
    a = _adapter()
    assert a.available() is True


# -- the registry must be READY, not merely present ---------------------------


@pytest.mark.parametrize("status", ["CREATING", "UPDATING", "DELETING", "CREATE_FAILED", "UPDATE_FAILED"])
def test_available_is_false_while_the_registry_is_still_creating(status):
    """Live-confirmed: CreateRegistryRecord against a non-READY registry fails with
    ``ConflictException: Registry is not in READY state``. available() used to only
    check that GetRegistry succeeded, so it returned True through the whole CREATING
    window — and enabling federation on a just-created registry then raced into that
    conflict on the first deploy, where auto-register swallows errors."""
    a = _adapter(registry_status=status)
    assert a.available() is False
    assert a.registry_status() == status


def test_registry_status_returns_none_when_unreadable():
    """None means "could not ask" — distinct from a status string. The router needs
    the distinction to avoid blaming a registryId that was never wrong."""

    class _Boom:
        def get_registry(self, **kw):
            raise RuntimeError("AccessDeniedException")

    a = AwsAgentRegistry.__new__(AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = _Boom()
    a.data = None
    assert a.registry_status() is None
    assert a.available() is False


def test_registry_status_none_on_old_boto3_bundle():
    a = AwsAgentRegistry.__new__(AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = None
    a.data = None
    assert a.registry_status() is None


# -- redeploy: register() is an upsert ----------------------------------------
#
# name + recordVersion is a uniqueness key and recordVersion is always "1.0" here,
# so the SECOND deployment of an agent under the same name used to raise
# ConflictException — swallowed by the deploy path's best-effort handler. The
# symptom was a record frozen at the first deployment's runtime ARN, stale forever,
# with no error surfaced anywhere.

_EXISTING = {
    "recordId": "rec000000001",
    "recordArn": "arn:aws:agent-registry:us-east-1:123456789012:registry/reg1/record/rec000000001",
    "name": "bot",
    "recordVersion": "1.0",
    "status": "APPROVED",
}


def test_register_updates_in_place_when_the_name_and_version_exist():
    a = _adapter(pages=[([_EXISTING], None)], conflict_on_create=True)
    out = a.register("bot", "CUSTOM", build_custom_descriptor({"endpoint": "https://new"}), description="redeploy")
    assert out["record_id"] == "rec000000001"
    assert out["updated"] is True
    ops = [name for name, _ in a.control.calls]
    assert "update_registry_record" in ops


def test_register_update_uses_the_optional_value_patch_envelope():
    """UpdateRegistryRecord takes a DIFFERENT descriptor shape from Create: every
    branch and leaf is wrapped in `optionalValue`. Passing the Create shape fails in
    botocore's own parameter validation, before any AWS call — so nothing but a real
    shape assertion catches it."""
    a = _adapter(pages=[([_EXISTING], None)], conflict_on_create=True)
    a.register("bot", "CUSTOM", build_custom_descriptor({"endpoint": "https://new"}))
    _, kw = next((c for c in a.control.calls if c[0] == "update_registry_record"), (None, None))
    assert kw is not None
    inner = kw["descriptors"]["optionalValue"]["custom"]["optionalValue"]
    assert json.loads(inner["data"]["optionalValue"])["endpoint"] == "https://new"
    assert kw["description"]["optionalValue"]
    assert kw["displayName"]["optionalValue"] == "bot"
    # identity members must NOT be sent — this call refreshes content only
    assert "name" not in kw and "recordType" not in kw and "recordVersion" not in kw


def test_update_envelope_carries_schema_versions_and_nested_tools():
    d = ar._to_update_descriptors(build_mcp_descriptor({"name": "io.github.acme/srv"}, tools=[{"name": "search"}]))
    server = d["optionalValue"]["mcpServer"]["optionalValue"]
    assert server["dataSchemaVersion"]["optionalValue"] == ar.MCP_SERVER_SCHEMA_VERSION
    tools = server["additionalData"]["optionalValue"]["tools"]["optionalValue"]
    assert tools["dataSchemaVersion"]["optionalValue"] == ar.MCP_TOOLS_SCHEMA_VERSION
    assert json.loads(tools["data"]["optionalValue"])["tools"][0]["name"] == "search"


def test_register_reraises_a_conflict_that_is_not_an_existing_record():
    """ConflictException ALSO means "registry is not in READY state". Treating every
    conflict as a redeploy would swallow that and silently register nothing, so the
    lookup must confirm a record exists before updating."""
    a = _adapter(pages=[([], None)], conflict_on_create=True)  # nothing found
    with pytest.raises(Exception, match="already exists"):
        a.register("bot", "CUSTOM", build_custom_descriptor({}))


def test_register_reraises_a_conflict_when_only_another_version_exists():
    other_version = dict(_EXISTING, recordVersion="2.0")
    a = _adapter(pages=[([other_version], None)], conflict_on_create=True)
    with pytest.raises(Exception, match="already exists"):
        a.register("bot", "CUSTOM", build_custom_descriptor({}))


def test_register_reraises_non_conflict_errors_untouched():
    """A ValidationException must not be reinterpreted as a redeploy."""

    class _Boom(_FakeControl):
        def create_registry_record(self, **kw):
            raise RuntimeError("ValidationException: descriptors invalid")

    a = _adapter()
    a.control = _Boom()
    with pytest.raises(RuntimeError, match="ValidationException"):
        a.register("bot", "CUSTOM", build_custom_descriptor({}))


def test_find_record_filters_server_side_by_name():
    a = _adapter(pages=[([_EXISTING], None)])
    assert a._find_record("bot", "1.0")["recordId"] == "rec000000001"
    _, kw = next(c for c in a.control.calls if c[0] == "list_registry_records")
    # `filters[].values` is capped at one entry by the service model.
    assert kw["filters"] == [{"name": "name", "values": ["bot"]}]


def test_find_record_returns_none_when_the_lookup_itself_fails():
    """A failed lookup must not be read as "no such record" — register() then
    re-raises the original conflict rather than inventing a create/update decision
    from missing data."""
    a = _adapter()
    a.control = None  # strict listing raises RegistryQueryFailed
    assert a._find_record("bot", "1.0") is None


def test_register_sends_ga_record_type_param():
    a = _adapter()
    out = a.register("bot", "AGENT", build_a2a_descriptor("bot", "d", "https://x"))
    assert out["record_id"] == "rec4567890ab"
    assert out["status"] == "CREATING"
    _, kw = a.control.calls[-1]
    assert kw["registryId"] == "reg1"
    # GA parameter name, GA enum value.
    assert kw["recordType"] == "AGENT"
    assert "descriptorType" not in kw
    assert kw["recordVersion"] == "1.0"
    assert "a2aAgentCard" in kw["descriptors"]


def test_register_normalizes_preview_record_type():
    """A caller still passing the preview "a2a" gets a GA AGENT record."""
    a = _adapter()
    out = a.register("bot", "a2a", build_a2a_descriptor("bot", "d", "https://x"))
    _, kw = a.control.calls[-1]
    assert kw["recordType"] == "AGENT"
    assert out["record_type"] == "AGENT"


def test_register_rejects_descriptor_type_mismatch():
    """recordType and descriptor key must agree — caught locally, not by AWS."""
    a = _adapter()
    with pytest.raises(ValueError, match="a2aAgentCard"):
        a.register("bot", "AGENT", build_custom_descriptor({"x": 1}))


def test_register_sanitizes_name_and_never_sends_empty_description():
    a = _adapter()
    a.register("my agent", "CUSTOM", build_custom_descriptor({"x": 1}), description="")
    _, kw = a.control.calls[-1]
    assert kw["name"] == "my_agent"
    # description has min length 1 in the service model.
    assert kw["description"]
    # displayName keeps the human-readable original.
    assert kw["displayName"] == "my agent"


def test_register_clamps_description_to_4096():
    a = _adapter()
    a.register("bot", "CUSTOM", build_custom_descriptor({"x": 1}), description="d" * 9000)
    _, kw = a.control.calls[-1]
    assert len(kw["description"]) == 4096


def test_set_status_sends_reason():
    a = _adapter()
    a.set_status("rec-1", "APPROVED", "ok via platform")
    name, kw = a.control.calls[-1]
    assert name == "update_status"
    assert kw["status"] == "APPROVED" and kw["statusReason"] == "ok via platform"


def test_submit_for_approval():
    a = _adapter()
    a.submit_for_approval("rec-1")
    assert a.control.calls[-1][0] == "submit"


def test_search_uses_ga_operation_name():
    """GA renamed SearchRegistryRecords -> SearchDiscoverableRegistryRecords."""
    a = _adapter(results=[{"name": "found-agent"}])
    assert a.search("agent")[0]["name"] == "found-agent"
    _, kw = a.data.calls[-1]
    assert kw["registryIds"] == ["reg1"]
    assert kw["searchQuery"] == "agent"


def test_search_record_type_filter_uses_structured_operator():
    """Data-plane filters is a structure with $eq/$ne/$in — not a list."""
    a = _adapter(results=[])
    a.search("agent", record_types=["a2a", "MCP"])
    _, kw = a.data.calls[-1]
    assert kw["filters"] == {"recordType": {"$in": ["AGENT", "MCP"]}}


def test_search_returns_empty_when_data_client_missing():
    a = _adapter()
    a.data = None
    assert a.search("agent") == []


def test_list_records_follows_next_token():
    """Truncated listings would silently fail-close the integration gate."""
    a = _adapter(
        pages=[
            ([{"name": "one"}], "tok1"),
            ([{"name": "two"}], None),
        ]
    )
    out = a.list_records()
    assert [r["name"] for r in out] == ["one", "two"]
    # second call carried the token forward
    second = [kw for name, kw in a.control.calls if name == "list_registry_records"][1]
    assert second["nextToken"] == "tok1"


def test_list_records_passes_filters_through():
    a = _adapter(pages=[([], None)])
    a.list_records(filters=[{"name": "status", "values": ["APPROVED"]}])
    _, kw = a.control.calls[-1]
    assert kw["filters"] == [{"name": "status", "values": ["APPROVED"]}]


def test_list_records_returns_partial_page_on_error():
    a = _adapter()

    class _Boom(_FakeControl):
        def list_registry_records(self, **kw):
            raise RuntimeError("throttled")

    a.control = _Boom()
    assert a.list_records() == []


# -- graceful degradation on an old boto3 bundle ------------------------------


def test_adapter_with_no_clients_degrades_instead_of_raising():
    """An old boto3 bundle has no agent-registry models; the feature must report
    unavailable rather than 500 the registry router."""
    a = AwsAgentRegistry.__new__(AwsAgentRegistry)
    a.registry_id = "reg1"
    a.region = "us-east-1"
    a.control = None
    a.data = None
    assert a.available() is False
    assert a.register("bot", "CUSTOM", build_custom_descriptor({})) == {}
    assert a.list_records() == []
    assert a.search("x") == []
    assert a.get("rec-1") is None
    assert a.delete("rec-1") is False
    # these are no-ops, not exceptions
    a.submit_for_approval("rec-1")
    a.set_status("rec-1", "APPROVED", "why")


def test_make_client_returns_none_for_unknown_service():
    assert ar._make_client("definitely-not-an-aws-service", "us-east-1") is None


def test_agent_registry_supported_is_a_bool():
    # Environment-dependent (depends on the installed boto3), so assert only the
    # contract: it never raises and always answers yes/no.
    assert isinstance(ar.agent_registry_supported(), bool)
