"""Make the hyphenated directory importable for pytest collection."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
