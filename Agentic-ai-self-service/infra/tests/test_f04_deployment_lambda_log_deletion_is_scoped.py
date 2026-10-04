"""F-04: the deployment Lambda deletes exactly the log groups the backend deletes, never ``*``.

``lambdas.py`` bundled ``logs:DeleteLogGroup`` into the Logs Insights statement on
``Resource: "*"``. Every ``delete_log_group`` reachable from this Lambda names
``/aws/bedrock-agentcore/evaluations/results/{id}``: ``deployment_handler.py:3372`` and
``runtime_deployer.destroy_runtime`` (``:1649``). The two in ``cfn_provider/handler.py`` run in
the exported customer stack's own provider role, not here. The failure-path step role already
scoped the same verb to that prefix (``step_lambdas.py``); the API role did not. ``DeleteLogGroup``
supports the ``log-group`` resource per the Service Reference feed and no condition keys, so the
resource is the only lever. ARCC cnt_SFJJhkOueCPRkd; a Lambda parsing untrusted canvases must not
be one bug away from erasing every log group in the account.
"""

from __future__ import annotations

import pytest

from tests.iam_attachment import role_logical_id, statements_for_role
from tests.p1_synth import actions, all_statements, resources_text, synth

DELETE = "logs:DeleteLogGroup"
EVAL_RESULTS_PREFIX = "log-group:/aws/bedrock-agentcore/evaluations/results/*"


@pytest.fixture(scope="module")
def tpl() -> dict:
    return synth("F04LogDeletionStack")


def test_the_deployment_lambda_can_still_delete_evaluation_result_groups(tpl):
    """Reach: the verb must still be granted, on the prefix the code deletes, or teardown leaks."""
    lid = role_logical_id(tpl, "DeploymentLambdaRole")
    grants = [st for _p, st in statements_for_role(tpl, lid) if st.get("Effect") == "Allow" and DELETE in actions(st)]
    assert grants, "the deployment Lambda lost logs:DeleteLogGroup entirely; destroy_runtime's eval cleanup is dead"
    assert any(any(EVAL_RESULTS_PREFIX in r for r in resources_text(st)) for st in grants)


def test_the_deployment_lambda_never_holds_delete_log_group_on_a_wildcard(tpl):
    """The assertion that fails on the pre-fix tree."""
    lid = role_logical_id(tpl, "DeploymentLambdaRole")
    for pid, st in statements_for_role(tpl, lid):
        if st.get("Effect") != "Allow" or DELETE not in actions(st):
            continue
        for r in resources_text(st):
            assert r != "*", f"{pid}: logs:DeleteLogGroup on Resource '*'"
            assert EVAL_RESULTS_PREFIX in r, f"{pid}: logs:DeleteLogGroup on a resource the backend never deletes: {r}"


def test_no_role_anywhere_holds_delete_log_group_on_a_wildcard(tpl):
    for pid, st in all_statements(tpl):
        if st.get("Effect") == "Allow" and DELETE in actions(st):
            assert "*" not in resources_text(st), f"{pid}: account-wide log deletion"
