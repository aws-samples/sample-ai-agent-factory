"""The adversarial case catalog.

Every entry is a declaration of intent, not a test body: which principal
attacks which target, what the expected decision is, which error codes and
audit records constitute acceptable proof, and which authorized positive twin
must pass for the negative result to mean anything.

Live probes are attached separately (see ``tests/adversarial/cases/probes``),
which keeps the catalog reviewable as a control matrix and makes an
unimplemented case visibly unimplemented rather than quietly absent.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

from .errors import CatalogError
from .manifest import REQUIRED_ACCOUNTS, REQUIRED_ROLES
from .outcome import FORBIDDEN_PROOF_CODES


class Domain(str, Enum):
    """Trust boundary or control family under attack."""

    SCP = "scp"
    IAM_STS = "iam-sts"
    INFERENCE_GATEWAY = "inference-gateway"
    TOOL_GATEWAY = "tool-gateway"
    MEMORY_ISOLATION = "memory-isolation"
    REGISTRY = "registry"
    PIPELINE_BYPASS = "pipeline-bypass"
    SUPPLY_CHAIN_TAMPER = "supply-chain-tamper"
    RATE_LIMITING = "rate-limiting"
    FAILURE_INJECTION = "failure-injection"
    ROLLBACK = "rollback"
    TEARDOWN = "teardown"


#: Every domain must carry at least one non-positive case.
REQUIRED_DOMAINS: tuple[Domain, ...] = tuple(Domain)


class Expectation(str, Enum):
    """The result a case asserts."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    AUTHENTICATION_DENIED = "AUTHENTICATION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    NOT_EXPOSED = "NOT_EXPOSED"
    ABSENT = "ABSENT"
    ROLLED_BACK = "ROLLED_BACK"
    DETECTED = "DETECTED"


#: Expectations that assert a control fired, and therefore require a twin.
NEGATIVE_EXPECTATIONS: frozenset[Expectation] = frozenset(
    {
        Expectation.DENY,
        Expectation.AUTHENTICATION_DENIED,
        Expectation.RATE_LIMITED,
        Expectation.GUARDRAIL_BLOCKED,
        Expectation.NOT_EXPOSED,
        Expectation.ABSENT,
        Expectation.ROLLED_BACK,
        Expectation.DETECTED,
    }
)

#: Codes acceptable when asserting a rate limit fired.
RATE_LIMIT_PROOF_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ServiceQuotaExceededException",
        "RateLimitExceeded",
    }
)


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


class AuditSource(str, Enum):
    """Where the corroborating audit record is read from."""

    CLOUDTRAIL = "cloudtrail"
    CLOUDWATCH_LOGS = "cloudwatch-logs"
    GATEWAY_ACCESS_LOG = "gateway-access-log"
    GUARDRAIL_TRACE = "guardrail-trace"
    AGENTCORE_OBSERVABILITY = "agentcore-observability"
    AGENTCORE_REGISTRY = "agentcore-registry"
    BEDROCK_INVOCATION_LOG = "bedrock-invocation-log"
    CONFIG = "aws-config"
    SECURITY_HUB = "security-hub"
    PIPELINE_EXECUTION = "pipeline-execution"
    GITHUB_AUDIT = "github-audit"
    EVIDENCE_ARCHIVE = "evidence-archive"


#: Rate-limit facets the catalog must cover.
REQUIRED_RATE_LIMIT_TAGS: frozenset[str] = frozenset(
    {"rpm", "tpm", "cps", "catch-all", "fail-open"}
)


@dataclass(frozen=True)
class AdversarialCase:
    """One catalogued adversarial or positive-twin case."""

    case_id: str
    domain: Domain
    title: str
    expectation: Expectation
    severity: Severity
    principal_ref: str
    target: str
    rationale: str
    control_refs: tuple[str, ...]
    expected_error_codes: tuple[str, ...] = ()
    expected_http_status: tuple[int, ...] = ()
    required_message_substrings: tuple[str, ...] = ()
    audit_sources: tuple[AuditSource, ...] = ()
    positive_twin: str | None = None
    tags: tuple[str, ...] = ()
    live_required: bool = True
    notes: str = ""

    @property
    def is_negative(self) -> bool:
        return self.expectation in NEGATIVE_EXPECTATIONS

    @property
    def account_key(self) -> str:
        return self.principal_ref.split(".", 1)[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "caseId": self.case_id,
            "domain": self.domain.value,
            "title": self.title,
            "expectation": self.expectation.value,
            "severity": self.severity.value,
            "principalRef": self.principal_ref,
            "target": self.target,
            "rationale": self.rationale,
            "controlRefs": list(self.control_refs),
            "expectedErrorCodes": list(self.expected_error_codes),
            "expectedHttpStatus": list(self.expected_http_status),
            "requiredMessageSubstrings": list(self.required_message_substrings),
            "auditSources": [source.value for source in self.audit_sources],
            "positiveTwin": self.positive_twin,
            "tags": list(self.tags),
            "liveRequired": self.live_required,
            "notes": self.notes,
        }


def _allow(
    case_id: str,
    domain: Domain,
    title: str,
    principal_ref: str,
    target: str,
    rationale: str,
    control_refs: tuple[str, ...],
    audit_sources: tuple[AuditSource, ...] = (AuditSource.CLOUDTRAIL,),
    tags: tuple[str, ...] = (),
    notes: str = "",
) -> AdversarialCase:
    return AdversarialCase(
        case_id=case_id,
        domain=domain,
        title=title,
        expectation=Expectation.ALLOW,
        severity=Severity.HIGH,
        principal_ref=principal_ref,
        target=target,
        rationale=rationale,
        control_refs=control_refs,
        audit_sources=audit_sources,
        tags=tags,
        notes=notes,
    )


def _negative(
    case_id: str,
    domain: Domain,
    title: str,
    expectation: Expectation,
    principal_ref: str,
    target: str,
    rationale: str,
    control_refs: tuple[str, ...],
    positive_twin: str,
    expected_error_codes: tuple[str, ...] = (),
    expected_http_status: tuple[int, ...] = (),
    required_message_substrings: tuple[str, ...] = (),
    audit_sources: tuple[AuditSource, ...] = (AuditSource.CLOUDTRAIL,),
    severity: Severity = Severity.CRITICAL,
    tags: tuple[str, ...] = (),
    notes: str = "",
) -> AdversarialCase:
    return AdversarialCase(
        case_id=case_id,
        domain=domain,
        title=title,
        expectation=expectation,
        severity=severity,
        principal_ref=principal_ref,
        target=target,
        rationale=rationale,
        control_refs=control_refs,
        expected_error_codes=expected_error_codes,
        expected_http_status=expected_http_status,
        required_message_substrings=required_message_substrings,
        audit_sources=audit_sources,
        positive_twin=positive_twin,
        tags=tags,
        notes=notes,
    )


_SCP_DENY = ("AccessDenied", "AccessDeniedException")
_SCP_MSG = ("explicit deny in a service control policy",)


