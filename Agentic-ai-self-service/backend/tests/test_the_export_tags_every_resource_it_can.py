"""Governance tags the caller asked for must appear on every resource that can hold one.

``resource_tags`` and ``tag_profile`` were two of the ten ``DeployRequest`` fields the CFN
export accepted and then silently discarded. They are the two that got a real fix instead
of a refusal, and the reason is measurable rather than aesthetic:
``ResourceTagFields.tsx`` resolves each tag from the user's input, then the selected
profile, then the tag policy's own ``default_value``. So in any organisation that sets a
default on a tag policy, ``resourceTags`` is non-empty on every export with no user input
at all -- and a refusal would have turned every CloudFormation export in that
organisation into an HTTP 400. Measured on the live stack ``acfe2e-p0920``:
``GET /api/settings/tags`` returns the three seeded policies
(``platform:application``, ``platform:group``, ``platform:owner``) with
``default_value: null``, and ``GET /api/settings/tag-profiles`` returns ``[]`` -- so a
default install is unaffected, and a bank that fills those defaults in is exactly the
caller who needs the export to work.

THE SHAPES ARE THE HARD PART, and they are not guessable. From cfn-lint's bundled
registry schemas -- the same ones CloudFormation validates against:

    AWS::BedrockAgentCore::Gateway                 Tags          MAP
    AWS::BedrockAgentCore::Memory                  Tags          MAP
    AWS::BedrockAgentCore::Runtime                 Tags          MAP
    AWS::BedrockAgentCore::RuntimeEndpoint         Tags          MAP
    AWS::BedrockAgentCore::OnlineEvaluationConfig  Tags          LIST  <- same service!
    AWS::BedrockAgentCore::PolicyEngine            Tags          LIST  <- same service!
    AWS::Bedrock::KnowledgeBase                    Tags          MAP
    AWS::Bedrock::Guardrail                        Tags          LIST
    AWS::Cognito::UserPool                         UserPoolTags  MAP   <- different NAME
    AWS::IAM::Role                                 Tags          LIST
    AWS::Lambda::Function                          Tags          LIST
    AWS::Logs::LogGroup                            Tags          LIST
    AWS::S3Vectors::Index                          Tags          LIST
    AWS::S3Vectors::VectorBucket                   Tags          LIST

There is no rule to derive: AgentCore itself is inconsistent, four of its types taking a
map and two taking a list. A wrong shape does not fail the export. It fails at
CreateChangeSet in the customer's account, on a property they never typed.
``test_cfn_lint_would_catch_a_wrong_tag_shape`` is the positive control for that claim:
swapping the two shapes produces two ``E3012`` errors, so the clean lint of the real
template means something.

AND THE TABLE HAS TO BE COMPLETE, not merely correct. ``_apply_resource_tags`` fails
CLOSED on a type it does not recognise, so an unclassified type does not produce an
untagged resource -- it produces HTTP 400 on the whole export. The first version of this
file classified 9 of the 26 types ``generate()`` can emit and every test was green,
because ``AWS::BedrockAgentCore::GatewayTarget`` is only emitted when a gateway and an MCP
server are BOTH enabled and no test combined them. Found by cfn-linting a live export per
component combination. :class:`TestTheTablesCoverTheWholeGenerator` now parses the
generator's own source, so completeness no longer depends on guessing which toggles reach
which branch.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import yaml
from app.models.deployment_models import DeployRequest, RuntimeConfig
from app.services.cfn_template_generator import (
    _TAG_PROPERTY,
    _UNTAGGABLE,
    CfnExportUnsupportedError,
    CfnTemplateGenerator,
)

TAGS = {"platform:application": "acf", "platform:owner": "omar", "CostCentre": "ECB-42"}


def _request(**overrides) -> DeployRequest:
    return DeployRequest(
        node_id="n1",
        config=RuntimeConfig(
            name="agent",
            model={"model_id": "us.anthropic.claude-sonnet-5"},
            system_prompt="hi",
            entrypoint="agent.py",
        ),
        **overrides,
    )


#: Component combinations, because each one adds resource types the others do not and a
#: type is only tagged if this pass knows about it.
#:
#: THE COMBINATIONS ARE NOT OPTIONAL. The first version of this file tested one component
#: at a time and passed while ``AWS::BedrockAgentCore::GatewayTarget`` was unclassified,
#: because that type is only emitted when a gateway and an MCP server are BOTH enabled --
#: so the fail-closed branch turned that canvas into an HTTP 400 for every caller
#: supplying tags. Caught by linting a live export per combination.
_SHAPES = {
    "minimal": {},
    "gateway": {"gateway_config": {"enabled": True, "name": "gw"}},
    "memory": {"memory_config": {"enabled": True}},
    # No bare "mcp" row: an MCP-server node with no gateway is now refused outright
    # (it emitted references to three resources it never created), so it cannot appear
    # here. The refusal is pinned in test_the_export_refuses_what_it_cannot_express.py.
    "gateway+mcp": {
        "gateway_config": {"enabled": True, "name": "gw"},
        "mcp_server_config": {"enabled": True, "name": "m"},
    },
    "everything": {
        "gateway_config": {"enabled": True, "name": "gw"},
        "mcp_server_config": {"enabled": True, "name": "m"},
        "memory_config": {"enabled": True},
    },
}

#: Every ``"Type": "AWS::…"`` literal in the generator's source. Parsed rather than listed,
#: because a list here is a second place to forget to update -- which is the exact defect
#: this guard exists to catch.
_GENERATOR_SOURCE = Path(inspect.getsourcefile(CfnTemplateGenerator)).read_text()


def _every_emittable_type() -> set[str]:
    return set(re.findall(r'"Type":\s*"(AWS::[A-Za-z0-9:]+)"', _GENERATOR_SOURCE))


def _template(**overrides) -> dict:
    bundle = CfnTemplateGenerator().generate(_request(**overrides))
    return yaml.safe_load(bundle.template_yaml)


def _applied(resource: dict) -> dict[str, str] | None:
    """The tags on a resource, normalized out of whichever shape it uses."""
    props = resource.get("Properties") or {}
    for prop in ("Tags", "UserPoolTags"):
        if prop in props:
            value = props[prop]
            if isinstance(value, dict):
                return {str(k): str(v) for k, v in value.items()}
            return {str(t["Key"]): str(t["Value"]) for t in value if isinstance(t, dict)}
    return None


@pytest.mark.parametrize("label", sorted(_SHAPES))
class TestEveryTaggableResourceCarriesTheTags:
    def test_no_taggable_resource_is_left_untagged(self, label):
        template = _template(resource_tags=TAGS, **_SHAPES[label])
        missed = []
        for logical_id, resource in template["Resources"].items():
            rtype = resource["Type"]
            if rtype not in _TAG_PROPERTY:
                continue
            got = _applied(resource) or {}
            absent = [k for k in TAGS if got.get(k) != TAGS[k]]
            if absent:
                missed.append(f"{logical_id} ({rtype}) is missing {absent}")
        assert not missed, (
            "the caller's governance tags are absent from resources that can carry "
            "them, so a cost-allocation or ownership audit of the deployed stack would "
            "come back short: " + "; ".join(missed)
        )

    def test_every_emitted_type_is_classified(self, label):
        """Fail closed on drift. A resource type added to the generator later is either
        tagged or recorded as untaggable with a reason -- it cannot quietly become a
        resource that escapes the organisation's tagging policy."""
        template = _template(resource_tags=TAGS, **_SHAPES[label])
        unclassified = sorted(
            {
                resource["Type"]
                for resource in template["Resources"].values()
                if resource["Type"] not in _TAG_PROPERTY
                and resource["Type"] not in _UNTAGGABLE
                and not resource["Type"].startswith("Custom::")
            }
        )
        assert not unclassified, (
            f"these emitted resource types are in neither _TAG_PROPERTY nor _UNTAGGABLE: "
            f"{unclassified}. Add each with its real tag property (check the registry "
            f"schema, do not guess the shape) or record why it cannot be tagged."
        )

    def test_an_untagged_export_is_unchanged(self, label):
        """The tagging pass must be inert when no tags were asked for. Otherwise every
        existing export changes, and the blast radius of this feature is everyone."""
        template = _template(**_SHAPES[label])
        tagged = {
            lid: resource["Type"]
            for lid, resource in template["Resources"].items()
            if _applied(resource) is not None and resource["Type"] in _TAG_PROPERTY
        }
        # The generator may set its own tags; what must not happen is the caller's.
        for lid, _rtype in tagged.items():
            got = _applied(template["Resources"][lid]) or {}
            assert not (set(TAGS) & set(got)), f"{lid} carries caller tags that were never supplied: {got}"


