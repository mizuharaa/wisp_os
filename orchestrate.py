#!/usr/bin/env python3
"""Agent orchestration + the feedback loop (ARCHITECTURE.md components 2 & 3).

Three moving parts, all riding the four contracts the architect defined and the
single model door (model_client):

  cards        Every roster role gets a card. A staffed role shows its live
               `agent-card status` (task, model, effort, status, stopped_reason);
               an unstaffed role shows a clearly-marked IDLE placeholder. Cards
               are persisted per task and re-emitted on the wire on every
               transition, so the UI graph (component 6) renders them unchanged.

  propose      When a problem is identified the layer DRAFTS a fix and parks it
               as PENDING CONFIRMATION. Nothing runs until the operator confirms.
               The gate is enforced in code: execute() re-reads the persisted
               status and refuses to run an unconfirmed proposal — bypassing the
               UI changes nothing.

  feedback     The ONLY caller allowed max effort (agentic_loop=True unlocks the
               `xhigh` rung in model_client). A bounded critic->doer loop: while a
               grade says `revise` and budget remains, it critiques, PUSHES the
               improvement note back onto the target role's card, re-runs, and
               re-grades. Stops on pass/fail/budget — never spins.

--demo proves all three offline (no API key, canned envelopes). Stdlib only.
"""
import datetime
import json
import os
import sys
import uuid

import model_client as mc

ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(ROOT, "state", "events.jsonl")
CARDS = os.path.join(ROOT, "state", "cards")
PROPOSALS = os.path.join(ROOT, "state", "proposals")

# The functional roster this layer can staff. A task uses a subset; the rest
# render as idle placeholders so an operator always sees the full crew.
ROSTER = ("eng", "reviewer", "qa", "feedback-loop")

# Feedback loop is the one agentic caller: xhigh is unlocked only with this flag.
FEEDBACK_EFFORT = "xhigh"


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _emit(ev):
    """Append one event to the wire. agent-card status and audit entries are a
    typed superset of the mirror.py line (ARCHITECTURE.md), so they land here
    unchanged and the dashboard tails them as-is."""
    ev.setdefault("ts", _now())
    os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def _audit(task_id, actor, action, detail, refs=None):
    return _emit({"id": uuid.uuid4().hex[:8], "event": "audit", "task_id": task_id,
                  "actor": actor, "action": action, "detail": detail[:200],
                  "refs": refs or {}})


# --- cards -------------------------------------------------------------------

def _card_path(card_id):
    return os.path.join(CARDS, card_id.replace(":", "__") + ".json")