_SCP_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "SCP-01-P",
        Domain.SCP,
        "Allow-listed model invoked through the platform inference Gateway",
        "workstream.agent_runtime",
        "bedrock:InvokeModel via platform inference Gateway target",
        "The allow-list must not block sanctioned models, or the denial twin "
        "proves nothing.",
        ("SCP-01 DenyNonAllowListedBedrockModels",),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.BEDROCK_INVOCATION_LOG),
    ),
    _negative(
        "SCP-01-N",
        Domain.SCP,
        "Non-allow-listed model is denied by SCP-01",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock:InvokeModel with a model id outside the allow-list",
        "An agent must not be able to reach an unapproved model even with a "
        "permissive identity policy.",
        ("SCP-01 DenyNonAllowListedBedrockModels",),
        "SCP-01-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-02-P",
        Domain.SCP,
        "Inference with an approved guardrail attached succeeds",
        "workstream.agent_runtime",
        "bedrock:Converse with guardrailIdentifier set",
        "Guardrailed inference is the sanctioned path and must work.",
        ("SCP-02 DenyBedrockInferenceWithoutGuardrail",),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.GUARDRAIL_TRACE),
    ),
    _negative(
        "SCP-02-N",
        Domain.SCP,
        "Inference without a guardrail is denied by SCP-02",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock:Converse with no guardrailIdentifier",
        "Guardrail attachment must be non-bypassable at the organization "
        "level, not merely a convention.",
        ("SCP-02 DenyBedrockInferenceWithoutGuardrail",),
        "SCP-02-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _negative(
        "SCP-02-N2",
        Domain.SCP,
        "Inference with an unapproved guardrail id is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock:Converse with a guardrail outside the approved set",
        "Attaching any guardrail must not be enough; only approved guardrails "
        "satisfy the control.",
        ("SCP-02 DenyBedrockWithoutApprovedGuardrail",),
        "SCP-02-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-04-P",
        Domain.SCP,
        "Bedrock reached over the approved VPC endpoint",
        "workstream.agent_runtime",
        "bedrock:Converse via the approved interface endpoint",
        "PrivateLink-only egress must still permit the sanctioned endpoint.",
        ("SCP-04 DenyBedrockOutsideApprovedVpce",),
    ),
    _negative(
        "SCP-04-N",
        Domain.SCP,
        "Bedrock call from outside the approved VPC endpoint is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock:Converse over the public endpoint",
        "Egress must be PrivateLink-only; a public-path call is an exfiltration "
        "route.",
        ("SCP-04 DenyBedrockOutsideApprovedVpce", "SCP-04 DenyBedrockWhenNoSourceVpce"),
        "SCP-04-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-05-P",
        Domain.SCP,
        "Platform admin can modify the managed guardrail",
        "platform.platform_admin",
        "bedrock:UpdateGuardrail",
        "The guardrail owner must retain the ability to manage it.",
        ("SCP-05 DenyGuardrailModificationOutsidePlatformAdmin",),
    ),
    _negative(
        "SCP-05-N",
        Domain.SCP,
        "Workstream admin cannot modify the managed guardrail",
        Expectation.DENY,
        "workstream.workstream_admin",
        "bedrock:UpdateGuardrail",
        "A workstream must not be able to weaken the control that constrains "
        "it.",
        ("SCP-05 DenyGuardrailModificationOutsidePlatformAdmin",),
        "SCP-05-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-06-P",
        Domain.SCP,
        "Action in an approved region succeeds",
        "workstream.agent_runtime",
        "bedrock:Converse in the primary approved region",
        "Region restriction must not break the approved region.",
        ("SCP-06 DenyActionsOutsideApprovedRegions",),
    ),
    _negative(
        "SCP-06-N",
        Domain.SCP,
        "Action in a non-approved region is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock:Converse in a region outside the approved set",
        "Data-residency commitments depend on region pinning being "
        "non-bypassable.",
        ("SCP-06 DenyActionsOutsideApprovedRegions",),
        "SCP-06-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _negative(
        "SCP-07-N",
        Domain.SCP,
        "AgentCore Runtime creation without subnets/security groups is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "bedrock-agentcore:CreateAgentRuntime with no VPC configuration",
        "A publicly reachable runtime would bypass the network boundary "
        "entirely.",
        (
            "SCP-07 DenyAgentCoreCreationWithoutSubnets",
            "SCP-07 DenyAgentCoreCreationWithoutSecurityGroups",
        ),
        "PIPE-01-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-08-P",
        Domain.SCP,
        "Private ECR pull from the platform repository succeeds",
        "workstream.agent_runtime",
        "ecr:BatchGetImage on the shared platform repository",
        "The sanctioned image path must work for the negative to be "
        "meaningful.",
        ("SCP-08 DenyEcrPublic",),
    ),
    _negative(
        "SCP-08-N",
        Domain.SCP,
        "ECR Public access is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "ecr-public:GetAuthorizationToken",
        "Pulling from ECR Public would let unreviewed images into the runtime.",
        ("SCP-08 DenyEcrPublic",),
        "SCP-08-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-09-P",
        Domain.SCP,
        "Platform admin can mutate an AgentCore Gateway",
        "platform.platform_admin",
        "bedrock-agentcore:UpdateGateway",
        "Gateway ownership must remain operable by its owner.",
        ("SCP-09 DenyGatewayMutationExceptPlatformAdmin",),
    ),
    _negative(
        "SCP-09-N",
        Domain.SCP,
        "Non-platform-admin Gateway mutation is denied",
        Expectation.DENY,
        "workstream.workstream_admin",
        "bedrock-agentcore:UpdateGateway on the central inference Gateway",
        "Editing the Gateway would let a tenant redirect inference or expose "
        "extra tools.",
        ("SCP-09 DenyGatewayMutationExceptPlatformAdmin",),
        "SCP-09-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _negative(
        "SCP-10-N",
        Domain.SCP,
        "Runtime role cannot invoke a Lambda outside the tool catalogue",
        Expectation.DENY,
        "workstream.agent_runtime",
        "lambda:InvokeFunction on a non-catalogued function",
        "Tool reach must be defined by the catalogue, not by whatever the "
        "runtime role can see.",
        ("SCP-10 DenyLambdaInvokeFromRuntimeRolesExceptCataloguedTargets",),
        "TG-01-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-11-P",
        Domain.SCP,
        "Registry admin can mutate the AgentCore Registry",
        "platform.registry_admin",
        "AgentCore Registry write",
        "The registry owner must retain write access.",
        ("SCP-11 DenyRegistryMutationExceptRegistryAdmin",),
    ),
    _negative(
        "SCP-11-N",
        Domain.SCP,
        "Registry mutation from a workstream identity is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "AgentCore Registry write",
        "The registry is the single source of truth; tenant-side writes would "
        "break it.",
        ("SCP-11 DenyRegistryMutationExceptRegistryAdmin",),
        "SCP-11-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
    _allow(
        "SCP-12-P",
        Domain.SCP,
        "Developer can read a platform-owned resource",
        "workstream.developer_readonly",
        "read of a resource tagged as platform-owned",
        "Read access is intended; only mutation is denied.",
        ("SCP-12 DenyDeveloperMutationOfPlatformOwnedResources",),
    ),
    _negative(
        "SCP-12-N",
        Domain.SCP,
        "Developer mutation of a platform-owned resource is denied",
        Expectation.DENY,
        "workstream.developer_readonly",
        "tag-conditioned mutation of a platform-owned resource",
        "Platform-owned resources must be immutable to tenant developers.",
        ("SCP-12 DenyDeveloperMutationOfPlatformOwnedResources",),
        "SCP-12-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
        required_message_substrings=_SCP_MSG,
    ),
)


