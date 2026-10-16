"""Dependency direction: the core is domain-agnostic, and the rest is measured.

Three questions, three tests — two rules and one report.

1. ``CoreStaysDomainAgnostic`` — an AST scan (module level **and** inside
   functions/methods, plus ``importlib.import_module``/``__import__`` string
   arguments) proves that nothing under ``engine/capsule_engine`` or
   ``engine/shared_kernel`` imports from ``data/domains``. The allowed list is
   empty: the core may depend on the standard library and on itself, never on a
   branch. Lazy imports count — that is why the scan reads the source instead
   of importing it, which cannot see them.

2. ``UpperLayerInversionsArePinned`` — the same scan over the two directories
   *above* the core (``engine/workers``, ``engine/orchestration``). The core is
   not allowed to import those either, but two pre-existing edges do (the
   sandbox discovers the worker catalog and drives the pulse). They are pinned
   as an exact set rather than exempted: a new inversion fails the test, and
   removing one fails it too until the pin shrinks. Emptying that set is
   injection/registry work, not a move.

3. ``DomainConsumerInventory`` — never fails. It prints every import from
   ``engine/orchestration`` and ``engine/workers`` into ``data/domains``, with
   line and scope, so the registry that will eventually own those edges can be
   designed from the real list rather than from memory.

The scan is static and path-based: a dotted module name is mapped through
:data:`PACKAGE_DIRS` to the repository directory it would import, and a target
that lands under a forbidden directory is a violation. Standard-library and
third-party names resolve to nothing and are ignored by construction.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

from tests._bootstrap import REPO_ROOT

import ast
import json
import re
import unittest
from pathlib import Path

#: The two packages the rules bind: the governance kernel and the shared
#: cross-domain contracts. Both must stay ignorant of every branch.
CORE_ROOTS = ("engine/capsule_engine", "engine/shared_kernel")

#: Layers that are measured, not bound: they legitimately reach into a domain
#: today, and the inventory is the input to designing their registry.
REPORTING_ROOTS = ("engine/orchestration", "engine/workers")

#: Where a branch lives (``data/domains/<name>/...``).
DOMAIN_ROOT = "data/domains"

#: The layers above the core: importing them from the core is an inversion.
UPPER_LAYERS = ("engine/workers", "engine/orchestration")

#: Importable top-level package -> repository directory. Anything not listed is
#: the standard library or a distribution from ``requirements.txt``.
PACKAGE_DIRS = {
    "capsule_engine": "engine/capsule_engine",
    "shared_kernel": "engine/shared_kernel",
    "workers": "engine/workers",
    "orchestration": "engine/orchestration",
    "data": "data",
    "engine": "engine",
}

#: Nothing is exempt: a single entry here would defeat the rule, so the test
#: asserts this tuple stays empty.
ALLOWED_CORE_IMPORTS: tuple[str, ...] = ()

#: The pre-existing core -> upper-layer edges, pinned by ``(file, target)`` so
#: the pin survives edits above them. Both live in the sandbox, whose job is to
#: drive a trial end to end: it discovers the worker catalog to pick the trial's
#: factory domain and calls the pulse to move the capsule. Neither can go
#: without handing those dependencies in from above (registry/injection work);
#: until then this set is the complete, frozen inventory.
KNOWN_UPPER_LAYER_INVERSIONS = frozenset(
    {
        ("engine/capsule_engine/sandbox.py", "workers"),
        ("engine/capsule_engine/sandbox.py", "orchestration.capsule_pulse"),
    }
)

#: Dynamic-import helpers whose constant string argument is resolved as if it
#: were an ``import`` statement.
_DYNAMIC_IMPORTERS = frozenset({"import_module", "importlib.import_module", "__import__"})

MODULE_SCOPE = "module-level"
FUNCTION_SCOPE = "lazy (inside a function)"


def _module_dir(target: str) -> str | None:
    """Repository directory a dotted ``target`` would import, or ``None``.

    ``data.domains.marketing.gateway_ops`` -> ``data/domains/marketing/gateway_ops``;
    ``json`` -> ``None`` (not part of this repository).
    """
    parts = [part for part in target.split(".") if part]
    if not parts:
        return None
    directory = PACKAGE_DIRS.get(parts[0])
    if directory is None:
        return None
    return "/".join([directory, *parts[1:]])


def _under(path: str, root: str) -> bool:
    """True when ``path`` *is* ``root`` or lives inside it."""
    return path == root or path.startswith(root + "/")


def _package_parts(relative: str) -> list[str]:
    """Dotted package parts of a repository file (its own directory)."""
    for package, directory in PACKAGE_DIRS.items():
        if _under(relative, directory):
            rest = relative[len(directory) :].strip("/").split("/")
            return [package, *rest[:-1]]
    return []


class _ImportCollector(ast.NodeVisitor):
    """Every import a file performs, with the scope it lives in.

    ``visit_FunctionDef`` flips :attr:`_scope` for the body of a function or
    method — an import inside one is invisible to an import-time check and is
    exactly what this rule must catch. ``try``/``if`` blocks keep the enclosing
    scope, so a module-level ``try: import x`` still reports as module-level.
    """

    def __init__(self, package_parts: list[str]) -> None:
        self._package_parts = package_parts
        self._scope = MODULE_SCOPE
        self.imports: list[tuple[int, str, str]] = []

    # -- scope --------------------------------------------------------- #
    def _visit_function(self, node: ast.AST) -> None:
        previous = self._scope
        self._scope = FUNCTION_SCOPE
        try:
            self.generic_visit(node)
        finally:
            self._scope = previous

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # -- imports ------------------------------------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append((node.lineno, alias.name, self._scope))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            drop = node.level - 1
            base = (
                self._package_parts[: len(self._package_parts) - drop]
                if drop
                else list(self._package_parts)
            )
            target = ".".join([*base, *[part for part in module.split(".") if part]])
        else:
            target = module
        if target:
            self.imports.append((node.lineno, target, self._scope))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _DYNAMIC_IMPORTERS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self.imports.append((node.lineno, first.value, self._scope))
        self.generic_visit(node)


def _imports_of(path: Path, relative: str) -> list[tuple[int, str, str]]:
    """Parsed imports of one file as ``(lineno, dotted target, scope)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collector = _ImportCollector(_package_parts(relative))
    collector.visit(tree)
    return collector.imports


