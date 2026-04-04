#!/usr/bin/env python3
"""Create test stacks for merge queue integration testing.

Creates two stacks:
  Stack A (should PASS): two PRs adding valid Python functions
  Stack B (should FAIL): two PRs where the second introduces a syntax error

Usage:
    # Create the stacks and upload via revup
    python integration/create_test_stacks.py create

    # Queue all PRs (add 'queue' label)
    python integration/create_test_stacks.py queue

    # Clean up: close PRs, delete branches
    python integration/create_test_stacks.py cleanup

    # Full cycle: create, wait for CI, queue, watch results
    python integration/create_test_stacks.py run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.environ.get("GITHUB_REPOSITORY", "gbalke/merge_queue")


def run(cmd: str, check: bool = True, capture: bool = False) -> str:
    """Run a shell command."""
    print(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, check=check,
        capture_output=capture, text=True,
    )
    return result.stdout.strip() if capture else ""


def gh(cmd: str, check: bool = True) -> str:
    """Run a gh API command."""
    return run(f"gh api repos/{REPO}/{cmd}", check=check, capture=True)


def create_stacks():
    """Create two test stacks on the current branch."""
    print("\n=== Creating test stacks ===\n")

    # Make sure we're on the right branch
    run("git checkout main")
    run("git pull origin main")

    # Stash any local changes
    run("git stash", check=False)

    # Create a fresh branch for commits
    run("git checkout -b integration-test main")

    # --- Stack A: Valid code (should pass CI) ---
    print("\n--- Stack A: Valid code (will pass) ---")

    # A1: Add a valid function
    with open("hello.py", "a") as f:
        f.write("""

def greet(name: str) -> str:
    \"\"\"Greet someone by name.\"\"\"
    return f"Hello, {name}!"
""")
    run("git add hello.py")
    run('''git commit -m "$(printf 'Add greet function\\n\\nTopic: pass-stack-a1')"''')

    # A2: Add another valid function (stacked on A1)
    with open("hello.py", "a") as f:
        f.write("""

def farewell(name: str) -> str:
    \"\"\"Say goodbye to someone.\"\"\"
    return f"Goodbye, {name}!"
""")
    run("git add hello.py")
    run('''git commit -m "$(printf 'Add farewell function\\n\\nTopic: pass-stack-a2\\nRelative: pass-stack-a1')"''')

    # --- Stack B: Syntax error in second PR (should fail CI) ---
    print("\n--- Stack B: Syntax error in PR 2 (will fail) ---")

    # B1: Valid function
    with open("hello.py", "a") as f:
        f.write("""

def calculate(x: int, y: int) -> int:
    \"\"\"Calculate the sum.\"\"\"
    return x + y
""")
    run("git add hello.py")
    run('''git commit -m "$(printf 'Add calculate function\\n\\nTopic: fail-stack-b1')"''')

    # B2: SYNTAX ERROR — missing colon, bad indent
    with open("hello.py", "a") as f:
        f.write("""

def broken_function(x)
    \"\"\"This function has a syntax error — missing colon.\"\"\"
    return x * 2