class TestTheTablesCoverTheWholeGenerator:
    """The guard that does not depend on reaching a branch.

    Every assertion in :class:`TestEveryTaggableResourceCarriesTheTags` can only see types
    the parametrized canvases actually emit. A component nobody thought to combine -- or
    one reachable only through a config this file does not construct -- escapes it
    entirely, and ``_apply_resource_tags`` fails CLOSED on an unknown type, so the
    consequence is not an untagged resource but a 400 on the whole export. That is how
    ``GatewayTarget`` shipped: 9 of 26 types were classified and every test was green.

    Reading the source is the only oracle that covers all 26 without having to guess which
    combination of toggles reaches each one.
    """

    def test_the_source_parse_is_actually_finding_types(self):
        """Vacuity guard. If the regex drifts from the source's shape it returns an empty
        set and the test below passes while checking nothing."""
        found = _every_emittable_type()
        assert len(found) >= 20, f"only parsed {len(found)} resource types from the generator: {sorted(found)}"
        assert "AWS::IAM::Role" in found and "AWS::BedrockAgentCore::Runtime" in found, sorted(found)

    def test_every_type_the_generator_can_emit_is_classified(self):
        unclassified = sorted(_every_emittable_type() - set(_TAG_PROPERTY) - set(_UNTAGGABLE))
        assert not unclassified, (
            f"the generator can emit {unclassified}, which appear in neither _TAG_PROPERTY "
            f"nor _UNTAGGABLE. _apply_resource_tags fails CLOSED on an unknown type, so a "
            f"canvas reaching any of these returns HTTP 400 for every caller who supplied "
            f"tags -- the export stops working rather than emitting an untagged resource. "
            f"Read each type's registry schema (do not guess the map-vs-list shape) and add "
            f"it to the right table."
        )

    def test_no_type_is_in_both_tables(self):
        """A type in both is ambiguous, and which one wins is an ordering accident."""
        both = sorted(set(_TAG_PROPERTY) & set(_UNTAGGABLE))
        assert not both, f"classified as both taggable and untaggable: {both}"


