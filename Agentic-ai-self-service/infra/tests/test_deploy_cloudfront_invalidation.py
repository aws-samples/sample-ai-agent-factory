"""deploy.sh must invalidate THIS stack's CloudFront distribution, and say so if it can't.

Why this exists: the lookup used to filter ``list-distributions`` on the
distribution ``Comment``, which ``infra/stacks/platform/cloudfront_waf.py`` sets to
``<project>-<env> distribution`` with no region in it. CloudFront is a global
service, so ``--region`` does not scope the listing either. The moment a second
region was deployed into the same account the query returned two tab-separated ids
and ``create-invalidation`` failed with ``NoSuchDistribution`` — observed for real
on the first ``eu-central-1`` deploy while ``us-east-1`` was live.

The old failure branch also only logged at INFO and returned, so the frontend was
already uploaded to S3 while the edges kept serving the previous build: a deploy
that looks green and shows stale code in the browser.

As in ``test_deploy_cognito_guard.py``, the domain-derivation helper is executed as
the *actual* bash shipped in deploy.sh rather than a transcription, and the wiring
is asserted textually.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DEPLOY_SH = _ROOT / "scripts" / "deploy.sh"
_CLOUDFRONT_PY = _ROOT / "infra" / "stacks" / "platform" / "cloudfront_waf.py"


def _deploy_sh() -> str:
    return _DEPLOY_SH.read_text()


def _extract_fn(name: str) -> str:
    src = _deploy_sh()
    start = src.index(f"{name}() {{")
    end = src.index("\n}\n", start) + len("\n}\n")
    return src[start:end]


def _cf_domain_from_url(url: str) -> str:
    """Run the real helper out of deploy.sh under bash."""
    fn = _extract_fn("cf_domain_from_url")
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{fn}\ncf_domain_from_url "$1"', "_", url],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


class TestDomainDerivation:
    @pytest.mark.parametrize(
        "url",
        [
            "https://d2hbw3cqibb0sd.cloudfront.net",
            "https://d2hbw3cqibb0sd.cloudfront.net/",
            "http://d2hbw3cqibb0sd.cloudfront.net",
            "d2hbw3cqibb0sd.cloudfront.net",
        ],
    )
    def test_the_scheme_and_path_are_stripped(self, url):
        """CloudFront's DomainName attribute is a bare host, so anything left over
        makes the JMESPath filter match nothing — which is the silent-skip path."""
        assert _cf_domain_from_url(url) == "d2hbw3cqibb0sd.cloudfront.net"

    def test_a_path_suffix_does_not_leak_in(self):
        assert _cf_domain_from_url("https://example.cloudfront.net/api/health") == "example.cloudfront.net"

    def test_an_empty_url_yields_empty_rather_than_aborting(self):
        """Runs inside ``$(...)`` under ``set -euo pipefail``; a non-zero exit or an
        unbound-variable error here would take the whole deploy down."""
        assert _cf_domain_from_url("") == ""


class TestTheLookupIsRegionSafe:
    def test_it_filters_on_domainname_from_this_stack(self):
        src = _deploy_sh()
        assert "DistributionList.Items[?DomainName=='${cf_domain}'].Id" in src
        assert 'cf_domain=$(cf_domain_from_url "${CLOUDFRONT_URL}")' in src

    def test_it_no_longer_filters_on_comment(self):
        """The regression guard. Comment is identical in every region."""
        assert "Comment==" not in _deploy_sh()

    def test_the_comment_really_is_region_agnostic(self):
        """Pins the premise, so this test starts failing if the CDK ever makes the
        comment unique and the Comment filter becomes viable again."""
        cdk = _CLOUDFRONT_PY.read_text()
        assert 'comment=f"{cfg.project}-{cfg.env} distribution"' in cdk
        assert "region" not in cdk[cdk.index('comment=f"{cfg.project}') :][:120]

    def test_outputs_are_validated_before_the_lookup_uses_them(self):
        """CLOUDFRONT_URL feeds the lookup, so the empty-output guard has to run
        first or the failure surfaces as an unexplained empty distribution list."""
        src = _deploy_sh()
        assert src.index("Failed to extract one or more stack outputs.") < src.index("cf_domain=$(")


class TestFailureIsLoud:
    def test_an_unresolved_distribution_fails_the_deploy(self):
        fn = _extract_fn("invalidate_cloudfront_cache")
        assert "log_error" in fn
        assert "INVALIDATION_FAILED=1" in fn
        assert "Skipping CloudFront invalidation" not in fn

    def test_an_ambiguous_lookup_is_refused_instead_of_passed_through(self):
        """Belt and braces for the exact old bug: two ids in one string must never
        reach create-invalidation."""
        fn = _extract_fn("invalidate_cloudfront_cache")
        assert '"${DISTRIBUTION_ID}" == *[[:space:]]*' in fn

    def test_main_exits_non_zero_after_reporting(self):
        src = _deploy_sh()
        main = src[src.index("main() {") :]
        assert main.index("print_summary") < main.index('INVALIDATION_FAILED}" == "1"')
        assert "exit 1" in main[main.index('INVALIDATION_FAILED}" == "1"') :]

    def test_the_flag_is_initialised_for_set_u(self):
        """``set -u`` is on; an unset flag would abort every successful deploy at the
        final check."""
        src = _deploy_sh()
        assert src.index("INVALIDATION_FAILED=0") < src.index("invalidate_cloudfront_cache() {")