def _source_files(root: str) -> list[Path]:
    """Every ``.py`` file under one repository root (sorted)."""
    base = REPO_ROOT / root
    if not base.is_dir():
        return []
    return sorted(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)


def _scan(roots: tuple[str, ...]) -> list[tuple[str, tuple[int, str, str]]]:
    """``[(relative file, ((lineno, target, scope), ...))]`` for the roots."""
    found: list[tuple[str, tuple[int, str, str]]] = []
    for root in roots:
        for path in _source_files(root):
            relative = path.relative_to(REPO_ROOT).as_posix()
            found.append((relative, tuple(_imports_of(path, relative))))
    return found


def _hits(targets: tuple[str, ...]) -> tuple[list[str], set[tuple[str, str]], int]:
    """Scan the core for imports landing under ``targets``.

    Returns ``(descriptions, {(file, dotted target)}, files_scanned)``; the
    middle value is what the pin compares.
    """
    descriptions: list[str] = []
    pinned: set[tuple[str, str]] = set()
    scanned = 0
    for relative, imports in _scan(CORE_ROOTS):
        scanned += 1
        for lineno, target, scope in imports:
            directory = _module_dir(target)
            if directory is None:
                continue
            if any(_under(directory, wanted) for wanted in targets):
                descriptions.append(
                    f"{relative}:{lineno}: {target!r} ({scope}) -> {directory}"
                )
                pinned.add((relative, target))
    return descriptions, pinned, scanned


class CoreStaysDomainAgnostic(unittest.TestCase):
    """The rule: no core module knows a branch exists."""

    def test_source_tree_is_walked(self) -> None:
        files = [path for root in CORE_ROOTS for path in _source_files(root)]
        self.assertGreater(len(files), 10, "core source walk found suspiciously few files")

    def test_allowed_list_is_empty(self) -> None:
        self.assertEqual(
            ALLOWED_CORE_IMPORTS,
            (),
            "the core may not depend on a domain: no exceptions",
        )

    def test_core_never_imports_a_domain(self) -> None:
        violations, _pinned, scanned = _hits((DOMAIN_ROOT,))
        self.assertGreater(scanned, 10, "core source walk was empty")
        self.assertEqual(
            violations,
            [],
            "core files import a domain branch (moves belong in the branch, "
            "not in the kernel):\n  " + "\n  ".join(violations),
        )


