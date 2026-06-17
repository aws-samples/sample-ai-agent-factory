# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for RegistryClient authentication logic."""

import pytest

from services.registry_client import _build_cognito_token_url


class TestBuildCognitoTokenUrl:

    def test_full_token_url_returned_as_is(self):
        creds = {"token_url": "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com/oauth2/token"}
        assert _build_cognito_token_url(creds, "us-west-2") == creds["token_url"]

    def test_prefix_only_domain_constructs_full_url(self):
        creds = {"cognito_domain": "workshop-mcp-registry"}
        url = _build_cognito_token_url(creds, "us-west-2")
        assert url == "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com/oauth2/token"

    def test_full_domain_url(self):
        creds = {"cognito_domain": "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com"}
        url = _build_cognito_token_url(creds, "us-west-2")
        assert url == "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com/oauth2/token"

    def test_full_domain_without_scheme(self):
        creds = {"cognito_domain": "workshop-mcp-registry.auth.us-west-2.amazoncognito.com"}
        url = _build_cognito_token_url(creds, "us-west-2")
        assert url == "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com/oauth2/token"

    def test_broken_token_url_falls_back_to_domain(self):
        """If token_url doesn't contain amazoncognito.com, use cognito_domain instead."""
        creds = {
            "token_url": "https://workshop-mcp-registry/oauth2/token",  # Bug in Siva's Lambda
            "cognito_domain": "workshop-mcp-registry",
        }
        url = _build_cognito_token_url(creds, "us-west-2")
        assert url == "https://workshop-mcp-registry.auth.us-west-2.amazoncognito.com/oauth2/token"

    def test_no_domain_raises(self):
        with pytest.raises(ValueError, match="No token_url or cognito_domain"):
            _build_cognito_token_url({}, "us-west-2")
