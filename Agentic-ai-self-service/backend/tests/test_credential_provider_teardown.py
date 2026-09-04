"""A credential provider must never survive a teardown that reports success.

Found by tearing down real deployments and then reading the account: five
credential providers were still in the vault after a delete that returned
``success=True``. Two independent causes, both covered here.

1. **The external-MCP path recorded provider names UNTYPED.** The connector path
   records ``"TYPE:name"`` (``API_KEY`` / ``OAUTH``), but the external-MCP path
   appended a bare name, and ``gateway_step._record_gateway_resources`` defaults an
   untyped entry to ``oauth2_credential_provider``. So every API-key provider for an
   external MCP server was filed under the wrong type.

2. **The two deleters silently no-op on each other's providers.** Verified live:
   ``delete_oauth2_credential_provider`` on an API-key provider returns success
   WITHOUT deleting it. A mis-typed manifest row therefore produced a clean-looking
   teardown and a stranded credential — the worst possible combination, because the
   leak is invisible and the credential outlives the agent it belonged to.

Fix (1) with typed records at the producer, and (2) by purging BOTH namespaces and
using each namespace's own ``get_*`` as the discriminator.
"""

from unittest.mock import MagicMock

import pytest
from app.services import gateway_deployer as gd
from botocore.exceptions import ClientError


def _not_found() -> ClientError:
    """A real ClientError, because the production code matches on the AWS error
    CODE rather than on ``str(e)`` (see services/aws_errors.py). A fake that raises
    a plain RuntimeError('ResourceNotFoundException') would pass while proving
    nothing about the live behavior."""
    return ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no such provider"}}, "GetProvider")


def _access_denied() -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "DeleteProvider")


class _Vault:
    """The credential-provider vault, modelled as what it actually is: TWO
    independent name->record namespaces behind one account-global API.

    The delete methods reproduce the live misbehavior that started all of this —
    deleting a name absent from *this* namespace is NOT an error and does NOT
    touch the other namespace.
    """

    def __init__(self, api_key=(), oauth=()):
        self.api_key = set(api_key)
        self.oauth = set(oauth)
        self.calls: list[str] = []

    def get_api_key_credential_provider(self, *, name):
        self.calls.append(f"get_api_key:{name}")
        if name not in self.api_key:
            raise _not_found()
        return {"credentialProviderArn": f"arn:...:apikey/{name}"}

    def get_oauth2_credential_provider(self, *, name):
        self.calls.append(f"get_oauth:{name}")
        if name not in self.oauth:
            raise _not_found()
        return {"credentialProviderArn": f"arn:...:oauth/{name}"}

    def delete_api_key_credential_provider(self, *, name):
        self.calls.append(f"delete_api_key:{name}")
        self.api_key.discard(name)

    def delete_oauth2_credential_provider(self, *, name):
        self.calls.append(f"delete_oauth:{name}")
        self.oauth.discard(name)


class TestPurgeLeavesNothingBehind:
    def test_an_api_key_provider_is_actually_gone(self):
        v = _Vault(api_key={"mcp-mcp-exa-abc123"})
        ok, msg = gd.purge_credential_provider(v, "mcp-mcp-exa-abc123")
        assert ok, msg
        assert v.api_key == set()

    def test_an_oauth_provider_is_actually_gone(self):
        v = _Vault(oauth={"mcp-databricks-abc123"})
        ok, msg = gd.purge_credential_provider(v, "mcp-databricks-abc123")
        assert ok, msg
        assert v.oauth == set()

    def test_it_does_not_delete_from_the_namespace_the_name_is_absent_from(self):
        """The probe, not a blind double-delete: a name that only exists as an API
        key must not send a delete to the OAuth namespace, where an unrelated
        provider could legitimately share the name."""
        v = _Vault(api_key={"shared-name"}, oauth={"shared-name"})
        gd.purge_credential_provider(v, "shared-name")
        # Both exist here, so both are purged — the point is that each delete was
        # preceded by a successful get of that same namespace.
        assert v.api_key == set() and v.oauth == set()

        v2 = _Vault(api_key={"only-apikey"})
        gd.purge_credential_provider(v2, "only-apikey")
        assert "delete_oauth:only-apikey" not in v2.calls

    def test_an_already_gone_name_is_success_not_failure(self):
        """Teardown is retried; the second pass must not report a failure."""
        ok, msg = gd.purge_credential_provider(_Vault(), "long-gone")
        assert ok
        assert "already gone" in msg

    def test_a_real_delete_error_is_reported_as_failure(self):
        """Anything other than not-found must surface. A silent False-negative here
        is precisely how the five orphans went unnoticed."""
        v = _Vault(api_key={"p1"})
        v.delete_api_key_credential_provider = MagicMock(side_effect=_access_denied())
        ok, msg = gd.purge_credential_provider(v, "p1")
        assert not ok
        assert "AccessDenied" in msg


