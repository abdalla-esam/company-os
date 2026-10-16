"""engine — Sovereign Agency OS Core Application Package.

Provides seamless module resolution for all internal engine subpackages:
- capsule_engine
- orchestration
- workers
- shared_kernel
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _ENGINE_ROOT.parent

for _p in (str(_ENGINE_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