_IAM_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "STS-01-P",
        Domain.IAM_STS,
        "Workstream assumes the inference Gateway invoker role with the "
        "correct ExternalId",
        "workstream.agent_runtime",
        "sts:AssumeRole platform.inference_gateway_invoker",
        "The sanctioned cross-account hop must work.",
        ("Trust policy ExternalId condition",),
    ),
    _negative(
        "STS-01-N",
        Domain.IAM_STS,
        "AssumeRole without an ExternalId is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "sts:AssumeRole platform.inference_gateway_invoker (ExternalId omitted)",
        "The confused-deputy control depends on the ExternalId condition being "
        "mandatory.",
        ("Trust policy ExternalId condition",),
        "STS-01-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "STS-02-N",
        Domain.IAM_STS,
        "AssumeRole with a wrong ExternalId is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "sts:AssumeRole platform.inference_gateway_invoker (wrong ExternalId)",
        "A guessed ExternalId must not be accepted.",
        ("Trust policy ExternalId condition",),
        "STS-01-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "STS-03-N",
        Domain.IAM_STS,
        "Workstream runtime cannot assume the platform admin role",
        Expectation.DENY,
        "workstream.agent_runtime",
        "sts:AssumeRole platform.platform_admin",
        "Privilege escalation across the platform boundary must be impossible.",
        ("Platform admin trust policy", "SCP-12 developer/platform separation"),
        "STS-01-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "STS-04-N",
        Domain.REGISTRY,
        "Unprivileged platform principal cannot assume the registry admin role",
        Expectation.DENY,
        "platform.unprivileged_probe",
        "sts:AssumeRole platform.registry_admin",
        "Registry write authority must not be reachable from a low-privilege "
        "identity in the same account.",
        ("Registry admin trust policy", "SCP-11 DenyRegistryMutationExceptRegistryAdmin"),
        "IAM-05-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _allow(
        "IAM-05-P",
        Domain.REGISTRY,
        "RegistryReaderRole can read the registry",
        "platform.registry_reader",
        "AgentCore Registry read",
        "One of the three sanctioned control-plane surfaces must function.",
        ("RegistryReaderRole",),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "IAM-05-N",
        Domain.REGISTRY,
        "RegistryReaderRole cannot write the registry",
        Expectation.DENY,
        "platform.registry_reader",
        "AgentCore Registry write",
        "A read surface that can write is an unsanctioned write path.",
        ("RegistryReaderRole", "Sanctioned control-plane surfaces"),
        "IAM-05-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "IAM-06-N",
        Domain.IAM_STS,
        "AgentBuilderInspectRole cannot mutate workstream resources",
        Expectation.DENY,
        "platform.agent_builder_inspect",
        "any mutating call against workstream resources",
        "The inspect role exists to read; a write would be a fourth, "
        "unsanctioned cross-boundary surface.",
        ("AgentBuilderInspectRole", "Sanctioned control-plane surfaces"),
        "IAM-06-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
    ),
    _allow(
        "IAM-06-P",
        Domain.IAM_STS,
        "AgentBuilderInspectRole can read workstream state",
        "platform.agent_builder_inspect",
        "read-only inspection of workstream agent state",
        "Inspection is the sanctioned purpose of this role.",
        ("AgentBuilderInspectRole",),
    ),
    _negative(
        "IAM-07-N",
        Domain.IAM_STS,
        "Management/Governance audit reader cannot write to a workload account",
        Expectation.DENY,
        "management-governance.audit_reader",
        "any mutating call in the workstream account",
        "Governance must observe without the ability to change workloads.",
        ("Audit reader least privilege",),
        "IAM-07-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
    ),
    _allow(
        "IAM-07-P",
        Domain.IAM_STS,
        "Audit reader can read the organization CloudTrail archive",
        "management-governance.audit_reader",
        "read of the organization trail / evidence archive",
        "Audit read is the sanctioned purpose and underpins every audit "
        "assertion in this suite.",
        ("Organization CloudTrail", "Evidence archive"),
        audit_sources=(AuditSource.EVIDENCE_ARCHIVE,),
    ),
)


_INFERENCE_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "INF-01-P",
        Domain.INFERENCE_GATEWAY,
        "Authenticated inference call through the Gateway succeeds",
        "workstream.agent_runtime",
        "POST /v1/chat/completions on the central inference Gateway",
        "LiteLLMModel against the Gateway's OpenAI-compatible endpoint is the "
        "golden path and must work.",
        ("AgentCore Inference Gateway", "AgentCore Identity"),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "INF-01-N",
        Domain.INFERENCE_GATEWAY,
        "Unauthenticated inference call is rejected",
        Expectation.AUTHENTICATION_DENIED,
        "workstream.unprivileged_probe",
        "POST /v1/chat/completions with no bearer token",
        "An open inference endpoint would make every downstream control "
        "irrelevant.",
        ("AgentCore Identity", "Gateway inbound auth"),
        "INF-01-P",
        expected_error_codes=("Unauthorized", "UnauthorizedException"),
        expected_http_status=(401,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
    ),
    _negative(
        "INF-02-N",
        Domain.INFERENCE_GATEWAY,
        "Non-allow-listed model requested through the Gateway is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "POST /v1/chat/completions with an unapproved model name",
        "The Gateway must enforce the model allow-list itself, not rely solely "
        "on the SCP behind it.",
        ("Gateway inference target allow-list", "SCP-01"),
        "INF-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "INF-03-N",
        Domain.INFERENCE_GATEWAY,
        "Cross-tenant inference profile use is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "inference against another tenant's Application Inference Profile",
        "Per-tenant cost attribution and isolation both collapse if a tenant "
        "can bill another tenant's profile.",
        ("Application Inference Profile per tenant", "AgentCore Policy"),
        "INF-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.BEDROCK_INVOCATION_LOG),
    ),
    _negative(
        "INF-04-N",
        Domain.INFERENCE_GATEWAY,
        "Prompt-injection payload is blocked by the guardrail",
        Expectation.GUARDRAIL_BLOCKED,
        "workstream.agent_runtime",
        "inference with a prompt-attack payload",
        "Guardrail interception must be observable in the trace, not assumed.",
        ("Managed Bedrock Guardrail", "prompt-attack filter"),
        "INF-01-P",
        audit_sources=(AuditSource.GUARDRAIL_TRACE, AuditSource.BEDROCK_INVOCATION_LOG),
        severity=Severity.HIGH,
    ),
    _negative(
        "INF-05-N",
        Domain.INFERENCE_GATEWAY,
        "Direct Bedrock call bypassing the Gateway is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "bedrock-runtime:Converse called directly from the runtime role",
        "Generated agents must reach models only through LiteLLMModel against "
        "the Gateway; a direct client is a contract violation.",
        ("Runtime role identity policy", "SCP-02", "SCP-04"),
        "INF-01-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
    ),
)