class TestTheUntypedLegacyRecordStillGetsCleanedUp:
    def test_a_bare_name_naming_an_api_key_provider_is_deleted(self):
        """Records written before the producer fix carry no type prefix. The old
        implementation verified deletion with the WRONG namespace's getter — which
        always reports not-found for the other type — and so declared victory
        after deleting nothing."""
        v = _Vault(api_key={"mcp-mcp-custom-litellm-proxy-a8d3f22a"})
        ok, _ = gd._delete_connector_credential_provider(v, "mcp-mcp-custom-litellm-proxy-a8d3f22a")
        assert ok
        assert v.api_key == set()

    def test_a_bare_name_naming_an_oauth_provider_is_deleted(self):
        v = _Vault(oauth={"legacy-oauth-name"})
        ok, _ = gd._delete_connector_credential_provider(v, "legacy-oauth-name")
        assert ok
        assert v.oauth == set()


class TestTheExternalMcpPathRecordsTheType:
    """Cause (1). These assert the RECORDED string, because the recorded string is
    the only input teardown gets."""

    @staticmethod
    def _capture(monkeypatch):
        monkeypatch.setattr(
            gd,
            "_create_gateway_target_with_retry",
            lambda ctrl, gw, name, params: {"targetId": "t-1"},
        )
        monkeypatch.setattr(gd, "_put_connector_secret", lambda region, owner, payload: "arn:aws:sm:::secret:fake")

    def test_an_api_key_server_is_recorded_as_api_key(self, monkeypatch):
        self._capture(monkeypatch)
        ctrl = _Vault()
        ctrl.create_api_key_credential_provider = MagicMock(return_value={"credentialProviderArn": "arn:...:p"})
        ctrl.get_gateway_target = MagicMock(return_value={"status": "READY", "statusReasons": []})

        out = gd._deploy_external_mcp_targets(
            ctrl, "gw-1", "us-east-1", [{"server_id": "exa", "secret_value": "sk-x"}], owner_sub="alice"
        )

        # The name must ALSO still match the one deploy_external_mcp_target creates
        # byte-for-byte — see the mirroring comment at the record site.
        expected = gd._scoped_provider_name(f"mcp-{gd._sanitize_provider_name('mcp-exa')[:48]}", "gw-1")
        assert out["credential_provider_names"] == [f"API_KEY:{expected}"]

    def test_an_oauth_server_is_recorded_as_oauth(self, monkeypatch):
        self._capture(monkeypatch)
        monkeypatch.setattr(gd, "_ensure_oauth2_credential_provider", lambda *a, **k: "arn:...:oauth-provider")
        ctrl = _Vault()
        ctrl.get_gateway_target = MagicMock(return_value={"status": "READY", "statusReasons": []})

        out = gd._deploy_external_mcp_targets(
            ctrl,
            "gw-1",
            "us-east-1",
            [
                {
                    "server_id": "databricks",
                    "endpoint_vars": {"workspace_hostname": "acme.cloud.databricks.com", "service": "vector-search"},
                    "oauth": {
                        "client_id": "cid",
                        "client_secret": "csecret",
                        "discovery_url": "https://acme.cloud.databricks.com/.well-known/openid-configuration",
                    },
                }
            ],
            owner_sub="alice",
        )
        assert out["credential_provider_names"] == [f"OAUTH:{gd._scoped_provider_name('mcp-databricks', 'gw-1')}"]

    def test_no_recorded_entry_is_ever_untyped(self, monkeypatch):
        """The regression guard, stated as the invariant rather than per-case: a
        teardown cannot recover a type it was never told."""
        self._capture(monkeypatch)
        ctrl = _Vault()
        ctrl.create_api_key_credential_provider = MagicMock(return_value={"credentialProviderArn": "arn:...:p"})
        ctrl.get_gateway_target = MagicMock(return_value={"status": "READY", "statusReasons": []})
        out = gd._deploy_external_mcp_targets(
            ctrl, "gw-1", "us-east-1", [{"server_id": "exa", "secret_value": "sk-x"}], owner_sub="alice"
        )
        for entry in out["credential_provider_names"]:
            assert entry.split(":", 1)[0] in {"API_KEY", "OAUTH"}, entry


