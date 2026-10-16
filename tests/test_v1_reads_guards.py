"""M1a: the read model's performance and honesty guards.

Three properties no single endpoint test can show:

- **no N+1.** Listing capsules, tickets, attention or the home opens **zero**
  manifests on the index path and issues a *constant* number of SQL statements —
  the count is measured twice, with a different number of rows in the vault, and
  must not move. A detail opens exactly the one manifest it serves.
- **the index is optional.** With ``AGENCY_INDEX=off`` the same list and detail
  come from the file scan, field for field (only the clock differs).
- **reads are pure.** Every ``GET`` leaves the spool, the domains tree and the
  gate queue byte- and mtime-identical — a read can never change the factory it
  describes.

This module reads; it never decides.
"""

from __future__ import annotations  # noqa: E402 — the suite runs on 3.9

from tests.v1_reads_fixtures import (  # noqa: E402
    MARKETING,
    V1ReadsCase,
    create_capsule,
)

import hashlib
import os
from pathlib import Path
from unittest import mock

import capsule_engine
import capsule_engine.manifest
from orchestration import read_index
from tests._bootstrap import DOMAINS, GATE_QUEUE, SPOOL


def _vault_state() -> dict:
    """Every file under the trees a read may touch: bytes + mtime + digest."""
    state: dict = {}
    for root in (SPOOL, DOMAINS, GATE_QUEUE):
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file():
                continue
            try:
                payload = path.read_bytes()
                stat = path.stat()
            except OSError:  # a file that vanished mid-scan is itself a change
                state[str(path)] = "missing"
                continue
            state[str(path)] = (
                len(payload),
                stat.st_mtime_ns,
                hashlib.sha256(payload).hexdigest(),
            )
    return state


def _without_clock(value):
    """Drop the fields that legitimately differ between two requests."""
    if isinstance(value, list):
        return [_without_clock(entry) for entry in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_clock(entry)
        for key, entry in value.items()
        # ``source`` and ``source_detail`` are *supposed* to differ between the
        # two paths — that is the honesty field working — so they are asserted
        # separately, never compared here.
        if key not in ("as_of", "age_seconds", "source", "source_detail")
    }


