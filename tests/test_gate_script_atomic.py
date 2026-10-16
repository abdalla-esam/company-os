"""Gate queue durability: the human decision path can lose no write.

``gate_script.py`` runs as a subprocess of the API and the tracker scripts, so
these tests exercise it *both ways*:

- end to end through real subprocesses (raise / decide / list), including an
  N-writer race where several processes decide the same gate at once — the
  lock-serialized read-modify-write must let exactly one decision win;
- in-process for fault injection between the temp write and the rename — the
  previous entry must survive byte-for-byte.

The queue directory is isolated through the same ``GATE_QUEUE_DIR`` contract
the API and ``shared_kernel.paths`` use; the in-process tests set the module
global directly.
"""

from __future__ import annotations

from tests import _bootstrap  # noqa: F401  — repository import paths

from tests._bootstrap import REPO_ROOT

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

_GATE_SCRIPT = REPO_ROOT / "data" / "governance" / "approval_gates" / "gate_script.py"

_spec = importlib.util.spec_from_file_location("gate_script_under_test", _GATE_SCRIPT)
assert _spec is not None and _spec.loader is not None
gate_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_script)


def _run_gate(args, *, queue_dir, input_text=None):
    """Run gate_script.py exactly as production does: a fresh subprocess."""
    env = dict(os.environ)
    env["GATE_QUEUE_DIR"] = str(queue_dir)
    return subprocess.run(
        [sys.executable, str(_GATE_SCRIPT), *args],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _raise_payload(reason="test gate"):
    return json.dumps(
        {"source": {"sector": "ideation"}, "risk_tier": "external", "reason": reason}
    )


class GateScriptAtomicWrites(unittest.TestCase):
    """raise / save_entry: atomic, fault-safe, litter-free."""

    def setUp(self) -> None:
        self.queue = Path(tempfile.mkdtemp(prefix="so_test_gate_"))
        self.addCleanup(shutil.rmtree, self.queue, ignore_errors=True)
        self._original_queue = gate_script.QUEUE_DIR
        gate_script.QUEUE_DIR = self.queue
        self.addCleanup(setattr, gate_script, "QUEUE_DIR", self._original_queue)

    def _entry(self, gate_id="G-TEST-1"):
        return {
            "gate_id": gate_id,
            "source": {"sector": "ideation"},
            "risk_tier": "external",
            "reason": "test gate",
            "opened_at": gate_script.now_utc().isoformat(),
            "state": "open",
            "decision": None,
            "decided_by": None,
            "decided_at": None,
            "decision_reason": None,
        }

    def test_raise_writes_durable_entry_via_subprocess(self) -> None:
        proc = _run_gate(["raise"], queue_dir=self.queue, input_text=_raise_payload())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        on_disk = json.loads(Path(gate_script.entry_path(printed["gate_id"])).read_text("utf-8"))
        self.assertEqual(on_disk, printed)
        self.assertEqual(on_disk["state"], "open")
        # no temp litter, and the hidden lock sidecars never gate_script raises
        self.assertEqual(
            [n for n in os.listdir(self.queue) if n.endswith(".tmp")], []
        )

    def test_fault_between_tmp_write_and_rename_keeps_old_entry(self) -> None:
        entry = self._entry()
        gate_script.save_entry(entry)  # the old, known-good file
        target = Path(gate_script.entry_path(entry["gate_id"]))
        before = target.read_bytes()

        closed = dict(entry, state="closed", decision="approved", decided_by="human")
        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                gate_script.save_entry(closed)


        self.assertEqual(target.read_bytes(), before, "old entry must survive a torn write")
        self.assertEqual(
            [n for n in os.listdir(self.queue) if n.endswith(".tmp")], []
        )

    def test_fault_during_decide_keeps_old_entry(self) -> None:
        entry = self._entry()
        gate_script.save_entry(entry)
        target = Path(gate_script.entry_path(entry["gate_id"]))
        before = target.read_bytes()

        args = argparse.Namespace(
            gate_id=entry["gate_id"], by="human_a", decision="approved", reason=None
        )
        with mock.patch("shared_kernel.atomic_io.os.replace", side_effect=OSError("disk fault")):
            with self.assertRaises(OSError):
                gate_script.cmd_decide(args)

        self.assertEqual(target.read_bytes(), before, "a failed decide must not close the gate")

    def test_list_survives_lock_sidecars_and_tmp_files(self) -> None:
        entry = self._entry()
        gate_script.save_entry(entry)
        # simulate debris from concurrent writers
        (self.queue / (".%s.lock" % ("gate_%s.json" % entry["gate_id"]))).write_text("{}", "utf-8")
        (self.queue / (".gate_%s.json.123.tmp" % entry["gate_id"])).write_text("{", "utf-8")
        proc = _run_gate(["list"], queue_dir=self.queue)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(entry["gate_id"], proc.stdout)


class GateScriptSingleDecide(unittest.TestCase):
    """The decide contract the API depends on, through real subprocesses."""

    def setUp(self) -> None:
        self.queue = Path(tempfile.mkdtemp(prefix="so_test_gate_"))
        self.addCleanup(shutil.rmtree, self.queue, ignore_errors=True)

    def _raise(self):
        proc = _run_gate(["raise"], queue_dir=self.queue, input_text=_raise_payload())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_decide_closes_the_gate_and_persists(self) -> None:
        raised = self._raise()
        proc = _run_gate(
            ["decide", "--gate-id", raised["gate_id"], "--by", "human_a",
             "--decision", "approved"],
            queue_dir=self.queue,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        decided = json.loads(proc.stdout)
        self.assertEqual(decided["state"], "closed")
        self.assertEqual(decided["decided_by"], "human_a")
        on_disk = json.loads(
            (self.queue / ("gate_%s.json" % raised["gate_id"])).read_text("utf-8")
        )
        self.assertEqual(on_disk, decided)

    def test_second_decide_is_refused(self) -> None:
        raised = self._raise()
        first = _run_gate(
            ["decide", "--gate-id", raised["gate_id"], "--by", "human_a",
             "--decision", "approved"],
            queue_dir=self.queue,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        second = _run_gate(
            ["decide", "--gate-id", raised["gate_id"], "--by", "human_b",
             "--decision", "rejected", "--reason", "changed my mind"],
            queue_dir=self.queue,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already closed", second.stderr)
        on_disk = json.loads((self.queue / ("gate_%s.json" % raised["gate_id"])).read_text("utf-8"))
        self.assertEqual(on_disk["decided_by"], "human_a", "the first decision must stand")

    def test_decide_missing_gate_is_refused(self) -> None:
        proc = _run_gate(
            ["decide", "--gate-id", "G-NOPE", "--by", "human_a", "--decision", "approved"],
            queue_dir=self.queue,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no such gate entry", proc.stderr)

    def test_decide_clock_skew_is_refused(self) -> None:
        entry = {
            "gate_id": "G-SKEW",
            "source": {"sector": "ideation"},
            "reason": "skew",
            "opened_at": "2099-01-01T00:00:00+00:00",
            "state": "open",
            "decision": None,
            "decided_by": None,
            "decided_at": None,
            "decision_reason": None,
        }
        (self.queue / "gate_G-SKEW.json").write_text(json.dumps(entry), "utf-8")
        proc = _run_gate(
            ["decide", "--gate-id", "G-SKEW", "--by", "human_a", "--decision", "approved"],
            queue_dir=self.queue,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("clock skew", proc.stderr)


class GateScriptConcurrentDecide(unittest.TestCase):
    """N writers, one gate: exactly one decision may win."""

    def setUp(self) -> None:
        self.queue = Path(tempfile.mkdtemp(prefix="so_test_gate_"))
        self.addCleanup(shutil.rmtree, self.queue, ignore_errors=True)

    def test_parallel_decides_lose_no_update(self) -> None:
        proc = _run_gate(["raise"], queue_dir=self.queue, input_text=_raise_payload())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        gate_id = json.loads(proc.stdout)["gate_id"]

        def _decide(human):
            return human, _run_gate(
                ["decide", "--gate-id", gate_id, "--by", human, "--decision", "approved"],
                queue_dir=self.queue,
            )

        humans = ["human_%02d" % i for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(_decide, humans))

        winners = [(h, p) for h, p in results if p.returncode == 0]
        losers = [(h, p) for h, p in results if p.returncode != 0]
        self.assertEqual(len(winners), 1, "exactly one human may decide a gate")
        for _h, proc in losers:
            self.assertIn("already closed", proc.stderr)

        on_disk = json.loads((self.queue / ("gate_%s.json" % gate_id)).read_text("utf-8"))
        self.assertEqual(on_disk["state"], "closed")
        self.assertEqual(on_disk["decided_by"], winners[0][0], "disk must match the winning write")
        self.assertEqual(on_disk, json.loads(winners[0][1].stdout))


if __name__ == "__main__":
    unittest.main()