class TestThePreMintedSecretIsTornDownToo:
    """The same leak one layer over, found in the same live account read: nine
    ``agentcore-connector/`` secrets holding a raw API key were still there after
    their agents were deleted.

    ``gateway_step`` mints the external-MCP api_key secret EARLY (so the plaintext is
    dropped before the SFN event is re-emitted) and passes ``secret_arn`` down. The
    deployer tracked only secrets it minted itself, so nothing recorded the
    pre-minted one and no manifest row ever existed to delete it. The connector path
    already had this exact fix; the external-MCP path did not.
    """

    _PLATFORM = "arn:aws:secretsmanager:us-east-1:1:secret:agentcore-connector/alice/abc123-x1"
    _CUSTOMER = "arn:aws:secretsmanager:us-east-1:1:secret:prod/my-own-vendor-key-y2"

    def _deploy(self, monkeypatch, secret_arn):
        TestTheExternalMcpPathRecordsTheType._capture(monkeypatch)
        ctrl = _Vault()
        ctrl.create_api_key_credential_provider = MagicMock(return_value={"credentialProviderArn": "arn:...:p"})
        ctrl.get_gateway_target = MagicMock(return_value={"status": "READY", "statusReasons": []})
        return gd._deploy_external_mcp_targets(
            ctrl,
            "gw-1",
            "us-east-1",
            [{"server_id": "exa", "secret_arn": secret_arn}],
            owner_sub="alice",
        )

    def test_a_secret_minted_by_the_step_handler_is_recorded(self, monkeypatch):
        out = self._deploy(monkeypatch, self._PLATFORM)
        assert out["secret_arns"] == [self._PLATFORM]

    def test_a_caller_owned_secret_is_not_scheduled_for_deletion(self, monkeypatch):
        """Teardown deletes what the platform created. Deleting a secret the customer
        brought would destroy a credential outside this agent's lifecycle."""
        out = self._deploy(monkeypatch, self._CUSTOMER)
        assert out["secret_arns"] == []

    @pytest.mark.parametrize(
        "arn,owned",
        [
            ("arn:aws:secretsmanager:eu-central-1:1:secret:agentcore-connector/bob/deadbeef-Ab1", True),
            ("agentcore-connector/bob/deadbeef", True),  # bare name (direct-path callers)
            ("arn:aws:secretsmanager:us-east-1:1:secret:my-agentcore-connector/x-Zz9", False),
            ("arn:aws:secretsmanager:us-east-1:1:secret:agentcore-provider/openai-Qq2", False),
            ("", False),
        ],
    )
    def test_ownership_is_decided_by_the_name_segment(self, arn, owned):
        """Region and account vary; the ``agentcore-connector/`` name prefix is the
        marker, and it must be matched at the START of the name segment so a secret
        merely containing the string is not swept up."""
        assert gd._is_platform_connector_secret(arn) is owned

    def test_gateway_step_records_a_secret_row_for_it(self):
        from app.step_handlers import gateway_step

        store = MagicMock()
        gateway_step._record_gateway_resources(store, "dep-1", "us-east-1", {"connector_secret_arns": [self._PLATFORM]})
        rows = [c.args[1] for c in store.record_resource.call_args_list]
        assert {"type": "secret", "id": self._PLATFORM, "region": "us-east-1"} in rows