_TOOL_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "TG-01-P",
        Domain.TOOL_GATEWAY,
        "Exposed tool call through the workstream Tool Gateway succeeds",
        "workstream.tool_gateway_invoker",
        "MCP tools/call on a tool exposed to this agent",
        "MCPClient against the workstream Tool Gateway is the golden path.",
        ("Workstream Tool Gateway", "Cedar per-tool entitlement"),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
    ),
    _negative(
        "TG-01-N",
        Domain.TOOL_GATEWAY,
        "Calling a registry tool that is not exposed to this agent is denied",
        Expectation.DENY,
        "workstream.tool_gateway_invoker",
        "MCP tools/call naming a catalogued but unexposed tool",
        "Knowing a tool's name must not be enough to invoke it. This is the "
        "core unexposed-tool attack.",
        ("Cedar per-tool entitlement", "Gateway target scoping"),
        "TG-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.CLOUDWATCH_LOGS),
    ),
    _negative(
        "TG-02-N",
        Domain.TOOL_GATEWAY,
        "Unexposed tools are absent from tools/list",
        Expectation.NOT_EXPOSED,
        "workstream.tool_gateway_invoker",
        "MCP tools/list",
        "Forbidden tools must be filtered from discovery, not merely rejected "
        "on call — an advertised tool invites the attack and leaks topology.",
        ("Cedar per-tool entitlement", "tools/list filtering"),
        "TG-01-P",
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
    ),
    _negative(
        "TG-03-N",
        Domain.TOOL_GATEWAY,
        "Forged tenant context in a tool call is denied",
        Expectation.DENY,
        "workstream.tool_gateway_invoker",
        "MCP tools/call with a tenant id belonging to another tenant",
        "Tool authorization must derive tenant from the verified token, never "
        "from caller-supplied arguments.",
        ("Cedar entitlement", "AgentCore Identity claims"),
        "TG-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
    ),
    _negative(
        "TG-04-N",
        Domain.TOOL_GATEWAY,
        "Direct invocation of a tool's backing target bypassing the Gateway is "
        "denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "direct invoke of the MCP target behind the Gateway",
        "If the backing target is reachable directly, every Gateway-side "
        "control is optional.",
        ("SCP-10 tool invoke allow-list", "Target resource policy"),
        "TG-01-P",
        expected_error_codes=_SCP_DENY,
        expected_http_status=(403,),
    ),
)


_MEMORY_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "MEM-01-P",
        Domain.MEMORY_ISOLATION,
        "Agent reads and writes its own Memory namespace",
        "workstream.memory_client",
        "AgentCore Memory read/write within the agent's own namespace",
        "The sanctioned memory path must work.",
        ("Static namespace template", "runtime actorId binding"),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.AGENTCORE_OBSERVABILITY),
    ),
    _negative(
        "MEM-01-N",
        Domain.MEMORY_ISOLATION,
        "Reading another actor's Memory namespace is denied",
        Expectation.DENY,
        "workstream.memory_client",
        "AgentCore Memory read with a substituted actorId",
        "Memory isolation is per actor; substituting an actorId is the obvious "
        "attack.",
        ("Static namespace template", "Memory resource policy"),
        "MEM-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
    ),
    _negative(
        "MEM-02-N",
        Domain.MEMORY_ISOLATION,
        "Writing outside the static namespace template is denied",
        Expectation.DENY,
        "workstream.memory_client",
        "AgentCore Memory write to a namespace not derivable from the template",
        "Namespaces are static at synth time; a runtime-chosen namespace would "
        "break the isolation model.",
        ("Static namespace template",),
        "MEM-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
    ),
    _negative(
        "MEM-03-N",
        Domain.MEMORY_ISOLATION,
        "Cross-tenant application data read is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "application data read scoped to another tenant id",
        "Tenant data isolation must be enforced by policy conditions, not by "
        "application code.",
        ("Tenant-scoped resource policy", "leading-key condition"),
        "MEM-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
    ),
    _negative(
        "MEM-04-N",
        Domain.MEMORY_ISOLATION,
        "KMS decrypt outside the ViaService condition is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "kms:Decrypt directly against the customer-managed key",
        "A key usable outside its service context is a data-exfiltration path "
        "around every resource policy.",
        ("Customer-managed key policy", "kms:ViaService condition"),
        "MEM-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(400, 403),
        notes=(
            "KMS returns AccessDeniedException with HTTP 400 for some denials; "
            "both statuses are accepted, but the code must still be a denial."
        ),
    ),
)


_REG_DENY = ("AccessDenied", "AccessDeniedException")


