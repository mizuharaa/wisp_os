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

import pulse  # account routing: which Claude account this loop delegates to
import runtime as agent_runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from hermes import hermes as hermes_store

ODIR = os.path.join(ROOT, "state", "orchestrations")
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")
IS_WIN = sys.platform == "win32"
WORKER_TIMEOUT = 1800
CRITIC_TIMEOUT = 240
MAX_TRANSIENT_RETRIES = 2
RETRY_BACKOFF_BASE = 0.5

# in-process registry: oid -> {"thread","proc","stop","human"}
LIVE = {}
LOCK = threading.RLock()

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


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save(o):
    # Serialize against action(stop): a completion that arrives after Stop may
    # update telemetry, but it may never transition the persisted run away from
    # stopped.
    with LOCK:
        live = LIVE.get(o.get("oid")) or {}
        thread = live.get("thread")
        thread_alive = not thread or not hasattr(thread, "is_alive") or thread.is_alive()
        if live.get("stop") and thread_alive:
            o["status"] = "stopped"
            o["detail"] = "stopped by operator"
            o["next_action"] = "Start a new loop if more work is needed."
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
                    o = _load_json(os.path.join(ODIR, fn))
                except (OSError, json.JSONDecodeError):
                    continue
                if o.get("dismissed"):
                    continue
                with LOCK:
                    live = o["oid"] in LIVE and LIVE[o["oid"]]["thread"].is_alive()
                o["live"] = live
                if o.get("status") in ("running", "retrying", "waiting") and not live:
                    o["status"] = "stalled"  # server restarted mid-run
                    o["detail"] = "the dashboard server stopped during this loop"
                out.append(o)
    out.sort(key=lambda o: o.get("started", ""), reverse=True)
    return out


def _claude(oid, argv, prompt, timeout, cwd, config_dir=None,
            recall_route="orchestrator_worker", brain_recall=True):
    """One claude -p call; prompt via stdin (survives quotes/newlines on cmd)."""
    env = dict(os.environ, MAESTRO_SID=oid,
               MAESTRO_RECALL_ROUTE=recall_route)
    if not brain_recall:
        # The critic must judge only the mission/report supplied below. Prior
        # memories would frame that verdict and make the feedback loop less
        # independent. Worker prompts still use the mandatory recall hook.
        env["MAESTRO_SKIP_BRAIN_RECALL"] = "1"
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir  # run under the delegated account
    p = subprocess.Popen(
        argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", shell=IS_WIN,
        env=env, start_new_session=not IS_WIN,
        creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                       if IS_WIN else 0))
    stop_after_spawn = False
    with LOCK:
        if oid in LIVE:
            LIVE[oid]["proc"] = p
            stop_after_spawn = bool(LIVE[oid].get("stop"))
    if stop_after_spawn:
        agent_runtime.terminate_process_tree(p)
        try:
            p.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        with LOCK:
            if oid in LIVE and LIVE[oid].get("proc") is p:
                LIVE[oid]["proc"] = None
        return {"is_error": True, "result": "stopped by operator"}
    try:
        out, err = p.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        agent_runtime.terminate_process_tree(p)
        try:
            p.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
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


def _stopped(oid):
    with LOCK:
        return bool(LIVE.get(oid, {}).get("stop"))


