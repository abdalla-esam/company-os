"""Bypass guards: only the pinned choke points may open a socket — or resurrect.

Two static rules over the whole source tree, written in the same spirit as
``tests/test_dependency_direction.py``: an exact, frozen inventory that a new
edit cannot silently widen.

1. ``NetworkEscapesArePinned`` — an AST scan of **every** ``.py`` file fails
   when a module outside the exact pin opens a network connection. The
   primitives are the ones money leaks travel through: ``urlopen`` in any
   spelling (``urllib.request.urlopen`` or ``from urllib.request import
   urlopen``), the outbound-only client libraries (``requests``, ``httpx``,
   ``urllib3``, ``http.client`` — importing them at all is a violation, since
   they exist to make HTTP calls), and socket connection constructors
   (``socket.socket`` / ``create_connection`` / ``.connect`` / DNS lookups).
   ``socket.gethostname()`` is local identity, not a connection, and inbound
   listeners (``http.server``) are not clients — neither is flagged.

   The pin **is** the inventory, exactly like ``KNOWN_UPPER_LAYER_INVERSIONS``:
   a new direct call fails the test, and a choke point that disappears fails
   it too until the pin is struck deliberately. Every pinned call site is
   behind ``spend_control.authorize()`` (Session C), so "pinned" means
   "gated", never "allowed to spend freely".

2. ``ResurrectIsHumanOnly`` — no production module (engine, data, ops) calls
   ``resurrect()``. It is the human way out of ``failed``; the pulse, the
   workers, the dispatcher and the API must never reach it on their own.
   The allowed set is empty and asserted to stay empty.

The scan reads source instead of importing it, so lazy imports inside
functions — where a bypass would naturally hide — are visible.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

from tests._bootstrap import REPO_ROOT

import ast
import unittest
from pathlib import Path

#: Outbound-only HTTP/network client libraries: existing at all in a module is the
#: violation (a stub target in ``mock.patch("requests.post")`` is a string,
#: not an import, and stays legal).
OUTBOUND_LIBRARIES: tuple[str, ...] = (
    "requests",
    "httpx",
    "urllib3",
    "http.client",
    "aiohttp",
    "smtplib",
    "ftplib",
    "telnetlib",
    "websockets",
)

#: ``socket.<name>(...)`` forms that open a connection or resolve via DNS.
SOCKET_CALLS: frozenset[str] = frozenset(
    {"socket", "create_connection", "socketpair", "fromfd", "fromshare"}
)
SOCKET_DNS_CALLS: frozenset[str] = frozenset(
    {"gethostbyname", "gethostbyaddr", "getaddrinfo"}
)

#: ``asyncio.<name>(...)`` client connection and server constructors.
ASYNCIO_CALLS: frozenset[str] = frozenset(
    {"open_connection", "start_server"}
)

#: Blocked subprocess binaries that can make raw network requests.
BLOCKED_SUBPROCESS_COMMANDS: frozenset[str] = frozenset({"curl", "wget", "nc"})

#: Dynamic-import helpers whose constant string argument counts as an import
#: of that module (same trick as the dependency-direction test).
_DYNAMIC_IMPORTERS = frozenset(
    {"import_module", "importlib.import_module", "__import__"}
)

#: (file, primitive) pairs that may open a connection in this repository.
#:
#: The complete inventory — every entry spends through ``spend_control``
#: first: the model gateway, the gear bridge, the two gated image fetchers and
#: the Directus tool bridge. The sector scripts are deliberately absent: the
#: operations worker now publishes through the ``social:publish`` gear on the
#: bridge (not a raw n8n webhook), like marketing/design it holds no socket of
#: its own.
ALLOWED_NETWORK_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("engine/capsule_engine/gateway.py", "urlopen"),
        ("engine/capsule_engine/bridge.py", "urlopen"),
        ("engine/workers/design.py", "urlopen"),
        ("engine/orchestration/directus_bridge.py", "urlopen"),
    }
)

#: Production roots scanned by both rules (tests/ are excluded from the
#: resurrect rule: the revision-cap suite exercises the human path on purpose).
PRODUCTION_ROOTS: tuple[str, ...] = ("engine", "data", "ops")


def _source_files() -> list[Path]:
    """Every ``.py`` file in the repository (sorted, cache dirs skipped)."""
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if ".git" not in path.parts and "__pycache__" not in path.parts
    )


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in PRODUCTION_ROOTS:
        base = REPO_ROOT / root
        if base.is_dir():
            files.extend(_ for _ in base.rglob("*.py") if "__pycache__" not in _.parts)
    return sorted(files)


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for Name/Attribute chains, ``""`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _root(node: ast.AST) -> str:
    """The leftmost identifier of a chain (``sqlite3`` for ``sqlite3.connect``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


class _NetworkCollector(ast.NodeVisitor):
    """Network primitives used by one file, as ``[(lineno, primitive, detail)]``."""

    def __init__(self) -> None:
        self.hits: list[tuple[int, str, str]] = []

    def _record(self, node: ast.AST, primitive: str, detail: str) -> None:
        self.hits.append((getattr(node, "lineno", 0), primitive, detail))

    # -- imports of outbound client libraries ---------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if self._is_outbound(alias.name):
                self._record(node, alias.name, f"import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if self._is_outbound(module):
            names = ", ".join(alias.name for alias in node.names)
            self._record(node, module, f"from {module} import {names}")
        self.generic_visit(node)

    # -- call sites ------------------------------------------------------- #
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        dotted = _dotted(func)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

        # urlopen in any spelling: urllib.request.urlopen(...) / urlopen(...)
        if name == "urlopen":
            self._record(node, "urlopen", dotted or "urlopen(...)")

        # socket connection constructors and DNS lookups
        if dotted.startswith("socket."):
            member = dotted.split(".", 1)[1]
            if member in SOCKET_CALLS or member in SOCKET_DNS_CALLS:
                self._record(node, "socket", f"{dotted}(...)")
        elif isinstance(func, ast.Name) and func.id == "socket":
            self._record(node, "socket", "socket(...)")

        # obj.connect(...) — sqlite3.connect is a database, not a network
        if name in ("connect", "connect_ex") and _root(func) != "sqlite3":
            self._record(node, "socket", f"{dotted}(...)")

        # asyncio connection and server constructors
        if dotted.startswith("asyncio."):
            member = dotted.split(".", 1)[1]
            if member in ASYNCIO_CALLS:
                self._record(node, "asyncio", f"{dotted}(...)")
        elif isinstance(func, ast.Name) and func.id in ASYNCIO_CALLS:
            self._record(node, "asyncio", f"{func.id}(...)")

        # subprocess executing network binaries: curl, wget, nc
        self._check_subprocess(node)

        # import_module("requests") and friends
        if name in _DYNAMIC_IMPORTERS and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if self._is_outbound(first.value):
                    self._record(
                        node, first.value, f"dynamic import of {first.value!r}"
                    )
        self.generic_visit(node)

    def _check_subprocess(self, node: ast.Call) -> None:
        func = node.func
        dotted = _dotted(func)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")

        is_subp = (
            dotted.startswith("subprocess.")
            or dotted.startswith("asyncio.create_subprocess_")
            or dotted.startswith("os.system")
            or dotted.startswith("os.popen")
            or name in (
                "run",
                "Popen",
                "call",
                "check_call",
                "check_output",
                "create_subprocess_exec",
                "create_subprocess_shell",
                "system",
                "popen",
            )
        )
        if not is_subp:
            return

        cmd_nodes: list[ast.AST] = []
        if node.args:
            cmd_nodes.append(node.args[0])
        for kw in node.keywords:
            if kw.arg in ("args", "cmd"):
                cmd_nodes.append(kw.value)

        for cmd_node in cmd_nodes:
            if isinstance(cmd_node, (ast.List, ast.Tuple)) and cmd_node.elts:
                first = cmd_node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    base_cmd = Path(first.value.strip()).name.lower()
                    if base_cmd in BLOCKED_SUBPROCESS_COMMANDS:
                        self._record(
                            node,
                            "subprocess",
                            f"subprocess executing {first.value!r}",
                        )
            elif isinstance(cmd_node, ast.Constant) and isinstance(cmd_node.value, str):
                tokens = cmd_node.value.strip().split()
                if tokens:
                    base_cmd = Path(tokens[0]).name.lower()
                    if base_cmd in BLOCKED_SUBPROCESS_COMMANDS:
                        self._record(
                            node,
                            "subprocess",
                            f"subprocess executing {tokens[0]!r}",
                        )

    @staticmethod
    def _is_outbound(module: str) -> bool:
        return any(
            module == allowed or module.startswith(allowed + ".")
            for allowed in OUTBOUND_LIBRARIES
        )


def _scan(paths: list[Path]) -> tuple[list[str], set[tuple[str, str]]]:
    """``(descriptions, {(file, primitive)})`` of every network escape found."""
    descriptions: list[str] = []
    found: set[tuple[str, str]] = set()
    for path in paths:
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _NetworkCollector()
        collector.visit(tree)
        for lineno, primitive, detail in collector.hits:
            descriptions.append(f"{relative}:{lineno}: {detail} [{primitive}]")
            found.add((relative, primitive))
    return descriptions, found


class NetworkEscapesArePinned(unittest.TestCase):
    """Only the exact pin may open a connection — the pin is the inventory."""

    def test_source_tree_is_walked(self) -> None:
        files = _source_files()
        self.assertGreater(len(files), 80, "repository walk found suspiciously few files")

    def test_pin_entries_exist(self) -> None:
        missing = sorted(
            (relative, primitive)
            for relative, primitive in ALLOWED_NETWORK_CALLS
            if not (REPO_ROOT / relative).is_file()
        )
        self.assertEqual(
            missing,
            [],
            f"pinned choke points no longer exist (strike them from the pin): {missing}",
        )

    def test_only_the_pinned_modules_open_connections(self) -> None:
        violations, found = _scan(_source_files())
        new = sorted(found - ALLOWED_NETWORK_CALLS)
        gone = sorted(ALLOWED_NETWORK_CALLS - found)
        self.assertEqual(
            (new, gone),
            ([], []),
            "network escapes changed.\n"
            f"  new (unpinned connection — route it through spend_control or "
            f"pin it deliberately): {new}\n"
            f"  resolved (strike from ALLOWED_NETWORK_CALLS): {gone}\n"
            f"  details: {'; '.join(violations)}",
        )


class ResurrectIsHumanOnly(unittest.TestCase):
    """Nothing automatic may call ``resurrect()`` — the allowed set is empty."""

    #: Frozen on purpose: a first automatic caller would defeat the rule that
    #: ``failed`` only leaves by a human decision (manifest.resurrect docstring).
    ALLOWED_RESURRECT_CALLERS: frozenset[str] = frozenset()

    def test_allowed_set_is_empty(self) -> None:
        self.assertEqual(
            self.ALLOWED_RESURRECT_CALLERS,
            frozenset(),
            "resurrect() may not have an automatic caller: no exceptions",
        )

    def test_no_production_module_calls_resurrect(self) -> None:
        offenders: list[str] = []
        scanned = 0
        for path in _production_files():
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", "")
                )
                if name == "resurrect":
                    relative = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertGreater(scanned, 30, "production walk found suspiciously few files")
        self.assertEqual(
            offenders,
            [],
            "resurrect() must stay a human decision (pulse/workers/dispatcher/API "
            "never call it):\n  " + "\n  ".join(offenders),
        )


class CollectorCatchesExtendedBypasses(unittest.TestCase):
    """Synthetic unit tests ensuring AST collector catches all extended bypasses."""

    def _collect(self, snippet: str) -> list[tuple[int, str, str]]:
        tree = ast.parse(snippet)
        collector = _NetworkCollector()
        collector.visit(tree)
        return collector.hits

    def test_catches_extended_network_libraries(self) -> None:
        for lib in ("aiohttp", "smtplib", "ftplib", "telnetlib", "websockets"):
            hits_import = self._collect(f"import {lib}")
            self.assertTrue(any(p == lib for _, p, _ in hits_import), f"Missed: import {lib}")

            hits_from = self._collect(f"from {lib} import something")
            self.assertTrue(any(p == lib for _, p, _ in hits_from), f"Missed: from {lib} import something")

    def test_catches_asyncio_connections(self) -> None:
        hits1 = self._collect("import asyncio\nasyncio.open_connection('127.0.0.1', 80)")
        self.assertTrue(any(p == "asyncio" for _, p, _ in hits1))

        hits2 = self._collect("import asyncio\nasyncio.start_server(None, '127.0.0.1', 80)")
        self.assertTrue(any(p == "asyncio" for _, p, _ in hits2))

        hits3 = self._collect("from asyncio import open_connection\nopen_connection('127.0.0.1', 80)")
        self.assertTrue(any(p == "asyncio" for _, p, _ in hits3))

        hits4 = self._collect("from asyncio import start_server\nstart_server(None, '127.0.0.1', 80)")
        self.assertTrue(any(p == "asyncio" for _, p, _ in hits4))

    def test_catches_subprocess_network_commands(self) -> None:
        for bin_name in ("curl", "wget", "nc"):
            snippet_list = f"import subprocess\nsubprocess.run(['{bin_name}', 'https://example.com'])"
            hits = self._collect(snippet_list)
            self.assertTrue(any(p == "subprocess" for _, p, _ in hits), f"Missed list subprocess: {bin_name}")

            snippet_popen = f"import subprocess\nsubprocess.Popen(['/usr/bin/{bin_name}', 'https://example.com'])"
            hits = self._collect(snippet_popen)
            self.assertTrue(any(p == "subprocess" for _, p, _ in hits), f"Missed popen: {bin_name}")

            snippet_os_system = f"import os\nos.system('{bin_name} https://example.com')"
            hits = self._collect(snippet_os_system)
            self.assertTrue(any(p == "subprocess" for _, p, _ in hits), f"Missed os.system: {bin_name}")

            snippet_os_popen = f"import os\nos.popen('{bin_name} https://example.com')"
            hits = self._collect(snippet_os_popen)
            self.assertTrue(any(p == "subprocess" for _, p, _ in hits), f"Missed os.popen: {bin_name}")


if __name__ == "__main__":
    unittest.main()
