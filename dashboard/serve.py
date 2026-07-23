#!/usr/bin/env python3
"""Rune dashboard server. Zero dependencies: stdlib http.server + ctypes.

GET  /...                serves the repo root (dashboard + live state), no-store.
GET  /api/instances      managed Claude Code windows Maestro launched, with liveness.
GET  /api/integrations   configured MCPs, hooks, agents, skills.
GET  /api/workflows      advisory workflow-coach suggestions (never execution).
GET  /api/brain          recall receipts and bounded Hermes storage health.
GET  /api/calendar       range-filtered last-good Outlook events.
GET  /api/briefing       where you left off: commits (here + GitHub), the missions
                         that ran and what's UNFINISHED, Hermes notes, calendar,
                         queued directives. Reads disk only — always works offline.
POST /api/message        queue a directive -> state/inbox.jsonl + wire.
POST /api/brain/query    verify one query with the production ranker; no model.
POST /api/spawn          launch a session on this repo:
                         mode "tab"        -> own titled console window (focusable/closable)
                         mode "background" -> headless claude -p, log in state/spawn-logs/
POST /api/focus  {sid}   bring that window to the foreground (Win32).
POST /api/close  {sid}   taskkill that window's process tree.
POST /api/orchestrate    start a conductor feedback loop (orchestrator.py).
GET  /api/orchestrations loop states with per-round worker/critic logs.
POST /api/orch-action    {oid,action:accept|revise|reject|stop[,feedback]}.
POST /api/ceo-action     stop/resume/archive or resolve a gated CEO role.
POST /api/ceo-delivery   review/test/commit or two-step non-force push.
POST /api/briefing/check atomically catch up the current 09:30 cycle when due.
POST /api/briefing/run   run one stored priority (IDs + safe|skip mode only).
GET  /api/ssh-creds      saved ssh credential KEYS (never secrets).
POST /api/ssh-forget     {key} drop a saved ssh credential.

Binds 127.0.0.1 only — that IS the boundary; anyone who can POST here can spawn
permission-skipping agents. Never bind 0.0.0.0.

Usage: python dashboard/serve.py [port]     (default 8817)
"""
import base64
import collections
import ctypes
import datetime
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

BOOT = time.time()  # this process's start — lets desktop.py detect a stale server
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "memory"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import vault_path  # single source of truth for the vault location
import askpass                   # DPAPI ssh credential store
import orchestrator              # the conductor feedback loop
import pulse                     # outside world: claude/codex usage, github, gmail, spotify
import chat                      # dashboard assistant (Haiku/Sonnet over the Anthropic API)
import ceo                       # command bar: prompt -> Haiku refine -> CEO plan -> roles
import daily_briefing            # offline: where you left off + what to continue
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")
INBOX = os.path.join(ROOT, "state", "inbox.jsonl")
WINDOWS = os.path.join(ROOT, "state", "windows.json")
LOGS = os.path.join(ROOT, "state", "spawn-logs")
WORKFLOW_ANALYZER = os.path.join(
    ROOT, "skills", "workflow-coach", "scripts", "analyze.py")
_WORKFLOW_CACHE = {"mtime_ns": None, "payload": None}
CREATE_NEW_CONSOLE = 0x00000010
IS_WIN = sys.platform == "win32"
_AUTO_SCHEDULER_LOCK = threading.Lock()
_AUTO_SCHEDULER_THREAD = None


def start_auto_scheduler(interval=60):
    """Boot catch-up plus a lightweight 09:30/retry watcher.

    Work is launched through the external ``scheduled`` CLI, so closing or
    restarting the dashboard cannot kill an in-flight model call. The shared
    filesystem locks and frozen source date make this safe alongside Windows
    Task Scheduler.
    """
    global _AUTO_SCHEDULER_THREAD
    with _AUTO_SCHEDULER_LOCK:
        if _AUTO_SCHEDULER_THREAD and _AUTO_SCHEDULER_THREAD.is_alive():
            return _AUTO_SCHEDULER_THREAD

        def watch():
            while True:
                try:
                    result = daily_briefing.ensure_scheduled_generation()
                    if result.get("started"):
                        emit(session="scheduler", event="briefing-scheduled",
                             detail="catch-up queued for %s" % result.get("source_date"))
                except Exception as exc:
                    # The watcher is best-effort; durable attempt state and the
                    # next tick retain recovery without taking down the server.
                    emit(session="scheduler", event="briefing-scheduler-error",
                         detail=("%s: %s" % (type(exc).__name__, exc))[:200])
                time.sleep(max(15, int(interval)))

        _AUTO_SCHEDULER_THREAD = threading.Thread(
            target=watch, name="briefing-auto-scheduler", daemon=True)
        _AUTO_SCHEDULER_THREAD.start()
        return _AUTO_SCHEDULER_THREAD


def short_path(p):
    """Windows 8.3 short path (no spaces). Win32-OpenSSH runs SSH_ASKPASS without
    quoting, so a space in the path (…\\Python Env\\…) breaks it — the short form
    avoids that. Falls back to the original if 8.3 names are unavailable."""
    if not IS_WIN:
        return p
    buf = ctypes.create_unicode_buffer(600)
    n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 600)
    return buf.value if n and " " not in buf.value else p


def emit(**kv):
    args = []
    for k, v in kv.items():
        args += ["--" + k, str(v)]
    subprocess.run([sys.executable, MIRROR] + args, capture_output=True)


