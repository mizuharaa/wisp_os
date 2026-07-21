#!/usr/bin/env python3
"""The CEO pipeline — how a prompt becomes a staffed mission.

  1. refine   Haiku sharpens the operator's raw prompt (keywords, missing
              specifics) — cheap, fast, before anything expensive runs.
  2. recall   Hermes is queried with the refined goal; prior solutions are
              folded into the CEO's brief so nothing gets re-solved.
  3. plan     The CEO (Opus, structured output) decomposes the goal into a
              minimum roster of roles, choosing each role's model and effort:
              Opus for hard implementation, Fable only for frontier-complex
              reasoning, Sonnet for light/logistics, Haiku for mechanical.
  4. execute  Roles run as headless Claude or Codex workers in dependency order.
              Per-role status (pending/working/blocked/review/done/failed,
              refined by retrying/repairing/waiting_permission/exhausted/
              stopped/skipped) is persisted after every transition — the
              dashboard polls it.
  5. learn    On completion the outcome is written to Hermes, which mirrors
              a card into the Obsidian vault. The flywheel closes itself.

State: one JSON per run in state/ceo/<cid>.json.  Wire: mirror.py events.
Stdlib only, raw urllib against the Anthropic API (repo rule: no deps).
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import chat    # API key resolution
import delivery  # guarded review/test/commit/push lane
import pulse   # account routing for workers
import runtime as agent_runtime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
MEMORY_DIR = os.path.join(ROOT, "memory")
if MEMORY_DIR not in sys.path:
    sys.path.insert(0, MEMORY_DIR)
import recall_engine  # structured Hermes retrieval + bounded proof receipts
from hermes.hermes import note_memory  # structured retention policy outcome

CDIR = os.path.join(ROOT, "state", "ceo")
ADIR = os.path.join(CDIR, "archive")   # finished runs moved here, kept not deleted
APPROVALS = os.path.join(ROOT, "state", "approvals.json")
MIRROR = os.path.join(ROOT, ".claude", "hooks", "mirror.py")
HERMES = os.path.join(ROOT, "hermes", "hermes.py")
IS_WIN = sys.platform == "win32"
WORKER_TIMEOUT = 1800

# A role that burns its whole turn budget was CUT OFF mid-task — it is not done.
# It used to be recorded as a silent success on half-finished work (the "it died
# out of nowhere and gave no reason" bug: a self-revamp needs far more than 40
# turns). Now we continue the SAME claude session, which keeps its full context,
# so it picks up where it stopped instead of re-doing the work. Only after this
# many auto-continues do we park the role as "exhausted" WITH a stated reason.
# ponytail: fixed budget; make it per-role if a mission ever needs a deeper one.
MAX_CONTINUES = 3
MAX_PLANNER_ATTEMPTS = 2
MAX_TRANSIENT_RETRIES = 2
MAX_RECOVERY_CYCLES = 2
MAX_PROVIDER_FALLBACKS = 1
MAX_VERIFY_REDOS = 1
RETRY_BACKOFF_BASE = 0.5

CONTINUE_PROMPT = """You ran out of your turn budget mid-task — this is a \
continuation of that same session, not a new one.

Continue exactly where you left off. Do NOT start over and do NOT redo work you \
already finished. Re-read anything you need, pick up the next unfinished step, \
and carry the task to completion. Finish by reporting what you did."""

# Successful missions move to history when their worker thread finishes.  This
# age window is retained for legacy success records that predate that behavior.
# Recoverable/failed records stay active until the operator resolves or archives
# them, because hiding unfinished work would recreate the silent-death problem.
AUTO_ARCHIVE_DAYS = 7
TERMINAL = ("done", "failed", "rejected", "exhausted", "stopped", "error", "stalled")
SUCCESSFUL = frozenset(("done", "completed", "success", "succeeded", "skipped"))
HISTORY_LIMIT = 50
HISTORY_MAX = 100

REFINER = "claude-haiku-4-5"   # prompt smith: cheap, fast
PLANNER = "claude-opus-4-8"    # the CEO itself: judgment is the product
ROLE_MODELS = ("haiku", "sonnet", "opus", "fable")
CLAUDE_WORKER_MODELS = frozenset(ROLE_MODELS)
CODEX_FALLBACK_MODEL = "gpt-5.6-sol"
CODEX_WORKER_MODELS = frozenset((CODEX_FALLBACK_MODEL,))

LIVE = {}  # cid -> {"thread","proc","stop","gate":{role_id:(action,feedback)}}
DELIVERY_BUSY = set()  # cids with one synchronous delivery transition in flight
# _save participates in cancellation ordering.  An RLock lets callers that are
# already inspecting LIVE persist state without deadlocking themselves.
LOCK = threading.RLock()

# a worker that dies on a usage/session/rate limit (not a real task failure) —
# used to flag the role so the UI can say "hit a limit, Continue when ready"
LIMIT_RE = agent_runtime.LIMIT_RE

ROUTES = ("answer", "solo", "delegate")

REFINE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},    # the improved, specific prompt
        "keywords": {"type": "string"},  # search keywords for brain recall
        "route": {"type": "string", "enum": list(ROUTES)},
    },
    "required": ["prompt", "keywords", "route"],
    "additionalProperties": False,
}

REFINE_SYSTEM = """You triage raw operator prompts for an agentic OS. Rewrite \
the prompt to be specific and unambiguous: expand vague verbs, name concrete \
deliverables, keep every constraint the operator stated, add nothing they \
didn't ask for. Also produce a short keyword string for searching a knowledge \
base of previously solved problems.

Then pick the cheapest route that can ACTUALLY do the job:
- "answer": pure knowledge/explanation, answerable in one reply from what you \
already know. NO tools, NO files, NO network, NO commands. e.g. 'what does X \
mean', 'explain this concept', 'give me advice'.
- "solo": real work, but one focused task a SINGLE agent can do end-to-end \
with tools — reading/editing files, running commands, fetching from an API or \
GitHub, a scoped fix, a lookup that needs the repo or network. Most work \
belongs here.
- "delegate": genuinely big — several independent workstreams, or a build that \
needs planning plus separate review/verification. Only when one agent working \
sequentially would be clearly worse.

CRITICAL: if doing it requires touching files, running anything, or reaching \
the network, it is NEVER "answer" — it is at least "solo". When unsure between \
solo and delegate, pick "solo"."""

# a chat-only answer uses the cheap assistant, not a Claude Code session. map
# the dropdown's model override to a real id; None lets chat pick Haiku/Sonnet.
DIRECT_MODELS = {"haiku": "claude-haiku-4-5", "sonnet": "claude-sonnet-5",
                 "opus": "claude-opus-4-8", "fable": "claude-fable-5"}

# solo = one Claude Code session, full tools, in this repo. It must DO the work,
# not describe it, and must not delegate (that's the whole point of the mode).
SOLO_BRIEF = """Do this task yourself, now, in this repo.

RULES:
- You have full tools. Actually DO the work — read/edit files, run commands,
  call APIs, fetch what you need. Never just describe what you would do, and
  never emit a command for someone else to run.
- Do NOT delegate. Do NOT spawn subagents or use the Agent/Task tool. You are
  a single agent working alone — that is deliberate.
- Stay scoped to what was asked; don't refactor or build extras around it.
- Finish by reporting concretely what you did and what changed (files, output,
  results). If you couldn't do something, say so plainly.
- Only if you hit something genuinely non-obvious, record it:
  python hermes/hermes.py note "<problem>" "<solution>" --tags solo

TASK:
"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},      # 2-4 word mission name
        "summary": {"type": "string"},   # one-line what/why
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},       # short slug e.g. "eng"
                    "title": {"type": "string"},    # e.g. "Backend engineer"
                    "mission": {"type": "string"},  # self-contained brief
                    "model": {"type": "string", "enum": list(ROLE_MODELS)},
                    "turns": {"type": "integer"},   # effort budget 5-80
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "review": {"type": "boolean"},  # park for operator review
                },
                "required": ["id", "title", "mission", "model", "turns",
                             "depends_on", "review"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "summary", "roles"],
    "additionalProperties": False,
}

PLAN_SYSTEM = """You are the CEO of a personal agentic OS. You never do the \
work yourself — you decompose the mission into the MINIMUM roster of roles \
(1-6) and delegate. Each role becomes a headless Claude Code session in this \
repo (full tool access, the .claude/agents roster, skills, the Hermes brain).

Per role you control model and effort deliberately:
- model: "opus" for most hard implementation/reasoning work (the default for \
anything substantial), "fable" ONLY for really, really complex frontier \
thinking, "sonnet" for light or logistics tasks (docs, checks, summaries), \
"haiku" for purely mechanical steps.
- turns: 5-80. Small for lookups, large for builds.
- depends_on: ids of roles whose output this role needs (keep the graph flat \
when possible — independent roles run without waiting).
- review: true ONLY when accepting the completed output would authorize an \
irreversible or outward action (destructive deletion, deploy/publish/send, \
spending, credential/access changes, or a soul write). Ordinary local edits, \
tests, analysis, design judgment, and previewable work are NOT gated. A gated \
role may produce a local report, but dependents wait for operator approval.

Each mission must be a self-contained brief: goal, concrete steps, \
constraints, and a CHECKABLE definition of done.

The brain holds SIGNAL, not a log of every run. Only tell a role to record a \
Hermes note when the work is genuinely worth remembering: a hard concept, a \
rare edge case, something token-expensive, or a point that was confusing to \
get right. For mechanical or obvious steps, do NOT add a note. When one is \
warranted, end that role's brief with: 'If you hit something non-obvious, \
record it: python hermes/hermes.py note "<problem>" "<solution>" --tags mission'.

If prior solutions are provided under "Brain recall", fold them into the \
relevant briefs so workers reuse instead of re-solving."""

REPLAN_SYSTEM = PLAN_SYSTEM + """

MID-MISSION REPLAN: some roles already ran; a ledger of their outcomes \
follows the mission. Plan ONLY the remaining work as 1-3 roles. Never repeat \
work a 'done' role finished. Read the failure evidence and choose a DIFFERENT \
approach — re-issuing a failed brief unchanged is forbidden. If a failure \
looks like a missing external prerequisite, produce one small role that \
verifies and reports it rather than a big build."""

VERIFY_MODEL = "claude-haiku-4-5"   # tier-1: the check must stay near-free
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "revise"]},
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}
VERIFY_SYSTEM = """You are the completion verifier for an agentic OS. A role \
just reported finishing its mission. Judge ONLY from the report whether the \
mission was actually completed. You have no tools.

"accept": the report gives concrete evidence the work happened — files \
changed, commands run, measurable results, or a direct answer to what the \
mission asked for.
"revise": the report shows the work was NOT completed — it describes plans or \
intentions instead of actions taken, skips an explicit deliverable the mission \
names, reports an unresolved error as if it were success, or answers a \
different task than the one assigned.

Bias toward accept: a wrong revise wastes an entire worker session. Never \
revise for style, verbosity, or polish the mission did not ask for. feedback: \
for revise, one short paragraph with the exact concrete instruction for what \
remains; empty for accept."""


def _verify_role(role):
    """Closed-loop completion check: does the report prove the mission happened?

    Advisory and best-effort — an unreachable verifier never blocks completion.
    ponytail: judges the report only (no tools), like the orchestrator critic;
    a repo-inspecting verifier is the upgrade path if false accepts show up."""
    if os.environ.get("RUNE_DISABLE_VERIFIER") == "1":
        return "accept", ""
    user = ("MISSION:\n%s\n\nROLE REPORT:\n%s" %
            (str(role.get("mission") or "")[:6000],
             str(role.get("result") or "")[:5000]))
    v = _api(VERIFY_MODEL, VERIFY_SYSTEM, user, VERIFY_SCHEMA,
             max_tokens=600, timeout=60)
    if (not isinstance(v, dict) or v.get("error") or
            v.get("verdict") not in ("accept", "revise")):
        return "accept", ""
    return v["verdict"], str(v.get("feedback") or "")[:1000]


def emit(cid, detail, event="ceo"):
    subprocess.run([sys.executable, MIRROR, "--session", cid, "--event", event,
                    "--detail", detail[:200]], capture_output=True)


# thresholds for "worth a brain note" — tuned so cheap mechanical successes are
# skipped and hard/rare/expensive/failed work is kept. Bump these if the brain
# still fills with noise; lower them if genuine learnings get dropped.
WORTH_COST = 0.15    # dollars: token-heavy runs
WORTH_TURNS = 40     # a single role that ran deep


def _remembering_reasons(o):
    """Return stable policy codes explaining why a result may be reusable."""
    roles = o.get("roles") or []
    reasons = []
    if any(agent_runtime.compact_recovery_evidence(r, learnable_only=True)
           for r in roles):
        reasons.append("verified_nonobvious_recovery")
    if o.get("status") in ("failed", "error"):
        reasons.append("failed_mission_evidence")
    if (o.get("cost") or 0) >= WORTH_COST:
        reasons.append("token_expensive")
    if len([r for r in roles if r.get("status") != "skipped"]) >= 2:
        reasons.append("coordinated_roles")
    if any((r.get("model") in ("opus", "fable") or
            ((r.get("provider_fallback")
              if isinstance(r.get("provider_fallback"), dict) else {})
             .get("from_model") in ("opus", "fable"))) for r in roles):
        reasons.append("hard_reasoning_model")
    if any((r.get("turns") or 0) >= WORTH_TURNS for r in roles):
        reasons.append("deep_role_budget")
    return reasons


def _worth_remembering(o):
    """The brain records reusable signal, not every cheap mechanical run."""
    return bool(_remembering_reasons(o))


def _learning_receipt(result, policy_reasons):
    """Bound Hermes' write decision for mission/UI state without stored text."""
    result = result if isinstance(result, dict) else {}
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    duplicate = (result.get("duplicate")
                 if isinstance(result.get("duplicate"), dict) else {})
    signals = quality.get("signals") if isinstance(quality.get("signals"), list) else []
    return {
        "version": 1,
        "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "attempted": True,
        "outcome": str(result.get("outcome") or "error")[:40],
        "reason_code": str(result.get("reason_code") or "memory-write-error")[:80],
        "memory_id": str(result.get("id") or "")[:40],
        "quality_score": round(float(quality.get("score") or 0), 4),
        "quality_signals": [str(item.get("code") or "")[:60]
                            for item in signals[:8] if isinstance(item, dict)],
        "duplicate_id": str(duplicate.get("id") or "")[:40],
        "duplicate_similarity": round(float(duplicate.get("similarity") or 0), 4),
        "archived_during_compaction": max(
            0, int(result.get("archived_during_compaction") or 0)),
        "policy_reasons": list(policy_reasons)[:8],
    }


def _api(model, system, user, schema, max_tokens=4000, timeout=120):
    """One structured-output Messages call. Returns parsed dict or {"error"}."""
    return chat.structured(model, system, user, schema,
                           max_tokens=max_tokens, timeout=timeout)


