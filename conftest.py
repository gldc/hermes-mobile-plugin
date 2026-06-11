"""Root conftest — ensures the repo root is importable as ``hermes_mobile``'s
parent regardless of pytest invocation directory."""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
