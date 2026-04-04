#!/usr/bin/env python3
"""Create test stacks for merge queue integration testing.

Creates two independent stacks that don't conflict (separate files):
  Stack A (should PASS): two PRs adding valid Python modules
  Stack B (should FAIL): two PRs where the second has a syntax error

Usage:
    python integration/create_test_stacks.py create   # Create stacks + upload
    python integration/create_test_stacks.py queue     # Add 'queue' label to all
    python integration/create_test_stacks.py cleanup   # Close PRs, delete branches
    python integration/create_test_stacks.py watch     # Watch queue progress
    python integration/create_test_stacks.py run       # Full cycle
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = os.environ.get("GITHUB_REPOSITORY", "gbalke/merge_queue")
REPO_DIR = Path(__file__).resolve().parent.parent


def run(cmd: str, check: bool = True, capture: bool = False, cwd: str | None = None) -> str:
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, check=check,
        capture_output=capture, text=True,
        cwd=cwd or str(REPO_DIR),
    )
    return result.stdout.strip() if capture else ""


def gh(cmd: str, check: bool = True) -> str:
    return run(f"gh api repos/{REPO}/{cmd}", check=check, capture=True)


def create_stacks():
    """Create two test stacks on separate files (no conflicts between stacks)."""
    print("\n=== Creating test stacks ===\n")

    run("git checkout main")
    run("git pull origin main")
    run("git stash", check=False)
    run("git branch -D integration-test", check=False)
    run("git checkout -b integration-test main")

    # --- Stack A: Valid code (should pass CI) ---
    # Uses stack_a.py — separate file, no conflicts with stack B
    print("\n--- Stack A: Valid code (will pass) ---")

    (REPO_DIR / "stack_a.py").write_text(
        '"""Stack A module — valid code."""\n\n\n'
        'def greet(name: str) -> str:\n'
        '    """Greet someone by name."""\n'
        '    return f"Hello, {name}!"\n'
    )
    run("git add stack_a.py")
    run('''git commit -m "$(printf 'Add greet function\\n\\nTopic: pass-a1')"''')

    (REPO_DIR / "stack_a.py").write_text(
        '"""Stack A module — valid code."""\n\n\n'
        'def greet(name: str) -> str:\n'
        '    """Greet someone by name."""\n'
        '    return f"Hello, {name}!"\n\n\n'
        'def farewell(name: str) -> str:\n'
        '    """Say goodbye."""\n'
        '    return f"Goodbye, {name}!"\n'
    )
    run("git add stack_a.py")
    run('''git commit -m "$(printf 'Add farewell function\\n\\nTopic: pass-a2\\nRelative: pass-a1')"''')

    # --- Stack B: Syntax error in second PR (should fail CI) ---
    # Uses stack_b.py — separate file
    print("\n--- Stack B: Syntax error in PR 2 (will fail) ---")

    (REPO_DIR / "stack_b.py").write_text(
        '"""Stack B module — will have a syntax error."""\n\n\n'
        'def calculate(x: int, y: int) -> int:\n'
        '    """Calculate the sum."""\n'
        '    return x + y\n'
    )
    run("git add stack_b.py")
    run('''git commit -m "$(printf 'Add calculate function\\n\\nTopic: fail-b1')"''')

    # SYNTAX ERROR: missing colon after def
    (REPO_DIR / "stack_b.py").write_text(
        '"""Stack B module — has a syntax error."""\n\n\n'
        'def calculate(x: int, y: int) -> int:\n'
        '    """Calculate the sum."""\n'
        '    return x + y\n\n\n'
        'def broken(x)\n'
        '    """Missing colon — syntax error."""\n'
        '    return x * 2\n'
    )
    run("git add stack_b.py")
    run('''git commit -m "$(printf 'Add broken function (intentional syntax error)\\n\\nTopic: fail-b2\\nRelative: fail-b1')"''')

    # Upload via revup
    print("\n--- Uploading via revup ---")
    run("revup upload --skip-confirm")

    print("\n=== Stacks created ===")
    print("Stack A (pass): pass-a1 -> pass-a2  [stack_a.py]")
    print("Stack B (fail): fail-b1 -> fail-b2  [stack_b.py — syntax error]")
    print()
    run(f"gh pr list --repo {REPO} --state open")


def queue_stacks():
    """Add 'queue' label to all test PRs (Stack A first for FIFO)."""
    print("\n=== Queuing test PRs ===\n")

    prs = json.loads(gh("pulls?state=open&per_page=50"))
    pass_prs = sorted(
        [pr for pr in prs if "pass-a" in pr["head"]["ref"]],
        key=lambda p: p["number"],
    )
    fail_prs = sorted(
        [pr for pr in prs if "fail-b" in pr["head"]["ref"]],
        key=lambda p: p["number"],
    )

    if not pass_prs and not fail_prs:
        print("No test PRs found. Run 'create' first.")
        return

    # Queue Stack A first (should be processed first via FIFO)
    for pr in pass_prs:
        print(f"  [PASS] Labeling PR #{pr['number']}: {pr['title']}")
        gh(f"issues/{pr['number']}/labels -f labels[]=queue", check=False)

    # Small delay so Stack B gets a later timestamp
    time.sleep(2)

    for pr in fail_prs:
        print(f"  [FAIL] Labeling PR #{pr['number']}: {pr['title']}")
        gh(f"issues/{pr['number']}/labels -f labels[]=queue", check=False)

    print(f"\nQueued {len(pass_prs)} pass PRs, {len(fail_prs)} fail PRs")
    print("Expected: Stack A merges first, Stack B fails CI")


def cleanup():
    """Close test PRs and delete branches."""
    print("\n=== Cleaning up ===\n")

    # Close open test PRs
    prs = json.loads(gh("pulls?state=open&per_page=50"))
    for pr in prs:
        ref = pr["head"]["ref"]
        if any(t in ref for t in ["pass-a", "fail-b"]):
            print(f"  Closing PR #{pr['number']}: {pr['title']}")
            run(f"gh pr close {pr['number']} --repo {REPO}", check=False)

    # Delete test branches
    for prefix in ["greg/revup/main/pass-a", "greg/revup/main/fail-b"]:
        refs = json.loads(gh(f"git/matching-refs/heads/{prefix}"))
        for ref in refs:
            print(f"  Deleting {ref['ref']}")
            gh(f"git/{ref['ref']} -X DELETE", check=False)

    # Delete mq branches
    mq_refs = json.loads(gh("git/matching-refs/heads/mq/"))
    for ref in mq_refs:
        if ref["ref"] != "refs/heads/mq/state":
            print(f"  Deleting {ref['ref']}")
            gh(f"git/{ref['ref']} -X DELETE", check=False)

    # Delete mq-lock rulesets
    rulesets = json.loads(gh("rulesets"))
    for rs in rulesets:
        if rs["name"].startswith("mq-lock-"):
            print(f"  Deleting ruleset {rs['name']}")
            gh(f"rulesets/{rs['id']} -X DELETE", check=False)

    # Clean up local
    run("git checkout main", check=False)
    run("git branch -D integration-test", check=False)

    # Remove test files
    for f in ["stack_a.py", "stack_b.py"]:
        p = REPO_DIR / f
        if p.exists():
            p.unlink()

    print("\nCleanup done")


def wait_for_ci(timeout: int = 300):
    """Wait for all CI runs to complete."""
    print("\n=== Waiting for CI ===\n")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(10)
        runs = json.loads(gh("actions/runs?per_page=20"))
        active = [
            r for r in runs["workflow_runs"]
            if r["status"] != "completed" and r["name"] == "CI"
        ]
        if not active:
            print("  All CI runs completed")
            return True
        print(f"  {len(active)} CI runs in progress...")
    print("  Timeout waiting for CI")
    return False


def watch_queue(timeout: int = 600):
    """Watch the merge queue until idle."""
    print("\n=== Watching merge queue ===\n")
    start = time.time()
    seen_active = False

    while time.time() - start < timeout:
        time.sleep(10)
        runs = json.loads(gh("actions/runs?per_page=20"))
        mq_runs = [
            r for r in runs["workflow_runs"]
            if r["name"] == "Merge Queue" and r["status"] != "completed"
        ]

        if mq_runs:
            seen_active = True
            for r in mq_runs:
                print(f"  MQ run {r['id']}: {r['status']}")
        elif seen_active:
            print("  Merge queue finished")
            break
        else:
            print("  Waiting for merge queue to start...")

    # Show final results
    print("\n=== Results ===\n")
    prs = json.loads(gh("pulls?state=all&per_page=50"))
    for pr in sorted(prs, key=lambda p: p["number"]):
        ref = pr["head"]["ref"]
        if any(t in ref for t in ["pass-a", "fail-b"]):
            state = pr["state"].upper()
            merged = " (merged)" if pr.get("merged_at") else ""
            expect = "PASS" if "pass-a" in ref else "FAIL"
            actual = "OK" if (expect == "PASS" and state == "MERGED") or (expect == "FAIL" and state != "MERGED") else "WRONG"
            marker = "✓" if actual == "OK" else "✗"
            print(f"  {marker} #{pr['number']:3d} {state:8s}{merged:10s} {pr['title']:50s} [expect: {expect}]")


def full_run():
    """Full integration test cycle."""
    print("=" * 60)
    print("  MERGE QUEUE INTEGRATION TEST")
    print("=" * 60)

    cleanup()
    create_stacks()
    print("\nWaiting for initial CI on PRs...")
    wait_for_ci()
    queue_stacks()
    watch_queue()


def main():
    parser = argparse.ArgumentParser(description="Merge queue integration tests")
    parser.add_argument(
        "action",
        choices=["create", "queue", "cleanup", "watch", "run"],
    )
    args = parser.parse_args()

    {"create": create_stacks, "queue": queue_stacks, "cleanup": cleanup,
     "watch": watch_queue, "run": full_run}[args.action]()


if __name__ == "__main__":
    main()
