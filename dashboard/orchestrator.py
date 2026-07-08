#!/usr/bin/env python3
"""The conductor loop: run a mission headless, read what it produced, critique
it, and re-prompt — accept / revise / reject — until done or the round budget
is spent. This is what makes instances autonomous instead of Daniel-prompted.

Each round:
  worker  claude -p [--resume]  --output-format json   (the doer)
  critic  claude -p --model haiku --max-turns 1        (reads the pipeline)
  verdict accept -> done | revise -> feedback becomes the next prompt | reject -> stop
auto=False parks the loop at "waiting" after each critic verdict so Daniel can
override from the dashboard (POST /api/orch-action).

State: one JSON per run in state/orchestrations/<oid>.json — the dashboard
polls GET /api/orchestrations. Every step also hits the wire (mirror.py), and
workers run with MAESTRO_SID=<oid> so their hook events land under this id.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ODIR = os.path.join(ROOT, "state", "orchestrations")
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")
IS_WIN = sys.platform == "win32"
WORKER_TIMEOUT = 1800
CRITIC_TIMEOUT = 240

# in-process registry: oid -> {"thread","proc","stop","human"}
LIVE = {}
LOCK = threading.Lock()

CRITIC_PROMPT = """You are the critic in an orchestration loop. Judge whether the worker completed the mission. Do NOT use any tools — judge only from the report below.

MISSION:
%s

WORKER'S LATEST REPORT:
---
%s
---

