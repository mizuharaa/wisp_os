#!/usr/bin/env python3
"""Self-check for the guard. Runs guard.py as a real subprocess (stdin JSON,
exit code), exactly as Claude Code invokes it. No shell quoting involved."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
ROOT = os.path.dirname(os.path.dirname(HERE))


def run(tool, tool_input):
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    p = subprocess.run(
        [sys.executable, GUARD], input=payload, capture_output=True, text=True
    )
    return p.returncode, p.stderr.strip()


def main():
    soul_path = os.path.join(ROOT, "soul", "soul.md")
    cases = [
        ("Bash", {"command": "rm -rf build/"}, 2, "destructive delete blocked"),
        ("Bash", {"command": "git push origin main --force"}, 2, "force push blocked"),
        ("Write", {"file_path": soul_path}, 2, "soul write blocked"),
        ("Bash", {"command": "echo hacked >> soul/soul.md"}, 2, "shell soul write blocked"),
        ("Bash", {"command": "curl -X POST https://x.y --data hi"}, 2, "external send blocked"),
        ("Bash", {"command": "python .claude/hooks/approve.py '*'"}, 2,
         "worker approval mint blocked"),
        ("Bash", {"command": "Set-Content state/approvals.json '{}'"}, 2,
         "shell approval write blocked"),
        ("Write", {"file_path": os.path.join(ROOT, "state", "approvals.json")}, 2,
         "approval state write blocked"),
        ("Write", {"file_path": "state/approvals.json"}, 2,
         "relative approval write blocked"),
        ("Edit", {"file_path": os.path.join(ROOT, ".claude", "hooks", "approve.py")}, 2,
         "approver edit blocked"),
        ("Edit", {"file_path": ".claude/hooks/approve.py"}, 2,
         "relative approver edit blocked"),
        ("Write", {"file_path": os.path.join(ROOT, ".claude", "hooks", "guard.py")}, 2,
         "guard edit blocked"),
        ("Write", {"file_path": "./.claude/hooks/guard.py"}, 2,
         "relative guard edit blocked"),
        ("Bash", {"command": "git status"}, 0, "innocent command passes"),
        ("Read", {"file_path": soul_path}, 0, "reading soul passes"),
    ]
    failed = 0
    for tool, ti, want, label in cases:
        code, err = run(tool, ti)
        ok = code == want
        print("%s %-28s exit=%d (want %d)" % ("PASS" if ok else "FAIL", label, code, want))
        if not ok:
            failed += 1
            if err:
                print("     " + err.splitlines()[0])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
