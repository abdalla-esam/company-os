#!/usr/bin/env python3
"""ticket_trial.py — Interactive CLI and Automation Runner for Capsule Sandbox Trials.

Usage:
  # 1. Run isolated trial for a ticket (offline by default, zero network side-effects):
  python3 tools/ticket_trial.py --ticket T-20261006T110747Z-c600a473

  # 2. Run trial with full auto-approval and publishing pipeline verification:
  python3 tools/ticket_trial.py --ticket T-20261006T110747Z-c600a473 --auto-approve

  # 3. List all trial capsules currently inside the sandbox:
  python3 tools/ticket_trial.py --list

  # 4. Deep-inspect a trial capsule:
  python3 tools/ticket_trial.py --inspect sbx_ideation_T-20261006T110747Z-c600a473_20261011_103000

  # 5. Promote a proven, verified trial capsule to production spool:
  python3 tools/ticket_trial.py --promote sbx_ideation_T-20261006T110747Z-c600a473_20261011_103000 --by admin --note "Verified by leadership"

  # 6. Purge sandbox trials:
  python3 tools/ticket_trial.py --clean

  # 7. Output pure JSON for API / automation:
  python3 tools/ticket_trial.py --ticket <id> --json
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

from capsule_engine.sandbox import CapsuleSandbox, TicketNotFoundError


# ANSI Color formatting
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"


def colorize(text: str, color: str, raw: bool = False) -> str:
    if raw or not sys.stdout.isatty():
        return text
    return f"{color}{text}{C_RESET}"


def print_banner(raw: bool = False):
    if raw or not sys.stdout.isatty():
        return
    print(colorize("╔════════════════════════════════════════════════════════════════╗", C_CYAN))
    print(colorize("║     SOVEREIGN CAPSULE SANDBOX — TICKET TRIAL RUNNER            ║", C_BOLD + C_CYAN))
    print(colorize("║     Zero-Pollution Isolated Factory Verification & Trial Spool ║", C_CYAN))
    print(colorize("╚════════════════════════════════════════════════════════════════╝", C_CYAN))


def format_table(headers: list[str], rows: list[list[str]], raw: bool = False) -> str:
    if not rows:
        return "No entries found."
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sovereign Agency OS — Capsule Sandbox & Ticket Trial Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ticket", help="Ticket ID to run a trial for")
    parser.add_argument("--sector", help="Domain sector (marketing, ideation, etc.)")
    parser.add_argument("--live", action="store_true", help="Run against live models/channels (requires --yes)")
    parser.add_argument("--yes", action="store_true", help="Confirm irreversible live execution")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-sign approval gate to test full publishing")
    parser.add_argument("--reviewer", default="trial_operator", help="Reviewer identity for approvals")
    parser.add_argument("--promote", help="Promote a verified sandbox capsule ID to production spool")
    parser.add_argument("--by", default="operator", help="User performing promotion")
    parser.add_argument("--note", default="", help="Audit note for promotion")
    parser.add_argument("--list", action="store_true", help="List all capsules currently in the sandbox")
    parser.add_argument("--inspect", help="Inspect a specific sandbox capsule ID")
    parser.add_argument("--clean", action="store_true", help="Purge sandbox capsules (preserving .gitkeep)")
    parser.add_argument("--keep", type=int, default=0, help="Number of newest trials to keep when cleaning")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Output pure machine-readable JSON")

    args = parser.parse_args(argv)
    sandbox = CapsuleSandbox()

    if args.live and not args.yes:
        err = {"error": "Refusing a live trial without --yes confirmation.", "hint": "add --live --yes"}
        if args.as_json:
            print(json.dumps(err, indent=2))
        else:
            print(colorize(f"Error: {err['error']}", C_RED), file=sys.stderr)
        return 2

    # --- Mode 1: List Sandbox Capsules ---
    if args.list:
        capsules = sandbox.list_sandbox_capsules()
        if args.as_json:
            print(json.dumps({"sandbox_capsules": capsules, "count": len(capsules)}, indent=2))
            return 0
        print_banner()
        print(colorize(f"\n📂 Capsules in Sandbox ({len(capsules)}):", C_BOLD))
        if not capsules:
            print("  (Sandbox is clean and empty)")
            return 0
        headers = ["Capsule ID", "State", "Sector", "Source Ticket", "Copy", "Image", "Created At"]
        rows = [
            [
                c["capsule_id"],
                c["state"] or "unknown",
                c["domain"] or "-",
                c.get("source_ticket_id") or "-",
                "✓" if c.get("has_copy") else "-",
                "✓" if c.get("has_image") else "-",
                (c.get("created_at") or "")[:19],
            ]
            for c in capsules
        ]
        print(format_table(headers, rows))
        return 0

    # --- Mode 2: Inspect Capsule ---
    if args.inspect:
        try:
            inspection = sandbox.inspect_sandbox_capsule(args.inspect)
        except Exception as exc:
            if args.as_json:
                print(json.dumps({"error": str(exc)}, indent=2))
            else:
                print(colorize(f"Error: {exc}", C_RED), file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(inspection, indent=2))
            return 0
        print_banner()
        print(colorize(f"\n🔍 Sandbox Capsule Inspection: {args.inspect}", C_BOLD))
        print(f"  State:               {colorize(inspection['state'], C_GREEN if inspection['state'] in ('approved', 'published') else C_YELLOW)}")
        print(f"  Domain:              {inspection['domain']}")
        print(f"  Source Ticket:       {inspection.get('source_ticket_id') or '-'}")
        print(f"  Artifact SHA-256:    {inspection.get('artifact_sha256') or '-'}")
        print(f"  Audit Chain Intact:  {'✓ YES' if inspection.get('audit_chain_intact') else '✗ NO'}")
        print("\n  📦 Artifacts:")
        for a in inspection.get("artifacts", []):
            print(f"    • {a['name']} ({a['size_bytes']} bytes) -> sha256:{a['sha256'][:16]}...")
        return 0

    # --- Mode 3: Clean Sandbox ---
    if args.clean:
        deleted = sandbox.clean_sandbox(keep_recent=args.keep)
        if args.as_json:
            print(json.dumps({"cleaned": deleted, "kept": args.keep}, indent=2))
        else:
            print(colorize(f"🧹 Cleaned {deleted} trial capsule(s) from sandbox. Kept: {args.keep}.", C_GREEN))
        return 0

    # --- Mode 4: Promote Sandbox Capsule to Production Spool ---
    if args.promote:
        try:
            res = sandbox.promote(args.promote, by=args.by, note=args.note)
        except Exception as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(colorize(f"Promotion failed: {exc}", C_RED), file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(res, indent=2))
        else:
            print_banner()
            print(colorize(f"\n🚀 PROMOTION SUCCESSFUL", C_BOLD + C_GREEN))
            print(f"  Capsule ID:   {res['capsule_id']}")
            print(f"  Target Spool: {res['spool_path']}")
            print(f"  State:        {res['state']}")
            print(f"  Promoted By:  {res['promoted_by']}")
            print(f"  Timestamp:    {res['promoted_at']}")
            print(colorize("  The capsule is now live in production spool.\n", C_GREEN))
        return 0

    # --- Mode 5: Run Ticket Trial ---
    if args.ticket:
        if not args.as_json:
            print_banner()
            mode_desc = colorize("LIVE (Meta/Directus)", C_MAGENTA) if args.live else colorize("OFFLINE (Deterministic/Zero-Cost)", C_CYAN)
            print(f"\n▶ Starting trial for ticket: {colorize(args.ticket, C_BOLD)} [{mode_desc}]")

        try:
            report = sandbox.run_trial(
                ticket_id=args.ticket,
                sector=args.sector,
                live=args.live,
                reviewer=args.reviewer,
                auto_approve=args.auto_approve,
            )
        except TicketNotFoundError as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(colorize(f"\n✗ Error: {exc}", C_RED), file=sys.stderr)
            return 1
        except Exception as exc:
            if args.as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(colorize(f"\n✗ Trial execution error: {exc}", C_RED), file=sys.stderr)
            return 1

        if args.as_json:
            print(json.dumps(report, indent=2))
            return 0 if report.get("ok") else 1

        # Rich interactive output
        status_color = C_GREEN if report.get("ok") else C_RED
        print(colorize("\n═══════════════════ TRIAL RESULTS ═══════════════════", C_BOLD))
        print(f"  Status:             {colorize('PASSED' if report.get('ok') else 'HELD / FAILED', status_color)}")
        print(f"  Capsule ID:         {report['capsule_id']}")
        print(f"  Sector:             {report['sector']}")
        print(f"  Final State:        {colorize(report['state'], C_BOLD + C_YELLOW)}")
        print(f"  Transitions:        {report['transitions_count']} steps")
        print(f"  Deliverable SHA256: {report.get('artifact_sha256') or '-'}")

        print(colorize("\n  📜 Transition History:", C_BOLD))
        for t in report.get("transitions", []):
            arrow = f"{t.get('from') or 'start'} -> {t.get('to')}"
            print(f"    • [{t.get('by') or 'sys'}] {arrow}: {t.get('why') or ''}")

        print(colorize("\n  📦 Artifacts Generated:", C_BOLD))
        for fname, finfo in report.get("artifacts", {}).items():
            print(f"    • {fname} ({finfo['size_bytes']} bytes)")
            if "preview" in finfo:
                snippet = finfo["preview"].replace("\n", " ")
                print(f"      preview: \"{snippet[:100]}...\"")

        if report.get("state") == "pending_approval":
            print(colorize("\n  🔒 Gate Status: Held at pending_approval (Constitutional Manager Gate).", C_YELLOW))
            print(f"     To test full publish flow, re-run with: --auto-approve")
            print(f"     To promote to production spool, run:   --promote {report['capsule_id']}")
        elif report.get("state") in ("approved", "published"):
            print(colorize(f"\n  ✓ Factory Pipeline Completed ({report.get('state')}).", C_GREEN))
            print(f"     Ready for production promotion: --promote {report['capsule_id']}")

        print(colorize("═════════════════════════════════════════════════════\n", C_BOLD))
        return 0 if report.get("ok") else 1

    # Default help if no action provided
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
