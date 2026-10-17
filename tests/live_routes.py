"""live_routes.py — the live FastAPI route table, seen through lazy includes.

The route guards in this suite read the app's **real** table, never a
hand-written list; that is what makes the capability map exhaustive instead of
advisory. FastAPI 0.143 broke the naive way of reading it: ``include_router``
no longer copies an included router's routes into ``app.routes`` — it appends
one lazy ``fastapi.routing._IncludedRouter`` entry — so a walk of
``app.routes`` sees a handful of entries and every assertion built on it goes
quietly vacuous. The routes are still served (the OpenAPI schema still lists
them, and the live service answers them); only the *introspection* changed.

``effective_route_contexts()`` is FastAPI's own answer for the expanded table,
prefixes applied. Older builds expose the routes directly on ``app.routes``, so
both shapes are handled here and one helper serves every walker.

Read-only. Import ``tests._bootstrap`` first, as always, so the isolated vault
exists before any engine module is imported.
"""

from __future__ import annotations  # noqa: E402  — the suite runs on 3.9

from typing import FrozenSet, Iterator, Set, Tuple


def iter_live_routes() -> Iterator[Tuple[str, FrozenSet[str]]]:
    """Yield ``(path, methods)`` for every route the live app really serves.

    ``methods`` is empty for a route with no method vocabulary (a static
    ``Mount``), matching what a plain ``app.routes`` walk used to hand out.
    """
    from orchestration import api

    for route in api.app.routes:
        expand = getattr(route, "effective_route_contexts", None)
        if callable(expand):  # FastAPI >= 0.140: an included router is lazy
            for context in expand():
                path = getattr(context, "path", "") or ""
                source = getattr(context, "original_route", None) or getattr(
                    context, "starlette_route", None
                )
                methods = frozenset(getattr(source, "methods", None) or ())
                if path:
                    yield path, methods
            continue

        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if isinstance(path, str) and path and methods:
            yield path, frozenset(methods)


def live_route_keys() -> Set[Tuple[str, str]]:
    """``(METHOD, path)`` for the whole table, normalized as the policy sees it.

    ``HEAD`` folds onto ``GET`` through the policy module's own normalizer, so
    this set and ``access_control.resolve()`` can never disagree about what a
    method is called.
    """
    from orchestration import access_control

    return {
        (access_control._normalize_method(method), path)
        for path, methods in iter_live_routes()
        for method in methods
        if method
    }
