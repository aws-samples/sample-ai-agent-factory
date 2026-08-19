"""pytest conftest for the LangGraph blueprint — adds the local dir to sys.path.

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