_REGISTRY_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "REG-01-P",
        Domain.REGISTRY,
        "RegistryReaderRole reads a registry record it is entitled to",
        "platform.registry_reader",
        "AgentCore Registry GetRecord for an in-tenant record",
        "The sanctioned read surface must function, or every registry denial "
        "twin proves nothing.",
        ("RegistryReaderRole", "Registry as source of truth"),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _allow(
        "REG-02-P",
        Domain.REGISTRY,
        "Cross-account read via RegistryReaderRole with the correct ExternalId "
        "succeeds",
        "workstream.agent_runtime",
        "sts:AssumeRole platform.registry_reader then GetRecord",
        "The sanctioned cross-account registry read hop must work.",
        ("RegistryReaderRole trust policy", "ExternalId condition"),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.AGENTCORE_REGISTRY),
    ),
    _negative(
        "REG-02-N",
        Domain.REGISTRY,
        "Cross-account registry read from a wrong account is denied",
        Expectation.DENY,
        "management-governance.unprivileged_probe",
        "sts:AssumeRole platform.registry_reader from an unauthorized account",
        "The registry is a cross-account trust boundary; only sanctioned "
        "workstream principals may assume the reader role.",
        ("RegistryReaderRole trust policy", "Cross-account boundary"),
        "REG-02-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "REG-03-N",
        Domain.REGISTRY,
        "Cross-account registry read with a wrong ExternalId is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "sts:AssumeRole platform.registry_reader with a wrong ExternalId",
        "The confused-deputy control on the registry boundary depends on the "
        "ExternalId condition being mandatory and exact.",
        ("RegistryReaderRole trust policy", "ExternalId condition"),
        "REG-02-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "REG-04-N",
        Domain.REGISTRY,
        "Cross-tenant registry record read is denied",
        Expectation.DENY,
        "workstream.agent_runtime",
        "AgentCore Registry GetRecord for another tenant's record id",
        "As the source of truth the registry must scope reads by tenant; one "
        "tenant reading another's agent definitions is an isolation breach.",
        ("Registry tenant scoping", "RegistryReaderRole entitlement"),
        "REG-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-05-N",
        Domain.REGISTRY,
        "Cross-tenant registry record discovery is denied or filtered",
        Expectation.DENY,
        "workstream.agent_runtime",
        "AgentCore Registry ListRecords scoped to another tenant",
        "Enumeration must not leak the existence of another tenant's records; "
        "discovery is the first half of a cross-tenant read attack.",
        ("Registry tenant scoping", "List filtering"),
        "REG-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-06-N",
        Domain.REGISTRY,
        "Reader cannot guess-read a record by a fabricated record id",
        Expectation.DENY,
        "platform.registry_reader",
        "AgentCore Registry GetRecord for a guessed/enumerated record id "
        "outside entitlement",
        "Knowing or guessing a record id must not grant access; entitlement is "
        "by policy, not by id secrecy.",
        ("Registry entitlement", "No id-as-secret"),
        "REG-01-P",
        expected_error_codes=("AccessDeniedException",),
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY,),
    ),
    _allow(
        "REG-07-P",
        Domain.REGISTRY,
        "Registry admin creates a record through AgentRegistrationApi",
        "platform.registry_admin",
        "AgentRegistrationApi create of a new registry record",
        "The sanctioned write path must work for the create/update denials to "
        "be meaningful.",
        ("AgentRegistrationApi", "Registry admin authority"),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-07-N",
        Domain.REGISTRY,
        "Unauthorized registry create is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "AgentRegistrationApi create from a non-registrar identity",
        "Only the registrar may create records; a workstream-side create would "
        "let a tenant seed the source of truth.",
        ("AgentRegistrationApi authorization", "Registry admin authority"),
        "REG-07-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-08-N",
        Domain.REGISTRY,
        "Unauthorized registry update is denied",
        Expectation.DENY,
        "workstream.developer_readonly",
        "AgentRegistrationApi update of an existing record",
        "Editing a record out of band would detach the running agent from its "
        "reviewed definition.",
        ("AgentRegistrationApi authorization",),
        "REG-07-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-09-N",
        Domain.REGISTRY,
        "Unauthorized registry approve is denied",
        Expectation.DENY,
        "workstream.workstream_admin",
        "AgentRegistrationApi approve of a pending registration",
        "Approval promotes a record to the source of truth; it must be reserved "
        "to the approver role.",
        ("AgentRegistrationApi authorization", "Registry approver authority"),
        "REG-11-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-10-N",
        Domain.REGISTRY,
        "Unauthorized registry delete is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "AgentRegistrationApi delete of a record",
        "Deleting a record would erase the reviewed definition and its audit "
        "trail.",
        ("AgentRegistrationApi authorization",),
        "REG-07-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _allow(
        "REG-11-P",
        Domain.REGISTRY,
        "A separate approver approves a record the registrar submitted",
        "platform.registry_approver",
        "AgentRegistrationApi approve of a record submitted by registry_admin",
        "Two-person control must permit the legitimate second person, or the "
        "self-approval denial proves nothing.",
        ("Separation of duties", "Registry approver authority"),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-11-N",
        Domain.REGISTRY,
        "Registrar cannot approve its own submitted record (separation of "
        "duties)",
        Expectation.DENY,
        "platform.registry_admin",
        "AgentRegistrationApi approve of a record the same principal submitted",
        "Self-approval collapses two-person control; the submitter approving "
        "its own record is the core SoD attack on the source of truth.",
        ("Separation of duties", "Self-approval prohibition"),
        "REG-11-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "REG-12-N",
        Domain.REGISTRY,
        "Mutation of an approved record's model/target/rate-profile reference "
        "is denied",
        Expectation.DENY,
        "platform.registry_admin",
        "AgentRegistrationApi update of an approved record's inference-target, "
        "model-id or rate-profile reference without re-approval",
        "The approved references bind an agent to sanctioned model, tool target "
        "and rate profile; silently repointing them after approval bypasses the "
        "review that made them safe.",
        (
            "Approved-reference immutability",
            "Re-approval on change",
            "Separation of duties",
        ),
        "REG-07-P",
        expected_error_codes=_REG_DENY,
        expected_http_status=(403,),
        audit_sources=(AuditSource.AGENTCORE_REGISTRY, AuditSource.CLOUDTRAIL),
    ),
)


_PIPELINE_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "PIPE-01-P",
        Domain.PIPELINE_BYPASS,
        "Workload pipeline deploy role can deploy the workstream stack",
        "workstream.cicd_deploy",
        "cloudformation:CreateChangeSet/ExecuteChangeSet from the pipeline",
        "The only sanctioned deployment path must work end to end.",
        ("Workload pipeline", "PR-driven deployment"),
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "PIPE-01-N",
        Domain.PIPELINE_BYPASS,
        "Developer cannot deploy directly to the workstream account",
        Expectation.DENY,
        "workstream.developer_readonly",
        "cloudformation:CreateStack in the workstream account",
        "There is no fast path; a human-initiated deploy must be impossible, "
        "not merely discouraged.",
        ("No-fast-path contract", "Permission set scoping"),
        "PIPE-01-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
    ),
    _negative(
        "PIPE-02-N",
        Domain.PIPELINE_BYPASS,
        "Workstream admin cannot update the AgentCore Runtime out of band",
        Expectation.DENY,
        "workstream.workstream_admin",
        "bedrock-agentcore:UpdateAgentRuntime outside the pipeline",
        "Out-of-band runtime updates would detach the running agent from its "
        "reviewed manifest.",
        ("No-fast-path contract", "Runtime mutation lockdown"),
        "PIPE-01-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
    ),
    _negative(
        "PIPE-03-N",
        Domain.PIPELINE_BYPASS,
        "Pipeline deploy role cannot be assumed from a developer identity",
        Expectation.DENY,
        "workstream.developer_readonly",
        "sts:AssumeRole workstream.cicd_deploy",
        "Borrowing the pipeline's identity is the direct route around the "
        "review gate.",
        ("Pipeline role trust policy",),
        "PIPE-01-P",
        expected_error_codes=("AccessDenied",),
        expected_http_status=(403,),
    ),
    _negative(
        "PIPE-04-N",
        Domain.PIPELINE_BYPASS,
        "Direct push to the protected branch is rejected",
        Expectation.DENY,
        "workstream.developer_readonly",
        "git push to the protected default branch",
        "The PR gate is the first half of the no-fast-path contract; branch "
        "protection is what makes it real.",
        ("Branch protection", "PR-driven deployment"),
        "PIPE-01-P",
        expected_error_codes=("GH006", "ProtectedBranchUpdateFailed"),
        expected_http_status=(403, 422),
        audit_sources=(AuditSource.GITHUB_AUDIT,),
        notes=(
            "Not an AWS call. The probe must capture the git/GitHub API refusal "
            "code; a bare nonzero git exit is not acceptable proof."
        ),
    ),
)


