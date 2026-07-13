#!/usr/bin/env python3
"""Daily production review pipeline (ARCHITECTURE.md component 1).

Two stages, one model door (model_client):

  scan       CHEAP tier (haiku) sweeps every system on the wire and emits a
             findings JSON. Each finding IS a `task` (source=daily-review) with
             a `complexity` label, so orchestration/grading read it unchanged.
  escalate   The manager (opus) writes a detailed A-Z execution plan for each
             finding and tags it with the model that should build it, derived
             from complexity via model_client.escalate() (light->sonnet,
             hard->opus, trivial->haiku, frontier->fable).

The whole point: a single model REFUSAL (HTTP 200 + stopped_reason from
model_client.normalize) marks that one finding blocked and the loop moves to the
next — it never hard-exits. --dry-run proves this offline with canned envelopes
(no API key, one deliberate mid-run refusal) so cron can be validated safely.

Run:  python review.py [--dry-run]     (loop.sh is the cron wrapper)
Stdlib only (repo rule: no deps).
"""
import datetime
import json
import os
import subprocess
import sys
import uuid

import model_client as mc

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(ROOT, "state", "review")
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")

# Findings the scan emits, shaped as the `task` contract plus scan-only extras
# (system/severity) — consumers read what they know and ignore the rest.
SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "system": {"type": "string"},   # which subsystem it came from
                    "goal": {"type": "string"},      # one-line what/why to fix
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "complexity": {"type": "string",
                                   "enum": list(mc._DELEGATE)},  # feeds escalate()
                },
                "required": ["system", "goal", "severity", "complexity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

SCAN_SYSTEM = """You are the daily production reviewer of an agentic OS. Given a \
snapshot of every system's recent state, sweep for items that need attention \
today: stalled tasks, unhandled directives, failing runs, decaying skills, \
recurring errors. Emit each as a finding with a one-line goal, a severity, and a \
complexity (trivial|light|hard|frontier) that sizes the fix. Report only what is \
actionable now; if a system is healthy, say nothing about it."""

MANAGER_SYSTEM = """You are the engineering manager of an agentic OS. For the one \
finding below, write a detailed, self-contained A-Z execution plan: lettered \
steps (A., B., C., ...) that a builder can follow end to end with no further \
context — each step concrete and verifiable. Then state the complexity. Do not \
do the work; plan it."""

MANAGER_SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},  # "A. ...", "B. ..."
        "complexity": {"type": "string", "enum": list(mc._DELEGATE)},
    },
    "required": ["steps", "complexity"],
    "additionalProperties": False,
}


def log(msg):
    """One timestamped line to stdout — loop.sh tees stdout to the logfile."""
    print("[%s] %s" % (datetime.datetime.now().isoformat(timespec="seconds"), msg),
          flush=True)


def wire(stage, detail):
    """Best-effort event on the wire; never let a logging failure break the run."""
    try:
        subprocess.run([sys.executable, MIRROR, "--stage", stage, "--event",
                        "review", "--detail", detail[:120]],
                       cwd=ROOT, capture_output=True, timeout=30)
    except Exception as e:  # the wire is telemetry, not the mission
        log("wire emit failed (%s) — continuing" % type(e).__name__)


# --- system snapshot: cheap context for the scan -----------------------------

def _tail(path, n):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def snapshot():
    """A compact, cheap picture of every system for the scan to reason over."""
    events = _tail(os.path.join(ROOT, "state", "events.jsonl"), 40)
    inbox = _tail(os.path.join(ROOT, "state", "inbox.jsonl"), 10)
    try:
        reg = json.load(open(os.path.join(ROOT, "skills", "registry.json"),
                             encoding="utf-8")).get("skills", {})
        skills = {k: {"status": v.get("status"), "uses": v.get("uses"),
                      "decay": v.get("decay")} for k, v in reg.items()}
    except (OSError, ValueError):
        skills = {}
    return {
        "recent_events": [ln.strip() for ln in events],
        "inbox": [ln.strip() for ln in inbox],
        "skills": skills,
    }


# --- dry-run: offline canned envelopes (no API, one deliberate refusal) -------

def _canned_scan():
    return {"findings": [
        {"system": "inbox", "goal": "Weekly skill-review directive 9d32e87a is "
         "unactioned", "severity": "medium", "complexity": "light"},
        {"system": "skills", "goal": "workflow-audit stuck at 2 uses — needs a "
         "third to earn", "severity": "low", "complexity": "trivial"},
        {"system": "events", "goal": "Reconcile duplicate ceo.py orchestration "
         "state on the wire", "severity": "high", "complexity": "hard"},
    ]}