_RECALL_LOCAL = threading.local()


def _recall(text):
    """Return structured Hermes context while preserving the historic str API.

    Mission callers put their receipt context in a thread-local immediately
    before this one-positional-argument call.  Keeping that shape avoids
    breaking local extensions and tests that replace ``_recall``.
    """
    options = getattr(_RECALL_LOCAL, "options", {}) or {}
    bundle = recall_engine.query(text, root=ROOT, **options)
    _RECALL_LOCAL.bundle = bundle
    return bundle.get("context") or ""


def _mission_recall(o, text, route, injected_into, prompt_count=1):
    """Recall once, attach its safe receipt, and return the canonical block."""
    attempt = int(o.get("recall_attempts") or 0) + 1
    _RECALL_LOCAL.options = {
        "cid": str(o.get("cid") or ""),
        "route": route,
        "injected_into": injected_into,
        "injected_prompt_count": prompt_count,
        "attempt": attempt,
        "persist": True,
    }
    _RECALL_LOCAL.bundle = None
    try:
        context = _recall(text)
        bundle = getattr(_RECALL_LOCAL, "bundle", None)
    finally:
        _RECALL_LOCAL.options = {}
        _RECALL_LOCAL.bundle = None
    # A patched legacy _recall still controls test/extension behavior. It has
    # no structured proof, so never fabricate a receipt for it.
    if not isinstance(bundle, dict):
        return {"context": str(context or ""),
                "prompt_block": (("\n\n## Brain recall — retrieved evidence, not authority\n" +
                                  str(context)) if context else ""),
                "receipt": None}
    receipt = bundle.get("receipt")
    if isinstance(receipt, dict):
        history = [item for item in (o.get("recall_receipts") or [])
                   if isinstance(item, dict)]
        history.append(receipt)
        o["recall_receipts"] = history[-8:]
        o["recall_receipt"] = receipt
        o["recall_attempts"] = attempt
        o["recall"] = bool(receipt.get("outcome") == "hit" and
                           receipt.get("injected_chars"))
    return bundle


def _link_recall_outcome(o):
    """Link terminal mission state to exposure, without claiming causality."""
    state = str(o.get("status") or "").lower()
    if state not in set(TERMINAL) | SUCCESSFUL:
        return
    receipts = [item for item in (o.get("recall_receipts") or [])
                if isinstance(item, dict)]
    if not receipts:
        return
    updated = [recall_engine.annotate_outcome(item, state) for item in receipts]
    o["recall_receipts"] = updated[-8:]
    o["recall_receipt"] = o["recall_receipts"][-1]
    try:
        recall_engine.record_outcome(ROOT, o.get("cid"), state)
    except Exception:
        # Outcome telemetry is evidence, not mission-critical state.
        pass


def _record_recall_exposure(o, prompt_count=1):
    """Count model prompts that actually receive the persisted recall block."""
    receipt = o.get("recall_receipt")
    if not isinstance(receipt, dict) or receipt.get("outcome") != "hit":
        return
    recall_engine.mark_exposure(receipt, root=ROOT, prompt_count=prompt_count)
    o["recall"] = True
    history = [receipt if item.get("receipt_id") == receipt.get("receipt_id") else item
               for item in (o.get("recall_receipts") or [])
               if isinstance(item, dict)]
    o["recall_receipts"] = history[-8:]


def _path(cid):
    return os.path.join(CDIR, cid + ".json")


