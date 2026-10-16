#!/usr/bin/env python3
"""scope.py — the ``/v1`` read-visibility rule: only the sectors you are granted.

Every ``/v1`` read is sector-scoped on the server. A principal may see a domain
when *its credential alone* says so:

- the root grant (``*``) and the legacy shared token — every sector;
- otherwise, exactly the sectors named in its ``domain:read`` grants;
  ``domain:read:*`` means every sector, declared out loud.

Visibility is derived from the verified :class:`~orchestration.access_control.Principal`
and nothing else. In particular it does **not**:

- read ``config/users.json`` (the ``/v1`` path has no filesystem identity
  lookup — the key is the state);
- derive from other grants (``capsule:approve:marketing`` is a *write* scope and
  confers no read).

Two properties make the rule safe:

- **no accidental skip.** The route map's floor is ``domain:read`` (any scope),
  so the middleware refuses a credential that can read no sector at all; the
  handler then calls :func:`require_sector` with the object's *real* domain.
- **no existence leak.** A sector a caller may not see answers ``404`` with the
  object's own "not found" body — never ``403``, and never anything that would
  let a caller distinguish "not yours" from "not there".

Scope of this slice (recorded for the aggregating reads M1 will add):

- single-domain objects only — a capsule and a ticket each have exactly one
  domain/sector, so one comparison is the whole rule;
- company-wide data is **not** exposed under ``/v1`` here. When it is, the rule
  is: finance status and the kill switch need root or ``finance:control``;
  the gear registry needs root or ``tools:invoke``; the audit spool needs root.
  ``domain:read`` never reaches them.

Standard library apart from the kernel it reads; Python 3.10+.
"""

from __future__ import annotations

from typing import Any, FrozenSet, Optional, Tuple

from capsule_engine.capabilities import (
    CAPABILITY_ALL,
    CAPABILITY_DOMAIN_READ,
    SCOPE_ALL,
)
from orchestration.v1.errors import V1Error

__all__ = ["sector_is_visible", "require_sector", "visible_sectors"]


def visible_sectors(principal: Any) -> Tuple[bool, FrozenSet[str]]:
    """Return ``(sees_every_sector, sectors)`` for a principal on ``/v1``.

    ``sees_every_sector`` is ``True`` for root (and the legacy root token) and
    for a ``domain:read:*`` grant; ``sectors`` is then empty and meaningless.
    Otherwise ``sectors`` is exactly the set named by ``domain:read:<sector>``
    grants, and ``sees_every_sector`` is ``False``. An absent principal sees
    nothing.
    """
    if principal is None:
        return False, frozenset()

    if getattr(principal, "is_root", False):
        return True, frozenset()

    sectors: set[str] = set()
    for grant in getattr(principal, "grants", frozenset()) or frozenset():
        capability = getattr(grant, "capability", None)
        if capability == CAPABILITY_ALL:
            return True, frozenset()
        if capability != CAPABILITY_DOMAIN_READ:
            continue
        scope = getattr(grant, "scope", None)
        if scope == SCOPE_ALL:
            return True, frozenset()
        if isinstance(scope, str) and scope:
            sectors.add(scope)
    return False, frozenset(sectors)


def sector_is_visible(principal: Any, sector: Optional[str]) -> bool:
    """True when ``principal`` may read ``sector``.

    Root sees every sector, including an object that names none. A non-root
    caller sees only the sectors it holds; an object with no (or unreadable)
    sector is invisible to it.
    """
    sees_every_sector, sectors = visible_sectors(principal)
    if sees_every_sector:
        return True
    if not isinstance(sector, str) or not sector:
        return False
    return sector in sectors


def require_sector(principal: Any, sector: Optional[str], *, not_found: str) -> None:
    """Refuse an invisible sector, exactly as if the object did not exist.

    ``not_found`` must be the object's *own* missing-resource message so the
    refusal is byte-identical to a read of something absent: an unauthorised
    caller learns nothing about whether the object exists, and a caller who may
    not read it is indistinguishable from one who mistyped the id.
    """
    if not sector_is_visible(principal, sector):
        raise V1Error(404, not_found, code="NOT_FOUND")
