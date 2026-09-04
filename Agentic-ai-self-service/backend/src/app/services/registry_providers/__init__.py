"""Registry backend selection — ``dynamodb`` (default) or ``litellm``.

Selection lives in a Settings row on the TagPolicy table, the same shape
``deploy_target.py`` and ``aws_agent_registry.py`` already use, so this needs no
new table and no new IAM. Anything uncertain — a missing row, an unreadable
table, an unrecognized value — resolves to ``dynamodb``, i.e. the behavior that
existed before this package.

Note the pre-existing keying inconsistency this deliberately preserves rather
than fixes: settings rows key on ``org_id="default"`` while registry entries use
``DEFAULT_ORG_ID == "default-org"``.

Naming: ``aws_agent_registry.get_registry()`` and
``registry_store.get_registry_store()`` already collide confusingly. This selector
is ``get_registry_provider()`` and nothing closer.

The provider object is NOT cached. It must be re-resolved per call so that a
setting change takes effect without a Lambda recycle, and so tests that flip the
setting (or swap ``registry_store._registry_store``) are actually honoured.
Constructing one is a no-op — the providers hold no state and resolve their store
per method.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_PROVIDER_SK = "SETTING#registry_provider"

VALID_PROVIDERS = ("dynamodb", "litellm")
DEFAULT_PROVIDER = "dynamodb"


def _settings_table():
    import boto3

    name = os.environ.get("TAG_POLICY_TABLE_NAME", "TagPolicy")
    region = os.environ.get("APP_AWS_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    return boto3.resource("dynamodb", region_name=region).Table(name)


def default_registry_provider() -> str:
    """The configured backend name. Fail-safe: always ``dynamodb`` when unsure."""
    env = os.environ.get("REGISTRY_PROVIDER", "").strip().lower()
    if env in VALID_PROVIDERS:
        return env
    try:
        item = _settings_table().get_item(Key={"org_id": "default", "sk": _PROVIDER_SK}).get("Item")
        value = str((item or {}).get("value", "")).strip().lower()
        return value if value in VALID_PROVIDERS else DEFAULT_PROVIDER
    except Exception as e:  # noqa: BLE001
        logger.info("registry_provider setting lookup failed (default %s): %s", DEFAULT_PROVIDER, e)
        return DEFAULT_PROVIDER


def set_default_registry_provider(provider: str) -> None:
    """Admin setter for the platform-wide registry backend."""
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"registry provider must be one of {list(VALID_PROVIDERS)}")
    _settings_table().put_item(Item={"org_id": "default", "sk": _PROVIDER_SK, "value": provider})


def get_registry_provider():
    """The active ``RegistryProvider``. Imports are lazy and inside the function.

    Lazy for two reasons: the LiteLLM provider pulls in ``aws_agent_registry`` and
    ``litellm_gateway_deployer``, which the default path has no reason to import;
    and the ``/litellm-*`` router handlers monkeypatch module-level functions in
    ``registry_providers.litellm``, which only works if the module is resolved at
    call time (the same convention the ``/aws-*`` handlers already follow).
    """
    provider = default_registry_provider()
    if provider == "litellm":
        from app.services.registry_providers.litellm import LiteLLMRegistryProvider

        return LiteLLMRegistryProvider()
    from app.services.registry_providers.dynamo import DynamoRegistryProvider

    return DynamoRegistryProvider()


def unapproved_integrations_for_provider(identifiers: list[str]) -> list[str] | None:
    """The LiteLLM deploy gate when that backend is active, else None.

    Returning None (rather than []) means "this backend does not govern the gate,
    fall through to the AWS Agent Registry path". [] would read as "everything is
    approved" and open the gate.
    """
    if default_registry_provider() != "litellm":
        return None
    from app.services.registry_providers.litellm import litellm_unapproved_integrations

    return litellm_unapproved_integrations(identifiers)
