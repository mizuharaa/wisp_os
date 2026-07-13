#!/usr/bin/env python3
"""The shared model client — the one place Rune talks to Claude.

Every module that reasons with a model routes through here so four policies are
enforced once, not re-litigated per caller:

1. Model swap is a ONE-LINE change. `MODEL` points at Fable 5 today; flip it to
   MODEL_IDS["opus"] for the 1-week Opus upgrade. Nothing else changes.
2. `max_tokens` is clamped to 64K-100K per call (below that Fable under-answers
   hard tasks; above 100K raw-HTTP turns risk timing out).
3. Effort is a ladder low->max. `xhigh` is gated: it is only allowed when the
   caller sets `agentic_loop=True`, otherwise it degrades to `high`.
4. Refusals NEVER crash a caller loop. A safety decline (or an HTTP error) is
   collapsed into a 200-shaped envelope carrying `stopped_reason` — so an
   orchestration loop keeps its footing and re-routes instead of exiting.

Stdlib only, raw urllib against the Anthropic API (repo rule: no deps), matching
dashboard/ceo.py and dashboard/chat.py.
"""
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.anthropic.com/v1/messages"

# The four tiers Rune staffs from, as {short name: API id}. Short names are what
# the escalation helper and `claude -p --model` speak; ids are what the API wants.
MODEL_IDS = {
    "haiku": "claude-haiku-4-5",    # mechanical / cheap scan
    "sonnet": "claude-sonnet-5",    # light or logistics work
    "opus": "claude-opus-4-8",      # hard implementation / the manager
    "fable": "claude-fable-5",      # frontier-complex reasoning
}

# === THE ONE LINE ===  swap to MODEL_IDS["opus"] for the 1-week Opus upgrade.
MODEL = MODEL_IDS["fable"]

# Effort ladder, cheapest to deepest. `xhigh` sits between high and max and is
# reserved for agentic loops (it burns tokens exploring); see resolve_effort.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

MAX_TOKENS_FLOOR = 64_000
MAX_TOKENS_CEIL = 100_000


def resolve_effort(effort="high", agentic_loop=False):
    """Validate an effort level and apply the xhigh gate. Unknown value -> error
    (config bug should surface loudly); xhigh without the agentic flag -> high."""
    if effort not in EFFORT_LEVELS:
        raise ValueError("effort must be one of %s, got %r" % (EFFORT_LEVELS, effort))
    if effort == "xhigh" and not agentic_loop:
        return "high"
    return effort