_SUPPLY_CHAIN_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "SUP-01-P",
        Domain.SUPPLY_CHAIN_TAMPER,
        "Pipeline-built image with a matching digest deploys",
        "workstream.cicd_deploy",
        "AgentCore Runtime update with the pipeline-produced image digest",
        "The sanctioned artifact must deploy, or digest pinning cannot be "
        "distinguished from a broken deploy.",
        ("Image digest pinning", "Workload pipeline"),
        audit_sources=(AuditSource.PIPELINE_EXECUTION,),
    ),
    _negative(
        "SUP-01-N",
        Domain.SUPPLY_CHAIN_TAMPER,
        "Overwriting an existing image tag in the shared repository is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "ecr:PutImage re-using an existing tag",
        "Mutable tags let a reviewed digest be swapped after approval.",
        ("Tag immutability", "ECR repository policy"),
        "SUP-01-P",
        expected_error_codes=("ImageTagAlreadyExistsException", "AccessDeniedException"),
        expected_http_status=(400, 403),
        notes=(
            "ImageTagAlreadyExistsException is accepted here because tag "
            "immutability, not IAM, is the control under test; the case must "
            "still fail on a generic validation error."
        ),
    ),
    _negative(
        "SUP-02-N",
        Domain.SUPPLY_CHAIN_TAMPER,
        "Manifest SHA mismatch blocks promotion",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "evaluation gate with a tampered agent manifest",
        "The manifest hash is the link between what was reviewed and what "
        "runs; a mismatch must stop the deployment.",
        ("buildAgentManifest byte-identical hashing", "Evaluation gate"),
        "SUP-01-P",
        expected_error_codes=("ManifestShaMismatch",),
        expected_http_status=(),
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.EVIDENCE_ARCHIVE),
        notes=(
            "Gate is a first-party script: the probe must assert the specific "
            "failure reason, never merely a nonzero exit."
        ),
    ),
    _negative(
        "SUP-03-N",
        Domain.SUPPLY_CHAIN_TAMPER,
        "Tool catalogue drift is detected and blocks release",
        Expectation.DETECTED,
        "platform.platform_admin",
        "catalogue drift detector against a mutated tool catalogue",
        "The registry is the source of truth; silent drift would let an "
        "unreviewed tool reach agents.",
        ("Catalogue drift detector", "Registry as source of truth"),
        "SUP-01-P",
        expected_error_codes=("CatalogueDriftDetected",),
        audit_sources=(AuditSource.SECURITY_HUB, AuditSource.PIPELINE_EXECUTION),
        severity=Severity.HIGH,
    ),
    _negative(
        "SUP-04-N",
        Domain.REGISTRY,
        "Registry entry mutation outside the registration API is denied",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "direct write to the registry store, bypassing AgentRegistrationApi",
        "Only the sanctioned registration surface may write; a direct store "
        "write would bypass validation and audit.",
        ("AgentRegistrationApi", "SCP-11"),
        "SCP-11-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.CLOUDTRAIL, AuditSource.AGENTCORE_REGISTRY),
    ),
)


_RATE_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "RATE-01-P",
        Domain.RATE_LIMITING,
        "Request within the configured rate limits succeeds",
        "workstream.agent_runtime",
        "inference at a rate below the target's RPM limit",
        "Rate limits must not throttle sanctioned traffic, or every throttle "
        "assertion is ambiguous.",
        ("Native Gateway rate limits",),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
        tags=("rpm",),
    ),
    _negative(
        "RATE-01-N",
        Domain.RATE_LIMITING,
        "Exceeding the RPM limit is throttled",
        Expectation.RATE_LIMITED,
        "workstream.agent_runtime",
        "inference burst above the target's requests-per-minute limit",
        "Per-target RPM limits are the primary noisy-neighbour defence.",
        ("Native Gateway rate limits (RPM)",),
        "RATE-01-P",
        expected_error_codes=("ThrottlingException", "TooManyRequestsException"),
        expected_http_status=(429,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
        severity=Severity.HIGH,
        tags=("rpm",),
    ),
    _negative(
        "RATE-02-N",
        Domain.RATE_LIMITING,
        "Exceeding the TPM limit is throttled and reconciles with usage",
        Expectation.RATE_LIMITED,
        "workstream.agent_runtime",
        "inference exceeding the tokens-per-minute limit",
        "TPM accounting must match observed token usage; a limit that never "
        "engages is not a limit.",
        ("Native Gateway rate limits (TPM)",),
        "RATE-01-P",
        expected_error_codes=("ThrottlingException", "TooManyRequestsException"),
        expected_http_status=(429,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.BEDROCK_INVOCATION_LOG),
        severity=Severity.HIGH,
        tags=("tpm",),
        notes="Probe must reconcile throttle onset against recorded token counts.",
    ),
    _negative(
        "RATE-03-N",
        Domain.RATE_LIMITING,
        "Exceeding concurrent-connection (CPS) limits is throttled",
        Expectation.RATE_LIMITED,
        "workstream.agent_runtime",
        "concurrent inference streams above the configured CPS limit",
        "Streaming concurrency is a separate exhaustion vector from request "
        "rate.",
        ("Native Gateway rate limits (CPS)",),
        "RATE-01-P",
        expected_error_codes=("ThrottlingException", "TooManyRequestsException"),
        expected_http_status=(429,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
        severity=Severity.HIGH,
        tags=("cps",),
    ),
    _negative(
        "RATE-04-N",
        Domain.RATE_LIMITING,
        "Unmatched tenant/model combination hits the catch-all limit",
        Expectation.RATE_LIMITED,
        "workstream.agent_runtime",
        "inference on a combination with no explicit limit profile",
        "Without a catch-all, an unlisted combination would be effectively "
        "unlimited.",
        ("Catch-all rate-limit profile",),
        "RATE-01-P",
        expected_error_codes=("ThrottlingException", "TooManyRequestsException"),
        expected_http_status=(429,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
        severity=Severity.HIGH,
        tags=("catch-all",),
    ),
    _negative(
        "RATE-05-N",
        Domain.RATE_LIMITING,
        "Authorization still denies when rate limiting fails open",
        Expectation.DENY,
        "workstream.unprivileged_probe",
        "inference while the rate limiter is unable to evaluate",
        "Native rate limiting evaluates before Policy and fails open. An "
        "unauthorized caller must therefore still be denied by Policy, "
        "authentication and Guardrails alone.",
        (
            "Native rate limiting fails open",
            "AgentCore Policy",
            "Compensating controls",
        ),
        "INF-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.CLOUDTRAIL),
        tags=("fail-open",),
        notes=(
            "This case must never accept a 429 as its result: a throttle would "
            "mean the request was never authorized-checked."
        ),
    ),
    _negative(
        "RATE-06-N",
        Domain.RATE_LIMITING,
        "Zero-rate blocked combination is refused every time",
        Expectation.RATE_LIMITED,
        "workstream.agent_runtime",
        "inference on a combination configured with a zero rate",
        "A zero-rate profile is how a model/tenant pair is blocked without "
        "removing the target; it must hold on the first request.",
        ("Zero-rate blocked combinations", "Catch-all rate-limit profile"),
        "RATE-01-P",
        expected_error_codes=("ThrottlingException", "TooManyRequestsException"),
        expected_http_status=(429,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
        tags=("catch-all", "zero-rate"),
    ),
)


_FAILURE_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "FAIL-01-P",
        Domain.FAILURE_INJECTION,
        "Inference succeeds while the kill switch is disengaged",
        "workstream.agent_runtime",
        "inference with the kill switch off",
        "Baseline for the kill-switch assertion.",
        ("Kill switch",),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG,),
    ),
    _negative(
        "FAIL-01-N",
        Domain.FAILURE_INJECTION,
        "Engaged kill switch denies inference",
        Expectation.DENY,
        "workstream.agent_runtime",
        "inference with the kill switch engaged",
        "The kill switch is the last-resort control; it must deny in the "
        "request path, not merely alarm.",
        ("Kill switch",),
        "FAIL-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GATEWAY_ACCESS_LOG, AuditSource.CLOUDTRAIL),
    ),
    _negative(
        "FAIL-02-N",
        Domain.FAILURE_INJECTION,
        "Open circuit breaker short-circuits calls to a failing dependency",
        Expectation.DETECTED,
        "workstream.agent_runtime",
        "tool calls against an injected failing dependency",
        "A breaker that never opens turns one failing tool into a whole-agent "
        "outage.",
        ("Circuit breaker",),
        "FAIL-01-P",
        expected_error_codes=("CircuitBreakerOpen",),
        audit_sources=(AuditSource.CLOUDWATCH_LOGS, AuditSource.AGENTCORE_OBSERVABILITY),
        severity=Severity.HIGH,
    ),
    _negative(
        "FAIL-03-N",
        Domain.FAILURE_INJECTION,
        "Guardrail unavailability fails closed",
        Expectation.DENY,
        "workstream.agent_runtime",
        "inference while the guardrail cannot be evaluated",
        "If guardrail failure fell through to the model, the control would be "
        "bypassable by inducing a fault.",
        ("Managed Bedrock Guardrail", "fail-closed requirement"),
        "FAIL-01-P",
        expected_error_codes=("AccessDeniedException", "Forbidden"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.GUARDRAIL_TRACE, AuditSource.CLOUDWATCH_LOGS),
        notes=(
            "A 5xx here is NOT acceptable evidence — see the rejection rules. "
            "The probe must observe an explicit denial."
        ),
    ),
    _negative(
        "FAIL-04-N",
        Domain.FAILURE_INJECTION,
        "Injected error rate raises the owning alarm",
        Expectation.DETECTED,
        "management-governance.audit_reader",
        "CloudWatch alarm state after injected errors",
        "An alarm that does not fire is indistinguishable from a healthy "
        "system, which is how missing-alarm false greens happen.",
        ("Agent health alarms", "OAM cross-account observability"),
        "FAIL-01-P",
        audit_sources=(AuditSource.CLOUDWATCH_LOGS, AuditSource.SECURITY_HUB),
        severity=Severity.HIGH,
    ),
)


