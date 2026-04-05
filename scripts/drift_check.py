#!/usr/bin/env python3
"""Infrastructure drift detection CLI.

Detects drift between live AWS state and Terraform-defined expected state,
optionally fixes low/medium-risk issues with confirmation, and can sync
AWS_STATE.md with discovered values.

Usage::

    # Detection only (default)
    python scripts/drift_check.py

    # Auto-fix low/medium risk issues (prompts for each)
    python scripts/drift_check.py --fix

    # Sync AWS_STATE.md with discovered values
    python scripts/drift_check.py --sync-docs

    # Both
    python scripts/drift_check.py --fix --sync-docs

    # Specific checks only
    python scripts/drift_check.py --check ecs sqs

    # JSON output (for CI)
    python scripts/drift_check.py --json

    # Also run terraform plan
    python scripts/drift_check.py --include-tf-plan

    # Exit code: 1=RED, 2=YELLOW, 0=GREEN
    python scripts/drift_check.py --exit-code
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root: python scripts/drift_check.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.debug.drift_checks import run_drift_checks  # noqa: E402

# ---------------------------------------------------------------------------
# ANSI colors for terminal output
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"

STATUS_COLORS = {"green": GREEN, "yellow": YELLOW, "red": RED}


def _color(status: str, text: str) -> str:
    return f"{STATUS_COLORS.get(status, '')}{text}{RESET}"


# ---------------------------------------------------------------------------
# Table output
# ---------------------------------------------------------------------------
def print_results(results: dict) -> None:
    """Print drift check results as a formatted table."""
    components = results.get("components", {})
    overall = results.get("overall", "red")

    print(f"\n{BOLD}Infrastructure Drift Report{RESET}")
    print(f"Checked at: {results.get('checked_at', 'unknown')}")
    print(f"Overall: {_color(overall, overall.upper())}")
    print()

    # Component summary table
    print(f"{'Component':<28} {'Status':<10} {'Message'}")
    print("-" * 80)
    for comp_name, comp in sorted(components.items()):
        status = comp.get("status", "red")
        message = comp.get("message", "")
        print(f"{comp_name:<28} {_color(status, status.upper()):<19} {message}")

    # Detailed checks for non-green components
    for comp_name, comp in sorted(components.items()):
        if comp.get("status") == "green":
            continue
        checks = comp.get("checks", [])
        if not checks:
            continue

        print(f"\n{BOLD}{comp_name}{RESET} — failed checks:")
        for check in checks:
            if check.get("passed"):
                continue
            marker = f"{RED}FAIL{RESET}"
            print(
                f"  {marker} {check['check']}: expected={check['expected']}, actual={check['actual']}"
            )

    # Fix summary
    fix_summary = results.get("fix_summary", {})
    total = fix_summary.get("total", 0)
    if total > 0:
        print(
            f"\n{BOLD}Fixes available:{RESET} {total} total "
            f"({_color('green', str(fix_summary.get('low', 0)))} low, "
            f"{_color('yellow', str(fix_summary.get('medium', 0)))} medium, "
            f"{_color('red', str(fix_summary.get('high', 0)))} high)"
        )

    # Suggested AWS_STATE.md updates
    _print_suggested_updates(components)


def _print_suggested_updates(components: dict) -> None:
    """Print values discovered for AWS_STATE.md [pending] fields."""
    updates = []

    ecs = components.get("drift_ecs", {}).get("details", {})
    if ecs.get("image"):
        updates.append(("ECS Image SHA", ecs["image"]))
    if ecs.get("task_def_revision"):
        updates.append(("ECS Task def revision", str(ecs["task_def_revision"])))
    if ecs.get("cpu"):
        updates.append(("ECS CPU / Memory", f"{ecs['cpu']} / {ecs.get('memory', '?')}"))

    rds = components.get("drift_rds", {}).get("details", {})
    if rds.get("endpoint"):
        updates.append(("RDS Endpoint", rds["endpoint"]))

    if updates:
        print(f"\n{BOLD}Suggested AWS_STATE.md updates:{RESET}")
        for label, value in updates:
            print(f"  {label}: {DIM}{value}{RESET}")


# ---------------------------------------------------------------------------
# --fix mode
# ---------------------------------------------------------------------------
def apply_fixes(results: dict) -> None:
    """Interactively apply low/medium-risk fixes."""
    all_fixes = results.get("fixes", [])
    if not all_fixes:
        print(f"\n{GREEN}No fixes needed.{RESET}")
        return

    # Group by risk
    for risk_level in ("low", "medium"):
        level_fixes = [f for f in all_fixes if f["risk"] == risk_level]
        if not level_fixes:
            continue

        print(f"\n{BOLD}--- {risk_level.upper()} risk fixes ---{RESET}")
        for fix in level_fixes:
            print(f"\n  {fix['description']}")
            print(f"  Command: {DIM}{fix['command']}{RESET}")

            if fix.get("requires_terraform"):
                print(f"  {YELLOW}Requires terraform — skipping auto-fix{RESET}")
                continue

            try:
                answer = input("  Apply this fix? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return

            if answer == "y":
                print(f"  Running: {fix['command']}")
                result = subprocess.run(
                    fix["command"],
                    shell=True,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"  {GREEN}Success{RESET}")
                else:
                    print(f"  {RED}Failed (exit {result.returncode}){RESET}")
                    if result.stderr:
                        print(f"  {result.stderr.strip()}")
            else:
                print("  Skipped.")

    # Print high-risk fixes (never auto-execute)
    high_fixes = [f for f in all_fixes if f["risk"] == "high"]
    if high_fixes:
        print(f"\n{BOLD}--- HIGH risk fixes (manual only) ---{RESET}")
        for fix in high_fixes:
            print(f"\n  {RED}{fix['description']}{RESET}")
            print(f"  Command: {DIM}{fix['command']}{RESET}")
        print(f"\n  {YELLOW}Run these commands manually after review.{RESET}")


# ---------------------------------------------------------------------------
# --sync-docs mode
# ---------------------------------------------------------------------------
AWS_STATE_PATH = Path(__file__).resolve().parent.parent / "AWS_STATE.md"


def sync_docs(components: dict) -> None:
    """Update AWS_STATE.md with discovered values."""
    if not AWS_STATE_PATH.exists():
        print(f"{RED}AWS_STATE.md not found at {AWS_STATE_PATH}{RESET}")
        return

    content = AWS_STATE_PATH.read_text()
    original = content
    changes: list[str] = []

    # ECS image SHA
    ecs = components.get("drift_ecs", {}).get("details", {})
    if ecs.get("image"):
        content, n = _replace_pending(
            content,
            "Image SHA",
            ecs["image"],
        )
        if n:
            changes.append(f"ECS Image SHA -> {ecs['image']}")

    # ECS task def revision
    if ecs.get("task_def_revision"):
        content, n = _replace_pending(
            content,
            "Task def revision",
            str(ecs["task_def_revision"]),
        )
        if n:
            changes.append(f"ECS Task def revision -> {ecs['task_def_revision']}")

    # ECS CPU / Memory
    if ecs.get("cpu") and ecs.get("memory"):
        # Fix stale values
        content = re.sub(
            r"(\|\s*CPU / Memory\s*\|)\s*\d+.*?/\s*\d+.*?MiB\s*\|",
            f"\\1 {ecs['cpu']} ({{:.2f}} vCPU) / {ecs['memory']} MiB |".format(
                int(ecs["cpu"]) / 1024
            ),
            content,
        )
        cpu_vcpu = int(ecs["cpu"]) / 1024
        new_cpu_mem = f"{ecs['cpu']} ({cpu_vcpu:.2f} vCPU) / {ecs['memory']} MiB"
        content = re.sub(
            r"(\|\s*CPU / Memory\s*\|)\s*.*?\s*\|",
            f"\\1 {new_cpu_mem} |",
            content,
        )
        changes.append(f"ECS CPU/Memory -> {new_cpu_mem}")

    # RDS endpoint
    rds = components.get("drift_rds", {}).get("details", {})
    if rds.get("endpoint"):
        content, n = _replace_pending(
            content,
            "Endpoint",
            rds["endpoint"],
        )
        if n:
            changes.append(f"RDS Endpoint -> {rds['endpoint']}")

    # Lambda code hashes -> mark as REMOVED
    content = re.sub(
        r"(\| \*\*Lambda Fetch\*\*.*?Code hash\s*\|)\s*\[pending.*?\]\s*\|",
        "\\1 REMOVED (JD ingestion now in ECS) |",
        content,
    )
    content = re.sub(
        r"(\| \*\*Lambda Persist\*\*.*?Code hash\s*\|)\s*\[pending.*?\]\s*\|",
        "\\1 REMOVED (JD ingestion now in ECS) |",
        content,
    )

    # Update last-updated timestamp
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    content = re.sub(
        r"\*\*Last updated:.*?\*\*",
        f"**Last updated: {now}**",
        content,
    )
    content = re.sub(
        r"(Last verified:.*)",
        f"Last verified: {now} (via drift_check.py)",
        content,
    )
    if "Last verified:" not in content:
        content = content.replace(
            f"**Last updated: {now}**",
            f"**Last updated: {now}**\n**Last verified: {now} (via drift_check.py)**",
        )

    if content != original:
        AWS_STATE_PATH.write_text(content)
        print(f"\n{GREEN}AWS_STATE.md updated:{RESET}")
        for change in changes:
            print(f"  - {change}")
        print(f"  - Last updated -> {now}")
    else:
        print(f"\n{DIM}AWS_STATE.md already up to date.{RESET}")


def _replace_pending(content: str, field_label: str, new_value: str) -> tuple[str, int]:
    """Replace [pending...] values in markdown table rows."""
    pattern = rf"(\|\s*\|?\s*{re.escape(field_label)}\s*\|)\s*`?\[pending[^]]*\]`?\s*\|"
    replacement = f"\\1 `{new_value}` |"
    result, n = re.subn(pattern, replacement, content)
    return result, n


# ---------------------------------------------------------------------------
# --include-tf-plan
# ---------------------------------------------------------------------------
def run_terraform_plan() -> int:
    """Run terraform plan and return exit code (0=no changes, 2=drift)."""
    infra_dir = Path(__file__).resolve().parent.parent / "infra"
    if not infra_dir.exists():
        print(f"{RED}infra/ directory not found{RESET}")
        return 1

    print(f"\n{BOLD}Running terraform plan...{RESET}")
    result = subprocess.run(
        ["terraform", "plan", "-detailed-exitcode", "-no-color"],
        cwd=infra_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode == 0:
        print(f"{GREEN}Terraform: no drift detected{RESET}")
    elif result.returncode == 2:
        print(f"{YELLOW}Terraform: drift detected{RESET}")
        # Print the plan summary (last few lines)
        lines = result.stdout.strip().split("\n")
        for line in lines[-10:]:
            print(f"  {line}")
    else:
        print(f"{RED}Terraform plan failed (exit {result.returncode}){RESET}")
        if result.stderr:
            print(result.stderr[:500])

    return result.returncode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect and fix infrastructure drift between AWS and Terraform",
    )
    parser.add_argument(
        "--check",
        nargs="+",
        metavar="NAME",
        help="Run only specific checks (e.g., ecs sqs eventbridge)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Interactively apply low/medium-risk fixes",
    )
    parser.add_argument(
        "--sync-docs",
        action="store_true",
        help="Update AWS_STATE.md with discovered values",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--include-tf-plan",
        action="store_true",
        help="Also run terraform plan -detailed-exitcode",
    )
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if RED, 2 if YELLOW, 0 if GREEN",
    )
    args = parser.parse_args()

    # Run drift checks
    results = asyncio.run(run_drift_checks())

    # Filter to specific checks if requested
    if args.check:
        filtered = {}
        for name in args.check:
            key = f"drift_{name}"
            if key in results.get("components", {}):
                filtered[key] = results["components"][key]
            else:
                print(f"{YELLOW}Warning: check '{name}' not found{RESET}")
        results["components"] = filtered

    # Output
    if args.json_output:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_results(results)

    # Optional: terraform plan
    tf_exit = 0
    if args.include_tf_plan:
        tf_exit = run_terraform_plan()

    # Optional: apply fixes
    if args.fix:
        apply_fixes(results)

        # Re-run to verify fixes
        print(f"\n{BOLD}Re-running drift checks to verify fixes...{RESET}")
        recheck = asyncio.run(run_drift_checks())
        if not args.json_output:
            print_results(recheck)
        results = recheck

    # Optional: sync docs
    if args.sync_docs:
        sync_docs(results.get("components", {}))

    # Exit code
    if args.exit_code:
        overall = results.get("overall", "red")
        if overall == "red" or tf_exit == 1:
            sys.exit(1)
        elif overall == "yellow" or tf_exit == 2:
            sys.exit(2)
        sys.exit(0)


if __name__ == "__main__":
    main()
