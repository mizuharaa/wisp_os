#!/usr/bin/env python3
"""Wisp MCP server: the control plane and UIA runtime as agent tools.

Stdio JSON-RPC (newline-delimited, MCP spec) exposing the running Wisp
engine to any MCP client — Claude Code, Codex, etc:

    claude mcp add wisp -- python mcp/server.py

Every tool is a thin HTTP call to the engine on 127.0.0.1, so one engine
instance keeps owning COM, serialization, approvals, and the audit wire —
agents get capabilities, never a second uncontrolled actor. Stdlib only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ENGINE = os.environ.get("WISP_ENGINE") or "http://127.0.0.1:8817"

LOCATOR = {
    "type": "object",
    "description": "Exact-match control locator; give at least one key.",
    "properties": {
        "auto_id": {"type": "string", "description": "UIA AutomationId"},
        "name": {"type": "string", "description": "Control name/title"},
        "control_type": {"type": "string", "description": "e.g. Button, Edit"},
    },
}
TARGET = {
    "pid": {"type": "integer", "description": "Target window's process id"},
    "title_re": {"type": "string", "description": "Regex on the window title"},
}

TOOLS = [
    {"name": "desktop_windows",
     "description": "List top-level Windows desktop windows (title, pid, "
                    "handle) — the map for targeting UIA tools.",
     "inputSchema": {"type": "object", "properties": {}},
     "route": ("GET", "/api/uia/windows")},
    {"name": "window_tree",
     "description": "Serialize one window's UI Automation control tree as "
                    "structured data (control types, names, automation ids, "
                    "current values). Bounded by depth/max_nodes.",
     "inputSchema": {"type": "object", "properties": dict(TARGET, **{
         "depth": {"type": "integer", "default": 3},
         "max_nodes": {"type": "integer", "default": 400}})},
     "route": ("POST", "/api/uia/tree")},
    {"name": "ui_act",
     "description": "Perform ONE structured action on a control (invoke, "
                    "set_text, toggle, focus) and get back before/after "
                    "state plus a verified flag — the runtime re-reads the "
                    "control and never claims unobserved success.",
     "inputSchema": {"type": "object", "properties": dict(TARGET, **{
         "locator": LOCATOR,
         "action": {"type": "string",
                    "enum": ["invoke", "set_text", "toggle", "focus"]},
         "value": {"type": "string", "description": "Text for set_text"}}),
      "required": ["locator", "action"]},
     "route": ("POST", "/api/uia/act")},
    {"name": "ui_read",
     "description": "Read one control's current state (name, value, toggle "
                    "state). Use it to assert an expected outcome.",
     "inputSchema": {"type": "object", "properties": dict(TARGET, **{
         "locator": LOCATOR}), "required": ["locator"]},
     "route": ("POST", "/api/uia/read")},
    {"name": "browser_tabs",
     "description": "List open tabs (id, title, url) in the Wisp-profile "
                    "browser — a real Edge/Chrome with persistent signed-in "
                    "sessions (GitHub, Figma, shops...).",
     "inputSchema": {"type": "object", "properties": {}},
     "route": ("GET", "/api/browser/tabs")},
    {"name": "browser_open",
     "description": "Open a URL in the Wisp-profile browser (starts it if "
                    "needed) and return the new tab id.",
     "inputSchema": {"type": "object",
                     "properties": {"url": {"type": "string"}},
                     "required": ["url"]},
     "route": ("POST", "/api/browser/open")},
    {"name": "browser_act",
     "description": "One structured page action with observed-state "
                    "readback: read (title/url/text), goto, click "
                    "(CSS selector, verified found), fill (verified value), "
                    "eval (JS expression). Targets tab_id or the first tab. "
                    "Purchases/sends must still go through mission approval.",
     "inputSchema": {"type": "object", "properties": {
         "tab_id": {"type": "string"},
         "action": {"type": "string",
                    "enum": ["read", "goto", "click", "fill", "eval"]},
         "selector": {"type": "string", "description": "CSS selector"},
         "value": {"type": "string", "description": "Text for fill"},
         "url": {"type": "string", "description": "For goto"},
         "js": {"type": "string", "description": "For eval"}},
      "required": ["action"]},
     "route": ("POST", "/api/browser/act")},
    {"name": "agent_activity",
     "description": "Wisp control-plane snapshot: running loops/missions, "
                    "recent endings, and the pending operator approval queue.",
     "inputSchema": {"type": "object", "properties": {}},
     "route": ("GET", "/api/activity")},
    {"name": "stop_all",
     "description": "Panic stop: halt every live conductor loop and active "
                    "mission on this machine, process-tree deep.",
     "inputSchema": {"type": "object", "properties": {}},
     "route": ("POST", "/api/stop-all")},
]


def call_engine(method, path, payload):
    req = urllib.request.Request(
        ENGINE + path,
        data=None if method == "GET" else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r), False
    except urllib.error.HTTPError as e:
        try:
            return json.load(e), True
        except json.JSONDecodeError:
            return {"error": "engine HTTP %d" % e.code}, True
    except (urllib.error.URLError, OSError) as e:
        return {"error": "Wisp engine unreachable at %s (%s) — start it with "
                         "`python dashboard/serve.py`" % (ENGINE, e)}, True


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            reply(mid, {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "wisp", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": [{k: t[k] for k in
                                   ("name", "description", "inputSchema")}
                                  for t in TOOLS]})
        elif method == "tools/call":
            params = msg.get("params") or {}
            tool = next((t for t in TOOLS
                         if t["name"] == params.get("name")), None)
            if not tool:
                reply(mid, error={"code": -32602, "message":
                                  "unknown tool %r" % params.get("name")})
                continue
            verb, path = tool["route"]
            out, is_err = call_engine(verb, path, params.get("arguments") or {})
            reply(mid, {"content": [{"type": "text",
                                     "text": json.dumps(out, indent=1)[:20000]}],
                        "isError": is_err})
        elif method == "ping":
            reply(mid, {})
        elif mid is not None:  # unknown request (not a notification)
            reply(mid, error={"code": -32601,
                              "message": "method %r not supported" % method})


if __name__ == "__main__":
    main()