class TestTheShapesAreRight:
    def test_each_type_uses_the_shape_its_schema_declares(self):
        """The drift guard on :data:`_TAG_PROPERTY` itself, checked against cfn-lint's
        bundled registry schemas rather than against my memory of them."""
        manager = pytest.importorskip(
            "cfnlint.schema", reason="cfn-lint not installed; the shape table cannot be cross-checked"
        ).PROVIDER_SCHEMA_MANAGER
        for rtype, (prop, shape) in sorted(_TAG_PROPERTY.items()):
            schema = manager.get_resource_schema("us-east-1", rtype).schema
            declared = schema["properties"].get(prop)
            assert declared is not None, f"{rtype} has no {prop} property in its registry schema"
            ref = declared.get("$ref")
            if ref:
                declared = schema["definitions"][ref.split("/")[-1]]
            expected = "map" if declared.get("type") == "object" else "list"
            assert expected == shape, (
                f"_TAG_PROPERTY says {rtype}.{prop} is a {shape}, but its registry schema "
                f"says {expected}. A template built on the wrong one fails at "
                f"CreateChangeSet in the customer's account."
            )

    def test_the_untaggable_types_really_have_no_tag_property(self):
        """The other half: a type recorded as untaggable must not actually be taggable,
        or the reason is a mistake that permanently exempts a real resource."""
        manager = pytest.importorskip("cfnlint.schema", reason="cfn-lint not installed").PROVIDER_SCHEMA_MANAGER
        for rtype in sorted(_UNTAGGABLE):
            props = manager.get_resource_schema("us-east-1", rtype).schema.get("properties", {})
            # Exact property names only. A substring sweep for "tag" reports
            # MaximumEventAgeInSeconds ("evenTAGe") as a tag property, which is how this
            # assertion was nearly written the wrong way round.
            assert "Tags" not in props and "UserPoolTags" not in props, (
                f"{rtype} IS taggable ({sorted(k for k in props if 'Tag' in k)}), so recording it as "
                f"untaggable exempts a real resource from the organisation's tagging policy"
            )

    def test_map_and_list_are_emitted_verbatim_in_the_right_shape(self):
        template = _template(resource_tags=TAGS, gateway_config={"enabled": True, "name": "gw"})
        role = template["Resources"]["GatewayRole"]["Properties"]["Tags"]
        assert isinstance(role, list) and all(set(t) == {"Key", "Value"} for t in role), role
        gateway = template["Resources"]["AgentCoreGateway"]["Properties"]["Tags"]
        assert isinstance(gateway, dict), gateway
        pool = template["Resources"]["CognitoUserPool"]["Properties"]
        assert "UserPoolTags" in pool, "Cognito's tag property is UserPoolTags, not Tags"
        assert isinstance(pool["UserPoolTags"], dict), pool["UserPoolTags"]