class TestAFailedGatewayDeployIsStillTearableDown:
    """The third leak from the same live account, and the worst of them: a deploy
    that failed AFTER creating the gateway recorded NOTHING.

    ``created_resources`` came back null, so no runtime existed to scan for and no
    manifest row named the gateway -- nothing could ever delete it. Two orphan
    gateways, each with a Cognito user pool, were stranded that way.

    ``deploy_gateway`` does attempt its own abort cleanup, but
    ``cleanup_gateway_resources`` never raises: it collects per-resource failures
    into its RETURN VALUE, which the abort path discarded while logging success at
    INFO. So the one time it failed, it failed invisibly and permanently.
    """

    class _Store:
        def __init__(self):
            self.resources = []

        def update_step(self, *a, **kw):
            pass

        def record_resource(self, deployment_id, resource):
            self.resources.append(resource)

    def _run_failing_step(self, monkeypatch, gateway_result):
        from app.step_handlers import gateway_step

        # Pin the region: the step reads APP_AWS_REGION and the dev shell exports
        # us-west-2, which would make the row assertions pass or fail by accident.
        monkeypatch.setenv("APP_AWS_REGION", "us-east-1")
        store = self._Store()
        monkeypatch.setattr(gateway_step, "_get_deployment_store", lambda: store)
        monkeypatch.setattr(gateway_step, "deploy_gateway", lambda **kw: gateway_result)
        with pytest.raises(RuntimeError, match="Gateway deployment failed"):
            gateway_step.handler({"deployment_id": "d1", "gateway_config": {"name": "gw"}}, None)
        return store.resources

    _FAILED = {
        "success": False,
        "error": "AccessDeniedException: Access denied when retrieving the provided secret",
        "gateway_id": "llmtgtp10-3que8whkac",
        "gateway_name": "LlmTgtP10",
        "client_info": {"provider": "cognito", "user_pool_id": "us-east-1_J6MhzCdas"},
        "connector_credential_providers": ["API_KEY:mcp-mcp-exa-abc"],
        "connector_secret_arns": ["arn:aws:secretsmanager:us-east-1:1:secret:agentcore-connector/a/b-x1"],
    }

    def test_the_gateway_is_recorded_so_a_later_delete_can_find_it(self, monkeypatch):
        rows = self._run_failing_step(monkeypatch, self._FAILED)
        assert {"type": "gateway", "id": "llmtgtp10-3que8whkac", "region": "us-east-1"} in rows

    def test_the_cognito_pool_is_recorded_too(self, monkeypatch):
        """The pool is the resource with a hard account quota, so an invisible leak
        here eventually blocks all deploys."""
        rows = self._run_failing_step(monkeypatch, self._FAILED)
        assert {"type": "cognito_user_pool", "id": "us-east-1_J6MhzCdas", "region": "us-east-1"} in rows

    def test_partial_credentials_and_secrets_are_recorded(self, monkeypatch):
        """Whatever the mid-loop rollback failed to remove is still named."""
        rows = self._run_failing_step(monkeypatch, self._FAILED)
        types = {r["type"] for r in rows}
        assert "api_key_credential_provider" in types
        assert "secret" in types

    def test_a_failure_before_the_gateway_exists_records_no_gateway_row(self, monkeypatch):
        """Symmetry: a row for a gateway that was never created would make every
        such teardown issue a delete for a nonexistent id."""
        rows = self._run_failing_step(monkeypatch, {"success": False, "error": "bad config"})
        assert not [r for r in rows if r["type"] == "gateway"]

    def test_the_error_payload_carries_no_client_secret(self):
        """The failure dict travels into a RuntimeError message and an SFN failure
        cause, unlike the success `result`. client_info nests client_secret, so the
        error path must hand back only the pool id."""
        import inspect

        from app.services import gateway_deployer as gdm

        src = inspect.getsource(gdm.deploy_gateway)
        tail = src[src.rindex("except Exception as e:") :]
        assert '"client_info": locals().get("client_info")' not in tail, (
            "the whole client_info dict (with client_secret) is being returned on the error path"
        )
        assert '_ci.get("user_pool_id")' in tail

    def test_the_pool_is_found_even_though_client_info_binds_late(self):
        """The subtle half of this defect, caught only by a live run: the Cognito
        pool is created near the TOP of deploy_gateway but ``client_info`` is not
        bound until the very END. A failure in between — which is most of the
        deploy, including all target creation — leaves ``client_info`` unbound, so a
        lookup of that name alone finds no pool and the pool is invisible to both
        the abort cleanup and the manifest. Verified live: run 11 recorded the
        gateway and role but left pool us-east-1_vhYIZjn34 unnamed.
        """
        import inspect

        from app.services import gateway_deployer as gdm

        src = inspect.getsource(gdm.deploy_gateway)
        tail = src[src.rindex("except Exception as e:") :]
        assert 'locals().get("cognito_response")' in tail, (
            "the error path reads only client_info, which is unbound for most failures"
        )
        # And the pool really is created before client_info is bound, which is the
        # premise that makes the above necessary.
        assert src.index("_create_cognito_oauth(") < src.index('client_info = cognito_response["client_info"]')

    def test_abort_cleanup_failures_are_surfaced(self):
        """cleanup_gateway_resources reports failures by RETURNING them. The abort
        path used to throw that list away and log success regardless, which is why
        the live leak had no log line at all."""
        import inspect

        from app.services import gateway_deployer as gdm

        src = inspect.getsource(gdm.deploy_gateway)
        tail = src[src.rindex("except Exception as e:") :]
        assert "_msgs = cleanup_gateway_resources(" in tail, "the returned cleanup log is still being discarded"
        assert "Abort-cleanup left resources behind" in tail