_ROLLBACK_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "ROLL-01-P",
        Domain.ROLLBACK,
        "Healthy canary promotes to full traffic",
        "workstream.cicd_deploy",
        "canary promotion with healthy metrics",
        "Baseline: promotion works when the agent is healthy.",
        ("Canary deployment", "Evaluation gate"),
        audit_sources=(AuditSource.PIPELINE_EXECUTION,),
    ),
    _negative(
        "ROLL-01-N",
        Domain.ROLLBACK,
        "Failing evaluation gate blocks promotion",
        Expectation.DENY,
        "workstream.cicd_deploy",
        "promotion attempt with a failing evaluation score",
        "The gate is mandatory; a failing score must stop the release rather "
        "than warn.",
        ("Evaluation gate", "Deploy -> gate -> approval -> canary -> prod"),
        "ROLL-01-P",
        expected_error_codes=("EvaluationGateFailed",),
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.EVIDENCE_ARCHIVE),
    ),
    _negative(
        "ROLL-02-N",
        Domain.ROLLBACK,
        "Canary error-rate breach triggers automatic rollback",
        Expectation.ROLLED_BACK,
        "workstream.cicd_deploy",
        "canary with an injected error rate above threshold",
        "Automatic rollback is what makes canarying a control rather than a "
        "delay.",
        ("Canary rollback", "Alarm-driven rollback"),
        "ROLL-01-P",
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.CLOUDWATCH_LOGS),
    ),
    _negative(
        "ROLL-03-N",
        Domain.ROLLBACK,
        "Rollback restores the previous runtime version and manifest SHA",
        Expectation.ROLLED_BACK,
        "workstream.cicd_deploy",
        "post-rollback runtime version and manifest hash",
        "A rollback that leaves the manifest hash unchanged has not actually "
        "reverted what runs.",
        ("Canary rollback", "Manifest hashing"),
        "ROLL-01-P",
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.EVIDENCE_ARCHIVE),
    ),
    _negative(
        "ROLL-04-N",
        Domain.ROLLBACK,
        "Manual approval cannot be bypassed",
        Expectation.DENY,
        "workstream.developer_readonly",
        "pipeline stage transition skipping the approval action",
        "Skipping approval would collapse the mandatory release sequence.",
        ("Manual approval stage", "Pipeline stage ordering"),
        "ROLL-01-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.PIPELINE_EXECUTION, AuditSource.CLOUDTRAIL),
    ),
)


_TEARDOWN_CASES: tuple[AdversarialCase, ...] = (
    _allow(
        "TD-00-P",
        Domain.TEARDOWN,
        "Ephemeral resources are present before teardown",
        "workstream.workstream_admin",
        "describe of the ephemeral test resources",
        "Absence after teardown means nothing unless presence before it was "
        "proven.",
        ("Teardown baseline",),
    ),
    _negative(
        "TD-01-N",
        Domain.TEARDOWN,
        "Ephemeral workstream resources are absent after teardown",
        Expectation.ABSENT,
        "workstream.workstream_admin",
        "describe of ephemeral runtime, gateway and memory resources",
        "Residue costs money and leaves an unowned attack surface.",
        ("Stage-aware teardown",),
        "TD-00-P",
        expected_error_codes=("ResourceNotFoundException", "ValidationError"),
        required_message_substrings=("does not exist",),
        audit_sources=(AuditSource.CLOUDTRAIL,),
        severity=Severity.MEDIUM,
        notes=(
            "ABSENT is the only expectation where NOT_FOUND is acceptable "
            "evidence, and only because non-existence is the assertion. The "
            "message substring keeps a generic CloudFormation ValidationError "
            "from passing."
        ),
    ),
    _negative(
        "TD-02-N",
        Domain.TEARDOWN,
        "No orphaned IAM roles or access keys remain",
        Expectation.ABSENT,
        "workstream.workstream_admin",
        "iam:GetRole for each ephemeral role created by the run",
        "Orphaned roles outlive the deployment and are a standing privilege.",
        ("Stage-aware teardown",),
        "TD-00-P",
        expected_error_codes=("NoSuchEntity", "NoSuchEntityException"),
        audit_sources=(AuditSource.CLOUDTRAIL,),
        severity=Severity.MEDIUM,
    ),
    _negative(
        "TD-03-N",
        Domain.TEARDOWN,
        "Teardown cannot delete retained compliance evidence",
        Expectation.DENY,
        "workstream.workstream_admin",
        "delete against the Management/Governance evidence archive",
        "Evidence immutability must survive the very script that cleans up "
        "everything else.",
        ("Evidence archive retention", "Management/Governance ownership"),
        "IAM-07-P",
        expected_error_codes=("AccessDenied", "AccessDeniedException"),
        expected_http_status=(403,),
        audit_sources=(AuditSource.EVIDENCE_ARCHIVE, AuditSource.CLOUDTRAIL),
    ),
)


CATALOG: tuple[AdversarialCase, ...] = (
    _SCP_CASES
    + _IAM_CASES
    + _INFERENCE_CASES
    + _TOOL_CASES
    + _MEMORY_CASES
    + _REGISTRY_CASES
    + _PIPELINE_CASES
    + _SUPPLY_CHAIN_CASES
    + _RATE_CASES
    + _FAILURE_CASES
    + _ROLLBACK_CASES
    + _TEARDOWN_CASES
)


