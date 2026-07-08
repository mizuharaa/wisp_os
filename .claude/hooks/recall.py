#!/usr/bin/env python3
"""UserPromptSubmit hook: before ANY prompt is worked, ask Hermes if this
pattern was already solved. Hits are injected into context so the session
relearns first instead of re-solving — the flywheel fires automatically now,
not just when someone remembers the boot sequence.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERMES = os.path.join(ROOT, "hermes", "hermes.py")


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    prompt = (data.get("prompt") or "").strip()
    if len(prompt.split()) < 3:  # too short to match anything meaningful
        return 0
    r = subprocess.run([sys.executable, HERMES, "query", prompt[:300]],
                       capture_output=True, text=True, timeout=15)
    if r.returncode == 0 and r.stdout.strip():
        print("[Hermes recall] Prior solutions match this prompt - reuse before re-solving:")
        print(r.stdout.strip()[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
