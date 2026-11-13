"""Platform feature flags (Phase 0 item 5): fail closed, exact 404, registry-bound, cached.

Contract under test (services/feature_flags.py, routers/config.py):

* a flag is on only for literal JSON ``true`` under a KNOWN name; a missing parameter, an SSM
  error, non-JSON, a non-object, an unknown key or a non-boolean value all resolve to off, and
  unknown / non-boolean keys are reported in ``ignored``;
* ``require_feature`` on an unregistered name is refused at import time; a flagged-off route
  answers the exact ``404 {"detail": "Not Found"}`` of a nonexistent route to every caller,
  including one that lacks the route's scope (flag is checked first);
* the resolved set is cached per container for the TTL and re-read after it;
* ``GET /api/config/features`` is behind ``agent:read`` (every builder holds it) and lists every
  known flag with its description; the canary route flips 404 → 200 with the flag;
* local development reads ``FEATURE_FLAGS`` from the environment with the same rules.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from app.routers import config as config_router
from app.services import feature_flags as ff
from app.services import rbac
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

CANARY = "platformFlagsCanary"
PARAM = "/agentcore-workflow/dev/platform-features"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    ff.reset_feature_flag_cache()
    monkeypatch.delenv(ff.LOCAL_ENV_VAR, raising=False)
    monkeypatch.delenv(ff.TTL_ENV_VAR, raising=False)
    yield
    ff.reset_feature_flag_cache()


def _deployed(monkeypatch, *, value=None, error=None):
    """Point the loader at a fake SSM client in a deployed environment."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    client = MagicMock()
    if error is not None:
        client.get_parameter.side_effect = error
    else:
        client.get_parameter.return_value = {"Parameter": {"Name": PARAM, "Value": value}}
    monkeypatch.setattr(ff, "_create_ssm_client", lambda region: client)
    return client


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetParameter")


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "raw, expected_on, ignored, valid",
    [
        (None, False, (), True),
        ("{}", False, (), True),
        ('{"platformFlagsCanary": true}', True, (), True),
        ('{"platformFlagsCanary": false}', False, (), True),
        ('{"platformFlagsCanary": "true"}', False, ("platformFlagsCanary",), True),  # string, not boolean
        ('{"platformFlagsCanary": 1}', False, ("platformFlagsCanary",), True),
        ('{"PlatformFlagsCanary": true}', False, ("PlatformFlagsCanary",), True),  # case matters
        ('{"copilot": true}', False, ("copilot",), True),  # not registered in this build
        ("true", False, (), False),
        ("[]", False, (), False),
        ("not json", False, (), False),
        ("", False, (), False),
    ],
)
def test_parse_fails_closed_and_reports_what_it_ignored(raw, expected_on, ignored, valid) -> None:
    features, got_ignored, got_valid = ff.parse_feature_flags(raw)
    assert set(features) == set(ff.KNOWN_FEATURE_FLAGS)
    assert features[CANARY] is expected_on
    assert got_ignored == ignored
    assert got_valid is valid


def test_registry_has_the_canary_and_nothing_undocumented() -> None:
    assert CANARY in ff.KNOWN_FEATURE_FLAGS
    assert all(desc.strip() for desc in ff.KNOWN_FEATURE_FLAGS.values())
    assert ff.all_off() == dict.fromkeys(ff.KNOWN_FEATURE_FLAGS, False)


# --------------------------------------------------------------------------- resolution sources


def test_ssm_true_turns_the_flag_on_and_reports_the_source(monkeypatch) -> None:
    client = _deployed(monkeypatch, value=json.dumps({CANARY: True}))
    flags = ff.get_feature_flags()
    assert flags.enabled(CANARY) is True
    assert (flags.source, flags.parameter, flags.ignored) == (ff.SOURCE_SSM, PARAM, ())
    client.get_parameter.assert_called_once_with(Name=PARAM, WithDecryption=True)


@pytest.mark.parametrize(
    "error, source",
    [
        (_client_error("ParameterNotFound"), ff.SOURCE_MISSING),
        (_client_error("AccessDeniedException"), ff.SOURCE_ERROR),
        (_client_error("ThrottlingException"), ff.SOURCE_ERROR),
        (EndpointConnectionError(endpoint_url="https://ssm.us-east-1.amazonaws.com"), ff.SOURCE_ERROR),
    ],
)
def test_an_absent_or_unreadable_parameter_means_every_flag_off(monkeypatch, error, source) -> None:
    _deployed(monkeypatch, error=error)
    flags = ff.get_feature_flags()
    assert flags.features == ff.all_off()
    assert flags.source == source


