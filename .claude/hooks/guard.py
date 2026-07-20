#!/usr/bin/env python3
"""PreToolUse gate. Blocks gated action classes unless state/approvals.json
holds an unexpired token for that class. Exit 2 = block; stderr goes to Claude.
Fails closed: anything matching a gate without a token does not run."""
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APPROVALS = os.path.join(ROOT, "state", "approvals.json")

GATES = {
    "destructive-delete": r"rm\s+-\w*[rf]|Remove-Item\b.*-(Recurse|Force)|rmdir\s+/s|del\s+/[sfq]|git\s+(reset\s+--hard|clean\s+-\w*f|push\b[^|;&]*(--force|\s-f\b))|DROP\s+(TABLE|DATABASE)|\bmkfs\b|format\s+[a-z]:",
    "deploy": r"\bgh\s+release\b|\bvercel\b|\bnetlify\b|\bfly\s+deploy\b|\bkubectl\s+apply\b|\bterraform\s+apply\b|\bdocker\s+push\b|\bdeploy\b",
    "external-send": r"\b(curl|wget|Invoke-RestMethod|Invoke-WebRequest)\b[^|;]*(-X\s*(POST|PUT|DELETE)|--data\b|\s-d\s|-Method\s+(Post|Put|Delete))|\bsendmail\b|\btwilio\b",
    "spend": r"\bnpm\s+publish\b|\btwine\s+upload\b|\bstripe\b|\baws\s+\S+\s+(create|run-instances)|\bgcloud\b.*\bcreate\b",
}

APPROVAL_ADMIN_PATHS = (
    "/state/approvals.json", "/.claude/hooks/approve.py",
    "/.claude/hooks/guard.py",
)


def gate_for(data):
    """Return (action, evidence) if this tool call is gated, else (None, None)."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        p = (ti.get("file_path") or ti.get("notebook_path") or "").replace("\\", "/").lower()
        while p.startswith("./"):
            p = p[2:]
        rooted = "/" + p.lstrip("/")
        if any(rooted.endswith(path) for path in APPROVAL_ADMIN_PATHS):
            return "approval-admin", p
        if "/soul/" in p or p.startswith("soul/"):
            return "soul-write", p
    cmd = ti.get("command", "")
    if cmd:
        normalized = cmd.replace("\\", "/").lower()
        if any(path.lstrip("/") in normalized
               for path in APPROVAL_ADMIN_PATHS):
            return "approval-admin", cmd
        if re.search(r"soul[/\\]", cmd) and re.search(
            r"(>>?|Set-Content|Out-File|Add-Content|sed\s+-i|\btee\b)", cmd
        ):
            return "soul-write", cmd
        for name, rx in GATES.items():
            if re.search(rx, cmd, re.I):
                return name, cmd
    return None, None


def approved(action):
    if action == "approval-admin":
        return False
    try:
        with open(APPROVALS, encoding="utf-8") as f:
            tokens = json.load(f).get("tokens", [])
    except (OSError, json.JSONDecodeError):
        return False  # ponytail: unreadable approvals = no approvals (fail closed)
    now = time.time()
    sid = os.environ.get("MAESTRO_SID", "")
    role_id = os.environ.get("MAESTRO_ROLE_ID", "")
    request_id = os.environ.get("MAESTRO_PERMISSION_REQUEST_ID", "")
    for token in tokens:
        try:
            active = float(token.get("expires") or 0) > now
        except (TypeError, ValueError):
            active = False
        if token.get("action") not in (action, "*") or not active:
            continue
        # CEO-minted approvals are scoped to the exact mission and role that
        # showed the operator the blocked request. A sibling worker cannot
        # consume the token. Legacy/manual tokens have no scope and retain the
        # approve.py behavior used by an operator at the terminal.
        if token.get("cid") and token.get("cid") != sid:
            continue
        if token.get("role") and token.get("role") != role_id:
            continue
        if token.get("request_id") and token.get("request_id") != request_id:
            continue
        return True
    return False


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    action, evidence = gate_for(data)
    if not action or approved(action):
        return 0
    sys.stderr.write(
        "MAESTRO GUARD: blocked gated action '%s'.\n"
        "Evidence: %s\n"
        "Resolve this request from Rune's Mission Activity permission card.\n"
        % (action, str(evidence)[:200])
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
