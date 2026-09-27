"""Unit tests for the adversarial harness itself.

Hard constraints for everything in this directory:

* no AWS calls, no network, no credentials, no boto3 import;
* no reading of any credential file;
* every test must be able to run on a laptop with nothing configured.

``test_no_aws_dependency.py`` enforces the first two mechanically.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