def test_a_client_that_cannot_be_built_means_every_flag_off(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")

    def _boom(region):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(ff, "_create_ssm_client", _boom)
    flags = ff.get_feature_flags()
    assert flags.features == ff.all_off() and flags.source == ff.SOURCE_ERROR


def test_an_invalid_value_means_every_flag_off_and_says_so(monkeypatch) -> None:
    _deployed(monkeypatch, value="[1, 2]")
    flags = ff.get_feature_flags()
    assert flags.features == ff.all_off() and flags.source == ff.SOURCE_INVALID


def test_unknown_and_non_boolean_keys_are_ignored_not_honoured(monkeypatch) -> None:
    _deployed(monkeypatch, value=json.dumps({CANARY: "true", "copilot": True, "hub": None}))
    flags = ff.get_feature_flags()
    assert flags.enabled(CANARY) is False
    assert flags.ignored == ("copilot", "hub", CANARY)
    assert flags.source == ff.SOURCE_SSM


def test_local_development_reads_the_environment_variable_with_the_same_rules(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv(ff.LOCAL_ENV_VAR, json.dumps({CANARY: True, "typo": True}))
    monkeypatch.setattr(ff, "_create_ssm_client", lambda region: pytest.fail("local must not touch SSM"))
    flags = ff.get_feature_flags()
    assert flags.enabled(CANARY) is True and flags.source == ff.SOURCE_ENV and flags.ignored == ("typo",)
    ff.reset_feature_flag_cache()
    monkeypatch.delenv(ff.LOCAL_ENV_VAR)
    assert ff.get_feature_flags().source == ff.SOURCE_MISSING


def test_feature_enabled_refuses_an_unregistered_name(monkeypatch) -> None:
    _deployed(monkeypatch, value="{}")
    with pytest.raises(ff.UnknownFeatureFlagError, match="unknown feature flag 'copilot'"):
        ff.feature_enabled("copilot")
    assert ff.feature_enabled(CANARY) is False


# --------------------------------------------------------------------------- cache


def test_the_set_is_cached_for_the_ttl_and_reread_after_it(monkeypatch) -> None:
    client = _deployed(monkeypatch, value="{}")
    now = [1000.0]
    monkeypatch.setattr(ff.time, "monotonic", lambda: now[0])
    monkeypatch.setenv(ff.TTL_ENV_VAR, "60")
    assert ff.get_feature_flags().enabled(CANARY) is False
    client.get_parameter.return_value = {"Parameter": {"Value": json.dumps({CANARY: True})}}
    now[0] += 59
    assert ff.get_feature_flags().enabled(CANARY) is False, "within the TTL the cached set is served"
    assert client.get_parameter.call_count == 1
    now[0] += 2
    assert ff.get_feature_flags().enabled(CANARY) is True, "after the TTL the parameter is re-read"
    assert client.get_parameter.call_count == 2
    assert ff.get_feature_flags(force_refresh=True).enabled(CANARY) is True
    assert client.get_parameter.call_count == 3


def test_a_zero_or_garbage_ttl_means_read_every_time(monkeypatch) -> None:
    client = _deployed(monkeypatch, value="{}")
    monkeypatch.setenv(ff.TTL_ENV_VAR, "0")
    ff.get_feature_flags()
    ff.get_feature_flags()
    assert client.get_parameter.call_count == 2
    monkeypatch.setenv(ff.TTL_ENV_VAR, "soon")
    assert ff.ttl_seconds() == ff.DEFAULT_TTL_SECONDS
    monkeypatch.setenv(ff.TTL_ENV_VAR, "-5")
    assert ff.ttl_seconds() == 0


# --------------------------------------------------------------------------- require_feature


def test_require_feature_refuses_an_unregistered_flag_at_definition_time() -> None:
    with pytest.raises(ff.UnknownFeatureFlagError, match="unknown flag 'copilot'"):
        ff.require_feature("copilot")
    assert ff.require_feature(CANARY).feature_flag == CANARY


def _request(groups=None) -> Request:
    claims = {"sub": "user-1"}
    if groups is not None:
        claims["cognito:groups"] = groups
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/config/features/canary",
        "headers": [],
        "aws.event": {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}},
    }
    return Request(scope)