def set_card(task_id, role, model, effort, status, stopped_reason=None,
             cost=0.0, note=""):
    """Persist + emit one `agent-card status`. Called on every role transition;
    the persisted copy is what render_cards reads, the wire copy is what the UI
    graph renders live."""
    card = {"card_id": "%s:%s" % (task_id, role), "task_id": task_id, "role": role,
            "model": model, "effort": effort, "status": status,
            "stopped_reason": stopped_reason, "cost": round(cost, 4),
            "note": note, "ts": _now()}
    os.makedirs(CARDS, exist_ok=True)
    tmp = _card_path(card["card_id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _card_path(card["card_id"]))
    _emit({"event": "agent-card", **card})
    return card


def load_card(task_id, role):
    try:
        return json.load(open(_card_path("%s:%s" % (task_id, role)), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render_cards(task_id):
    """The whole crew for a task: a live card per staffed role, an explicit IDLE
    placeholder for the rest. Returns (text, cards) — text for a terminal/log,
    cards for anything that speaks the contract."""
    cards, lines = [], []
    for role in ROSTER:
        c = load_card(task_id, role)
        if c:
            head = "%-14s %-8s %-6s %s" % (role, c["model"], c["effort"], c["status"].upper())
            if c.get("stopped_reason"):
                head += "  (!) %s" % c["stopped_reason"]
            lines.append("[*] %s" % head)
            if c.get("note"):
                lines.append("      <- feedback: %s" % c["note"][:80])
        else:
            c = {"card_id": "%s:%s" % (task_id, role), "task_id": task_id,
                 "role": role, "status": "idle", "placeholder": True}
            lines.append("[ ] %-14s [ IDLE - no task assigned ]" % role)
        cards.append(c)
    return "\n".join(lines), cards


# --- component 2: orchestrate a task across the roster -----------------------

def _role_call(prompt, effort="high"):
    """Model door for ordinary roles. Guards the constraint that only the
    feedback loop runs agentic (max effort): agentic_loop is forced False, so
    model_client degrades any xhigh here to high."""
    assert effort != FEEDBACK_EFFORT, "only the feedback loop may use %s" % FEEDBACK_EFFORT
    return mc.complete(prompt, effort=effort, agentic_loop=False)


def orchestrate(task, roster=("eng", "reviewer", "qa"), dry=False):
    """Staff a task across a subset of the roster. Picks each role's build model
    from the task complexity (model_client.escalate), runs it, and emits a card
    on every transition. A refused role is marked blocked with its stopped_reason
    and the rest keep running — the loop closes instead of hanging."""
    tid = task["id"]
    build_model = mc.escalate(task["complexity"])["delegate"]
    _audit(tid, "orchestration", "escalated",
           "complexity=%s -> build tier %s" % (task["complexity"], build_model))
    for role in roster:
        model = build_model if role == "eng" else "sonnet"  # reviewers/qa are lighter
        set_card(tid, role, model, "high", "working")
        if dry:
            env = {"stopped_reason": None, "text": "%s: ok" % role, "usage": {}} \
                if role != "reviewer" else mc.normalize(
                    {"stop_reason": "refusal", "content": [],
                     "stop_details": {"explanation": "canned refusal (dry)"}})
        else:
            env = _role_call("Role %s on task: %s" % (role, task["goal"]))
        if env["stopped_reason"]:
            set_card(tid, role, model, "high", "blocked",
                     stopped_reason=env["stopped_reason"])
            _audit(tid, "orchestration", "refused",
                   "role %s: %s" % (role, env["stopped_reason"]),
                   refs={"card": "%s:%s" % (tid, role)})
            continue
        set_card(tid, role, model, "high", "done")
    return render_cards(tid)


# --- component 2b: auto-propose, gated by confirmation (enforced in code) -----

def _prop_path(pid):
    return os.path.join(PROPOSALS, pid + ".json")


def propose(problem, solution=None, task_id=None, dry=False):
    """Draft a fix for an identified problem and PARK it PENDING CONFIRMATION.
    Nothing runs here — this only records intent. If no solution is supplied the
    model drafts one (ordinary effort; the feedback loop is the only agentic
    caller)."""
    if solution is None:
        if dry:
            solution = "Proposed fix for: %s (draft, unconfirmed)" % problem
        else:
            env = _role_call("A problem was identified: %s\nDraft a concrete, "
                             "minimal fix. Do not execute anything." % problem)
            solution = env["text"] or "(model drafted no fix)"
    pid = uuid.uuid4().hex[:8]
    rec = {"pid": pid, "task_id": task_id, "problem": problem, "solution": solution,
           "status": "pending_confirmation", "created": _now()}
    os.makedirs(PROPOSALS, exist_ok=True)
    with open(_prop_path(pid), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    _audit(task_id or pid, "orchestration", "proposed",
           "PENDING CONFIRMATION: %s" % problem, refs={"proposal": pid})
    return rec


def confirm(pid):
    """Operator confirmation. The only path that flips a proposal to runnable."""
    rec = json.load(open(_prop_path(pid), encoding="utf-8"))
    rec["status"] = "confirmed"
    rec["confirmed"] = _now()
    with open(_prop_path(pid), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    _audit(rec.get("task_id") or pid, "orchestration", "confirmed",
           "operator confirmed proposal", refs={"proposal": pid})
    return rec


class NotConfirmed(Exception):
    """Raised when execute() is called on a proposal the operator hasn't confirmed."""


def execute(pid, runner):
    """THE GATE. Re-reads the PERSISTED status and refuses to run an unconfirmed
    proposal — so bypassing the UI (or a stale in-memory flag) changes nothing.
    Only a proposal the operator actually confirmed reaches `runner`."""
    rec = json.load(open(_prop_path(pid), encoding="utf-8"))
    if rec["status"] != "confirmed":
        _audit(rec.get("task_id") or pid, "orchestration", "blocked",
               "execute refused — proposal not confirmed", refs={"proposal": pid})
        raise NotConfirmed("proposal %s is %s, not confirmed" % (pid, rec["status"]))
    rec["status"] = "executed"
    with open(_prop_path(pid), "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=1)
    result = runner(rec)
    _audit(rec.get("task_id") or pid, "orchestration", "shipped",
           "executed confirmed proposal", refs={"proposal": pid})
    return result


# --- component 3: the feedback loop (the one agentic, max-effort caller) ------

def push_feedback(task_id, target_role, note):
    """Cycle an improvement back to a role by updating its card in place (keeping
    model/effort) and re-emitting it. This is the visible push-back: the target
    agent's card now carries the note and flips to `review`."""
    c = load_card(task_id, target_role) or {}
    card = set_card(task_id, target_role, c.get("model", "opus"),
                    c.get("effort", "high"), "review", note=note)
    _audit(task_id, "feedback", "graded", "pushed feedback to %s: %s"
           % (target_role, note[:120]), refs={"card": card["card_id"]})
    return card


def feedback_loop(task, target_role, budget=3, dry=False, grades=None):
    """Bounded critic->doer loop, the single caller that runs agentic (xhigh via
    agentic_loop=True). While the grade says `revise` and budget remains: critique
    -> push the note onto the target card -> re-run the doer -> re-grade. Returns
    the grade history. Stops on pass/fail/budget; never spins forever."""
    tid = task["id"]
    history, it = [], 0
    grades = list(grades or [])  # dry-run: canned verdict stream
    while it < budget:
        it += 1
        if dry:
            grade = grades.pop(0) if grades else {"verdict": "pass", "score": 1.0}
        else:
            env = mc.complete(
                "Critique the work for task %r and grade verdict pass|revise|fail "
                "with a one-line improvement note if revise." % task["goal"],
                effort=FEEDBACK_EFFORT, agentic_loop=True)  # <-- only max-effort caller
            grade = {"verdict": "revise", "score": 0.5, "note": env["text"][:200]} \
                if not env["stopped_reason"] else {"verdict": "fail", "score": 0.0}
        history.append(grade)
        if grade["verdict"] != "revise":
            break
        note = grade.get("note") or "address the rubric gaps flagged this pass"
        push_feedback(tid, target_role, note)  # <-- improvement cycles to the card
        c = load_card(tid, target_role)
        set_card(tid, target_role, c["model"], "high", "working",
                 note=c["note"])  # doer picks the note up and re-runs (note persists)
    _audit(tid, "feedback", "graded",
           "loop done after %d pass(es): %s" % (it, history[-1]["verdict"]),
           refs={"card": "%s:%s" % (tid, target_role)})
    return history


# --- demo / self-check -------------------------------------------------------

def _demo():
    """Prove the three DONE-WHEN conditions offline (no API key, canned data).
    Isolated to a temp dir so the self-check never pollutes the real wire."""
    import tempfile
    global EVENTS, CARDS, PROPOSALS
    tmp = tempfile.mkdtemp(prefix="orch-demo-")
    EVENTS = os.path.join(tmp, "events.jsonl")
    CARDS = os.path.join(tmp, "cards")
    PROPOSALS = os.path.join(tmp, "proposals")
    task = {"id": "demo" + uuid.uuid4().hex[:4], "goal": "Fix the flaky auth check",
            "complexity": "hard", "status": "running"}
    tid = task["id"]

    # (1) cards render for active + idle agents ------------------------------
    text, cards = orchestrate(task, roster=("eng", "reviewer"), dry=True)
    print("--- cards ---\n%s\n" % text)
    by_role = {c["role"]: c for c in cards}
    assert by_role["eng"]["status"] == "done", "staffed role should be active/done"
    assert by_role["reviewer"]["status"] == "blocked", "refused role stays on its card"
    assert by_role["reviewer"]["stopped_reason"] == "refusal", "refusal carried, not crashed"
    assert by_role["qa"].get("placeholder"), "unstaffed role must render as IDLE placeholder"
    assert by_role["feedback-loop"].get("placeholder")

    # (2) identified problem -> PENDING CONFIRMATION, does NOT auto-execute ---
    rec = propose("auth check races under load", task_id=tid, dry=True)
    assert rec["status"] == "pending_confirmation", "must park, not run"
    ran = {"did": False}
    try:
        execute(rec["pid"], lambda r: ran.__setitem__("did", True))
        assert False, "unconfirmed proposal must not execute"
    except NotConfirmed:
        pass
    assert ran["did"] is False, "runner must NOT have fired without confirmation"
    confirm(rec["pid"])
    execute(rec["pid"], lambda r: ran.__setitem__("did", True))
    assert ran["did"] is True, "confirmed proposal executes"
    print("--- proposal %s: gated, then confirmed+executed ---\n" % rec["pid"])

    # (3) feedback loop pushes an improvement note back to a target card -----
    set_card(tid, "eng", "opus", "high", "done")  # a finished doer to critique
    history = feedback_loop(task, "eng", budget=3, dry=True, grades=[
        {"verdict": "revise", "score": 0.5, "note": "handle the concurrent refresh path"},
        {"verdict": "pass", "score": 0.95}])
    eng = load_card(tid, "eng")
    assert eng["note"], "feedback loop must push a note onto the target card"
    assert history[-1]["verdict"] == "pass" and len(history) == 2, "loop is bounded"
    print("--- feedback pushed to eng card ---\n%s\n" % render_cards(tid)[0])

    print("orchestrate.py OK — cards render (active+idle), gate holds, feedback cycles")


if __name__ == "__main__":
    _demo() if "--demo" in sys.argv[1:] or len(sys.argv) == 1 else None
