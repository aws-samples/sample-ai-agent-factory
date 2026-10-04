"""Raw write-only credentials never persist -- not through the flow API, not in either storage, not
in a registry snapshot, not in the typed workflow record -- while their references survive.

Reproduces the P0 (a LiteLLM virtual key typed into the gateway modal was autosaved and stored in
DynamoDB in plaintext) and pins the fix at every backend boundary, independent of what the frontend
sends. Matching is by canonical key, so casing and separators cannot smuggle a value past it.
"""

from __future__ import annotations

import json
import logging

import pytest
from app.models.components import APIKeyCredentials
from app.models.flow import Flow, FlowUpdateRequest
from app.routers import flows as flows_router
from app.routers.registry import PublishRequest, RegistryCanvasSnapshotV2
from app.services.auth import get_caller_sub
from app.services.credential_scrub import (
    WRITE_ONLY_CREDENTIAL_TOKENS,
    canonical_key,
    contains_write_only_credential,
    is_write_only_credential_key,
    scrub_write_only_credentials,
)
from app.services.flow_storage import FlowStorage, _serialize_flow
from fastapi import FastAPI
from fastapi.testclient import TestClient

SENTINEL = "sk-matrix-sentinel-NEVER-PERSIST-0123456789"
ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:llm-AbCdEf"

# Every spelling a client has used or could use for a credential VALUE.
ALIASES = [
    "litellmApiKey",
    "litellm_api_key",
    "LITELLM_API_KEY",
    "litellm-api-key",
    "apiKey",
    "api_key",
    "api-key",
    "API.KEY",
    "ApiKey",
    "clientSecret",
    "client_secret",
    "secretValue",
    "secret_value",
    "secret",
    "password",
    "Authorization",
    "authorization",
    "accessToken",
    "refresh-token",
    "sessionToken",
    "idToken",
    "id_token",
    "aws_secret_access_key",
    "token",
    "privateKey",
    "private_key",
    "bearerToken",
    "webhookSecret",
    "signing_secret",
]

# Keys that name a REFERENCE or a locator, which must survive untouched.
REFERENCES = {
    "litellmApiKeyRef": ARN,
    "apiKeyRef": ARN,
    "api_key_ref": ARN,
    "secretRef": ARN,
    "secretArn": ARN,
    "clientSecretArn": ARN,
    "apiKeySecretArn": ARN,
    "secretName": "llm-key",
    "secretId": "llm-key",
    "tokenUrl": "https://idp.example/token",
    "token_url": "https://idp.example/token",
    "credentialParameterName": "X-API-Key",
    "credentialLocation": "header",
    "maxTokens": 4096,
    "clientId": "abc",
    "serverId": "github",
}


def _workflow(config: dict, *, edge_data: dict | None = None) -> dict:
    return {
        "nodes": [{"id": "gw-1", "type": "gateway", "data": {"componentType": "gateway", "configuration": config}}],
        "edges": [{"id": "e-1", "source": "gw-1", "target": "rt-1", "data": edge_data or {}}],
    }