class UpperLayerInversionsArePinned(unittest.TestCase):
    """The core may not import the layers above it — and cannot silently grow."""

    def test_only_the_known_inversions_reach_upward(self) -> None:
        violations, found, scanned = _hits(UPPER_LAYERS)
        self.assertGreater(scanned, 10, "core source walk was empty")
        new = sorted(found - KNOWN_UPPER_LAYER_INVERSIONS)
        gone = sorted(KNOWN_UPPER_LAYER_INVERSIONS - found)
        self.assertEqual(
            (new, gone),
            ([], []),
            "core -> upper-layer inversions changed.\n"
            f"  new (not pinned, fix or pin deliberately): {new}\n"
            f"  resolved (strike from KNOWN_UPPER_LAYER_INVERSIONS): {gone}\n"
            f"  details: {'; '.join(violations)}",
        )


class TaskTypesNameWorkersAsStrings(unittest.TestCase):
    """The loader is core, and a task type *names* a worker — it never imports one.

    ``engine/capsule_engine/task_type.py`` sits in the core, so the scans above
    already bind it. What this class adds is the *data* half of the rule: a
    task-type document may only ever name a worker as a registry identifier. A
    dotted module path would let configuration reach around the worker registry
    straight into a domain package — precisely the coupling this layering
    exists to prevent.
    """

    #: A registry identifier: no dots, no path separators, no package syntax.
    WORKER_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

    def test_loader_is_scanned_as_part_of_the_core(self) -> None:
        loader = REPO_ROOT / "engine" / "capsule_engine" / "task_type.py"
        self.assertTrue(loader.is_file(), "the task type loader must ship in the core")
        offenders = [
            f"{target!r} ({scope})"
            for _lineno, target, scope in _imports_of(
                loader, "engine/capsule_engine/task_type.py"
            )
            if (_module_dir(target) or "").startswith(DOMAIN_ROOT)
        ]
        self.assertEqual(offenders, [], "the task type loader imports a domain")

    def test_declared_workers_are_registry_names(self) -> None:
        base = REPO_ROOT / "config" / "task_types"
        if not base.is_dir():
            self.skipTest("no task type catalogue in this checkout")

        offenders: list[str] = []
        checked = 0
        for path in sorted(base.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            for step in document.get("steps") or []:
                worker = step.get("worker")
                if worker is None:
                    continue
                checked += 1
                if (
                    not isinstance(worker, str)
                    or "." in worker
                    or not self.WORKER_NAME.match(worker)
                ):
                    offenders.append(
                        f"{path.name}: step {step.get('id')!r} -> {worker!r}"
                    )

        self.assertGreater(checked, 0, "no declared workers were checked")
        self.assertEqual(
            offenders,
            [],
            "task types must name workers (a registry identifier), never import "
            "them (a dotted module path):\n  " + "\n  ".join(offenders),
        )


class DomainConsumerInventory(unittest.TestCase):
    """The report: who reaches into a domain, from where, and how."""

    @staticmethod
    def _consumers() -> tuple[int, list[str], dict[str, int]]:
        """One pass over the reporting roots, in file/line order."""
        scanned = 0
        consumers: list[str] = []
        counts: dict[str, int] = {}
        for relative, imports in _scan(REPORTING_ROOTS):
            scanned += 1
            for lineno, target, scope in imports:
                directory = _module_dir(target)
                if directory is None or not _under(directory, DOMAIN_ROOT):
                    continue
                counts[scope] = counts.get(scope, 0) + 1
                consumers.append(f"  {relative}:{lineno}  ({scope})  {target}")
        return scanned, consumers, counts

    def test_report_domain_consumers(self) -> None:
        scanned, consumers, counts = self._consumers()
        summary = ", ".join(f"{scope}: {count}" for scope, count in sorted(counts.items()))
        print(
            "\n".join(
                [
                    "domain consumers (engine/orchestration + engine/workers -> data/domains)",
                    f"  files scanned: {scanned}",
                    f"  imports found: {len(consumers)}" + (f"  [{summary}]" if summary else ""),
                    *consumers,
                ]
            )
        )

        self.assertGreater(scanned, 5, "report walk found suspiciously few files")
        # The report is evidence for designing the registry, so it must be
        # reproducible byte for byte.
        self.assertEqual(self._consumers(), (scanned, consumers, counts))


if __name__ == "__main__":
    unittest.main()
