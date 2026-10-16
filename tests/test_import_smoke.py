"""Import smoke test: every module under engine/, ops/ and data/ imports.

The behavioural suite exercises endpoint *bodies*, but a module move changes
import paths — including imports that live inside function bodies (lazy
imports) which the suite never executes until the route is called. This test
imports every ``.py`` file under the three source roots directly, each with
its own directory on ``sys.path`` (mirroring how the file is executed in
production: scripts import their siblings, engine modules import packages).

A failure here names the exact file and exception, so a stale import left
behind by a move is caught immediately instead of at first API call.
"""

from tests import _bootstrap  # noqa: F401  — isolated vault before engine imports

import importlib.util
import sys
import unittest
from pathlib import Path

from tests._bootstrap import REPO_ROOT

SOURCE_ROOTS = ("engine", "ops", "data")

# The interpreter path production boots with (see ops/server_boot.sh): engine
# and the repo root. `data/` is added for the domain package tree, exactly as
# orchestration/api.py does.
PACKAGE_ROOTS = (
    str(REPO_ROOT / "engine"),
    str(REPO_ROOT),
    str(REPO_ROOT / "data"),
)


def _module_files() -> list[Path]:
    """Every importable module file under the three source roots (sorted)."""
    paths: list[Path] = []
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if path.name == "__init__.py":
                continue
            paths.append(path)
    return paths


class EveryModuleImports(unittest.TestCase):
    """One subTest per module so a break names the file in the failure."""

    def test_source_tree_is_walked(self):
        files = _module_files()
        # Guards against a broken walk silently turning the smoke test into a
        # no-op: the repository has dozens of modules and keeps growing.
        self.assertGreater(len(files), 50, "module walk found suspiciously few files")

    def test_every_module_imports(self):
        failures: list[str] = []
        for path in _module_files():
            relative = path.relative_to(REPO_ROOT).as_posix()
            name = "smoke_" + "_".join(
                path.relative_to(REPO_ROOT).with_suffix("").parts
            )
            added: list[str] = []
            for entry in (str(path.parent), *PACKAGE_ROOTS):
                if entry not in sys.path:
                    sys.path.insert(0, entry)
                    added.append(entry)
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                self.assertIsNotNone(spec, f"no import spec for {relative}")
                self.assertIsNotNone(spec.loader, f"no loader for {relative}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 — report every failure kind
                failures.append(f"{relative}: {type(exc).__name__}: {exc}")
            finally:
                for entry in added:
                    try:
                        sys.path.remove(entry)
                    except ValueError:
                        pass
        self.assertEqual(
            failures,
            [],
            "modules failed to import:\n  " + "\n  ".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