def clamp_max_tokens(n):
    """Force a per-call output budget into the allowed 64K-100K band."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = MAX_TOKENS_FLOOR
    return max(MAX_TOKENS_FLOOR, min(MAX_TOKENS_CEIL, n))


# --- escalation tiering: cheap scan -> manager plan -> delegate by complexity ---
# The manager never does the work; a haiku scan sizes the task, opus plans, then
# the build is handed to the tier that fits. Complexity is a coarse label so the
# whole plan stays JSON-serializable and cheap to reason about.
_DELEGATE = {"trivial": "haiku", "light": "sonnet", "hard": "opus", "frontier": "fable"}


def escalate(complexity="hard", agentic_loop=False):
    """Return the escalation plan for a task of the given complexity. Callers run
    the stages in order and can stop early when the scan says the task is trivial."""
    return {
        "scan": "haiku",                                  # cheap first pass, always
        "plan": "opus",                                   # manager decomposes
        "delegate": _DELEGATE.get(complexity, "opus"),    # who builds it
        "effort": resolve_effort("high", agentic_loop),
    }


def normalize(data, http_status=200, model=MODEL):
    """Collapse EVERY outcome — clean answer, refusal, or HTTP error — into one
    200-shaped envelope. `stopped_reason` is None only on a clean end_turn; on a
    refusal, error, or truncation it names the reason so the caller's loop keeps
    running and re-routes instead of raising. This is the whole point of the
    module: a model saying "no" must not read as the program crashing."""
    stop = data.get("stop_reason")
    text = "".join(b.get("text", "") for b in (data.get("content") or [])
                   if b.get("type") == "text")
    if http_status != 200:
        stopped = "http_%s" % http_status
    elif data.get("type") == "error":
        stopped = "api_error"
    elif stop in ("refusal", "max_tokens", "pause_turn",
                  "model_context_window_exceeded"):
        stopped = stop
    else:
        stopped = None  # end_turn / tool_use / stop_sequence — nothing to route around
    detail = None
    if data.get("type") == "error":
        detail = (data.get("error") or {}).get("message")
    elif stop == "refusal":
        detail = (data.get("stop_details") or {}).get("explanation") or "safety refusal"
    return {
        "status": 200,               # ALWAYS 200 to the caller, by design
        "stopped_reason": stopped,
        "text": text,
        "stop_reason": stop,
        "model": data.get("model") or model,
        "usage": data.get("usage") or {},
        "detail": detail,
    }


def _api_key():
    """ANTHROPIC_API_KEY from the environment, else the gitignored <ROOT>/.env."""
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k.strip()
    try:
        for line in open(os.path.join(ROOT, ".env"), encoding="utf-8"):
            m = re.match(r"\s*ANTHROPIC_API_KEY\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def complete(prompt, system=None, max_tokens=MAX_TOKENS_FLOOR, effort="high",
             agentic_loop=False, model=None, schema=None, timeout=600):
    """One Messages call, always returning a normalize() envelope (never raising
    on a refusal or HTTP error). Fable 5 thinking is always on, so the `thinking`
    param is omitted; depth is controlled by effort. `schema` opts into
    structured JSON output.

    ponytail: non-streaming. Fine for the bounded structured outputs the
    substrate makes; switch to streaming if a caller wants long free-form 100K
    generations and hits the timeout."""
    model = model or MODEL
    key = _api_key()
    if not key:
        return normalize({"type": "error", "error": {"message":
                          "no ANTHROPIC_API_KEY (set it in the environment or .env)"}},
                         http_status=200, model=model)
    body = {
        "model": model,
        "max_tokens": clamp_max_tokens(max_tokens),
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": resolve_effort(effort, agentic_loop)},
    }
    if system:
        body["system"] = system
    if schema:
        body["output_config"]["format"] = {"type": "json_schema", "schema": schema}
    req = urllib.request.Request(API, data=json.dumps(body).encode("utf-8"), headers={
        "content-type": "application/json", "x-api-key": key,
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return normalize(json.load(r), http_status=r.status, model=model)
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "ignore"))
        except (json.JSONDecodeError, ValueError):
            data = {"type": "error", "error": {"message": "HTTP %s" % e.code}}
        return normalize(data, http_status=e.code, model=model)
    except Exception as e:
        return normalize({"type": "error", "error": {"message":
                          type(e).__name__ + ": " + str(e)[:200]}},
                         http_status=200, model=model)


if __name__ == "__main__":
    # self-check — no API call needed. Proves the four policies hold.

    # 1. model swap is a single constant, pointing at a real id
    assert MODEL == MODEL_IDS["fable"], "MODEL must default to Fable 5"
    assert MODEL_IDS["opus"] == "claude-opus-4-8"

    # 2. max_tokens config: clamped to the 64K-100K band
    assert clamp_max_tokens(1000) == 64_000, "below floor -> floor"
    assert clamp_max_tokens(500_000) == 100_000, "above ceil -> ceil"
    assert clamp_max_tokens(80_000) == 80_000, "in-band passes through"
    assert clamp_max_tokens("nonsense") == 64_000, "garbage -> floor"

    # 3. effort ladder: xhigh gated behind the agentic-loop flag; unknown errors
    assert resolve_effort("low") == "low"
    assert resolve_effort("max") == "max"
    assert resolve_effort("xhigh") == "high", "xhigh must degrade without the flag"
    assert resolve_effort("xhigh", agentic_loop=True) == "xhigh", "flag unlocks xhigh"
    try:
        resolve_effort("turbo")
        assert False, "unknown effort should raise"
    except ValueError:
        pass

    # 4. THE CRITICAL ONE: a simulated refusal -> HTTP 200 + stopped_reason present
    refusal = {"stop_reason": "refusal", "content": [],
               "stop_details": {"type": "refusal", "category": "cyber",
                                "explanation": "declined"},
               "model": MODEL}
    out = normalize(refusal)
    assert out["status"] == 200, "refusal must still be 200 to the caller"
    assert out["stopped_reason"] == "refusal", "stopped_reason must flag the refusal"
    assert out["detail"] == "declined"

    # an HTTP error is likewise a 200 envelope the loop can survive
    err = normalize({"type": "error", "error": {"message": "boom"}}, http_status=500)
    assert err["status"] == 200 and err["stopped_reason"] == "http_500"

    # a clean answer has stopped_reason None (nothing to route around)
    ok = normalize({"stop_reason": "end_turn",
                    "content": [{"type": "text", "text": "hi"}]})
    assert ok["stopped_reason"] is None and ok["text"] == "hi"

    # escalation tiering: cheap scan -> manager plan -> delegate by complexity
    plan = escalate("frontier")
    assert plan["scan"] == "haiku" and plan["plan"] == "opus"
    assert plan["delegate"] == "fable" and plan["effort"] == "high"
    assert escalate("light")["delegate"] == "sonnet"
    assert escalate("hard", agentic_loop=True)["effort"] == "high"  # non-xhigh input

    print("model_client.py OK — MODEL=%s | key present: %s"
          % (MODEL, bool(_api_key())))