# ---------------------------------------------------------------------------
# lookup + validation
# ---------------------------------------------------------------------------


def case_index(
    catalog: Iterable[AdversarialCase] = CATALOG,
) -> Mapping[str, AdversarialCase]:
    return {case.case_id: case for case in catalog}


def case_by_id(
    case_id: str, catalog: Iterable[AdversarialCase] = CATALOG
) -> AdversarialCase:
    index = case_index(catalog)
    try:
        return index[case_id]
    except KeyError as exc:
        raise CatalogError([f"unknown case id {case_id!r}"]) from exc


def cases_for_domain(
    domain: Domain, catalog: Iterable[AdversarialCase] = CATALOG
) -> tuple[AdversarialCase, ...]:
    return tuple(case for case in catalog if case.domain is domain)


def execution_order(
    catalog: Iterable[AdversarialCase] = CATALOG,
) -> tuple[AdversarialCase, ...]:
    """Positives first, then negatives, each group ordered by case id.

    The twin ledger depends on this: a negative case may only be treated as
    evidence once its authorized twin has passed in the same run.
    """
    return tuple(
        sorted(
            catalog,
            key=lambda case: (
                0 if case.expectation is Expectation.ALLOW else 1,
                case.case_id,
            ),
        )
    )


def catalog_sha(catalog: Iterable[AdversarialCase] = CATALOG) -> str:
    """Stable hash of the catalog, recorded alongside evidence."""
    payload = json.dumps(
        [case.to_dict() for case in catalog],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_catalog(catalog: Iterable[AdversarialCase] = CATALOG) -> list[str]:
    """Return every consistency problem found in ``catalog``."""
    cases = tuple(catalog)
    problems: list[str] = []
    index: dict[str, AdversarialCase] = {}

    for case in cases:
        if case.case_id in index:
            problems.append(f"duplicate case id {case.case_id!r}")
        index[case.case_id] = case

    for case in cases:
        where = f"case {case.case_id}"

        if not case.rationale.strip():
            problems.append(f"{where}: rationale is required")
        if not case.control_refs:
            problems.append(f"{where}: at least one control ref is required")
        if not case.target.strip():
            problems.append(f"{where}: target is required")

        # principal must exist in the manifest contract
        if "." not in case.principal_ref:
            problems.append(
                f"{where}: principalRef {case.principal_ref!r} must be "
                "'<account>.<role_ref>'"
            )
        else:
            account_key, role_ref = case.principal_ref.split(".", 1)
            if account_key not in REQUIRED_ACCOUNTS:
                problems.append(
                    f"{where}: principalRef names unknown account {account_key!r}"
                )
            elif role_ref not in REQUIRED_ROLES.get(account_key, ()):
                problems.append(
                    f"{where}: principalRef names role {role_ref!r} which is not "
                    f"a required role of account {account_key!r}"
                )

        if case.is_negative:
            if not case.positive_twin:
                problems.append(
                    f"{where}: negative expectation {case.expectation.value} "
                    "requires a positive twin"
                )
            elif case.positive_twin not in index and case.positive_twin not in {
                other.case_id for other in cases
            }:
                problems.append(
                    f"{where}: positive twin {case.positive_twin!r} does not exist"
                )
            else:
                twin = {other.case_id: other for other in cases}[case.positive_twin]
                if twin.expectation is not Expectation.ALLOW:
                    problems.append(
                        f"{where}: positive twin {twin.case_id} must have "
                        f"expectation ALLOW (has {twin.expectation.value})"
                    )
            if not case.audit_sources:
                problems.append(
                    f"{where}: at least one audit source is required for a "
                    "negative case"
                )
        else:
            if case.positive_twin:
                problems.append(f"{where}: ALLOW cases must not declare a twin")
            if case.expected_error_codes:
                problems.append(
                    f"{where}: ALLOW cases must not declare expected error codes"
                )

        if case.expectation in (
            Expectation.DENY,
            Expectation.AUTHENTICATION_DENIED,
        ):
            if not case.expected_error_codes:
                problems.append(
                    f"{where}: expected error codes are required — a denial "
                    "assertion must name the exact code it accepts"
                )
            if not case.expected_http_status and case.expectation is Expectation.DENY:
                if case.audit_sources != (AuditSource.PIPELINE_EXECUTION,):
                    # first-party gates have no HTTP status; everything that
                    # crosses an AWS/HTTP boundary must name one.
                    if AuditSource.PIPELINE_EXECUTION not in case.audit_sources:
                        problems.append(
                            f"{where}: expected HTTP status is required for a "
                            "service-boundary denial"
                        )

        if case.expectation is Expectation.DENY:
            forbidden = [
                code
                for code in case.expected_error_codes
                if code in FORBIDDEN_PROOF_CODES
            ]
            if forbidden:
                problems.append(
                    f"{where}: {', '.join(sorted(forbidden))} cannot be accepted "
                    "as authorization evidence (missing resource / validation / "
                    "5xx / timeout / credential / conflict class)"
                )
            if 429 in case.expected_http_status:
                problems.append(
                    f"{where}: HTTP 429 is a throttle, not an authorization "
                    "denial"
                )

        if case.expectation is Expectation.RATE_LIMITED:
            non_rate = [
                code
                for code in case.expected_error_codes
                if code not in RATE_LIMIT_PROOF_CODES
            ]
            if non_rate:
                problems.append(
                    f"{where}: {', '.join(sorted(non_rate))} is not throttle "
                    "evidence"
                )
            if not case.expected_error_codes:
                problems.append(f"{where}: rate-limit cases must name a throttle code")

        if case.expectation is Expectation.ABSENT and not case.expected_error_codes:
            problems.append(
                f"{where}: absence cases must name the not-found code they accept"
            )

    covered = {case.domain for case in cases if case.is_negative}
    for domain in REQUIRED_DOMAINS:
        if domain not in covered:
            problems.append(
                f"domain {domain.value!r} has no negative case; every trust "
                "boundary must be attacked"
            )

    rate_tags = {
        tag
        for case in cases
        if case.domain is Domain.RATE_LIMITING and case.is_negative
        for tag in case.tags
    }
    for tag in sorted(REQUIRED_RATE_LIMIT_TAGS - rate_tags):
        problems.append(f"rate-limiting domain is missing coverage for {tag!r}")

    return problems


def assert_catalog_valid(catalog: Iterable[AdversarialCase] = CATALOG) -> None:
    problems = validate_catalog(catalog)
    if problems:
        raise CatalogError(problems)


__all__ = [
    "AdversarialCase",
    "AuditSource",
    "CATALOG",
    "Domain",
    "Expectation",
    "NEGATIVE_EXPECTATIONS",
    "RATE_LIMIT_PROOF_CODES",
    "REQUIRED_DOMAINS",
    "REQUIRED_RATE_LIMIT_TAGS",
    "Severity",
    "assert_catalog_valid",
    "case_by_id",
    "case_index",
    "cases_for_domain",
    "catalog_sha",
    "execution_order",
    "validate_catalog",
]
