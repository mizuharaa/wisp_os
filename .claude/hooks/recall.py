#!/usr/bin/env python3
"""UserPromptSubmit hook: before ANY prompt is worked, ask Hermes if this
pattern was already solved. Hits are injected into context so the session
relearns first instead of re-solving — the flywheel fires automatically now,
not just when someone remembers the boot sequence.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from memory import recall_engine


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return 0
    # CEO workers already carry a receipted block in their explicit mission.
    # The trusted process environment prevents duplicate cards/tokens while
    # ordinary Claude sessions still recall before every operator prompt.
    if (os.environ.get("MAESTRO_BRAIN_PREINJECTED") == "1" or
            os.environ.get("MAESTRO_SKIP_BRAIN_RECALL") == "1"):
        return 0
    try:
        bundle = recall_engine.query(
            prompt, root=ROOT,
            cid=str(os.environ.get("MAESTRO_SID") or data.get("session_id") or ""),
            route=str(os.environ.get("MAESTRO_RECALL_ROUTE") or "claude_hook"),
            injected_into="claude_user_prompt_hook", injected_prompt_count=0)
    except Exception:
        return 0
    if bundle.get("prompt_block"):
        recall_engine.mark_exposure(
            bundle.get("receipt"), root=ROOT, prompt_count=1)
        print(bundle["prompt_block"].lstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