""")
    run("git add hello.py")
    run('''git commit -m "$(printf 'Add broken function (intentional syntax error)\\n\\nTopic: fail-stack-b2\\nRelative: fail-stack-b1')"''')

    # Upload via revup
    print("\n--- Uploading via revup ---")
    run("revup upload --skip-confirm")

    print("\n=== Stacks created ===")
    print("Stack A (pass): pass-stack-a1 -> pass-stack-a2")
    print("Stack B (fail): fail-stack-b1 -> fail-stack-b2")

    # Show the PRs
    run(f"gh pr list --repo {REPO} --state open")


def queue_stacks():
    """Add 'queue' label to all test PRs."""
    print("\n=== Queuing all test PRs ===\n")

    prs = json.loads(gh("pulls?state=open&per_page=50"))
    test_prs = [
        pr for pr in prs
        if any(t in pr["head"]["ref"] for t in ["pass-stack", "fail-stack"])
    ]

    if not test_prs:
        print("No test PRs found. Run 'create' first.")
        return

    for pr in sorted(test_prs, key=lambda p: p["number"]):
        print(f"  Labeling PR #{pr['number']}: {pr['title']}")
        gh(f"issues/{pr['number']}/labels -f labels[]=queue", check=False)

    print(f"\nQueued {len(test_prs)} PRs")


def cleanup():
    """Close test PRs and delete branches."""
    print("\n=== Cleaning up ===\n")

    prs = json.loads(gh("pulls?state=open&per_page=50"))
    test_prs = [
        pr for pr in prs
        if any(t in pr["head"]["ref"] for t in ["pass-stack", "fail-stack"])
    ]

    for pr in test_prs:
        print(f"  Closing PR #{pr['number']}")
        run(f"gh pr close {pr['number']} --repo {REPO}", check=False)

    # Delete remote branches
    refs = json.loads(gh("git/matching-refs/heads/greg/revup/main/"))
    for ref in refs:
        name = ref["ref"]
        if any(t in name for t in ["pass-stack", "fail-stack"]):
            print(f"  Deleting {name}")
            gh(f"git/{name} -X DELETE", check=False)

    # Delete mq branches
    mq_refs = json.loads(gh("git/matching-refs/heads/mq/"))
    for ref in mq_refs:
        print(f"  Deleting {ref['ref']}")
        gh(f"git/{ref['ref']} -X DELETE", check=False)

    # Delete mq-lock rulesets
    rulesets = json.loads(gh("rulesets"))
    for rs in rulesets:
        if rs["name"].startswith("mq-lock-"):
            print(f"  Deleting ruleset {rs['name']}")
            gh(f"rulesets/{rs['id']} -X DELETE", check=False)

    # Clean up local branch
    run("git checkout main", check=False)
    run("git branch -D integration-test", check=False)

    print("\nCleanup done")


def wait_for_ci():
    """Wait for CI to pass on all test PRs."""
    print("\n=== Waiting for CI ===\n")
    for _ in range(30):  # 5 min max
        time.sleep(10)
        runs = json.loads(gh("actions/runs?per_page=10"))
        active = [r for r in runs["workflow_runs"] if r["status"] != "completed"]
        if not active:
            print("  All CI runs completed")
            return
        print(f"  {len(active)} runs still in progress...")
    print("  Timeout waiting for CI")


def watch_queue():
    """Watch the merge queue status."""
    print("\n=== Watching merge queue ===\n")
    for i in range(60):  # 10 min max
        time.sleep(10)
        runs = json.loads(gh("actions/runs?per_page=20"))
        mq_runs = [
            r for r in runs["workflow_runs"]
            if r["name"] == "Merge Queue" and r["status"] != "completed"
        ]
        if not mq_runs and i > 3:
            print("  Merge queue idle")
            break
        for r in mq_runs:
            print(f"  MQ run {r['id']}: {r['status']} on {r['head_branch']}")

    # Show final PR states
    print("\n=== Final PR states ===\n")
    prs = json.loads(gh("pulls?state=all&per_page=50"))
    test_prs = [
        pr for pr in prs
        if any(t in pr["head"]["ref"] for t in ["pass-stack", "fail-stack"])
    ]
    for pr in sorted(test_prs, key=lambda p: p["number"]):
        print(f"  #{pr['number']} {pr['state'].upper():8s} {pr['title']}")


def full_run():
    """Full integration test: create, wait, queue, watch."""
    cleanup()
    create_stacks()
    wait_for_ci()
    queue_stacks()
    watch_queue()


def main():
    parser = argparse.ArgumentParser(description="Merge queue integration tests")
    parser.add_argument(
        "action",
        choices=["create", "queue", "cleanup", "watch", "run"],
        help="Action to perform",
    )
    args = parser.parse_args()

    actions = {
        "create": create_stacks,
        "queue": queue_stacks,
        "cleanup": cleanup,
        "watch": watch_queue,
        "run": full_run,
    }
    actions[args.action]()


if __name__ == "__main__":
    main()
