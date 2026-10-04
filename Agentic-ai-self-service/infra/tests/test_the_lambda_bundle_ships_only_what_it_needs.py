"""The backend Lambda asset must carry only what the function runs (O-5).

``get_backend_code`` packages a *directory of the working tree* minus a deny list, so it ships
whatever a developer happens to have left in ``backend/``. A deny list can only exclude what
somebody already noticed, and nobody noticed for two entries until the real deployed artifact
was read back out of S3.

Measured on ``acfe2e-p0920-deployment`` (155,176,077 bytes, 2026-09-21), before the fix:

    agentcore-deps  138,214,472   75% of the bundle, never opened by this function
    lib              41,889,750   the runtime dependencies
    src               2,976,308   the application
    .coverage           106,496   local test state

The costly one is ``agentcore-deps``. Those zips reach the runtime through
``buckets.upload_agentcore_deps`` — a separate ``BucketDeployment`` asset — and every
reference to them in ``backend/src`` is an S3 *key* fetched from the artifacts bucket at run
time, never a local path. So they were uploaded twice per deploy, and the copy in this bundle
consumed 55% of Lambda's 250 MB unzipped limit (183 MB of 250 used, 73%) for nothing. Worse,
that number moves with whether the developer has run ``scripts/install-agentcore-deps.sh``,
so the ceiling could have been crossed by a *tree state* rather than by a code change.

Why this test is shaped as an allow-list. ARCC ``cnt_db6JTpAHC6jztZ`` (secure build process)
asks for a hermetic build that validates and controls all inputs; ``Code.from_asset`` takes
no include list, so the deny list stays and the bound lives here instead. A new top-level
entry in ``backend/`` fails this test until somebody states which side of the line it is on.
That is the whole point: the two defects above were both new entries nobody classified.

The test reads ``BACKEND_ASSET_EXCLUDE`` out of the module the deploy uses rather than
holding a copy. A test with its own copy of the list passes while the deploy ships something
else -- the same failure mode as a test holding its own copy of a size limit.
"""

from __future__ import annotations

import fnmatch
import os

from stacks.platform.lambdas import BACKEND_ASSET_EXCLUDE

_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

# What the deployed function legitimately needs, with the reason it needs it. Anything in
# backend/ that is neither excluded nor listed here fails the test by design.
_ALLOWED: dict[str, str] = {
    "src": "the application",
    "lib": "runtime dependencies, pip-installed by the deploy script",
    "requirements-lambda.txt": "read by nothing at run time, but 881 bytes and it documents lib/",
    "pyproject.toml": "4 KB; tool config, harmless and useful when debugging in the console",
    ".env.example": "336 bytes of documentation, and deliberately an EXAMPLE -- .env itself is excluded",
}


def _excluded(name: str) -> bool:
    """Match CDK's asset exclusion the way it applies to a TOP-LEVEL entry.

    Only top-level entries are checked here, so a bare name and a glob are the only two
    shapes that matter; deeper patterns like ``__pycache__`` are covered because the
    directory itself is excluded at whatever level it appears.
    """
    return any(fnmatch.fnmatch(name, pattern) for pattern in BACKEND_ASSET_EXCLUDE)


def test_the_lambda_bundle_ships_only_what_it_needs():
    unclassified = sorted(name for name in os.listdir(_BACKEND) if not _excluded(name) and name not in _ALLOWED)
    assert not unclassified, (
        f"{unclassified} would be packaged into the backend Lambda bundle and is neither "
        f"excluded nor allowed. Add it to BACKEND_ASSET_EXCLUDE in stacks/platform/lambdas.py "
        f"if the function does not read it, or to _ALLOWED here with the reason it does. "
        f"Two entries reached a real deployed artifact this way: .coverage, and 138 MB of "
        f"agentcore-deps that the function only ever reads back from S3."
    )


def test_the_two_measured_offenders_are_excluded():
    """Named individually, because the allow-list test above would also pass if somebody
    "fixed" it by adding these to ``_ALLOWED``. The exclusion is the requirement; the
    classification test is only the tripwire for the NEXT one."""
    assert _excluded(".coverage"), "local coverage state shipped in the real bundle once"
    assert _excluded("agentcore-deps"), (
        "138 MB uploaded twice per deploy; these bundles are fetched from the artifacts "
        "bucket by S3 key at run time, never read from the package"
    )


def test_the_deny_list_does_not_exclude_the_application_itself():
    """A refusal-shaped list needs its happy path asserted too. A pattern like ``*`` or a
    stray ``src`` entry would make every test above pass -- an empty bundle excludes both
    offenders -- while the deployed function imported nothing."""
    for needed in ("src", "lib"):
        assert not _excluded(needed), f"{needed} must ship; excluding it deploys a Lambda that cannot import"
    assert os.path.isdir(os.path.join(_BACKEND, "src", "app")), "sanity: the source tree moved"


def test_dot_env_is_excluded_but_its_example_is_not():
    """``.env`` may hold real values on a developer's machine and must never be packaged;
    ``.env.example`` is documentation. The distinction is one character, and a glob written
    as ``.env*`` would silently collapse it -- so it is pinned rather than assumed."""
    assert _excluded(".env")
    assert not _excluded(".env.example")
