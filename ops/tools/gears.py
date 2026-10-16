#!/usr/bin/env python3
"""gears.py — CLI for inspecting and probing the 16 Constitutional Gears.

Usage:
  # 1. List all 16 constitutional gears and their active endpoints:
  python3 tools/gears.py list

  # 2. Probe a specific gear (measures live latency & response):
  python3 tools/gears.py probe text:review
  python3 tools/gears.py probe social:publish

  # 3. Probe all 16 gears in a single run:
  python3 tools/gears.py probe-all

  # 4. Output machine-readable JSON:
  python3 tools/gears.py list --json
  python3 tools/gears.py probe text:draft --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
for _p in (str(REPO_ROOT), str(ENGINE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from capsule_engine.gear_registry import (
    CONSTITUTIONAL_GEARS,
    get_gear_info,
    probe_gear,
    registry_summary,
)

# Terminal colors
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"


def colorize(text: str, color: str, raw: bool = False) -> str:
    if raw or not sys.stdout.isatty():
        return text
    return f"{color}{text}{C_RESET}"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "No entries."
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * widths[i] for i in range(len(headers)))
    formatted_rows = [
        " | ".join(str(val).ljust(widths[i]) for i, val in enumerate(row))
        for row in rows
    ]
    return f"{header_line}\n{sep_line}\n" + "\n".join(formatted_rows)


def print_banner(raw: bool = False):
    if raw or not sys.stdout.isatty():
        return
    print(colorize("╔════════════════════════════════════════════════════════════════╗", C_CYAN))
    print(colorize("║     SOVEREIGN CONSTITUTIONAL GEARS REGISTRY & TOOL BRIDGE      ║", C_BOLD + C_CYAN))
    print(colorize("║     16 Verbs · Unified Endpoint Mapping · Zero-Coupling Seams  ║", C_CYAN))
    print(colorize("╚════════════════════════════════════════════════════════════════╝", C_CYAN))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sovereign Gears Management CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # List command
    list_p = subparsers.add_parser("list", help="List all 16 gears and their endpoints")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Output pure JSON")

    # Probe command
    probe_p = subparsers.add_parser("probe", help="Probe a specific gear verb")
    probe_p.add_argument("verb", help="Verb to probe (e.g. text:review, social:publish)")
    probe_p.add_argument("--timeout", type=float, default=3.0, help="Probe timeout in seconds")
    probe_p.add_argument("--json", action="store_true", dest="as_json", help="Output pure JSON")

    # Probe-all command
    probe_all_p = subparsers.add_parser("probe-all", help="Probe all 16 gears sequentially")
    probe_all_p.add_argument("--timeout", type=float, default=2.0, help="Per-gear timeout in seconds")
    probe_all_p.add_argument("--json", action="store_true", dest="as_json", help="Output pure JSON")

    # Backward compatibility: default to list if no args provided or if --json is passed
    if len(sys.argv) == 1:
        argv = ["list"]

    args = parser.parse_args(argv)

    if args.command == "list" or not args.command:
        summary = registry_summary()
        if getattr(args, "as_json", False):
            print(json.dumps(summary, indent=2))
            return 0

        print_banner()
        mode = "OFFLINE (Deterministic)" if summary["is_offline_mode"] else "LIVE"
        print(f"\n⚙️ Active Operational Mode: {colorize(mode, C_BOLD + C_GREEN if summary['is_offline_mode'] else C_CYAN)}")
        print(f"Total Constitutional Gears: {summary['total_gears']} | HTTP: {summary['http_gears']} | Offline: {summary['offline_gears']} | Unconfigured: {summary['unconfigured_gears']}\n")

        headers = ["Verb", "Transport", "Status", "Endpoint URL / Env Variable", "Purpose"]
        rows = []
        for g in summary["gears"]:
            status_text = "✓ READY" if g["is_configured"] else "✗ UNCONFIGURED"
            endpoint_str = g["endpoint"] or g["env_var"]
            rows.append([
                g["verb"],
                g["transport"],
                status_text,
                endpoint_str[:38] + ("..." if len(endpoint_str) > 38 else ""),
                g["purpose"][:40] + ("..." if len(g["purpose"]) > 40 else ""),
            ])

        print(format_table(headers, rows))
        return 0

    if args.command == "probe":
        verb = args.verb
        res = probe_gear(verb, timeout_seconds=args.timeout)
        if getattr(args, "as_json", False):
            print(json.dumps(res, indent=2))
            return 0 if res.get("ok") else 1

        print_banner()
        status_color = C_GREEN if res.get("ok") else (C_YELLOW if res.get("status") == "refused" else C_RED)
        print(f"\n🔬 Probing Gear: {colorize(verb, C_BOLD)}")
        print(f"  Status:       {colorize(res.get('status', 'unknown').upper(), status_color)}")
        print(f"  Transport:    {res.get('transport')}")
        print(f"  Latency:      {res.get('latency_ms', 0.0)} ms")
        if "tool" in res:
            print(f"  Tool Name:    {res['tool']}")
        if "error" in res:
            print(colorize(f"  Error Detail: {res['error']}", C_RED))
        if "code" in res:
            print(f"  Refusal Code: {res['code']} ({res.get('detail')})")
        if "receipt" in res:
            print(f"  Receipt ID:   {res['receipt']}")
        return 0 if res.get("ok") else 1

    if args.command == "probe-all":
        results = []
        print_banner()
        print(colorize("\n🔬 Probing All 16 Constitutional Gears...\n", C_BOLD))
        for verb in CONSTITUTIONAL_GEARS:
            res = probe_gear(verb, timeout_seconds=args.timeout)
            results.append(res)

        if getattr(args, "as_json", False):
            print(json.dumps({"probes": results}, indent=2))
            return 0

        headers = ["Verb", "Status", "Latency", "Transport", "Tool / Error"]
        rows = []
        for r in results:
            detail = r.get("tool") or r.get("error") or r.get("code") or "-"
            rows.append([
                r["verb"],
                r.get("status", "unknown").upper(),
                f"{r.get('latency_ms', 0)} ms",
                r.get("transport", "-"),
                str(detail)[:35],
            ])
        print(format_table(headers, rows))
        all_ok = all(r.get("ok") for r in results)
        return 0 if all_ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
