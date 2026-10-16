"""The ``/v1`` mobile contract (Slice 1): review + attested decisions.

Slice 0 guarded the ``/v1/`` namespace before its first route existed. This
package adds that first route: a *read-only* review of a capsule (what the
reviewer will see, hashed by the server) and a *fail-closed* decision that
carries the SHA-256 of exactly what the reviewer saw. The decision service
(`orchestration.approval`) is the single source of truth; this package only
shapes the mobile contract around it.

``install(app)`` mounts the router and registers the ``/v1`` error envelope.
It is called from the bottom of ``orchestration/api.py`` so the v1 routes are
mounted after every ``/api`` route and the auth middleware.
"""

from __future__ import annotations

from orchestration.v1.errors import install
from orchestration.v1.router import router

__all__ = ["install", "router"]