CASES = {
    "litellm key": {"litellmApiKey": SENTINEL, "litellmApiKeyRef": ARN},
    "mcp target apiKey": {
        "targets": [{"targetType": "mcp", "targetConfig": {"apiKey": SENTINEL, "serverId": "github"}}]
    },
    "oauth clientSecret": {
        "oauth2Config": {"clientId": "abc", "clientSecret": SENTINEL, "tokenUrl": "https://idp.example/token"}
    },
    "connector secretValue": {"connectors": [{"name": "jira", "secretValue": SENTINEL, "secretRef": ARN}]},
    "snake_case spelling": {"litellm_api_key": SENTINEL, "client_secret": SENTINEL, "api_key": SENTINEL},
    "separator and case aliases": {
        "api-key": SENTINEL,
        "API.KEY": SENTINEL,
        "Authorization": SENTINEL,
        "sessionToken": SENTINEL,
    },
    "header object": {"headers": {"Authorization": f"Bearer {SENTINEL}", "Accept": "application/json"}},
    "credential-keyed object": {"apiKey": {"value": SENTINEL, "secretArn": ARN}},
    "list of raw values": {"tokens": ["a"], "token": [SENTINEL, {"arn": ARN}]},
    "credential scalar": {"credential": SENTINEL, "credentials": SENTINEL, "creds": SENTINEL},
    "credentials object keeps its non-secret shape": {
        "credentials": {"clientId": "abc", "clientSecret": SENTINEL, "clientSecretArn": ARN, "scopes": ["read"]}
    },
    "credentials list of raw values": {"credentials": [SENTINEL, SENTINEL]},
    "credentials list of objects": {"creds": [{"clientId": "abc", "password": SENTINEL, "secretArn": ARN}, SENTINEL]},
    "credentials object with value": {"credentials": {"value": SENTINEL, "clientId": "abc"}},
    "credential object with key": {"credential": {"key": SENTINEL, "label": "prod"}},
    "creds list with object value": {"creds": [{"value": SENTINEL, "label": "prod"}]},
    "credentials object with values list": {"credentials": {"values": [SENTINEL], "scopes": ["read"]}},
    "payload leaves outside a container are data": {"value": "ok", "key": "k1", "data": {"raw": "fine"}},
    "apiKey object with authorization header": {
        "apiKey": {"authorizationHeader": f"Bearer {SENTINEL}", "secretArn": ARN}
    },
    "apiKey object with token url query": {"apiKey": {"tokenUrl": f"https://idp.example/token?key={SENTINEL}"}},
    "apiKey object with secret source": {"apiKey": {"secretSource": SENTINEL, "secretRef": ARN}},
    "credentials with authorization header": {
        "credentials": {"authorizationHeader": f"Bearer {SENTINEL}", "clientId": "abc"}
    },
    "credentials with url query": {
        "credentials": {"url": f"https://api.example/v1?api_key={SENTINEL}", "clientId": "abc"}
    },
    "credentials label carrying a bearer string": {"credentials": {"label": f"Bearer {SENTINEL}", "clientId": "abc"}},
    "credentials secretSource scalar": {"credentials": {"secretSource": SENTINEL, "clientId": "abc"}},
    "credentials opaque scalar": {"credentials": {"opaque": SENTINEL, "clientId": "abc"}},
    "credentials apiKeyHeader scalar": {"credentials": {"apiKeyHeader": SENTINEL, "clientId": "abc"}},
    "credentials label BearerRAW no space": {"credentials": {"label": f"Bearer{SENTINEL}", "clientId": "abc"}},
    "credentials url with secret in path": {
        "credentials": {"url": f"https://api.example/{SENTINEL}/v1", "clientId": "abc"}
    },
    "credentials url with secret in userinfo": {
        "credentials": {"url": f"https://{SENTINEL}@api.example/v1", "clientId": "abc"}
    },
    "apiKey secretId raw": {"apiKey": {"secretId": SENTINEL}},
    "apiKey clientId raw": {"apiKey": {"clientId": SENTINEL}},
    "apiKey secretRef object with secretName raw": {"apiKey": {"secretRef": {"secretName": SENTINEL}}},
}


# --------------------------------------------------------------------------- the matcher


@pytest.mark.parametrize("alias", ALIASES)
def test_every_alias_is_a_credential_key(alias):
    assert is_write_only_credential_key(alias), alias
    assert canonical_key(alias) in WRITE_ONLY_CREDENTIAL_TOKENS


@pytest.mark.parametrize("key", sorted(REFERENCES))
def test_reference_and_locator_keys_are_never_credential_keys(key):
    assert not is_write_only_credential_key(key), key


def test_scrub_removes_every_alias_and_keeps_every_reference():
    config = {**{alias: SENTINEL for alias in ALIASES}, **REFERENCES}
    out = scrub_write_only_credentials(config)
    assert SENTINEL not in json.dumps(out)
    assert out == REFERENCES


def test_scrub_reaches_arrays_nested_objects_and_edge_data():
    wf = _workflow(
        {"targets": [{"a": [{"b": {"clientSecret": SENTINEL, "clientSecretArn": ARN}}]}]},
        edge_data={"auth": {"api-key": SENTINEL}, "label": "x"},
    )
    out = scrub_write_only_credentials(wf)
    assert SENTINEL not in json.dumps(out)
    assert out["nodes"][0]["data"]["configuration"] == {"targets": [{"a": [{"b": {"clientSecretArn": ARN}}]}]}
    assert out["edges"][0]["data"] == {"auth": {}, "label": "x"}


def test_a_credential_key_holding_an_object_keeps_only_references():
    src = {"apiKey": {"value": SENTINEL, "secretArn": ARN, "key": SENTINEL}, "secret": [SENTINEL, {"arn": ARN}]}
    assert scrub_write_only_credentials(src) == {"apiKey": {"secretArn": ARN}, "secret": [{"arn": ARN}]}