def _call_with_transient_retries(oid, o, actor, call):
    """Run worker/critic with a bounded retry only for classified transients."""
    retries, total_cost = 0, 0.0
    while True:
        if _stopped(oid):
            return {"is_error": True, "result": "stopped by operator",
                    "classification": "stopped"}
        value = call()
        if _stopped(oid):
            return {"is_error": True, "result": "stopped by operator",
                    "classification": "stopped", "retry_cost_usd": total_cost,
                    "retry_count": retries}
        total_cost += value.get("total_cost_usd") or 0
        detail = str(value.get("result") or "")
        classification = agent_runtime.classify_failure(
            detail, bool(value.get("is_error")), value.get("subtype") or "")
        value["classification"] = classification
        value["retry_cost_usd"] = total_cost
        value["retry_count"] = retries
        if (not value.get("is_error") or
                classification not in ("transient", "transient_limit") or
                retries >= MAX_TRANSIENT_RETRIES):
            return value
        retries += 1
        o["turns_log"].append({
            "role": "system", "round": o.get("round", 0),
            "result": "%s %s; retry %d/%d" %
                      (actor, classification, retries, MAX_TRANSIENT_RETRIES),
            "classification": classification,
        })
        o["status"] = "retrying"
        o["detail"] = ("%s %s; bounded retry %d/%d" %
                       (actor, classification, retries, MAX_TRANSIENT_RETRIES))
        o["next_action"] = "Retrying automatically after a short backoff."
        _save(o)
        emit(oid, o["detail"])
        if not agent_runtime.wait_backoff(
                lambda: _stopped(oid), retries, base=RETRY_BACKOFF_BASE):
            return {"is_error": True, "result": "stopped by operator",
                    "classification": "stopped"}


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
    o = _load_json(_path(oid))
    cwd = o.get("dir") or ROOT
    prompt = o["mission"]
    sid = o.get("session_id") or None
    # resolve which Claude account this loop delegates to. "auto" picks the
    # account with the most headroom right now (least tokens used this window).
    acct = o.get("account") or ""
    if acct == "auto":
        acct = pulse.least_used()
        o["account_resolved"] = acct
        _save(o)
    cfg_dir = pulse.dir_for(acct) if acct else ""
    if acct:
        emit(oid, "delegating to account: %s" % acct)
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
            if _stopped(oid):
                o["status"] = "stopped"
                o["detail"] = "stopped by operator"
                _save(o)
                return
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
            w = _call_with_transient_retries(
                oid, o, "worker",
                lambda: _claude(oid, argv, prompt, WORKER_TIMEOUT, cwd, cfg_dir))
            if w.get("classification") == "stopped":
                o["status"] = "stopped"
                o["detail"] = "stopped by operator"
                _save(o)
                return
            o["status"] = "running"
            o["detail"] = ""
            o["next_action"] = "Critic will verify the worker report."
            sid = w.get("session_id") or sid
            o["session_id"] = sid
            o["cost"] = round(o.get("cost", 0) +
                              (w.get("retry_cost_usd", w.get("total_cost_usd") or 0)), 4)
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
                "classification": w.get("classification"),
                "retries": w.get("retry_count", 0),
                "secs": round(time.time() - t0),
                "cost": w.get("retry_cost_usd", w.get("total_cost_usd"))})
            _save(o)
            if w.get("is_error") and not ran_out:
                o["status"] = "error"
                o["detail"] = "%s: %s" % (w.get("classification") or "task", report[:300])
                o["next_action"] = "Inspect the retry evidence and start a new loop if appropriate."
                _save(o)
                emit(oid, "worker error: " + report[:120])
                return
            # the critic IS the core's judgment — never a small model (Daniel's
            # call: outputs are the product, don't risk them). One report per
            # round keeps opus cost bounded.
            cargv = ["claude", "-p", "--output-format", "json", "--max-turns", "2",
                     "--disallowedTools", "*",
                     "--model", o.get("critic") or "opus"]
            c = _call_with_transient_retries(
                oid, o, "critic",
                lambda: _claude(oid, cargv,
                                CRITIC_PROMPT % (o["mission"], report),
                                CRITIC_TIMEOUT, cwd, cfg_dir,
                                recall_route="orchestrator_critic",
                                brain_recall=False))
            if c.get("classification") == "stopped":
                o["status"] = "stopped"
                o["detail"] = "stopped by operator"
                _save(o)
                return
            o["status"] = "running"
            o["detail"] = ""
            o["next_action"] = "Applying the critic verdict."
            o["cost"] = round(o.get("cost", 0) +
                              (c.get("retry_cost_usd", c.get("total_cost_usd") or 0)), 4)
            if c.get("is_error"):
                o["status"] = "error"
                o["detail"] = "%s critic failure: %s" % (
                    c.get("classification") or "task",
                    agent_runtime.safe_excerpt(c.get("result"), 280))
                o["next_action"] = "Retry the loop after the critic service recovers."
                o["turns_log"].append({"role": "critic", "round": rnd,
                                       "error": True,
                                       "classification": c.get("classification"),
                                       "feedback": o["detail"]})
                _save(o)
                emit(oid, o["detail"])
                return
            verdict, feedback = _verdict(str(c.get("result") or ""))
            o["turns_log"].append({"role": "critic", "round": rnd,
                                   "verdict": verdict, "feedback": feedback[:2000],
                                   "retries": c.get("retry_count", 0)})
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
                # learn — but only when worth remembering: the brain holds signal,
                # not every accepted run. Keep it if the loop actually cost real
                # tokens, iterated (>=2 rounds), or ran hard reasoning.
                worth = ((o.get("cost") or 0) >= 0.15 or rnd >= 2
                         or o.get("model") in ("opus", "fable"))
                if worth:
                    try:
                        learned = hermes_store.note_memory(
                            agent_runtime.safe_excerpt(o["mission"], 180),
                            agent_runtime.safe_excerpt(
                                "accepted after %d round(s): %s" % (rnd, report), 400),
                            "mission,orchestrated", source="loop:" + oid)
                        quality = learned.get("quality") if isinstance(
                            learned.get("quality"), dict) else {}
                        o["learning_receipt"] = {
                            "outcome": learned.get("outcome"),
                            "id": learned.get("id"),
                            "reason_code": learned.get("reason_code"),
                            "quality_score": quality.get("score"),
                            "quality_threshold": quality.get("threshold"),
                        }
                        _save(o)
                        emit(oid, "brain %s: %s" % (
                            learned.get("outcome"), learned.get("reason_code")))
                    except Exception as learn_error:
                        emit(oid, "brain note failed (mission unaffected): " +
                             type(learn_error).__name__)
                else:
                    emit(oid, "skipped brain note (routine run — brain holds signal)")
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
        if _stopped(oid):
            o["status"] = "stopped"
            o["detail"] = "stopped by operator"
            _save(o)
        else:
            o["status"] = "error"
            o["turns_log"].append({"role": "system", "round": o.get("round", 0),
                                   "result": repr(e)[:500]})
            _save(o)
            emit(oid, "orchestrator crashed: " + repr(e)[:120])
    finally:
        try:
            from memory import recall_engine
            recall_engine.record_outcome(ROOT, oid, o.get("status"))
        except Exception:
            pass  # outcome telemetry can never change mission state
        with LOCK:
            LIVE.pop(oid, None)