class TestTheManifestRowRoutesCorrectly:
    def test_gateway_step_maps_an_api_key_entry_to_the_api_key_type(self):
        """Cause (1) end-to-end at the seam: the typed producer only helps if the
        step handler carries the type through to the manifest."""
        from app.step_handlers import gateway_step

        store = MagicMock()
        gateway_step._record_gateway_resources(
            store,
            "dep-1",
            "us-east-1",
            {"connector_credential_providers": ["API_KEY:mcp-mcp-exa-abc", "OAUTH:mcp-databricks-abc"]},
        )
        recorded = [c.args[1] for c in store.record_resource.call_args_list]
        by_name = {r["name"]: r["type"] for r in recorded if "name" in r}
        assert by_name["mcp-mcp-exa-abc"] == "api_key_credential_provider"
        assert by_name["mcp-databricks-abc"] == "oauth2_credential_provider"

    @pytest.mark.parametrize("row_type", ["oauth2_credential_provider", "api_key_credential_provider"])
    def test_a_mistyped_manifest_row_still_deletes_the_provider(self, row_type, monkeypatch):
        """Cause (2), and the repair for records ALREADY written: whichever type the
        row claims, the provider must end up gone. The live orphans were API-key
        providers filed as oauth2."""
        from app import deployment_handler as dh

        v = _Vault(api_key={"mcp-mcp-custom-litellm-proxy-a8d3f22a6e"})
        # _delete_managed_resource rebinds boto3 to a target-aware shim that routes
        # through step_clients, so that is the seam to patch (patching dh.boto3 is
        # shadowed and would test nothing).
        from app.services import step_clients

        monkeypatch.setattr(step_clients, "client", lambda event, service, **kw: v)

        msg = dh._delete_managed_resource(
            {"type": row_type, "name": "mcp-mcp-custom-litellm-proxy-a8d3f22a6e", "region": "us-east-1"},
            "us-east-1",
        )
        assert v.api_key == set(), "provider survived a teardown that returned a success message"
        assert "deleted" in msg

    def test_a_failing_provider_delete_fails_the_row(self, monkeypatch):
        """It must raise rather than return a cheerful string — the caller's only
        signal that teardown was incomplete."""
        from app import deployment_handler as dh

        v = _Vault(oauth={"p1"})
        v.delete_oauth2_credential_provider = MagicMock(side_effect=_access_denied())
        # _delete_managed_resource rebinds boto3 to a target-aware shim that routes
        # through step_clients, so that is the seam to patch (patching dh.boto3 is
        # shadowed and would test nothing).
        from app.services import step_clients

        monkeypatch.setattr(step_clients, "client", lambda event, service, **kw: v)

        with pytest.raises(Exception, match="AccessDenied"):
            dh._delete_managed_resource(
                {"type": "oauth2_credential_provider", "name": "p1", "region": "us-east-1"}, "us-east-1"
            )
