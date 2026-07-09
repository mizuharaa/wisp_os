#!/usr/bin/env python3
"""Maestro dashboard server. Zero dependencies: stdlib http.server + ctypes.

GET  /...                serves the repo root (dashboard + live state), no-store.
GET  /api/instances      managed Claude Code windows Maestro launched, with liveness.
GET  /api/integrations   configured MCPs, hooks, agents, skills.
POST /api/message        queue a directive -> state/inbox.jsonl + wire.
POST /api/spawn          launch a session on this repo:
                         mode "tab"        -> own titled console window (focusable/closable)
                         mode "background" -> headless claude -p, log in state/spawn-logs/
POST /api/focus  {sid}   bring that window to the foreground (Win32).
POST /api/close  {sid}   taskkill that window's process tree.
POST /api/orchestrate    start a conductor feedback loop (orchestrator.py).
GET  /api/orchestrations loop states with per-round worker/critic logs.
POST /api/orch-action    {oid,action:accept|revise|reject|stop[,feedback]}.
GET  /api/ssh-creds      saved ssh credential KEYS (never secrets).
POST /api/ssh-forget     {key} drop a saved ssh credential.

Binds 127.0.0.1 only — that IS the boundary; anyone who can POST here can spawn
permission-skipping agents. Never bind 0.0.0.0.

Usage: python dashboard/serve.py [port]     (default 8817)
"""
import collections
import ctypes
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import uuid
from ctypes import wintypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "memory"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import vault_path  # single source of truth for the vault location
import askpass                   # DPAPI ssh credential store
import orchestrator              # the conductor feedback loop
import pulse                     # outside world: claude usage, github, gmail, spotify
import chat                      # dashboard assistant (Haiku/Sonnet over the Anthropic API)
import mission                   # command bar: goal -> refined mission -> orchestrator
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")
INBOX = os.path.join(ROOT, "state", "inbox.jsonl")
WINDOWS = os.path.join(ROOT, "state", "windows.json")
LOGS = os.path.join(ROOT, "state", "spawn-logs")
CREATE_NEW_CONSOLE = 0x00000010
IS_WIN = sys.platform == "win32"


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


def integrations():
    out = {"mcp": [], "hooks": [], "agents": [], "skills": {}}
    try:
        mj = json.load(open(os.path.join(ROOT, ".mcp.json"), encoding="utf-8"))
        for name, cfg in mj.get("mcpServers", {}).items():
            cmd = (cfg.get("command", "") + " " + " ".join(cfg.get("args", []))).strip()
            out["mcp"].append({"name": name, "command": cmd})
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # hermes c9d5a2f
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

    def do_GET(self):
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
        if self.path == "/api/ssh-creds":
            return self._json(200, {"keys": askpass.keys()})  # names only, never secrets
        if self.path == "/api/pulse":
            return self._json(200, pulse.get())
        return super().do_GET()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad json"})
        route = {
            "/api/message": self.api_message, "/api/spawn": self.api_spawn,
            "/api/focus": self.api_focus, "/api/close": self.api_close,
            "/api/orchestrate": self.api_orchestrate,
            "/api/orch-action": self.api_orch_action,
            "/api/ssh-forget": self.api_ssh_forget,
            "/api/chat": self.api_chat,
            "/api/skill": self.api_skill,
            "/api/mission": self.api_mission,
            "/api/spotify/ctl": self.api_spotify_ctl,
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

    def api_spotify_ctl(self, data):
        out = pulse.spotify_ctl(str(data.get("action") or ""), data.get("pos_ms"))
        return self._json(200 if out.get("ok") else 502, out)

    def api_mission(self, data):
        text = (data.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "empty goal"})
        hist = data.get("history") if isinstance(data.get("history"), list) else []
        out = mission.intake(text, hist)
        if out.get("error"):
            return self._json(502, out)
        if out.get("action") == "launch":
            brief = (out.get("mission") or text).strip()
            wd = (out.get("dir") or "").strip()
            if wd and not os.path.isdir(wd):
                wd = ""  # never launch into a hallucinated path
            # dropdown overrides: "auto"/missing = let the intake's choice stand
            opts = data.get("opts") if isinstance(data.get("opts"), dict) else {}
            try:
                turns = max(10, min(80, int(out.get("turns") or 40)))
                # >=2 rounds: the opus critic often demands proof on round 1 —
                # one revise pass keeps correct work from ending "exhausted"
                rounds = max(2, min(5, int(out.get("rounds") or 3)))
            except (TypeError, ValueError):
                turns, rounds = 40, 3
            try:
                if str(opts.get("turns") or "auto") != "auto":
                    turns = max(5, min(100, int(opts["turns"])))
                if str(opts.get("rounds") or "auto") != "auto":
                    rounds = max(1, min(5, int(opts["rounds"])))  # explicit 1 respected
            except (TypeError, ValueError):
                pass
            model = opts.get("model") if opts.get("model") in ("haiku", "sonnet", "opus", "default") \
                else str(out.get("model") or "default")
            critic = "sonnet" if opts.get("critic") == "sonnet" else "opus"
            account = str(opts.get("account") or "auto")
            auto = not bool(opts.get("gate"))  # gate=True -> verdicts wait on Daniel
            oid, err = orchestrator.start(
                brief, name=(out.get("name") or text[:40]).strip()[:40],
                model=model, critic=critic,
                turns=turns, rounds=rounds, auto=auto, skip=True,
                workdir=wd, account=account)
            if err:
                return self._json(400, {"error": err})
            out["oid"] = oid
            emit(session="operator", event="mission",
                 detail=("[%s] command bar: %s" % (oid, out.get("name") or brief))[:200])
        return self._json(200, out)

    def api_chat(self, data):
        out = chat.ask(str(data.get("message") or ""),
                       history=data.get("history") if isinstance(data.get("history"), list) else [],
                       model=(str(data.get("model")) if data.get("model") else None))
        return self._json(200 if "reply" in out else 502, out)

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
                 "opus": "opus"}.get(str(data.get("model", "")).lower(), "")
        try:
            budget = max(1, min(100, int(data.get("budget") or 40)))
        except (TypeError, ValueError):
            budget = 40
        safe = re.sub(r'[&|<>^%"\r\n]', " ", mission).strip()
        sid = uuid.uuid4().hex[:8]
        # human name so the manager reads like a team, not a hex dump
        name = (data.get("name") or "").strip()[:40] or re.sub(r"\s+", " ", mission)[:40]
        title = "MAESTRO " + sid
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
    print("Maestro: http://127.0.0.1:%d/dashboard/" % port)
    print("serving %s (Ctrl+C to stop)" % ROOT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
