#!/usr/bin/env python3
"""Mint an approval token for a gated action class.
Usage: python .claude/hooks/approve.py <action|*> [--minutes N]
Actions: destructive-delete, deploy, external-send, spend, soul-write, *"""
import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPROVALS = os.path.join(ROOT, "state", "approvals.json")


def main():
    if os.environ.get("MAESTRO_SID") or os.environ.get("MAESTRO_ROLE_ID"):
        print("worker sessions cannot mint approvals; use Rune Mission Activity",
              file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    action = sys.argv[1]
    minutes = 15.0
    if "--minutes" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])
    try:
        with open(APPROVALS, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        doc = {"tokens": []}
    now = time.time()
    doc["tokens"] = [t for t in doc.get("tokens", []) if t.get("expires", 0) > now]
    doc["tokens"].append(
        {
            "action": action,
            "expires": now + minutes * 60,
            "minted": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    )
    with open(APPROVALS, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    print("approved '%s' for %g min (expires with the token, guard re-arms itself)" % (action, minutes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
