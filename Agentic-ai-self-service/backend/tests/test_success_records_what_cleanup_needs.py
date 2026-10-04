"""A successful deploy must record everything DELETE needs to clean up.

The defect this pins. ``status_update_step`` has two ``store.update_status`` calls: a
FAILED one that saves partial results "so delete handler can clean up", and a
SUCCEEDED one. The FAILED call passed ``guardrails_result``; the SUCCEEDED call did
not. ``deployment_handler``'s cleanup Step 0.7 is that field's only consumer -- it
deletes a flow-created Bedrock guardrail only when the *stored* record has
``guardrails_result.created_by_flow``. So deleting a successfully-deployed agent left
the guardrail behind, and because the branch was skipped rather than raising, nothing
was appended to ``cleanup_failures`` and ``DELETE`` returned success.

Worth noting which way round that is: the error path was correct and the happy path
leaked. A test that only exercised failure would have passed.

The interesting test here is not the one that names ``guardrails_result`` -- that one
goes green the moment the field is added and then guards nothing general. It is
``test_success_persists_every_result_field_failure_does``, which compares the two call
sites structurally, so the next result field added to one branch and forgotten in the
other fails immediately.
"""

import ast
import inspect
import pathlib

import pytest

# Fields the SUCCESS call is allowed to omit, each with the reason it is not a leak.
# Anything else missing is a cleanup gap by default.
_ALLOWED_TO_DIFFER = {
    # Meaningless on success; it is the failure description itself.
    "error_details",
}


def _handler_source() -> str:
    from app.step_handlers import status_update_step

    return pathlib.Path(inspect.getfile(status_update_step)).read_text()


def _update_status_calls() -> list[set[str]]:
    """Keyword names of every ``store.update_status(...)`` call in the module.

    Parsed rather than monkeypatched because the two branches are mutually exclusive
    at runtime: a single handler invocation can only ever reach one of them, so no
    behavioural test can compare the two call sites against each other.
    """
    tree = ast.parse(_handler_source())
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "update_status":
            calls.append({kw.arg for kw in node.keywords if kw.arg})
    return calls


def _branch_calls() -> tuple[set[str], set[str]]:
    """The FAILED call and the SUCCEEDED call, told apart by ``error_details``."""
    calls = [c for c in _update_status_calls() if c]
    failed = [c for c in calls if "error_details" in c]
    succeeded = [c for c in calls if "error_details" not in c]
    assert failed, "no update_status call carrying error_details; did the module change shape?"
    assert succeeded, "no update_status call without error_details; did the module change shape?"
    # Take the richest of each, so an unrelated small update_status call elsewhere in
    # the module cannot masquerade as the terminal one.
    return max(failed, key=len), max(succeeded, key=len)


def test_the_parser_finds_both_branches():
    """Vacuity guard. An ast walk that matched nothing would make every assertion
    below trivially true."""
    failed, succeeded = _branch_calls()
    assert len(failed) > 5, f"suspiciously few kwargs on the FAILED call: {sorted(failed)}"
    assert len(succeeded) > 5, f"suspiciously few kwargs on the SUCCEEDED call: {sorted(succeeded)}"
    assert "runtime_id" in failed and "runtime_id" in succeeded


def test_success_persists_every_result_field_failure_does():
    """The general invariant. Everything the failure path saves for the deleter, the
    success path must save too -- a resource created by a *successful* deploy is at
    least as much of a leak risk as one created by a failed deploy."""
    failed, succeeded = _branch_calls()
    missing = failed - succeeded - _ALLOWED_TO_DIFFER
    assert not missing, (
        "the SUCCEEDED update_status omits field(s) the FAILED one records: "
        f"{sorted(missing)}.\n"
        "deployment_handler's cleanup reads these off the stored record to decide what "
        "to delete, so a field missing here means that resource is leaked when a "
        "successfully-deployed agent is deleted -- silently, because a skipped cleanup "
        "branch adds nothing to cleanup_failures.\n"
        "Either pass it on the success call too, or add it to _ALLOWED_TO_DIFFER with "
        "the reason it cannot leak."
    )


@pytest.mark.parametrize(
    "field",
    ["guardrails_result", "gateway_result", "memory_result", "knowledge_base_result", "policy_result"],
)
def test_each_cleanup_input_is_recorded_on_success(field):
    """Named explicitly as well, so the failure message says which resource leaks."""
    _, succeeded = _branch_calls()
    assert field in succeeded, f"{field} is not persisted on success; its cleanup branch will never run"


def test_every_field_cleanup_reads_is_in_the_allowed_or_recorded_set():
    """Close the loop from the other end: read the cleanup handler and check each
    ``deployment_record.get("<x>_result")`` it consumes is actually persisted on the
    success path. Catches a new cleanup step added against a field nobody stores."""
    from app.deployment_handler import __file__ as handler_file

    src = pathlib.Path(handler_file).read_text()
    tree = ast.parse(src)
    consumed: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "deployment_record"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.endswith("_result")
        ):
            consumed.add(node.args[0].value)

    assert consumed, "found no deployment_record.get('*_result') reads; the probe is vacuous"

    _, succeeded = _branch_calls()
    unstored = consumed - succeeded
    assert not unstored, (
        f"deployment_handler cleanup reads {sorted(unstored)} off the stored deployment "
        "record, but the SUCCEEDED update_status never writes it. That cleanup step is "
        "dead on the success path."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
