"""P0-C: one caller cannot reach another caller's export bundle through the platform.

A staged bundle's presigned URL is a deliberate one-hour BEARER capability (ARCC
cnt_AUfPj1lspAXlAO): whoever holds it can read the object, so leaking it is out of
scope here. The claim pinned below is narrower and structural. Through the platform's
own API and credentials, caller B cannot discover, list, read or delete caller A's
bundle, and the application does not log or store the URL. The key's 128-bit suffix makes
guessing hard, but that only limits discovery. The authorization is that no path
exists at all:

  * EVERY S3 call in the backend (any operation in boto3's own S3 model, the transfer
    helpers, presigns, paginators and waiters, writes included: a put_object or
    put_object_tagging with a caller's key could overwrite a bundle or forge the tag the
    generic delete trusts) is on a reviewed inventory keyed per call site, with the
    reason its key cannot be an export key. Every caller of a helper that hosts one of
    those calls is inventoried too, so the key's provenance is reviewed, not only the
    call. Dynamic dispatch (getattr, __getattribute__, attrgetter, list_all, the
    resource API, bound aliases, _make_api_call) is refused or on its own reviewed
    list. A new one fails until someone reviews it. This is the structural proof; it
    does not depend on route names;
  * the export prefixes are referenced by exactly one function, which only writes and
    presigns, and only the two export routes call it;
  * that function's URL, under WHATEVER name the route binds it to, goes straight into
    the returned dict. It is not logged, passed on, copied or stored;
  * as a TRIPWIRE only (it matches words, so ``/api/files/{key}`` would pass it): no
    route path besides the two exports looks like a bundle route. The route table is
    walked over the EFFECTIVE routes, because included routers are lazy;
  * the generic owned-object delete refuses a bundle's tag set, because a bundle
    carries no DeploymentId, whichever deployment id the caller supplies.

The infra half (JWT on every route, no identity pool, no caller-assumable role, no
caller grant in the bucket policy) is infra/tests/test_export_bundle_isolation.py.
Each checker below has a positive control that injects the violation it exists to catch.

What is and is not logged, layer by layer:
  * the route handlers and the stager do not log or store the URL (AST, below);
  * the audit middleware records the method, path, status, actor and session id, never
    a body (its fields are pinned below);
  * the HTTP API stage has no access log (infra test);
  * the artifacts bucket's S3 server access log DOES record each bundle's key and
    request URI. S3 redacts ``X-Amz-Signature`` in it, so the key is logged but the
    bearer capability is not (infra test pins the destination; the redaction is
    proven live);
  * a live search of the app log groups, DynamoDB tables, other API responses and the
    delivered S3 access logs for the URL's 64-hex signature closes what AST cannot see.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "app"

EXPORT_ROUTES = {("POST", "/api/generate-cfn-template"), ("POST", "/api/export-python")}
# Matches the path of anything that could serve or manage a bundle.
_BUNDLE_WORDS = re.compile(r"export|artifact|bundle|cfn|download|presign|s3", re.I)
# Matched by the words above but proven not to be bundle routes. Each entry needs a reason.
_NOT_BUNDLE_ROUTES = {
    # Returns the caller's own canvas JSON from DynamoDB, tenant-scoped; never touches S3.
    ("GET", "/api/workflows/{workflow_id}/export"),
}
_PREFIX_LITERALS = {"cfn-templates", "python-exports"}
_STAGER = "_stage_export_bundle"


# --------------------------------------------------------------------------- routes


def _effective_routes():
    from app.deployment_handler import deployment_app
    from app.main import app
    from fastapi.routing import APIRoute

    for application in (app, deployment_app):
        for route in application.routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "effective_route_contexts"):
                yield from route.effective_route_contexts()


def _method_paths(routes) -> set[tuple[str, str]]:
    return {(m, r.path) for r in routes for m in r.methods}


def bundle_route_tripwire(method_paths: set[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted(
        mp
        for mp in method_paths
        if _BUNDLE_WORDS.search(mp[1]) and mp not in EXPORT_ROUTES and mp not in _NOT_BUNDLE_ROUTES
    )


@pytest.fixture(scope="module")
def method_paths() -> set[tuple[str, str]]:
    return _method_paths(list(_effective_routes()))


def test_the_route_walk_reaches_included_routers_and_both_exports(method_paths):
    # Reach first: an oracle that saw only app.routes would pass vacuously.
    assert EXPORT_ROUTES <= method_paths
    assert ("GET", "/api/workflows/{workflow_id}/export") in method_paths  # an included router
    assert len(method_paths) > 100, len(method_paths)


def test_tripwire_no_route_but_the_two_exports_looks_like_a_bundle_route(method_paths):
    assert bundle_route_tripwire(method_paths) == []


@pytest.mark.parametrize(
    "injected",
    [("GET", "/api/exports"), ("GET", "/api/export-python/{key}"), ("DELETE", "/api/artifacts/{key}")],
)
def test_positive_control_a_bundle_route_is_caught(method_paths, injected):
    assert bundle_route_tripwire(method_paths | {injected}) == [injected]


# --------------------------------------------------------------------------- source


def _references_export_prefixes(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "EXPORT_BUNDLE_PREFIXES":
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            if any(p in sub.value for p in _PREFIX_LITERALS):
                return True
    return False


def prefix_reference_violations(sources: dict[str, str]) -> list[str]:
    """Every top-level statement or function, in every module, that names an export prefix,
    other than the prefix table itself and the one staging function."""
    out = []
    for rel, text in sources.items():
        for node in ast.parse(text).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPORT_BUNDLE_PREFIXES" for t in node.targets
            ):
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _STAGER:
                continue
            if _references_export_prefixes(node):
                out.append(f"{rel}:{getattr(node, 'name', type(node).__name__)}:{node.lineno}")
    return out


@pytest.fixture(scope="module")
def sources() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text() for p in SRC.rglob("*.py")}


def _function(text: str, name: str):
    return next(
        n
        for n in ast.walk(ast.parse(text))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )


def test_only_the_staging_function_names_an_export_prefix(sources):
    assert "deployment_handler.py" in sources and len(sources) > 50  # reach
    assert prefix_reference_violations(sources) == []


def test_positive_control_a_second_reader_of_the_prefixes_is_caught(sources):
    rogue = (
        "def list_my_bundles(s3, bucket):\n"
        "    return s3.list_objects_v2(Bucket=bucket, Prefix=EXPORT_BUNDLE_PREFIXES['cfn-template'])\n"
        "def purge(s3, bucket):\n"
        "    s3.delete_object(Bucket=bucket, Key='python-exports/x.zip')\n"
    )
    assert prefix_reference_violations({**sources, "rogue.py": rogue}) == [
        "rogue.py:list_my_bundles:1",
        "rogue.py:purge:3",
    ]


def stager_s3_calls(text: str) -> list[tuple[str, str | None]]:
    """``(method, first positional str arg)`` for every attribute call in the stager."""
    calls = []
    for sub in ast.walk(_function(text, _STAGER)):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
            first = sub.args[0].value if sub.args and isinstance(sub.args[0], ast.Constant) else None
            calls.append((sub.func.attr, first))
    return calls


def test_the_stager_only_writes_and_presigns_a_read(sources):
    s3_calls = {
        c for c in stager_s3_calls(sources["deployment_handler.py"]) if c[0] not in ("get", "hex", "urlencode", "uuid4")
    }
    assert s3_calls == {("put_object", None), ("generate_presigned_url", "get_object")}, s3_calls


def _is_stager(func: ast.AST) -> bool:
    return (isinstance(func, ast.Name) and func.id == _STAGER) or (
        isinstance(func, ast.Attribute) and func.attr == _STAGER
    )


def staged_url_names(fn: ast.AST) -> set[str]:
    """Every name a stager call's result is bound to, whatever it is called."""
    parents = {id(c): n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _is_stager(node.func):
            parent = parents[id(node)]
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                out.add(parent.targets[0].id)
    return out


def url_leaks(text: str, route: str) -> list[int]:
    """Lines in *route* where the staged URL goes anywhere but the returned dict.

    The URL is whatever a stager call is assigned to; the name does not matter. Allowed:
    that one assignment, and the name as a VALUE of a dict that is returned. Anything else
    (a log call, a helper call, a second assignment, an f-string, a bare ``return name``,
    or a stager call that is not a plain single-name assignment) is a leak.
    """
    fn = _function(text, route)
    parents = {id(c): n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    tainted = staged_url_names(fn)
    leaks = []
    returned = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _is_stager(node.func):
            parent = parents[id(node)]
            if not (
                isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name)
            ):
                leaks.append(node.lineno)
        elif isinstance(node, (ast.Name, ast.Attribute)) and _named(node) in _REFLECTION:
            # locals()["url"], builtins.locals(), v = vars: any REFERENCE, not only a direct call.
            leaks.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in tainted and isinstance(node.ctx, ast.Load):
            parent = parents[id(node)]
            # Exactly the value of the "download_url" key of a dict that is returned.
            under_download_url = isinstance(parent, ast.Dict) and any(
                v is node and isinstance(k, ast.Constant) and k.value == "download_url"
                for k, v in zip(parent.keys, parent.values, strict=True)
            )
            if under_download_url and isinstance(parents.get(id(parent)), ast.Return):
                returned += 1
            else:
                leaks.append(node.lineno)
    if returned != 1 and tainted:
        leaks.append(fn.lineno)  # zero or several returned uses of the URL
    return sorted(leaks)


# Names that reach an attribute or a local without spelling it: banned as REFERENCES (a call,
# an alias ``v = vars``, a qualified ``builtins.locals``) wherever this module looks.
_REFLECTION = frozenset(
    {
        "locals", "vars", "globals", "eval", "exec", "builtins", "__builtins__", "__dict__",
        "__getattribute__", "__getattr__", "attrgetter", "methodcaller", "import_module", "__import__",
    }
)  # fmt: skip


@pytest.mark.parametrize("route", ["handle_generate_cfn_template", "handle_export_python"])
def test_the_presigned_url_is_only_ever_returned(sources, route):
    text = sources["deployment_handler.py"]
    assert staged_url_names(_function(text, route)) == {"url"}  # reach: the checker found the sink
    assert url_leaks(text, route) == []


_URL_ANCHOR = 'url = _stage_export_bundle(\n                owner_hash, "python-export", deployment_name, zip_bytes, f"{deployment_name}-python.zip"\n            )\n'
_URL_RETURN = 'return {"download_url": url, "filename": f"{deployment_name}-python.zip"}\n'


def _mutate_export_route(text: str, *, rename: str | None = None, extra: str = "", ret: str | None = None) -> str:
    """Rewrite the one stager assignment (and optionally its return) in handle_export_python."""
    assert text.count(_URL_ANCHOR) == 1 and text.count(_URL_RETURN) == 1
    indent = text.split(_URL_ANCHOR)[0].rsplit("\n", 1)[1]
    assign = _URL_ANCHOR.replace("url =", f"{rename} =") if rename else _URL_ANCHOR
    text = text.replace(_URL_ANCHOR, assign + (indent + extra + "\n" if extra else ""))
    if ret is not None:
        text = text.replace(_URL_RETURN, ret + "\n")
    return text


_URL_LEAKS = {
    "logged": dict(extra='logger.info("staged %s", url)'),
    "renamed, recorded, returned bare": dict(
        rename="staged_url", extra="_record_secret_url(staged_url)", ret="return staged_url"
    ),
    "copied to a second name": dict(extra="keep = url"),
    "interpolated": dict(extra='note = f"see {url}"'),
    "stored": dict(extra='state["download_url"] = url'),
    "stager result passed straight to a helper": dict(
        rename="_", extra="", ret='return _wrap(_stage_export_bundle(owner_hash, "python-export", deployment_name, zip_bytes))'
    ),
    "reached through locals()": dict(extra='_record(locals()["url"])'),
    "reached through vars()": dict(extra='_record(vars()["url"])'),
    "reached through builtins.locals()": dict(extra='_record(builtins.locals()["url"])'),
    "reached through an alias of vars": dict(extra='v = vars; _record(v()["url"])'),
    "returned twice": dict(ret='return {"download_url": url, "debug_url": url}'),
    "returned under another key": dict(ret='return {"url": url}'),
    "returned bare": dict(ret="return url"),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(_URL_LEAKS))
def test_positive_control_a_leaked_url_is_caught(sources, case):
    mutated = _mutate_export_route(sources["deployment_handler.py"], **_URL_LEAKS[case])
    assert url_leaks(mutated, "handle_export_python"), case


# ------------------------------------------------------------------ S3 call inventory


_TRANSFER_HELPERS = frozenset(
    {
        "upload_file",
        "upload_fileobj",
        "download_file",
        "download_fileobj",
        "copy",
        "generate_presigned_url",
        "generate_presigned_post",
    }
)


def _s3_names() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """EVERY S3 operation, paginator and waiter, from boto3's OWN model.

    Not a hand list and not only reads: a put_object or put_object_tagging with a
    caller's key can overwrite a bundle, or forge the DeploymentId tag the generic
    delete trusts. A boto3 upgrade that adds an operation adds it here. Returns
    (client methods including the transfer helpers, paginator names, waiter names).
    """
    import boto3
    from botocore import xform_name
    from botocore.loaders import Loader

    model = boto3.client("s3", region_name="us-east-1").meta.service_model
    ops = {xform_name(o) for o in model.operation_names} | _TRANSFER_HELPERS
    loader = Loader()
    paginators = {xform_name(k) for k in loader.load_service_model("s3", "paginators-1")["pagination"]}
    waiters = {xform_name(k) for k in loader.load_service_model("s3", "waiters-2")["waiters"]}
    return frozenset(ops), frozenset(paginators), frozenset(waiters)


_S3_OPS, _S3_PAGINATORS, _S3_WAITERS = _s3_names()
_FACTORIES = {"get_paginator": _S3_PAGINATORS, "get_waiter": _S3_WAITERS}

_DEPS = "constant agentcore-deps/ bundle key"
_CUSTOMER = "runs inside the CUSTOMER's exported stack on its own template's keys; no platform-API path"
_TEARDOWN = "key from a server-written manifest row; feeds only the DeploymentId-gated delete"
_BUCKET = "bucket-level call on an operator-registered bucket; addresses no object"
_WRITE = "writes under deployments/ or connector-specs/ with a server-built key; never an export prefix"
_KB = "reads the BUCKET's tags to authorize a caller-supplied KB source; addresses no object"
_INTERNAL = "inside the DeploymentId-gated delete, on the key it was given"

# One row per CALL SITE: (module, innermost function, op, the call's own arguments) -> (count, why).
# The arguments are part of the key, so a new call inside an already-reviewed function is a
# new row; the count catches an exact duplicate. Reviewed 2026-09-23.
S3_CALL_INVENTORY = {
    ('deployment_handler.py', '_stage_export_bundle', 'generate_presigned_url',
     "'get_object', Params={'Bucket': bucket, 'Key': key, 'ResponseContentDisposition': "
     "f'attachment; filename=\"{download_name}\"', 'ResponseContentType': 'application/zip'}, ExpiresIn=3600"):
        (1, "the stager: presigns a read of the key it just wrote for this caller; the response disposition names the "
            "download after the reported filename (a browser saves a cross-origin download under the object's name)"),
    ('deployment_handler.py', '_stage_export_bundle', 'put_object',
     'Bucket=bucket, Key=key, Body=body, Tagging=urllib.parse.urlencode(tags)'):
        (1, "the stager: writes under the CALLER's own hash prefix with a fresh 128-bit suffix"),
    ('services/cfn_provider/handler.py', '_delete_every_version', 'delete_object',
     'Bucket=bucket, Key=key, VersionId=version_id, **owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', '_handle_code_package_create_update', 'get_object',
     'Bucket=bucket, Key=agent_code_key, **owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', '_handle_code_package_create_update', 'get_object',
     'Bucket=bucket, Key=bundle_key, **owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', '_handle_code_package_create_update', 'put_object',
     "Bucket=bucket, Key=output_key, Body=merged, "
     "**{'Tagging': urlencode(sorted(resource_tags.items()))} if resource_tags else {}, **owner"):
        (1, _CUSTOMER + "; the tags are the export's own ResourceTags written onto the customer's code.zip (P0-A)"),
    ('services/cfn_provider/handler.py', '_iter_versions', 'list_object_versions',
     'Bucket=bucket, Prefix=key, **owner, **token'):
        (1, _CUSTOMER),
    ('services/deploy_target.py', 'validate_artifact_bucket', 'get_bucket_location',
     'Bucket=resolved, ExpectedBucketOwner=account_id'):
        (1, _BUCKET),
    ('services/deploy_target.py', 'validate_artifact_bucket', 'head_bucket',
     'Bucket=resolved, ExpectedBucketOwner=account_id'):
        (1, _BUCKET),
    ('services/deployment.py', 'deploy', 'get_object',
     "Bucket=bucket, Key='agentcore-deps/strands-mcp.zip'"):
        (1, _DEPS),
    ('services/deployment.py', 'deploy', 'get_object',
     'Bucket=bucket, Key=bundle_key'):
        (1, _DEPS + " (strands-mcp.zip or base.zip, chosen two lines up)"),
    ('services/gateway_deployer.py', '_build_openapi_schema', 'put_object',
     '**put_kwargs'):
        (1, _WRITE + "; key is connector-specs/<safe id>/<uuid12>.json, built in this function"),
    ('services/resource_ownership.py', '_exact_key_versions', 'list_object_versions',
     'Bucket=bucket, Prefix=key, **owner, **token'):
        (1, _TEARDOWN),
    ('services/resource_ownership.py', 'assert_s3_object_owned.<lambda>', 'get_object_tagging',
     '**request'):
        (1, _TEARDOWN),
    ('services/resource_ownership.py', 'delete_owned_s3_object', 'delete_object',
     'Bucket=bucket, Key=key, VersionId=version_id, **owner'):
        (1, _TEARDOWN),
    ('services/runtime_deployer.py', 'upload_code_to_s3', 'put_object',
     '**put_kwargs'):
        (1, _WRITE + "; the key comes from the caller, and every caller is in S3_HELPER_CALLER_INVENTORY"),
    ('step_handlers/codegen_step.py', '_download_bundle', 'get_object',
     'Bucket=bucket, Key=bundle_key'):
        (1, _DEPS + "; every caller passes a *_BUNDLE_KEY constant or provider key"),
    ('step_handlers/knowledge_base_step.py', '_authorize_s3_uri.<lambda>', 'get_bucket_tagging',
     'Bucket=bucket'):
        (1, _KB),
    ('step_handlers/mcp_server_step.py', 'handler', 'get_object',
     'Bucket=platform_bucket, Key=_lean_key'):
        (1, _DEPS + "; _lean_key is the fixed agentcore-deps/mcp-lean.zip literal"),
}  # fmt: skip


def _call_args(call: ast.Call) -> str:
    return ", ".join(
        [ast.unparse(a) for a in call.args]
        + [f"{k.arg}={ast.unparse(k.value)}" if k.arg else f"**{ast.unparse(k.value)}" for k in call.keywords]
    )


def _walk_scoped(tree: ast.AST):
    """Yield ``(node, innermost function, parent)``; a lambda keeps its enclosing name."""
    stack = [(tree, "<module>", None)]
    while stack:
        node, fn, parent = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        elif isinstance(node, ast.Lambda):
            fn = f"{fn}.<lambda>"
        yield node, fn, parent
        stack.extend((child, fn, node) for child in ast.iter_child_nodes(node))


def _factory_target(call: ast.Call):
    """The name a get_paginator/get_waiter call builds, positional OR ``operation_name=``/``waiter_name=``."""
    if call.args:
        return call.args[0]
    return next((k.value for k in call.keywords if k.arg in ("operation_name", "waiter_name")), None)


def s3_call_sites(sources: dict[str, str]) -> Counter:
    """Every S3 operation, transfer helper, presign, paginator or waiter call, keyed per call site."""
    sites: Counter = Counter()
    for rel, text in sources.items():
        for node, fn, _parent in _walk_scoped(ast.parse(text)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            if attr == "copy" and not (node.args or node.keywords):
                continue  # dict.copy()/list.copy(); the S3 managed copy always takes arguments
            if attr in _S3_OPS:
                sites[(rel, fn, attr, _call_args(node))] += 1
            elif attr in _FACTORIES:
                target = _factory_target(node)
                # Only a constant naming another service's paginator is provably not S3.
                if not (isinstance(target, ast.Constant) and target.value not in _FACTORIES[attr]):
                    sites[(rel, fn, attr, _call_args(node))] += 1
    return sites


def unreviewed_s3_calls(sources: dict[str, str]) -> dict:
    found = s3_call_sites(sources)
    expected = Counter({k: n for k, (n, _why) in S3_CALL_INVENTORY.items()})
    return {k: (found[k], expected[k]) for k in found.keys() | expected.keys() if found[k] != expected[k]}


# --- dispatch the name check cannot see: each shape is either refused or on a reviewed list.

# ``getattr(obj, <not a constant>)``: the attribute is chosen at run time, so the operation-name
# check above cannot see it. Keyed (module, function, the getattr call). Reviewed 2026-09-23.
_ATTR_TABLE = "the attribute name comes from a constant table of non-S3 operations in this module"
_RECORD_FIELD = "reads a field of a record or config object, not a client"
_EXC_ATTR = "reads a module-constant attribute off an exception, not a client"
DYNAMIC_ATTR_INVENTORY = {
    ('routers/git_sync.py', 'git_sync_workflow', 'getattr(workflow, field)'):
        (1, _RECORD_FIELD),
    ('services/agent_versions_store.py', 'to_item', 'getattr(self, fld)'):
        (2, _RECORD_FIELD),
    ('services/aws_pagination.py', 'list_all', 'getattr(client, operation)'):
        (1, "the pagination helper itself; the op it dispatches is checked at every list_all call site"),
    ('services/cfn_template_generator.py', '_reject_what_the_export_cannot_express', 'getattr(request, name, None)'):
        (1, _RECORD_FIELD),
    ('services/deployment_state_store.py', 'gateway_targets_deleted', 'getattr(exc, _TARGETS_DELETED_ATTR, None)'):
        (1, _EXC_ATTR),
    ('services/gateway_deployer.py', '_create_cognito_oauth', "getattr(e, SECRET_CANDIDATE_ATTR, '')"):
        (1, _EXC_ATTR),
    ('services/gateway_deployer.py', '_create_cognito_oauth_in_shared_pool', "getattr(e, SECRET_CANDIDATE_ATTR, '')"):
        (1, _EXC_ATTR),
    ('services/gateway_deployer.py', '_delete_connector_credential_provider', 'getattr(agentcore_ctrl, delete_method)'):
        (1, _ATTR_TABLE),
    ('services/gateway_deployer.py', 'deploy_gateway', 'getattr(e, COGNITO_LEFTOVER_ATTR, None)'):
        (1, _EXC_ATTR),
    ('services/hitl_store.py', 'to_item', 'getattr(self, fld)'):
        (1, _RECORD_FIELD),
    ('services/resource_ownership.py', 'assert_agentcore_resource_owned.<lambda>', 'getattr(client, method_name)'):
        (1, _ATTR_TABLE),
    ('services/resource_ownership.py', 'assert_aoss_policy_owned.<lambda>', 'getattr(client, method_name)'):
        (1, _ATTR_TABLE),
    ('services/resource_ownership.py', 'delete_owned_credential_provider', 'getattr(client, delete_method)'):
        (1, _ATTR_TABLE),
    ('services/runtime_deployer.py', '_cfg_get', 'getattr(config, name, None)'):
        (1, _RECORD_FIELD),
    ('services/runtime_target_context.py', '_field', 'getattr(record, name, None)'):
        (1, _RECORD_FIELD),
    ('services/trigger_runtime.py', '_field', 'getattr(record, name, None)'):
        (1, _RECORD_FIELD),
    ('services/trigger_store.py', 'to_item', 'getattr(self, fld)'):
        (1, _RECORD_FIELD),
    ('services/validation.py', '_validate_required_field', 'getattr(config, field, None)'):
        (1, _RECORD_FIELD),
}  # fmt: skip

# ``list_all(client, <op>, ...)`` calls ``getattr(client, op)`` itself (aws_pagination). A constant
# op is checked against the S3 names; a non-constant one must be reviewed here.
DYNAMIC_LIST_ALL_INVENTORY = {
    ('services/resource_ownership.py', '_iam_pages', 'method_name'):
        (1, "IAM role policy listings on an IAM client (list_attached_role_policies / list_role_policies)"),
}  # fmt: skip

# A string constant EQUAL to an S3 operation, paginator or waiter name is how a table-driven
# getattr would reach S3. Keyed (module, function, value). Reviewed 2026-09-23.
S3_NAME_LITERAL_INVENTORY = {
    ('deployment_handler.py', '_stage_export_bundle', 'get_object'):
        (1, "the presign's ClientMethod argument, reviewed above as a call site"),
    ('services/connectors_catalog.py', '<module>', 'download_file'):
        (1, "a connector tool NAME in the catalog; never dispatched"),
    ('services/mcp_catalog.py', '<module>', 'upload_file'):
        (1, "an example tool NAME in the Box connector catalog entry; never dispatched"),
}  # fmt: skip

# Every REFERENCE to a reflection name (``_REFLECTION``) in the backend. Keyed (module, function,
# the reference as written). The only production use is gateway_deployer's abort path reading
# its OWN ``locals()`` to learn which resources exist; nothing dispatches through it.
_OWN_LOCALS = "reads the enclosing function's own locals in the abort path; no attribute or client is chosen by it"
_VARS_AS_DATA = "serialises a deployment object to its __dict__ as plain data for the memory identity; no attribute or client is chosen by it"
REFLECTION_INVENTORY = {
    # 18, not 17, since F-74c: recording a custom tool's per-resource pairing added
    # ``locals().get("custom_tool_pairs")`` to the same partial-result dict. 19 since the
    # abort inventory carries ``locals().get("kb_lambda_name")``, so a Knowledge Base tool
    # function created before a later failure reaches the manifest. The count is
    # reviewed, but it is only a count -- the SHAPE of every one of these reads is enforced
    # separately in ``s3_dispatch_findings``, so bumping this number cannot admit a read that
    # takes its variable name from data.
    ("services/gateway_deployer.py", "deploy_gateway", "locals"): (19, _OWN_LOCALS),
    ("services/trigger_runtime.py", "invoke_trigger", "vars"): (1, _VARS_AS_DATA),
}  # fmt: skip

# The one resource() call that is not a constant "dynamodb": step_clients.resource forwards its
# own ``service`` argument, and every CALLER of it is held to the constant rule below.
_RESOURCE_WRAPPERS = {("services/step_clients.py", "resource")}


def _named(func: ast.AST) -> str | None:
    """The name a call is made through: ``f(...)`` -> f, ``m.f(...)`` -> f, anything else -> None."""
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _resource_is_dynamodb(call: ast.Call) -> bool:
    values = list(call.args) + [k.value for k in call.keywords if k.arg in ("service_name", "service")]
    strings = [v.value for v in values if isinstance(v, ast.Constant) and isinstance(v.value, str)]
    return "dynamodb" in strings and "s3" not in strings


def s3_dispatch_findings(sources: dict[str, str]) -> dict:
    """Every way to reach an S3 operation without naming it at a call site.

    Returns {kind: Counter or list}: refusals are lists of locations; reviewed shapes are
    the diff against their inventory (found, expected), both directions.
    """
    s3_names = _S3_OPS | _S3_PAGINATORS | _S3_WAITERS
    refused: list[str] = []
    attrs: Counter = Counter()
    list_alls: Counter = Counter()
    literals: Counter = Counter()
    reflection: Counter = Counter()
    for rel, text in sources.items():
        tree = ast.parse(text)
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        # A ``locals()`` read is reviewed ONLY in the shape ``locals().get("<literal>")``, which
        # names at the call site the one variable it wants. ``locals()[k]``, ``.get(k)``, or a
        # bare ``locals()`` handed to something else are each a dispatch surface where the name
        # can come from data. REFLECTION_INVENTORY counts reads, not shapes, so a count bumped
        # for a legitimate new read would otherwise also admit one of those.
        literal_locals = {
            id(call.func.value.func)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and isinstance(call.func.value, ast.Call)
            and isinstance(call.func.value.func, ast.Name)
            and call.func.value.func.id == "locals"
            and not call.func.value.args
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        }
        for node, fn, _parent in _walk_scoped(tree):
            where = f"{rel}:{fn}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Attribute):
                if node.attr == "_make_api_call":
                    refused.append(f"{where} _make_api_call")
                # A bound alias (``f = s3.get_object``) is a call the name check never sees.
                elif node.attr in (s3_names | set(_FACTORIES)) - {"copy"} and id(node) not in called:
                    refused.append(f"{where} bound alias {ast.unparse(node)}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "resource":
                    if (rel, fn) not in _RESOURCE_WRAPPERS and not _resource_is_dynamodb(node):
                        refused.append(f"{where} resource({_call_args(node)})")
                elif isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
                    if not isinstance(node.args[1], ast.Constant):
                        attrs[(rel, fn, ast.unparse(node))] += 1
                    elif node.args[1].value in s3_names:
                        refused.append(f"{where} getattr of S3 {node.args[1].value}")
                elif _named(func) == "list_all" and len(node.args) >= 2:
                    # Direct or qualified (aws_pagination.list_all); the storage .list_all() takes no args.
                    op = node.args[1]
                    if not isinstance(op, ast.Constant):
                        list_alls[(rel, fn, ast.unparse(op))] += 1
                    elif op.value in s3_names:
                        refused.append(f"{where} list_all over S3 {op.value}")
            elif isinstance(node, ast.ImportFrom):
                # ``from aws_pagination import list_all as la`` would hide every call above.
                for alias in node.names:
                    if alias.name == "list_all" and alias.asname:
                        refused.append(f"{where} import list_all as {alias.asname}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in s3_names:
                    literals[(rel, fn, node.value)] += 1
                elif node.value == "list_all":
                    refused.append(f"{where} literal 'list_all'")  # getattr/vars() reach to the paginator
            if isinstance(node, (ast.Name, ast.Attribute)):
                # Any reference to list_all that is not the call target above: an alias, a default
                # argument, a functools.partial, a callback. One rule for every spelling.
                if _named(node) == "list_all" and id(node) not in called:
                    refused.append(f"{where} reference to list_all: {ast.unparse(_parent)[:60]}")
                elif _named(node) in _REFLECTION:
                    reflection[(rel, fn, ast.unparse(node))] += 1
                    if _named(node) == "locals" and id(node) not in literal_locals:
                        refused.append(f'{where} locals() not read as locals().get("<literal>")')

    def diff(found: Counter, inventory: dict) -> dict:
        expected = Counter({k: n for k, (n, _why) in inventory.items()})
        return {k: (found[k], expected[k]) for k in found.keys() | expected.keys() if found[k] != expected[k]}

    return {
        "refused": sorted(refused),
        "getattr": diff(attrs, DYNAMIC_ATTR_INVENTORY),
        "list_all": diff(list_alls, DYNAMIC_LIST_ALL_INVENTORY),
        "literal": diff(literals, S3_NAME_LITERAL_INVENTORY),
        "reflection": diff(reflection, REFLECTION_INVENTORY),
    }


_CLEAN_DISPATCH = {"refused": [], "getattr": {}, "list_all": {}, "literal": {}, "reflection": {}}


# --- callers: a helper that hosts an S3 call takes its bucket/key from its CALLER, so the call
# site above is only reviewed if every call of the helper is too.

S3_HELPERS = {
    "_download_bundle": "step_handlers/codegen_step.py",
    "upload_code_to_s3": "services/runtime_deployer.py",
    "delete_owned_s3_object": "services/resource_ownership.py",
    "assert_s3_object_owned": "services/resource_ownership.py",
    "_exact_key_versions": "services/resource_ownership.py",
    "validate_artifact_bucket": "services/deploy_target.py",
    "_authorize_s3_uri": "step_handlers/knowledge_base_step.py",
    "_build_openapi_schema": "services/gateway_deployer.py",
    "_handle_code_package_create_update": "services/cfn_provider/handler.py",
    "_iter_versions": "services/cfn_provider/handler.py",
    "_delete_every_version": "services/cfn_provider/handler.py",
}
# Keyed (module, calling function, callee as written, the call's arguments). Reviewed 2026-09-23.
S3_HELPER_CALLER_INVENTORY = {
    ('deployment_handler.py', '_delete_managed_resource', 'delete_owned_s3_object',
     's3, _b, _k, region=res_region, deployment_id=deployment_id, expected_bucket_owner=str(res_account) if res_account else None'):
        (1, _TEARDOWN),
    ('routers/admin.py', 'add_account_target', 'dt.validate_artifact_bucket',
     'session, account_id=body.account_id, region=body.region, artifact_bucket=body.artifact_bucket'):
        (1, _BUCKET),
    ('routers/admin.py', 'add_region_target', 'dt.validate_artifact_bucket',
     'session, account_id=account_id, region=body.region, artifact_bucket=body.artifact_bucket, same_account=True'):
        (1, _BUCKET),
    ('services/cfn_provider/handler.py', '_count_versions', '_iter_versions',
     's3, bucket, key, owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', '_delete_every_version', '_iter_versions',
     's3, bucket, key, owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', '_handle_code_package_delete', '_delete_every_version',
     's3, bucket, output_key, owner'):
        (1, _CUSTOMER),
    ('services/cfn_provider/handler.py', 'handler', '_handle_code_package_create_update',
     'event'):
        (1, _CUSTOMER),
    ('services/deploy_target.py', 'resolve_registered_account_target', 'validate_artifact_bucket',
     "session, account_id=account_id, region=resolved_region, artifact_bucket=target.get('artifact_bucket')"):
        (1, _BUCKET),
    ('services/deploy_target.py', 'resolve_registered_region_target', 'validate_artifact_bucket',
     'session, account_id=account_id, region=resolved_region, artifact_bucket=artifact_bucket, same_account=True'):
        (1, _BUCKET),
    ('services/deployment.py', 'deploy', 'upload_code_to_s3',
     "s3_client, bucket, mcp_s3_key, mcp_code, '', 'agent.py', deps_bundle=deps_bundle, region=self.region, deployment_id=deployment_id"):
        (1, _WRITE + "; key from scoped_mcp_code_s3_key"),
    ('services/deployment.py', 'deploy', 'upload_code_to_s3',
     "s3_client, bucket, s3_key, agent_code, requirements_txt, 'agent.py', deps_bundle=deps_bundle, extra_bundles=extra_bundles, region=self.region, deployment_id=deployment_id"):
        (1, _WRITE + "; key is deployments/by-name/<sanitized runtime name>/...code.zip"),
    ('services/gateway_deployer.py', '_delete_spec_s3_object', 'delete_owned_s3_object',
     's3, bucket, key, region=region, deployment_id=deployment_id, expected_bucket_owner=expected_owner'):
        (1, _TEARDOWN),
    ('services/gateway_deployer.py', '_deploy_config_targets', '_build_openapi_schema',
     'spec_inline, connector_id=base_name, region=region, deployment_id=deployment_id'):
        (1, _WRITE + "; the helper builds its own key"),
    ('services/gateway_deployer.py', '_deploy_connector_targets_inner', '_build_openapi_schema',
     "spec_inline, connector_id=connector_id or 'generic', region=region, deployment_id=deployment_id"):
        (1, _WRITE + "; the helper builds its own key"),
    ('services/resource_ownership.py', 'delete_owned_s3_object', 'assert_s3_object_owned',
     'client, bucket, key, region=region, deployment_id=deployment_id, expected_bucket_owner=expected_bucket_owner, version_id=version_id'):
        (1, _INTERNAL),
    ('services/resource_ownership.py', 'delete_owned_s3_object.<lambda>', '_exact_key_versions',
     'client, bucket, key, owner'):
        (2, _INTERNAL),
    ('step_handlers/codegen_step.py', '_provider_bundles', '_download_bundle',
     'deps_s3, platform_bucket, key'):
        (1, _DEPS + "; key from provider_bundle_keys_for()"),
    ('step_handlers/codegen_step.py', 'handler', '_download_bundle',
     'deps_s3, platform_bucket, bundle_key'):
        (1, _DEPS + "; key is a *_BUNDLE_KEY constant chosen above"),
    ('step_handlers/codegen_step.py', 'handler', 'upload_code_to_s3',
     "upload_s3, upload_bucket, s3_key, agent_code, '', entrypoint, deps_bundle=deps_bundle, extra_bundles=extra_bundles, expected_bucket_owner=str(_target_account) if _target_account else None, region=region, deployment_id=deployment_id"):
        (1, _WRITE + "; key is deployments/by-name/<sanitized runtime name>/...code.zip"),
    ('step_handlers/knowledge_base_step.py', '_authorize_create_new_resources', '_authorize_s3_uri',
     "event, str(kb_config.get('bdaSupplementalS3Uri') or ''), label='Bedrock Data Automation supplemental S3 bucket', owner_sub=owner_sub, deployment_id=deployment_id, region=region, trusted_buckets=trusted_s3_buckets"):
        (1, _KB),
    ('step_handlers/knowledge_base_step.py', '_authorize_create_new_resources', '_authorize_s3_uri',
     "event, str(kb_config.get('s3BucketUri') or ''), label='Knowledge Base S3 data source', owner_sub=owner_sub, deployment_id=deployment_id, region=region, trusted_buckets=trusted_s3_buckets"):
        (1, _KB),
    ('step_handlers/knowledge_base_step.py', '_authorize_create_new_resources', '_authorize_s3_uri',
     "event, transform_s3, label='Knowledge Base transformation S3 bucket', owner_sub=owner_sub, deployment_id=deployment_id, region=region, trusted_buckets=trusted_s3_buckets"):
        (1, _KB),
    ('step_handlers/mcp_server_step.py', 'handler', 'upload_code_to_s3',
     "upload_s3, bucket, mcp_s3_key, mcp_code, '', 'agent.py', deps_bundle=deps_bundle, expected_bucket_owner=str(event['target_account_id']) if event.get('target_account_id') else None, region=region, deployment_id=deployment_id"):
        (1, _WRITE + "; key from scoped_mcp_code_s3_key"),
    ('step_handlers/status_update_step.py', '_cleanup_resource.<lambda>', 'delete_owned_s3_object',
     "s3, _b, _k, region=res_region, deployment_id=event.get('deployment_id'), expected_bucket_owner=str(expected_owner) if expected_owner else None"):
        (1, _TEARDOWN),
}  # fmt: skip


def s3_helper_findings(sources: dict[str, str]) -> dict:
    """Callers of every S3 helper against the inventory, plus every way to reach one that is
    not a direct call: a second definition of the name, an import from another module, or a
    reference that is not a call (``dl = _download_bundle``)."""
    callers: Counter = Counter()
    refused: list[str] = []
    for rel, text in sources.items():
        tree = ast.parse(text)
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node, fn, parent in _walk_scoped(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in S3_HELPERS:
                if S3_HELPERS[node.name] != rel:
                    refused.append(f"{rel}:{node.lineno} second definition of {node.name}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in S3_HELPERS:
                        home = "app." + S3_HELPERS[alias.name][: -len(".py")].replace("/", ".")
                        if node.module != home or alias.asname:
                            refused.append(f"{rel}:{node.lineno} import {alias.name} from {node.module}")
            elif isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name not in S3_HELPERS:
                    continue
                if id(node) in called:
                    callers[(rel, fn, ast.unparse(node), _call_args(parent))] += 1
                else:
                    refused.append(f"{rel}:{fn}:{node.lineno} non-call reference {ast.unparse(node)}")
    expected = Counter({k: n for k, (n, _why) in S3_HELPER_CALLER_INVENTORY.items()})
    return {
        "refused": sorted(refused),
        "callers": {
            k: (callers[k], expected[k]) for k in callers.keys() | expected.keys() if callers[k] != expected[k]
        },
    }


_CLEAN_HELPERS = {"refused": [], "callers": {}}


def _delta(check, sources: dict[str, str], rel: str, text: str) -> dict:
    """Only what an injection adds to *check*'s findings, so a control does not depend on
    the tree being clean. Works for a plain diff dict or a {kind: list|dict} result."""
    before, after = check(sources), check({**sources, rel: text})
    if not isinstance(after, dict) or not all(isinstance(v, (list, dict)) for v in after.values()):
        return {k: v for k, v in after.items() if before.get(k) != v}
    out = {}
    for kind, value in after.items():
        if isinstance(value, list):
            got = [v for v in value if v not in before[kind]]
        else:
            got = {k: v for k, v in value.items() if before[kind].get(k) != v}
        if got:
            out[kind] = got
    return out


def _locations(delta: dict) -> list[str]:
    """The module of every finding in a delta: the KEYS of a plain diff, and inside a
    {kind: list|dict} result the list entries or the nested keys. Never the values."""
    out = []
    for key, value in delta.items():
        if isinstance(key, tuple):
            out.append(key[0])
        elif isinstance(value, list):
            out.extend(value)
        else:
            out.extend(k[0] if isinstance(k, tuple) else k for k in value)
    return out


def test_the_s3_name_sets_are_boto3s_own(sources):
    # Reach: the model-derived sets hold the operations a hand list most easily forgets.
    assert {
        "get_object", "head_object", "get_object_tagging", "list_objects_v2", "delete_objects",
        "put_object", "put_object_tagging", "put_object_acl", "create_multipart_upload", "upload_part",
    } <= _S3_OPS  # fmt: skip
    assert {"list_objects_v2", "list_object_versions"} <= _S3_PAGINATORS
    assert {"object_exists", "bucket_exists"} <= _S3_WAITERS


def test_every_s3_call_site_is_reviewed(sources):
    # Both directions: an unreviewed site, an extra copy of a reviewed one, or a stale row.
    assert unreviewed_s3_calls(sources) == {}


def test_no_s3_operation_is_reached_by_dynamic_dispatch(sources):
    assert s3_dispatch_findings(sources) == _CLEAN_DISPATCH


def test_every_caller_of_an_s3_helper_is_reviewed(sources):
    for name, rel in S3_HELPERS.items():  # reach: each helper is where the table says
        assert any(n.name == name for n in ast.walk(ast.parse(sources[rel])) if isinstance(n, ast.FunctionDef)), name
    assert s3_helper_findings(sources) == _CLEAN_HELPERS


# Each is injected as a NEW module; the checker that must catch it is named with it.
_ROGUE_CALLS = {
    "new route": (unreviewed_s3_calls, "async def get_file(key, s3, b):\n    return s3.get_object(Bucket=b, Key=key)\n"),
    "presign": (unreviewed_s3_calls, "def u(s3, b, k):\n    return s3.generate_presigned_url('get_object', Params={'Key': k})\n"),
    "head": (unreviewed_s3_calls, "def e(s3, b, k):\n    return s3.head_object(Bucket=b, Key=k)\n"),
    "tagging": (unreviewed_s3_calls, "def t(s3, b, k):\n    return s3.get_object_tagging(Bucket=b, Key=k)\n"),
    "put with a caller key": (unreviewed_s3_calls, "def w(s3, b, req):\n    s3.put_object(Bucket=b, Key=req.query_params['key'], Body=b'')\n"),
    "forged tagging": (unreviewed_s3_calls, "def f(s3, b, k):\n    s3.put_object_tagging(Bucket=b, Key=k, Tagging={'TagSet': []})\n"),
    "managed copy": (unreviewed_s3_calls, "def c(s3, b, k):\n    s3.copy({'Bucket': b, 'Key': k}, b, 'x')\n"),
    "paginator v2": (unreviewed_s3_calls, "def p(s3):\n    return s3.get_paginator('list_objects_v2')\n"),
    "paginator versions": (unreviewed_s3_calls, "def p(s3):\n    return s3.get_paginator('list_object_versions')\n"),
    "paginator by variable": (unreviewed_s3_calls, "def p(s3, op):\n    return s3.get_paginator(op)\n"),
    "paginator by keyword": (unreviewed_s3_calls, "def p(s3):\n    return s3.get_paginator(operation_name='list_objects_v2')\n"),
    "waiter": (unreviewed_s3_calls, "def x(s3, b, k):\n    s3.get_waiter('object_exists').wait(Bucket=b, Key=k)\n"),
    "batch delete": (unreviewed_s3_calls, "def d(s3, b, ks):\n    return s3.delete_objects(Bucket=b, Delete=ks)\n"),
    "resource by keyword": (s3_dispatch_findings, "import boto3\ndef r(b, k):\n    return boto3.resource(service_name='s3').Object(b, k).get()\n"),
    "resource by variable": (s3_dispatch_findings, "import boto3\ndef r(svc):\n    return boto3.resource(svc)\n"),
    "resource through the wrapper": (s3_dispatch_findings, "from app.services import step_clients\ndef r(ev):\n    return step_clients.resource(ev, 's3')\n"),
    "locals by a variable key": (s3_dispatch_findings, "def a(k):\n    return locals().get(k)\n"),
    "locals by subscript": (s3_dispatch_findings, "def a(k):\n    return locals()[k]\n"),
    "bare locals handed on": (s3_dispatch_findings, "def a(f):\n    return f(locals())\n"),
    "getattr dispatch": (s3_dispatch_findings, "def g(s3, op, kw):\n    return getattr(s3, op)(**kw)\n"),
    "getattr by constant": (s3_dispatch_findings, "def g(s3, kw):\n    return getattr(s3, 'get_object')(**kw)\n"),
    "bound alias": (s3_dispatch_findings, "def a(s3, kw):\n    f = s3.get_object\n    return f(**kw)\n"),
    "list_all over S3": (s3_dispatch_findings, "def l(s3):\n    return list_all(s3, 'list_objects_v2', item_keys=('Contents',), request={})\n"),
    "list_all qualified, op from the request": (s3_dispatch_findings, "from app.services import aws_pagination\ndef l(s3, req):\n    return aws_pagination.list_all(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all aliased import": (s3_dispatch_findings, "from app.services.aws_pagination import list_all as la\ndef l(s3, req):\n    return la(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all bound alias": (s3_dispatch_findings, "from app.services.aws_pagination import list_all\nla = list_all\ndef l(s3, req):\n    return la(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all by getattr literal": (s3_dispatch_findings, "from app.services import aws_pagination\ndef l(s3, req):\n    return getattr(aws_pagination, 'list_all')(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all qualified bound alias": (s3_dispatch_findings, "from app.services import aws_pagination\nla = aws_pagination.list_all\ndef l(s3, req):\n    return la(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all as a default argument": (s3_dispatch_findings, "from app.services import aws_pagination\ndef l(s3, req, la=aws_pagination.list_all):\n    return la(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all through vars() and a concatenated name": (s3_dispatch_findings, "from app.services import aws_pagination\ndef l(s3, req):\n    opfn = vars(aws_pagination)['list_' + 'all']\n    return opfn(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all through __dict__ and a request-chosen name": (s3_dispatch_findings, "from app.services import aws_pagination\ndef l(s3, req):\n    opfn = aws_pagination.__dict__[req.query_params['helper']]\n    return opfn(s3, req.query_params['op'], item_keys=('Contents',), request={})\n"),
    "list_all through functools.partial": (s3_dispatch_findings, "import functools\nfrom app.services import aws_pagination\ndef l(s3, req):\n    opfn = functools.partial(aws_pagination.list_all, s3, req.query_params['op'])\n    return opfn(item_keys=('Contents',), request={})\n"),
    "getattr with a concatenated S3 name": (s3_dispatch_findings, "def g(s3, kw):\n    return getattr(s3, 'get_' + 'object')(**kw)\n"),
    "make_api_call": (s3_dispatch_findings, "def m(s3, kw):\n    return s3._make_api_call('GetObject', kw)\n"),
    "__getattribute__": (s3_dispatch_findings, "def g(s3, op, kw):\n    return s3.__getattribute__(op)(**kw)\n"),
    "attrgetter": (s3_dispatch_findings, "import operator\ndef g(s3, op, kw):\n    return operator.attrgetter(op)(s3)(**kw)\n"),
    "methodcaller": (s3_dispatch_findings, "from operator import methodcaller\ndef g(s3, op, kw):\n    return methodcaller(op, **kw)(s3)\n"),
    "helper caller": (s3_helper_findings, "from app.step_handlers.codegen_step import _download_bundle\ndef h(s3, b, req):\n    return _download_bundle(s3, b, req.query_params['key'])\n"),
    "helper alias": (s3_helper_findings, "from app.step_handlers import codegen_step\ndl = codegen_step._download_bundle\n"),
    "helper re-import": (s3_helper_findings, "from app.step_handlers.codegen_step import _download_bundle as fetch\n"),
    "helper shadow": (s3_helper_findings, "def _download_bundle(s3, b, k):\n    return s3.get_object(Bucket=b, Key=k)['Body'].read()\n"),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(_ROGUE_CALLS))
def test_positive_control_an_unreviewed_s3_path_is_caught(sources, case):
    check, text = _ROGUE_CALLS[case]
    delta = _delta(check, sources, "routers/rogue.py", text)
    locations = _locations(delta)
    assert locations, f"{case}: {check.__name__} saw nothing"
    # Everything it reports is in the injected module: a control that passes on noise proves nothing.
    assert all(loc.startswith("routers/rogue.py") for loc in locations), delta


def test_positive_control_a_new_read_inside_a_reviewed_function_is_caught(sources):
    """The same (module, function, op) as a reviewed row, but a caller-controlled key."""
    text = sources["services/deployment.py"]
    anchor = "resp = s3_client.get_object(Bucket=bucket, Key=bundle_key)\n"
    assert text.count(anchor) == 1
    indent = text.split(anchor)[0].rsplit("\n", 1)[1]
    mutated = text.replace(
        anchor, anchor + indent + "leak = s3_client.get_object(Bucket=bucket, Key=runtime_config.name)\n"
    )
    diff = _delta(unreviewed_s3_calls, sources, "services/deployment.py", mutated)
    assert list(diff) == [
        ("services/deployment.py", "deploy", "get_object", "Bucket=bucket, Key=runtime_config.name")
    ], diff


def test_positive_control_a_duplicate_of_a_reviewed_call_is_caught(sources):
    text = sources["step_handlers/codegen_step.py"]
    anchor = "resp = s3_client.get_object(Bucket=bucket, Key=bundle_key)\n"
    assert text.count(anchor) == 1
    indent = text.split(anchor)[0].rsplit("\n", 1)[1]
    diff = _delta(
        unreviewed_s3_calls, sources, "step_handlers/codegen_step.py", text.replace(anchor, anchor + indent + anchor)
    )
    assert list(diff.values()) == [(2, 1)], diff


def stager_callers(sources: dict[str, str]) -> set[str]:
    """Every function that reaches the stager: a direct call, a qualified
    ``module._stage_export_bundle(...)`` call, a non-call reference (an alias), or an
    import of the name (aliased or not) anywhere in the tree."""
    out = set()
    for rel, text in sources.items():
        tree = ast.parse(text)
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node, fn, _parent in _walk_scoped(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == _STAGER for a in node.names):
                out.add(f"{rel}:{fn}:import")
            elif isinstance(node, (ast.Name, ast.Attribute)):
                if (node.id if isinstance(node, ast.Name) else node.attr) == _STAGER:
                    out.add(f"{rel}:{fn}" if id(node) in called else f"{rel}:{fn}:reference")
            elif isinstance(node, ast.Constant) and node.value == _STAGER:
                out.add(f"{rel}:{fn}:literal")  # getattr(module, "...") / vars(module)["..."]
    return out


def test_only_the_two_export_routes_call_the_stager(sources):
    assert stager_callers(sources) == {
        "deployment_handler.py:handle_generate_cfn_template",
        "deployment_handler.py:handle_export_python",
    }


@pytest.mark.parametrize(
    "rogue",
    [
        "async def harmless_preview(req):\n    return _stage_export_bundle('h', 'cfn-template', 'n', b'')\n",
        "from app import deployment_handler\nasync def harmless_preview(req):\n"
        "    return deployment_handler._stage_export_bundle('h', 'cfn-template', 'n', b'')\n",
        "from app.deployment_handler import _stage_export_bundle as stage\nasync def harmless_preview(req):\n"
        "    return stage('h', 'cfn-template', 'n', b'')\n",
        "from app import deployment_handler\nstage = deployment_handler._stage_export_bundle\n",
        "from app import deployment_handler\nasync def harmless_preview(req):\n"
        "    return getattr(deployment_handler, '_stage_export_bundle')('h', 'cfn-template', 'n', b'')\n",
        "from app import deployment_handler\nasync def harmless_preview(req):\n"
        "    return vars(deployment_handler)['_stage_export_bundle']('h', 'cfn-template', 'n', b'')\n",
    ],
    ids=["direct", "qualified", "aliased import", "module-level alias", "getattr literal", "vars literal"],
)
def test_positive_control_a_third_stager_caller_is_caught(sources, rogue):
    extra = stager_callers({**sources, "routers/preview.py": rogue}) - stager_callers(sources)
    assert extra and all(k.startswith("routers/preview.py:") for k in extra), extra


def audit_middleware_response_uses(text: str) -> list[str]:
    """How ``_audit_middleware`` touches ``response``. Allowed, exactly once each: the
    assignment from ``await call_next(request)``, ``getattr(response, "status_code", 0)``,
    and ``return response``. Any other use (a ``.json()``, a ``.body``, a helper call)
    could read the response body into the audit record."""
    fn = _function(text, "_audit_middleware")
    parents = {id(c): n for n in ast.walk(fn) for c in ast.iter_child_nodes(n)}
    uses = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Name) and n.id == "response"):
            continue
        parent = parents[id(n)]
        if isinstance(parent, ast.Assign) and ast.unparse(parent.value) == "await call_next(request)":
            uses.append("assigned from call_next")
        elif isinstance(parent, ast.Call) and ast.unparse(parent) == "getattr(response, 'status_code', 0)":
            uses.append("status_code read")
        elif isinstance(parent, ast.Return):
            uses.append("returned")
        else:
            uses.append(f"OTHER at line {n.lineno}: {ast.unparse(parent)[:80]}")
    return sorted(uses)


_ALLOWED_RESPONSE_USES = ["assigned from call_next", "returned", "status_code read"]


def test_the_audit_middleware_records_no_response_content(sources):
    text = sources["deployment_handler.py"]
    fn = _function(text, "_audit_middleware")
    events = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "AuditEvent"]
    assert len(events) == 1
    assert {k.arg for k in events[0].keywords} == {
        "org_id", "actor_sub", "action", "method", "path", "status_code", "session_uuid",
    }  # fmt: skip
    assert audit_middleware_response_uses(text) == _ALLOWED_RESPONSE_USES


@pytest.mark.parametrize(
    "extra",
    ["_b = await response.json()", "_b = response.body", "_b = response.render({})", "_record_body(response)"],
)
def test_positive_control_a_response_body_read_is_caught(sources, extra):
    text = sources["deployment_handler.py"]
    anchor = "    response = await call_next(request)\n"
    assert text.count(anchor) == 1
    mutated = text.replace(anchor, anchor + "    " + extra + "\n")
    uses = audit_middleware_response_uses(mutated)
    assert uses != _ALLOWED_RESPONSE_USES and any(u.startswith("OTHER") for u in uses), uses


# --------------------------------------------------------------------------- delete


class _TaggedObject:
    def __init__(self, tags: dict[str, str]):
        self.tags = tags

    def get_object_tagging(self, **_kwargs):
        return {"TagSet": [{"Key": k, "Value": v} for k, v in self.tags.items()]}


def _bundle_tags() -> dict[str, str]:
    import app.deployment_handler as dh
    from app.services.resource_ownership import owner_sub_hash, owner_tags

    return owner_tags(
        dh.config.aws_region, extra={"OwnerSubHash": owner_sub_hash("caller-a"), "ArtifactType": "cfn-template"}
    )


@pytest.mark.parametrize("deployment_id", [None, "", "dep-of-caller-b"])
def test_the_owned_object_delete_refuses_a_bundle(deployment_id):
    import app.deployment_handler as dh
    from app.services.resource_ownership import ResourceDeletionRefused, assert_s3_object_owned

    with pytest.raises(ResourceDeletionRefused, match="DeploymentId"):
        assert_s3_object_owned(
            _TaggedObject(_bundle_tags()),
            "acf-test-artifacts",
            "cfn-templates/abc/x-" + "f" * 32 + ".zip",
            region=dh.config.aws_region,
            deployment_id=deployment_id,
        )


def test_positive_control_the_delete_guard_accepts_a_real_deployment_object():
    """The refusal above must be the DeploymentId rule, not a guard that refuses everything."""
    import app.deployment_handler as dh
    from app.services.resource_ownership import assert_s3_object_owned

    tags = {**_bundle_tags(), "DeploymentId": "dep-1"}
    assert_s3_object_owned(
        _TaggedObject(tags),
        "acf-test-artifacts",
        "deployments/x.zip",
        region=dh.config.aws_region,
        deployment_id="dep-1",
    )