class TestAnUnusableTagIsRefusedNotEmitted:
    """Emitting a tag the service will reject produces a stack that fails partway through
    creation and rolls back -- strictly worse than an error the caller can read."""

    @pytest.mark.parametrize(
        "tags,expect",
        [
            ({"aws:cloudformation:thing": "x"}, "aws:"),
            ({"bad,key": "x"}, "bad,key"),
            ({"ok": "has,a,comma"}, "resource tag 'ok'"),
            ({"k" * 129: "x"}, "129"),
            ({"ok": "v" * 257}, "257"),
        ],
    )
    def test_it_is_refused_with_the_reason(self, tags, expect):
        with pytest.raises(CfnExportUnsupportedError) as err:
            _template(resource_tags=tags)
        assert expect in str(err.value), f"the refusal does not name what is wrong: {err.value}"

    def test_too_many_tags_is_refused(self):
        with pytest.raises(CfnExportUnsupportedError) as err:
            _template(resource_tags={f"k{i}": "v" for i in range(51)})
        assert "50" in str(err.value)

    def test_a_legal_tag_set_is_not_refused(self):
        """Vacuity guard: the validator must not reject everything."""
        assert _template(resource_tags=TAGS)["Resources"]


class TestAGeneratorTagIsNotOverwritten:
    def test_an_existing_tag_wins(self):
        """A caller tag must not clobber a tag the template itself relies on. Checked by
        colliding on a key the generator sets rather than by asserting on the mechanism."""
        template = _template(gateway_config={"enabled": True, "name": "gw"})
        preset = {}
        for logical_id, resource in template["Resources"].items():
            got = _applied(resource)
            if got:
                preset[logical_id] = got
        if not preset:
            pytest.skip("the generator currently sets no tags of its own, so nothing can collide")
        logical_id, existing = next(iter(preset.items()))
        key = next(iter(existing))
        after = _template(
            gateway_config={"enabled": True, "name": "gw"},
            resource_tags={**TAGS, key: "CALLER-VALUE-MUST-NOT-WIN"},
        )
        assert (_applied(after["Resources"][logical_id]) or {})[key] == existing[key]


def test_cfn_lint_would_catch_a_wrong_tag_shape(tmp_path):
    """The positive control for the whole file.

    A clean cfn-lint run over the emitted template only means the shapes are right if
    cfn-lint checks shapes at all. Swap the two -- a map onto IAM::Role, a list onto the
    AgentCore gateway -- and it must object. Run in-process; shelling out to the CLI
    would make this depend on PATH.

    Scoped to ONE region on purpose. ``lint_all`` checks every region cfn-lint knows, and
    AgentCore does not exist in about twenty of them (af-south-1, ap-east-1, the two
    cn- regions, us-gov-east-1, ...), so it reports 60 E3006 "resource type does not
    exist" errors that say nothing about this template. That is a real property of the
    export -- it is deployable only where AgentCore is -- and not what this test measures.
    """
    lint = pytest.importorskip("cfnlint.api", reason="cfn-lint not installed")
    template = _template(resource_tags=TAGS, gateway_config={"enabled": True, "name": "gw"})

    def errors(tpl, rule=None):
        found = lint.lint(yaml.safe_dump(tpl), regions=["us-east-1"])
        return [m for m in found if (str(m.rule.id) == rule if rule else str(m.rule.id).startswith("E"))]

    clean = errors(template)
    assert not clean, f"the emitted template has CloudFormation errors: {[str(m) for m in clean]}"

    template["Resources"]["GatewayRole"]["Properties"]["Tags"] = {"a": "b"}
    template["Resources"]["AgentCoreGateway"]["Properties"]["Tags"] = [{"Key": "a", "Value": "b"}]
    broken = errors(template, rule="E3012")
    assert len(broken) >= 2, (
        "cfn-lint did not object to a map where a list belongs and a list where a map "
        f"belongs, so the clean run above proves nothing about tag shapes. Got: {[str(m) for m in broken]}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