def test_require_feature_answers_the_exact_not_found_of_a_nonexistent_route(monkeypatch) -> None:
    _deployed(monkeypatch, value="{}")
    dep = ff.require_feature(CANARY)
    with pytest.raises(Exception) as error:
        dep(_request(["g-admins-super"]))
    assert (error.value.status_code, error.value.detail) == (404, "Not Found")
    ff.reset_feature_flag_cache()
    _deployed(monkeypatch, value=json.dumps({CANARY: True}))
    assert dep(_request(["g-admins-super"])) is None


# --------------------------------------------------------------------------- router


def _app(monkeypatch, groups=("g-users-default",)) -> TestClient:
    """The config router under enforced RBAC, with the API Gateway JWT event a Lambda request carries.

    ``caller_scopes`` grants every scope when ``aws.event`` is absent (local dev), so the event
    is injected exactly as Mangum does, with the caller's ``cognito:groups`` claim.
    """
    monkeypatch.setenv("RBAC_ENFORCE", "true")
    app = FastAPI()
    app.include_router(config_router.router)
    claims = {"sub": "user-1", "cognito:groups": list(groups)}

    @app.middleware("http")
    async def _inject_lambda_event(request, call_next):
        request.scope["aws.event"] = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
        return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def test_features_route_lists_every_known_flag_with_its_description(monkeypatch) -> None:
    _deployed(monkeypatch, value=json.dumps({CANARY: True, "typo": 1}))
    resp = _app(monkeypatch).get("/api/config/features")
    assert resp.status_code == 200
    body = resp.json()
    # Every KNOWN flag is listed; the one the parameter set is on, the rest are off (unset = false).
    assert body["features"] == {name: name == CANARY for name in ff.KNOWN_FEATURE_FLAGS}
    assert body["features"][CANARY] is True
    assert body["source"] == "ssm" and body["parameter"] == PARAM
    assert body["ignored"] == ["typo"] and body["ttlSeconds"] == 60
    assert set(body["descriptions"]) == set(ff.KNOWN_FEATURE_FLAGS)
    assert body["refreshedAt"].endswith("+00:00")


def test_features_route_requires_agent_read_which_every_builder_holds(monkeypatch) -> None:
    _deployed(monkeypatch, value="{}")
    assert "agent:read" in rbac.GROUP_SCOPES["g-users-default"]
    assert _app(monkeypatch, groups=("g-users-default",)).get("/api/config/features").status_code == 200
    assert _app(monkeypatch, groups=()).get("/api/config/features").status_code == 403


def test_canary_route_is_a_404_until_the_flag_is_on_then_200(monkeypatch) -> None:
    _deployed(monkeypatch, value="{}")
    client = _app(monkeypatch)
    off = client.get("/api/config/features/canary")
    nonexistent = client.get("/api/config/nonexistent")
    assert (off.status_code, off.json()) == (404, {"detail": "Not Found"})
    assert (off.status_code, off.json()) == (nonexistent.status_code, nonexistent.json())
    ff.reset_feature_flag_cache()
    _deployed(monkeypatch, value=json.dumps({CANARY: True}))
    on = client.get("/api/config/features/canary")
    assert (on.status_code, on.json()) == (200, {"canary": "enabled", "flag": CANARY})


def test_flagged_off_route_is_404_even_for_a_caller_without_the_scope(monkeypatch) -> None:
    """The flag is checked before the scope, so a flagged-off route never reveals itself with a 403."""
    _deployed(monkeypatch, value="{}")
    assert _app(monkeypatch, groups=()).get("/api/config/features/canary").status_code == 404
    ff.reset_feature_flag_cache()
    _deployed(monkeypatch, value=json.dumps({CANARY: True}))
    assert _app(monkeypatch, groups=()).get("/api/config/features/canary").status_code == 403


def test_canary_route_declares_flag_before_scope() -> None:
    route = next(r for r in config_router.router.routes if r.path == "/api/config/features/canary")
    kinds = [
        getattr(d.dependency, "feature_flag", None) or getattr(d.dependency, "required_scopes", None)
        for d in route.dependencies
    ]
    assert kinds == [CANARY, ("agent:read",)]


def test_main_app_mounts_the_config_router(monkeypatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "local")
    from app.main import app

    # main.py includes routers lazily (an _IncludedRouter per include_router call), so the
    # effective routes are read the way the API-route parity test reads them.
    routes = []
    for route in app.routes:
        if hasattr(route, "effective_route_contexts"):
            routes.extend(route.effective_route_contexts())
        else:
            routes.append(route)
    paths = {getattr(route, "path", None) for route in routes}
    assert {"/api/config/features", "/api/config/features/canary"} <= paths