# ---------------------------------------------------------------- window mgmt
def load_windows():
    try:
        with open(WINDOWS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"windows": []}


def save_windows(doc):
    with open(WINDOWS, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def pid_alive(pid):
    if not IS_WIN or not pid:
        return False
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
    if not h:
        return False
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def _foreground(hwnd):
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    try:
        user32.SwitchToThisWindow(hwnd, True)  # ponytail: undocumented but the
    except Exception:                          # only call that foregrounds from
        user32.SetForegroundWindow(hwnd)       # a background process. Upgrade to
    return True                                # AttachThreadInput if it regresses.


def focus_by(pid, needle):
    """Foreground the console window for this instance. We launch each tab via
    conhost.exe, so the window's owning process IS the pid we tracked — match on
    that (Claude rewrites the console title after launch, so title-match alone
    misses). Title substring is kept as a fallback."""
    if not IS_WIN:
        return False
    user32 = ctypes.windll.user32
    by_pid, by_title = [], []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if pid and wpid.value == pid:
            by_pid.append(hwnd)
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if needle and needle.lower() in buf.value.lower():
            by_title.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    hits = by_pid or by_title
    return _foreground(hits[0]) if hits else False


VCACHE = {"t": 0.0, "data": None}


def vault_tree():
    """The real brain: walk the Obsidian vault, return notes + folders +
    wikilink edges so the graph shows what Maestro actually knows."""
    now = time.time()
    if VCACHE["data"] and now - VCACHE["t"] < 20:
        return VCACHE["data"]
    vp = vault_path()
    notes = []
    if vp and os.path.isdir(vp):
        for dirpath, dirs, files in os.walk(vp):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.endswith(".md"):
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, vp).replace("\\", "/")
                    folder = rel.split("/")[0] if "/" in rel else "(root)"
                    try:
                        mt = os.path.getmtime(full)
                    except OSError:
                        mt = 0
                    notes.append({"path": rel, "name": fn[:-3], "folder": folder, "mtime": mt})
    notes.sort(key=lambda n: -n["mtime"])
    notes = notes[:240]  # ponytail: cap for graph perf; paginate if the vault outgrows it
    ix = {}
    for i, n in enumerate(notes):
        ix.setdefault(n["name"].lower(), i)
    links = []
    for i, n in enumerate(notes):
        try:
            txt = open(os.path.join(vp, n["path"]), encoding="utf-8", errors="ignore").read(20000)
        except OSError:
            continue
        for m in re.findall(r"\[\[([^\]\|#\n]+)", txt):
            j = ix.get(m.strip().lower())
            if j is not None and j != i:
                links.append([i, j])
    data = {"vault": vp, "notes": notes, "links": links[:600]}
    VCACHE.update(t=now, data=data)
    return data


def vault_note(rel):
    """Safe read of one vault note (preview) — path must stay inside the vault."""
    vp = vault_path()
    if not vp:
        return None
    full = os.path.realpath(os.path.join(vp, rel))
    if not full.startswith(os.path.realpath(vp)) or not full.endswith(".md"):
        return None
    try:
        return open(full, encoding="utf-8", errors="ignore").read(2600)
    except OSError:
        return None


_WHISPER = None
_WHISPER_LOCK = threading.Lock()

LOOP_ACTIVE = ("running", "retrying", "waiting")
MISSION_ACTIVE = ("planning", "running", "retrying", "repairing",
                  "waiting_permission", "review")


def activity_payload():
    """Small stable feed for the mini bar: what runs now, what just finished."""
    running, recent = [], []
    for o in orchestrator.list_all():
        item = {"kind": "loop", "id": o.get("oid") or "",
                "title": (o.get("name") or o.get("mission") or "loop")[:80],
                "status": o.get("status") or ""}
        (running if o.get("status") in LOOP_ACTIVE and o.get("live") else recent).append(item)
    for r in ceo.list_all():
        item = {"kind": "mission", "id": r.get("cid") or "",
                "title": (r.get("name") or r.get("mission") or "mission")[:80],
                "status": r.get("status") or ""}
        (running if r.get("status") in MISSION_ACTIVE else recent).append(item)
    return {"running": running, "recent": recent[:8]}


def brain_payload(limit=100):
    """Return bounded retrieval receipts and storage health without overclaiming.

    Observable prompt insertion is kept separate from counterfactual token
    savings or model compliance, neither of which can be proven from a run.
    """
    try:
        from memory import recall_engine
        receipt_doc = recall_engine.read_receipts(ROOT, limit=limit)
    except Exception:
        receipt_doc = {"summary": {}, "receipts": []}
    try:
        from hermes import hermes as hermes_store
        storage = hermes_store.storage_health()
    except Exception:
        storage = {
            "schema_version": 2,
            "kind": "hermes.storage_health",
            "status": "unavailable",
        }
    return {
        "schema_version": 1,
        "summary": receipt_doc.get("summary") or {},
        "receipts": receipt_doc.get("receipts") or [],
        "storage": storage,
        "proof": {
            "boundary": "retrieval_and_prompt_insertion",
            "exact_savings_known": False,
            "model_use_proven": False,
        },
    }