def test_the_scrub_is_a_deep_copy_and_never_mutates_the_input():
    src = {"a": {"b": [{"apiKey": "x", "keep": 1, "apiKeyRef": "r"}]}, "password": "p", "name": "n"}
    snapshot = json.dumps(src, sort_keys=True)
    out = scrub_write_only_credentials(src)
    assert out == {"a": {"b": [{"keep": 1, "apiKeyRef": "r"}]}, "name": "n"}
    assert json.dumps(src, sort_keys=True) == snapshot
    assert not contains_write_only_credential(out)
    assert contains_write_only_credential(src)
    assert not contains_write_only_credential({"apiKeyRef": ARN, "n": [1, {"tokenUrl": "u"}]})


def test_non_dict_values_pass_through_unchanged():
    for value in ("s", 1, None, True, [1, "a"], [{"x": [None]}]):
        assert scrub_write_only_credentials(value) == value


def test_the_warning_names_only_fixed_field_names_and_a_count(caplog):
    # An ancestor key is caller-controlled text and could itself be the secret, so it never reaches
    # the log either: only canonical names from the module's closed vocabulary, plus a count.
    with caplog.at_level(logging.WARNING, logger="app.services.credential_scrub"):
        scrub_write_only_credentials(_workflow({"litellmApiKey": SENTINEL, SENTINEL: {"api-key": SENTINEL}}))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Dropped 2 write-only credential field(s)" in text
    assert "litellmapikey" in text and "apikey" in text
    assert SENTINEL not in text
    assert "configuration" not in text and "nodes" not in text


def test_a_scalar_credential_container_is_dropped():
    assert scrub_write_only_credentials(
        {"credential": SENTINEL, "credentials": SENTINEL, "creds": SENTINEL, "n": 1}
    ) == {"n": 1}


def test_a_credentials_object_is_walked_not_dropped():
    src = {"credentials": {"clientId": "abc", "clientSecret": SENTINEL, "clientSecretArn": ARN, "scopes": ["read"]}}
    assert scrub_write_only_credentials(src) == {
        "credentials": {"clientId": "abc", "clientSecretArn": ARN, "scopes": ["read"]}
    }


def test_a_credentials_list_drops_scalar_members():
    assert scrub_write_only_credentials({"credentials": [SENTINEL, 1, None], "n": 1}) == {"credentials": [], "n": 1}
    # An unrecognised leaf ("user") inside a container is dropped too: the allowlist is closed.
    assert scrub_write_only_credentials(
        {"creds": [[SENTINEL], [{"password": SENTINEL, "user": "u", "clientId": "c"}]]}
    ) == {"creds": [[], [{"clientId": "c"}]]}


def test_payload_leaves_inside_a_container_are_dropped_and_outside_are_kept():
    # Codex reproduced these four shapes surviving the first cut; each is exact, no escape clause.
    assert scrub_write_only_credentials({"credentials": {"value": SENTINEL, "clientId": "abc"}}) == {
        "credentials": {"clientId": "abc"}
    }
    assert scrub_write_only_credentials({"credential": {"key": SENTINEL, "label": "prod", "type": "apiKey"}}) == {
        "credential": {"type": "apiKey"}
    }
    assert scrub_write_only_credentials({"creds": [{"value": SENTINEL, "label": "prod", "clientId": "c"}]}) == {
        "creds": [{"clientId": "c"}]
    }
    # A list of raw values under a payload key keeps its (now empty) shape, like ``credentials: [RAW]``.
    assert scrub_write_only_credentials({"credentials": {"values": [SENTINEL], "scopes": ["read"]}}) == {
        "credentials": {"scopes": ["read"]}
    }
    assert scrub_write_only_credentials(
        {"credentials": {"nested": {"plaintext": SENTINEL, "clientSecretArn": ARN}}}
    ) == {"credentials": {"nested": {"clientSecretArn": ARN}}}
    # The same leaf names OUTSIDE a container are ordinary data and must survive.
    plain = {"value": "ok", "key": "k1", "data": {"raw": "fine"}, "values": [1, 2]}
    assert scrub_write_only_credentials(plain) == plain


def test_under_a_value_key_only_validated_references_survive():
    # Adversarial audit shapes: headers, sources, URLs, ids and names are not references.
    assert scrub_write_only_credentials(
        {"apiKey": {"authorizationHeader": f"Bearer {SENTINEL}", "secretArn": ARN}}
    ) == {"apiKey": {"secretArn": ARN}}
    assert scrub_write_only_credentials({"apiKey": {"tokenUrl": f"https://idp.example/token?key={SENTINEL}"}}) == {
        "apiKey": {}
    }
    assert scrub_write_only_credentials({"apiKey": {"secretSource": SENTINEL, "secretRef": ARN}}) == {
        "apiKey": {"secretRef": ARN}
    }
    assert scrub_write_only_credentials(
        {"apiKey": {"secretId": SENTINEL, "clientId": SENTINEL, "secretName": SENTINEL}}
    ) == {"apiKey": {}}
    assert scrub_write_only_credentials({"apiKey": {"secretRef": {"secretName": SENTINEL, "secretArn": ARN}}}) == {
        "apiKey": {"secretRef": {"secretArn": ARN}}
    }
    # A reference-named leaf must hold an ARN; anything else under a value key is the value.
    assert scrub_write_only_credentials({"apiKey": {"secretRef": SENTINEL, "keyArn": f"Bearer {SENTINEL}"}}) == {
        "apiKey": {}
    }


