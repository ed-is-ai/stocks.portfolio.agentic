"""Add the fixture Strategy's ``scripts`` directory to ``sys.path``.

Mirrors the documented Strategy authoring convention (see
``docs/strategy-manager/strategy-authoring-v1.md``) and the existing
``skills/*/scripts/tests/conftest.py`` pattern: ``strategy.py`` lives one
directory up from this file's ``tests/`` package.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
