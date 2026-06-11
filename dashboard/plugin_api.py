"""Dashboard API shim — imported by hermes' plugin-API mounter.

The dashboard web server imports this file (manifest.json ``api`` field,
which must point inside ``dashboard/``) as a standalone module named
``hermes_dashboard_plugin_mobile`` and reads its module-level ``router``
(CONTRACTS.md §2.2). The real routes live in ``hermes_mobile.plugin_api``
so they are import-testable as a normal package; this shim only makes
the plugin root importable and re-exports the router.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hermes_mobile.plugin_api import router  # noqa: E402,F401