def _canned_plan(finding, refuse):
    """Fake manager. Refuses on the flagged finding to prove the loop survives a
    refusal exactly as the live path would (same normalize() envelope)."""
    if refuse:
        return mc.normalize({"stop_reason": "refusal", "content": [],
                             "stop_details": {"explanation": "simulated safety "
                                              "refusal (dry-run)"}})
    text = json.dumps({"steps": [
        "A. Reproduce: %s" % finding["goal"],
        "B. Locate the owning module on the wire and read its recent events.",
        "C. Draft the minimal fix and its rollback.",
        "D. Apply, run the module self-check, and confirm green.",
        "E. Emit the audit entry and close the task.",
    ], "complexity": finding["complexity"]})
    return mc.normalize({"stop_reason": "end_turn",
                         "content": [{"type": "text", "text": text}]})


# --- stages ------------------------------------------------------------------

def scan(dry_run):
    """CHEAP tier sweep -> list of findings shaped as `task` records."""
    if dry_run:
        data = _canned_scan()
    else:
        env = mc.complete(
            "System snapshot:\n" + json.dumps(snapshot(), ensure_ascii=False),
            system=SCAN_SYSTEM, model=mc.MODEL_IDS["haiku"], effort="medium",
            schema=SCAN_SCHEMA)
        if env["stopped_reason"]:               # scan itself refused/errored
            log("SCAN stopped_reason=%s (%s) — no findings this run"
                % (env["stopped_reason"], env["detail"]))
            return []
        try:
            data = json.loads(env["text"])
        except (ValueError, TypeError):
            log("SCAN returned unparseable output — no findings this run")
            return []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    findings = []
    for f in data.get("findings", []):
        findings.append({
            "id": uuid.uuid4().hex[:8],
            "goal": f["goal"],
            "complexity": f.get("complexity", "hard"),
            "status": "pending",
            "source": "daily-review",
            "created": now,
            "system": f.get("system", "?"),
            "severity": f.get("severity", "medium"),
        })
    log("SCAN found %d item(s) needing attention" % len(findings))
    return findings


def escalate(findings, dry_run):
    """Manager writes an A-Z plan per finding, tagged complexity->model. A single
    refusal blocks THAT finding and the loop continues — it never aborts."""
    plans = []
    refuse_idx = 1 if dry_run else -1   # dry-run: refuse on the 2nd finding
    for i, f in enumerate(findings):
        # who should BUILD it — reuse the one escalation policy, don't reinvent
        model = mc._DELEGATE.get(f["complexity"], "opus")
        if dry_run:
            env = _canned_plan(f, refuse=(i == refuse_idx))
        else:
            env = mc.complete(
                "Finding:\n" + json.dumps(f, ensure_ascii=False),
                system=MANAGER_SYSTEM, model=mc.MODEL_IDS["opus"], effort="high",
                schema=MANAGER_SCHEMA)
        if env["stopped_reason"]:
            # THE CRITICAL PATH: refusal/error is data, not a crash. Log, mark
            # the finding blocked, keep sweeping the rest.
            log("REFUSAL on finding %s [%s] stopped_reason=%s (%s) — skipping "
                "plan, continuing loop" % (f["id"], f["system"],
                env["stopped_reason"], env["detail"]))
            f["status"] = "blocked"
            plans.append({"task_id": f["id"], "model": model,
                          "complexity": f["complexity"], "steps": [],
                          "stopped_reason": env["stopped_reason"]})
            continue
        try:
            body = json.loads(env["text"])
            steps = body.get("steps", [])
        except (ValueError, TypeError):
            log("finding %s: manager output unparseable — skipping" % f["id"])
            f["status"] = "blocked"
            continue
        f["status"] = "review"
        plans.append({"task_id": f["id"], "model": model,
                      "complexity": f["complexity"], "steps": steps,
                      "stopped_reason": None})
        log("PLAN for %s [%s] -> tagged %s (%s), %d steps"
            % (f["id"], f["system"], model.upper(), f["complexity"], len(steps)))
    return plans


def run(dry_run=False):
    tag = "DRY-RUN" if dry_run else "LIVE"
    log("daily review start (%s)" % tag)
    wire("review", "daily review start (%s)" % tag)

    findings = scan(dry_run)
    plans = escalate(findings, dry_run)

    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    name = "%s%s.json" % (stamp, "-dryrun" if dry_run else "")
    out = os.path.join(OUTDIR, name)
    report = {"date": stamp, "dry_run": dry_run,
              "generated": datetime.datetime.now().isoformat(timespec="seconds"),
              "findings": findings, "plans": plans}
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    built = sum(1 for p in plans if p["steps"])
    blocked = sum(1 for p in plans if p["stopped_reason"])
    log("wrote %s — %d findings, %d plans built, %d blocked (survived)"
        % (out, len(findings), built, blocked))
    wire("review", "daily review done: %d findings, %d plans, %d blocked"
         % (len(findings), built, blocked))
    return report


if __name__ == "__main__":
    run(dry_run="--dry-run" in sys.argv[1:])