def test_inside_a_container_only_allowlisted_metadata_and_validated_references_survive():
    out = scrub_write_only_credentials(
        {
            "credentials": {
                "authorizationHeader": f"Bearer {SENTINEL}",
                "url": f"https://api.example/v1?api_key={SENTINEL}#frag",
                "tokenUrl": "https://idp.example/token",
                "baseUrl": "https://idp.example",
                "label": f"Bearer{SENTINEL}",
                "opaque": SENTINEL,
                "secretSource": SENTINEL,
                "clientId": "abc",
                "clientSecretArn": ARN,
                "scopes": ["read", f"Bearer {SENTINEL}", "has space"],
                "credentialLocation": "header",
                "credentialParameterName": "X-API-Key",
                "nested": {"plaintext": SENTINEL, "region": "us-east-1"},
            }
        }
    )
    assert out == {
        "credentials": {
            "baseUrl": "https://idp.example",
            "clientId": "abc",
            "clientSecretArn": ARN,
            "scopes": ["read"],
            "credentialLocation": "header",
            "credentialParameterName": "X-API-Key",
            "nested": {"region": "us-east-1"},
        }
    }
    # A URL is dropped, never truncated: userinfo, query, fragment, http, or ANY path (a path
    # segment is indistinguishable from a token by shape). Origin-only https survives.
    assert scrub_write_only_credentials({"credentials": {"url": f"https://{SENTINEL}@api.example/v1"}}) == {
        "credentials": {}
    }
    assert scrub_write_only_credentials({"credentials": {"url": "http://api.example/v1"}}) == {"credentials": {}}
    assert scrub_write_only_credentials({"credentials": {"url": f"https://api.example/{SENTINEL}"}}) == {
        "credentials": {}
    }
    assert scrub_write_only_credentials({"credentials": {"url": "https://api.example/"}}) == {
        "credentials": {"url": "https://api.example/"}
    }
    # Outside a container the same shapes are ordinary data.
    plain = {"url": "https://api.example/v1?page=2", "label": "Bearer of bad news", "opaque": "x"}
    assert scrub_write_only_credentials(plain) == plain


def test_a_credentials_list_of_objects_is_walked_and_scalar_siblings_dropped():
    src = {"creds": [{"clientId": "abc", "password": SENTINEL, "secretArn": ARN, "note": "kept?"}, SENTINEL]}
    assert scrub_write_only_credentials(src) == {"creds": [{"clientId": "abc", "secretArn": ARN}]}


# --------------------------------------------------------------------------- flow model boundaries


@pytest.mark.parametrize("label,config", CASES.items(), ids=list(CASES))
def test_the_flow_update_request_drops_the_value_and_keeps_the_reference(label, config):
    req = FlowUpdateRequest.model_validate({"workflow": _workflow(config)})
    dumped = json.dumps(req.model_dump(mode="json"))
    assert SENTINEL not in dumped, label
    assert not contains_write_only_credential(req.workflow), label
    if ARN in json.dumps(config):
        assert ARN in dumped, "references must survive"


@pytest.mark.parametrize("label,config", CASES.items(), ids=list(CASES))
def test_the_stored_flow_item_never_contains_the_value(label, config):
    flow = Flow.model_validate(
        {
            "id": "f1",
            "name": "n",
            "workflow": _workflow(config),
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
        }
    )
    item = _serialize_flow(flow)
    assert SENTINEL not in json.dumps(item), label
    assert not contains_write_only_credential(item["workflow"]), label


def test_the_in_memory_store_scrubs_a_value_handed_to_it_directly():
    store = FlowStorage()
    flow = store.create("n", owner_sub="u1")
    # No request model in the way: this is the path a future internal caller would take.
    updated = store.update(flow.id, workflow=_workflow({"litellmApiKey": SENTINEL, "litellmApiKeyRef": ARN}))
    assert updated is not None
    assert SENTINEL not in json.dumps(updated.model_dump(mode="json"))
    assert SENTINEL not in json.dumps(store.get(flow.id).model_dump(mode="json"))
    assert ARN in json.dumps(store.get(flow.id).model_dump(mode="json"))