Reply with ONLY one JSON object, no prose, no code fences:
{"verdict":"accept","feedback":""} if the mission is genuinely complete,
{"verdict":"revise","feedback":"<the exact, concrete next instruction for the worker>"} if there is progress but work remains,
{"verdict":"reject","feedback":"<why>"} if the worker is fundamentally off-track or doing something unsafe."""


def emit(oid, detail):
    subprocess.run([sys.executable, MIRROR, "--session", oid, "--event", "orchestrate",
                    "--detail", detail[:200]], capture_output=True)


def _path(oid):
    return os.path.join(ODIR, oid + ".json")


def _save(o):
    o["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = _path(o["oid"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(o["oid"]))


def list_all():
    out = []
    if os.path.isdir(ODIR):
        for fn in os.listdir(ODIR):
            if fn.endswith(".json"):
                try:
                    o = json.load(open(os.path.join(ODIR, fn), encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                with LOCK:
                    live = o["oid"] in LIVE and LIVE[o["oid"]]["thread"].is_alive()
                o["live"] = live
                if o.get("status") in ("running", "waiting") and not live:
                    o["status"] = "stalled"  # server restarted mid-run
                out.append(o)
    out.sort(key=lambda o: o.get("started", ""), reverse=True)
    return out


def _claude(oid, argv, prompt, timeout, cwd):
    """One claude -p call; prompt via stdin (survives quotes/newlines on cmd)."""
    env = dict(os.environ, MAESTRO_SID=oid)
    p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8",
                         shell=IS_WIN, env=env)
    with LOCK:
        if oid in LIVE:
            LIVE[oid]["proc"] = p
    try:
        out, err = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return {"is_error": True, "result": "timed out after %ss" % timeout}
    finally:
        with LOCK:
            if oid in LIVE:
                LIVE[oid]["proc"] = None
    if p.returncode != 0 and not out.strip():
        return {"is_error": True, "result": (err or "exit %d" % p.returncode).strip()[:2000]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"is_error": True, "result": (out or err).strip()[:2000]}


def _verdict(text):
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            j = json.loads(m.group(0))
            if j.get("verdict") in ("accept", "revise", "reject"):
                return j["verdict"], str(j.get("feedback") or "")
        except json.JSONDecodeError:
            pass
    # unparseable critic never auto-accepts
    return "revise", "Critic reply was unparseable; continue the mission: " + text[:300]


def _wait_human(oid, o):
    o["status"] = "waiting"
    _save(o)
    emit(oid, "waiting for Daniel's verdict (round %d)" % o["round"])
    while True:
        with LOCK:
            st = LIVE.get(oid) or {}
            if st.get("stop"):
                return ("stop", "")
            if st.get("human"):
                v = st["human"]
                st["human"] = None
                return v
        time.sleep(0.5)


def _run(oid):
    o = json.load(open(_path(oid), encoding="utf-8"))
    cwd = o.get("dir") or ROOT
    prompt = o["mission"]
    sid = None
    try:
        for rnd in range(1, o["rounds"] + 1):
            with LOCK:
                if LIVE.get(oid, {}).get("stop"):
                    o["status"] = "stopped"
                    _save(o)
                    return
            o["round"] = rnd
            o["status"] = "running"
            _save(o)
            argv = ["claude", "-p", "--output-format", "json",
                    "--max-turns", str(o["turns"])]
            if sid:
                argv += ["--resume", sid]
            if o.get("model") and o["model"] != "default":
                argv += ["--model", o["model"]]
            if o.get("skip", True):
                argv.append("--dangerously-skip-permissions")
            emit(oid, "round %d/%d worker: %s" % (rnd, o["rounds"], prompt))
            t0 = time.time()
            w = _claude(oid, argv, prompt, WORKER_TIMEOUT, cwd)
            sid = w.get("session_id") or sid
            o["cost"] = round(o.get("cost", 0) + (w.get("total_cost_usd") or 0), 4)
            # a worker that burns its whole turn budget mid-work is not a crash —
            # it has no final message, but the critic can still steer the resume
            ran_out = w.get("subtype") == "error_max_turns"
            report = str(w.get("result") or "")[:6000] or (
                "(no final message — the worker used all %d turns mid-work; "
                "its session resumes where it stopped)" % o["turns"])
            o["turns_log"].append({
                "role": "worker", "round": rnd, "prompt": prompt,
                "result": report,
                "error": bool(w.get("is_error")) and not ran_out,
                "secs": round(time.time() - t0), "cost": w.get("total_cost_usd")})
            _save(o)
            if w.get("is_error") and not ran_out:
                o["status"] = "error"
                _save(o)
                emit(oid, "worker error: " + report[:120])
                return
            cargv = ["claude", "-p", "--output-format", "json", "--max-turns", "2",
                     "--disallowedTools", "*",
                     "--model", o.get("critic") or "haiku"]
            c = _claude(oid, cargv, CRITIC_PROMPT % (o["mission"], report),
                        CRITIC_TIMEOUT, cwd)
            o["cost"] = round(o.get("cost", 0) + (c.get("total_cost_usd") or 0), 4)
            verdict, feedback = _verdict(str(c.get("result") or ""))
            o["turns_log"].append({"role": "critic", "round": rnd,
                                   "verdict": verdict, "feedback": feedback[:2000]})
            _save(o)
            emit(oid, "round %d critic: %s — %s" % (rnd, verdict, feedback))
            if not o.get("auto", True):
                hv, hf = _wait_human(oid, o)
                if hv == "stop":
                    o["status"] = "stopped"
                    _save(o)
                    return
                verdict, feedback = hv, (hf or feedback)
                emit(oid, "round %d Daniel: %s" % (rnd, verdict))
            if verdict == "accept":
                o["status"] = "done"
                _save(o)
                emit(oid, "mission accepted after %d round(s), $%s" % (rnd, o["cost"]))
                return
            if verdict == "reject":
                o["status"] = "rejected"
                _save(o)
                emit(oid, "mission rejected: " + feedback[:120])
                return
            prompt = feedback or "Continue the mission and report concretely what changed."
        o["status"] = "exhausted"
        _save(o)
        emit(oid, "round budget exhausted without acceptance")
    except Exception as e:  # never leave a run stuck at "running" on a crash
        o["status"] = "error"
        o["turns_log"].append({"role": "system", "round": o.get("round", 0),
                               "result": repr(e)[:500]})
        _save(o)
        emit(oid, "orchestrator crashed: " + repr(e)[:120])
    finally:
        with LOCK:
            LIVE.pop(oid, None)


def start(mission, name="", model="default", critic="haiku", turns=40, rounds=3,
          auto=True, skip=True, workdir=""):
    os.makedirs(ODIR, exist_ok=True)
    oid = uuid.uuid4().hex[:8]
    workdir = workdir.strip()
    if workdir and not os.path.isdir(workdir):
        return None, "workdir does not exist: " + workdir
    o = {"oid": oid, "name": name[:40] or mission[:40], "mission": mission[:2000],
         "dir": workdir, "model": model, "critic": critic,
         "turns": max(1, min(100, turns)), "rounds": max(1, min(8, rounds)),
         "auto": bool(auto), "skip": bool(skip), "status": "running", "round": 0,
         "cost": 0, "turns_log": [],
         "started": datetime.datetime.now().isoformat(timespec="seconds")}
    _save(o)
    t = threading.Thread(target=_run, args=(oid,), daemon=True)
    with LOCK:
        LIVE[oid] = {"thread": t, "proc": None, "stop": False, "human": None}
    t.start()
    emit(oid, "orchestration started: %s (%s worker / %s critic, %d rounds x %d turns, %s)"
         % (o["name"], model, critic, o["rounds"], o["turns"],
            "auto" if auto else "manual approval"))
    return oid, None


def action(oid, act, feedback=""):
    with LOCK:
        st = LIVE.get(oid)
        if not st:
            return "not running (finished or server restarted)"
        if act == "stop":
            st["stop"] = True
            if st.get("proc"):
                try:
                    st["proc"].kill()
                except OSError:
                    pass
            return None
        if act in ("accept", "revise", "reject"):
            st["human"] = (act, feedback)
            return None
    return "unknown action"