def integrations():
    out = {"mcp": [], "hooks": [], "agents": [], "skills": {}}
    try:
        mj = json.load(open(os.path.join(ROOT, ".mcp.json"), encoding="utf-8"))
        for name, cfg in mj.get("mcpServers", {}).items():
            cmd = (cfg.get("command", "") + " " + " ".join(cfg.get("args", []))).strip()
            if not cmd and cfg.get("url"):
                cmd = (cfg.get("type") or "http") + " " + cfg.get("url", "")
            out["mcp"].append({"name": name, "command": cmd, "type": cfg.get("type") or "stdio"})
    except (OSError, json.JSONDecodeError):
        pass
    try:
        sj = json.load(open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8"))
        for ev, arr in sj.get("hooks", {}).items():
            n = sum(len(g.get("hooks", [])) for g in arr)
            out["hooks"].append({"event": ev, "count": n})
    except (OSError, json.JSONDecodeError):
        pass
    adir = os.path.join(ROOT, ".claude", "agents")
    if os.path.isdir(adir):
        out["agents"] = sorted(f[:-3] for f in os.listdir(adir) if f.endswith(".md"))
    cdir = os.path.join(ROOT, ".claude", "commands")
    out["commands"] = sorted(
        "/" + f[:-3] for f in os.listdir(cdir) if f.endswith(".md")
    ) if os.path.isdir(cdir) else []
    try:
        reg = json.load(open(os.path.join(ROOT, "skills", "registry.json"), encoding="utf-8"))
        out["skills"] = dict(collections.Counter(s["status"] for s in reg["skills"].values()))
    except (OSError, json.JSONDecodeError):
        pass
    return out


def workflow_suggestions():
    """Top read-only workflow-coach candidates, cached until the wire changes."""
    events = os.path.join(ROOT, "state", "events.jsonl")
    try:
        mtime_ns = os.stat(events).st_mtime_ns
        if (_WORKFLOW_CACHE["mtime_ns"] == mtime_ns
                and _WORKFLOW_CACHE["payload"] is not None):
            return _WORKFLOW_CACHE["payload"]
        spec = importlib.util.spec_from_file_location(
            "rune_dashboard_workflow_coach", WORKFLOW_ANALYZER)
        if spec is None or spec.loader is None:
            raise ImportError("workflow coach loader unavailable")
        coach = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(coach)
        report = coach.analyze_path(events)
        payload = dict(report)
        suggestions = list(report.get("suggestions") or [])
        payload["total_suggestions"] = len(suggestions)
        payload["suggestions"] = suggestions[:12]
        _WORKFLOW_CACHE.update(mtime_ns=mtime_ns, payload=payload)
        return payload
    except (OSError, ImportError, ValueError) as exc:
        return {
            "suggestions": [], "total_suggestions": 0,
            "advisory_only": True, "review_required": True, "executed": False,
            "error": "workflow coach unavailable: %s" % str(exc)[:160],
        }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # hermes c9d5a2f
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _request_path(self):
        """Decoded URL path used for routing and static-file security checks."""
        path = urllib.parse.urlparse(self.path).path
        # SimpleHTTPRequestHandler decodes before opening a file. Decode to a
        # fixed point here too so a double-encoded dot cannot bypass our guard.
        for _ in range(4):
            decoded = urllib.parse.unquote(path)
            if decoded == path:
                break
            path = decoded
        return path.replace("\\", "/")

    def _static_denied(self, path):
        """Expose only the small, explicit set of files the dashboard needs.

        This process serves from the repository root, which also contains local
        credentials and runtime state.  Windows normalises trailing dots/spaces
        in path segments (``state.`` becomes ``state``), and ``:`` can address
        an NTFS alternate data stream, so reject those spellings before applying
        the allowlist.
        """
        parts = [part for part in path.split("/") if part]
        if any(part.startswith(".") or part != part.rstrip(" .") or ":" in part
               for part in parts):
            return True
        clean = "/" + "/".join(parts)
        if clean.startswith("/api/") or clean == "/api":
            return False
        public = {
            "/dashboard", "/dashboard/index.html", "/dashboard/lofi.jpg",
            "/tokens.css",
            "/state/events.jsonl", "/state/inbox.jsonl",
            "/state/approvals.json", "/skills/registry.json",
            "/hermes/solved.jsonl", "/memory/OBSIDIAN.md",
        }
        return clean.rstrip("/") not in public

    def _origin_allowed(self):
        """Allow same-server browser POSTs and non-browser local CLI clients."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
            host = (parsed.hostname or "").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except (TypeError, ValueError):
            return False
        return (parsed.scheme in ("http", "https")
                and host in ("127.0.0.1", "localhost", "::1")
                and port == self.server.server_port)

    def do_GET(self):
        path = self._request_path()
        if self._static_denied(path):
            return self._json(404, {"error": "not found"})
        if self.path == "/api/spotify/login":
            # FIXED redirect URI (not Host-derived): Spotify requires the exact
            # string to be pre-registered, and loopback must be 127.0.0.1 (not
            # localhost). Register pulse.spotify_redirect() in your Spotify app.
            url = pulse.spotify_authorize_url(pulse.spotify_redirect())
            return self._redirect(url or "/dashboard/?spotify=noclient")
        if self.path.startswith("/api/spotify/callback"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (q.get("code") or [""])[0]
            err = pulse.spotify_exchange(code, pulse.spotify_redirect()) if code \
                else (q.get("error") or ["no code"])[0]
            return self._redirect("/dashboard/?spotify=" + ("connected" if not err else "error"))
        if self.path == "/api/instances":
            doc = load_windows()
            for w in doc["windows"]:
                w["alive"] = pid_alive(w.get("pid"))
            # auto-prune dead windows (finished background runs, closed terminals)
            # so the list self-cleans — no manual removal needed.
            alive = [w for w in doc["windows"] if w["alive"]]
            if len(alive) != len(doc["windows"]):
                doc["windows"] = alive
                save_windows(doc)
            return self._json(200, doc)
        if self.path == "/api/integrations":
            return self._json(200, integrations())
        if self.path == "/api/workflows":
            return self._json(200, workflow_suggestions())
        if self.path == "/api/vault":
            return self._json(200, vault_tree())
        if self.path.startswith("/api/vault-note"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = vault_note((q.get("path") or [""])[0])
            if body is None:
                return self._json(404, {"error": "note not found"})
            return self._json(200, {"content": body})
        if self.path == "/api/orchestrations":
            return self._json(200, {"orchestrations": orchestrator.list_all()})
        if self.path == "/api/activity":
            return self._json(200, activity_payload())
        if self.path == "/api/brain":
            return self._json(200, brain_payload())
        if self.path == "/api/ceo":
            return self._json(200, {
                "runs": [ceo.public_run(run) for run in ceo.list_all()],
                "history": [ceo.public_run(run) for run in ceo.list_history()],
            })
        if self.path == "/api/ssh-creds":
            return self._json(200, {"keys": askpass.keys()})  # names only, never secrets
        if self.path == "/api/pulse":
            return self._json(200, pulse.get())
        if path == "/api/briefing":
            # A cheap read of the last atomically generated plan. Git/model work
            # only happens in the explicit async generation endpoint.
            return self._json(200, daily_briefing.dashboard_payload())
        if path == "/api/briefing/job":
            return self._json(200, daily_briefing.job_status())
        if path == "/api/briefing/settings":
            return self._json(200, daily_briefing.get_settings())
        if path in ("/api/briefing/calendar", "/api/calendar"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw_start = (query.get("start") or query.get("from") or [""])[0]
            raw_end = (query.get("end") or query.get("to") or [""])[0]
            raw_days = (query.get("days") or [""])[0]
            try:
                day0 = datetime.date.fromisoformat(raw_start) if raw_start else None
                if raw_end:
                    day_end = datetime.date.fromisoformat(raw_end)
                    base = day0 or datetime.datetime.now().astimezone().date()
                    days = (day_end - base).days
                else:
                    days = int(raw_days) if raw_days else 92
            except (TypeError, ValueError):
                return self._json(400, {
                    "error": "calendar range needs ISO start/end dates and an integer days value"})
            if days < 0:
                return self._json(400, {"error": "calendar end precedes start"})
            return self._json(
                200, daily_briefing.calendar_payload(day0=day0, days=min(days, 184)))
        if self.path == "/api/version":
            # index.html mtime — an open window polls this to notice when Rune
            # has rewritten its own UI and offer a reload instead of going stale.
            try:
                mt = int(max(
                    os.path.getmtime(os.path.join(ROOT, "dashboard", "index.html")),
                    os.path.getmtime(os.path.join(ROOT, "tokens.css"))))
            except OSError:
                mt = 0
            # boot: this process's start time, so desktop.py can tell a route
            # was added/changed in a .py file *after* the running server started
            # (backend code doesn't hot-reload like index.html does) and knows
            # to restart instead of silently reusing the stale process.
            return self._json(200, {"v": mt, "boot": BOOT})
        return super().do_GET()

    def do_POST(self):
        if not self._origin_allowed():
            return self._json(403, {"error": "cross-origin POST denied"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1024 * 1024:
                return self._json(413, {"error": "request body too large"})
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        if not isinstance(data, dict):
            return self._json(400, {"error": "json body must be an object"})
        route = {
            "/api/message": self.api_message, "/api/spawn": self.api_spawn,
            "/api/focus": self.api_focus, "/api/close": self.api_close,
            "/api/orchestrate": self.api_orchestrate,
            "/api/orch-action": self.api_orch_action,
            "/api/ssh-forget": self.api_ssh_forget,
            "/api/chat": self.api_chat,
            "/api/brain/query": self.api_brain_query,
            "/api/skill": self.api_skill,
            "/api/ceo": self.api_ceo,
            "/api/ceo-action": self.api_ceo_action,
            "/api/ceo-delivery": self.api_ceo_delivery,
            "/api/briefing/check": self.api_briefing_check,
            "/api/briefing/generate": self.api_briefing_generate,
            "/api/briefing/agent": self.api_briefing_agent,
            "/api/briefing/run": self.api_briefing_run,
            "/api/briefing/settings": self.api_briefing_settings,
            "/api/spotify/ctl": self.api_spotify_ctl,
            "/api/stop-all": self.api_stop_all,
            "/api/voice": self.api_voice,
        }.get(self.path)
        if not route:
            return self._json(404, {"error": "unknown endpoint"})
        return route(data)

    def api_skill(self, data):
        name = re.sub(r"[^a-z0-9\-]", "", str(data.get("name") or "").lower().strip())
        if not name:
            return self._json(400, {"error": "skill name required (a-z, 0-9, -)"})
        branch = re.sub(r"[^a-z0-9\-]", "", str(data.get("branch") or "misc").lower()) or "misc"
        trig = re.sub(r"[^a-z0-9\-]", "", str(data.get("trigger") or name).lower().lstrip("/"))
        eng = os.path.join(ROOT, "skills", "engine.py")
        r = subprocess.run([sys.executable, eng, "add", name, "--branch", branch,
                            "--trigger", "/" + trig], capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            return self._json(500, {"error": ((r.stderr or r.stdout) or "engine error")[:200]})
        emit(session="operator", event="skill", detail="added skill '%s' (%s, /%s)" % (name, branch, trig))
        return self._json(200, {"ok": True, "name": name, "branch": branch})

    def api_stop_all(self, data):
        """Panic button: stop every live loop and active mission."""
        stopped, errors = [], []
        for o in orchestrator.list_all():
            if o.get("status") in LOOP_ACTIVE and o.get("live"):
                err = orchestrator.action(o.get("oid") or "", "stop", "")
                (errors if err else stopped).append("loop:%s" % o.get("oid"))
        for r in ceo.list_all():
            if r.get("status") in MISSION_ACTIVE:
                err = ceo.action(r.get("cid") or "", "", "stop", "")
                (errors if err else stopped).append("mission:%s" % r.get("cid"))
        emit(session="operator", event="stop-all",
             detail="stopped %d (%s)" % (len(stopped), ", ".join(stopped)[:150]))
        return self._json(200, {"ok": True, "stopped": stopped, "errors": errors})

    def api_voice(self, data):
        """Transcribe one mini-bar voice clip with local Whisper."""
        try:
            raw = base64.b64decode(str(data.get("audio_b64") or ""), validate=True)
        except (ValueError, TypeError):
            return self._json(400, {"error": "audio_b64 must be valid base64"})
        if not raw:
            return self._json(400, {"error": "empty audio"})
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return self._json(501, {"error": "faster-whisper not installed",
                                    "hint": "pip install faster-whisper"})
        clip = os.path.join(ROOT, "state", "voice-last.webm")
        with open(clip, "wb") as f:
            f.write(raw)
        global _WHISPER
        with _WHISPER_LOCK:
            if _WHISPER is None:
                # ponytail: base/int8 on CPU; bump to small or GPU if accuracy hurts
                _WHISPER = WhisperModel("base", device="cpu", compute_type="int8")
            try:
                segments, _info = _WHISPER.transcribe(clip, vad_filter=True)
                text = " ".join(s.text.strip() for s in segments).strip()
            except Exception as e:
                return self._json(500, {"error": "transcription failed: %s" % str(e)[:150]})
        return self._json(200, {"ok": True, "text": text})

    def api_spotify_ctl(self, data):
        out = pulse.spotify_ctl(str(data.get("action") or ""), data.get("pos_ms"))
        return self._json(200 if out.get("ok") else 502, out)

    def api_briefing_generate(self, data):
        """Queue plan-only generation and return before either model is called."""
        roots = data.get("repo_roots")
        try:
            job = daily_briefing.start_generation(
                date=str(data.get("date") or "yesterday"),
                model=(str(data["model"]) if data.get("model") else None),
                effort=(str(data["effort"]) if data.get("effort") else None),
                more=bool(data.get("more")), force=bool(data.get("force")),
                roots=roots)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except daily_briefing.GenerationBusy as exc:
            return self._json(409, {"error": str(exc),
                                    "job": daily_briefing.job_status()})
        return self._json(202, {"ok": True, "job": job})

    def api_briefing_check(self, data):
        """Catch up the due 09:30 cycle without duplicating model work."""
        if data:
            return self._json(400, {
                "error": "briefing check does not accept options",
            })
        result = daily_briefing.check_scheduled_generation()
        return self._json(202 if result.get("started") else 200, result)

    def api_briefing_agent(self, data):
        required = ("batch_id", "priority_id", "agent_id")
        if any(not data.get(key) for key in required):
            return self._json(400, {"error": "batch_id, priority_id, and agent_id are required"})
        try:
            agent = daily_briefing.update_agent(
                str(data["batch_id"]), str(data["priority_id"]), str(data["agent_id"]),
                model=data.get("model"), effort=data.get("effort"))
        except KeyError as exc:
            return self._json(404, {"error": str(exc).strip("'")})
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except (daily_briefing.BriefingError, daily_briefing.GenerationBusy) as exc:
            return self._json(409, {"error": str(exc)})
        return self._json(200, {"ok": True, "agent": agent})

    def api_briefing_run(self, data):
        """Run one authoritative stored priority; never trust browser plan text."""
        unknown = set(data) - {"batch_id", "priority_id", "rerun", "permission_mode"}
        if unknown:
            return self._json(400, {"error": "unsupported briefing run fields: %s" %
                                    ", ".join(sorted(unknown))})
        if not data.get("batch_id") or not data.get("priority_id"):
            return self._json(400, {"error": "batch_id and priority_id are required"})
        if "rerun" in data and not isinstance(data.get("rerun"), bool):
            return self._json(400, {"error": "rerun must be true or false"})
        permission_mode = data.get("permission_mode", "safe")
        if permission_mode not in ("safe", "skip"):
            return self._json(400, {"error": "permission_mode must be safe or skip"})
        batch_id = str(data["batch_id"]).strip()
        priority_id = str(data["priority_id"]).strip()
        if len(batch_id) > 128 or len(priority_id) > 128:
            return self._json(400, {"error": "briefing identifiers are too long"})
        try:
            spec = daily_briefing.execution_spec(batch_id, priority_id)
            roles = (daily_briefing.direct_execution_roles(spec["priority"])
                     if permission_mode == "skip" else None)
        except KeyError as exc:
            return self._json(404, {"error": str(exc).strip("'")})
        except daily_briefing.GenerationBusy as exc:
            return self._json(409, {"error": str(exc)})
        except daily_briefing.BriefingError as exc:
            return self._json(409, {"error": str(exc)})
        out, err = ceo.start_briefing_mission(
            spec["direct_prompt"] if permission_mode == "skip" else spec["prompt"],
            spec["source"], spec["workdir"],
            rerun=bool(data.get("rerun", False)),
            permission_mode=permission_mode, roles=roles)
        if err:
            return self._json(502, {"error": err})
        payload = dict(out)
        payload.update(source=out.get("source") or spec["source"],
                       workdir=out.get("workdir") or spec["workdir"],
                       reused=bool(out.get("reused")))
        if not payload["reused"]:
            emit(session="operator", event="mission",
                 detail=("[%s] briefing priority %s/%s: %s" %
                         (payload.get("cid"), batch_id, priority_id,
                          payload.get("name") or "CEO mission"))[:200])
        return self._json(200, payload)

    def api_briefing_settings(self, data):
        patch = data.get("settings") if isinstance(data.get("settings"), dict) else data
        try:
            settings = daily_briefing.update_settings(patch)
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except daily_briefing.GenerationBusy as exc:
            return self._json(409, {"error": str(exc)})
        return self._json(200, {"ok": True, "settings": settings})

    def api_ceo(self, data):
        """Command bar: prompt -> Haiku refine -> brain recall -> CEO plan -> roles.
        opts (from the Run-it dropdown) override the CEO's per-role choices."""
        opts = data.get("opts") if isinstance(data.get("opts"), dict) else {}
        out, err = ceo.plan_and_start(str(data.get("text") or ""), opts)
        if err:
            return self._json(502, {"error": err})
        if out.get("kind") == "answer":
            emit(session="operator", event="mission",
                 detail=("CEO answered directly (%s): %s" % (out.get("model"), out.get("goal")))[:200])
        else:
            emit(session="operator", event="mission",
                 detail=("[%s] CEO: %s" % (out["cid"], out["name"]))[:200])
        return self._json(200, out)

    def api_ceo_action(self, data):
        unknown = set(data) - {"cid", "role", "action", "feedback", "request_id"}
        if unknown:
            return self._json(400, {"error": "unsupported CEO action fields: %s" %
                                    ", ".join(sorted(unknown))})
        cid = data.get("cid")
        role = data.get("role", "")
        action = data.get("action")
        feedback = data.get("feedback", "")
        request_id = data.get("request_id", "")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
            return self._json(400, {"error": "valid mission cid is required"})
        if not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", role):
            return self._json(400, {"error": "invalid role id"})
        allowed = {"approve", "redo", "skip", "stop", "resume", "archive",
                   "allow", "retry", "deny"}
        if not isinstance(action, str) or action not in allowed:
            return self._json(400, {"error": "unknown CEO action"})
        if not isinstance(feedback, str) or len(feedback) > 2000:
            return self._json(400, {"error": "feedback must be a short string"})
        if action in ("allow", "retry", "deny") and feedback:
            return self._json(400, {"error": "permission decisions do not accept feedback"})
        permission_action = action in ("allow", "retry", "deny")
        if permission_action:
            if (not isinstance(request_id, str) or
                    not re.fullmatch(r"(?:pr|legacy)_[a-f0-9]{32}", request_id)):
                return self._json(400, {"error": "valid permission request_id is required"})
        elif request_id:
            return self._json(400, {"error": "request_id is only valid for permission decisions"})
        err = ceo.action(cid, role, action, feedback, request_id=request_id)
        if err:
            return self._json(409, {"error": err})
        emit(session="operator", event="ceo-action",
             detail="%s/%s -> %s" % (cid, role, action))
        return self._json(200, {"ok": True})

    def api_ceo_delivery(self, data):
        """Guarded post-mission delivery; repository controls stay server-side."""
        action = data.get("action")
        cid = data.get("cid")
        if not isinstance(action, str) or action not in (
                "review", "test", "commit", "prepare_push", "confirm_push", "fix"):
            return self._json(400, {"error": "unknown delivery action"})
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
            return self._json(400, {"error": "valid mission cid is required"})
        allowed = {"cid", "action"}
        if action == "commit":
            allowed.add("message")
        elif action == "confirm_push":
            allowed.add("token")
        unknown = set(data) - allowed
        if unknown:
            return self._json(400, {"error": "unsupported delivery fields: %s" %
                                    ", ".join(sorted(unknown))})
        message = data.get("message", "")
        token = data.get("token", "")
        if action == "commit" and (not isinstance(message, str) or len(message) > 200):
            return self._json(400, {"error": "commit message must be at most 200 characters"})
        if action == "confirm_push" and (not isinstance(token, str) or
                                          not token or len(token) > 200):
            return self._json(400, {"error": "push confirmation token is required"})
        if action == "fix":
            out, err = ceo.delivery_fix(cid)
            if err:
                return self._json(409, {"error": err})
            emit(session="operator", event="ceo-delivery",
                 detail=("%s -> fix mission %s" % (cid, out.get("cid", "")))[:200])
            return self._json(200, {"ok": True, "mission": ceo.public_run(out)})
        out, err = ceo.delivery_action(cid, action, message=message, token=token)
        if err:
            payload = {"error": err}
            if isinstance(out, dict):
                payload.update(out)
            return self._json(409, payload)
        emit(session="operator", event="ceo-delivery",
             detail=("%s -> %s" % (cid, action))[:200])
        return self._json(200, out)

    def api_chat(self, data):
        out = chat.ask(str(data.get("message") or ""),
                       history=data.get("history") if isinstance(data.get("history"), list) else [],
                       model=(str(data.get("model")) if data.get("model") else None))
        return self._json(200 if "reply" in out else 502, out)

    def api_brain_query(self, data):
        """Run the production ranker as a no-model reproducibility check."""
        text = data.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._json(400, {"error": "query text is required"})
        if len(text) > 2000:
            return self._json(400, {"error": "query text is limited to 2000 characters"})
        try:
            from memory import recall_engine
            bundle = recall_engine.query(
                text, root=ROOT, cid="verify-" + uuid.uuid4().hex[:12],
                route="brain_verify", injected_into="verification_only",
                injected_prompt_count=0, track_usage=False)
        except Exception:
            return self._json(503, {"error": "brain ranker is temporarily unavailable"})
        return self._json(200, {
            "ok": True,
            "receipt": bundle.get("receipt"),
            "proof": {"model_called": False, "context_injected": False},
        })

    def api_message(self, data):
        text = (data.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "empty directive"})
        row = {"id": uuid.uuid4().hex[:8],
               "ts": datetime.datetime.now().isoformat(timespec="seconds"), "text": text}
        with open(INBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        emit(session="operator", event="directive", detail=("[%s] %s" % (row["id"], text))[:200])
        return self._json(200, {"ok": True, "id": row["id"]})

    def api_spawn(self, data):
        mission = (data.get("mission") or "").strip()
        mode = data.get("mode", "tab")
        ssh = data.get("ssh") if isinstance(data.get("ssh"), dict) else None
        skip = bool(data.get("skip", True))  # --dangerously-skip-permissions is now a tick
        if not mission:
            return self._json(400, {"error": "empty mission"})
        # conscious spend: least privilege covers the MODEL and the turn budget,
        # not just tools. Default = inherit the config model; pick a smaller model
        # for mechanical work, a bigger one only for hard reasoning.
        model = {"": "", "default": "", "haiku": "haiku", "sonnet": "sonnet",
                 "opus": "opus", "fable": "fable"}.get(
                     str(data.get("model", "")).lower(), "")
        try:
            budget = max(1, min(100, int(data.get("budget") or 40)))
        except (TypeError, ValueError):
            budget = 40
        safe = re.sub(r'[&|<>^%"\r\n]', " ", mission).strip()
        sid = uuid.uuid4().hex[:8]
        # human name so the manager reads like a team, not a hex dump
        name = (data.get("name") or "").strip()[:40] or re.sub(r"\s+", " ", mission)[:40]
        title = "RUNE " + sid
        flag = " --dangerously-skip-permissions" if skip else ""
        mflag = (" --model " + model) if model else ""
        host_label = None
        # MAESTRO_SID rides the env into claude, so the instance's hook events
        # land on the wire under the SAME id the dashboard tracks it by.
        env = dict(os.environ, MAESTRO_SID=sid)
        # launch under a specific Claude account: CLAUDE_CONFIG_DIR points claude
        # at that account's config/transcripts (local terminals only; a remote
        # host has its own). Blank/unknown/"main-with-no-dir" = default account.
        account = (data.get("account") or "").strip()
        acct_dir = pulse.dir_for(account) if account else ""
        if acct_dir and not ssh:
            env["CLAUDE_CONFIG_DIR"] = acct_dir
        if ssh:
            # Remote terminal. Password: typed in the window by default; with a
            # saved/typed one, ssh fetches it from the DPAPI store via askpass.
            mode = "ssh"
            host = re.sub(r"[^\w.\-]", "", str(ssh.get("host", "")))
            user = re.sub(r"[^\w.\-]", "", str(ssh.get("user", "")))
            try:
                port = max(1, min(65535, int(ssh.get("port") or 22)))
            except (TypeError, ValueError):
                port = 22
            rdir = re.sub(r"[\"'`$\\]", "", str(ssh.get("dir") or "")).strip()
            if not host or not user:
                return self._json(400, {"error": "ssh needs host and user"})
            host_label = "%s@%s" % (user, host)
            key = "%s:%d" % (host_label, port)
            pw = str(ssh.get("password") or "")
            if pw and ssh.get("save", True):
                askpass.store(key, pw)
            if pw or askpass.fetch(key) is not None:
                env.update(SSH_ASKPASS=short_path(os.path.join(ROOT, "dashboard", "askpass.cmd")),
                           SSH_ASKPASS_REQUIRE="force", MAESTRO_SSH_KEY=key)
                if pw and not ssh.get("save", True):
                    env["MAESTRO_SSH_PW"] = pw  # use once, never written to disk
            rsafe = re.sub(r"[&|<>^%\"'`$\\\r\n]", " ", mission).strip()
            remote = ("cd '%s' && " % rdir) if rdir else ""
            # ssh with a command runs a NON-login shell: ~/.profile and nvm never
            # load, so claude (in ~/.local/bin or npm/nvm bin) is "command not
            # found" even though interactive logins work. Recreate the login
            # environment before running it. No double quotes here — the whole
            # thing rides inside cmd.exe double quotes below.
            boot = (". /etc/profile >/dev/null 2>&1; . ~/.profile >/dev/null 2>&1; "
                    ". ~/.bashrc >/dev/null 2>&1; "
                    "[ -s ~/.nvm/nvm.sh ] && . ~/.nvm/nvm.sh >/dev/null 2>&1; "
                    "export PATH=~/.local/bin:~/.npm-global/bin:~/bin:/usr/local/bin:$PATH; "
                    "command -v claude >/dev/null || echo '[Maestro] claude is not installed on this host (or not on PATH) - install Claude Code there first'; ")
            rcmd = "%s%sclaude%s%s '%s'" % (boot, remote, flag, mflag, rsafe)
            cmd = 'conhost.exe cmd /k title %s && ssh -t -p %d %s "%s"' % (title, port, host_label, rcmd)
            p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                 creationflags=CREATE_NEW_CONSOLE if IS_WIN else 0)
        elif mode == "background":
            os.makedirs(LOGS, exist_ok=True)
            log = open(os.path.join(LOGS, sid + ".log"), "w", encoding="utf-8")
            argv = ["claude", "-p", mission, "--max-turns", str(budget)]
            if skip:
                argv.insert(2, "--dangerously-skip-permissions")
            if model:
                argv += ["--model", model]
            p = subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                 shell=IS_WIN, env=env)
        else:
            mode = "tab"
            # conhost.exe hosts the window itself, so p.pid owns the HWND (used by
            # /api/focus) and it forces a real console even if Windows Terminal is
            # the default terminal (which would otherwise share one window).
            cmd = 'conhost.exe cmd /k title %s && claude%s%s "%s"' % (title, flag, mflag, safe)
            p = subprocess.Popen(cmd, cwd=ROOT, env=env,
                                 creationflags=CREATE_NEW_CONSOLE if IS_WIN else 0)
        doc = load_windows()
        doc["windows"].append({
            "sid": sid, "name": name, "title": title, "mission": mission[:200], "mode": mode,
            "host": host_label, "skip": skip,
            "model": model or "default", "budget": budget if mode == "background" else None,
            "pid": p.pid, "account": account or "main",
            "started": datetime.datetime.now().isoformat(timespec="seconds")})
        save_windows(doc)
        emit(session="operator", event="user-spawn", agent=mode,
             detail=("[%s] %s%s model=%s acct=%s skip=%s · %s"
                     % (sid, name, (" @" + host_label) if host_label else "",
                        model or "default", account or "main", skip, mission))[:200])
        return self._json(200, {"ok": True, "id": sid, "mode": mode,
                                "model": model or "default", "account": account or "main"})

    def api_orchestrate(self, data):
        mission = (data.get("mission") or "").strip()
        if not mission:
            return self._json(400, {"error": "empty mission"})
        try:
            turns = max(1, min(100, int(data.get("turns") or 40)))
            rounds = max(1, min(8, int(data.get("rounds") or 3)))
        except (TypeError, ValueError):
            turns, rounds = 40, 3
        oid, err = orchestrator.start(
            mission, name=(data.get("name") or "").strip(),
            model=str(data.get("model") or "default"),
            critic=str(data.get("critic") or "opus"),
            turns=turns, rounds=rounds, auto=bool(data.get("auto", True)),
            skip=bool(data.get("skip", True)), workdir=str(data.get("dir") or ""),
            account=str(data.get("account") or ""))
        if err:
            return self._json(400, {"error": err})
        return self._json(200, {"ok": True, "id": oid})

    def api_orch_action(self, data):
        err = orchestrator.action(data.get("oid", ""), data.get("action", ""),
                                  str(data.get("feedback") or ""))
        if err:
            return self._json(409, {"error": err})
        emit(session="operator", event="orch-action",
             detail="%s -> %s %s" % (data.get("oid"), data.get("action"),
                                     str(data.get("feedback") or ""))[:200])
        return self._json(200, {"ok": True})

    def api_ssh_forget(self, data):
        ok = askpass.forget(str(data.get("key") or ""))
        return self._json(200 if ok else 404,
                          {"ok": ok, "error": None if ok else "no such credential"})

    def api_focus(self, data):
        sid = data.get("sid", "")
        w = next((x for x in load_windows()["windows"] if x["sid"] == sid), None)
        if not w:
            return self._json(404, {"error": "unknown instance"})
        ok = focus_by(w.get("pid"), w.get("title"))
        return self._json(200 if ok else 409,
                          {"ok": ok, "error": None if ok else "window not found (closed?)"})

    def api_close(self, data):
        sid = data.get("sid", "")
        doc = load_windows()
        w = next((x for x in doc["windows"] if x["sid"] == sid), None)
        if not w:
            return self._json(404, {"error": "unknown instance"})
        if w.get("pid"):
            subprocess.run(["taskkill", "/PID", str(w["pid"]), "/T", "/F"],
                           capture_output=True, shell=IS_WIN)
        doc["windows"] = [x for x in doc["windows"] if x["sid"] != sid]
        save_windows(doc)
        emit(session="operator", event="instance-close", detail="closed %s" % sid)
        return self._json(200, {"ok": True})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8817
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    start_auto_scheduler()
    print("Rune: http://127.0.0.1:%d/dashboard/" % port)
    print("serving %s (Ctrl+C to stop)" % ROOT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
