"""One way to call UpdateGateway without clearing the gateway (F-62).

``UpdateGateway`` is a full replace: every optional field it is not sent is reset,
not left alone. Measured live for ``protocolConfiguration`` (an update that omitted it
dropped a pinned gateway back to ``None``), and the service model gives no field a
merge semantic. Every site used to rebuild the request by hand from ``GetGateway``,
each copying a different subset, so the policy attach, the promoter flip, the teardown
detach and the redeploy adoption each silently cleared something else: interceptors,
the WAF association, the custom transform, ``exceptionLevel``, and on some paths the
customer-managed KMS key, the description and the attached policy engine.

``PRESERVED_FIELDS`` is explicit so that a field the service adds is a deliberate
decision, not an automatic forward. Two things keep the list honest: a test pins it to
exactly the fields GetGateway and UpdateGateway share in the installed botocore model,
and at runtime a gateway holding a value in a field the running model can update but
this list does not name is refused, because sending the update would clear it.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import botocore.session

from app.services.mcp_gateway_protocol import pinned_protocol_configuration

logger = logging.getLogger(__name__)

PRESERVED_FIELDS = frozenset(
    {
        "authorizerConfiguration",
        "authorizerType",
        "customTransformConfiguration",
        "description",
        "exceptionLevel",
        "interceptorConfigurations",
        "kmsKeyArn",
        "name",
        "policyEngineConfiguration",
        "protocolConfiguration",
        "protocolType",
        "roleArn",
        "wafConfiguration",
    }
)

#: The one field a caller may drop: detaching a policy engine IS sending the update
#: without it. Anything else has to be re-sent or replaced by an override.
DETACHABLE_FIELDS = frozenset({"policyEngineConfiguration"})


#: The ``allowedClients`` entry that admits no client (F-66d). An empty list is not
#: "nobody": it lifts the client restriction, widening the gateway to every client in
#: the pool. Cognito client ids match ``[\w+]+``, so an id with a ``:`` and ``-`` can
#: never be issued, and a list holding only it admits nothing.
NO_CLIENT_ALLOWED = "revoked:no-client-is-allowed"


class GatewayUpdateRefused(RuntimeError):
    """The update would clear or malform the gateway, so it was not sent."""


@lru_cache(maxsize=1)
def _update_shape():
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    return model.operation_model("UpdateGateway").input_shape


def _sendable(field: str, value) -> bool:
    """Whether the model's own validation accepts ``value`` for ``field``.

    Falsey values are kept wherever the model accepts them. An empty string, list or
    map is dropped only when the member declares a ``min`` it violates
    (``description`` and ``interceptorConfigurations`` declare ``min: 1``): sending it
    fails param validation, and omitting it means the same thing to the service.
    """
    if value is None:
        return False
    if isinstance(value, (str, list, dict)):
        return len(value) >= (_update_shape().members[field].metadata.get("min") or 0)
    return True


def preserving_gateway_update(
    detail: dict,
    gateway_id: str,
    *,
    overrides: dict | None = None,
    detach: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """Build UpdateGateway kwargs that re-send everything ``detail`` holds.

    ``detail`` is a GetGateway response for ``gateway_id``. ``overrides`` replaces
    whole fields and must carry sendable values: an override of ``None`` would be a
    detach by another name. ``detach`` removes fields and may only name
    ``DETACHABLE_FIELDS``. The MCP protocol pin is applied last, so it survives every
    override. Raises ``GatewayUpdateRefused`` rather than send a request that would
    clear a field or omit a required one.
    """
    shape = _update_shape()
    overrides = dict(overrides or {})
    detach = frozenset(detach)
    if set(overrides) - PRESERVED_FIELDS:
        raise GatewayUpdateRefused(f"not an updatable gateway field: {sorted(set(overrides) - PRESERVED_FIELDS)}")
    if detach - DETACHABLE_FIELDS:
        raise GatewayUpdateRefused(f"only {sorted(DETACHABLE_FIELDS)} may be detached, not {sorted(detach)}")
    if detach & set(overrides):
        raise GatewayUpdateRefused(f"{sorted(detach & set(overrides))} is both overridden and detached")
    unsendable = sorted(f for f, v in overrides.items() if not _sendable(f, v))
    if unsendable:
        raise GatewayUpdateRefused(
            f"override would clear {unsendable}; only {sorted(DETACHABLE_FIELDS)} may be detached"
        )

    unknown_held = sorted(
        f for f in set(shape.members) - PRESERVED_FIELDS - {"gatewayIdentifier"} if detail.get(f) is not None
    )
    if unknown_held:
        raise GatewayUpdateRefused(
            f"gateway {gateway_id} holds {unknown_held}, which this SDK can update but the "
            f"platform does not preserve yet; the update would clear it"
        )

    params = {f: detail[f] for f in PRESERVED_FIELDS if f in detail}
    params.update(overrides)
    for f in detach:
        params.pop(f, None)
    params["protocolConfiguration"] = pinned_protocol_configuration(params.get("protocolConfiguration"))

    request = {f: v for f, v in params.items() if _sendable(f, v)}
    missing = sorted(set(shape.required_members) - {"gatewayIdentifier"} - set(request))
    if missing:
        raise GatewayUpdateRefused(f"gateway {gateway_id} detail lacks required {missing}")
    dropped = sorted(f for f, v in params.items() if v is not None and f not in request)
    if dropped:
        # Names only, never values.
        logger.warning("UpdateGateway %s: omitting empty field(s) the API rejects: %s", gateway_id, dropped)
    return {"gatewayIdentifier": gateway_id, **request}
