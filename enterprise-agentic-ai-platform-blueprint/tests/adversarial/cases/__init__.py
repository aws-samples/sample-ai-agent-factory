"""Catalog-driven live cases.

There is exactly one test body (``test_catalog_cases.py``) and one registry of
probes (``probes/``). Adding a live case means registering a probe for a
catalogued case id — never writing a new bespoke test that could quietly skip
its own assertions.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
