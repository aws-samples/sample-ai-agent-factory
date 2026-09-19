"""Adversarial verification suite.

Two independent halves:

``unit/``
    Tests of the harness itself. No AWS, no network, no credentials. These are
    the tests that run in ordinary CI.
``cases/``
    Catalog-driven live cases. They skip when live mode is off and fail — never
    skip — when live mode was asked for but is unavailable, or when a
    catalogued control has no probe yet.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
