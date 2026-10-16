"""The ops shell entrypoints must name files that exist.

A shell script that runs ``python3 <path>`` is invisible to both contracts we
already enforce: the import contract reads python sources, and
``test_subprocess_entrypoints.py`` reads the python launchers. Nothing fails at
build time when the tree moves underneath a shell script — the only symptom is
a service that logs a missing file on every beat.

That is exactly what happened when the packages moved to ``engine/``:
``ops/bootstrap/entrypoint.sh`` kept calling ``orchestration/pulse.py`` while
the loop reported a failed beat every ``PULSE_INTERVAL`` seconds, and
``company-engine-pulse.service`` stayed ``active`` the whole time because the
loop tolerates a failed beat by design. An enabled, running, useless gear.

This test reads the shell entrypoints, expands the tree-locating variables they
define, and asserts every python file they invoke resolves inside the repo.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

from tests._bootstrap import REPO_ROOT

import re
import unittest
from pathlib import Path

#: Shell scripts that start engine processes — on a host (systemd) or in the
#: container (``ops/bootstrap/compose.yml``). Every one of them is a place a
#: stale path can hide.
ENTRYPOINTS = (
    "ops/bootstrap/entrypoint.sh",
    "ops/bootstrap/up.sh",
    "ops/server_boot.sh",
    "ops/deploy/install.sh",
    "ops/deploy/preflight.sh",
)

#: ``python3 <path>.py`` — quoted or bare, anywhere on a line.
INVOCATION = re.compile(r"\bpython3?\s+[\"']?(?P<path>[^\s\"'|;&)]+\.py)")

#: ``$ROOT`` is the repository root (``entrypoint.sh`` derives it from its own
#: path: ``dirname($0)/../..``); ``$HERE`` is the script's own directory
#: (``up.sh``). Both are the documented convention, not guesses.
ROOT_VARS = ("${ROOT}", "$ROOT")
HERE_VARS = ("${HERE}", "$HERE")


def _resolve(raw: str, script: Path) -> Path | None:
    """Expand a shell path expression, or ``None`` when it is not resolvable.

    Anything still holding a ``$`` after the known conventions are expanded is
    a variable this test cannot reason about (``$PREFIX``, ``$VENV``, ...): it
    is skipped rather than failed, because the point is to catch a path that
    moved, not to reimplement the shell.
    """
    target = raw
    for var in ROOT_VARS:
        target = target.replace(var, str(REPO_ROOT))
    for var in HERE_VARS:
        target = target.replace(var, str(script.parent))
    if "$" in target:
        return None
    candidate = Path(target)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


class OpsEntrypointsNameRealFiles(unittest.TestCase):
    def test_every_invoked_script_exists(self) -> None:
        checked = 0
        for rel in ENTRYPOINTS:
            script = REPO_ROOT / rel
            with self.subTest(entrypoint=rel):
                self.assertTrue(script.is_file(), f"entrypoint missing: {rel}")
                for raw in INVOCATION.findall(script.read_text(encoding="utf-8")):
                    resolved = _resolve(raw, script)
                    if resolved is None:
                        continue
                    checked += 1
                    self.assertTrue(
                        resolved.is_file(),
                        f"{rel} runs {raw!r} but {resolved} does not exist",
                    )
        # A parser that silently matches nothing would pass forever.
        self.assertGreater(checked, 0, "no python invocations parsed — the pattern is stale")


if __name__ == "__main__":
    unittest.main()
