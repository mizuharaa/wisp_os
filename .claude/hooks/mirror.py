#!/usr/bin/env python3
"""The event wire. Every event AIOS emits lands as one line in
state/events.jsonl — the single file the dashboard tails.

Hook mode (no args):   reads Claude Code hook JSON on stdin.
Manual mode (args):    mirror.py --stage build --detail "..." [--event E]
                       [--agent A] [--session S]
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVENTS = os.path.join(ROOT, "state", "events.jsonl")


def emit(ev):
    ev["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def from_hook():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    # MAESTRO_SID is set by /api/spawn and the orchestrator so an instance's
    # wire events carry the SAME id the dashboard tracks it under.
    sid = os.environ.get("MAESTRO_SID") or (data.get("session_id") or "local")[:8]
    he = data.get("hook_event_name", "")
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    ev = {"session": sid, "event": he.lower() or "event"}
    # the subagent tool is named "Agent" in Claude Code 2.x, "Task" in older builds
    spawn_tool = tool in ("Task", "Agent")
    if he == "SessionStart":
        ev.update(event="session-start", detail="session online")
    elif he == "PreToolUse":
        if not spawn_tool:
            return  # pre-execution, only spawns are wire-worthy (rest logged post)
        ev.update(
            event="spawn",
            agent=ti.get("subagent_type", "agent"),
            detail=(ti.get("description") or "")[:120],
        )
    elif he == "PostToolUse":
        if spawn_tool:
            return  # spawn logged live at PreToolUse; SubagentStop logs the exit
        ev.update(event="tool", tool=tool)
        detail = ti.get("command") or ti.get("file_path") or ""
        if detail:
            ev["detail"] = str(detail)[:120]
    elif he == "SubagentStop":
        ev.update(event="agent-exit", detail="subagent reported and exited")
    elif he == "Stop":
        ev.update(event="session-stop", detail="conductor idle")
    emit(ev)


def from_args(argv):
    ev = {"session": os.environ.get("MAESTRO_SID")
          or os.environ.get("AIOS_SESSION", "conductor"), "event": "stage"}
    i = 0
    while i < len(argv):
        key, val = argv[i].lstrip("-"), argv[i + 1] if i + 1 < len(argv) else ""
        if key in ("stage", "detail", "event", "agent", "session"):
            ev[key] = val
        i += 2
    print("event: " + json.dumps(emit(ev), ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        from_args(sys.argv[1:])
    else:
        from_hook()
