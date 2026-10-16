#!/usr/bin/env python3
"""workers — concrete capsule workers of the Sovereign Agency OS.

Two lawful ways to add a tool (worker) to the factory — everything else in
the system (branches, tickets, capsules, models) is likewise a sample, not a
contract:

1. **Core worker** (shared machinery): a module in this package defining one
   :class:`capsule_engine.BaseWorker` subclass, registered explicitly in
   :data:`WORKERS`. The core registry stays explicit on purpose — these are
   the engine's own gears, and a routing-table entry missing here is reported
   by the pulse (``no_worker`` bucket), never guessed.

2. **Branch-owned worker** (a single folder, zero core edits): a domain may
   ship ``domains/<name>/worker.py`` defining at least one ``BaseWorker``
   subclass. :func:`domain_workers` discovers these at pulse time and
   registers each class under its ``name`` ClassVar (falling back to the
   domain directory name). A branch-owned worker is more specific than a core
   one and overrides a same-named core entry. This is a declared contract,
   not import magic: only a file named exactly ``worker.py`` directly inside
   ``domains/<name>/`` is read, and a module that fails to import is skipped
   with a warning — reported, never simulated.

Importing a worker class never executes anything; the pulse (or a human)
decides when ``run()`` is called.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Union

from capsule_engine import BaseWorker
from workers.design import DesignWorker
from workers.gate_runner import GateRunnerWorker
from workers.governor import GovernorWorker
from workers.marketing import MarketingWorker
from workers.publishing import PublishingWorker
from workers.qa import QAWorker

logger = logging.getLogger("workers")

__all__ = [
    "WORKERS",
    "domain_workers",
    "DesignWorker",
    "GateRunnerWorker",
    "GovernorWorker",
    "MarketingWorker",
    "PublishingWorker",
    "QAWorker",
]

# Core worker name -> worker class. Keys must match CapsuleRouter's routing
# table (see DEFAULT_WORKER_BY_STATE) — a drift guard test enforces this.
WORKERS: dict[str, type[BaseWorker]] = {
    MarketingWorker.name: MarketingWorker,
    QAWorker.name: QAWorker,
    DesignWorker.name: DesignWorker,
    PublishingWorker.name: PublishingWorker,
    GovernorWorker.name: GovernorWorker,
    GateRunnerWorker.name: GateRunnerWorker,
}

# The one optional file a branch may ship to own its factory stage.
DOMAIN_WORKER_FILE = "worker.py"


def domain_workers(
    domains_dir: Union[str, Path, None] = None,
) -> dict[str, type[BaseWorker]]:
    """Discover branch-owned workers: ``domains/<name>/worker.py``.

    Returns ``{worker_name: worker_class}``. The branch folder is the unit of
    extension: dropping a ``worker.py`` into a domain directory (new or old)
    wires its factory stage into the next pulse — the router already routes
    that branch's ``draft``/``needs_revision`` capsules by domain name.

    Failure policy (fail closed, never guessed): a module that cannot be
    imported, or that defines no ``BaseWorker`` subclass, contributes nothing
    and logs a warning; the pulse then honestly reports ``no_worker`` for any
    capsule routed to it.
    """
    base = Path(domains_dir) if domains_dir is not None else _default_domains_dir()
    if not base.is_dir():
        return {}

    discovered: dict[str, type[BaseWorker]] = {}
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        module_file = entry / DOMAIN_WORKER_FILE
        if not module_file.is_file():
            continue

        module_name = f"domain_worker_{entry.name}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_file)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {module_file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # a broken branch tool must not kill the pass
            logger.warning(
                "domain worker skipped: %s failed to import: %s", module_file, exc
            )
            continue

        classes = [
            attr
            for attr in vars(module).values()
            if isinstance(attr, type)
            and issubclass(attr, BaseWorker)
            and attr is not BaseWorker
            and getattr(attr, "__module__", None) == module_name
        ]
        if not classes:
            logger.warning(
                "domain worker skipped: %s defines no BaseWorker subclass",
                module_file,
            )
            continue
        for cls in classes:
            name = str(getattr(cls, "name", None) or entry.name)
            discovered[name] = cls

    return discovered


def _default_domains_dir() -> Path:
    """Return canonical domains directory."""
    try:
        from shared_kernel.paths import DOMAINS_DIR
        return DOMAINS_DIR
    except Exception:
        repo_root = Path(__file__).resolve().parents[2]
        return repo_root / "data" / "domains"
