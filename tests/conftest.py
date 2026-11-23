"""Put the repo root on sys.path so `import sig`, `import strategies`, etc. resolve under pytest.

Without this, pytest's default (prepend) import mode adds only the tests/ dir to sys.path, so test
modules that do `from sig.oms... import ...` fail to collect with ModuleNotFoundError: No module named
'sig'. Several suites (test_oms, test_orchestrator) rely on the repo root being importable; this makes
the whole suite collect uniformly. (Individual tests that already sys.path.insert still work — the insert
is idempotent.)
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