class ConstantQueriesAndNoManifestReads(V1ReadsCase):
    """The counting test: rows may grow, the work per request may not."""

    def _measure(self, route: str, headers: dict = None, **params: object):
        """Serve one request, counting manifest loads and SQL statements."""
        manifests: list = []
        statements: list = []
        real_load = capsule_engine.manifest.load_manifest
        real_connect = read_index._connect

        def counting_load(*args: object, **kwargs: object):
            manifests.append(args)
            return real_load(*args, **kwargs)

        class _CountingConnection:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, statement, *args, **kwargs):
                statements.append(" ".join(str(statement).split())[:60])
                return self._conn.execute(statement, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        patches = [
            mock.patch.object(capsule_engine.manifest, "load_manifest", counting_load),
            mock.patch.object(
                read_index, "_connect", lambda *a, **k: _CountingConnection(real_connect(*a, **k))
            ),
        ]
        if hasattr(capsule_engine, "load_manifest"):
            patches.append(mock.patch.object(capsule_engine, "load_manifest", counting_load))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        try:
            response = self.get(route, headers or self.root_headers, **params)
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            for patch in reversed(patches):
                patch.stop()
        return len(manifests), len(statements)

    def test_lists_open_no_manifest_and_their_query_count_does_not_grow(self) -> None:
        routes = (
            ("/v1/capsules", {"limit": 100}),
            ("/v1/tickets", {"limit": 100}),
            ("/v1/attention", {"limit": 100}),
            ("/v1/overview", {}),
        )
        baseline = {route: self._measure(route, **params) for route, params in routes}

        # More rows in the vault: the *work* per request must not change. This is
        # the N+1 guard — a per-row query or a per-row manifest read would move
        # one of these numbers with the data, not with the page size.
        for index in range(3):
            created = f"m1a_nplus1_{os.getpid()}_{index}"
            create_capsule(created, MARKETING)
            self._created_capsules.append(created)
        read_index.rebuild()

        for route, params in routes:
            with self.subTest(route=route):
                manifests, statements = self._measure(route, **params)
                self.assertEqual(
                    manifests, 0, "a list or aggregate read must not open a manifest"
                )
                self.assertEqual(
                    statements,
                    baseline[route][1],
                    "the query count grew with the data — that is an N+1",
                )
                self.assertGreater(baseline[route][1], 0, "the index was really used")
                self.assertEqual(baseline[route][0], 0)

    def test_a_detail_opens_exactly_the_one_manifest_it_serves(self) -> None:
        for route, headers in (
            (f"/v1/capsules/{self.gated.name}", self.marketing_approver),
            (f"/v1/capsules/{self.gated.name}/trace", self.marketing_approver),
        ):
            with self.subTest(route=route):
                manifests, statements = self._measure(route, headers)
                self.assertEqual(manifests, 1, "exactly one manifest, never a scan")
                self.assertLessEqual(statements, 4, "and no per-row query storm")

    def test_a_ticket_detail_is_read_through_the_index_not_a_vault_walk(self) -> None:
        manifests, statements = self._measure(
            f"/v1/tickets/{self.marketing_ticket}", self.marketing_approver
        )
        self.assertEqual(manifests, 0, "the ticket path came from the index")
        self.assertLessEqual(statements, 4)


class IndexOffEqualsIndexOn(V1ReadsCase):
    def test_the_work_list_is_the_same_from_files(self) -> None:
        indexed = self.json_of("/v1/capsules", self.root_headers, limit=100)
        filtered = self.json_of(
            "/v1/capsules", self.root_headers, stage="human", limit=100
        )
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of("/v1/capsules", self.root_headers, limit=100)
            filtered_from_files = self.json_of(
                "/v1/capsules", self.root_headers, stage="human", limit=100
            )
        self.assertEqual(indexed["source"], "index")
        self.assertEqual(from_files["source"], "files")
        self.assertEqual(_without_clock(indexed), _without_clock(from_files))
        self.assertEqual(
            _without_clock(filtered), _without_clock(filtered_from_files)
        )
        self.assertTrue(read_index.enabled(), "the kill switch was restored")

    def test_a_capsule_detail_is_the_same_from_files(self) -> None:
        indexed = self.json_of(f"/v1/capsules/{self.gated.name}", self.marketing_approver)
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of(
                f"/v1/capsules/{self.gated.name}", self.marketing_approver
            )
        # The detail always reads the file, so even the clock-free body must be
        # byte-equal: only ``as_of`` (and the age derived from it) may differ.
        self.assertEqual(_without_clock(indexed), _without_clock(from_files))
        self.assertEqual(indexed["source"], from_files["source"])

    def test_the_attention_list_is_the_same_from_files(self) -> None:
        indexed = self.json_of("/v1/attention", self.root_headers, limit=100)
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of("/v1/attention", self.root_headers, limit=100)
        self.assertEqual(indexed["source"], "index")
        self.assertEqual(from_files["source"], "files")
        self.assertEqual(_without_clock(indexed), _without_clock(from_files))
        # Same items, same order, same can_act: the path changed, the answer did
        # not. The gate queue is file-backed either way, and says so.
        self.assertEqual(
            [item["entity"] for item in indexed["items"]],
            [item["entity"] for item in from_files["items"]],
        )
        self.assertEqual(
            from_files["source_detail"],
            {"capsules": "files", "tickets": "files", "gates": "files"},
        )

    def test_the_trace_is_the_same_from_files(self) -> None:
        indexed = self.json_of(
            f"/v1/capsules/{self.gated.name}/trace", self.marketing_approver, limit=10
        )
        with mock.patch.dict(os.environ, {"AGENCY_INDEX": "off"}):
            from_files = self.json_of(
                f"/v1/capsules/{self.gated.name}/trace",
                self.marketing_approver,
                limit=10,
            )
        self.assertEqual(_without_clock(indexed), _without_clock(from_files))


class EveryReadIsPure(V1ReadsCase):
    def test_every_get_leaves_the_vault_byte_and_mtime_identical(self) -> None:
        routes = (
            ("/v1/me", self.marketing_headers, {}),
            ("/v1/me", self.root_headers, {}),
            ("/v1/overview", self.root_headers, {}),
            ("/v1/overview", self.marketing_headers, {}),
            ("/v1/attention", self.root_headers, {"limit": 50}),
            ("/v1/capsules", self.root_headers, {"limit": 50}),
            ("/v1/capsules", self.marketing_approver, {"needs_me": "true"}),
            (f"/v1/capsules/{self.gated.name}", self.marketing_approver, {}),
            (f"/v1/capsules/{self.gated.name}/review", self.marketing_approver, {}),
            (f"/v1/capsules/{self.gated.name}/trace", self.marketing_approver, {}),
            (f"/v1/capsules/{self.halted.name}", self.root_headers, {}),
            ("/v1/tickets", self.root_headers, {"limit": 50}),
            ("/v1/tickets", self.marketing_headers, {"limit": 50}),
            (f"/v1/tickets/{self.marketing_ticket}", self.marketing_approver, {}),
            (f"/v1/tickets/{self.marketing_ticket}/review", self.marketing_approver, {}),
            # Refusals write nothing either.
            (f"/v1/capsules/{self.development_capsule.name}", self.marketing_headers, {}),
            ("/v1/capsules", self.root_headers, {"cursor": "not-a-cursor"}),
        )
        read_index.rebuild()
        before = _vault_state()
        for route, headers, params in routes:
            with self.subTest(route=route):
                response = self.get(route, headers, **params)
                self.assertIn(response.status_code, (200, 201, 403, 404, 422), response.text)
        self.assertEqual(
            _vault_state(),
            before,
            "a GET changed a file under the spool, the domains tree or the gate queue",
        )

    def test_a_read_never_rebuilds_the_index(self) -> None:
        # A rebuild is the one thing a read could do that *is* expensive: it
        # scans the whole vault. Counted here independently of the SQL counter.
        read_index.rebuild()
        with mock.patch.object(
            read_index, "rebuild", side_effect=AssertionError("a read rebuilt the index")
        ):
            self.json_of("/v1/overview", self.root_headers)
            self.json_of("/v1/attention", self.root_headers, limit=50)
            self.json_of("/v1/capsules", self.root_headers, limit=50)
            self.json_of("/v1/tickets", self.root_headers, limit=50)


if __name__ == "__main__":
    import unittest

    unittest.main()