def start(mission, name="", model="default", critic="opus", turns=40, rounds=3,
          auto=True, skip=True, workdir="", account=""):
    os.makedirs(ODIR, exist_ok=True)
    oid = uuid.uuid4().hex[:8]
    workdir = workdir.strip()
    if workdir and not os.path.isdir(workdir):
        return None, "workdir does not exist: " + workdir
    o = {"oid": oid, "name": name[:40] or mission[:40], "mission": mission[:2000],
         "dir": workdir, "model": model, "critic": critic, "account": account,
         "turns": max(1, min(100, turns)), "rounds": max(1, min(8, rounds)),
         "auto": bool(auto), "skip": bool(skip), "status": "running", "round": 0,
         "cost": 0, "turns_log": [], "detail": "", "next_action": "Worker is starting.",
         "session_id": None,
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


def dismiss(oid):
    """Hide a finished loop without deleting its record. Refuses a live run."""
    with LOCK:
        st = LIVE.get(oid)
        if st and st["thread"].is_alive():
            return "can't dismiss a running loop — stop it first"
    try:
        o = _load_json(_path(oid))
    except (OSError, json.JSONDecodeError):
        return "no such loop"
    if not o.get("dismissed"):
        o["dismissed"] = True
        _save(o)
    return None


def action(oid, act, feedback=""):
    # A finished loop is popped from LIVE the moment _run()'s finally block
    # runs, so dismiss must be checked before the LIVE gate below -- routed
    # through it, dismiss would always fail with "not running" for exactly
    # the loops it needs to dismiss.
    if act == "dismiss":
        return dismiss(oid)
    proc = None
    with LOCK:
        st = LIVE.get(oid)
        if not st:
            return "not running (finished or server restarted)"
        if act == "stop":
            st["stop"] = True
            proc = st.get("proc")
        if act in ("accept", "revise", "reject"):
            st["human"] = (act, feedback)
            return None
    if act == "stop":
        agent_runtime.terminate_process_tree(proc)
        try:
            o = _load_json(_path(oid))
            o["status"] = "stopped"
            o["detail"] = "stopped by operator"
            o["next_action"] = "Start a new loop if more work is needed."
            _save(o)
        except (OSError, json.JSONDecodeError):
            pass
        return None
    return "unknown action"