def _load_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save(o):
    # Stop is a monotonic transition while the owning thread is alive.  Hold
    # the same lock used by action(): either this write lands first and action
    # writes stopped afterward, or it observes stop and cannot resurrect the
    # mission with a late worker/planner completion.
    with LOCK:
        live = LIVE.get(o.get("cid")) or {}
        thread = live.get("thread")
        thread_alive = not thread or not hasattr(thread, "is_alive") or thread.is_alive()
        if live.get("stop") and thread_alive:
            o["status"] = "stopped"
            o["detail"] = "stopped by operator"
            o["next_action"] = "Resume to continue unfinished roles, or archive this mission."
            for role in o.get("roles") or []:
                if role.get("status") in ("working", "retrying", "repairing"):
                    role["status"] = "stopped"
                    role["detail"] = "stopped by operator"
                    role["next_action"] = "Resume this role when ready."
        now = datetime.datetime.now().isoformat(timespec="seconds")
        # Completion time is an immutable lifecycle boundary.  `updated` can
        # still change for metadata and must not be used to date a finished run.
        if str(o.get("status") or "").lower() in SUCCESSFUL:
            o.setdefault("finished_at", now)
        o["updated"] = now
        tmp = _path(o["cid"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(o, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _path(o["cid"]))


def _stopped(cid):
    with LOCK:
        return bool(LIVE.get(cid, {}).get("stop"))


def _drop_live(cid):
    """Forget a planner thread that ended before handing off to _run."""
    with LOCK:
        LIVE.pop(cid, None)


def _stop_state(o, detail="stopped by operator"):
    """Persist cancellation immediately; a killed worker must never look failed."""
    o["status"] = "stopped"
    o["detail"] = detail
    o["next_action"] = "Resume to continue unfinished roles, or archive this mission."
    for role in o.get("roles") or []:
        if role.get("status") in ("working", "retrying", "repairing"):
            role["status"] = "stopped"
            role["detail"] = detail
            role["next_action"] = "Resume this role when ready."
    _save(o)


def _permission_request(detail, role_id=""):
    """Build the bounded public state used by persisted permission controls."""
    request = agent_runtime.permission_request(detail)
    request.update(
        request_id="pr_" + uuid.uuid4().hex,
        role_id=str(role_id or ""),
        status="pending",
        requested_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    return request


_LEGACY_PERMISSION_PLACEHOLDERS = frozenset((
    "operator permission or credentials are required",
    "operator permission or credentials are required.",
    "operator permission is required",
    "operator action required",
    "permission required",
    "approval needed",
    "blocked",
))


def _legacy_permission_request(o, role=None):
    """Deterministically normalize a pre-request-id permission boundary."""
    planner_request = not isinstance(role, dict)
    role = role if isinstance(role, dict) else {}
    planning = o.get("planning_history") or []
    planning_detail = (planning[-1].get("detail") if planning and
                       isinstance(planning[-1], dict) else "")
    evidence = (role.get("last_failure") or role.get("result") or
                role.get("detail") or planning_detail or o.get("detail") or "")
    evidence = str(evidence or "").strip()
    useful = bool(evidence and evidence.lower() not in _LEGACY_PERMISSION_PLACEHOLDERS)
    if useful:
        request = agent_runtime.permission_request(evidence)
    else:
        request = {
            "kind": "unknown",
            "scope": "unresolved-prerequisite",
            "summary": ("Legacy permission wait has no recoverable evidence. "
                        "Retry after fixing the prerequisite, or Deny and skip."),
            "can_authorize": False,
        }
    if planner_request:
        request.update(
            kind="planner",
            scope="planner-api",
            can_authorize=False,
        )
    requested_at = str(o.get("updated") or o.get("started") or "legacy")
    rid = str(role.get("id") or "")
    material = "\x1f".join((str(o.get("cid") or ""), rid, evidence, requested_at))
    request.update(
        request_id="legacy_" + hashlib.sha256(
            material.encode("utf-8", errors="replace")).hexdigest()[:32],
        role_id=rid,
        status="pending",
        requested_at=requested_at,
    )
    return request


def _normalize_persisted_permission_request(o, role, request):
    """Reconcile old nonce-bearing request metadata with server-held evidence."""
    role_record = role if isinstance(role, dict) else {}
    request = dict(request) if isinstance(request, dict) else {}
    if not request.get("request_id"):
        return _legacy_permission_request(o, role if isinstance(role, dict) else None)
    planning = o.get("planning_history") or []
    planning_detail = (planning[-1].get("detail") if planning and
                       isinstance(planning[-1], dict) else "")
    evidence = (role_record.get("last_failure") or role_record.get("result") or
                request.get("summary") or role_record.get("detail") or
                planning_detail or o.get("detail") or "")
    parsed = agent_runtime.permission_request(evidence) if evidence else {}
    if parsed.get("kind") == "credential":
        request.update(kind="credential", scope="external-prerequisite",
                       summary=parsed.get("summary") or request.get("summary") or "",
                       can_authorize=False)
    elif parsed.get("kind") == "guard":
        was_restrictive = request.get("can_authorize") is False
        request.update(kind="guard", scope=parsed.get("scope") or "",
                       summary=parsed.get("summary") or request.get("summary") or "",
                       can_authorize=(bool(parsed.get("can_authorize")) and
                                      not was_restrictive))
    if not isinstance(role, dict):
        request.update(kind="planner", scope="planner-api", can_authorize=False)
    return request


def _set_permission_wait(o, detail, role=None, next_action=None):
    """Persist the exact permission evidence instead of a generic dead end."""
    rid = str((role or {}).get("id") or "")
    request = _permission_request(detail, rid)
    if role is None:
        # The staffing planner is an API request, not a headless tool worker.
        # There is no narrowly scoped skip/yolo override to grant here.
        request.update(kind="planner", scope="planner-api", can_authorize=False)
    message = ("Fix the external prerequisite, then Retry, or Deny this request."
               if not request.get("can_authorize") else
               "Allow this scoped request, Retry after fixing it, or Deny and skip.")
    next_action = next_action or message
    if role is not None:
        role["status"] = "waiting_permission"
        role["detail"] = request["summary"] or "operator permission is required"
        role["next_action"] = next_action
        role["permission_request"] = request
    o["status"] = "waiting_permission"
    o["detail"] = ((rid + ": ") if rid else "") + (
        request["summary"] or "operator permission is required")
    o["detail"] = o["detail"][:500]
    o["next_action"] = next_action
    o["permission_request"] = dict(request)
    return request


def _wait_retry(cid, retry_number):
    return agent_runtime.wait_backoff(
        lambda: _stopped(cid), retry_number, base=RETRY_BACKOFF_BASE)


def _record_attempt(role, w, classification, secs, kind="worker"):
    """Keep bounded recovery evidence instead of overwriting the last failure."""
    attempts = role.setdefault("attempts", [])
    attempts.append({
        "attempt": role.get("attempt", len(attempts) + 1),
        "kind": kind,
        "status": ("done" if classification == "success" else
                   "gated" if classification == "permission" else "failed"),
        "classification": classification,
        "provider": str(w.get("provider") or role.get("provider") or ""),
        "model": str(role.get("model") or ""),
        "detail": agent_runtime.safe_excerpt(w.get("result"), 500),
        "session": w.get("session_id") or "",
        "secs": round(secs),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    del attempts[:-12]


def _codex_fallback_status(now=None):
    """Return whether the local Codex CLI is a usable failover target.

    This is a local readiness check only: the executable must be on PATH, an
    authenticated Codex account must be visible, and the most recent known
    capacity window must not still be exhausted.  A cold pulse snapshot is
    filled from Codex's local auth/session files; no network probe is made.
    """
    if not shutil.which("codex"):
        return False, "Codex CLI is not installed or is not on PATH."
    try:
        status = (pulse.get().get("codex") or {})
    except Exception:
        status = {}
    # A newly logged-in/running CLI can be newer than the 45-second dashboard
    # pulse. Refresh a missing/error snapshot directly from the same local
    # Codex files so failover does not reject a connection that is already live.
    if not status or status.get("error"):
        try:
            status = pulse._codex(pulse._cfg())  # local auth/session files only
        except Exception:
            status = {"error": "local Codex account state is unreadable"}
    if not isinstance(status, dict):
        return False, "Codex account status is unavailable."
    if status.get("error"):
        return False, "Codex is not connected; run `codex login` first."
    current = time.time() if now is None else float(now)
    for pct_key, reset_key, label in (
            ("pct", "reset_at", "5-hour"),
            ("pct7d", "reset_at7d", "weekly")):
        try:
            exhausted = float(status.get(pct_key)) >= 100
        except (TypeError, ValueError):
            exhausted = False
        if not exhausted:
            continue
        try:
            reset_at = float(status.get(reset_key) or 0)
        except (TypeError, ValueError):
            reset_at = 0
        if not reset_at or reset_at > current:
            return False, "Codex's %s capacity window is exhausted." % label
    return True, "Codex CLI is connected and has available capacity."


def _switch_role_to_codex(role, failure):
    """Persist a single, auditable Claude-to-Codex provider switch in-place."""
    previous_provider = _provider_for_model(role.get("model"))
    if previous_provider != "claude":
        raise ValueError("only Claude roles can fail over to Codex")
    prior = role.get("provider_fallback")
    used = int(prior.get("count") or 0) if isinstance(prior, dict) else 0
    if used >= MAX_PROVIDER_FALLBACKS:
        raise ValueError("provider fallback budget exhausted")
    fallback = {
        "count": used + 1,
        "label": "Claude → Codex",
        "from_provider": "claude",
        "from_model": str(role.get("model") or ""),
        "from_session": str(role.get("session") or ""),
        "to_provider": "codex",
        "to_model": CODEX_FALLBACK_MODEL,
        "reason": agent_runtime.safe_excerpt(failure, 300),
        "status": "running",
        "summary": "Claude capacity limit; Codex is continuing this role.",
        "switched_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    role["provider_fallback"] = fallback
    role["provider"] = "codex"
    role["model"] = CODEX_FALLBACK_MODEL
    # Claude and Codex session identifiers are provider-specific. Retaining the
    # old id here could make a later Resume target the wrong CLI conversation.
    role["session"] = ""
    role.pop("continue_from", None)
    role["continues"] = 0
    return fallback


def _age_days(o):
    ts = o.get("finished_at") or o.get("updated") or o.get("started") or ""
    try:
        return (datetime.datetime.now()
                - datetime.datetime.fromisoformat(ts)).total_seconds() / 86400
    except ValueError:
        return 0.0


def _archive_file(cid):
    """Move a run out of the active dir into state/ceo/archive/ (kept, not deleted).
    list_all only scans the top level, so archived runs drop out of the UI."""
    src = _path(cid)
    dst = os.path.join(ADIR, cid + ".json")
    if not os.path.exists(src):
        # Archive is an idempotent lifecycle transition.  A double click or a
        # retry after a lost HTTP response must not turn success into an error.
        return os.path.isfile(dst)
    os.makedirs(ADIR, exist_ok=True)
    os.replace(src, dst)
    return True


def archive(cid):
    """Manual archive (the mission card's Archive button). Refuses a live run.

    A successful mission is already auto-archived into ADIR the instant it
    finishes (see run()'s completion path), so for anything already there
    this was previously a no-op that left the card sitting in Completed &
    delivery forever -- the button appeared broken. Mark the record
    `dismissed` so list_history() stops showing it. find_source_run()
    deliberately ignores this flag: a dismissed card must never let the
    same briefing task silently relaunch as a duplicate."""
    with LOCK:
        st = LIVE.get(cid)
        if st and st["thread"].is_alive():
            return "can't archive a running mission — stop it first"
    path = _record_path(cid)
    if path:
        try:
            record = _load_json(path)
        except (OSError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict) and not record.get("dismissed"):
            record["dismissed"] = True
            _save_record(record, path)
    return None if _archive_file(cid) else "no such mission"


def list_all():
    out = []
    if os.path.isdir(CDIR):
        for fn in os.listdir(CDIR):
            if not fn.endswith(".json"):
                continue
            try:
                o = _load_json(os.path.join(CDIR, fn))
            except (OSError, json.JSONDecodeError):
                continue
            with LOCK:
                o["live"] = o["cid"] in LIVE and LIVE[o["cid"]]["thread"].is_alive()
            if o.get("status") in ("running", "review", "planning") and not o["live"]:
                # the run's thread is gone but the file says running: the server
                # process died under it (window closed, restart, or Rune rewrote
                # its own backend). Say so — a stalled run used to give no reason.
                o["status"] = "stalled"
                o["detail"] = ("the Rune server stopped while this was running (app "
                               "window closed, restart, or Rune rewrote its own "
                               "backend) — Continue picks it up where it left off")
            # auto-archive: finished, not live, and past the window -> move it out
            # so the active list stays short. Runs the sweep for free on the poll.
            if (not o["live"] and str(o.get("status") or "").lower() in SUCCESSFUL
                    and _age_days(o) >= AUTO_ARCHIVE_DAYS):
                _archive_file(o["cid"])
                continue
            out.append(o)
    out.sort(key=lambda o: o.get("started", ""), reverse=True)
    return out


def list_history(limit=HISTORY_LIMIT):
    """Return bounded, newest-first mission history without deleting evidence.

    Archived records are the durable source.  Recent successful top-level
    records are included too so the API cannot briefly lose a completion
    between its final save and the worker thread's archive cleanup.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = HISTORY_LIMIT
    limit = max(0, min(limit, HISTORY_MAX))
    if not limit:
        return []

    by_cid = {}
    locations = ((CDIR, False), (ADIR, True))
    for directory, archived in locations:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                run = _load_json(os.path.join(directory, name))
            except (OSError, json.JSONDecodeError):
                continue
            status = str(run.get("status") or "").lower()
            if not archived and status not in SUCCESSFUL:
                continue
            if archived and run.get("dismissed"):
                continue
            cid = str(run.get("cid") or name[:-5])
            live = False
            if not archived:
                with LOCK:
                    state = LIVE.get(cid) or {}
                    thread = state.get("thread")
                    live = bool(thread and thread.is_alive())
            item = dict(run, cid=cid, archived=archived, live=live)
            # Prefer the archived copy if a stale duplicate exists in both
            # locations; it is the authoritative lifecycle destination.
            if cid not in by_cid or archived:
                by_cid[cid] = item

    out = list(by_cid.values())
    out.sort(key=lambda run: str(run.get("finished_at") or run.get("updated") or
                                 run.get("started") or ""), reverse=True)
    return out[:limit]


EFFORT_TURNS = {"quick": 15, "standard": 40, "deep": 80}


# Not every prompt needs a Haiku rewrite. A long, detailed prompt is already
# specific — re-writing it burns a call and can dilute the operator's wording.
# Only short/terse prompts get refined; the rest go to the CEO verbatim.
REFINE_MAX_CHARS = 180
# anything that has to touch files / run something / hit the network is real
# work — it needs a Claude Code session with tools, never a chat-only answer.
WORK_RE = re.compile(
    r"\b(fix|build|creat|add|implement|refactor|writ|run|test|deploy|audit|"
    r"migrat|updat|remov|delet|revamp|orchestrat|automat|debug|investigat|"
    r"scan|generat|set ?up|install|configur|clean|optimi[sz]|rename|wire|"
    r"fetch|pull|clone|push|commit|search|find|check|review|analy[sz]|"
    r"refresh|sync|verify|prioriti[sz]|plan)\w*\b", re.I)
ASK_RE = re.compile(
    r"^\s*(what|who|whom|whose|when|where|why|how|which|is|are|was|were|does|do|did|"
    r"can|could|should|would|explain|describe|summar|define|tell me)\b", re.I)


def _classify(text):
    """Free, local triage — no API call. Returns (needs_refine, route_guess).

    needs_refine: only short/terse prompts benefit from the Haiku rewrite.
    route_guess: a pure question with no work verbs can be answered from
    knowledge; anything else is real work, so default to a solo Claude Code
    run (tools, one agent) rather than the full CEO roster. Used as-is when we
    skip the refiner; otherwise the model's judgment wins."""
    long_prompt = len(text) >= REFINE_MAX_CHARS
    work = bool(WORK_RE.search(text))
    ask = bool(ASK_RE.match(text))
    if ask and not work and not long_prompt:
        return True, "answer"
    return (not long_prompt), "solo"


def plan_and_start(text, opts=None, source=None, workdir=None,
                   safe_permissions=False):
    """Intake. Returns fast — the CEO's planning call (which can take minutes)
    runs on a thread, so the HTTP request never hangs (a long synchronous plan
    was what produced "Failed to fetch" in the browser).

    opts (from the Run-it dropdown):
      mode    "auto" (route per prompt) | answer | solo | delegate
      refine  "auto" (only short/vague prompts) | off (never rewrite my prompt)
      model   "auto" (CEO decides) | haiku|sonnet|opus|fable (force all roles)
      effort  "auto" | quick|standard|deep (force every role's turn budget)
      account "auto" (least used) | an account display-name
      gate    True -> operator reviews every role before it runs

    Three real routes:
      answer   chat-only reply (no tools) — pure knowledge questions
      solo     ONE Claude Code session with full tools, no CEO, no subagents
      delegate the CEO staffs a roster of roles
    Returns (result, None) or (None, error); result["kind"] is "answer" or
    "mission" (solo and delegate both produce a mission card)."""
    opts = opts if isinstance(opts, dict) else {}
    mode = opts.get("mode") if opts.get("mode") in ("auto",) + ROUTES else "auto"
    refine_off = str(opts.get("refine") or "auto") == "off"
    force_model = opts.get("model") if opts.get("model") in ROLE_MODELS else None
    force_turns = EFFORT_TURNS.get(opts.get("effort"))
    text = (text or "").strip()
    if not text:
        return None, "empty goal"
    run_dir = os.path.normpath(os.path.realpath(workdir or ROOT))
    if not os.path.isdir(run_dir):
        return None, "working directory is unavailable"
    needs_refine, route = _classify(text)
    refined, keywords = text, text
    # 1. Haiku prompt smith — ONLY for short/vague prompts, and only when its
    # routing judgment can still matter (never when the operator forced a mode
    # AND turned refinement off, or in answer mode where there's nothing to plan).
    if needs_refine and not refine_off and mode != "answer":
        r = _api(REFINER, REFINE_SYSTEM, text, REFINE_SCHEMA, max_tokens=1500, timeout=45)
        if not r.get("error"):
            refined = r.get("prompt") or text
            keywords = r.get("keywords") or text
            if r.get("route") in ROUTES:
                route = r["route"]        # the model's judgment beats the heuristic
    if mode != "auto":
        route = mode                       # an explicit choice always wins
    # 2. answer: chat only, no tools, no agents. Cheap questions.
    if route == "answer":
        answer_cid = "answer-" + uuid.uuid4().hex[:12]
        ans = chat.ask(
            refined, model=DIRECT_MODELS.get(force_model),
            recall_context={"cid": answer_cid, "route": "answer"})
        if ans.get("error"):
            return None, ans["error"]
        os.makedirs(CDIR, exist_ok=True)
        _save({"cid": answer_cid, "name": text[:40], "goal": text[:1000],
               "route": "answer", "roles": [], "status": "done", "cost": 0,
               "reply": ans.get("reply") or "(no reply)",
               "started": datetime.datetime.now().isoformat(timespec="seconds")})
        return {"kind": "answer", "cid": answer_cid,
                "reply": ans.get("reply") or "(no reply)",
                "model": ans.get("model"), "goal": text[:1000],
                "recall_receipt": ans.get("recall_receipt")}, None
    os.makedirs(CDIR, exist_ok=True)
    cid = uuid.uuid4().hex[:8]
    briefing_source = (isinstance(source, dict) and
                       source.get("kind") == "daily_briefing")
    prompt_limit = 12000 if briefing_source else 4000
    goal_limit = prompt_limit if briefing_source else 1000
    o = {"cid": cid, "name": text[:40], "summary": "", "goal": text[:goal_limit],
         "refined": refined[:prompt_limit], "keywords": keywords[:300], "recall": False,
         "roles": [], "route": route,
         "source": dict(source) if isinstance(source, dict) else None,
         "workdir": run_dir, "safe_permissions": bool(safe_permissions),
         "permission_mode": "safe" if safe_permissions else "skip",
         "opts": {"model": force_model, "turns": force_turns,
                   "gate": bool(opts.get("gate"))},
         "account_pref": str(opts.get("account") or "auto"),
         "status": "running", "cost": 0, "auto_recover": True,
         "planning_attempt": 0, "planning_history": [], "next_action": "",
         "started": datetime.datetime.now().isoformat(timespec="seconds")}
    o["git_baseline"] = delivery.capture_git_baseline(run_dir)
    # 3. solo: run the prompt in ONE Claude Code session with full tools — no
    # planning call, no roster, no subagents. This is "just run it" mode.
    if route == "solo":
        recall_bundle = _mission_recall(
            o, keywords, "solo", "solo_worker", prompt_count=0)
        recall = recall_bundle.get("context") or ""
        brief = SOLO_BRIEF + refined + (recall_bundle.get("prompt_block") or "")
        o.update(name=text[:40], summary="Single Claude Code session — no delegation.",
                 recall=bool(recall),
                 roles=[{"id": "solo", "title": "Direct run (no delegation)",
                         "mission": brief, "model": force_model or "sonnet",
                         "provider": "claude",
                         "brain_preinjected": bool(recall_bundle.get("prompt_block")),
                         "turns": force_turns or 40, "depends_on": [],
                         "review": bool(opts.get("gate")), "status": "pending",
                         "result": "", "secs": 0, "cost": 0}])
        _save(o)
        t = threading.Thread(target=_run, args=(cid,), daemon=True)
        with LOCK:
            LIVE[cid] = {"thread": t, "proc": None, "stop": False, "gate": {}}
        t.start()
        emit(cid, "solo run (%s, %dt, no subagents): %s"
             % (o["roles"][0]["model"], o["roles"][0]["turns"], text[:100]))
        return dict(o, kind="mission"), None
    # 4. delegate: create the card NOW (status "planning") and hand the slow
    # recall + CEO plan to a thread, so the HTTP request never hangs.
    o["status"] = "planning"
    _save(o)
    t = threading.Thread(target=_plan_then_run, args=(cid,), daemon=True)
    with LOCK:
        LIVE[cid] = {"thread": t, "proc": None, "stop": False, "gate": {}}
    t.start()
    emit(cid, "CEO planning: " + text[:120])
    return dict(o, kind="mission"), None


def _source_identity(source, exact_snapshot=True):
    """Comparable identity for an authoritative persisted-plan launch."""
    source = source if isinstance(source, dict) else {}
    keys = ["kind", "source_date", "batch_id", "priority_id"]
    if exact_snapshot:
        keys.append("snapshot")
    return tuple(str(source.get(key) or "") for key in keys)


def _source_run_active(run):
    """Whether starting another copy would overlap unresolved execution."""
    cid = str(run.get("cid") or "")
    with LOCK:
        live = LIVE.get(cid)
        if live and live.get("thread") and live["thread"].is_alive():
            return True
    return str(run.get("status") or "").lower() in (
        "review", "waiting_permission", "waiting-permission")


def find_source_run(source, exact_snapshot=True, active_only=False):
    """Find the newest persisted run for a briefing card or exact snapshot."""
    wanted = _source_identity(source, exact_snapshot=exact_snapshot)
    if not wanted[0] or not os.path.isdir(CDIR):
        return None
    matches = []
    # Active lookup is deliberately top-level/LIVE only.  A normal exact-source
    # lookup additionally sees archived successes so clearing the UI cannot make
    # a completed briefing card launch again without explicit rerun=True.
    locations = [(CDIR, False)]
    if exact_snapshot and not active_only:
        locations.append((ADIR, True))
    for directory, archived in locations:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            try:
                run = _load_json(os.path.join(directory, name))
            except (OSError, json.JSONDecodeError):
                continue
            if archived and str(run.get("status") or "").lower() not in SUCCESSFUL:
                continue
            if (_source_identity(run.get("source"), exact_snapshot=exact_snapshot) == wanted
                    and (not active_only or _source_run_active(run))):
                run["archived"] = archived
                matches.append(run)
    if not matches:
        return None
    matches.sort(key=lambda run: str(run.get("updated") or run.get("started") or ""),
                 reverse=True)
    run = matches[0]
    with LOCK:
        live = LIVE.get(str(run.get("cid") or ""))
        run["live"] = bool(not run.get("archived") and live and live.get("thread")
                           and live["thread"].is_alive())
    return run


def _start_direct_briefing(text, source, workdir, roles):
    """Persist and start an explicit provider-aware briefing mission."""
    if not isinstance(roles, list) or not 2 <= len(roles) <= 4:
        return None, "direct briefing execution requires 2-4 validated roles"
    prepared = []
    recall_terms = []
    seen = set()
    previous = ""
    for index, item in enumerate(roles, 1):
        if not isinstance(item, dict):
            return None, "direct briefing role %d is invalid" % index
        role_id = str(item.get("id") or "").strip()
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", role_id) or
                role_id in seen):
            return None, "direct briefing role %d has an invalid id" % index
        try:
            provider = _provider_for_model(item.get("model"))
        except ValueError as exc:
            return None, str(exc)
        if str(item.get("provider") or "") != provider:
            return None, "direct briefing role %d has corrupt provider metadata" % index
        title = str(item.get("title") or "").strip()[:80]
        assignment = str(item.get("mission") or "").strip()[:2000]
        deliverable = str(item.get("deliverable") or "").strip()[:1000]
        if not title or not assignment or not deliverable:
            return None, "direct briefing role %d is incomplete" % index
        try:
            turns = int(item.get("turns"))
        except (TypeError, ValueError):
            return None, "direct briefing role %d has an invalid effort budget" % index
        if not 1 <= turns <= 100:
            return None, "direct briefing role %d has an invalid effort budget" % index
        mission = (text + "\n\n## YOUR DIRECT SAVED ASSIGNMENT\nRole: " + title +
                   "\nMission: " + assignment + "\nDeliverable: " + deliverable +
                   "\nExecute this assignment with the saved provider/model. Do not "
                   "restaff it onto another provider. Inspect existing work from earlier "
                   "roles before making changes.")
        prepared.append({
            "id": role_id, "title": title, "mission": mission[:14000],
            "deliverable": deliverable, "model": str(item.get("model")),
            "provider": provider, "effort": str(item.get("effort") or ""),
            "turns": turns, "depends_on": [previous] if previous else [],
            "review": False, "status": "pending", "result": "", "secs": 0,
            "cost": 0,
        })
        recall_terms.extend((title, assignment, deliverable))
        seen.add(role_id)
        previous = role_id
    os.makedirs(CDIR, exist_ok=True)
    cid = uuid.uuid4().hex[:8]
    o = {
        "cid": cid, "name": (prepared[0]["title"] or text[:40])[:40],
        "summary": "Saved briefing cards running directly through their selected providers.",
        "goal": text[:12000], "refined": text[:12000], "keywords": "",
        "recall": False, "roles": prepared, "route": "direct",
        "source": dict(source), "workdir": workdir,
        "safe_permissions": False, "permission_mode": "skip",
        "providers": sorted({role["provider"] for role in prepared}),
        "opts": {"model": None, "turns": None, "gate": False},
        "account_pref": "auto", "status": "running", "cost": 0,
        "auto_recover": True, "planning_attempt": 0, "planning_history": [],
        "next_action": "Executing the first saved provider role.",
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    o["git_baseline"] = delivery.capture_git_baseline(workdir)
    recall_query = " ".join(recall_terms)[:300]
    o["keywords"] = recall_query
    recall_bundle = _mission_recall(
        o, recall_query, "direct", "direct_workers", prompt_count=0)
    recall_block = recall_bundle.get("prompt_block") or ""
    if recall_block:
        for role in prepared:
            keep = max(0, 14000 - len(recall_block))
            role["mission"] = role["mission"][:keep] + recall_block
            role["brain_preinjected"] = True
    _save(o)
    thread = threading.Thread(target=_run, args=(cid,), daemon=True)
    with LOCK:
        LIVE[cid] = {"thread": thread, "proc": None, "stop": False, "gate": {}}
    thread.start()
    emit(cid, "direct briefing (%s): %s" %
         ("+".join(o["providers"]), text[:100]))
    return dict(o, kind="mission"), None


def start_briefing_mission(text, source, workdir, rerun=False,
                           permission_mode="safe", roles=None):
    """Idempotently launch one server-resolved Daily Briefing priority.

    The lock spans lookup and mission persistence.  ``plan_and_start`` is forced
    down its no-refiner delegated route, so it performs no blocking model call
    before saving the source-tagged run.  A second HTTP thread therefore sees
    and reuses the same cid instead of launching duplicate work.
    """
    if permission_mode not in ("safe", "skip"):
        return None, "permission_mode must be safe or skip"
    if not isinstance(source, dict) or source.get("kind") != "daily_briefing":
        return None, "invalid briefing source"
    if any(not str(source.get(key) or "") for key in
           ("batch_id", "priority_id", "snapshot")):
        return None, "incomplete briefing source"
    run_dir = os.path.normpath(os.path.realpath(workdir or ""))
    if not os.path.isdir(run_dir):
        return None, "briefing repository is unavailable"
    with LOCK:
        # Never overlap two runs for the same logical card, even if its agent
        # dropdowns changed and therefore produced a new snapshot hash.
        active = find_source_run(source, exact_snapshot=False, active_only=True)
        if active is not None:
            return dict(active, kind="mission", reused=True), None
        existing = find_source_run(source)
        if existing is not None and not rerun:
            return dict(existing, kind="mission", reused=True), None
        if permission_mode == "skip":
            out, err = _start_direct_briefing(text, source, run_dir, roles)
            if err:
                return None, err
            return dict(out, reused=False), None
        out, err = plan_and_start(
            text,
            {"mode": "delegate", "refine": "off", "model": "auto",
             "effort": "auto", "account": "auto", "gate": False},
            source=source, workdir=run_dir, safe_permissions=True)
        if err:
            return None, err
        return dict(out, reused=False), None


def _plan_then_run(cid):
    """Thread: brain recall -> CEO staffs the roles -> run them. Slow work that
    used to block the HTTP request now lives here. Planning is retryable and a
    Stop received during the API call/backoff wins before any roles can start."""
    try:
        o = _load_json(_path(cid))
    except (OSError, json.JSONDecodeError):
        _drop_live(cid)
        return
    ov = o.get("opts") or {}
    force_model, force_turns, gate_all = ov.get("model"), ov.get("turns"), ov.get("gate")
    try:
        if _stopped(cid):
            _stop_state(o)
            _drop_live(cid)
            return
        recall_bundle = _mission_recall(
            o, o.get("keywords") or o["goal"], "delegate", "planner",
            prompt_count=0)
        recall = recall_bundle.get("context") or ""
        recall_block = recall_bundle.get("prompt_block") or ""
        # _api bounds user content at 12k. Reserve the recall block explicitly
        # so a long Daily Briefing cannot silently truncate the evidence.
        prefix = "MISSION:\n"
        mission_limit = max(0, 12000 - len(prefix) - len(recall_block))
        brief = prefix + o["refined"][:mission_limit] + recall_block
        # Persist the proof before the first planner request. If Rune stops in
        # the model call, the retrieval attempt and exposure remain visible.
        _save(o)
        p, roles = {}, []
        final_classification = "task"
        for attempt in range(1, MAX_PLANNER_ATTEMPTS + 1):
            if _stopped(cid):
                _stop_state(o)
                _drop_live(cid)
                return
            o["status"] = "planning"
            o["planning_attempt"] = attempt
            o["next_action"] = "The CEO is preparing a bounded staffing plan."
            _save(o)
            if _stopped(cid):
                _stop_state(o)
                _drop_live(cid)
                return
            _record_recall_exposure(o, 1)
            _save(o)
            p = _api(PLANNER, PLAN_SYSTEM, brief, PLAN_SCHEMA,
                     max_tokens=8000, timeout=180)
            if _stopped(cid):
                _stop_state(o)
                _drop_live(cid)
                return
            if not isinstance(p, dict):
                p = {"error": "malformed planner response"}
            raw_roles = None if p.get("error") else p.get("roles")
            malformed_roles = (raw_roles is not None and
                               (not isinstance(raw_roles, list) or
                                any(not isinstance(role, dict) for role in raw_roles)))
            roles = (raw_roles if isinstance(raw_roles, list) and
                     not malformed_roles else []) or []
            error = ("" if roles else p.get("error") or
                     ("CEO returned malformed roles" if malformed_roles else
                      "CEO returned no roles"))
            classification = ("success" if roles else
                              agent_runtime.classify_failure(error, True))
            final_classification = classification
            o.setdefault("planning_history", []).append({
                "attempt": attempt,
                "status": "done" if roles else "failed",
                "classification": classification,
                "detail": agent_runtime.safe_excerpt(error, 300),
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            })
            del o["planning_history"][:-8]
            if roles:
                break
            o["detail"] = agent_runtime.safe_excerpt(error, 300)
            if classification == "permission":
                _set_permission_wait(o, error)
                _save(o)
                emit(cid, "CEO planning paused for operator permission: " +
                     o["detail"][:100])
                _drop_live(cid)
                return
            # Capacity/transport failures are retryable.  An empty or malformed
            # structured plan is also safe to ask the planner for again; other
            # task/API errors stop immediately instead of retrying blindly.
            retryable_plan = (
                classification in ("transient", "transient_limit") or
                not p.get("error") or
                "malformed" in str(p.get("error") or "").lower())
            if retryable_plan and attempt < MAX_PLANNER_ATTEMPTS:
                o["next_action"] = "Planning failed transiently; retrying automatically."
                _save(o)
                emit(cid, "CEO planning attempt %d/%d failed — retrying: %s" %
                     (attempt, MAX_PLANNER_ATTEMPTS, o["detail"][:100]))
                if not _wait_retry(cid, attempt):
                    _stop_state(o)
                    _drop_live(cid)
                    return
            else:
                break
        if not roles:
            o["detail"] = agent_runtime.safe_excerpt(
                p.get("error") or "CEO returned no roles", 300)
            if final_classification == "permission":
                _set_permission_wait(o, p.get("error") or o["detail"])
            else:
                o["status"] = "error"
                o["next_action"] = "Retry planning; no repository role was started."
            if o["status"] == "error":
                _link_recall_outcome(o)
            _save(o)
            emit(cid, "CEO planning failed: " + o["detail"][:120])
            _drop_live(cid)
            return
        seen = set()
        for i, role in enumerate(roles[:6]):
            rid = re.sub(r"[^a-z0-9\-]", "", str(role.get("id") or "").lower()) or "r%d" % i
            while rid in seen:
                rid += "x"
            seen.add(rid)
            role["id"] = rid
            role["model"] = role.get("model") if role.get("model") in ROLE_MODELS else "opus"
            role["provider"] = "claude"
            try:
                role["turns"] = max(5, min(80, int(role.get("turns") or 30)))
            except (TypeError, ValueError):
                role["turns"] = 30
            # operator overrides from the Run-it dropdown beat the CEO's choices
            if force_model:
                role["model"] = force_model
            if force_turns:
                role["turns"] = force_turns
            if gate_all:
                role["review"] = True
            role["depends_on"] = [d for d in (role.get("depends_on") or [])
                                  if d in seen and d != rid]
            role.update(status="pending", result="", secs=0, cost=0)
        if _stopped(cid):
            _stop_state(o)
            _drop_live(cid)
            return
        o.update(name=(p.get("name") or o["goal"][:40])[:40],
                  summary=(p.get("summary") or "")[:300],
                  recall=bool(recall), roles=roles[:6], status="running",
                  detail="", next_action="Executing the first runnable role.")
        _save(o)
        if _stopped(cid):
            _stop_state(o)
            _drop_live(cid)
            return
        emit(cid, "CEO staffed '%s': %s" % (o["name"], ", ".join(
            "%s(%s/%dt)" % (r["id"], r["model"], r["turns"]) for r in o["roles"])))
    except Exception as e:
        if _stopped(cid):
            _stop_state(o)
            _drop_live(cid)
            return
        o["status"] = "error"
        o["detail"] = repr(e)[:300]
        _link_recall_outcome(o)
        _save(o)
        emit(cid, "CEO planning crashed: " + repr(e)[:120])
        _drop_live(cid)
        return
    _run(cid)   # same thread: staffing flows straight into execution


def _provider_for_model(model):
    """Return the allowlisted CLI provider for a persisted worker model."""
    model = str(model or "").strip().lower()
    if model in CLAUDE_WORKER_MODELS:
        return "claude"
    if model in CODEX_WORKER_MODELS:
        return "codex"
    raise ValueError("unsupported worker model: %s" % (model or "(empty)"))


def _permission_mode_for(mission, role=None):
    """Resolve the persisted operator policy for one worker, fail-closed.

    A one-role permission grant is narrower than changing the whole mission, so
    a valid role override wins.  New mission records persist ``permission_mode``;
    ``safe_permissions`` is retained only as a legacy-record fallback.  Unknown
    or missing legacy values default to safe rather than accidentally enabling a
    bypass after a corrupt/manual state edit.
    """
    for record in (role, mission):
        if not isinstance(record, dict):
            continue
        mode = str(record.get("permission_mode") or "").strip().lower()
        if record is role and mode == "skip":
            authorization = record.get("operator_authorization")
            if not isinstance(authorization, dict):
                continue
            if (authorization.get("status") != "active" or
                    authorization.get("kind") not in ("provider", "protected")):
                continue
            try:
                expires = datetime.datetime.fromisoformat(
                    str(authorization.get("expires_at") or ""))
            except ValueError:
                continue
            if expires <= datetime.datetime.now():
                continue
        if mode in ("safe", "skip"):
            return mode
    if isinstance(mission, dict) and mission.get("safe_permissions") is False:
        return "skip"
    return "safe"


def _consume_operator_authorization(mission, role):
    """Mark both runtime and audit copies when a scoped grant is used."""
    authorization = role.get("operator_authorization")
    if not isinstance(authorization, dict) or authorization.get("status") != "active":
        return
    consumed_at = datetime.datetime.now().isoformat(timespec="seconds")
    authorization["status"] = "consumed"
    authorization["consumed_at"] = consumed_at
    if (authorization.get("kind") in ("provider", "protected") and
            role.get("permission_mode") == "skip"):
        # This field is a one-invocation capability, not durable role policy.
        # Removing it also keeps the persisted/UI mode aligned with the next
        # backend decision; an explicit mission-level skip remains untouched.
        role.pop("permission_mode", None)
    authorization_id = authorization.get("authorization_id")
    request_id = authorization.get("request_id")
    for receipt in mission.get("permission_authorizations") or []:
        if not isinstance(receipt, dict):
            continue
        if ((authorization_id and receipt.get("authorization_id") == authorization_id) or
                (not authorization_id and request_id and
                 receipt.get("request_id") == request_id)):
            receipt["status"] = "consumed"
            receipt["consumed_at"] = consumed_at
            break


def _worker_argv(role, permission_mode="safe", workdir=ROOT, resume_sid="",
                 output_path=""):
    """Build an argv-only, provider-aware headless worker command.

    The model and provider are allowlisted and must agree.  Permission policy is
    a mission-level operator choice: an explicit skip mission keeps its bypass
    on original, resumed, and bounded-recovery workers.  Otherwise a recovery
    worker can hit the same permission denial it was created to repair and park
    the mission even though the operator selected skip.  Safe missions remain
    contained.  Prompt text is deliberately absent from argv and is supplied
    over stdin.
    """
    if permission_mode not in ("safe", "skip"):
        raise ValueError("permission_mode must be safe or skip")
    model = str(role.get("model") or "").strip().lower()
    provider = _provider_for_model(model)
    saved_provider = str(role.get("provider") or provider).strip().lower()
    if saved_provider != provider:
        raise ValueError("worker provider does not match its model")
    bypass = permission_mode == "skip"
    if provider == "claude":
        try:
            turns = max(1, min(100, int(role.get("turns") or 40)))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Claude worker turn budget") from exc
        argv = ["claude", "-p", "--output-format", "json",
                "--max-turns", str(turns), "--model", model]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        else:
            argv += ["--permission-mode", "auto"]
        if resume_sid:
            argv += ["--resume", str(resume_sid)]
        return argv
    if not output_path:
        raise ValueError("Codex workers require an output path")
    # Codex parses sandbox/approval policy at the `exec` level.  Keep every
    # execution option before the optional `resume` subcommand; placing
    # `--sandbox` after `resume` is rejected by current CLIs before any worker
    # can start.
    argv = ["codex", "exec", "--json", "-o", os.path.normpath(output_path),
            "-m", model]
    if bypass:
        argv.append("--yolo")
    else:
        # `exec` has no interactive approval channel. Keep recovery contained in
        # the workspace sandbox and make denials visible to the worker without
        # granting the native yolo/bypass policy.
        argv += ["--sandbox", "workspace-write", "-c", 'approval_policy="never"']
    if resume_sid:
        argv += ["resume", str(resume_sid), "-"]
    else:
        argv += ["-C", os.path.normpath(workdir), "-"]
    return argv


def _codex_result(stdout, stderr, output_path, returncode):
    """Normalize Codex JSONL/final-message output into the Claude worker shape."""
    events = []
    for line in str(stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    session_id = ""
    failure = ""
    for event in events:
        kind = str(event.get("type") or "")
        if kind == "thread.started":
            thread = event.get("thread") if isinstance(event.get("thread"), dict) else {}
            session_id = str(event.get("thread_id") or thread.get("id") or "")
        if kind in ("turn.failed", "error"):
            failure = str(event.get("message") or event.get("error") or failure)
    result = ""
    try:
        with open(output_path, encoding="utf-8") as handle:
            result = handle.read().strip()
    except OSError:
        pass
    if not result:
        for event in reversed(events):
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if (str(event.get("type") or "") == "item.completed" and
                    str(item.get("type") or "") == "agent_message"):
                result = str(item.get("text") or item.get("content") or "").strip()
                if result:
                    break
    is_error = bool(returncode) or bool(failure) or not bool(result)
    if not result and is_error:
        result = (failure or str(stderr or "") or str(stdout or "") or
                  "Codex worker returned no final message (exit %s)" % returncode).strip()[:6000]
    return {"is_error": is_error, "result": result, "session_id": session_id,
            "total_cost_usd": 0, "provider": "codex"}


def _worker(cid, role, context, cfg_dir, resume_sid="", workdir=ROOT,
            safe_permissions=False):
    """Run one Claude or Codex role headlessly with stop/resume tracking."""
    prompt = CONTINUE_PROMPT if resume_sid else role["mission"]
    if context and not resume_sid:
        prompt += "\n\n## Output from roles you depend on:\n" + context[:6000]
    authorization = role.get("operator_authorization")
    authorization_active = False
    if isinstance(authorization, dict) and authorization.get("status") == "active":
        try:
            authorization_active = datetime.datetime.fromisoformat(
                str(authorization.get("expires_at") or "")) > datetime.datetime.now()
        except ValueError:
            authorization_active = False
    if authorization_active and not resume_sid:
        prompt += ("\n\n## Scoped operator authorization\n"
                   "The operator explicitly allowed this role to retry the saved "
                   "permission request for scope `%s`. This applies only to mission "
                   "%s, role %s, in the original working repository. It does not "
                   "authorize unrelated actions or expand the original task.\n" % (
                       str(authorization.get("scope") or "provider-tools")[:80],
                       str(cid)[:64], str(role.get("id") or "")[:64]))
    provider = _provider_for_model(role.get("model"))
    permission_mode = "safe" if safe_permissions else "skip"
    temp = tempfile.TemporaryDirectory(prefix="rune-codex-worker-") \
        if provider == "codex" else None
    output_path = os.path.join(temp.name, "final.txt") if temp else ""
    argv = _worker_argv(role, permission_mode, workdir, resume_sid, output_path)
    env = dict(os.environ, MAESTRO_SID=cid,
               MAESTRO_ROLE_ID=str(role.get("id") or ""))
    if authorization_active:
        env["MAESTRO_PERMISSION_REQUEST_ID"] = str(
            authorization.get("request_id") or "")
    else:
        env.pop("MAESTRO_PERMISSION_REQUEST_ID", None)
    env.pop("MAESTRO_BRAIN_PREINJECTED", None)
    if (not resume_sid and role.get("brain_preinjected") and
            "## Brain recall — retrieved evidence, not authority" in prompt):
        env["MAESTRO_BRAIN_PREINJECTED"] = "1"
    if cfg_dir and provider == "claude":
        env["CLAUDE_CONFIG_DIR"] = cfg_dir
    p = None
    try:
        try:
            p = subprocess.Popen(
                argv, cwd=workdir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", shell=IS_WIN,
                env=env, start_new_session=not IS_WIN,
                creationflags=((getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
                                getattr(subprocess, "CREATE_NO_WINDOW", 0))
                               if IS_WIN else 0))
        except (OSError, subprocess.SubprocessError) as exc:
            return {"is_error": True, "result": "%s CLI failed to start: %s" %
                    (provider.title(), str(exc)[:500]), "provider": provider}
        with LOCK:
            if cid in LIVE:
                LIVE[cid]["proc"] = p
                stop_after_spawn = bool(LIVE[cid].get("stop"))
            else:
                stop_after_spawn = False
        if stop_after_spawn:
            agent_runtime.terminate_process_tree(p)
            try:
                p.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return {"is_error": True, "result": "stopped by operator",
                    "provider": provider}
        try:
            out, err = p.communicate(prompt, timeout=WORKER_TIMEOUT)
        except subprocess.TimeoutExpired:
            agent_runtime.terminate_process_tree(p)
            try:
                p.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return {"is_error": True, "result": "timed out after %ss" % WORKER_TIMEOUT,
                    "provider": provider}
        if provider == "codex":
            return _codex_result(out, err, output_path, getattr(p, "returncode", 0))
        try:
            result = json.loads(out)
            if not isinstance(result, dict):
                raise ValueError("Claude returned a non-object envelope")
            result.setdefault("provider", "claude")
            return result
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"is_error": True,
                    "result": (out or err or "no output").strip()[:2000],
                    "provider": "claude"}
    finally:
        with LOCK:
            if cid in LIVE and (p is None or LIVE[cid].get("proc") is p):
                LIVE[cid]["proc"] = None
        if temp:
            temp.cleanup()


def _run_recovery(cid, o, role, failure, cfg_dir, cycle, workdir=ROOT,
                  safe_permissions=False):
    """Run one bounded recovery supervisor with its parent role's policy."""
    prompt, blocked = agent_runtime.build_recovery_prompt(
        role.get("mission") or "", failure, cycle, MAX_RECOVERY_CYCLES)
    rec = {
        "cycle": cycle,
        "failure_class": agent_runtime.classify_failure(failure, True),
        "failure": agent_runtime.safe_excerpt(failure, 300),
        "status": "blocked" if blocked else "working",
        "detail": blocked or "Recovery supervisor is inspecting the local failure.",
        "verification": "not-run",
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    history = role.setdefault("recovery_history", [])
    history.append(rec)
    del history[:-6]
    if blocked:
        evidence = failure if agent_runtime.classify_failure(
            failure, True) == "permission" else blocked
        request = _set_permission_wait(o, evidence, role)
        # A protected recovery boundary is not necessarily a provider prompt.
        # Keep it authorizable only as a retry of this original role; it never
        # mints a wildcard guard token.
        if evidence == blocked and request.get("kind") == "provider":
            request.update(kind="protected", scope="original-role",
                           can_authorize=True)
            role["permission_request"] = request
            o["permission_request"] = dict(request)
        _save(o)
        emit(cid, "role %s recovery paused: %s" % (role["id"], blocked))
        return "blocked", ""

    role["status"] = "repairing"
    role["detail"] = "bounded recovery cycle %d/%d" % (cycle, MAX_RECOVERY_CYCLES)
    role["next_action"] = "Recovery supervisor will verify a minimal local fix."
    o["status"] = "running"
    o["next_action"] = role["next_action"]
    _save(o)
    if _stopped(cid):
        rec.update(status="stopped", detail="stopped by operator")
        _stop_state(o)
        return "stopped", ""
    emit(cid, "role %s recovery cycle %d/%d" %
         (role["id"], cycle, MAX_RECOVERY_CYCLES))
    fixer = {
        "id": role["id"] + "-recovery-%d" % cycle,
        "title": "Recovery supervisor / fixer",
        "mission": prompt,
        "model": "sonnet" if role.get("model") in ("haiku", "sonnet") else role.get("model", "sonnet"),
        "turns": max(10, min(30, int(role.get("turns") or 30) // 2)),
        "recovery": True,
    }
    t0 = time.time()
    w = _worker(cid, fixer, "", cfg_dir, workdir=workdir,
                safe_permissions=safe_permissions)
    rec["secs"] = round(time.time() - t0)
    rec["cost"] = round(w.get("total_cost_usd") or 0, 4)
    role["cost"] = round((role.get("cost") or 0) + rec["cost"], 4)
    report = str(w.get("result") or "")[:3000]
    classification = agent_runtime.classify_failure(
        report, bool(w.get("is_error")), w.get("subtype") or "")
    # A successful-looking fixer can still explicitly discover that a protected
    # operator decision is required. Treat that as a gate, never as success.
    rec["repair_class"] = classification
    rec["repair_summary"] = agent_runtime.safe_excerpt(report, 320)
    if _stopped(cid):
        rec.update(status="stopped", detail="stopped by operator")
        _stop_state(o)
        return "stopped", ""
    if classification == "permission":
        rec.update(status="blocked", detail="operator permission or credentials are required")
        _set_permission_wait(o, report, role)
        _save(o)
        return "blocked", ""
    if w.get("is_error"):
        rec.update(status="failed", detail=agent_runtime.safe_excerpt(report, 300))
        role["detail"] = "recovery cycle %d failed: %s" % (cycle, rec["detail"])
        role["next_action"] = ("Trying the final bounded recovery cycle." if
                               cycle < MAX_RECOVERY_CYCLES else
                               "Inspect the recovery evidence and retry manually.")
        _save(o)
        return "failed", report
    rec.update(status="repaired", detail="minimal local repair reported; original role will verify",
               verification="pending-original-rerun")
    role["detail"] = rec["detail"]
    role["next_action"] = "Re-running the original role to verify completion."
    _save(o)
    return "repaired", report


def _replan_if_stuck(cid, o):
    """One bounded mid-mission replan when a role fails terminally.

    The CEO reads the full role ledger and staffs a revised tail for the
    remaining work instead of firing tasks and forgetting them. Unfinished
    roles are superseded; done work is kept. Once per mission — a replan of a
    replan is the operator's call, not an automatic loop."""
    if os.environ.get("RUNE_DISABLE_REPLAN") == "1":
        return False
    if o.get("replans") or o.get("route") != "delegate" or _stopped(cid):
        return False
    if not any(r["status"] == "failed" for r in o.get("roles") or []):
        return False
    if any(r["status"] in ("waiting_permission", "review")
           for r in o.get("roles") or []):
        return False
    ledger = "\n".join(
        "- %s (%s): %s%s" % (
            r["id"], r.get("title") or "", r["status"],
            (" — " + agent_runtime.safe_excerpt(
                r.get("last_failure") or r.get("detail") or "", 200))
            if r["status"] not in ("done", "skipped") else "")
        for r in o["roles"])
    brief = ("MISSION:\n%s\n\nROLE LEDGER (everything that already happened):\n%s"
             % (str(o.get("refined") or o.get("goal") or "")[:8000], ledger))
    p = _api(PLANNER, REPLAN_SYSTEM, brief, PLAN_SCHEMA,
             max_tokens=8000, timeout=180)
    raw = p.get("roles") if isinstance(p, dict) and not p.get("error") else None
    if (not isinstance(raw, list) or not raw or
            any(not isinstance(item, dict) for item in raw) or _stopped(cid)):
        emit(cid, "mid-mission replan unavailable: " + agent_runtime.safe_excerpt(
            str((p if isinstance(p, dict) else {}).get("error") or "no roles"), 120))
        return False
    for r in o["roles"]:
        if r["status"] in ("failed", "blocked", "pending"):
            r["status"] = "skipped"
            r["detail"] = "superseded by the mid-mission replan"
            r["next_action"] = ""
    ov = o.get("opts") or {}
    seen = {r["id"] for r in o["roles"]}
    added = []
    for i, item in enumerate(raw[:3]):
        rid = "r2-" + (re.sub(r"[^a-z0-9\-]", "",
                              str(item.get("id") or "").lower()) or "r%d" % i)
        while rid in seen:
            rid += "x"
        seen.add(rid)
        item["id"] = rid
        item["model"] = item.get("model") if item.get("model") in ROLE_MODELS else "opus"
        item["provider"] = "claude"
        try:
            item["turns"] = max(5, min(80, int(item.get("turns") or 30)))
        except (TypeError, ValueError):
            item["turns"] = 30
        if ov.get("model"):
            item["model"] = ov["model"]
        if ov.get("turns"):
            item["turns"] = ov["turns"]
        if ov.get("gate"):
            item["review"] = True
        item["depends_on"] = [d for d in (item.get("depends_on") or [])
                              if d in seen and d != rid]
        item.update(status="pending", result="", secs=0, cost=0)
        added.append(item)
    o["roles"].extend(added)
    o["replans"] = 1
    o["next_action"] = "The CEO replanned the remaining work after a role failure."
    _save(o)
    emit(cid, "mid-mission replan staffed: " + ", ".join(
        "%s(%s/%dt)" % (r["id"], r["model"], r["turns"]) for r in added))
    return True


def _wait_gate(cid, o, role):
    """Park a review-gated role until the operator acts (approve/redo/skip)."""
    role["status"] = "review"
    o["status"] = "review"
    _save(o)
    emit(cid, "role %s awaiting review: %s" % (role["id"], role["title"]))
    while True:
        with LOCK:
            st = LIVE.get(cid) or {}
            if st.get("stop"):
                return ("stop", "")
            v = st.get("gate", {}).pop(role["id"], None)
        if v:
            return v
        time.sleep(0.5)


def _run(cid):
    o = _load_json(_path(cid))
    workdir = os.path.normpath(os.path.realpath(o.get("workdir") or ROOT))
    # Claude roles use the selected Claude account. Pure Codex missions do not
    # claim or expose an unrelated Claude account in Activity.
    has_claude = any(_provider_for_model(role.get("model")) == "claude"
                     for role in o.get("roles") or [])
    pref = o.get("account_pref") or "auto"
    acct = (pulse.least_used() if pref == "auto" else pref) if has_claude else ""
    cfg_dir = pulse.dir_for(acct) if acct else ""
    if acct:
        o["account"] = acct
        _save(o)
        emit(cid, "delegating to account: " + acct)
    roles = {r["id"]: r for r in o["roles"]}
    try:
        # dependency-ordered pass; repeats until no runnable role remains.
        # ponytail: sequential execution — parallel threads per role when a
        # real mission is actually bottlenecked by it.
        while True:
            if _stopped(cid):
                _stop_state(o)
                return
            # A permission boundary is an operator checkpoint for the mission,
            # not an invitation to keep spending on unrelated roles while its
            # decision buttons remain unavailable behind a live thread.
            if any(role.get("status") == "waiting_permission"
                   for role in o.get("roles") or []):
                break
            runnable = next(
                (r for r in o["roles"] if r["status"] == "pending"
                 and all(roles[d]["status"] in ("done", "skipped")
                         for d in r["depends_on"] if d in roles)), None)
            if not runnable:
                if _replan_if_stuck(cid, o):
                    roles = {r["id"]: r for r in o["roles"]}
                    continue
                # anything still pending has a failed/blocked dependency — name it,
                # so a blocked role never sits there without a reason
                for r in o["roles"]:
                    if r["status"] == "pending":
                        r["status"] = "blocked"
                        bad = [d for d in r["depends_on"] if d in roles
                               and roles[d]["status"] not in ("done", "skipped")]
                        r["detail"] = "blocked: %s didn't finish (%s)" % (
                            ", ".join(bad) or "a dependency",
                            ", ".join(roles[d]["status"] for d in bad) or "unfinished")
                break
            role = runnable
            # Resolve per role so an operator can grant one blocked role without
            # silently widening permission policy for the rest of the mission.
            safe_permissions = _permission_mode_for(o, role) == "safe"
            transient_retries = 0
            recovery_cycles = len(role.get("recovery_history") or [])
            recovery_context = ""
            while True:  # worker retry / recovery / human-redo loop
                if _stopped(cid):
                    _stop_state(o)
                    return
                role["status"] = "working"
                role["attempt"] = (role.get("attempt") or 0) + 1
                role["detail"] = "attempt %d is running" % role["attempt"]
                role["next_action"] = "Wait for the role report."
                if (isinstance(role.get("provider_fallback"), dict) and
                        _provider_for_model(role.get("model")) == "codex"):
                    role["provider_fallback"]["status"] = "running"
                    role["provider_fallback"]["summary"] = (
                        "Codex is continuing the unfinished role.")
                o["status"] = "running"
                o["next_action"] = "%s is working." % role["title"]
                _save(o)
                if _stopped(cid):
                    _stop_state(o)
                    return
                emit(cid, "role %s attempt %d working (%s, %dt): %s"
                     % (role["id"], role["attempt"], role["model"],
                        role["turns"], role["title"]))
                contexts = ["### %s (%s)\n%s" % (roles[d]["title"], d,
                                                  roles[d]["result"][:1500])
                            for d in role["depends_on"] if roles[d].get("result")]
                if recovery_context:
                    contexts.append("### Bounded recovery report\n" + recovery_context[:2000])
                ctx = "\n\n".join(contexts)
                t0 = time.time()
                # A Continue on an exhausted role resumes its prior provider session
                # (context intact) instead of re-running the mission from zero
                sid = role.pop("continue_from", "")
                if not sid and o.get("route") in ("solo", "direct"):
                    _record_recall_exposure(o, 1)
                    _save(o)
                w = _worker(cid, role, "" if sid else ctx, cfg_dir, resume_sid=sid,
                            workdir=workdir, safe_permissions=safe_permissions)
                _consume_operator_authorization(o, role)
                spend = w.get("total_cost_usd") or 0
                sid = w.get("session_id") or sid
                # ran out of turns => cut off mid-task, NOT finished. Continue the
                # same session until it lands or the continue budget runs out.
                cont = role.get("continues") or 0
                while (w.get("subtype") == "error_max_turns" and sid
                       and cont < MAX_CONTINUES):
                    with LOCK:
                        if LIVE.get(cid, {}).get("stop"):
                            break
                    cont += 1
                    role["continues"] = cont
                    role["session"] = sid
                    _save(o)
                    emit(cid, "role %s ran out of %d turns — auto-continuing the "
                         "same session (%d/%d)" % (role["id"], role["turns"],
                                                   cont, MAX_CONTINUES))
                    w = _worker(cid, role, "", cfg_dir, resume_sid=sid,
                                workdir=workdir, safe_permissions=safe_permissions)
                    spend += w.get("total_cost_usd") or 0
                    sid = w.get("session_id") or sid
                # A one-request provider grant covers the original invocation
                # and its same-session turn continuations only. Any separate
                # fallback, retry, or recovery worker re-resolves mission policy.
                safe_permissions = _permission_mode_for(o, role) == "safe"
                role["session"] = sid
                role["continues"] = cont
                elapsed = time.time() - t0
                role["secs"] = round((role.get("secs") or 0) + elapsed)
                role["cost"] = round((role.get("cost") or 0) + spend, 4)
                o["cost"] = round(sum(r.get("cost") or 0 for r in o["roles"]), 4)
                ran_out = w.get("subtype") == "error_max_turns"
                role["result"] = str(w.get("result") or "")[:6000] or (
                    "(no final message — used all %d turns)" % role["turns"])
                classification = agent_runtime.classify_failure(
                    role["result"], bool(w.get("is_error")), w.get("subtype") or "")
                _record_attempt(role, w, classification, elapsed)
                if _stopped(cid):
                    _stop_state(o)
                    return
                # A Claude capacity limit is an account/provider outage, not a
                # repository failure. If the local Codex CLI is connected and
                # has headroom, persist one provider switch before launching it
                # synchronously. The old Claude session is retained as evidence
                # but is never resumed through the incompatible Codex CLI.
                if (not ran_out and w.get("is_error") and
                        classification == "transient_limit" and
                        _provider_for_model(role.get("model")) == "claude"):
                    ready, readiness = _codex_fallback_status()
                    if ready:
                        claude_failure = role["result"]
                        fallback = _switch_role_to_codex(role, claude_failure)
                        role["attempt"] = (role.get("attempt") or 0) + 1
                        role["status"] = "retrying"
                        role["detail"] = (
                            "Claude capacity limit detected; continuing this role "
                            "once through Codex.")
                        role["next_action"] = "Codex is continuing the unfinished role."
                        o["providers"] = sorted({_provider_for_model(
                            item.get("model")) for item in o.get("roles") or []})
                        o["next_action"] = role["next_action"]
                        _save(o)
                        emit(cid, "role %s hit a Claude capacity limit — switching "
                             "to Codex (%s)" % (role["id"], CODEX_FALLBACK_MODEL))
                        if _stopped(cid):
                            _stop_state(o)
                            return
                        failover_context = "\n\n".join(filter(None, (
                            ctx,
                            "### Provider failover\nClaude stopped at a usage/capacity "
                            "limit. Continue the same assignment with Codex. Inspect "
                            "the current repository state first, keep any valid prior "
                            "work, and do not repeat completed changes.",
                        )))
                        fallback_started = time.time()
                        w = _worker(cid, role, failover_context, cfg_dir,
                                    workdir=workdir,
                                    safe_permissions=safe_permissions)
                        fallback_elapsed = time.time() - fallback_started
                        fallback_spend = w.get("total_cost_usd") or 0
                        role["secs"] = round((role.get("secs") or 0) +
                                             fallback_elapsed)
                        role["cost"] = round((role.get("cost") or 0) +
                                             fallback_spend, 4)
                        o["cost"] = round(sum(item.get("cost") or 0
                                              for item in o["roles"]), 4)
                        role["session"] = str(w.get("session_id") or "")
                        role["result"] = str(w.get("result") or "")[:6000] or (
                            "Codex fallback returned no final message")
                        classification = agent_runtime.classify_failure(
                            role["result"], bool(w.get("is_error")),
                            w.get("subtype") or "")
                        ran_out = w.get("subtype") == "error_max_turns"
                        _record_attempt(role, w, classification,
                                        fallback_elapsed, kind="provider_fallback")
                        fallback["status"] = (
                            "waiting_permission" if classification == "permission" else
                            "failed" if w.get("is_error") else "succeeded")
                        fallback["summary"] = (
                            "Codex fallback reached an operator permission boundary."
                            if classification == "permission" else
                            "Codex fallback attempt failed; bounded recovery remains visible."
                            if w.get("is_error") else
                            "Codex completed the role after Claude hit its capacity limit.")
                        fallback["finished_at"] = datetime.datetime.now().isoformat(
                            timespec="seconds")
                        fallback["result"] = agent_runtime.safe_excerpt(
                            role["result"], 300)
                        _save(o)
                        if _stopped(cid):
                            _stop_state(o)
                            return
                    else:
                        role["fallback_unavailable"] = {
                            "provider": "codex", "reason": readiness,
                            "checked_at": datetime.datetime.now().isoformat(
                                timespec="seconds"),
                        }
                if ran_out:
                    # STILL unfinished after the continue budget. Park it with a
                    # stated reason — never report half-done work as success.
                    role["status"] = "exhausted"
                    role["detail"] = (
                        "ran out of turns: %d turn budget × %d run(s). The task was "
                         "cut off, not finished. Continue resumes this same session."
                         % (role["turns"], cont + 1))
                    role["next_action"] = "Continue resumes this exact worker session."
                    _save(o)
                    emit(cid, "role %s EXHAUSTED: %s" % (role["id"], role["detail"]))
                    break
                if classification == "permission":
                    role["last_failure_class"] = "permission"
                    role["last_failure"] = agent_runtime.safe_excerpt(role["result"], 500)
                    _set_permission_wait(o, role["result"], role)
                    _save(o)
                    emit(cid, "role %s waiting for operator permission" % role["id"])
                    break
                if w.get("is_error"):
                    role["last_failure_class"] = classification
                    role["last_failure"] = agent_runtime.safe_excerpt(role["result"], 500)
                    role["limit"] = classification == "transient_limit"
                    if classification == "task" and role.get("recovery_history"):
                        latest = role["recovery_history"][-1]
                        if latest.get("verification") == "pending-original-rerun":
                            latest["verification"] = "failed-original-rerun"
                            latest["status"] = "verification-failed"
                    if classification in ("transient", "transient_limit") \
                            and transient_retries < MAX_TRANSIENT_RETRIES:
                        transient_retries += 1
                        role["status"] = "retrying"
                        role["detail"] = "%s failure; bounded retry %d/%d" % (
                            classification, transient_retries, MAX_TRANSIENT_RETRIES)
                        role["next_action"] = "Retrying automatically after a short backoff."
                        o["next_action"] = role["next_action"]
                        _save(o)
                        emit(cid, "role %s %s — retry %d/%d" %
                             (role["id"], classification, transient_retries,
                              MAX_TRANSIENT_RETRIES))
                        if not _wait_retry(cid, transient_retries):
                            _stop_state(o)
                            return
                        recovery_context = ""
                        continue
                    if classification == "task" and recovery_cycles < MAX_RECOVERY_CYCLES:
                        recovery_cycles += 1
                        state, report = _run_recovery(
                            cid, o, role, role["result"], cfg_dir, recovery_cycles,
                            workdir=workdir, safe_permissions=safe_permissions)
                        o["cost"] = round(sum(r.get("cost") or 0 for r in o["roles"]), 4)
                        if state == "stopped":
                            return
                        if state == "blocked":
                            break
                        recovery_context = report
                        # Whether the fixer landed or itself failed, the original
                        # role is the verifier. It inspects existing work and either
                        # completes or produces evidence for the next bounded cycle.
                        continue
                    role["status"] = "failed"
                    role["detail"] = (
                        "transient retry budget exhausted: " + role["last_failure"]
                        if classification in ("transient", "transient_limit") else
                        "recovery budget exhausted: " + role["last_failure"])
                    role["next_action"] = "Inspect attempt/recovery history, then Resume or archive."
                    if isinstance(role.get("provider_fallback"), dict):
                        role["provider_fallback"]["status"] = "failed"
                        role["provider_fallback"]["summary"] = (
                            "Codex could not complete the role; inspect the attempt history.")
                    _save(o)
                    emit(cid, "role %s %s: %s" % (role["id"],
                         "retry budget exhausted" if role["limit"] else "FAILED",
                         role["detail"][:120]))
                    break
                # Closed loop: a cheap verifier judges the report against the
                # mission before "done". One bounded redo — never an open loop.
                verdict, veto = _verify_role(role)
                role["verification"] = {
                    "verdict": verdict, "feedback": veto[:500],
                    "model": VERIFY_MODEL,
                    "at": datetime.datetime.now().isoformat(timespec="seconds")}
                if _stopped(cid):
                    _stop_state(o)
                    return
                if (verdict == "revise" and veto and
                        int(role.get("verifies") or 0) < MAX_VERIFY_REDOS):
                    role["verifies"] = int(role.get("verifies") or 0) + 1
                    role["status"] = "retrying"
                    role["detail"] = "verifier sent it back: " + veto[:160]
                    role["next_action"] = "Re-running the role to close the verifier's gap."
                    role["mission"] += (
                        "\n\nVERIFIER FEEDBACK (a completion check found this gap — "
                        "inspect the existing work first and finish only what is "
                        "missing, do not redo completed steps): " + veto)
                    o["next_action"] = role["next_action"]
                    _save(o)
                    emit(cid, "role %s verifier revise (%d/%d): %s" %
                         (role["id"], role["verifies"], MAX_VERIFY_REDOS, veto[:100]))
                    recovery_context = ""
                    continue
                # A successful original rerun is the verification step for the
                # latest recovery cycle. Keep only compact, secret-safe evidence.
                if role.get("recovery_history"):
                    latest = role["recovery_history"][-1]
                    if latest.get("verification") == "pending-original-rerun":
                        latest["verification"] = "passed-original-rerun"
                        latest["status"] = "verified"
                        # A brain note is a reusable recipe, not a process log.
                        # Require a successful fixer, actual original-role
                        # verification, and enough concrete evidence to be more
                        # useful than "fixed it" before marking it learnable.
                        repair = latest.get("repair_summary") or ""
                        latest["learnable"] = bool(
                            latest.get("repair_class") == "success" and
                            len(repair) >= 40 and len(repair.split()) >= 6)
                        role["recovery_summary"] = agent_runtime.compact_recovery_evidence(role)
                        emit(cid, "role %s recovery verified: %s" %
                             (role["id"], role["recovery_summary"][:120]))
                role.pop("limit", None)
                if isinstance(role.get("provider_fallback"), dict):
                    role["provider_fallback"]["status"] = "succeeded"
                    role["provider_fallback"]["summary"] = (
                        "Codex completed the role after Claude hit its capacity limit.")
                role["detail"] = ("" if verdict == "accept" else
                                  "completed with a verifier reservation: " + veto[:160])
                role["next_action"] = ("Awaiting operator approval." if role.get("review")
                                       else "Role complete.")
                if not role.get("review"):
                    role["status"] = "done"
                    _save(o)
                    emit(cid, "role %s done ($%s, %ss)" % (role["id"], role["cost"], role["secs"]))
                    break
                verdict, feedback = _wait_gate(cid, o, role)
                if verdict == "stop":
                    _stop_state(o)
                    return
                if verdict == "approve":
                    role["status"] = "done"
                    _save(o)
                    emit(cid, "role %s approved by operator" % role["id"])
                    break
                if verdict == "skip":
                    role["status"] = "skipped"
                    _save(o)
                    break
                # redo: feedback becomes an addendum to the mission
                role["mission"] += "\n\nOPERATOR FEEDBACK (address this): " + (feedback or "revise")
                transient_retries = 0
                recovery_context = ""
                emit(cid, "role %s redo: %s" % (role["id"], (feedback or "")[:100]))
        # a mission is only "done" when every role actually finished. Exhausted
        # roles are unfinished work, not success — they get their own status so
        # the card says why and offers Continue instead of claiming completion.
        waiting = [r for r in o["roles"] if r["status"] == "waiting_permission"]
        stuck = [r for r in o["roles"] if r["status"] in
                 ("failed", "blocked", "exhausted", "waiting_permission")]
        if not stuck:
            o["status"] = "done"
            o["detail"] = ""
            o["next_action"] = "Mission complete; archive when no longer needed."
        elif waiting:
            o["status"] = "waiting_permission"
            o["detail"] = "; ".join("%s: %s" %
                                     (r["id"], r.get("detail") or "operator action required")
                                     for r in waiting)[:300]
            request = o.get("permission_request") or {}
            o["next_action"] = (
                "Fix the external prerequisite, then Retry, or Deny this request."
                if not request.get("can_authorize", False) else
                "Allow this scoped request, Retry after fixing it, or Deny and skip.")
        elif all(r["status"] == "exhausted" for r in stuck):
            o["status"] = "exhausted"
            o["detail"] = ("out of turns before finishing — Continue picks each role "
                            "up in its own session, right where it stopped")
            o["next_action"] = "Continue resumes exhausted sessions without starting over."
        else:
            o["status"] = "failed"
            o["detail"] = "; ".join("%s: %s" % (r["id"], r.get("detail") or r["status"])
                                     for r in stuck)[:300]
            o["next_action"] = "Inspect attempt/recovery evidence, then Resume or archive."
        if o["status"] == "waiting_permission":
            # Settle the owner thread immediately so nonce-bound permission
            # controls become actionable. A wait is not a learning outcome;
            # the resumed terminal mission will produce at most one receipt.
            _save(o)
            emit(cid, "mission waiting for operator permission: " +
                 o.get("detail", "")[:160])
            return
        _link_recall_outcome(o)
        if str(o.get("status") or "").lower() in SUCCESSFUL:
            # Delivery is attached before the final save/archive so completed
            # work leaves the active queue with a durable review/test/ship lane.
            try:
                delivery.initialize_completed_delivery(o)
            except Exception as delivery_error:
                # Shipping metadata is useful, but it is never allowed to turn
                # successfully completed agent work into a failed mission.
                o["delivery"] = {
                    "version": 1, "available": False, "status": "unavailable",
                    "reason": "Delivery setup failed; the completed work is preserved.",
                    "created_at": datetime.datetime.now().astimezone().isoformat(
                        timespec="seconds"),
                }
                emit(cid, "delivery setup unavailable (mission unaffected): " +
                     type(delivery_error).__name__)
        _save(o)
        emit(cid, "mission %s: %s ($%s)%s" % (o["status"], o["name"], o["cost"],
                                              " — " + o["detail"] if o.get("detail") else ""))
        # 5. learn — but only when it's worth remembering (signal, not a log).
        # Trivial cheap successes are skipped so the brain stays high-signal.
        policy_reasons = _remembering_reasons(o)
        if policy_reasons:
            outcome = "; ".join("%s=%s" % (r["id"], r["status"]) for r in o["roles"])
            recovery_parts = [
                agent_runtime.compact_recovery_evidence(r, learnable_only=True)
                for r in o["roles"] if r.get("recovery_history")]
            recovery = "; ".join(part for part in recovery_parts if part)
            had_recovery = any(r.get("recovery_history") for r in o["roles"])
            last = next((r["result"] for r in reversed(o["roles"]) if r.get("result")), "")
            evidence = (recovery or
                        ("bounded recovery ended without a verified reusable recipe"
                         if had_recovery else agent_runtime.safe_excerpt(last, 320)))
            try:  # learning is best-effort and can never turn a done run into error
                note_result = note_memory(
                    agent_runtime.safe_excerpt(o["goal"], 180),
                    ("%s. %s" % (outcome, evidence))[:500],
                    "mission,ceo,recovery" if recovery else "mission,ceo",
                    "ceo:" + cid)
                o["learning_receipt"] = _learning_receipt(
                    note_result, policy_reasons)
                emit(cid, "brain learning %s (%s): %s" % (
                    o["learning_receipt"]["outcome"],
                    ",".join(policy_reasons)[:100],
                    o["learning_receipt"]["reason_code"]))
            except Exception as learn_error:
                o["learning_receipt"] = {
                    "version": 1,
                    "ts": datetime.datetime.now().astimezone().isoformat(
                        timespec="seconds"),
                    "attempted": True, "outcome": "error",
                    "reason_code": "memory-write-error",
                    "error_type": type(learn_error).__name__[:60],
                    "policy_reasons": policy_reasons[:8],
                }
                emit(cid, "brain note failed (mission unaffected): " +
                     type(learn_error).__name__)
        else:
            o["learning_receipt"] = {
                "version": 1,
                "ts": datetime.datetime.now().astimezone().isoformat(
                    timespec="seconds"),
                "attempted": False, "outcome": "skipped",
                "reason_code": "routine-low-reuse-signal",
                "policy_reasons": [],
            }
            emit(cid, "skipped brain note (routine run — brain holds signal, not logs)")
        try:
            _save(o)
        except OSError as receipt_error:
            emit(cid, "learning receipt persistence failed (mission unaffected): " +
                 type(receipt_error).__name__)
    except Exception as e:  # never leave a run stuck at "running"
        if _stopped(cid):
            _stop_state(o)
        else:
            o["status"] = "error"
            o["detail"] = repr(e)[:300]
            _link_recall_outcome(o)
            _save(o)
            emit(cid, "CEO run crashed: " + repr(e)[:120])
    finally:
        completed = str(o.get("status") or "").lower() in SUCCESSFUL
        with LOCK:
            LIVE.pop(cid, None)
            if completed:
                try:
                    _archive_file(cid)
                except OSError as archive_error:
                    # The final active JSON remains intact if the move fails;
                    # archival housekeeping can never turn success into error.
                    emit(cid, "history archive failed (mission preserved): " +
                         type(archive_error).__name__)


def _resume_review_then_run(cid, role_id):
    """Re-create an in-memory review wait after a server restart, without
    re-running the already completed gated role."""
    o = _load_json(_path(cid))
    role = next((r for r in o.get("roles") or [] if r.get("id") == role_id), None)
    if not role:
        return
    verdict, feedback = _wait_gate(cid, o, role)
    if verdict == "stop":
        _stop_state(o)
        with LOCK:
            LIVE.pop(cid, None)
        return
    if verdict == "approve":
        role["status"] = "done"
    elif verdict == "skip":
        role["status"] = "skipped"
    else:
        role["mission"] += "\n\nOPERATOR FEEDBACK (address this): " + (feedback or "revise")
        role["status"] = "pending"
    _save(o)
    _run(cid)


def resume(cid, automatic=False):
    """Pick a stopped / failed / stalled mission back up. Every non-terminal role
    (failed, blocked, working, review) resets to pending and re-runs; done and
    skipped work is kept, so a mission that got 3/6 through a session limit
    continues from role 4 instead of restarting. Only when not already live."""
    path = _path(cid)
    if not os.path.exists(path):
        return "no such mission"
    with LOCK:
        st = LIVE.get(cid)
        if st and st["thread"].is_alive():
            return "already running"
    try:
        o = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return "mission file unreadable"
    # Planning failures have no roles. The old resume path iterated an empty
    # list and claimed there was nothing to resume; safely re-plan instead.
    if not o.get("roles") and o.get("route") == "delegate":
        o["status"] = "planning"
        o["detail"] = ""
        o["next_action"] = "Retrying the bounded CEO staffing plan."
        o["resumes"] = o.get("resumes", 0) + 1
        _save(o)
        t = threading.Thread(target=_plan_then_run, args=(cid,), daemon=True)
        with LOCK:
            LIVE[cid] = {"thread": t, "proc": None, "stop": False, "gate": {}}
        t.start()
        emit(cid, "planning resumed (#%d); no repository role had started" % o["resumes"])
        return None

    # A persisted review is already-completed work awaiting a verdict. Rebuild
    # only its wait loop; never auto-approve it and never rerun it on boot.
    review = next((r for r in o.get("roles") or [] if r.get("status") == "review"), None)
    if review:
        if automatic:
            return "operator review is still required"
        o["status"] = "review"
        o["next_action"] = "Approve, redo, or skip the gated role."
        _save(o)
        t = threading.Thread(target=_resume_review_then_run,
                             args=(cid, review["id"]), daemon=True)
        with LOCK:
            LIVE[cid] = {"thread": t, "proc": None, "stop": False, "gate": {}}
        t.start()
        return None

    reset = 0
    for r in o["roles"]:
        if r["status"] in ("failed", "blocked", "working", "retrying", "repairing",
                            "waiting_permission", "exhausted", "stopped"):
            # A role cut off mid-task still has its provider session: hand
            # it back so it CONTINUES (context intact) instead of starting over.
            # A role that genuinely failed re-runs fresh from its mission.
            if r["status"] in ("exhausted", "working", "stopped") and r.get("session"):
                r["continue_from"] = r["session"]
                r["continues"] = 0        # fresh continue budget for this attempt
            else:
                r["result"] = ""          # drop stale error text; re-run fresh
            r["status"] = "pending"
            r.pop("limit", None)
            r.pop("detail", None)
            r["next_action"] = "Role queued after resume."
            reset += 1
    if not reset:
        return "nothing to resume — every role is already done or skipped"
    o["status"] = "running"
    o["detail"] = ""
    o["next_action"] = "Resuming unfinished roles; completed roles are retained."
    o["resumes"] = o.get("resumes", 0) + 1
    _save(o)
    t = threading.Thread(target=_run, args=(cid,), daemon=True)
    with LOCK:
        LIVE[cid] = {"thread": t, "proc": None, "stop": False, "gate": {}}
    t.start()
    kept = len([r for r in o["roles"] if r["status"] in ("done", "skipped")])
    emit(cid, "resumed (#%d) — re-running %d role(s), keeping %d completed"
         % (o["resumes"], reset, kept))
    return None


def _mint_guard_approval(action, cid, role_id, request_id, minutes=15):
    """Mint one short-lived, server-derived Maestro guard token atomically."""
    action = str(action or "").lower()
    if action not in agent_runtime.GUARD_APPROVAL_ACTIONS:
        raise ValueError("permission request is not an allowlisted guard action")
    now = time.time()
    try:
        doc = _load_json(APPROVALS)
    except (OSError, json.JSONDecodeError):
        doc = {"tokens": []}
    tokens = []
    for token in doc.get("tokens", []):
        if not isinstance(token, dict):
            continue
        try:
            active = float(token.get("expires") or 0) > now
        except (TypeError, ValueError):
            active = False
        if active:
            tokens.append(token)
    expires = now + max(1, min(30, int(minutes))) * 60
    tokens.append({
        "action": action,
        "expires": expires,
        "minted": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "ceo-permission",
        "cid": str(cid),
        "role": str(role_id),
        "request_id": str(request_id),
    })
    # The guard only needs a tiny active-token window; never let stale UI
    # decisions turn this operational file into an unbounded audit log.
    doc = {"tokens": tokens[-32:]}
    os.makedirs(os.path.dirname(APPROVALS), exist_ok=True)
    temp_path = APPROVALS + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, APPROVALS)
    return expires


def _permission_target(o, role_id):
    """Resolve only a server-persisted waiting role; never trust UI scope."""
    waiting = [role for role in o.get("roles") or []
               if role.get("status") == "waiting_permission"]
    role_id = str(role_id or "")
    if role_id:
        role = next((item for item in waiting if item.get("id") == role_id), None)
        return role, ("named role is not waiting for permission" if not role else None)
    if len(waiting) == 1:
        return waiting[0], None
    if waiting:
        return None, "role is required when more than one permission is waiting"
    if not o.get("roles") and o.get("status") == "waiting_permission":
        return None, None  # planner permission failure
    return None, "mission has no persisted permission request"


def _reset_permission_dependents(o):
    """Requeue roles parked only because a permission-blocked dependency stopped."""
    for role in o.get("roles") or []:
        if (role.get("status") == "blocked" and
                str(role.get("detail") or "").startswith("blocked:")):
            role["status"] = "pending"
            role["detail"] = ""
            role["next_action"] = "Waiting for dependencies."


def permission_decision(cid, role_id, decision, request_id):
    """Resolve a permission wait after its worker thread has exited.

    allow: authorize only the persisted role/scope, then retry it with provider
           prompts skipped. A Maestro guard request also gets one 15-minute,
           named token derived from server-held evidence.
    retry: operator fixed an external prerequisite; rerun without escalation.
    deny:  skip the named role and continue only its unfinished dependents.
    """
    decision = str(decision or "").lower()
    if decision not in ("allow", "retry", "deny"):
        return "unknown permission decision"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(cid or "")):
        return "invalid mission id"
    request_id = str(request_id or "")
    if not re.fullmatch(r"(?:pr|legacy)_[a-f0-9]{32}", request_id):
        return "valid permission request_id is required"
    thread = None
    target = None
    request = None
    with LOCK:
        if cid in LIVE:
            return "permission worker is still settling; refresh and try again"
        try:
            o = _load_json(_path(cid))
        except FileNotFoundError:
            return "no such mission"
        except (OSError, json.JSONDecodeError):
            return "mission file unreadable"
        target, error = _permission_target(o, role_id)
        if error:
            return error
        request = dict((target or o).get("permission_request") or
                       o.get("permission_request") or {})
        request = _normalize_persisted_permission_request(o, target, request)
        if request.get("request_id") != request_id:
            return "permission request is stale; refresh Mission Activity"
        if request.get("status") not in (None, "pending"):
            return "permission request was already resolved"
        if decision == "allow" and not bool(request.get("can_authorize", False)):
            return ("this request cannot be authorized in Rune; fix the external "
                    "prerequisite, then use Retry")

        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        expires = None
        if decision == "allow":
            if request.get("kind") == "guard":
                try:
                    expires = _mint_guard_approval(
                        request.get("scope"), cid,
                        (target or {}).get("id") or "planner",
                        request.get("request_id"))
                except (OSError, ValueError, TypeError) as exc:
                    return "could not persist the scoped guard approval: %s" % type(exc).__name__
            else:
                expires = time.time() + 15 * 60

        request.update(status={"allow": "allowed", "retry": "retrying",
                               "deny": "denied"}[decision],
                       resolved_at=now_iso, decision=decision)
        if expires is not None:
            request["expires_at"] = datetime.datetime.fromtimestamp(
                expires).isoformat(timespec="seconds")
        authorization = {
            "authorization_id": "auth_" + uuid.uuid4().hex,
            "request_id": str(request.get("request_id") or ""),
            "decision": decision,
            "role_id": str((target or {}).get("id") or "planner"),
            "kind": str(request.get("kind") or "provider"),
            "scope": str(request.get("scope") or "provider-tools"),
            "workdir": os.path.normpath(os.path.realpath(o.get("workdir") or ROOT)),
            "at": now_iso,
            "status": "active" if decision == "allow" else "recorded",
        }
        if request.get("expires_at"):
            authorization["expires_at"] = request["expires_at"]
        history = o.setdefault("permission_authorizations", [])
        history.append(authorization)
        del history[:-12]
        o["permission_request"] = dict(request)
        o["resumes"] = o.get("resumes", 0) + (0 if decision == "deny" else 1)

        if target is None:  # planner permission failure
            if decision == "deny":
                o["status"] = "stopped"
                o["detail"] = "CEO planning permission was denied by the operator."
                o["next_action"] = "Archive this mission or start a narrower one."
                _save(o)
            else:
                o["status"] = "planning"
                o["detail"] = ""
                o["next_action"] = "Retrying the bounded CEO staffing plan."
                _save(o)
                thread = threading.Thread(target=_plan_then_run, args=(cid,), daemon=True)
                LIVE[cid] = {"thread": thread, "proc": None, "stop": False, "gate": {}}
        else:
            target["permission_request"] = dict(request)
            if decision == "allow":
                # A Maestro guard token authorizes one evidence-derived action;
                # never widen that into blanket Claude/Codex provider bypass.
                # Generic provider/protected prompts do require a bounded role
                # override so the exact retry cannot hit the same CLI dead end.
                if request.get("kind") in ("provider", "protected"):
                    target["permission_mode"] = "skip"
                target["operator_authorization"] = authorization
            if decision == "deny":
                target["status"] = "skipped"
                target["detail"] = "permission request denied by the operator"
                target["next_action"] = "Role skipped; unfinished dependents may continue."
            else:
                target["status"] = "pending"
                target["result"] = ""
                target.pop("limit", None)
                target["detail"] = ""
                target["next_action"] = "Role queued after operator decision."
            _reset_permission_dependents(o)
            o["status"] = "running"
            o["detail"] = ""
            o["next_action"] = ("Continuing after the denied role was skipped."
                                if decision == "deny" else
                                "Resuming the operator-resolved permission role.")
            _save(o)
            thread = threading.Thread(target=_run, args=(cid,), daemon=True)
            LIVE[cid] = {"thread": thread, "proc": None, "stop": False, "gate": {}}
    if thread:
        thread.start()
    emit(cid, "permission %s for %s (%s)" % (
        decision, authorization["role_id"], authorization["scope"]))
    return None


def _record_path(cid):
    """Locate an active or archived mission without accepting a path."""
    cid = str(cid or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cid):
        return ""
    active = _path(cid)
    archived = os.path.join(ADIR, cid + ".json")
    if os.path.isfile(active):
        return active
    if os.path.isfile(archived):
        return archived
    return ""


def _save_record(record, path):
    """Atomically update a record in its current active/archive location."""
    record["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=1)
    os.replace(temp_path, path)


def public_run(record):
    """Return a browser-safe mission copy without confirmation secrets."""
    clean = json.loads(json.dumps(record if isinstance(record, dict) else {}))
    clean.pop("git_baseline", None)
    latest_request = None
    for role in clean.get("roles") or []:
        if role.get("status") != "waiting_permission":
            continue
        request = role.get("permission_request")
        request = _normalize_persisted_permission_request(clean, role, request)
        role["permission_request"] = request
        latest_request = request
    top_request = clean.get("permission_request")
    if clean.get("status") == "waiting_permission":
        if latest_request is not None:
            clean["permission_request"] = dict(latest_request)
        else:
            clean["permission_request"] = _normalize_persisted_permission_request(
                clean, None, top_request)
    if isinstance(clean.get("delivery"), dict):
        clean["delivery"] = delivery.public_delivery(clean["delivery"])
    return clean


def _delivery_repo_key(record):
    record = record if isinstance(record, dict) else {}
    baseline = record.get("git_baseline")
    root = baseline.get("repo_root") if isinstance(baseline, dict) else ""
    value = str(root or record.get("workdir") or "").strip()
    return os.path.normcase(os.path.realpath(value)) if value else ""


def _peer_repo_busy(cid, record):
    wanted = _delivery_repo_key(record)
    if not wanted:
        return False
    for other_cid, state in list(LIVE.items()):
        thread = state.get("thread") if isinstance(state, dict) else None
        if other_cid == cid or not thread or not thread.is_alive():
            continue
        path = _record_path(other_cid)
        try:
            other = _load_json(path) if path else {}
        except (OSError, json.JSONDecodeError):
            other = {}
        if _delivery_repo_key(other) == wanted:
            return True
    for other_cid in list(DELIVERY_BUSY):
        if other_cid == cid:
            continue
        path = _record_path(other_cid)
        try:
            other = _load_json(path) if path else {}
        except (OSError, json.JSONDecodeError):
            other = {}
        if _delivery_repo_key(other) == wanted:
            return True
    return False


def delivery_action(cid, act, message="", token=""):
    """Run one guarded delivery transition for a completed mission."""
    cid = str(cid or "").strip()
    path = _record_path(cid)
    if not path:
        return None, "no such completed mission"
    record = None
    with LOCK:
        live = LIVE.get(cid) or {}
        thread = live.get("thread")
        if thread and thread.is_alive():
            return None, "mission is still running"
        if cid in DELIVERY_BUSY:
            return None, "another delivery action is already running"
        try:
            record = _load_json(path)
        except (OSError, json.JSONDecodeError):
            return None, "mission record is unreadable"
        if str(record.get("status") or "").lower() not in SUCCESSFUL:
            return None, "delivery is available only for successful missions"
        DELIVERY_BUSY.add(cid)
        peer_active = _peer_repo_busy(cid, record)
        if peer_active:
            DELIVERY_BUSY.discard(cid)
            return None, "another mission or delivery action is using this repository"
    result, error = None, None
    try:
        result = delivery.perform(record, act, message=message, token=token,
                                  peer_active=peer_active)
    except delivery.DeliveryError as exc:
        error = str(exc)
    except Exception as exc:
        error = "delivery action crashed: %s" % type(exc).__name__
    finally:
        with LOCK:
            try:
                _save_record(record, path)
            except OSError:
                if not error:
                    error = "delivery result could not be persisted"
            DELIVERY_BUSY.discard(cid)
    if error:
        # Delivery transitions deliberately persist any state they managed to
        # reach before failing (for example, tests=unavailable or push=failed).
        # Return that browser-safe state with the error so the workbench can
        # render a durable next step instead of leaving a stale "pending" card
        # behind a short-lived toast.
        return {"delivery": delivery.public_delivery(
            record.get("delivery") or {})}, error
    payload = dict(result or {})
    payload["delivery"] = delivery.public_delivery(record.get("delivery") or {})
    return payload, None


def delivery_fix(cid):
    """Spawn one solo fixer mission from a persisted failed delivery step.

    The brief is derived entirely from server-held evidence — the browser
    supplies only the mission id, matching the delivery lane's trust model."""
    path = _record_path(cid)
    if not path:
        return None, "no such completed mission"
    try:
        record = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return None, "mission record is unreadable"
    lane = record.get("delivery") if isinstance(record.get("delivery"), dict) else {}
    step, evidence = "", ""
    for name in ("review", "tests", "commit", "push"):
        item = lane.get(name) if isinstance(lane.get(name), dict) else {}
        if item.get("status") in ("failed", "unavailable", "stale"):
            step = name
            evidence = str(item.get("output") or item.get("error") or
                           item.get("detail") or "")
            break
    commit_blocked = str((lane.get("commit") or {}).get("blocked_reason") or "")
    if not step and commit_blocked:
        step = "commit"
    if step == "commit" and commit_blocked:
        # This is delivery.py's attribution boundary, not a code defect: the
        # gate refuses to auto-commit paths that were already dirty before the
        # mission started (the operator's own work in progress), and no
        # in-mission edit can ever change that fact. Spawning a fixer agent
        # here cannot succeed — it can only edit those same excluded paths and
        # burn cost re-discovering the same block. Surface it directly instead.
        return None, ("Commit is blocked, not broken: %s No agent can fix this "
                      "from inside the mission — stage and commit those paths "
                      "yourself in Git, then Review again." % commit_blocked)
    if not step:
        return None, "no failed delivery step to fix"
    workdir = str(record.get("workdir") or "")
    if not os.path.isdir(workdir):
        return None, "mission working directory is unavailable"
    goal = ("Fix the failing delivery step '%s' for the completed mission "
            "\"%s\" in this repository.\n\nOriginal mission goal:\n%s\n\n"
            "Failure evidence:\n%s\n\nDiagnose the root cause, apply the "
            "minimal fix, and re-run the failing check yourself to confirm it "
            "passes. Do not commit and do not push." % (
                step, str(record.get("name") or cid)[:80],
                str(record.get("goal") or "")[:1500],
                agent_runtime.safe_excerpt(evidence, 2500)))
    return plan_and_start(goal, {"mode": "solo", "refine": "off"},
                          workdir=workdir)


def action(cid, role_id, act, feedback="", request_id=""):
    """Operator verdicts, including persisted permission decisions."""
    if act in ("allow", "retry", "deny"):
        # Permission scope always comes from persisted server evidence. Feedback
        # is deliberately ignored here so credentials or broader instructions
        # cannot be smuggled into an authorization receipt.
        return permission_decision(cid, role_id, act, request_id)
    if act == "resume":
        return resume(cid)  # valid precisely when the mission is NOT live
    if act == "archive":
        return archive(cid)
    proc = None
    with LOCK:
        st = LIVE.get(cid)
        if not st:
            return "not running (finished or server restarted)"
        if act == "stop":
            st["stop"] = True
            proc = st.get("proc")
        elif act in ("approve", "redo", "skip"):
            st["gate"][role_id] = (act, feedback)
            return None
        else:
            return "unknown action"
    if act == "stop":
        agent_runtime.terminate_process_tree(proc)
        try:
            o = _load_json(_path(cid))
            _stop_state(o)
        except (OSError, json.JSONDecodeError):
            pass
        return None
    return "unknown action"


def recover_stalled_on_boot():
    """Resume only crash-interrupted planning/working states marked recoverable.

    Review and permission waits are deliberately left gated. New missions carry
    ``auto_recover``; legacy files without it are never restarted implicitly.
    Returns the ids successfully scheduled, primarily for diagnostics/tests.
    """
    recovered = []
    if not os.path.isdir(CDIR):
        return recovered
    for fn in os.listdir(CDIR):
        if not fn.endswith(".json"):
            continue
        try:
            o = _load_json(os.path.join(CDIR, fn))
        except (OSError, json.JSONDecodeError):
            continue
        if not o.get("auto_recover"):
            continue
        states = {r.get("status") for r in o.get("roles") or []}
        if states & {"review", "waiting_permission"}:
            continue
        interrupted = (o.get("status") == "planning" or
                       (o.get("status") in ("running", "stalled") and
                        bool(states & {"working", "retrying", "repairing"})) or
                       (o.get("route") == "direct" and o.get("status") == "running" and
                        bool(states & {"pending"})))
        if interrupted and resume(o.get("cid") or fn[:-5], automatic=True) is None:
            recovered.append(o.get("cid") or fn[:-5])
    return recovered


if __name__ != "__main__" and os.environ.get("RUNE_DISABLE_BOOT_RECOVERY") != "1":
    recover_stalled_on_boot()


if __name__ == "__main__":
    # self-check: schemas serialize, model tiers sane, no API call needed
    json.dumps(REFINE_SCHEMA), json.dumps(PLAN_SCHEMA)
    assert "opus" in ROLE_MODELS and "fable" in ROLE_MODELS
    assert "fold" in PLAN_SYSTEM.lower() and "hermes" in PLAN_SYSTEM.lower()
    # the brain gate: keep signal, drop noise
    trivial = {"status": "done", "cost": 0.05,
               "roles": [{"model": "haiku", "turns": 8, "status": "done"}]}
    hard = {"status": "done", "cost": 0.05,
            "roles": [{"model": "opus", "turns": 30, "status": "done"}]}
    failed = {"status": "failed", "cost": 0.01,
              "roles": [{"model": "haiku", "turns": 5, "status": "failed"}]}
    assert not _worth_remembering(trivial), "cheap mechanical success should be skipped"
    assert _worth_remembering(hard), "opus reasoning should be kept"
    assert _worth_remembering(failed), "failures should be kept"
    # archive age math: an 8-day-old run is past the 7-day window, a fresh one isn't
    old = {"updated": (datetime.datetime.now() - datetime.timedelta(days=8)).isoformat()}
    assert _age_days(old) >= AUTO_ARCHIVE_DAYS and _age_days({"updated": ""}) == 0.0

    # ---- the regression that started all this: a role that burns its whole turn
    # budget was reported as a SILENT SUCCESS on half-finished work. It must now
    # auto-continue the same session, then park as "exhausted" with a reason.
    import tempfile
    CDIR = tempfile.mkdtemp()                      # run against a scratch dir
    calls = []

    def _worker(cid, role, context, cfg_dir, resume_sid="", workdir=ROOT,
                safe_permissions=False):   # never runs claude
        calls.append(resume_sid)
        return {"is_error": True, "subtype": "error_max_turns", "result": "",
                "session_id": "sess1", "total_cost_usd": 0.01}

    def emit(*a, **kw):
        pass

    pulse.least_used = lambda: ""
    role = {"id": "solo", "title": "T", "mission": "m", "model": "sonnet", "turns": 5,
            "depends_on": [], "review": False, "status": "pending", "result": "",
            "secs": 0, "cost": 0}
    _save({"cid": "t1", "name": "t", "goal": "g", "roles": [role], "status": "running",
           "cost": 0, "started": datetime.datetime.now().isoformat()})
    LIVE["t1"] = {"thread": threading.current_thread(), "proc": None,
                  "stop": False, "gate": {}}
    _run("t1")
    got = _load_json(_path("t1"))
    r = got["roles"][0]
    assert r["status"] == "exhausted", "out of turns was reported as %r" % r["status"]
    assert got["status"] == "exhausted" and r["detail"] and got["detail"]
    assert calls == [""] + ["sess1"] * MAX_CONTINUES, calls   # continued, not restarted
    assert r["cost"] == round(0.01 * (1 + MAX_CONTINUES), 4)  # every attempt billed
    # Continue = resume that same claude session, not re-run the mission from zero
    assert resume("t1") is None
    with LOCK:
        t = LIVE["t1"]["thread"]
    t.join(timeout=10)
    assert calls[1 + MAX_CONTINUES] == "sess1", "Continue restarted instead of resuming"
    assert _load_json(_path("t1"))["resumes"] == 1
    print("ceo.py OK — key present:", bool(chat._api_key()))