def test_the_dynamodb_store_returns_the_scrubbed_record_not_the_input(monkeypatch):
    import importlib

    # ``app.services.flow_storage`` the ATTRIBUTE is the package singleton; we need the module.
    fs = importlib.import_module("app.services.flow_storage")

    table: dict[str, dict] = {}
    seed = Flow.model_validate(
        {
            "id": "f1",
            "name": "n",
            "workflow": {},
            "owner_sub": "u1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
        }
    )
    table["f1"] = fs._serialize_flow(seed)
    monkeypatch.setattr(fs, "_get_item", lambda _t, key: table.get(key["flow_id"]))
    monkeypatch.setattr(fs, "_conditional_put_item", lambda _t, item, **_kw: table.__setitem__(item["flow_id"], item))
    store = object.__new__(fs.DynamoDBFlowStorage)
    store._table = None

    returned = store.update("f1", workflow=_workflow({"litellmApiKey": SENTINEL, "litellmApiKeyRef": ARN}))
    assert returned is not None
    assert SENTINEL not in json.dumps(returned.model_dump(mode="json")), "the RETURNED model carried the raw value"
    assert SENTINEL not in json.dumps(table["f1"]), "the stored item carried the raw value"
    assert ARN in json.dumps(table["f1"])


def _client(store: FlowStorage, monkeypatch, caller_sub: str = "user-1") -> TestClient:
    monkeypatch.setattr(flows_router, "_get_flow_storage", lambda: store)
    app = FastAPI()
    app.include_router(flows_router.router)
    app.dependency_overrides[get_caller_sub] = lambda: caller_sub
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("label,config", CASES.items(), ids=list(CASES))
def test_the_flow_api_save_then_get_never_echoes_or_stores_the_value(label, config, monkeypatch):
    store = FlowStorage()
    flow = store.create("n", owner_sub="user-1")
    client = _client(store, monkeypatch)

    saved = client.put(f"/flows/{flow.id}", json={"workflow": _workflow(config)})
    assert saved.status_code == 200, saved.text
    assert SENTINEL not in saved.text, f"{label}: the save response echoed the value"

    fetched = client.get(f"/flows/{flow.id}")
    assert fetched.status_code == 200, fetched.text
    assert SENTINEL not in fetched.text, f"{label}: the read echoed the value"
    if ARN in json.dumps(config):
        assert ARN in fetched.text, "references must survive the round trip"

    stored = store.get(flow.id)
    assert stored is not None
    assert SENTINEL not in json.dumps(stored.model_dump(mode="json")), f"{label}: stored in memory"
    assert SENTINEL not in json.dumps(_serialize_flow(stored)), f"{label}: would be stored in DynamoDB"


# --------------------------------------------------------------------------- registry snapshot


def _snapshot(config: dict, *, edge_data: dict | None = None) -> dict:
    wf = _workflow(config, edge_data=edge_data)
    return {
        "schemaVersion": 2,
        "name": "n",
        "nodes": wf["nodes"],
        "edges": wf["edges"],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "governance": {"version": 1},
    }


@pytest.mark.parametrize("label,config", CASES.items(), ids=list(CASES))
def test_a_registry_snapshot_never_publishes_the_value(label, config):
    snap = RegistryCanvasSnapshotV2.model_validate(_snapshot(config, edge_data={"apiKey": SENTINEL}))
    dumped = json.dumps(snap.model_dump(mode="json", by_alias=True))
    assert SENTINEL not in dumped, label
    if ARN in json.dumps(config):
        assert ARN in dumped


def test_a_publish_request_never_carries_the_value_in_nodes_or_edges():
    req = PublishRequest.model_validate(
        {
            "display_name": "d",
            "canvas_snapshot": _snapshot({"litellmApiKey": SENTINEL}, edge_data={"auth": {"token": SENTINEL}}),
        }
    )
    dumped = json.dumps(req.model_dump(mode="json", by_alias=True))
    assert SENTINEL not in dumped
    assert req.canvas_snapshot.edges[0]["data"] == {"auth": {}}


# --------------------------------------------------------------------------- typed workflow record


def test_the_typed_api_key_credential_is_write_only():
    creds = APIKeyCredentials(api_key=SENTINEL)
    assert SENTINEL not in json.dumps(creds.model_dump(mode="json"))
    assert SENTINEL not in repr(creds)
    # A record stored without the key (every record, from now on) still re-validates.
    assert APIKeyCredentials.model_validate(creds.model_dump(mode="json")).api_key is None
    with pytest.raises(ValueError):
        APIKeyCredentials(api_key="")
