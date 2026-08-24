"""Test-only pytest configuration.

Adds the ``src/github-copilot-activity`` package directory to ``sys.path`` so
tests can ``import`` the (not-yet-written) domain modules by simple module
name, matching the flat, no-``__init__.py`` layout already used by
``main.py`` / ``client.py`` / ``tools.py`` in this sample.

This file is test-only scaffolding; it defines no production behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))
